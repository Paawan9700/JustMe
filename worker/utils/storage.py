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
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
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


def delete_file(r2_key: str) -> None:
    """
    Delete one object. Idempotent: an already-absent key is not an error.

    Used to reclaim the `ephemeral/` intermediates (source.mp4, audio.wav) the
    moment the stage that needed them is finished — see the layout notes in
    shared/constants.py for why code deletion is required rather than a
    lifecycle rule alone.
    """
    _client().delete_object(Bucket=_bucket(), Key=r2_key)


def delete_prefix(prefix: str) -> int:
    """
    Delete every object under `prefix`; returns how many were removed.

    Needed for the per-speaker snippet fan-out, where the key count isn't known
    up front. Pages the listing and deletes in batches of 1000 (the S3 API cap).

    Refuses a prefix that doesn't start with "ephemeral/" — this only ever exists
    to reclaim intermediates, and a bad caller must not be able to wipe the
    durable `jobs/` deliverables.
    """
    if not prefix.startswith("ephemeral/"):
        raise ValueError(
            f"delete_prefix refuses non-ephemeral prefix {prefix!r} — "
            "durable artifacts under jobs/ must never be bulk-deleted"
        )
    client, bucket, removed = _client(), _bucket(), 0
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if not keys:
            continue
        for i in range(0, len(keys), 1000):
            client.delete_objects(Bucket=bucket, Delete={"Objects": keys[i:i + 1000]})
        removed += len(keys)
    return removed
