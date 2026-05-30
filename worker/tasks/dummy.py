"""
Dummy worker tasks for Milestone 1.

These simulate the real video-processing pipeline so the API + frontend
can be developed end-to-end without the GPU dependencies. They will be
replaced by real tasks (yt-dlp ingest, ffmpeg audio, WhisperX +
pyannote diarization, snippet generation, ffmpeg render) in M3-M6.

Tasks registered:
  - process_video(job_id)   - simulates ingest through AWAITING_SELECTION
  - render_video(job_id)    - simulates final render through DONE
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from worker.celery_app import celery_app
from worker.db import get_db
from shared.constants import JobStatus, is_legal_transition

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sync state helpers (worker-side mirror of app.services.job_service)
# ---------------------------------------------------------------------------

def _transition(
    job_id: str,
    new_status: str,
    *,
    stage: str | None = None,
    percent: float | None = None,
    message: str | None = None,
    extra_set: dict[str, Any] | None = None,
) -> bool:
    """
    Conditional update: status -> new_status, enforcing
    is_legal_transition. Returns True on success, False if the
    transition was rejected.
    """
    db = get_db()
    doc = db.jobs.find_one({"job_id": job_id}, {"status": 1})
    if not doc:
        logger.warning("worker: job %s not found", job_id)
        return False

    current = doc["status"]
    if not is_legal_transition(current, new_status):
        logger.warning(
            "worker: illegal transition for job %s: %s -> %s",
            job_id, current, new_status,
        )
        return False

    set_doc: dict[str, Any] = {
        "status": new_status,
        "updated_at": datetime.now(timezone.utc),
    }
    if stage is not None:
        set_doc["progress.stage"] = stage
    if percent is not None:
        set_doc["progress.percent"] = float(percent)
    if message is not None:
        set_doc["progress.message"] = message
    if extra_set:
        set_doc.update(extra_set)

    res = db.jobs.update_one(
        {"job_id": job_id, "status": current},
        {"$set": set_doc},
    )
    return res.modified_count == 1


def _progress(
    job_id: str,
    *,
    stage: str | None = None,
    percent: float | None = None,
    message: str | None = None,
) -> None:
    """Progress-only update (no status change)."""
    db = get_db()
    set_doc: dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
    if stage is not None:
        set_doc["progress.stage"] = stage
    if percent is not None:
        set_doc["progress.percent"] = float(percent)
    if message is not None:
        set_doc["progress.message"] = message
    db.jobs.update_one({"job_id": job_id}, {"$set": set_doc})


# ---------------------------------------------------------------------------
# Task 1: process_video (dummy)
# ---------------------------------------------------------------------------

@celery_app.task(name="process_video", bind=True)
def process_video(self, job_id: str) -> dict[str, Any]:  # noqa: ARG001
    """
    Dummy pipeline: QUEUED -> DOWNLOADING -> EXTRACTING_AUDIO ->
    DIARIZING -> GENERATING_SNIPPETS -> AWAITING_SELECTION.

    Each step sleeps briefly and updates Mongo so a polling client sees
    the status change.
    """
    logger.info("process_video[%s] start", job_id)

    # ---- DOWNLOADING --------------------------------------------------
    if not _transition(
        job_id, JobStatus.DOWNLOADING.value,
        stage="downloading", percent=0.0, message="Starting download",
    ):
        return {"ok": False, "reason": "initial transition rejected"}
    time.sleep(3)
    _progress(job_id, percent=50.0, message="Downloading video...")
    time.sleep(3)

    # ---- EXTRACTING_AUDIO ---------------------------------------------
    _transition(
        job_id, JobStatus.EXTRACTING_AUDIO.value,
        stage="extracting_audio", percent=100.0,
        message="Audio extraction complete",
    )
    time.sleep(3)

    # ---- DIARIZING ----------------------------------------------------
    _transition(
        job_id, JobStatus.DIARIZING.value,
        stage="diarizing", percent=0.0, message="Diarization starting",
    )
    time.sleep(2)
    _progress(job_id, percent=50.0, message="Diarizing speakers...")
    time.sleep(2)
    _progress(job_id, percent=100.0, message="Diarization complete")

    # ---- GENERATING_SNIPPETS + fake speakers --------------------------
    fake_speakers = [
        {
            "label": "SPEAKER_00",
            "total_speaking_sec": 180.0,
            "segment_count": 5,
            "snippet_key": None,
        },
        {
            "label": "SPEAKER_01",
            "total_speaking_sec": 120.0,
            "segment_count": 3,
            "snippet_key": None,
        },
    ]
    _transition(
        job_id, JobStatus.GENERATING_SNIPPETS.value,
        stage="generating_snippets", percent=0.0,
        message="Generating identification snippets",
        extra_set={"speakers": fake_speakers},
    )
    time.sleep(2)

    # ---- AWAITING_SELECTION -------------------------------------------
    _transition(
        job_id, JobStatus.AWAITING_SELECTION.value,
        stage="awaiting_selection", percent=100.0,
        message="Please select your voice",
    )
    logger.info("process_video[%s] done -> AWAITING_SELECTION", job_id)
    return {"ok": True, "job_id": job_id}


# ---------------------------------------------------------------------------
# Task 2: render_video (dummy)
# ---------------------------------------------------------------------------

@celery_app.task(name="render_video", bind=True)
def render_video(self, job_id: str) -> dict[str, Any]:  # noqa: ARG001
    """
    Dummy renderer: RENDERING -> DONE.

    The API has already moved the job to RENDERING via select_speaker(),
    so this task only needs to simulate work and then mark DONE.
    """
    logger.info("render_video[%s] start", job_id)

    # Job is already in RENDERING when we get here (set atomically by the
    # API). Just emit progress updates.
    _progress(job_id, stage="rendering", percent=0.0, message="Rendering started")
    time.sleep(5)
    _progress(job_id, percent=80.0, message="Stitching segments...")

    _transition(
        job_id, JobStatus.DONE.value,
        stage="done", percent=100.0, message="Render complete",
    )
    logger.info("render_video[%s] done", job_id)
    return {"ok": True, "job_id": job_id}
