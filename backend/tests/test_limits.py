"""
Tests for per-user limits (backend/app/services/limits.py + POST /api/jobs +
GET /api/usage): 5 videos per IST day for regular users (failed jobs don't
count), a 5-hour cap stamped on each job, and no daily limit / a 15-hour cap
for admins. Mongo, Google and Modal are never touched — the service calls
are swapped for in-memory fakes. Run with:

    ./venv/bin/python backend/tests/test_limits.py
"""

import os
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BACKEND))

for var in (
    "MONGO_URL", "DB_NAME", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET_NAME", "R2_ENDPOINT_URL",
):
    os.environ.setdefault(var, "test")

from app.core.config import settings  # noqa: E402
from app.services import auth, limits  # noqa: E402

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _configure():
    settings.auth_jwt_secret = "limits-test-secret-" + "q" * 32
    settings.admin_emails = "owner@gmail.com"
    settings.allowed_emails = ""
    settings.open_signup = True
    settings.blocked_emails = ""
    settings.user_daily_video_limit = 5
    settings.user_max_video_hours = 5
    settings.max_video_hours = 15


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# ---- IST day window ------------------------------------------------------------

def test_day_window_afternoon_ist():
    # 10:00 UTC = 15:30 IST on the 26th -> day is 26th 00:00 IST = 25th 18:30 UTC
    start, end = limits.ist_day_window(_utc(2026, 9, 26, 10, 0))
    assert start == _utc(2026, 9, 25, 18, 30)
    assert end == _utc(2026, 9, 26, 18, 30)


def test_day_window_just_after_ist_midnight():
    # 18:45 UTC on the 26th = 00:15 IST on the 27th -> a NEW IST day
    start, end = limits.ist_day_window(_utc(2026, 9, 26, 18, 45))
    assert start == _utc(2026, 9, 26, 18, 30)
    assert end == _utc(2026, 9, 27, 18, 30)


def test_day_window_just_before_ist_midnight():
    start, _ = limits.ist_day_window(_utc(2026, 9, 26, 18, 29))
    assert start == _utc(2026, 9, 25, 18, 30)


# ---- routes -------------------------------------------------------------------------

def _client(used_today: int):
    from fastapi.testclient import TestClient

    import app.api.jobs as jobs_api
    from app.main import app
    from app.services import job_service, user_service

    users = {
        "u-owner": {"user_id": "u-owner", "email": "owner@gmail.com"},
        "u-user": {"user_id": "u-user", "email": "someone@gmail.com"},
    }
    created = []

    async def get_user(user_id):
        return dict(users[user_id]) if user_id in users else None

    async def count_jobs_since(user_id, since):
        return used_today

    async def create_job(youtube_url, *, user_id, user_email, max_video_hours):
        doc = {"job_id": f"job-{len(created)}", "status": "QUEUED", "user_id": user_id,
               "max_video_hours": max_video_hours}
        created.append(doc)
        return doc

    async def set_task_id(job_id, task_id):
        return None

    user_service.get_user = get_user
    job_service.count_jobs_since = count_jobs_since
    job_service.create_job = create_job
    job_service.set_task_id = set_task_id
    jobs_api.enqueue_process_video = lambda job_id: "fc-fake"   # never touch Modal
    return TestClient(app), created


def _bearer(user_id):
    return {"Authorization": f"Bearer {auth.issue_token(user_id)}"}


def test_user_under_the_daily_limit_gets_a_5_hour_job():
    _configure()
    client, created = _client(used_today=4)
    r = client.post("/api/jobs", json={"youtube_url": URL}, headers=_bearer("u-user"))
    assert r.status_code == 201, r.text
    assert created[-1]["max_video_hours"] == 5


def test_sixth_video_of_the_day_is_refused_with_429():
    _configure()
    client, created = _client(used_today=5)
    r = client.post("/api/jobs", json={"youtube_url": URL}, headers=_bearer("u-user"))
    assert r.status_code == 429, r.status_code
    assert "5 videos for today" in r.json()["detail"]
    assert created == []   # nothing created, nothing dispatched


def test_admin_has_no_daily_limit_and_a_15_hour_cap():
    _configure()
    client, created = _client(used_today=50)
    r = client.post("/api/jobs", json={"youtube_url": URL}, headers=_bearer("u-owner"))
    assert r.status_code == 201, r.text
    assert created[-1]["max_video_hours"] == 15


def test_bad_url_is_rejected_before_counting():
    _configure()
    client, created = _client(used_today=5)
    r = client.post("/api/jobs", json={"youtube_url": "https://vimeo.com/1"}, headers=_bearer("u-user"))
    assert r.status_code == 400
    assert created == []


def test_usage_endpoint_for_user_and_admin():
    _configure()
    client, _ = _client(used_today=3)
    u = client.get("/api/usage", headers=_bearer("u-user")).json()
    assert (u["daily_limit"], u["used_today"], u["remaining_today"], u["max_video_hours"]) == (5, 3, 2, 5)
    a = client.get("/api/usage", headers=_bearer("u-owner")).json()
    assert a["daily_limit"] is None and a["remaining_today"] is None and a["max_video_hours"] == 15
    assert client.get("/api/usage").status_code == 401


def test_limits_follow_settings():
    _configure()
    settings.user_daily_video_limit = 2
    settings.user_max_video_hours = 3
    client, created = _client(used_today=1)
    r = client.post("/api/jobs", json={"youtube_url": URL}, headers=_bearer("u-user"))
    assert r.status_code == 201 and created[-1]["max_video_hours"] == 3
    client, _ = _client(used_today=2)
    assert client.post("/api/jobs", json={"youtube_url": URL}, headers=_bearer("u-user")).status_code == 429
    _configure()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
