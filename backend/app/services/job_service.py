"""
Job persistence + state machine.

This module is the single source of truth for:
  * Creating new jobs
  * Reading jobs (with optional presigned URL hydration)
  * Mutating job state (with transition validation)

State machine: ANY -> FAILED is always allowed. Otherwise transitions
must follow the linear pipeline declared in ALLOWED_TRANSITIONS.

Same-status updates (e.g. DOWNLOADING 0% -> DOWNLOADING 50%) bypass
transition validation — they only mutate `progress` fields.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from pymongo import ReturnDocument

from app.db.mongo import get_db
from app.services.storage import get_storage
from shared.constants import (
    ALLOWED_TRANSITIONS,
    JobStatus,
    is_legal_transition,
    r2_key_final_video,
    r2_key_snippet,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------

async def create_job(youtube_url: str) -> dict[str, Any]:
    """
    Insert a new job document with status=QUEUED and return it.

    `task_id` is left as None — the caller fills it in after enqueuing
    the Celery task (see api/jobs.py).
    """
    db = get_db()
    now = datetime.now(timezone.utc)

    doc = {
        "job_id": str(uuid.uuid4()),
        "youtube_url": youtube_url,
        "video_title": None,
        "duration_sec": 0,
        "status": JobStatus.QUEUED.value,
        "progress": {"stage": "queued", "percent": 0.0, "message": "Queued for processing"},
        "error": None,
        "artifacts": {
            "source_video_key": None,
            "audio_key": None,
            "final_video_key": None,
        },
        "speakers": [],
        "selected_speaker": None,
        "task_id": None,
        "created_at": now,
        "updated_at": now,
    }
    await db.jobs.insert_one(doc)
    return doc


async def set_task_id(job_id: str, task_id: str) -> None:
    """Stamp the Celery task id onto the job document."""
    db = get_db()
    await db.jobs.update_one(
        {"job_id": job_id},
        {"$set": {"task_id": task_id, "updated_at": datetime.now(timezone.utc)}},
    )


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

async def get_job_raw(job_id: str) -> dict[str, Any] | None:
    """Return the raw Mongo doc (with _id stripped) or None."""
    db = get_db()
    doc = await db.jobs.find_one({"job_id": job_id}, {"_id": 0})
    return doc


async def get_job_hydrated(job_id: str) -> dict[str, Any] | None:
    """
    Same as get_job_raw, plus:
      * inject `snippet_url` on each speaker (presigned, 1h) when status
        >= AWAITING_SELECTION and the snippet_key exists.
      * inject top-level `download_url` (presigned, 1h) when status == DONE
        and final_video_key exists.

    Presigned URL generation is sync (boto3), but the underlying call only
    signs locally — no network — so we don't bother offloading to a thread.
    """
    doc = await get_job_raw(job_id)
    if doc is None:
        return None

    storage = get_storage()
    status = doc.get("status")

    show_snippets = status in {
        JobStatus.AWAITING_SELECTION.value,
        JobStatus.RENDERING.value,
        JobStatus.DONE.value,
        JobStatus.FAILED.value,
    }

    speakers_out = []
    for sp in doc.get("speakers", []) or []:
        snippet_url = None
        if show_snippets and sp.get("snippet_key"):
            # Force audio/mpeg + inline so HTML5 <audio> can stream/seek the
            # clip. Without this, objects served as binary/octet-stream stall
            # after ~1s in the browser.
            snippet_url = storage.get_presigned_url(
                sp["snippet_key"],
                response_content_type="audio/mpeg",
                inline=True,
            )
        speakers_out.append(
            {
                "label": sp.get("label"),
                "total_speaking_sec": float(sp.get("total_speaking_sec", 0.0)),
                "segment_count": int(sp.get("segment_count", 0)),
                "snippet_url": snippet_url,
            }
        )
    doc["speakers"] = speakers_out

    download_url = None
    final_key = (doc.get("artifacts") or {}).get("final_video_key")
    if status == JobStatus.DONE.value and final_key:
        download_url = storage.get_presigned_url(
            final_key,
            response_content_type="video/mp4",
        )
    doc["download_url"] = download_url

    return doc


# ---------------------------------------------------------------------------
# Mutation
# ---------------------------------------------------------------------------

def _is_legal_transition(current: str, new: str) -> bool:
    """Thin wrapper around shared.constants.is_legal_transition."""
    return is_legal_transition(current, new)


async def transition_status(
    job_id: str,
    new_status: str,
    *,
    stage: str | None = None,
    percent: float | None = None,
    message: str | None = None,
    error: dict[str, str] | None = None,
    extra_set: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    Move a job to `new_status`, enforcing ALLOWED_TRANSITIONS.

    Returns the updated document on success, or None if the transition
    was rejected (illegal) or the job doesn't exist. Illegal transitions
    are logged as a WARNING per spec.
    """
    db = get_db()
    current = await db.jobs.find_one({"job_id": job_id}, {"status": 1, "_id": 0})
    if current is None:
        logger.warning("transition_status: job %s not found", job_id)
        return None

    current_status = current["status"]
    if not _is_legal_transition(current_status, new_status):
        logger.warning(
            "transition_status: illegal transition for job %s: %s -> %s",
            job_id, current_status, new_status,
        )
        return None

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
    if error is not None:
        set_doc["error"] = error
    if extra_set:
        set_doc.update(extra_set)

    updated = await db.jobs.find_one_and_update(
        {"job_id": job_id, "status": current_status},  # CAS on status
        {"$set": set_doc},
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0},
    )
    return updated


