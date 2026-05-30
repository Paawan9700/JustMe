"""Worker task package.

Importing the task modules here is enough for Celery autodiscovery
when running `celery -A worker.celery_app worker`.
"""

from . import dummy  # noqa: F401
