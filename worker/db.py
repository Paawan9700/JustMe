"""
Sync MongoDB helper for the Worker.

The API uses Motor (async). Celery tasks are sync, so we use plain
pymongo here. The connection is process-global and lazily created.
"""

from __future__ import annotations

import os

from pymongo import MongoClient
from pymongo.database import Database

_client: MongoClient | None = None
_db: Database | None = None


def get_db() -> Database:
    """Return the application database; create the client on first use."""
    global _client, _db
    if _db is None:
        mongo_url = os.environ.get("MONGO_URL")
        db_name = os.environ.get("DB_NAME")
        if not mongo_url or not db_name:
            raise RuntimeError("MONGO_URL and DB_NAME must be set for the worker")
        _client = MongoClient(mongo_url)
        _db = _client[db_name]
    return _db
