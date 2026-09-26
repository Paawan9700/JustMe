"""
Application configuration.

All values come from environment variables (loaded from backend/.env in
development). Missing required values cause the app to fail fast on
startup, which is the behaviour we want — no silent fallbacks.

Note on naming: the project spec uses MONGO_URI as the conceptual name,
but the Emergent platform pre-provisions MONGO_URL for the local managed
MongoDB. We honour both by reading from MONGO_URL via a Pydantic alias.
"""

from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- MongoDB ---------------------------------------------------------
    # `mongo_uri` is the field name used throughout the codebase, but the
    # actual env var is MONGO_URL (Emergent convention).
    mongo_uri: str = Field(validation_alias="MONGO_URL")
    db_name: str = Field(validation_alias="DB_NAME")

    # ---- Redis (Celery fallback dispatch only; see queue_backend) --------
    redis_url: Optional[str] = Field(default=None, validation_alias="REDIS_URL")

    # ---- Job dispatch ------------------------------------------------------
    # "modal" (default): spawn the deployed Modal functions on demand —
    # containers start per job and scale to zero after, so idle cost is $0.
    # "celery": legacy path — publish to Upstash Redis for a resident worker
    # (requires REDIS_URL here and a running worker, e.g. modal_app.run_worker).
    queue_backend: str = Field(default="modal", validation_alias="QUEUE_BACKEND")
    modal_app_name: str = Field(default="justme-worker", validation_alias="MODAL_APP_NAME")
    modal_token_id: Optional[str] = Field(default=None, validation_alias="MODAL_TOKEN_ID")
    modal_token_secret: Optional[str] = Field(default=None, validation_alias="MODAL_TOKEN_SECRET")

    # ---- Cloudflare R2 ---------------------------------------------------
    r2_access_key_id: str = Field(validation_alias="R2_ACCESS_KEY_ID")
    r2_secret_access_key: str = Field(validation_alias="R2_SECRET_ACCESS_KEY")
    r2_bucket_name: str = Field(validation_alias="R2_BUCKET_NAME")
    r2_endpoint_url: str = Field(validation_alias="R2_ENDPOINT_URL")

    # ---- HuggingFace (used by Worker for pyannote; deferred until M3+) ---
    hf_token: Optional[str] = Field(default=None, validation_alias="HF_TOKEN")

    # ---- App limits ------------------------------------------------------
    # Longest video an ADMIN may submit. Regular users get
    # user_max_video_hours instead. The API stamps the right value onto each
    # job and the worker enforces it at ingest.
    max_video_hours: int = Field(default=15, validation_alias="MAX_VIDEO_HOURS")

    # ---- Gemini (stock-recommendations feature) -------------------------
    # Optional so the app still boots without it; the generate-recommendations
    # endpoint returns a clear 503 when the key is absent. Get a free key from
    # Google AI Studio: https://aistudio.google.com/apikey
    gemini_api_key: Optional[str] = Field(default=None, validation_alias="GEMINI_API_KEY")
    # Default stays gemini-2.5-flash (battle-tested, free tier) as the rollback
    # baseline; production overrides via GEMINI_MODEL (currently
    # gemini-3-flash-preview).
    #
    # DO NOT "upgrade" this without re-running the real pass-1 request against
    # the candidate — cheap text probes prove nothing. Re-probed 2026-08-14 with
    # the actual transcription prompt on an 8:53 clip:
    #   gemini-3-flash-preview  audio 13,730 tok / 63.9s / full transcript  OK
    #   gemini-3.5-flash        403 PERMISSION_DENIED on video, and on audio
    #                           returns a ONE-CHARACTER transcript after 299s —
    #                           which trips the empty-transcript branch below and
    #                           silently yields zero recommendations. Worse than
    #                           an error. (The 2026-07-19 note that it "works" is
    #                           superseded.)
    #   gemini-3.6 / 3.7-flash  503 UNAVAILABLE on the real request
    #   2.5-pro                 404 on our key
    # So gemini-3-flash-preview is the only model that actually does this job.
    # There is no viable model fallback; transient failures are handled by the
    # backoff retry in services/recommendations.py instead.
    gemini_model: str = Field(default="gemini-2.5-flash", validation_alias="GEMINI_MODEL")

    # ---- Auth (Google sign-in -> our own session JWT) --------------------
    # Optional so the app (and the hermetic unit tests) still boot without
    # them, but auth FAILS CLOSED: with no AUTH_JWT_SECRET every protected
    # route returns 503, and with no GOOGLE_CLIENT_ID nobody can sign in.
    # There is no "auth disabled" mode.
    google_client_id: Optional[str] = Field(default=None, validation_alias="GOOGLE_CLIENT_ID")
    auth_jwt_secret: Optional[str] = Field(default=None, validation_alias="AUTH_JWT_SECRET")
    # Comma-separated Google emails. Admins are implicitly allowed, can see
    # every user's jobs, and are the only ones who can open a job they don't
    # own. Kept as plain strings (not list[str]) so pydantic-settings doesn't
    # try to JSON-decode them.
    admin_emails: str = Field(default="", validation_alias="ADMIN_EMAILS")
    allowed_emails: str = Field(default="", validation_alias="ALLOWED_EMAILS")
    auth_token_ttl_days: int = Field(default=30, validation_alias="AUTH_TOKEN_TTL_DAYS")

    # ---- Sign-up + per-user limits ---------------------------------------
    # OPEN_SIGNUP=true: any verified Google account may sign in and
    # ALLOWED_EMAILS stops mattering. BLOCKED_EMAILS always wins over both
    # (except for admins), so one abusive account can be shut out without
    # closing sign-up for everyone.
    open_signup: bool = Field(default=False, validation_alias="OPEN_SIGNUP")
    blocked_emails: str = Field(default="", validation_alias="BLOCKED_EMAILS")
    # Non-admin limits. The day is the IST calendar day (resets at midnight
    # IST), and failed jobs don't count, so our own outages never burn a
    # user's quota.
    user_daily_video_limit: int = Field(default=5, validation_alias="USER_DAILY_VIDEO_LIMIT")
    user_max_video_hours: int = Field(default=5, validation_alias="USER_MAX_VIDEO_HOURS")


# Singleton — import this anywhere you need config.
settings = Settings()
