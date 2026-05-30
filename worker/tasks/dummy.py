"""
Pipeline orchestrator + still-mocked diarization/snippet stages.

What's real (since M3):
  - DOWNLOADING        -> worker.tasks.ingest.download_video
  - EXTRACTING_AUDIO   -> worker.tasks.audio.extract_audio

What's still mocked (will land in M5 + M6):
  - DIARIZING + GENERATING_SNIPPETS + AWAITING_SELECTION
    (fake speakers SPEAKER_00 / SPEAKER_01 with snippet_key=null)
  - RENDERING -> DONE (render_video task; final_video_key stays null)

Real livestream detection happens in M3 via yt-dlp metadata.
We check the 'is_live' field from yt-dlp's info extraction:
  - is_live == True  -> stream is happening RIGHT NOW   -> reject
  - is_live == False OR was_live == True -> completed recording -> allow
Do NOT detect livestreams from URL patterns - they are unreliable
(e.g. completed past livestreams keep the /live/<id> URL form).

When the worker rejects an active livestream it uses the constant
LIVE_STREAM_REJECT_MESSAGE from shared/constants.py:
    "This video is currently live. Please wait until the stream ends
     and try again."

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
from worker.tasks import ingest as ingest_task
from shared.constants import JobStatus

logger = logging.getLogger(__name__)

JOB_TMP_ROOT = Path("/tmp/justme")


# ---------------------------------------------------------------------------
# Task 1: process_video — ingest -> audio -> (dummy) diarize -> awaiting
# ---------------------------------------------------------------------------

@celery_app.task(name="process_video", bind=True)
def process_video(self, job_id: str) -> dict[str, Any]:  # noqa: ARG001
    logger.info("process_video[%s] start", job_id)

    db = get_db()
    job = db.jobs.find_one({"job_id": job_id}, {"youtube_url": 1, "_id": 0})
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

        # Both artifacts are in R2 now; local files no longer needed
        # for the dummy diarization that follows. We'll still wipe the
        # whole job_dir in `finally`.

        # ---- DIARIZING (still mocked — replaced in M5) ----------------
        _run_dummy_diarization(job_id)

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

    except Exception as exc:  # noqa: BLE001
        logger.exception("process_video[%s] unexpected error", job_id)
        fail(job_id, "WORKER_ERROR", f"Unexpected worker error: {exc!s}"[:300])
        return {"ok": False, "code": "WORKER_ERROR", "reason": str(exc)}

    finally:
        # Disk hygiene — always remove the job's local working dir.
        shutil.rmtree(job_dir, ignore_errors=True)


def _run_dummy_diarization(job_id: str) -> None:
    """Placeholder for the M5 diarization. Same behaviour as M1 dummy."""
    transition(
        job_id, JobStatus.DIARIZING.value,
        stage="diarizing", percent=0.0,
        message="Diarization starting",
    )
    time.sleep(2)
    progress(job_id, percent=50.0, message="Diarizing speakers...")
    time.sleep(2)
    progress(job_id, percent=100.0, message="Diarization complete")

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
    transition(
        job_id, JobStatus.GENERATING_SNIPPETS.value,
        stage="generating_snippets", percent=0.0,
        message="Generating identification snippets",
        extra_set={"speakers": fake_speakers},
    )
    time.sleep(2)
    transition(
        job_id, JobStatus.AWAITING_SELECTION.value,
        stage="awaiting_selection", percent=100.0,
        message="Please select your voice",
    )


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