async def update_progress(
    job_id: str,
    *,
    stage: str | None = None,
    percent: float | None = None,
    message: str | None = None,
) -> None:
    """Progress-only update — does not change status."""
    db = get_db()
    set_doc: dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
    if stage is not None:
        set_doc["progress.stage"] = stage
    if percent is not None:
        set_doc["progress.percent"] = float(percent)
    if message is not None:
        set_doc["progress.message"] = message
    await db.jobs.update_one({"job_id": job_id}, {"$set": set_doc})


async def select_speaker(job_id: str, speaker_label: str) -> dict[str, Any]:
    """
    Mark which speaker the user identified as themselves.

    Returns a dict {"ok": bool, "error_code": str, "message": str,
                    "current_status": str, "job": dict | None}

    error_code values:
      - "NOT_FOUND"     -> 404
      - "WRONG_STATE"   -> 409
      - "SPEAKER_NOT_FOUND" -> 400
    """
    db = get_db()
    job = await db.jobs.find_one({"job_id": job_id}, {"_id": 0})
    if job is None:
        return {"ok": False, "error_code": "NOT_FOUND", "message": "Job not found"}

    if job["status"] != JobStatus.AWAITING_SELECTION.value:
        return {
            "ok": False,
            "error_code": "WRONG_STATE",
            "current_status": job["status"],
            "message": f"Cannot select speaker when job status is {job['status']}",
        }

    labels = {s.get("label") for s in (job.get("speakers") or [])}
    if speaker_label not in labels:
        return {
            "ok": False,
            "error_code": "SPEAKER_NOT_FOUND",
            "message": f"Speaker {speaker_label} not found in this job",
        }

    # Atomically: set selected_speaker AND move to RENDERING. We do these
    # as one update so polling clients see a consistent state.
    updated = await db.jobs.find_one_and_update(
        {"job_id": job_id, "status": JobStatus.AWAITING_SELECTION.value},
        {
            "$set": {
                "selected_speaker": speaker_label,
                "status": JobStatus.RENDERING.value,
                "progress.stage": "rendering",
                "progress.percent": 0.0,
                "progress.message": "Queued for render",
                "updated_at": datetime.now(timezone.utc),
            }
        },
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0},
    )
    if updated is None:
        # Lost the race — someone moved the job between our read and update.
        return {
            "ok": False,
            "error_code": "WRONG_STATE",
            "current_status": "unknown",
            "message": "Job state changed during selection; please retry",
        }

    return {"ok": True, "job": updated}


# ---------------------------------------------------------------------------
# Helpers shared with R2 (re-export)
# ---------------------------------------------------------------------------

__all__ = [
    "ALLOWED_TRANSITIONS",
    "create_job",
    "get_job_hydrated",
    "get_job_raw",
    "select_speaker",
    "set_task_id",
    "transition_status",
    "update_progress",
    "r2_key_snippet",
    "r2_key_final_video",
]
