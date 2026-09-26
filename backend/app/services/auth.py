"""
Authentication: Google sign-in exchanged for our own session token.

Flow:
  1. The browser gets a Google ID token from Google Identity Services.
  2. POST /api/auth/google hands it to `verify_google_credential`, which checks
     signature, audience (GOOGLE_CLIENT_ID), issuer, expiry and email_verified.
  3. The email must be allowed: any account when OPEN_SIGNUP is on, otherwise
     the invite list (ALLOWED_EMAILS, plus ADMIN_EMAILS). BLOCKED_EMAILS
     always refuses, except for admins.
  4. We issue OUR OWN HS256 JWT (`issue_token`) that the frontend sends as
     `Authorization: Bearer ...` on every call.

Why our own token instead of forwarding Google's: Google ID tokens expire
after one hour, which would sign a user out in the middle of watching a long
job process. Ours lasts AUTH_TOKEN_TTL_DAYS.

Why a Bearer header instead of a cookie: the frontend and API live on
different Render domains, so a cookie would have to be SameSite=None and
still get eaten by third-party-cookie blocking.

The list/role/token/access helpers are pure (no I/O) and unit-tested in
backend/tests/test_auth.py. Everything fails closed: a missing secret raises
AuthNotConfigured, never "let everyone in".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt  # PyJWT

from app.core.config import settings

_ALGORITHM = "HS256"

# Google's own timestamps vs our server clock: a few seconds of drift would
# otherwise surface as a baffling "Token used too early" on sign-in.
_GOOGLE_CLOCK_SKEW_S = 10


class AuthError(Exception):
    """The credential or token is invalid, expired, or not acceptable."""


class AuthNotConfigured(Exception):
    """A required auth setting (secret / client id) is missing on the server."""


# ---------------------------------------------------------------------------
# Invite list + roles
# ---------------------------------------------------------------------------

def parse_email_list(raw: str | None) -> set[str]:
    """'A@x.com, b@y.com ,,' -> {'a@x.com', 'b@y.com'}."""
    return {e.strip().lower() for e in (raw or "").split(",") if e.strip()}


def _admins() -> set[str]:
    return parse_email_list(settings.admin_emails)


def is_blocked(email: str | None) -> bool:
    """True if this (non-admin) email is on BLOCKED_EMAILS."""
    if not email:
        return False
    email = email.strip().lower()
    return email not in _admins() and email in parse_email_list(settings.blocked_emails)


def is_allowed(email: str | None) -> bool:
    """
    True if this email may sign in. Order of precedence:
    admin (always) > blocked (never) > OPEN_SIGNUP (anyone) > invite list.
    """
    if not email:
        return False
    email = email.strip().lower()
    if email in _admins():
        return True
    if is_blocked(email):
        return False
    if settings.open_signup:
        return True
    return email in parse_email_list(settings.allowed_emails)


def role_for(email: str | None) -> str:
    """'admin' for ADMIN_EMAILS, else 'user'. Recomputed per request, so an
    ADMIN_EMAILS change takes effect on restart without touching the DB."""
    return "admin" if email and email.strip().lower() in _admins() else "user"


def can_access(job_doc: dict[str, Any], user: dict[str, Any]) -> bool:
    """Owner or admin. A job with no owner (pre-auth jobs, until claimed by
    backend/scripts/claim_unowned_jobs.py) is visible to admins only."""
    if user.get("role") == "admin":
        return True
    owner = job_doc.get("user_id")
    return owner is not None and owner == user.get("user_id")


# ---------------------------------------------------------------------------
# Session tokens
# ---------------------------------------------------------------------------

def _secret() -> str:
    if not settings.auth_jwt_secret:
        raise AuthNotConfigured("AUTH_JWT_SECRET is not configured")
    return settings.auth_jwt_secret


def issue_token(user_id: str, *, now: datetime | None = None) -> str:
    """Signed session token whose `sub` is our user_id."""
    now = now or datetime.now(timezone.utc)
    claims = {
        "sub": user_id,
        "iat": now,
        "exp": now + timedelta(days=settings.auth_token_ttl_days),
    }
    return jwt.encode(claims, _secret(), algorithm=_ALGORITHM)


def decode_token(token: str) -> str:
    """Return the user_id inside a valid token; raise AuthError otherwise.

    `algorithms` is pinned, so a token claiming alg=none (or any other
    algorithm) is rejected rather than trusted.
    """
    secret = _secret()
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[_ALGORITHM],
            options={"require": ["sub", "exp", "iat"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Your session has expired. Please sign in again.") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("Invalid session. Please sign in again.") from exc
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise AuthError("Invalid session. Please sign in again.")
    return sub


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------

def verify_google_credential(credential: str) -> dict[str, Any]:
    """
    Verify a Google Identity Services ID token and return its claims.

    Blocking (fetches Google's signing certs over the network) — call it via
    anyio.to_thread.run_sync. google-auth checks the signature, `aud` against
    our client id, `iss`, and expiry; we additionally require a verified email.
    """
    if not settings.google_client_id:
        raise AuthNotConfigured("GOOGLE_CLIENT_ID is not configured")

    # Lazy import: keeps this module importable where google-auth is absent.
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    try:
        claims = id_token.verify_oauth2_token(
            credential,
            google_requests.Request(),
            settings.google_client_id,
            clock_skew_in_seconds=_GOOGLE_CLOCK_SKEW_S,
        )
    except ValueError as exc:  # google-auth raises ValueError for any bad token
        raise AuthError("Google sign-in could not be verified. Please try again.") from exc

    if not claims.get("email") or not claims.get("email_verified"):
        raise AuthError("Your Google account's email is not verified.")
    if not claims.get("sub"):
        raise AuthError("Google sign-in could not be verified. Please try again.")
    return claims
