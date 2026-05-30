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
from worker.utils.ffmpeg import FFmpegError, run_ffmpeg
from worker.utils.storage import download_file, upload_file
from shared.constants import JobStatus, r2_key_final_video

logger = logging.getLogger(__name__)

FINAL_MERGE_GAP_SEC = 2.0  # spec step 3b


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
        {"selected_speaker": 1, "artifacts": 1, "_id": 0},
    ) or {}
    selected = job.get("selected_speaker")
    source_key = (job.get("artifacts") or {}).get("source_video_key")

    if not selected:
        raise RenderError(
            "NO_SPEAKER_SELECTED",
            "No speaker has been selected for this job.",
        )

    raw_segments = list(
        db.segments
          .find({"job_id": job_id, "speaker": selected}, {"_id": 0, "start": 1, "end": 1})
          .sort("start", 1)
    )

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
    cut_list = final_merge_pass(raw_segments, FINAL_MERGE_GAP_SEC)
    if not cut_list:
        # Mostly unreachable (we already checked raw_segments was non-empty)
        raise RenderError(
            "EMPTY_CUT_LIST",
            "After post-processing there were no segments to render.",
        )

    # 4. Download source video from R2
    progress(job_id, percent=10.0, message="Downloading source from storage...")
    local_source = job_dir / "source.mp4"
    try:
        download_file(source_key, str(local_source))
    except Exception as exc:  # noqa: BLE001
        raise RenderError(
            "SOURCE_DOWNLOAD_FAILED",
            f"Could not download source video: {exc}",
        ) from exc
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
    video_duration = (db.jobs.find_one(
        {"job_id": job_id}, {"duration_sec": 1, "_id": 0},
    ) or {}).get("duration_sec") or 0
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
