"""
HTTP endpoints for job lifecycle:

    POST /api/jobs                          - create a new job
    GET  /api/jobs                          - list recent jobs (My Jobs)
    GET  /api/jobs/{job_id}                 - read job + hydrated URLs
    POST /api/jobs/{job_id}/select-speaker  - user picks their voice

All routes are mounted under /api 
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, HTTPException, status

from app.core.config import settings
from app.models.job import (
    GenerateRecommendationsResponse,
    JobCreateRequest,
    JobCreateResponse,
    JobResponse,
    JobSummaryResponse,
    SelectSpeakerRequest,
    SelectSpeakerResponse,
)
from app.services import job_service, recommendations
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
# Allowed video paths on youtube.com:
#   /watch (must have ?v=...)
#   /live/<id>     - includes COMPLETED past livestreams, fully recorded.
#                    Whether a stream is happening right now is decided by
#                    yt-dlp's `is_live` metadata in M3, NOT by URL pattern.
#   /embed/<id>
# Shorts and playlists are rejected with dedicated messages below.
_YT_VIDEO_PATH = re.compile(r"^/(watch|live/[\w-]+|embed/[\w-]+)$")


def _validate_youtube_url(url: str) -> tuple[bool, str | None]:
    """
    Returns (is_valid, reject_reason). Lightweight format check only —
    authoritative validation (including is_live) happens in the worker
    via yt-dlp.

    M7 (hardening) compliance — the three rules are:
      1. Reject non-YouTube hosts.
      2. Reject playlist URLs (`/playlist` or `?list=`).
      3. Reject YouTube Shorts (`/shorts/`).
    All three were introduced in the M2 URL-validation bug fix; M7
    re-verified them via curl. No code changes required.
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
    query = u.query or ""

    # Playlists: dedicated path or any URL carrying a list= parameter.
    if path == "/playlist" or path.startswith("/playlist/"):
        return False, "Playlist URLs are not supported. Please use a single video URL."
    if "list=" in query:
        return False, "Playlist URLs are not supported. Please use a single video URL."

    # YouTube Shorts.
    if path == "/shorts" or path.startswith("/shorts/"):
        return False, "YouTube Shorts are not supported"

    if host == "youtu.be":
        # youtu.be/<video_id>
        if len(path) < 2:
            return False, "Missing video id in URL"
        return True, None

    # youtube.com / m.youtube.com / music.youtube.com — accept /watch,
    # /live/<id>, /embed/<id>. The /live/ form includes completed
    # past livestreams (allowed); the live-RIGHT-NOW case is rejected
    # later by the worker after yt-dlp tells us is_live=True.
    if _YT_VIDEO_PATH.match(path):
        if path == "/watch":
            qs = dict([kv.split("=", 1) for kv in query.split("&") if "=" in kv])
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
# GET /api/jobs   (My Jobs list — lightweight, no presigned URLs)
# ---------------------------------------------------------------------------

@router.get("", response_model=list[JobSummaryResponse])
async def list_jobs(limit: int = 100) -> list[JobSummaryResponse]:
    docs = await job_service.list_jobs(limit=limit)
    return [JobSummaryResponse(**d) for d in docs]


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


# ---------------------------------------------------------------------------
# POST /api/jobs/{job_id}/generate-recommendations
# ---------------------------------------------------------------------------

@router.post(
    "/{job_id}/generate-recommendations",
    response_model=GenerateRecommendationsResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_recommendations(
    job_id: str, background_tasks: BackgroundTasks
) -> GenerateRecommendationsResponse:
    """
    Kick off LLM extraction of stock recommendations from the job's final video.
    Runs as an in-process background task; the client polls GET /api/jobs/{id}
    and reads `recommendations_status` / `recommendations_url`.
    """
    # Fail fast if the feature isn't configured — don't enter GENERATING.
    if not settings.gemini_api_key:
        raise HTTPException(
            status_code=503,
            detail="Recommendations are not available — LLM key not configured.",
        )

    result = await job_service.claim_recommendations_generating(job_id)
    if not result["ok"]:
        code = result["error_code"]
        if code == "NOT_FOUND":
            raise HTTPException(status_code=404, detail=result["message"])
        if code == "NO_VIDEO":
            raise HTTPException(status_code=422, detail=result["message"])
        if code in ("WRONG_STATE", "ALREADY_GENERATING"):
            raise HTTPException(status_code=409, detail=result["message"])
        raise HTTPException(status_code=500, detail=result["message"])

    background_tasks.add_task(recommendations.generate_for_job, job_id)
    return GenerateRecommendationsResponse(
        job_id=job_id, recommendations_status="GENERATING"
    )
