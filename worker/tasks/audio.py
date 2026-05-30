"""
Audio extraction with raw ffmpeg.

Public entry point: `extract_audio(job_id, local_video_path, job_dir)`.
Called from worker.tasks.dummy.process_video right after ingest.

Pipeline:
  1. Move job to EXTRACTING_AUDIO, progress 0%.
  2. Run ffmpeg to produce 16 kHz mono PCM WAV (the format WhisperX
     and pyannote.audio expect natively).
  3. Upload audio.wav to R2 (shared.constants.r2_key_audio).
  4. Persist artifacts.audio_key.
  5. Progress 100%, message "Audio extracted".

Failures raise AudioExtractionError; the caller marks the job FAILED.
"""

from __future__ import annotations

import logging
from pathlib import Path

from worker.db import get_db
from worker.state import progress, transition
from worker.utils.ffmpeg import FFmpegError, run_ffmpeg
from worker.utils.storage import upload_file
from shared.constants import JobStatus, r2_key_audio

logger = logging.getLogger(__name__)


class AudioExtractionError(Exception):
    """User-facing audio extraction failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def extract_audio(job_id: str, local_video_path: Path, job_dir: Path) -> Path:
    """Extract mono 16kHz WAV from a local video and mirror it to R2."""
    transition(
        job_id, JobStatus.EXTRACTING_AUDIO.value,
        stage="extracting_audio", percent=0.0,
        message="Extracting audio...",
    )

    audio_path = job_dir / "audio.wav"

    # ffmpeg -i <video> -vn -acodec pcm_s16le -ar 16000 -ac 1 audio.wav -y
    try:
        run_ffmpeg([
            "-i", str(local_video_path),
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",
            str(audio_path),
            "-y",
        ])
    except FFmpegError as exc:
        raise AudioExtractionError(
            "FFMPEG_FAILED",
            f"Audio extraction failed: {exc}",
        ) from exc

    if not audio_path.exists() or audio_path.stat().st_size == 0:
        raise AudioExtractionError(
            "FFMPEG_NO_OUTPUT",
            "Audio extraction produced no output file.",
        )

    progress(job_id, percent=80.0, message="Uploading audio...")

    r2_key = r2_key_audio(job_id)
    try:
        upload_file(str(audio_path), r2_key)
    except Exception as exc:  # noqa: BLE001
        raise AudioExtractionError(
            "UPLOAD_FAILED",
            f"Could not upload audio to storage: {exc}",
        ) from exc

    db = get_db()
    db.jobs.update_one(
        {"job_id": job_id},
        {"$set": {"artifacts.audio_key": r2_key}},
    )

    progress(job_id, percent=100.0, message="Audio extracted")
    logger.info("audio[%s] extracted -> %s", job_id, r2_key)
    return audio_path
