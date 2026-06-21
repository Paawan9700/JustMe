"""
Cloudflare R2 storage service (S3-compatible via boto3).

R2 speaks the S3 API, so we use the standard boto3 S3 client pointed at
the R2 endpoint. Bucket name is supplied separately on every call — the
endpoint URL must be the bare account endpoint, not the per-bucket URL.

Usage:
    storage = get_storage()
    storage.upload_file("/tmp/video.mp4", "jobs/abc/source.mp4")
    url = storage.get_presigned_url("jobs/abc/final.mp4")
"""

import logging
import os
from functools import lru_cache

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.core.config import settings

logger = logging.getLogger(__name__)

# boto3's upload_file does not sniff content type (defaults to
# binary/octet-stream), which breaks HTML5 <audio>/<video> streaming.
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
    return _CONTENT_TYPES.get(ext, "application/octet-stream")


class R2Storage:
    def __init__(self) -> None:
        self.bucket = settings.r2_bucket_name
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint_url,
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            # R2 requires SigV4. "auto" region works for R2.
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )

    # ---- core operations -------------------------------------------------

    def upload_file(self, local_path: str, r2_key: str) -> None:
        """Upload a local file to R2 under the given key."""
        self._client.upload_file(
            local_path,
            self.bucket,
            r2_key,
            ExtraArgs={"ContentType": _guess_content_type(local_path)},
        )

    def download_file(self, r2_key: str, local_path: str) -> None:
        """Download an R2 object to a local path."""
        self._client.download_file(self.bucket, r2_key, local_path)

    def get_presigned_url(
        self,
        r2_key: str,
        expires_in: int = 3600,
        response_content_type: str | None = None,
        inline: bool = False,
    ) -> str:
        """
        Return a signed GET URL that lets anyone download the object
        without needing credentials. Default expiry: 1 hour.

        `response_content_type` forces the Content-Type R2 returns for this
        request (overriding whatever the object was stored with). This makes
        <audio>/<video> streaming work even for objects uploaded before we
        started stamping Content-Type at upload time.

        `inline` sets Content-Disposition: inline so the browser plays the
        media in-page instead of triggering a download.
        """
        params: dict[str, object] = {"Bucket": self.bucket, "Key": r2_key}
        if response_content_type:
            params["ResponseContentType"] = response_content_type
        if inline:
            params["ResponseContentDisposition"] = "inline"
        return self._client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=expires_in,
        )

    def file_exists(self, r2_key: str) -> bool:
        """True if the object exists in the bucket."""
        try:
            self._client.head_object(Bucket=self.bucket, Key=r2_key)
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    # ---- health check ----------------------------------------------------

    def ping(self) -> bool:
        """
        Verify R2 is reachable and credentials work by performing a
        head_bucket against the configured bucket. Used by /health.
        """
        try:
            self._client.head_bucket(Bucket=self.bucket)
            return True
        except Exception:
            return False


@lru_cache(maxsize=1)
def get_storage() -> R2Storage:
    """Cached singleton accessor — boto3 clients are thread-safe."""
    return R2Storage()
