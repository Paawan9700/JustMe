"""
MongoDB connection and index management.

We use Motor (async PyMongo) so FastAPI request handlers can await DB
calls without blocking the event loop.

Collections:
    - jobs:     one document per JustMe job (owned by jobs.user_id)
    - segments: per-speaker diarization segments for a job
    - users:    one document per signed-in Google account

Indexes are (re)created on app startup via `init_db()`. Index creation in
MongoDB is idempotent, so this is safe to run on every boot.
"""

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING

from app.core.config import settings


_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


def get_client() -> AsyncIOMotorClient:
    """Return the global Motor client, creating it on first use."""
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(settings.mongo_uri)
    return _client


def get_db() -> AsyncIOMotorDatabase:
    """Return the application database handle."""
    global _db
    if _db is None:
        _db = get_client()[settings.db_name]
    return _db


async def init_db() -> None:
    """
    Ensure required indexes exist. Called once on FastAPI startup.

    jobs:
        - unique index on job_id
        - index on status
        - index on created_at
        - compound index on (user_id, created_at desc) — "My Videos"
    segments:
        - compound index on (job_id, speaker)
    users:
        - unique index on user_id and on google_sub
        - index on email (NOT unique: a recreated Google account can come
          back with a new sub but the same address)
    """
    db = get_db()

    await db.jobs.create_index([("job_id", ASCENDING)], unique=True, name="uniq_job_id")
    await db.jobs.create_index([("status", ASCENDING)], name="status_idx")
    await db.jobs.create_index([("created_at", ASCENDING)], name="created_at_idx")
    await db.jobs.create_index(
        [("user_id", ASCENDING), ("created_at", DESCENDING)],
        name="user_created_idx",
    )

    await db.segments.create_index(
        [("job_id", ASCENDING), ("speaker", ASCENDING)],
        name="job_speaker_idx",
    )

    await db.users.create_index([("user_id", ASCENDING)], unique=True, name="uniq_user_id")
    await db.users.create_index([("google_sub", ASCENDING)], unique=True, name="uniq_google_sub")
    await db.users.create_index([("email", ASCENDING)], name="email_idx")


async def ping() -> bool:
    """Lightweight liveness check used by /health."""
    try:
        # `ping` is a cheap admin command. Will raise on connection failure.
        await get_client().admin.command("ping")
        return True
    except Exception:
        return False


async def close_db() -> None:
    """Close Motor client on shutdown."""
    global _client, _db
    if _client is not None:
        _client.close()
        _client = None
        _db = None
