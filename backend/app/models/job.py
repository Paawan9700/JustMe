"""
Pydantic request/response models for the Jobs API.

These define the wire format — what FastAPI accepts and returns. The
MongoDB document layout itself is handled in `app.services.job_service`.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------

class JobCreateRequest(BaseModel):
    youtube_url: str = Field(..., min_length=1)


class SelectSpeakerRequest(BaseModel):
    speaker_label: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------

class JobCreateResponse(BaseModel):
    job_id: str
    status: str


class JobProgress(BaseModel):
    stage: str = ""
    percent: float = 0.0
    message: str = ""


class JobError(BaseModel):
    code: str
    message: str


class SpeakerFavorite(BaseModel):
    """This speaker's link to one of the viewer's favourite voices."""
    favorite_id: str
    name: Optional[str] = None      # the user's own name for it, never generated
    in_box: bool = False            # matched by voice -> "Your favourites" box
    strength: Optional[str] = None  # "strong" | "likely" when in_box


class SpeakerInfoResponse(BaseModel):
    label: str
    total_speaking_sec: float
    segment_count: int
    # Presigned URL injected at read time; null if snippet not yet uploaded.
    snippet_url: Optional[str] = None
    # Favourite voices — only filled for the job's owner while the job is
    # AWAITING_SELECTION; null/false otherwise (old jobs, admin views).
    favorite: Optional[SpeakerFavorite] = None
    can_favorite: bool = False


class JobResponse(BaseModel):
    job_id: str
    status: str
    progress: JobProgress
    error: Optional[JobError] = None
    video_title: Optional[str] = None
    duration_sec: int = 0
    speakers: list[SpeakerInfoResponse] = []
    # How many favourite voices the viewer has (null when favourites don't
    # apply to this view) — drives the "none found" vs "tap ❤️" hint.
    favorites_total: Optional[int] = None
    selected_speaker: Optional[str] = None
    download_url: Optional[str] = None  # presigned, only when status == DONE
    transcription_url: Optional[str] = None  # presigned .txt, only when status == DONE
    # Stock-recommendations sub-resource (independent of `status`).
    recommendations_status: Optional[str] = None  # None | GENERATING | READY | FAILED
    recommendations_url: Optional[str] = None  # presigned .csv, only when READY
    recommendations_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class JobSummaryResponse(BaseModel):
    """Lightweight row for the My Jobs list. Strict subset of the job doc —
    no presigned URLs / speakers / artifacts, so listing stays cheap."""
    job_id: str
    status: str
    video_title: Optional[str] = None
    youtube_url: str  # title fallback in UI (video_title is null early on)
    user_email: Optional[str] = None  # owner; shown in the admin "everyone" view
    duration_sec: int = 0
    progress_percent: float = 0.0  # flattened from progress.percent
    selected_speaker: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class UsageResponse(BaseModel):
    """Today's usage for the signed-in user. Nulls mean "no limit" (admin)."""
    daily_limit: Optional[int] = None
    used_today: int = 0
    remaining_today: Optional[int] = None
    max_video_hours: int
    resets_at: datetime  # next IST midnight, as UTC


class SelectSpeakerResponse(BaseModel):
    job_id: str
    status: str


class GenerateRecommendationsResponse(BaseModel):
    job_id: str
    recommendations_status: str
