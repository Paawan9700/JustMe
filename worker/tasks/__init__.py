"""Worker task package.

Deliberately free of imports: Celery registers its tasks via
`include=["worker.tasks.dummy"]` in worker/celery_app.py, and Modal
containers import worker.tasks.pipeline directly. Importing dummy here
would drag in worker.celery_app, which raises at import time when
REDIS_URL is unset — the Modal path has no Redis at all.
"""
