"""
HTTP endpoints for job lifecycle:

    POST /api/jobs                          - create a new job
    GET  /api/jobs/{job_id}                 - read job + hydrated URLs
    POST /api/jobs/{job_id}/select-speaker  - user picks their voice

All routes are mounted under /api so they're reachable through Emergent's
ingress.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, status

from app.models.job import (
    JobCreateRequest,
    JobCreateResponse,
    JobResponse,
    SelectSpeakerRequest,
    SelectSpeakerResponse,
)
from app.services import job_service
from app.services.queue import enqueue_process_video, enqueue_render_video

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["jobs"])


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

_YT_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtu.be",
}
# /watch?v=XXX  or /shorts/XXX  or youtu.be/XXX  — covers the formats we
# actually care about. Worker re-validates via yt-dlp before downloading.
_YT_WATCH_PATH = re.compile(r"^/(watch|shorts/[\w-]+|embed/[\w-]+)$")


def _validate_youtube_url(url: str) -> tuple[bool, str | None]:
    """
    Returns (is_valid, reject_reason). Lightweight format check only —
    authoritative validation happens in the worker via yt-dlp.
    """
    try:
        u = urlparse(url.strip())
    except Exception:
        return False, "Malformed URL"

    if u.scheme not in {"http", "https"}:
        return False, "URL must start with http:// or https://"

    host = (u.hostname or "").lower()
    if host not in _YT_HOSTS:
        return False, "URL is not a YouTube URL"

    path = u.path or "/"
    # Basic livestream guard — full check happens in M3 via yt-dlp.
    if "/live/" in path:
        return False, "Livestreams are not supported"

    if host == "youtu.be":
        # youtu.be/<video_id>
        if len(path) < 2:
            return False, "Missing video id in URL"
        return True, None

    # youtube.com — must have a known video path with an `?v=` param for /watch
    if _YT_WATCH_PATH.match(path):
        if path == "/watch":
            qs = dict([kv.split("=", 1) for kv in u.query.split("&") if "=" in kv])
            if not qs.get("v"):
                return False, "Missing video id (?v=) in URL"
        return True, None

    return False, "URL is not a recognised YouTube video URL"


# ---------------------------------------------------------------------------
# POST /api/jobs
# ---------------------------------------------------------------------------

@router.post("", response_model=JobCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_job(payload: JobCreateRequest) -> JobCreateResponse:
    ok, reason = _validate_youtube_url(payload.youtube_url)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)

    job = await job_service.create_job(payload.youtube_url)

    # Enqueue the worker task. If Redis is unreachable, surface a 502 so
    # the client knows the job won't progress — but the doc has already
    # been written so we keep it for visibility.
    try:
        task_id = enqueue_process_video(job["job_id"])
        await job_service.set_task_id(job["job_id"], task_id)
    except Exception as exc:
        logger.exception("Failed to enqueue process_video for %s", job["job_id"])
        # Mark as FAILED so the user sees the error on GET.
        await job_service.transition_status(
            job["job_id"],
            "FAILED",
            stage="enqueue",
            percent=0.0,
            message="Failed to enqueue background task",
            error={"code": "ENQUEUE_FAILED", "message": str(exc)},
        )
        raise HTTPException(
            status_code=502,
            detail="Background queue unavailable; job marked FAILED",
        ) from exc

    return JobCreateResponse(job_id=job["job_id"], status=job["status"])


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}
# ---------------------------------------------------------------------------

@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: str) -> JobResponse:
    doc = await job_service.get_job_hydrated(job_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Job not found")
    # JobResponse will silently drop fields it doesn't know about (e.g.
    # task_id, artifacts) — that's intentional, those are internal.
    return JobResponse(**doc)


# ---------------------------------------------------------------------------
# POST /api/jobs/{job_id}/select-speaker
# ---------------------------------------------------------------------------

@router.post("/{job_id}/select-speaker", response_model=SelectSpeakerResponse)
async def select_speaker(job_id: str, payload: SelectSpeakerRequest) -> SelectSpeakerResponse:
    result = await job_service.select_speaker(job_id, payload.speaker_label)

    if not result["ok"]:
        code = result["error_code"]
        if code == "NOT_FOUND":
            raise HTTPException(status_code=404, detail=result["message"])
        if code == "WRONG_STATE":
            raise HTTPException(status_code=409, detail=result["message"])
        if code == "SPEAKER_NOT_FOUND":
            raise HTTPException(status_code=400, detail=result["message"])
        raise HTTPException(status_code=500, detail=result["message"])

    # State has already been moved to RENDERING by select_speaker(); now
    # enqueue the render task.
    try:
        task_id = enqueue_render_video(job_id)
        await job_service.set_task_id(job_id, task_id)
    except Exception as exc:
        logger.exception("Failed to enqueue render_video for %s", job_id)
        await job_service.transition_status(
            job_id,
            "FAILED",
            stage="enqueue",
            message="Failed to enqueue render task",
            error={"code": "ENQUEUE_FAILED", "message": str(exc)},
        )
        raise HTTPException(
            status_code=502,
            detail="Background queue unavailable; job marked FAILED",
        ) from exc

    job = result["job"]
    return SelectSpeakerResponse(job_id=job["job_id"], status=job["status"])
