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


class SpeakerInfoResponse(BaseModel):
    label: str
    total_speaking_sec: float
    segment_count: int
    # Presigned URL injected at read time; null if snippet not yet uploaded.
    snippet_url: Optional[str] = None


class JobResponse(BaseModel):
    job_id: str
    status: str
    progress: JobProgress
    error: Optional[JobError] = None
    video_title: Optional[str] = None
    duration_sec: int = 0
    speakers: list[SpeakerInfoResponse] = []
    selected_speaker: Optional[str] = None
    download_url: Optional[str] = None  # presigned, only when status == DONE
    created_at: datetime
    updated_at: datetime


class SelectSpeakerResponse(BaseModel):
    job_id: str
    status: str
