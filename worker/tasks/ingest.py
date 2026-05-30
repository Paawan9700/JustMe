"""
Real YouTube ingest using yt-dlp.

Public entry point: `download_video(job_id, youtube_url, job_dir)`.
It is called from worker.tasks.dummy.process_video — not a Celery task
itself, just a piece of the pipeline.

Responsibilities:
  1. Extract metadata WITHOUT downloading and validate:
     - is_live == True -> reject (returns user-facing message from
                          shared.constants.LIVE_STREAM_REJECT_MESSAGE)
     - duration > MAX_VIDEO_HOURS * 3600 -> reject
     Completed past livestreams (is_live=False OR was_live=True) are
     allowed normally.
  2. Persist video_title and duration_sec on the job document.
  3. Download with bestvideo<=720p + bestaudio, merge to mp4, retries=3,
     resume enabled, progress hook -> throttled Mongo writes.
  4. Upload the mp4 to R2 (key: shared.constants.r2_key_source_video).
     Persist artifacts.source_video_key.
  5. Return the local Path of the downloaded video (caller passes it to
     the audio task and is responsible for cleanup at the end of the
     pipeline).

Failures raise IngestError with a user-facing message. The caller
(process_video in dummy.py) catches it and marks the job FAILED.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import yt_dlp

from worker.db import get_db
from worker.state import progress, transition
from worker.utils.storage import upload_file
from shared.constants import (
    JobStatus,
    LIVE_STREAM_REJECT_MESSAGE,
    r2_key_source_video,
)

logger = logging.getLogger(__name__)

# Cached path to the cookies file written from the YOUTUBE_COOKIES env var.
# Lazy-initialised the first time _maybe_add_cookies() runs, then reused for
# every subsequent yt-dlp call in this worker process. We can't put cookies
# in job_dir because _extract_info() runs *before* job_dir is even created.
_COOKIES_FILE_PATH: str | None = None


class IngestError(Exception):
    """User-facing ingest failure. `code` matches job.error.code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def download_video(job_id: str, youtube_url: str, job_dir: Path) -> Path:
    """Download a YouTube video to local disk and mirror it to R2."""
    transition(
        job_id, JobStatus.DOWNLOADING.value,
        stage="downloading", percent=0.0,
        message="Starting download...",
    )

    # ---- 1. Metadata + validation ---------------------------------------
    info = _extract_info(youtube_url)

    if info.get("is_live") is True:
        raise IngestError("LIVE_STREAM", LIVE_STREAM_REJECT_MESSAGE)

    duration_sec = int(info.get("duration") or 0)
    max_hours = int(os.environ.get("MAX_VIDEO_HOURS", "15"))
    if duration_sec > max_hours * 3600:
        raise IngestError(
            "TOO_LONG",
            f"Video exceeds maximum allowed length of {max_hours} hours",
        )

    title = (info.get("title") or info.get("id") or "Unknown")[:500]

    db = get_db()
    db.jobs.update_one(
        {"job_id": job_id},
        {"$set": {"video_title": title, "duration_sec": duration_sec}},
    )
    progress(
        job_id, percent=2.0,
        message=f"Downloading: {title[:60]}",
    )

    # ---- 2. Download with progress hook ---------------------------------
    out_template = str(job_dir / "source.%(ext)s")
    state = {"last_pct": 0.0, "last_t": 0.0}

    def _hook(d: dict[str, Any]) -> None:
        # Called by yt-dlp throughout the download. We throttle Mongo
        # writes to either every 5% or every 2s, whichever comes first,
        # so high-bandwidth downloads don't hammer the DB.
        if d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes") or 0
        if total <= 0:
            return
        # Cap at 99% during download; we'll set 100% after upload.
        pct = min(99.0, (done / total) * 100.0)
        now = time.time()
        if pct - state["last_pct"] >= 5.0 or (now - state["last_t"]) > 2.0:
            try:
                progress(
                    job_id, percent=pct,
                    message=f"Downloading... {pct:.0f}%",
                )
            except Exception:  # noqa: BLE001
                logger.exception("progress hook failed (non-fatal)")
            state["last_pct"] = pct
            state["last_t"] = now

    ydl_opts: dict[str, Any] = {
        "format": "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "outtmpl": out_template,
        "merge_output_format": "mp4",
        "continue_dl": True,
        "retries": 3,
        "fragment_retries": 3,
        "progress_hooks": [_hook],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
    }
    _maybe_add_cookies(ydl_opts)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(youtube_url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        raise IngestError(*_map_ytdlp_error(str(exc))) from exc

    # ---- 3. Find the produced file --------------------------------------
    # With merge_output_format=mp4 yt-dlp should land on source.mp4.
    # Fall back to whatever source.* exists, in case the merge step
    # produced a different container.
    local_path = job_dir / "source.mp4"
    if not local_path.exists():
        candidates = sorted(job_dir.glob("source.*"))
        if not candidates:
            raise IngestError(
                "DOWNLOAD_FAILED",
                "Download completed but no output file was produced.",
            )
        local_path = candidates[0]

    # ---- 4. Upload to R2 ------------------------------------------------
    progress(job_id, percent=99.0, message="Uploading to storage...")
    r2_key = r2_key_source_video(job_id)
    try:
        upload_file(str(local_path), r2_key)
    except Exception as exc:  # noqa: BLE001
        raise IngestError("UPLOAD_FAILED", f"Could not upload to storage: {exc}") from exc

    db.jobs.update_one(
        {"job_id": job_id},
        {"$set": {"artifacts.source_video_key": r2_key}},
    )

    progress(job_id, percent=100.0, message="Download complete")
    try:
        size_mb = local_path.stat().st_size / 1_000_000.0
    except OSError:
        size_mb = 0.0
    logger.info(
        "ingest[%s] downloaded %r (%.1fs duration, %.1f MB) -> %s",
        job_id, title, duration_sec, size_mb, r2_key,
    )
    return local_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _maybe_add_cookies(ydl_opts: dict[str, Any]) -> None:
    """
    Inject YouTube cookies into yt-dlp options if `YOUTUBE_COOKIES` is set.

    YouTube blocks downloads from cloud/datacenter IPs with HTTP 403
    ("Sign in to confirm you're not a bot"). The fix is to authenticate
    yt-dlp with a Netscape-format cookies.txt exported from a logged-in
    browser. On Modal we ship that file's contents via the
    `YOUTUBE_COOKIES` env var (part of the `justme-secrets` Modal secret).

    The cookies content is written once per process to a temp file and
    that path is reused for every subsequent yt-dlp call.

    No-op if `YOUTUBE_COOKIES` is unset or empty — tests in the Emergent
    container can still run without cookies (they'll hit the same 403,
    but that's expected here).
    """
    global _COOKIES_FILE_PATH  # noqa: PLW0603

    cookies_blob = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if not cookies_blob:
        return

    if _COOKIES_FILE_PATH is None:
        # Write to a NamedTemporaryFile with delete=False so the path
        # survives until the process exits. yt-dlp opens it read-only.
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            prefix="yt_cookies_",
            delete=False,
        )
        try:
            tmp.write(cookies_blob)
            if not cookies_blob.endswith("\n"):
                tmp.write("\n")
        finally:
            tmp.close()
        _COOKIES_FILE_PATH = tmp.name
        logger.info(
            "ingest: wrote YouTube cookies to %s (size=%d bytes, lines=%d, tabs=%d)",
            _COOKIES_FILE_PATH,
            len(cookies_blob),
            cookies_blob.count("\n") + 1,
            cookies_blob.count("\t"),
        )

    ydl_opts["cookiefile"] = _COOKIES_FILE_PATH


def _extract_info(youtube_url: str) -> dict[str, Any]:
    """Metadata-only probe (no download)."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    _maybe_add_cookies(ydl_opts)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(youtube_url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise IngestError(*_map_ytdlp_error(str(exc))) from exc


def _map_ytdlp_error(msg: str) -> tuple[str, str]:
    """Translate yt-dlp's stringly-typed errors into user-facing messages."""
    lower = msg.lower()
    if "private" in lower or "sign in" in lower:
        return "PRIVATE", "This video is private or requires sign-in."
    if "members-only" in lower or "members only" in lower:
        return "MEMBERS_ONLY", "This video is for channel members only."
    if "age" in lower and "confirm" in lower:
        return "AGE_RESTRICTED", "This video is age-restricted."
    if "unavailable" in lower or "has been removed" in lower or "removed by the user" in lower:
        return "UNAVAILABLE", "This video is unavailable or has been removed."
    if "copyright" in lower:
        return "COPYRIGHT", "This video is blocked for copyright reasons."
    if "region" in lower and ("block" in lower or "not available" in lower):
        return "REGION_BLOCKED", "This video is not available in our region."
    # Fallback — keep the raw message short for the user.
    return "DOWNLOAD_FAILED", f"Could not download video: {msg[:200]}"
