"""
Per-user usage limits (non-admins only):

  * USER_DAILY_VIDEO_LIMIT videos per IST calendar day (default 5). The count
    is jobs created since IST midnight that have NOT failed, so our own
    outages (a burned proxy IP, a worker crash) never cost the user a slot,
    and an in-flight job counts from the moment it's submitted.
  * USER_MAX_VIDEO_HOURS per video (default 5). The API can't know a video's
    length before the worker probes it, so the limit is stamped onto the job
    doc (`max_video_hours`) and the worker enforces it at ingest, before any
    download (worker/tasks/ingest.py::resolve_max_video_hours).

Admins have no daily limit and get MAX_VIDEO_HOURS (default 15) per video.

Known gap, accepted at this scale: the count-then-insert in POST /api/jobs
isn't atomic, so two submissions in the same instant could let one extra job
through.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.config import settings
from app.services import job_service

# India has no DST, so a fixed offset is exact.
IST = timezone(timedelta(hours=5, minutes=30), "IST")


def ist_day_window(now: datetime) -> tuple[datetime, datetime]:
    """[start, end) of the IST calendar day containing `now`, as UTC datetimes."""
    local = now.astimezone(IST)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def is_admin(user: dict[str, Any]) -> bool:
    return user.get("role") == "admin"


def max_video_hours_for(user: dict[str, Any]) -> int:
    return settings.max_video_hours if is_admin(user) else settings.user_max_video_hours


async def usage_for(user: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Today's usage + limits for this user, shaped like UsageResponse."""
    start, end = ist_day_window(now or datetime.now(timezone.utc))
    used = await job_service.count_jobs_since(user["user_id"], start)
    if is_admin(user):
        daily_limit = remaining = None
    else:
        daily_limit = settings.user_daily_video_limit
        remaining = max(0, daily_limit - used)
    return {
        "daily_limit": daily_limit,
        "used_today": used,
        "remaining_today": remaining,
        "max_video_hours": max_video_hours_for(user),
        "resets_at": end,
    }
