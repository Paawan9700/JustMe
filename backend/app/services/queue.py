"""
Job dispatch used by the API to hand work to the Worker.

Two backends, selected by settings.queue_backend (env QUEUE_BACKEND):

  * "modal" (default) — spawn the deployed Modal functions
    (worker/modal_app.py: process_video / render_video) directly. A GPU
    container starts on demand, bills per-second while working, and
    scales to zero afterwards; nothing runs (or costs) while idle.
    Requires MODAL_TOKEN_ID / MODAL_TOKEN_SECRET.

  * "celery" — legacy fallback: publish by task name to Upstash Redis
    for a resident Celery worker (RunPod, local dev, or Modal's
    run_worker). Requires REDIS_URL and a worker that's actually
    running.

Either way the API never imports worker task code — dispatch is by
name, keeping the API deployable without the worker's heavy AI
dependencies. Errors deliberately propagate to the callers in
api/jobs.py, which mark the job FAILED (code=ENQUEUE_FAILED) and
return HTTP 502.
"""

from functools import lru_cache
from typing import Any

from app.core.config import settings


# ---------------------------------------------------------------------------
# Modal backend (primary)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=None)
def _modal_function(name: str) -> Any:
    """
    Lazy, cached handle to a deployed Modal function.

    pydantic-settings loads backend/.env into the Settings object only —
    NOT os.environ — while the modal client authenticates from env vars,
    so bridge the token pair across before importing the client.
    `Function.from_name` itself is lazy (no network); hydration happens on
    the first .spawn() and the cached handle is reused afterwards.
    """
    import os

    if settings.modal_token_id and not os.environ.get("MODAL_TOKEN_ID"):
        os.environ["MODAL_TOKEN_ID"] = settings.modal_token_id
    if settings.modal_token_secret and not os.environ.get("MODAL_TOKEN_SECRET"):
        os.environ["MODAL_TOKEN_SECRET"] = settings.modal_token_secret

    import modal  # lazy: celery-mode deployments need not install modal

    return modal.Function.from_name(settings.modal_app_name, name)


def _spawn(fn_name: str, job_id: str) -> str:
    """Fire-and-forget spawn; returns the FunctionCall id ("fc-...")."""
    call = _modal_function(fn_name).spawn(job_id)
    return call.object_id


# ---------------------------------------------------------------------------
# Celery backend (legacy fallback)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_celery() -> Any:
    """
    Return a Celery instance configured against Upstash Redis.

    Both broker and result backend point at REDIS_URL. Upstash uses TLS
    (`rediss://` scheme); Celery + redis-py require `ssl_cert_reqs` to be
    set explicitly when the URL uses rediss://.
    """
    from celery import Celery

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


# ---------------------------------------------------------------------------
# Public dispatch API (signatures unchanged — see api/jobs.py call sites)
# ---------------------------------------------------------------------------

def _dispatch(task_name: str, job_id: str) -> str:
    backend = settings.queue_backend
    if backend == "modal":
        return _spawn(task_name, job_id)
    if backend == "celery":
        return get_celery().send_task(task_name, args=[job_id]).id
    raise RuntimeError(f"Unknown QUEUE_BACKEND: {backend!r} (expected 'modal' or 'celery')")


def enqueue_process_video(job_id: str) -> str:
    """Dispatch `process_video`; returns the dispatch id (fc-... or Celery task id)."""
    return _dispatch("process_video", job_id)


def enqueue_render_video(job_id: str) -> str:
    """Dispatch `render_video`; returns the dispatch id (fc-... or Celery task id)."""
    return _dispatch("render_video", job_id)


__all__: list[Any] = ["enqueue_process_video", "enqueue_render_video", "get_celery"]
