"""
Sync MongoDB state-mutation helpers for the worker.

Mirror of the async helpers in backend/app/services/job_service.py but
implemented with sync pymongo. The state machine is enforced via
shared.constants.is_legal_transition so the API and worker share one
source of truth.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from worker.db import get_db
from shared.constants import is_legal_transition

logger = logging.getLogger(__name__)


def transition(
    job_id: str,
    new_status: str,
    *,
    stage: str | None = None,
    percent: float | None = None,
    message: str | None = None,
    error: dict[str, str] | None = None,
    extra_set: dict[str, Any] | None = None,
) -> bool:
    """
    Atomic status change with state-machine validation. CAS on current
    status so concurrent workers can't double-transition.

    Returns True on success, False if the transition was rejected
    (illegal) or the doc was missing / changed underneath us.
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
    if error is not None:
        set_doc["error"] = error
    if extra_set:
        set_doc.update(extra_set)

    res = db.jobs.update_one(
        {"job_id": job_id, "status": current},
        {"$set": set_doc},
    )
    return res.modified_count == 1


def progress(
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
    db.jobs.update_one({"job_id": job_id}, {"$set": set_doc})


def fail(job_id: str, code: str, message: str) -> None:
    """
    Mark a job FAILED with a user-facing error message. Always succeeds
    regardless of current status (ANY -> FAILED is legal).
    """
    db = get_db()
    db.jobs.update_one(
        {"job_id": job_id},
        {
            "$set": {
                "status": "FAILED",
                "error": {"code": code, "message": message},
                "progress.message": message,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )
