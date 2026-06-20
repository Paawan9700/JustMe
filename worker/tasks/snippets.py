"""
Per-speaker identification snippets.

For each diarized speaker we cut an up-to-20-second mp3 from their
longest segment (clamped to the segment's own boundaries so it never
bleeds into surrounding music/silence), upload it to R2, and stamp
the resulting key
onto `job.speakers[].snippet_key`. The frontend then plays these
clips on the AWAITING_SELECTION screen so the user can pick their
own voice.

Pipeline:
  1. Transition to GENERATING_SNIPPETS, progress 0%.
  2. Pull job: speakers, artifacts.source_video_key, duration_sec.
  3. Skip cleanly when there's no source video or no speakers
     (dev fallback in Emergent; production gets here only when
     diarization actually ran). snippet_key stays null and the
     frontend already handles that with "No preview available".
  4. Download source.mp4 from R2 to job_dir if not already there.
  5. For each speaker: pick longest segment from `segments`, cut a
     window of up to SNIPPET_LENGTH_SEC clamped inside that segment
     (centred on its midpoint when the segment is long enough, else
     the whole segment), ffmpeg -> mp3 @ 128k,
     upload to R2, set `speakers.$.snippet_key`. Per-speaker errors
     are logged and skipped (other speakers' clips still ship).
  6. Delete the local source video (M6 will re-download for render).
  7. Transition to AWAITING_SELECTION, progress 100%.

Public entry point: `generate_snippets(job_id, job_dir)`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from worker.db import get_db
from worker.state import progress, transition
from worker.utils.ffmpeg import FFmpegError, run_ffmpeg
from worker.utils.storage import download_file, upload_file
from shared.constants import JobStatus, r2_key_snippet

logger = logging.getLogger(__name__)

SNIPPET_LENGTH_SEC = 20.0  # identification clip length (target)
SNIPPET_HALF = SNIPPET_LENGTH_SEC / 2.0


class SnippetError(Exception):
    """User-facing snippet failure with an error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_snippets(job_id: str, job_dir: Path) -> None:
    """Generate and upload per-speaker snippets; advance state machine."""
    transition(
        job_id, JobStatus.GENERATING_SNIPPETS.value,
        stage="generating_snippets", percent=0.0,
        message="Generating identification snippets...",
    )

    db = get_db()
    job = db.jobs.find_one(
        {"job_id": job_id},
        {"speakers": 1, "artifacts": 1, "duration_sec": 1, "_id": 0},
    ) or {}
    speakers: list[dict[str, Any]] = job.get("speakers") or []
    source_key = (job.get("artifacts") or {}).get("source_video_key")
    duration_sec = float(job.get("duration_sec") or 0.0)

    # Skip path: nothing to cut (dev fallback in Emergent, or unusual prod state).
    # In production this branch never runs because ingest + diarize would
    # have populated both.
    if not source_key or not speakers:
        logger.warning(
            "snippets[%s] skipping snippet generation "
            "(source_key=%r, speakers=%d)",
            job_id, source_key, len(speakers),
        )
    else:
        _do_snippets(job_id, job_dir, speakers, source_key, duration_sec)

    progress(job_id, percent=100.0, message="Ready for selection")
    transition(
        job_id, JobStatus.AWAITING_SELECTION.value,
        stage="awaiting_selection", percent=100.0,
        message="Please select your voice",
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _do_snippets(
    job_id: str,
    job_dir: Path,
    speakers: list[dict[str, Any]],
    source_key: str,
    duration_sec: float,
) -> None:
    """The hot path — only runs when we actually have source + speakers."""
    db = get_db()

    # 1. Make sure source.mp4 is local. In normal flow it's left here by
    # the ingest step; the download branch is a safety net for re-runs.
    local_source = job_dir / "source.mp4"
    if not local_source.exists():
        progress(job_id, percent=5.0, message="Downloading source from storage...")
        try:
            download_file(source_key, str(local_source))
        except Exception as exc:  # noqa: BLE001
            raise SnippetError(
                "SOURCE_DOWNLOAD_FAILED",
                f"Could not download source video: {exc}",
            ) from exc

    # 2. Per-speaker snippet
    n = max(1, len(speakers))
    for i, sp in enumerate(speakers):
        label = sp.get("label")
        if not label:
            continue
        # Reserve 5..95% for per-speaker work so the bookends stay clean.
        pct = 5.0 + ((i / n) * 90.0)
        progress(job_id, percent=pct, message=f"Cutting snippet for {label}...")

        try:
            r2_key = _make_one_snippet(
                job_id=job_id,
                speaker_label=label,
                local_source=local_source,
                job_dir=job_dir,
                duration_sec=duration_sec,
            )
        except _NoSegmentsForSpeaker:
            logger.warning(
                "snippets[%s] no segments for %s; leaving snippet_key=null",
                job_id, label,
            )
            continue
        except Exception as exc:  # noqa: BLE001
            # Per-speaker failure must not abort the whole job — others'
            # clips are still useful. Log and move on; snippet_key stays
            # null and the UI shows "No preview available".
            logger.warning(
                "snippets[%s] failed for %s: %s", job_id, label, exc,
            )
            continue

        db.jobs.update_one(
            {"job_id": job_id, "speakers.label": label},
            {"$set": {"speakers.$.snippet_key": r2_key}},
        )

    # 3. Cleanup local source (M6 re-downloads from R2 for the render).
    try:
        local_source.unlink()
    except OSError:
        pass


class _NoSegmentsForSpeaker(Exception):
    pass


def _make_one_snippet(
    job_id: str,
    speaker_label: str,
    local_source: Path,
    job_dir: Path,
    duration_sec: float,
) -> str:
    """Cut, upload, and return the R2 key for one speaker's snippet."""
    db = get_db()

    # Pick the longest segment for this speaker. Segment counts per
    # speaker are typically small (tens), so iterating in Python is
    # simpler than building an aggregation pipeline.
    cursor = db.segments.find(
        {"job_id": job_id, "speaker": speaker_label},
        {"start": 1, "end": 1, "_id": 0},
    )
    longest = max(
        cursor,
        key=lambda s: float(s["end"]) - float(s["start"]),
        default=None,
    )
    if longest is None:
        raise _NoSegmentsForSpeaker(speaker_label)

    start = float(longest["start"])
    end = float(longest["end"])
    seg_len = end - start

    # Clamp the snippet window to the chosen segment's own boundaries so it
    # never bleeds into surrounding audio (intro music, jingles, silence,
    # another speaker). Bleed was the cause of "music for the first few
    # seconds, then someone speaks" on cards whose longest segment is short.
    if seg_len >= SNIPPET_LENGTH_SEC:
        # Segment is long enough: take a centered SNIPPET_LENGTH_SEC window
        # from inside it. Centering on the midpoint keeps it within [start, end]
        # because seg_len >= SNIPPET_LENGTH_SEC.
        mid = (start + end) / 2.0
        snippet_start = mid - SNIPPET_HALF
        snippet_end = mid + SNIPPET_HALF
    else:
        # Segment shorter than target: use the whole segment. A clean clip of
        # pure speech beats a longer one padded with non-speech audio.
        snippet_start = start
        snippet_end = end

    # Final safety clamp to the video bounds.
    snippet_start = max(0.0, snippet_start)
    if duration_sec > 0:
        snippet_end = min(snippet_end, duration_sec)

    local_mp3 = job_dir / f"snippet_{speaker_label}.mp3"

    # ffmpeg -ss <start> -to <end> -i <src> -vn -acodec mp3 -ab 128k <out> -y
    try:
        run_ffmpeg([
            "-ss", f"{snippet_start:.3f}",
            "-to", f"{snippet_end:.3f}",
            "-i", str(local_source),
            "-vn",
            "-acodec", "mp3",
            "-ab", "128k",
            str(local_mp3),
            "-y",
        ])
    except FFmpegError as exc:
        raise RuntimeError(f"ffmpeg failed for {speaker_label}: {exc}") from exc

    if not local_mp3.exists() or local_mp3.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg produced no output for {speaker_label}")

    # Verification log: confirm the clip window and produced file size.
    # Lets us check in the worker logs the actual snippet length per speaker
    # (up to SNIPPET_LENGTH_SEC; shorter when the segment itself is shorter).
    logger.info(
        "snippets[%s] cut %s: window=%.3f-%.3fs (%.3fs) -> %s (%d bytes)",
        job_id, speaker_label, snippet_start, snippet_end,
        snippet_end - snippet_start, local_mp3.name, local_mp3.stat().st_size,
    )

    r2_key = r2_key_snippet(job_id, speaker_label)
    try:
        upload_file(str(local_mp3), r2_key)
    finally:
        # Whether upload succeeded or failed, remove the local copy —
        # it isn't needed once it's in R2 (and we don't want stale files
        # mixed into M6's render input).
        try:
            local_mp3.unlink()
        except OSError:
            pass

    return r2_key
