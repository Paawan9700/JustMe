"""
Shared FastAPI dependencies.

`get_current_user` guards every non-health route. It resolves the Bearer
session token to a user doc and returns it with `role` recomputed from the
current ADMIN_EMAILS. It also re-checks the invite list on every request, so
removing someone from ALLOWED_EMAILS revokes their access at the next restart
even though their token has not expired yet.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.services import auth, user_service

# auto_error=False: we want our own 401 message (and a 401, not FastAPI's
# default 403) when the header is missing.
_bearer = HTTPBearer(auto_error=False)

_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer"}


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict[str, Any]:
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Please sign in.",
            headers=_UNAUTHORIZED_HEADERS,
        )
    try:
        user_id = auth.decode_token(creds.credentials)
    except auth.AuthNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sign-in is not configured on the server.",
        ) from exc
    except auth.AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers=_UNAUTHORIZED_HEADERS,
        ) from exc

    user = await user_service.get_user(user_id)
    if user is None or not auth.is_allowed(user.get("email")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your access has been removed. Please sign in again.",
            headers=_UNAUTHORIZED_HEADERS,
        )
    user["role"] = auth.role_for(user.get("email"))
    return user
