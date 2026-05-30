"""
Celery client used by the API to enqueue tasks for the Worker.

The API never imports worker task code directly — instead it dispatches
by task name through the shared Redis broker. This keeps the API
deployable without the worker's heavy AI dependencies.
"""

from functools import lru_cache
from typing import Any

from celery import Celery

from app.core.config import settings


@lru_cache(maxsize=1)
def get_celery() -> Celery:
    """
    Return a Celery instance configured against Upstash Redis.

    Both broker and result backend point at REDIS_URL. Upstash uses TLS
    (`rediss://` scheme); Celery + redis-py require `ssl_cert_reqs` to be
    set explicitly when the URL uses rediss://.
    """
    if not settings.redis_url:
        raise RuntimeError("REDIS_URL is not configured")

    url = settings.redis_url
    if url.startswith("rediss://") and "ssl_cert_reqs=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}ssl_cert_reqs=CERT_REQUIRED"

    app = Celery(
        "justme_api",
        broker=url,
        backend=url,
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        # Don't auto-discover anything on the API side — we only ever
        # call send_task() by name. Worker has its own Celery app.
        broker_connection_retry_on_startup=True,
    )
    return app


def enqueue_process_video(job_id: str) -> str:
    """Dispatch the `process_video` task; returns the Celery task id."""
    result = get_celery().send_task("process_video", args=[job_id])
    return result.id


def enqueue_render_video(job_id: str) -> str:
    """Dispatch the `render_video` task; returns the Celery task id."""
    result = get_celery().send_task("render_video", args=[job_id])
    return result.id


__all__: list[Any] = ["enqueue_process_video", "enqueue_render_video", "get_celery"]
