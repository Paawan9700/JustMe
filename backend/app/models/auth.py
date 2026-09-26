"""Pydantic request/response models for the Auth API."""

from typing import Optional

from pydantic import BaseModel, Field


class GoogleSignInRequest(BaseModel):
    # The ID token (JWT) Google Identity Services hands the browser.
    credential: str = Field(..., min_length=1)


class UserResponse(BaseModel):
    user_id: str
    email: str
    name: Optional[str] = None
    picture: Optional[str] = None
    role: str = "user"


class AuthResponse(BaseModel):
    token: str
    user: UserResponse
