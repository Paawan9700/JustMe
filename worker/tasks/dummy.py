"""
Celery task registrations — thin wrappers only.

The actual orchestration lives in worker/tasks/pipeline.py so it can be
invoked from either dispatcher:

  - Celery (this module): legacy/fallback path used by the resident
    worker (`celery -A worker.celery_app worker`) on RunPod / local dev
    / Modal's `run_worker`.
  - Modal (worker/modal_app.py): primary path — the API spawns deployed
    `process_video` / `render_video` Modal functions on demand.

Task names, retry policy and binding are unchanged from the original
implementation so the API's Celery `send_task("process_video"| \
"render_video")` fallback keeps working byte-for-byte.
"""

from __future__ import annotations

from typing import Any

from worker.celery_app import celery_app
from worker.tasks import pipeline


@celery_app.task(
    name="process_video",
    bind=True,
    max_retries=2,
    default_retry_delay=60,
)
def process_video(self, job_id: str) -> dict[str, Any]:  # noqa: ARG001
    return pipeline.run_process_video(job_id)


@celery_app.task(
    name="render_video",
    bind=True,
    max_retries=1,
    default_retry_delay=30,
)
def render_video(self, job_id: str) -> dict[str, Any]:  # noqa: ARG001
    return pipeline.run_render_video(job_id)
