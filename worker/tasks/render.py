"""
Final render — cut the selected speaker's segments and stitch them into
a single mp4.

Public entry point: `run_render(job_id, job_dir)`. Called from the
`render_video` Celery task in worker/tasks/dummy.py, which owns its own
try/finally cleanup.

Pipeline (matches the M6 spec):
  1. Transition to RENDERING (0%, "Preparing your video segments...").
  2. Pull job: selected_speaker, artifacts.source_video_key.
     Pull segments where speaker == selected_speaker, sorted by start.
  2b. Voice-print reclamation (worker/tasks/reclaim.py): re-verify every
     other-speaker span against the selected speaker's voice embedding and
     pull back turns diarization mislabeled. Additive-only, best-effort.
  3. Final merge pass on the segments (gap < 2.0s -> merge), giving
     the cut list of (start, end) pairs.
  4. Download source.mp4 from R2 to job_dir. Progress 20%.
  5. For each segment: re-encode with libx264 / aac to seg_NNNN.mp4.
     Re-encoding is intentional (not -c copy) for frame-accurate cuts.
     Progress 20-80% across all segments.
  6. Write concat_list.txt referencing the seg files.
  7. ffmpeg -f concat -safe 0 -i list -c copy final.mp4. Progress 90%.
  8. Upload final.mp4 to R2 at r2_key_final_video. Stamp
     artifacts.final_video_key on the job.
  9. Stream-copy final.mp4's audio to final_audio.m4a and upload it
     (artifacts.final_audio_key) — the recommendations LLM reads audio, not
     video. Best-effort.
 10. Reclaim the ephemeral intermediates: delete source.mp4 and the whole
     snippets/ prefix, unsetting their keys. Best-effort. Together with
     audio.wav (deleted in pipeline.py) that is 98% of a job's storage.
 11. Transition to DONE (100%, "Your video is ready!").

Skip path: when source_video_key is null OR there are no segments for
the selected speaker, we transition straight to DONE with
artifacts.final_video_key unset. The frontend's M2 "Download link will
appear..." placeholder kicks in. This branch only runs in the Emergent
dev container (where M3 ingest doesn't have a real source on R2);
production never hits it.

Errors raise RenderError with a user-facing message; the orchestrator
catches and marks the job FAILED.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from worker.db import get_db
from worker.state import progress, transition
from worker.utils.ffmpeg import FFmpegError, detect_silence, run_ffmpeg
from worker.utils.storage import delete_prefix, download_file, upload_file
from shared.constants import (
    JobStatus,
    r2_key_final_audio,
    r2_key_final_video,
    r2_prefix_ephemeral,
    r2_key_transcript,
    r2_key_transcription,
)

logger = logging.getLogger(__name__)

FINAL_MERGE_GAP_SEC = 2.0  # spec step 3b

# Extra padding applied at RENDER time (on top of diarize.py PAD_SEC) so
# leading/trailing words aren't clipped at cut boundaries. Applied here
# rather than in diarize.py so existing jobs can be fixed with a cheap
# re-render instead of a full GPU re-diarization. Any overlaps these pads
# create between adjacent kept segments are absorbed by final_merge_pass.
RENDER_START_PAD_SEC = 0.50  # lead-in so word onsets aren't clipped
RENDER_END_PAD_SEC = 1.00    # larger tail — trailing syllables were getting cut

# Gap-aware bridging: when two of the selected speaker's segments are
# separated by a gap that NO other speaker occupies, that gap is almost
# always the speaker's own speech that diarization failed to assign (a
# "no-speaker" drop) — so we bridge it back in. Gaps that another speaker
# occupies are genuine turn-taking and are left cut.
MAX_BRIDGE_SEC = 20.0             # only bridge clear gaps up to this length
BRIDGE_OTHER_TOLERANCE_SEC = 0.5  # gap counts as "clear" if <= this much
                                  # other-speaker audio falls inside it

# Silence-aware edge extension (Phase 1, recall-first): grow each selected
# turn outward into adjacent no-speaker space, but only over audio that is
# actually NOT silent — stopping at the first real silence gap (or the next
# speaker). Unlike a fixed extension this adapts to the true length of the
# speaker's dropped speech and adds no dead air.
SILENCE_NOISE_DB = -30.0          # below this level counts as silence
SILENCE_MIN_SILENCE_SEC = 0.5     # shortest silence gap that stops extension
SILENCE_EXTEND_MAX_SEC = 20.0     # safety cap on how far a turn may grow per side

# Talk-over recovery: when another speaker starts BEFORE the selected speaker's
# turn boundary (they briefly talk over each other), the selected speaker's own
# leading/trailing words are usually still audible under/around the other voice.
# The silence-aware extension would otherwise stop dead at the overlap and clip
# those words — e.g. an analyst's final stop-loss number cut off the instant the
# anchor cuts in. We instead recover up to this many seconds INTO the overlap,
# per side. Bleed is bounded and still halts at the first real silence
# (recall over precision — a couple of seconds of the other voice is acceptable).
OVERLAP_EXTEND_SEC = 3.0


class RenderError(Exception):
    """User-facing render failure with an error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_render(job_id: str, job_dir: Path) -> None:
    """Render the selected speaker's segments into a single mp4."""
    transition(
        job_id, JobStatus.RENDERING.value,
        stage="rendering", percent=0.0,
        message="Preparing your video segments...",
    )

    db = get_db()
    job = db.jobs.find_one(
        {"job_id": job_id},
        {"selected_speaker": 1, "artifacts": 1, "duration_sec": 1, "_id": 0},
    ) or {}
    selected = job.get("selected_speaker")
    source_key = (job.get("artifacts") or {}).get("source_video_key")
    duration_sec = float(job.get("duration_sec") or 0.0)

    if not selected:
        raise RenderError(
            "NO_SPEAKER_SELECTED",
            "No speaker has been selected for this job.",
        )

    # Load ALL speakers' segments — we need the other speakers' timing to
    # decide which gaps inside the selected speaker are "clear" (bridgeable)
    # versus genuine turn-taking (leave cut).
    all_segments = list(
        db.segments
          .find({"job_id": job_id}, {"_id": 0, "start": 1, "end": 1, "speaker": 1})
          .sort("start", 1)
    )
    raw_segments = [s for s in all_segments if s.get("speaker") == selected]
    other_segments = [s for s in all_segments if s.get("speaker") != selected]

    # ---- Skip path (dev fallback) ----------------------------------------
    if not source_key or not raw_segments:
        logger.warning(
            "render[%s] skipping render (source_key=%r, segments=%d) "
            "- this branch runs only in dev",
            job_id, source_key, len(raw_segments),
        )
        transition(
            job_id, JobStatus.DONE.value,
            stage="done", percent=100.0,
            message="Your video is ready!",
        )
        return

    # ---- Hot path --------------------------------------------------------
    # 4. Download the source FIRST — its audio is needed for silence detection.
    progress(job_id, percent=10.0, message="Downloading source from storage...")
    local_source = job_dir / "source.mp4"
    try:
        download_file(source_key, str(local_source))
    except Exception as exc:  # noqa: BLE001
        raise RenderError(
            "SOURCE_DOWNLOAD_FAILED",
            f"Could not download source video: {exc}",
        ) from exc

    # Detect silent intervals so we can extend turns over real (non-silent)
    # audio only. Best-effort: on any failure we fall back to pad + bridge.
    progress(job_id, percent=15.0, message="Analysing audio...")
    try:
        silences = detect_silence(
            str(local_source),
            noise_db=SILENCE_NOISE_DB,
            min_silence_sec=SILENCE_MIN_SILENCE_SEC,
            duration_sec=duration_sec or None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "render[%s] silence detection failed (%s); skipping "
            "silence-aware extension", job_id, exc,
        )
        silences = []

    # Reclaim mislabeled turns BEFORE building the cut list: diarization
    # sometimes assigns a turn of the selected speaker to another speaker's
    # cluster (job bc5ce57c lost the analyst's Titan numbers this way), and
    # no timing heuristic downstream can recover that. Voice-embedding
    # verification re-checks every other-speaker span against the selected
    # speaker's voice-print and returns the ranges that are really theirs.
    # Strictly additive and best-effort: on any failure the render proceeds
    # with the original labels, exactly as before.
    progress(job_id, percent=17.0, message="Verifying speaker attribution...")
    # Imported here, not at module top: importing via the worker.tasks
    # package runs its __init__ (Celery autodiscovery), which drags in the
    # full task chain (ingest -> yt_dlp) that dev/test venvs don't have.
    from worker.tasks.reclaim import reclaim_for_render, subtract_ranges

    reclaimed: list[dict[str, Any]] = []
    try:
        reclaimed = reclaim_for_render(
            job_id, local_source, job_dir, selected, all_segments,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "render[%s] attribution reclaim failed (%s); rendering with "
            "original labels", job_id, exc,
        )
    if reclaimed:
        raw_segments = sorted(
            raw_segments + [{"start": r["start"], "end": r["end"]} for r in reclaimed],
            key=lambda s: float(s["start"]),
        )
        # Reclaimed spans are the selected speaker's own speech — they must
        # no longer count as "another speaker" when extending/bridging.
        other_segments = subtract_ranges(other_segments, reclaimed)
        logger.info(
            "render[%s] attribution reclaim recovered %d range(s) "
            "totaling %.1fs", job_id, len(reclaimed),
            sum(r["end"] - r["start"] for r in reclaimed),
        )

    # Build the cut list: pad -> silence-aware extend -> merge -> bridge.
    # Padding catches words tight against boundaries; the silence-aware
    # extension recovers longer dropped spans by following non-silent audio
    # at turn edges and stopping at the first real silence.
    padded = apply_render_padding(
        raw_segments,
        start_pad=RENDER_START_PAD_SEC,
        end_pad=RENDER_END_PAD_SEC,
        duration_sec=duration_sec,
    )
    extended = silence_aware_extend(
        padded,
        other_segments,
        silences,
        max_extend_sec=SILENCE_EXTEND_MAX_SEC,
        duration_sec=duration_sec,
    )
    ext_gain = (
        sum(s["end"] - s["start"] for s in extended)
        - sum(s["end"] - s["start"] for s in padded)
    )
    if ext_gain > 0.05:
        logger.info(
            "render[%s] silence-aware extension recovered %.1fs of speech",
            job_id, ext_gain,
        )

    merged = final_merge_pass(extended, FINAL_MERGE_GAP_SEC)
    # Bridge clear (no-other-speaker) gaps to recover dropped solo speech.
    cut_list = bridge_clear_gaps(
        merged,
        other_segments,
        max_bridge_sec=MAX_BRIDGE_SEC,
        other_tolerance_sec=BRIDGE_OTHER_TOLERANCE_SEC,
    )
    bridged = len(merged) - len(cut_list)
    if bridged > 0:
        recovered = (
            sum(s["end"] - s["start"] for s in cut_list)
            - sum(s["end"] - s["start"] for s in merged)
        )
        logger.info(
            "render[%s] bridged %d clear gap(s), recovering %.1fs of "
            "likely-dropped solo speech",
            job_id, bridged, recovered,
        )
    if not cut_list:
        # Mostly unreachable (we already checked raw_segments was non-empty)
        raise RenderError(
            "EMPTY_CUT_LIST",
            "After post-processing there were no segments to render.",
        )

    progress(job_id, percent=20.0,
             message=f"Cutting {len(cut_list)} segments...")

    # 5. Cut + re-encode each segment (frame-accurate)
    seg_paths: list[Path] = []
    n = len(cut_list)
    for i, seg in enumerate(cut_list):
        seg_path = job_dir / f"seg_{i:04d}.mp4"
        try:
            run_ffmpeg([
                "-ss", f"{seg['start']:.3f}",
                "-to", f"{seg['end']:.3f}",
                "-i", str(local_source),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                str(seg_path),
                "-y",
            ])
        except FFmpegError as exc:
            raise RenderError(
                "ENCODE_FAILED",
                f"Failed to encode segment {i + 1}/{n}: {exc}",
            ) from exc
        if not seg_path.exists() or seg_path.stat().st_size == 0:
            raise RenderError(
                "ENCODE_EMPTY",
                f"Segment {i + 1}/{n} encoded to an empty file.",
            )
        seg_paths.append(seg_path)
        pct = 20.0 + ((i + 1) / n) * 60.0  # 20% -> 80%
        progress(
            job_id, percent=pct,
            message=f"Encoded segment {i + 1} of {n}",
        )

    # 6. Concat list file
    concat_list = job_dir / "concat_list.txt"
    # Each line: file 'absolute/path.mp4'
    # The single quotes mean ffmpeg won't try to interpret special chars;
    # we control the paths so no escaping concerns here.
    concat_list.write_text(
        "".join(f"file '{p}'\n" for p in seg_paths),
        encoding="utf-8",
    )

    # 7. Final concat (stream copy — all inputs share codec from step 5)
    progress(job_id, percent=85.0, message="Stitching final video...")
    final_path = job_dir / "final.mp4"
    try:
        run_ffmpeg([
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            str(final_path),
            "-y",
        ])
    except FFmpegError as exc:
        raise RenderError(
            "CONCAT_FAILED",
            f"Final concat failed: {exc}",
        ) from exc
    if not final_path.exists() or final_path.stat().st_size == 0:
        raise RenderError(
            "CONCAT_EMPTY",
            "Concat completed but final.mp4 is empty.",
        )

    progress(job_id, percent=90.0, message="Uploading final video...")

    # 8. Upload to R2
    final_key = r2_key_final_video(job_id)
    try:
        upload_file(str(final_path), final_key)
    except Exception as exc:  # noqa: BLE001
        raise RenderError(
            "UPLOAD_FAILED",
            f"Could not upload final video: {exc}",
        ) from exc

    db.jobs.update_one(
        {"job_id": job_id},
        {"$set": {"artifacts.final_video_key": final_key}},
    )

    # 9. Audio-only sidecar for the recommendations LLM (best-effort).
    #
    # Stream copy, so no re-encode and no quality loss. This exists because the
    # API service sends this file to Gemini instead of final.mp4: pass 1 only
    # transcribes speech, so shipping video frames costs ~3.6x the input tokens
    # for an identical transcript and makes the request much likelier to be
    # rejected with 503 when Gemini is busy. The API has no ffmpeg, so it has to
    # happen here.
    #
    # Deliberately non-fatal: the render itself has already succeeded and been
    # uploaded, so a failure here must not fail the job. The API falls back to
    # final.mp4 when artifacts.final_audio_key is absent (which is also the case
    # for every job rendered before this change).
    final_audio_path = job_dir / "final_audio.m4a"
    try:
        run_ffmpeg([
            "-i", str(final_path),
            "-vn",
            "-c:a", "copy",
            str(final_audio_path),
            "-y",
        ])
        if not final_audio_path.exists() or final_audio_path.stat().st_size == 0:
            raise FFmpegError("audio extraction produced an empty file")
        final_audio_key = r2_key_final_audio(job_id)
        upload_file(str(final_audio_path), final_audio_key)
        db.jobs.update_one(
            {"job_id": job_id},
            {"$set": {"artifacts.final_audio_key": final_audio_key}},
        )
        logger.info(
            "render[%s] final audio uploaded (%.1f MB) -> %s",
            job_id,
            final_audio_path.stat().st_size / 1_000_000.0,
            final_audio_key,
        )
    except Exception:  # noqa: BLE001 — best-effort sidecar; never fail the job.
        logger.warning(
            "render[%s] final audio extraction/upload failed; recommendations "
            "will fall back to final.mp4",
            job_id,
            exc_info=True,
        )

    # 10. Reclaim the ephemeral intermediates (best-effort).
    #
    # Both are dead at this point:
    #   * source.mp4 (~485 MB) — the render is uploaded and there is NO re-render
    #     path (select_speaker only accepts AWAITING_SELECTION), so nothing can
    #     ever ask for it again.
    #   * snippets/*.mp3 — only used by the pre-render speaker-selection UI.
    #
    # Together with audio.wav (deleted in pipeline.py) this is 98% of a job's
    # storage, which is what keeps the bucket inside R2's free tier. The
    # ephemeral/ lifecycle rule is only a backstop for jobs that crash or are
    # abandoned before reaching here.
    #
    # One prefix delete covers source.mp4, snippets/ AND any audio.wav that
    # process_video failed to remove.
    #
    # The artifact keys are unset alongside the objects so nothing keeps handing
    # out presigned URLs to bytes that no longer exist: job_service hydrates
    # speakers[].snippet_url off snippet_key, and the pipeline's resume check
    # reads artifacts.source_video_key.
    #
    # Never fatal — the job has already succeeded and been uploaded.
    try:
        n_removed = delete_prefix(r2_prefix_ephemeral(job_id))
        db.jobs.update_one(
            {"job_id": job_id},
            {"$unset": {
                "artifacts.source_video_key": "",
                "speakers.$[].snippet_key": "",
            }},
        )
        logger.info(
            "render[%s] reclaimed %d ephemeral object(s)", job_id, n_removed,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "render[%s] could not delete the ephemeral/ objects; the lifecycle "
            "rule will reclaim them",
            job_id, exc_info=True,
        )

    total_dur = sum(s["end"] - s["start"] for s in cut_list)
    video_duration = duration_sec
    try:
        final_size_mb = final_path.stat().st_size / 1_000_000.0
    except OSError:
        final_size_mb = 0.0

    logger.info(
        "render[%s] DONE: extracted %d segments totaling %.1f minutes "
        "from %.1f-hour video (final.mp4 = %.1f MB) -> %s",
        job_id,
        len(cut_list),
        total_dur / 60.0,
        (video_duration or 0) / 3600.0,
        final_size_mb,
        final_key,
    )

    # 8b. Build the downloadable transcript of the FINAL video.
    # The video is already uploaded and stamped above, so this is strictly
    # best-effort: any failure (missing transcript.json on a pre-feature job,
    # download/upload error) is logged and swallowed — the video still ships.
    # Selector is window-overlap, NOT speaker: anything audible in the final
    # video must appear, including no-speaker spans that bridging pulled in.
    progress(job_id, percent=95.0, message="Building transcript...")
    try:
        _build_final_transcript(db, job_id, job_dir, cut_list)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "render[%s] transcript build failed (%s); continuing — the "
            "video is unaffected, the transcript just won't be available",
            job_id, exc,
        )

    # 11. Finish
    transition(
        job_id, JobStatus.DONE.value,
        stage="done", percent=100.0,
        message="Your video is ready!",
    )


