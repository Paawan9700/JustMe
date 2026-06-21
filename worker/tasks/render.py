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
  9. Transition to DONE (100%, "Your video is ready!").

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

import logging
from pathlib import Path
from typing import Any

from worker.db import get_db
from worker.state import progress, transition
from worker.utils.ffmpeg import FFmpegError, detect_silence, run_ffmpeg
from worker.utils.storage import download_file, upload_file
from shared.constants import JobStatus, r2_key_final_video

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

    # 9. Finish
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
                elif os <= en < oe:   # other speaker already spans our end
                    limit = en
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
                elif os < st <= oe:   # other speaker already spans our start
                    limit = st
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
