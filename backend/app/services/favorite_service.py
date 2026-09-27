"""
Favourite voices: the voices a user has hearted, kept as voice prints, and
matched against the speakers of a job that is awaiting speaker selection.

How it fits together
--------------------
* The worker writes one voice print per speaker to `voiceprints` at the end
  of the snippet stage (worker/tasks/voiceprints.py). Those docs expire after
  a week (db/mongo.py): they are only needed while the job awaits selection.
* Hearting a speaker copies its print into a `favorite_voices` doc owned by
  the user, and copies its snippet clip to a permanent R2 key so the
  Favourite voices page can still play it after the job's ephemeral/ tree is
  deleted.
* Reading a job that awaits selection matches every speaker's print against
  the owner's favourites (shared/voice_match.py) to fill the "Your favourites
  in this video" box. Favourites saved from that same job are left out of
  its matching, so hearting a card never moves it into the box mid-session.
* Picking a speaker from the box teaches its favourite: the print is added as
  another sample (original + newest MAX_LEARNED_SAMPLES), so matching keeps up
  with how the person sounds on other days. Only high-confidence matches are
  learned, so one wrong match can't pollute a favourite.

Favourites are strictly per user. Only a job's OWNER gets favourite info or
can heart its speakers; an admin viewing someone else's job sees plain cards.
Voice prints never leave the backend.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import anyio.to_thread
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.db.mongo import get_db
from app.services.storage import get_storage, is_not_found
from shared import voice_match
from shared.constants import JobStatus, r2_key_favorite_sample

logger = logging.getLogger(__name__)

MAX_FAVORITES = 20
MAX_LEARNED_SAMPLES = 9

# Everything but the vectors — for listing / single reads.
_NO_VECTORS = {"_id": 0, "original_sample.centroid": 0, "learned_samples.centroid": 0}


class FavoriteError(Exception):
    """A user-facing refusal, carrying the HTTP status the route returns."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def owns(job: dict[str, Any], user: dict[str, Any] | None) -> bool:
    owner = job.get("user_id")
    return bool(user) and owner is not None and owner == user.get("user_id")


def clean_name(name: str | None) -> str | None:
    """The user's own label, trimmed; empty clears it. Never generated."""
    name = (name or "").strip()
    return name or None


def _sample_vectors(fav: dict[str, Any], model: Any, recipe: Any) -> list[list[float]]:
    """A favourite's sample centroids made with the same model + recipe."""
    out = []
    for s in [fav.get("original_sample"), *(fav.get("learned_samples") or [])]:
        if s and s.get("centroid") and s.get("model") == model \
                and s.get("recipe_version") == recipe:
            out.append(voice_match.unpack(s["centroid"]))
    return out


