"""
User persistence (`users` collection).

One document per Google account:
    user_id        our own id (uuid4) — what jobs.user_id points at
    google_sub     Google's stable account id (unique) — the upsert key
    email          lowercased; refreshed on every sign-in
    name, picture  from the Google profile; refreshed on every sign-in
    role           "admin" | "user" as of the last sign-in (informational —
                   access checks recompute it from ADMIN_EMAILS per request)
    created_at, last_login_at

Keyed by `google_sub` rather than email because Google guarantees `sub` is
stable for the life of the account, while the email on it can change.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pymongo import ReturnDocument

from app.db.mongo import get_db


async def upsert_google_user(claims: dict[str, Any], *, role: str) -> dict[str, Any]:
    """Create the user on first sign-in, refresh profile fields after that."""
    db = get_db()
    now = datetime.now(timezone.utc)
    return await db.users.find_one_and_update(
        {"google_sub": claims["sub"]},
        {
            "$set": {
                "email": claims["email"].strip().lower(),
                "name": claims.get("name"),
                "picture": claims.get("picture"),
                "role": role,
                "last_login_at": now,
            },
            "$setOnInsert": {
                "user_id": str(uuid.uuid4()),
                "google_sub": claims["sub"],
                "created_at": now,
            },
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0},
    )


async def get_user(user_id: str) -> dict[str, Any] | None:
    """Return the user doc (without _id) or None."""
    db = get_db()
    return await db.users.find_one({"user_id": user_id}, {"_id": 0})
