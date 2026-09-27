"""
MongoDB connection and index management.

We use Motor (async PyMongo) so FastAPI request handlers can await DB
calls without blocking the event loop.

Collections:
    - jobs:     one document per JustMe job (owned by jobs.user_id)
    - segments: per-speaker diarization segments for a job
    - users:    one document per signed-in Google account
    - voiceprints:     one voice print per (job, speaker); short-lived (TTL)
    - favorite_voices: a user's saved favourite voices

Indexes are (re)created on app startup via `init_db()`. Index creation in
MongoDB is idempotent, so this is safe to run on every boot.
"""

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING

from app.core.config import settings

# Voice prints only matter while a job awaits speaker selection, and the
# snippets that page plays expire within ~2 days anyway. NOTE: to change this
# on an existing deployment use `collMod`; create_index with a different
# expireAfterSeconds raises IndexOptionsConflict and would stop the API booting.
VOICEPRINT_TTL_SEC = 7 * 24 * 3600


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
    voiceprints:
        - unique (job_id, label)
        - TTL on created_at (VOICEPRINT_TTL_SEC)
    favorite_voices:
        - unique favorite_id
        - (user_id, created_at desc) — the Favourite voices page
        - unique (user_id, source_job_id, source_speaker_label) — one heart
          per speaker, even under a double tap
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

    await db.voiceprints.create_index(
        [("job_id", ASCENDING), ("label", ASCENDING)], unique=True, name="uniq_job_label",
    )
    await db.voiceprints.create_index(
        [("created_at", ASCENDING)],
        expireAfterSeconds=VOICEPRINT_TTL_SEC, name="ttl_created_at",
    )

    await db.favorite_voices.create_index(
        [("favorite_id", ASCENDING)], unique=True, name="uniq_favorite_id",
    )
    await db.favorite_voices.create_index(
        [("user_id", ASCENDING), ("created_at", DESCENDING)], name="user_created_idx",
    )
    await db.favorite_voices.create_index(
        [("user_id", ASCENDING), ("source_job_id", ASCENDING), ("source_speaker_label", ASCENDING)],
        unique=True, name="uniq_user_source",
    )


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