# ---------------------------------------------------------------------------
# Pure helper (testable without ffmpeg / R2)
# ---------------------------------------------------------------------------

def apply_render_padding(
    segments: list[dict[str, Any]],
    start_pad: float = RENDER_START_PAD_SEC,
    end_pad: float = RENDER_END_PAD_SEC,
    duration_sec: float | None = None,
) -> list[dict[str, Any]]:
    """
    Widen each segment by `start_pad`/`end_pad` seconds, clamped to
    [0, duration_sec]. Pure function, no I/O.

    Run before `final_merge_pass` so any overlaps the padding creates
    between adjacent kept segments get merged away rather than producing
    duplicated/overlapping cuts.
    """
    upper = float(duration_sec) if duration_sec else float("inf")
    out: list[dict[str, Any]] = []
    for s in segments:
        start = max(0.0, float(s["start"]) - start_pad)
        end = min(upper, float(s["end"]) + end_pad)
        if end > start:
            out.append({"start": start, "end": end})
    return out


def silence_aware_extend(
    segments: list[dict[str, Any]],
    other_segments: list[dict[str, Any]],
    silences: list[tuple[float, float]],
    max_extend_sec: float = SILENCE_EXTEND_MAX_SEC,
    duration_sec: float | None = None,
    overlap_extend_sec: float = OVERLAP_EXTEND_SEC,
) -> list[dict[str, Any]]:
    """
    Grow each selected-speaker segment outward over adjacent NON-SILENT
    audio, stopping at the first silence interval, the nearest other
    speaker, the `max_extend_sec` cap, or the video bounds — whichever
    comes first. Pure function, no I/O (silences are detected by the
    caller via ffmpeg).

    This recovers the selected speaker's leading/trailing speech that
    diarization dropped as "no-speaker", following the audio to its true
    length instead of a fixed guess, and adding no dead air (it halts the
    moment the audio goes silent).
    """
    if not segments:
        return []

    upper = float(duration_sec) if duration_sec else float("inf")
    others = [(float(o["start"]), float(o["end"])) for o in other_segments]
    sil = sorted((float(a), float(b)) for a, b in silences)

    def covered_by_silence(t: float) -> bool:
        return any(a <= t <= b for a, b in sil)

    out: list[dict[str, Any]] = []
    for s in segments:
        st, en = float(s["start"]), float(s["end"])

        # ----- Extend END forward over non-silent audio -----
        new_en = en
        if not covered_by_silence(en):
            # Limit: cap, nearest other-speaker start >= en, video end.
            limit = min(upper, en + max_extend_sec)
            for os, oe in others:
                if os >= en:
                    limit = min(limit, os)
                elif os <= en < oe:
                    # Another speaker is already talking OVER our end. Our own
                    # trailing words are very likely still here, so recover a
                    # BOUNDED bit into the overlap instead of stopping dead at
                    # `en` (the silence loop below still halts us at a real pause).
                    limit = min(limit, en + overlap_extend_sec)
            # Stop at the first silence that starts after `en`.
            for a, b in sil:
                if a > en:
                    limit = min(limit, a)
                    break
            new_en = max(en, limit)

        # ----- Extend START backward over non-silent audio -----
        new_st = st
        if not covered_by_silence(st):
            limit = max(0.0, st - max_extend_sec)
            for os, oe in others:
                if oe <= st:
                    limit = max(limit, oe)
                elif os < st <= oe:
                    # Another speaker overlaps our start — recover a bounded bit
                    # backward to catch our leading words (symmetric with the end
                    # side; the silence loop below still halts us at a real pause).
                    limit = max(limit, st - overlap_extend_sec)
            # Stop at the last silence that ends before `st`.
            for a, b in reversed(sil):
                if b < st:
                    limit = max(limit, b)
                    break
            new_st = min(st, limit)

        out.append({"start": new_st, "end": max(new_en, new_st)})
    return out


