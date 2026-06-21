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


def detect_silence(
    file_path: str,
    noise_db: float = -30.0,
    min_silence_sec: float = 0.5,
    duration_sec: float | None = None,
    timeout: float | None = None,
) -> list[tuple[float, float]]:
    """
    Detect silent intervals in `file_path`'s audio via ffmpeg's
    `silencedetect` filter. Returns a list of (start, end) tuples in
    seconds, sorted by start.

    Energy-based: distinguishes speech/sound from silence, NOT speech from
    music. `noise_db` is the threshold below which audio counts as silent;
    `min_silence_sec` is the shortest gap reported.

    Decodes audio only (`-vn`) over the whole file in one pass. silencedetect
    logs to stderr at the `info` level, so we run ffmpeg directly here rather
    than through run_ffmpeg() (which forces `-loglevel error`).
    """
    ffmpeg = _require_bin("ffmpeg")
    cmd = [
        ffmpeg, "-hide_banner", "-nostats", "-loglevel", "info",
        "-i", file_path,
        "-vn",
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence_sec}",
        "-f", "null", "-",
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False,
    )
    if proc.returncode != 0:
        raise FFmpegError(
            f"ffmpeg silencedetect failed (exit {proc.returncode}): "
            f"{(proc.stderr or '').strip()[:500]}"
        )

    # Parse "silence_start: X" / "silence_end: Y" lines from stderr.
    intervals: list[tuple[float, float]] = []
    cur_start: float | None = None
    for line in (proc.stderr or "").splitlines():
        if "silence_start:" in line:
            try:
                cur_start = float(line.split("silence_start:")[1].strip().split()[0])
            except (IndexError, ValueError):
                cur_start = None
        elif "silence_end:" in line and cur_start is not None:
            try:
                end = float(line.split("silence_end:")[1].strip().split()[0])
                intervals.append((cur_start, end))
            except (IndexError, ValueError):
                pass
            cur_start = None
    # A trailing silence reports only silence_start; close it at duration.
    if cur_start is not None and duration_sec:
        intervals.append((cur_start, float(duration_sec)))

    intervals.sort()
    return intervals


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