def box_matches(
    job_id: str,
    prints: dict[str, dict[str, Any]],
    favorites: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """{label: {favorite_id, score, strength}} for the favourites box."""
    if not prints or not favorites:
        return {}
    first = next(iter(prints.values()))
    model, recipe = first.get("model"), first.get("recipe_version")
    speakers = {
        label: voice_match.unpack(p["centroid"])
        for label, p in prints.items()
        if p.get("model") == model and p.get("recipe_version") == recipe
    }
    candidates = []
    for fav in favorites:
        if fav.get("source_job_id") == job_id:
            continue
        vecs = _sample_vectors(fav, model, recipe)
        if vecs:
            candidates.append((fav["favorite_id"], vecs))
    return voice_match.match_speakers(speakers, candidates)


def should_learn(match: dict[str, Any] | None, vp: dict[str, Any] | None) -> bool:
    """Only a confident match from a well-sampled speaker teaches a favourite."""
    return bool(
        match and vp
        and match["score"] >= voice_match.LEARN_THRESHOLD
        and float(vp.get("speech_sec") or 0.0) >= voice_match.LEARN_MIN_SPEECH_SEC
    )


def build_annotations(
    job: dict[str, Any],
    prints: dict[str, dict[str, Any]],
    favorites: list[dict[str, Any]],
    matches: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Per speaker label: {"favorite": {...} | None, "can_favorite": bool}."""
    names = {f["favorite_id"]: f.get("name") for f in favorites}
    # Voices hearted on THIS job: shown with a filled heart, in place.
    saved_here = {
        f.get("source_speaker_label"): f
        for f in favorites if f.get("source_job_id") == job.get("job_id")
    }
    out: dict[str, dict[str, Any]] = {}
    for sp in job.get("speakers") or []:
        label = sp.get("label")
        m = matches.get(label)
        if m:
            fav = {
                "favorite_id": m["favorite_id"],
                "name": names.get(m["favorite_id"]),
                "in_box": True,
                "strength": m["strength"],
            }
        elif label in saved_here:
            f = saved_here[label]
            fav = {"favorite_id": f["favorite_id"], "name": f.get("name"),
                   "in_box": False, "strength": None}
        else:
            fav = None
        out[label] = {
            "favorite": fav,
            "can_favorite": label in prints and bool(sp.get("snippet_key")),
        }
    return out


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

async def get_job_prints(job_id: str) -> dict[str, dict[str, Any]]:
    """label -> voiceprint doc (centroid only; the stored windows are skipped)."""
    db = get_db()
    cursor = db.voiceprints.find({"job_id": job_id}, {"_id": 0, "windows": 0})
    return {d["label"]: d async for d in cursor}


async def _favorites_with_vectors(user_id: str) -> list[dict[str, Any]]:
    db = get_db()
    cursor = db.favorite_voices.find({"user_id": user_id}, {"_id": 0})
    return [d async for d in cursor]


async def annotate(
    job: dict[str, Any], viewer: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]], int | None]:
    """
    Favourite info for each speaker of `job`, plus the viewer's favourite
    count. ({}, None) unless the viewer owns a job awaiting selection.
    """
    if job.get("status") != JobStatus.AWAITING_SELECTION.value or not owns(job, viewer):
        return {}, None
    prints = await get_job_prints(job["job_id"])
    favorites = await _favorites_with_vectors(viewer["user_id"])
    matches = box_matches(job["job_id"], prints, favorites)
    return build_annotations(job, prints, favorites, matches), len(favorites)


async def list_favorites(user_id: str) -> list[dict[str, Any]]:
    db = get_db()
    cursor = db.favorite_voices.find({"user_id": user_id}, _NO_VECTORS).sort("created_at", -1)
    return [d async for d in cursor]


async def get_favorite(user_id: str, favorite_id: str) -> dict[str, Any] | None:
    db = get_db()
    return await db.favorite_voices.find_one(
        {"user_id": user_id, "favorite_id": favorite_id}, _NO_VECTORS,
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

async def create_favorite(user: dict[str, Any], job_id: str, label: str) -> dict[str, Any]:
    """Heart `label` on `job_id`. Idempotent per (job, speaker)."""
    db = get_db()
    user_id = user["user_id"]
    job = await db.jobs.find_one(
        {"job_id": job_id},
        {"_id": 0, "job_id": 1, "user_id": 1, "status": 1, "speakers": 1, "video_title": 1},
    )
    if job is None or not owns(job, user):
        raise FavoriteError(404, "Job not found")
    if job.get("status") != JobStatus.AWAITING_SELECTION.value:
        raise FavoriteError(409, "Voices can only be saved while you're choosing a speaker.")
    speaker = next((s for s in job.get("speakers") or [] if s.get("label") == label), None)
    if speaker is None:
        raise FavoriteError(400, f"Speaker {label} not found in this job")

    existing = await db.favorite_voices.find_one(
        {"user_id": user_id, "source_job_id": job_id, "source_speaker_label": label},
        _NO_VECTORS,
    )
    if existing:
        return existing

    vp = await db.voiceprints.find_one({"job_id": job_id, "label": label}, {"_id": 0, "windows": 0})
    if vp is None:
        raise FavoriteError(
            422, "There's too little clear speech from this speaker to remember their voice.",
        )
    if await db.favorite_voices.count_documents({"user_id": user_id}) >= MAX_FAVORITES:
        raise FavoriteError(
            409,
            f"You have {MAX_FAVORITES} favourite voices — remove one on the "
            "Favourite voices page first.",
        )
    snippet_key = speaker.get("snippet_key")
    if not snippet_key:
        raise FavoriteError(410, "This speaker's sample is no longer available.")

    favorite_id = str(uuid.uuid4())
    sample_key = r2_key_favorite_sample(user_id, favorite_id)
    storage = get_storage()
    try:
        await anyio.to_thread.run_sync(storage.copy_object, snippet_key, sample_key)
    except Exception as exc:  # noqa: BLE001
        if is_not_found(exc):
            raise FavoriteError(
                410, "This speaker's sample has expired, so the voice can't be saved.",
            ) from exc
        raise

    now = datetime.now(timezone.utc)
    doc = {
        "favorite_id": favorite_id,
        "user_id": user_id,
        "name": None,
        "created_at": now,
        "updated_at": now,
        "source_job_id": job_id,
        "source_video_title": job.get("video_title"),
        "source_speaker_label": label,
        "sample_key": sample_key,
        "original_sample": _sample_from_print(job_id, label, vp, now),
        "learned_samples": [],
    }
    try:
        await db.favorite_voices.insert_one(doc)
    except DuplicateKeyError:
        # A double tap raced us to the same (job, speaker): keep theirs.
        await _delete_sample(sample_key)
        existing = await db.favorite_voices.find_one(
            {"user_id": user_id, "source_job_id": job_id, "source_speaker_label": label},
            _NO_VECTORS,
        )
        if existing:
            return existing
        raise
    doc.pop("_id", None)
    return doc


async def rename_favorite(user_id: str, favorite_id: str, name: str | None) -> dict[str, Any] | None:
    db = get_db()
    return await db.favorite_voices.find_one_and_update(
        {"user_id": user_id, "favorite_id": favorite_id},
        {"$set": {"name": clean_name(name), "updated_at": datetime.now(timezone.utc)}},
        projection=_NO_VECTORS,
        return_document=ReturnDocument.AFTER,
    )


async def delete_favorite(user_id: str, favorite_id: str) -> bool:
    db = get_db()
    doc = await db.favorite_voices.find_one_and_delete(
        {"user_id": user_id, "favorite_id": favorite_id},
        projection={"_id": 0, "sample_key": 1},
    )
    if doc is None:
        return False
    if doc.get("sample_key"):
        await _delete_sample(doc["sample_key"])
    return True


async def record_selection(job_id: str, label: str, user: dict[str, Any]) -> None:
    """
    After the owner picks a speaker: teach the matched favourite (when the
    match is confident), and store what the box showed next to what was
    picked on the job as `selection_context` — real-world data to re-tune the
    thresholds against, which outlives the week-long voiceprint TTL.
    """
    db = get_db()
    job = await db.jobs.find_one({"job_id": job_id}, {"_id": 0, "job_id": 1, "user_id": 1})
    if job is None or not owns(job, user):
        return
    prints = await get_job_prints(job_id)
    if not prints:
        return
    favorites = await _favorites_with_vectors(user["user_id"])
    matches = box_matches(job_id, prints, favorites)

    picked = matches.get(label)
    p = prints.get(label)
    learned = False
    if should_learn(picked, p):
        learned = await _learn(user["user_id"], picked["favorite_id"], job_id, label, p)

    keep = set(matches) | ({label} if p else set())
    await db.jobs.update_one(
        {"job_id": job_id},
        {"$set": {"selection_context": {
            "picked_label": label,
            "picked_from_box": label in matches,
            "box": {lb: m for lb, m in matches.items()},
            "favorites_total": len(favorites),
            "learned": learned,
            "centroids": {lb: prints[lb]["centroid"] for lb in keep},
            "model": p.get("model") if p else None,
            "recipe_version": p.get("recipe_version") if p else None,
            "at": datetime.now(timezone.utc),
        }}},
    )


async def record_selection_safe(job_id: str, label: str, user: dict[str, Any]) -> None:
    """BackgroundTasks entry point: selection bookkeeping must never surface."""
    try:
        await record_selection(job_id, label, user)
    except Exception:  # noqa: BLE001
        logger.warning("favourites: record_selection failed for %s", job_id, exc_info=True)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _sample_from_print(job_id: str, label: str, vp: dict[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "label": label,
        "centroid": vp["centroid"],
        "model": vp.get("model"),
        "recipe_version": vp.get("recipe_version"),
        "speech_sec": vp.get("speech_sec"),
        "added_at": now,
    }


async def _learn(
    user_id: str, favorite_id: str, job_id: str, label: str, vp: dict[str, Any],
) -> bool:
    """Append a learned sample; never the same (job, speaker) twice, and the
    $slice keeps only the newest MAX_LEARNED_SAMPLES (the original is separate,
    so it can never be evicted)."""
    now = datetime.now(timezone.utc)
    db = get_db()
    res = await db.favorite_voices.update_one(
        {
            "user_id": user_id,
            "favorite_id": favorite_id,
            "learned_samples": {"$not": {"$elemMatch": {"job_id": job_id, "label": label}}},
            "$nor": [{"original_sample.job_id": job_id, "original_sample.label": label}],
        },
        {
            "$push": {"learned_samples": {
                "$each": [_sample_from_print(job_id, label, vp, now)],
                "$slice": -MAX_LEARNED_SAMPLES,
            }},
            "$set": {"updated_at": now},
        },
    )
    return res.modified_count == 1


async def _delete_sample(sample_key: str) -> None:
    try:
        await anyio.to_thread.run_sync(get_storage().delete_object, sample_key)
    except Exception:  # noqa: BLE001
        logger.warning("favourites: could not delete sample %s", sample_key, exc_info=True)
