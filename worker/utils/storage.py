"""
Worker-side Cloudflare R2 helpers.

Mirrors backend/app/services/storage.py but is fully self-contained
so the worker can be deployed and run without the backend package.
Credentials come from environment variables.

R2 ARTIFACT EXPIRY (production setup — do this in the Cloudflare dashboard):
    Go to:  R2 -> <bucket> -> Settings -> Object Lifecycle Rules -> Add Rule
    Name:    "Delete old job artifacts"
    Prefix:  jobs/
    Action:  "Delete objects" after  7 days  since object upload date
    Status:  Enabled
This wipes source.mp4, audio.wav, snippets/*.mp3 and final.mp4 a week
after each job is created so storage costs stay bounded. The API and
worker code do not perform deletion themselves — lifecycle rules are
the canonical, retry-safe mechanism for this.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from functools import lru_cache

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Explicit content types for the artifacts we produce. boto3's upload_file
# does NOT sniff content type — it defaults to binary/octet-stream, which
# breaks HTML5 <audio>/<video> streaming (the browser can't tell it's
# seekable media, so playback stalls after ~1s). Map by extension.
_CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".webm": "video/webm",
}


def _guess_content_type(local_path: str) -> str:
    _, ext = os.path.splitext(local_path.lower())
    if ext in _CONTENT_TYPES:
        return _CONTENT_TYPES[ext]
    guessed, _ = mimetypes.guess_type(local_path)
    return guessed or "application/octet-stream"


def _bucket() -> str:
    return os.environ["R2_BUCKET_NAME"]


@lru_cache(maxsize=1)
def _client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def upload_file(local_path: str, r2_key: str) -> None:
    content_type = _guess_content_type(local_path)
    _client().upload_file(
        local_path,
        _bucket(),
        r2_key,
        ExtraArgs={"ContentType": content_type},
    )
    logger.info(
        "storage: uploaded %s -> %s (Content-Type=%s)",
        local_path, r2_key, content_type,
    )


def download_file(r2_key: str, local_path: str) -> None:
    _client().download_file(_bucket(), r2_key, local_path)


def get_presigned_url(r2_key: str, expires_in: int = 3600) -> str:
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": _bucket(), "Key": r2_key},
        ExpiresIn=expires_in,
    )


def file_exists(r2_key: str) -> bool:
    try:
        _client().head_object(Bucket=_bucket(), Key=r2_key)
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise
