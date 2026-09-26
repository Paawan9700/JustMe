"""
GET /api/usage — today's usage and limits for the signed-in user, so the
UI can show "3 of 5 videos left today · up to 5 hours per video" before
someone submits. The limits themselves are enforced in POST /api/jobs.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.models.job import UsageResponse
from app.services import limits

router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.get("", response_model=UsageResponse)
async def get_usage(user: dict[str, Any] = Depends(get_current_user)) -> UsageResponse:
    return UsageResponse(**(await limits.usage_for(user)))