def bridge_clear_gaps(
    segments: list[dict[str, Any]],
    other_segments: list[dict[str, Any]],
    max_bridge_sec: float = MAX_BRIDGE_SEC,
    other_tolerance_sec: float = BRIDGE_OTHER_TOLERANCE_SEC,
) -> list[dict[str, Any]]:
    """
    Merge across gaps between selected-speaker segments when the gap is
    short (<= max_bridge_sec) AND essentially unoccupied by any other
    speaker (<= other_tolerance_sec of overlap). Pure function, no I/O.

    Rationale: a gap inside one speaker's timeline that no other speaker
    fills is almost always that speaker's own speech which diarization
    failed to assign (dropped as "no-speaker"). Bridging restores it.
    Gaps an other speaker occupies are real turn-taking and stay cut.

    `segments` must be sorted by start (caller passes final_merge_pass
    output). Returns a new list of {start, end} dicts.
    """
    if not segments:
        return []

    out: list[dict[str, Any]] = [
        {"start": float(segments[0]["start"]), "end": float(segments[0]["end"])}
    ]
    for s in segments[1:]:
        start = float(s["start"])
        end = float(s["end"])
        gap0 = out[-1]["end"]
        gap_len = start - gap0

        if gap_len <= 0:
            # Already overlapping/adjacent — just extend.
            out[-1]["end"] = max(out[-1]["end"], end)
            continue

        # How much of the gap (gap0 -> start) does any other speaker occupy?
        other_overlap = 0.0
        for o in other_segments:
            ov = min(start, float(o["end"])) - max(gap0, float(o["start"]))
            if ov > 0:
                other_overlap += ov

        if gap_len <= max_bridge_sec and other_overlap <= other_tolerance_sec:
            # Clear gap — bridge it (swallow the gap into the current cut).
            out[-1]["end"] = max(out[-1]["end"], end)
        else:
            out.append({"start": start, "end": end})
    return out


