"""
HTTP endpoints for sign-in:

    POST /api/auth/google   - exchange a Google ID token for a session token
    GET  /api/auth/me       - who am I (validates the stored session)
"""

from __future__ import annotations

import logging
from typing import Any

import anyio.to_thread
from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_current_user
from app.core.config import settings
from app.models.auth import AuthResponse, GoogleSignInRequest, UserResponse
from app.services import auth, user_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/google", response_model=AuthResponse)
async def google_sign_in(payload: GoogleSignInRequest) -> AuthResponse:
    if not settings.google_client_id or not settings.auth_jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sign-in is not configured on the server.",
        )

    # Verification fetches Google's signing certs — blocking network I/O, so
    # keep it off the event loop (same pattern as job dispatch in api/jobs.py).
    try:
        claims = await anyio.to_thread.run_sync(
            auth.verify_google_credential, payload.credential,
        )
    except auth.AuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    email = claims["email"].strip().lower()
    if auth.is_blocked(email):
        logger.warning("sign-in refused for blocked account %s", email)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account can't use Alphavox. Contact the admin if you think this is a mistake.",
        )
    if not auth.is_allowed(email):
        logger.warning("sign-in refused for uninvited account %s", email)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"{email} isn't on the invite list yet. "
                "Ask the admin to add you, then sign in again."
            ),
        )

    role = auth.role_for(email)
    user = await user_service.upsert_google_user(claims, role=role)
    logger.info("sign-in ok: %s (%s)", email, role)
    return AuthResponse(token=auth.issue_token(user["user_id"]), user=UserResponse(**user))


@router.get("/me", response_model=UserResponse)
async def me(user: dict[str, Any] = Depends(get_current_user)) -> UserResponse:
    return UserResponse(**user)
