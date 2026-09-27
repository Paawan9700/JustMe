"""Request/response models for the Favourite voices API."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class FavoriteCreateRequest(BaseModel):
    job_id: str = Field(..., min_length=1)
    speaker_label: str = Field(..., min_length=1)


class FavoriteRenameRequest(BaseModel):
    # Empty / whitespace clears the name.
    name: Optional[str] = Field(None, max_length=60)


class FavoriteResponse(BaseModel):
    favorite_id: str
    name: Optional[str] = None
    created_at: datetime
    source_job_id: str
    source_video_title: Optional[str] = None
    sample_url: Optional[str] = None  # presigned (1 h) original sample clip
    sample_count: int = 1             # the original + samples learned since
