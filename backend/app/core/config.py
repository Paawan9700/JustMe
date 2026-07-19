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
    max_video_hours: int = Field(default=15, validation_alias="MAX_VIDEO_HOURS")

    # ---- Gemini (stock-recommendations feature) -------------------------
    # Optional so the app still boots without it; the generate-recommendations
    # endpoint returns a clear 503 when the key is absent. Get a free key from
    # Google AI Studio: https://aistudio.google.com/apikey
    gemini_api_key: Optional[str] = Field(default=None, validation_alias="GEMINI_API_KEY")
    # Default stays gemini-2.5-flash (battle-tested, free tier) as the rollback
    # baseline; production overrides via GEMINI_MODEL. Probed 2026-07-19 on our
    # key: gemini-3.5-flash and gemini-3-flash-preview WORK on the free tier;
    # 2.5-pro / 3-pro-preview / 3.1-pro-preview are quota-0 (paid-only).
    gemini_model: str = Field(default="gemini-2.5-flash", validation_alias="GEMINI_MODEL")


# Singleton — import this anywhere you need config.
settings = Settings()