def final_merge_pass(
    segments: list[dict[str, Any]],
    max_gap_sec: float = FINAL_MERGE_GAP_SEC,
) -> list[dict[str, Any]]:
    """
    Merge adjacent segments whose gap is < `max_gap_sec` apart.

    Input segments must come in sorted by start; we re-sort defensively
    so callers don't have to remember. Returns a new list of merged
    {start, end} dicts.

    This is one extra merge pass on top of M4's post-processing — M4
    used a 1.5 s threshold, M6 uses 2.0 s, with the goal of avoiding
    too many tiny cuts in the final render.
    """
    if not segments:
        return []

    sorted_segs = sorted(segments, key=lambda s: float(s["start"]))
    first = sorted_segs[0]
    out: list[dict[str, Any]] = [
        {"start": float(first["start"]), "end": float(first["end"])}
    ]
    for s in sorted_segs[1:]:
        start = float(s["start"])
        end = float(s["end"])
        if start - out[-1]["end"] < max_gap_sec:
            out[-1]["end"] = max(out[-1]["end"], end)
        else:
            out.append({"start": start, "end": end})
    return out


def select_transcript_text(
    transcript: list[dict[str, Any]],
    windows: list[dict[str, Any]],
) -> str:
    """
    Join the text of every transcript segment whose audio falls inside any
    final-video window. Pure function, no I/O.

    `transcript` is the {start, end, speaker, text} list produced at diarize
    time; `windows` is the render cut list (the segments actually placed in
    the final video). Selection is by time-overlap, NOT by speaker — anything
    audible in the final video must be transcribed, which is what upholds the
    recall guarantee (a few boundary/other-speaker words are acceptable).

    Overlap is half-open (`seg.end > win.start and seg.start < win.end`) so a
    segment that merely abuts a window edge isn't pulled in. Each segment is
    emitted at most once even if it spans several windows. Output is ordered
    by start time and joined with single spaces.
    """
    selected: list[dict[str, Any]] = []
    for seg in transcript:
        try:
            s_start = float(seg["start"])
            s_end = float(seg["end"])
        except (KeyError, TypeError, ValueError):
            continue
        for w in windows:
            w_start = float(w["start"])
            w_end = float(w["end"])
            if s_end > w_start and s_start < w_end:
                selected.append(seg)
                break  # emit this segment once, regardless of how many windows it hits

    selected.sort(key=lambda s: float(s["start"]))
    parts = [(s.get("text") or "").strip() for s in selected]
    return " ".join(p for p in parts if p)


