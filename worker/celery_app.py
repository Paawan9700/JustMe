"""
Celery application for the JustMe Worker.

Boot order:
  1. Load env vars from /app/backend/.env if it exists (dev convenience).
     In production the worker container will already have env vars set
     so the load_dotenv call is a no-op.
  2. Construct the Celery app pointing at REDIS_URL (Upstash, TLS).
  3. Import task modules so they register with Celery on worker startup.

Task names registered (must match what the API dispatches by name):
  - process_video  (worker.tasks.dummy.process_video)
  - render_video   (worker.tasks.dummy.render_video)
"""

from __future__ import annotations

import os
from pathlib import Path

from celery import Celery
from dotenv import load_dotenv

# ---- Env bootstrap --------------------------------------------------------
# When running inside the Emergent container, env vars live in
# /app/backend/.env. When deployed externally on a GPU box, the operator
# will set env vars directly — the load_dotenv call is harmless if the
# file doesn't exist.
_BACKEND_ENV = Path(__file__).resolve().parent.parent / "backend" / ".env"
if _BACKEND_ENV.exists():
    load_dotenv(_BACKEND_ENV)

REDIS_URL = os.environ.get("REDIS_URL")
if not REDIS_URL:
    raise RuntimeError(
        "REDIS_URL is required to start the worker. "
        "Set it in /app/backend/.env or in the worker's environment."
    )

# Celery + redis-py require explicit SSL config when using rediss://.
# Upstash uses Let's Encrypt-style valid certs, so CERT_REQUIRED is correct.
if REDIS_URL.startswith("rediss://") and "ssl_cert_reqs=" not in REDIS_URL:
    sep = "&" if "?" in REDIS_URL else "?"
    REDIS_URL = f"{REDIS_URL}{sep}ssl_cert_reqs=CERT_REQUIRED"


# ---- Celery app -----------------------------------------------------------
celery_app = Celery(
    "justme_worker",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["worker.tasks.dummy"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    # Acknowledge tasks only after they finish so a worker crash mid-task
    # doesn't lose the message.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)


__all__ = ["celery_app"]
