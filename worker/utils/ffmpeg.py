"""
Subprocess wrappers around ffmpeg / ffprobe.

We use raw subprocess (per project rule: never MoviePy). ffmpeg-python
is in the requirements only for ffprobe-style helpers; encoding always
goes through ffmpeg's CLI directly so we get exact control over flags.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Sequence


class FFmpegError(RuntimeError):
    """Raised on non-zero exit or missing binary."""


def _require_bin(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise FFmpegError(f"{name} binary not found in PATH")
    return path


def run_ffmpeg(args: Sequence[str], timeout: float | None = None) -> tuple[str, str]:
    """
    Run `ffmpeg` with the given args (excluding the program name itself).

    Returns (stdout, stderr) on success. Always passes -hide_banner and
    -loglevel error so the output stays clean. Caller is responsible for
    putting -y at the right position if it wants to overwrite outputs.

    Raises FFmpegError if exit code is non-zero or the binary is missing.
    """
    ffmpeg = _require_bin("ffmpeg")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", *args]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False,
    )
    if proc.returncode != 0:
        msg = (proc.stderr or "").strip() or (proc.stdout or "").strip()
        raise FFmpegError(
            f"ffmpeg failed (exit {proc.returncode}): {msg[:500]}"
        )
    return proc.stdout, proc.stderr


def get_video_duration(file_path: str) -> float:
    """
    Return the video duration in seconds (float) via ffprobe.

    Raises FFmpegError on probe failure.
    """
    ffprobe = _require_bin("ffprobe")
    cmd = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        file_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise FFmpegError(
            f"ffprobe failed (exit {proc.returncode}): {proc.stderr.strip()[:500]}"
        )
    try:
        data = json.loads(proc.stdout)
        return float(data["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise FFmpegError(f"ffprobe returned unparseable output: {exc}") from exc
