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

    # ---- Redis (used by Worker for Celery; deferred until M3+) -----------
    redis_url: Optional[str] = Field(default=None, validation_alias="REDIS_URL")

    # ---- Cloudflare R2 ---------------------------------------------------
    r2_access_key_id: str = Field(validation_alias="R2_ACCESS_KEY_ID")
    r2_secret_access_key: str = Field(validation_alias="R2_SECRET_ACCESS_KEY")
    r2_bucket_name: str = Field(validation_alias="R2_BUCKET_NAME")
    r2_endpoint_url: str = Field(validation_alias="R2_ENDPOINT_URL")

    # ---- HuggingFace (used by Worker for pyannote; deferred until M3+) ---
    hf_token: Optional[str] = Field(default=None, validation_alias="HF_TOKEN")

    # ---- App limits ------------------------------------------------------
    max_video_hours: int = Field(default=15, validation_alias="MAX_VIDEO_HOURS")


# Singleton — import this anywhere you need config.
settings = Settings()
