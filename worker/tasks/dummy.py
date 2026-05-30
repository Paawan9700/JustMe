"""
Pipeline orchestrator + still-mocked snippet/render stages.

What's real (since M3 / M4 / M5):
  - DOWNLOADING        -> worker.tasks.ingest.download_video
  - EXTRACTING_AUDIO   -> worker.tasks.audio.extract_audio
  - DIARIZING          -> worker.tasks.diarize.run_diarization
                          (WhisperX + pyannote, GPU only — dev box
                          falls back to a mocked 2-speaker fixture so
                          the Emergent-hosted M2 UI can still be
                          exercised)
  - GENERATING_SNIPPETS -> worker.tasks.snippets.generate_snippets
                           (ffmpeg cuts a 6s mp3 per speaker, uploads
                           to R2, stamps snippet_key on each speaker)

What's still mocked (will land in M6):
  - RENDERING -> DONE    -> render_video task; final_video_key stays null

Real livestream detection happens in ingest via yt-dlp's `is_live` field;
the URL-pattern check that used to live here was removed in the M2 bug
fix. See worker/tasks/ingest.py.

Tasks registered with Celery:
  - process_video(job_id)
  - render_video(job_id)

Local working directory:
  /tmp/justme/{job_id}/   (created on entry, recursively removed in finally)
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Any

from worker.celery_app import celery_app
from worker.db import get_db
from worker.state import fail, progress, transition
from worker.tasks import audio as audio_task
from worker.tasks import diarize as diarize_task
from worker.tasks import ingest as ingest_task
from worker.tasks import snippets as snippets_task
from shared.constants import JobStatus, r2_key_audio

logger = logging.getLogger(__name__)

JOB_TMP_ROOT = Path("/tmp/justme")


# ---------------------------------------------------------------------------
# Task 1: process_video — ingest -> audio -> diarize -> awaiting_selection
# ---------------------------------------------------------------------------

@celery_app.task(name="process_video", bind=True)
def process_video(self, job_id: str) -> dict[str, Any]:  # noqa: ARG001
    logger.info("process_video[%s] start", job_id)

    db = get_db()
    job = db.jobs.find_one(
        {"job_id": job_id},
        {"youtube_url": 1, "duration_sec": 1, "_id": 0},
    )
    if not job:
        logger.warning("process_video[%s] job not found", job_id)
        return {"ok": False, "reason": "job not found"}

    youtube_url = job["youtube_url"]
    job_dir = JOB_TMP_ROOT / job_id

    try:
        job_dir.mkdir(parents=True, exist_ok=True)

        # ---- DOWNLOADING (real) ---------------------------------------
        local_video = ingest_task.download_video(job_id, youtube_url, job_dir)

        # ---- EXTRACTING_AUDIO (real) ----------------------------------
        audio_task.extract_audio(job_id, local_video, job_dir)

        # ---- DIARIZING (real, with dev fallback only on MISSING_DEPS) -
        duration_sec = (
            db.jobs.find_one({"job_id": job_id}, {"duration_sec": 1, "_id": 0})
            or {}
        ).get("duration_sec") or 0
        _diarize_or_fallback(job_id, job_dir, duration_sec)

        # ---- GENERATING_SNIPPETS -> AWAITING_SELECTION (real, M5) -----
        snippets_task.generate_snippets(job_id, job_dir)

        logger.info("process_video[%s] done -> AWAITING_SELECTION", job_id)
        return {"ok": True, "job_id": job_id}

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
    the deps aren't installed (i.e. we're running in the Emergent dev
    container), fall back to a mocked 2-speaker fixture so the M2 UI
    flow can still be exercised end-to-end during development.

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
    """
    Stand-in for real diarization when whisperx isn't installed.
    Used only by the Emergent CPU-only dev container so the rest of
    the pipeline (snippet/render mocks, frontend states) stays
    exercisable. Status is already DIARIZING when we get here.
    """
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


def _dummy_snippets_stage(job_id: str) -> None:
    """
    DEPRECATED. The real snippet stage now lives in
    `worker/tasks/snippets.py:generate_snippets()`. This function is
    kept for one release as a compatibility shim in case anything
    external still imports it.
    """
    snippets_task.generate_snippets(job_id, JOB_TMP_ROOT / job_id)


# ---------------------------------------------------------------------------
# Task 2: render_video (still mocked — real in M6)
# ---------------------------------------------------------------------------

@celery_app.task(name="render_video", bind=True)
def render_video(self, job_id: str) -> dict[str, Any]:  # noqa: ARG001
    logger.info("render_video[%s] start", job_id)

    progress(job_id, stage="rendering", percent=0.0, message="Rendering started")
    time.sleep(5)
    progress(job_id, percent=80.0, message="Stitching segments...")

    transition(
        job_id, JobStatus.DONE.value,
        stage="done", percent=100.0, message="Render complete",
    )
    logger.info("render_video[%s] done", job_id)
    return {"ok": True, "job_id": job_id}
