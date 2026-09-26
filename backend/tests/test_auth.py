"""
Unit tests for backend/app/services/auth.py (invite list, roles, session
tokens, job access) plus route-level checks that every job endpoint demands a
session and hides other users' jobs as 404.

Same hermetic style as test_recommendations.py: dummy env vars are set BEFORE
importing so Settings() validates without real credentials. Mongo and Google
are never touched — the service functions the routes call are swapped for
in-memory fakes. Run with:

    ./venv/bin/python backend/tests/test_auth.py
"""

import asyncio
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(ROOT))      # for `shared`
sys.path.insert(0, str(BACKEND))   # for `app`

for var in (
    "MONGO_URL", "DB_NAME", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET_NAME", "R2_ENDPOINT_URL",
):
    os.environ.setdefault(var, "test")

import jwt  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.services import auth  # noqa: E402

SECRET = "unit-test-secret-" + "x" * 32


def _configure(allowed="friend@gmail.com", admins="Owner@Gmail.com", secret=SECRET,
               open_signup=False, blocked=""):
    settings.allowed_emails = allowed
    settings.admin_emails = admins
    settings.auth_jwt_secret = secret
    settings.auth_token_ttl_days = 30
    settings.open_signup = open_signup
    settings.blocked_emails = blocked


# ---- invite list + roles ----------------------------------------------------

def test_parse_email_list_trims_lowercases_and_drops_blanks():
    assert auth.parse_email_list(" A@x.com, b@Y.com ,, ") == {"a@x.com", "b@y.com"}
    assert auth.parse_email_list("") == set()
    assert auth.parse_email_list(None) == set()


def test_is_allowed_is_case_insensitive_and_admins_are_implicit():
    _configure()
    assert auth.is_allowed("friend@gmail.com")
    assert auth.is_allowed("  FRIEND@gmail.com ")
    assert auth.is_allowed("owner@gmail.com")      # only listed in ADMIN_EMAILS
    assert not auth.is_allowed("stranger@gmail.com")
    assert not auth.is_allowed("")
    assert not auth.is_allowed(None)


def test_empty_invite_list_admits_only_admins():
    _configure(allowed="")
    assert auth.is_allowed("owner@gmail.com")
    assert not auth.is_allowed("friend@gmail.com")


def test_open_signup_admits_any_verified_account():
    _configure(allowed="", open_signup=True)
    assert auth.is_allowed("anyone@gmail.com")
    assert auth.is_allowed("owner@gmail.com")
    assert not auth.is_allowed("")


def test_blocked_beats_open_signup_and_invite_list_but_never_admins():
    _configure(open_signup=True, blocked="Spammer@gmail.com, friend@gmail.com")
    assert not auth.is_allowed("spammer@gmail.com")
    assert auth.is_blocked("SPAMMER@gmail.com")
    assert not auth.is_allowed("friend@gmail.com")        # invited AND blocked -> blocked
    assert auth.is_allowed("someone-else@gmail.com")
    _configure(blocked="owner@gmail.com")
    assert auth.is_allowed("owner@gmail.com")              # an admin can't lock themself out
    assert not auth.is_blocked("owner@gmail.com")
    _configure()


def test_role_for():
    _configure()
    assert auth.role_for("OWNER@gmail.com") == "admin"
    assert auth.role_for("friend@gmail.com") == "user"
    assert auth.role_for(None) == "user"


# ---- session tokens ----------------------------------------------------------

def test_token_round_trip():
    _configure()
    assert auth.decode_token(auth.issue_token("user-123")) == "user-123"


def test_expired_token_is_rejected():
    _configure()
    old = datetime.now(timezone.utc) - timedelta(days=31)
    token = auth.issue_token("user-123", now=old)
    try:
        auth.decode_token(token)
    except auth.AuthError as e:
        assert "expired" in str(e)
    else:
        raise AssertionError("expired token was accepted")


def test_token_signed_with_another_secret_is_rejected():
    _configure()
    forged = jwt.encode(
        {"sub": "user-123", "iat": datetime.now(timezone.utc),
         "exp": datetime.now(timezone.utc) + timedelta(days=1)},
        "some-other-secret-" + "y" * 32, algorithm="HS256",
    )
    try:
        auth.decode_token(forged)
    except auth.AuthError:
        pass
    else:
        raise AssertionError("forged token was accepted")


def test_tampered_and_unsigned_tokens_are_rejected():
    _configure()
    good = auth.issue_token("user-123")
    header, payload, sig = good.split(".")
    tampered = ".".join([header, payload, sig[:-2] + ("AA" if sig[-2:] != "AA" else "BB")])
    unsigned = jwt.encode({"sub": "user-123"}, key="", algorithm="none")
    for bad in (tampered, unsigned, "garbage", ""):
        try:
            auth.decode_token(bad)
        except auth.AuthError:
            continue
        raise AssertionError(f"bad token accepted: {bad[:20]!r}")


