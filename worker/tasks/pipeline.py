"""
Pipeline orchestration bodies — dispatcher-agnostic.

This module owns the actual job orchestration that used to live inside
the Celery task bodies in worker/tasks/dummy.py. It deliberately has NO
dependency on worker.celery_app (and therefore no REDIS_URL requirement)
so the same code can be invoked from either dispatcher:

  - Celery (legacy/fallback): worker/tasks/dummy.py registers thin
    `process_video` / `render_video` tasks that call into here.
  - Modal (primary): worker/modal_app.py defines on-demand
    `@app.function`s that call into here.

What's real (since M3 / M4 / M5 / M6):
  - DOWNLOADING        -> worker.tasks.ingest.download_video
  - EXTRACTING_AUDIO   -> worker.tasks.audio.extract_audio
  - DIARIZING          -> worker.tasks.diarize.run_diarization
                          (WhisperX + pyannote, GPU only — dev box
                          falls back to a mocked 2-speaker fixture so
                          the Emergent-hosted M2 UI can still be
                          exercised)
  - GENERATING_SNIPPETS -> worker.tasks.snippets.generate_snippets
  - RENDERING          -> worker.tasks.render.run_render

M7 hardening:
  - Each stage is skipped on retry if its output already exists
    (artifact in R2 / status already past it), so a worker crash and
    requeue resumes from the last completed stage instead of starting
    over.
  - SoftTimeLimitExceeded is caught and translated into a clean
    job.status=FAILED with code="TIMEOUT". Under Celery the exception
    comes from the soft time limit; under Modal it is raised by a
    SIGALRM shim in modal_app.py — same class, same handler.

Local working directory:
  /tmp/justme/{job_id}/   (created on entry, recursively removed in finally)
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Any

# celery stays installed in the worker image; importing its exceptions
# module needs no broker configuration.
from celery.exceptions import SoftTimeLimitExceeded

from worker.db import get_db
from worker.state import fail, progress
from worker.tasks import audio as audio_task
from worker.tasks import diarize as diarize_task
from worker.tasks import ingest as ingest_task
from worker.tasks import render as render_task
from worker.tasks import snippets as snippets_task
from worker.utils.storage import delete_file as r2_delete
from worker.utils.storage import download_file as r2_download
from worker.utils.storage import file_exists as r2_file_exists
from shared.constants import JobStatus, r2_key_audio

logger = logging.getLogger(__name__)

JOB_TMP_ROOT = Path("/tmp/justme")

# Friendly message used when the soft time limit fires. Mirrors the
# frontend's TIMEOUT entry in JobStatus.jsx::ERROR_CODE_MESSAGES.
_TIMEOUT_MSG = "Processing timed out. Please try again."

# Statuses that imply diarization has already produced segments.
_PAST_DIARIZING = {
    JobStatus.GENERATING_SNIPPETS.value,
    JobStatus.AWAITING_SELECTION.value,
    JobStatus.RENDERING.value,
    JobStatus.DONE.value,
}

# Statuses that imply the snippet step has already advanced the pipeline.
_PAST_SNIPPETS = {
    JobStatus.AWAITING_SELECTION.value,
    JobStatus.RENDERING.value,
    JobStatus.DONE.value,
}

# Terminal statuses: a freshly spawned run must not touch these jobs
# (e.g. the API marked the job FAILED/ENQUEUE_FAILED after the spawn
# succeeded but before its own response completed).
_TERMINAL = {JobStatus.DONE.value, JobStatus.FAILED.value}


# ---------------------------------------------------------------------------
# Stage-skip predicates  (M7 idempotency)
# ---------------------------------------------------------------------------

def _ingest_already_done(job_id: str) -> bool:
    """Skip ingest when the source mp4 is already in R2."""
    db = get_db()
    job = db.jobs.find_one({"job_id": job_id}, {"artifacts": 1, "_id": 0}) or {}
    key = (job.get("artifacts") or {}).get("source_video_key")
    if not key:
        return False
    return r2_file_exists(key)


def _audio_already_done(job_id: str) -> bool:
    """Skip audio extraction when audio.wav is already in R2."""
    db = get_db()
    job = db.jobs.find_one({"job_id": job_id}, {"artifacts": 1, "_id": 0}) or {}
    key = (job.get("artifacts") or {}).get("audio_key")
    if not key:
        return False
    return r2_file_exists(key)


def _diarize_already_done(job_id: str) -> bool:
    """Skip diarization when status is past DIARIZING AND segments exist."""
    db = get_db()
    job = db.jobs.find_one({"job_id": job_id}, {"status": 1, "_id": 0}) or {}
    if job.get("status") not in _PAST_DIARIZING:
        return False
    return db.segments.count_documents({"job_id": job_id}) > 0


def _snippets_already_done(job_id: str) -> bool:
    """Skip snippet generation when status is past GENERATING_SNIPPETS."""
    db = get_db()
    job = db.jobs.find_one({"job_id": job_id}, {"status": 1, "_id": 0}) or {}
    return job.get("status") in _PAST_SNIPPETS


# ---------------------------------------------------------------------------
# process_video body — ingest -> audio -> diarize -> snippets -> awaiting
# ---------------------------------------------------------------------------

def run_process_video(job_id: str) -> dict[str, Any]:
    t_start = time.perf_counter()
    logger.info("process_video[%s] start", job_id)

    db = get_db()
    job = db.jobs.find_one(
        {"job_id": job_id},
        {"youtube_url": 1, "duration_sec": 1, "status": 1, "_id": 0},
    )
    if not job:
        logger.warning("process_video[%s] job not found", job_id)
        return {"ok": False, "reason": "job not found"}
    if job.get("status") in _TERMINAL:
        logger.warning(
            "process_video[%s] job already terminal (%s) — nothing to do",
            job_id, job.get("status"),
        )
        return {"ok": False, "reason": "job already terminal"}

    youtube_url = job["youtube_url"]
    job_dir = JOB_TMP_ROOT / job_id

    try:
        job_dir.mkdir(parents=True, exist_ok=True)

        # ---- INGEST -----------------------------------------------------
        if _ingest_already_done(job_id):
            logger.info(
                "process_video[%s] source already in R2 — skipping ingest", job_id,
            )
            local_video = job_dir / "source.mp4"
        else:
            t0 = time.perf_counter()
            local_video = ingest_task.download_video(job_id, youtube_url, job_dir)
            logger.info(
                "process_video[%s] ingest done in %.1fs (%.1f MB local)",
                job_id, time.perf_counter() - t0,
                _mb(local_video),
            )

        # ---- AUDIO ------------------------------------------------------
        if _audio_already_done(job_id):
            logger.info(
                "process_video[%s] audio.wav already in R2 — skipping extraction",
                job_id,
            )
            # Bridge the state machine if we resumed at EXTRACTING_AUDIO.
            current = db.jobs.find_one(
                {"job_id": job_id}, {"status": 1, "_id": 0},
            )["status"]
            if current == JobStatus.EXTRACTING_AUDIO.value:
                # Diarize's own transition() will move us forward.
                pass
        else:
            # If we skipped ingest, source.mp4 isn't local — pull from R2.
            if not local_video.exists():
                src_key = (db.jobs.find_one(
                    {"job_id": job_id}, {"artifacts": 1, "_id": 0},
                ).get("artifacts") or {}).get("source_video_key")
                if not src_key:
                    raise RuntimeError(
                        "audio stage requires source.mp4 but no source_video_key on job"
                    )
                logger.info("process_video[%s] pulling source.mp4 from R2 for audio", job_id)
                r2_download(src_key, str(local_video))
            t0 = time.perf_counter()
            audio_task.extract_audio(job_id, local_video, job_dir)
            logger.info(
                "process_video[%s] audio extraction done in %.1fs",
                job_id, time.perf_counter() - t0,
            )

        # ---- DIARIZE ----------------------------------------------------
        if _diarize_already_done(job_id):
            logger.info(
                "process_video[%s] segments already in DB — skipping diarize",
                job_id,
            )
        else:
            t0 = time.perf_counter()
            duration_sec = (
                db.jobs.find_one(
                    {"job_id": job_id}, {"duration_sec": 1, "_id": 0},
                ) or {}
            ).get("duration_sec") or 0
            _diarize_or_fallback(job_id, job_dir, duration_sec)
            logger.info(
                "process_video[%s] diarize done in %.1fs",
                job_id, time.perf_counter() - t0,
            )

        # ---- SNIPPETS ---------------------------------------------------
        if _snippets_already_done(job_id):
            logger.info(
                "process_video[%s] snippets already done — skipping",
                job_id,
            )
        else:
            t0 = time.perf_counter()
            snippets_task.generate_snippets(job_id, job_dir)
            logger.info(
                "process_video[%s] snippets done in %.1fs",
                job_id, time.perf_counter() - t0,
            )

        # ---- Reclaim audio.wav ------------------------------------------
        # Diarization is the only reader (snippets are cut from source.mp4) and
        # its output is already persisted to the `segments` collection plus
        # transcript.json, so this ~351 MB file is now dead weight. Deleting it
        # here frees the space in minutes instead of waiting for the ephemeral/
        # lifecycle rule.
        #
        # Safe against resume: _audio_already_done() verifies the object with
        # r2_file_exists(), so a re-run simply re-extracts it from source.mp4
        # (cheap, local ffmpeg) rather than skipping the stage and failing.
        #
        # Best-effort: the job has succeeded, so cleanup must never fail it.
        try:
            r2_delete(r2_key_audio(job_id))
            logger.info("process_video[%s] deleted ephemeral audio.wav", job_id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "process_video[%s] could not delete audio.wav; the ephemeral/ "
                "lifecycle rule will reclaim it",
                job_id, exc_info=True,
            )

        # ---- Summary log ------------------------------------------------
        final_doc = db.jobs.find_one(
            {"job_id": job_id},
            {"speakers": 1, "duration_sec": 1, "video_title": 1, "_id": 0},
        ) or {}
        seg_count = db.segments.count_documents({"job_id": job_id})
        logger.info(
            "process_video[%s] DONE in %.1fs | title=%r | duration=%ds | "
            "speakers=%d | segments=%d",
            job_id, time.perf_counter() - t_start,
            final_doc.get("video_title"),
            final_doc.get("duration_sec") or 0,
            len(final_doc.get("speakers") or []),
            seg_count,
        )
        return {"ok": True, "job_id": job_id}

    except SoftTimeLimitExceeded:
        logger.warning("process_video[%s] soft time limit exceeded", job_id)
        fail(job_id, "TIMEOUT", _TIMEOUT_MSG)
        return {"ok": False, "code": "TIMEOUT", "reason": _TIMEOUT_MSG}

    except ingest_task.IngestError as exc:
        logger.warning("process_video[%s] ingest failed: %s", job_id, exc.message)
        fail(job_id, exc.code, exc.message)
        return {"ok": False, "code": exc.code, "reason": exc.message}

    except audio_task.AudioExtractionError as exc:
        logger.warning("process_video[%s] audio failed: %s", job_id, exc.message)
        fail(job_id, exc.code, exc.message)
        return {"ok": False, "code": exc.code, "reason": exc.message}

    except diarize_task.DiarizationError as exc:
        logger.warning("process_video[%s] diarize failed: %s", job_id, exc.message)
        fail(job_id, exc.code, exc.message)
        return {"ok": False, "code": exc.code, "reason": exc.message}

    except snippets_task.SnippetError as exc:
        logger.warning("process_video[%s] snippets failed: %s", job_id, exc.message)
        fail(job_id, exc.code, exc.message)
        return {"ok": False, "code": exc.code, "reason": exc.message}

    except Exception as exc:  # noqa: BLE001
        logger.exception("process_video[%s] unexpected error", job_id)
        fail(job_id, "WORKER_ERROR", f"Unexpected worker error: {exc!s}"[:300])
        return {"ok": False, "code": "WORKER_ERROR", "reason": str(exc)}

    finally:
        # Disk hygiene — always remove the job's local working dir.
        shutil.rmtree(job_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Diarization wrapper: real first, dev fallback only on MISSING_DEPS
# ---------------------------------------------------------------------------

def _diarize_or_fallback(job_id: str, job_dir: Path, duration_sec: float) -> None:
    """
    Try the real WhisperX + pyannote pipeline first. If — and only if —
    the deps aren't installed (Emergent dev container), fall back to a
    mocked 2-speaker fixture so the M2 UI flow can still be exercised
    end-to-end during development.

    Any other diarization failure (missing HF_TOKEN, model crash, R2
    download error) is re-raised and surfaces as job.status = FAILED
    with the appropriate user-facing message.
    """
    audio_key = r2_key_audio(job_id)
    try:
        diarize_task.run_diarization(job_id, audio_key, job_dir, duration_sec)
        return
    except diarize_task.DiarizationError as exc:
        if exc.code != "MISSING_DEPS":
            raise
        logger.warning(
            "diarize[%s] WhisperX not installed — using mocked speakers "
            "(this branch should NEVER run in production)",
            job_id,
        )
        _mock_diarize_for_dev(job_id)


def _mock_diarize_for_dev(job_id: str) -> None:
    """Stand-in for real diarization on the Emergent CPU-only dev container."""
    progress(job_id, percent=50.0, message="Diarizing (dev mode — no GPU)")
    time.sleep(2)
    progress(job_id, percent=100.0, message="Diarization complete (mocked)")

    fake_speakers = [
        {"label": "SPEAKER_00", "total_speaking_sec": 180.0,
         "segment_count": 5, "snippet_key": None},
        {"label": "SPEAKER_01", "total_speaking_sec": 120.0,
         "segment_count": 3, "snippet_key": None},
    ]
    db = get_db()
    db.jobs.update_one(
        {"job_id": job_id},
        {"$set": {"speakers": fake_speakers}},
    )


# ---------------------------------------------------------------------------
# render_video body — real ffmpeg cut + concat (M6)
# ---------------------------------------------------------------------------

def run_render_video(job_id: str) -> dict[str, Any]:
    t_start = time.perf_counter()
    logger.info("render_video[%s] start", job_id)

    job_dir = JOB_TMP_ROOT / job_id
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        render_task.run_render(job_id, job_dir)
        logger.info(
            "render_video[%s] DONE in %.1fs",
            job_id, time.perf_counter() - t_start,
        )
        return {"ok": True, "job_id": job_id}

    except SoftTimeLimitExceeded:
        logger.warning("render_video[%s] soft time limit exceeded", job_id)
        fail(job_id, "TIMEOUT", _TIMEOUT_MSG)
        return {"ok": False, "code": "TIMEOUT", "reason": _TIMEOUT_MSG}

    except render_task.RenderError as exc:
        logger.warning("render_video[%s] failed: %s", job_id, exc.message)
        fail(job_id, exc.code, exc.message)
        return {"ok": False, "code": exc.code, "reason": exc.message}

    except Exception as exc:  # noqa: BLE001
        logger.exception("render_video[%s] unexpected error", job_id)
        fail(job_id, "WORKER_ERROR", f"Unexpected worker error: {exc!s}"[:300])
        return {"ok": False, "code": "WORKER_ERROR", "reason": str(exc)}

    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mb(path: Path) -> float:
    """Best-effort file-size in MB for logging."""
    try:
        return path.stat().st_size / 1_000_000.0
    except OSError:
        return 0.0