def _build_final_transcript(
    db: Any,
    job_id: str,
    job_dir: Path,
    cut_list: list[dict[str, Any]],
) -> None:
    """
    Download the diarize-time transcript.json, select the text overlapping
    the final cut windows, and upload it as transcription.txt. Stamps
    `artifacts.transcription_key` on success.

    Raises on any failure — the caller wraps this in best-effort try/except so
    a missing transcript.json (pre-feature jobs) or an I/O error never fails
    the render. Writes nothing when there's no overlapping text (so the
    frontend simply shows no transcript button rather than an empty file).
    """
    local_transcript = job_dir / "transcript.json"
    download_file(r2_key_transcript(job_id), str(local_transcript))
    transcript = json.loads(local_transcript.read_text(encoding="utf-8"))

    text = select_transcript_text(transcript, cut_list)
    if not text:
        logger.info(
            "render[%s] transcript empty after window selection; skipping upload",
            job_id,
        )
        return

    out_path = job_dir / "transcription.txt"
    out_path.write_text(text, encoding="utf-8")
    transcription_key = r2_key_transcription(job_id)
    upload_file(str(out_path), transcription_key)
    db.jobs.update_one(
        {"job_id": job_id},
        {"$set": {"artifacts.transcription_key": transcription_key}},
    )
    logger.info(
        "render[%s] transcript saved: %d chars -> %s",
        job_id, len(text), transcription_key,
    )