def test_missing_secret_fails_closed():
    _configure(secret=None)
    for call in (lambda: auth.issue_token("u"), lambda: auth.decode_token("x.y.z")):
        try:
            call()
        except auth.AuthNotConfigured:
            continue
        raise AssertionError("worked without AUTH_JWT_SECRET")
    _configure()


# ---- job access ---------------------------------------------------------------

def test_can_access():
    owner = {"user_id": "u1", "role": "user"}
    stranger = {"user_id": "u2", "role": "user"}
    admin = {"user_id": "u9", "role": "admin"}
    job = {"user_id": "u1"}
    unowned = {}  # pre-auth job, not yet claimed
    assert auth.can_access(job, owner)
    assert not auth.can_access(job, stranger)
    assert auth.can_access(job, admin)
    assert not auth.can_access(unowned, owner)
    assert auth.can_access(unowned, admin)


# ---- routes (in-memory fakes, no Mongo) ------------------------------------------

def _client():
    """TestClient over the real app with Mongo-backed service calls faked.
    Not used as a context manager, so the Mongo-touching lifespan never runs."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import job_service, user_service

    users = {
        "u-owner": {"user_id": "u-owner", "email": "owner@gmail.com", "name": "Owner"},
        "u-friend": {"user_id": "u-friend", "email": "friend@gmail.com", "name": "Friend"},
    }
    jobs = {"job-of-owner": {"user_id": "u-owner"}}

    async def get_user(user_id):
        return dict(users[user_id]) if user_id in users else None

    async def get_job_owner(job_id):
        return jobs.get(job_id)

    async def list_jobs(limit=100, *, user_id=None):
        list_jobs.last_user_id = user_id
        return []

    user_service.get_user = get_user
    job_service.get_job_owner = get_job_owner
    job_service.list_jobs = list_jobs
    return TestClient(app), list_jobs


def _bearer(user_id):
    return {"Authorization": f"Bearer {auth.issue_token(user_id)}"}


def test_job_routes_require_a_session():
    _configure()
    client, _ = _client()
    for method, path in (
        ("get", "/api/jobs"),
        ("post", "/api/jobs"),
        ("get", "/api/jobs/job-of-owner"),
        ("post", "/api/jobs/job-of-owner/select-speaker"),
        ("post", "/api/jobs/job-of-owner/generate-recommendations"),
        ("get", "/api/auth/me"),
    ):
        r = getattr(client, method)(path)
        assert r.status_code == 401, f"{method.upper()} {path} -> {r.status_code}"
    r = client.get("/api/jobs", headers={"Authorization": "Bearer not-a-token"})
    assert r.status_code == 401


def test_health_stays_public():
    client, _ = _client()
    assert client.get("/api").status_code == 200


def test_other_users_job_is_404_but_owner_and_admin_pass_the_check():
    _configure()
    client, _ = _client()
    r = client.get("/api/jobs/job-of-owner", headers=_bearer("u-friend"))
    assert r.status_code == 404, r.status_code
    r = client.post(
        "/api/jobs/job-of-owner/select-speaker",
        json={"speaker_label": "SPEAKER_00"}, headers=_bearer("u-friend"),
    )
    assert r.status_code == 404, r.status_code
    r = client.get("/api/jobs/does-not-exist", headers=_bearer("u-owner"))
    assert r.status_code == 404, r.status_code


def test_list_scope_all_only_for_admins():
    _configure()
    client, list_jobs = _client()
    client.get("/api/jobs?scope=all", headers=_bearer("u-friend"))
    assert list_jobs.last_user_id == "u-friend"   # silently scoped to self
    client.get("/api/jobs", headers=_bearer("u-owner"))
    assert list_jobs.last_user_id == "u-owner"    # admin default = own jobs
    client.get("/api/jobs?scope=all", headers=_bearer("u-owner"))
    assert list_jobs.last_user_id is None         # admin everyone view


def test_removed_from_invite_list_revokes_a_live_token():
    _configure()
    client, _ = _client()
    token_headers = _bearer("u-friend")
    assert client.get("/api/auth/me", headers=token_headers).status_code == 200
    _configure(allowed="")                        # friend removed
    assert client.get("/api/auth/me", headers=token_headers).status_code == 401
    _configure()


def test_me_reports_role_from_current_admin_list():
    _configure()
    client, _ = _client()
    assert client.get("/api/auth/me", headers=_bearer("u-owner")).json()["role"] == "admin"
    assert client.get("/api/auth/me", headers=_bearer("u-friend")).json()["role"] == "user"


def test_sign_in_is_503_when_not_configured():
    _configure()
    settings.google_client_id = None
    client, _ = _client()
    r = client.post("/api/auth/google", json={"credential": "x"})
    assert r.status_code == 503, r.status_code


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
