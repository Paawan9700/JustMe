"""
Shared constants used by both the API (backend) and the Worker.

Anything that needs to stay in sync between the two services (job status
values, R2 object key naming) lives here so there is exactly one source
of truth.
"""

from enum import Enum


class JobStatus(str, Enum):
    """
    Lifecycle of a single JustMe job.

    Order of progression (happy path):
        QUEUED
          -> DOWNLOADING
          -> EXTRACTING_AUDIO
          -> DIARIZING
          -> GENERATING_SNIPPETS
          -> AWAITING_SELECTION      (waits for user to pick their voice)
          -> RENDERING
          -> DONE

    FAILED is terminal from any state.
    """

    QUEUED = "QUEUED"
    DOWNLOADING = "DOWNLOADING"
    EXTRACTING_AUDIO = "EXTRACTING_AUDIO"
    DIARIZING = "DIARIZING"
    GENERATING_SNIPPETS = "GENERATING_SNIPPETS"
    AWAITING_SELECTION = "AWAITING_SELECTION"
    RENDERING = "RENDERING"
    DONE = "DONE"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------
# Linear pipeline. ANY -> FAILED is always legal and is appended below.

_LINEAR_TRANSITIONS: dict[str, set[str]] = {
    JobStatus.QUEUED.value: {JobStatus.DOWNLOADING.value},
    JobStatus.DOWNLOADING.value: {JobStatus.EXTRACTING_AUDIO.value},
    JobStatus.EXTRACTING_AUDIO.value: {JobStatus.DIARIZING.value},
    JobStatus.DIARIZING.value: {JobStatus.GENERATING_SNIPPETS.value},
    JobStatus.GENERATING_SNIPPETS.value: {JobStatus.AWAITING_SELECTION.value},
    JobStatus.AWAITING_SELECTION.value: {JobStatus.RENDERING.value},
    JobStatus.RENDERING.value: {JobStatus.DONE.value},
    JobStatus.DONE.value: set(),
    JobStatus.FAILED.value: set(),
}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    src: (targets | {JobStatus.FAILED.value}) if src != JobStatus.FAILED.value else set()
    for src, targets in _LINEAR_TRANSITIONS.items()
}


def is_legal_transition(current: str, new: str) -> bool:
    """
    Same-status updates (progress-only) are always allowed. Otherwise
    consult ALLOWED_TRANSITIONS. ANY -> FAILED is always legal.
    """
    if current == new:
        return True
    return new in ALLOWED_TRANSITIONS.get(current, set())


# ---------------------------------------------------------------------------
# R2 object key naming
# ---------------------------------------------------------------------------
# All worker/API code MUST go through these helpers when reading/writing R2,
# so the layout stays consistent and is easy to debug from the R2 dashboard.
#
# Layout:
#   jobs/{job_id}/source.mp4
#   jobs/{job_id}/audio.wav
#   jobs/{job_id}/snippets/{speaker_label}.mp3
#   jobs/{job_id}/final.mp4
#   jobs/{job_id}/transcript.json     (structured, all segments — Phase-2 source)
#   jobs/{job_id}/transcription.txt   (plain-text transcript of the final video)
#   jobs/{job_id}/recommendations.csv (LLM-extracted stock recommendations)
# ---------------------------------------------------------------------------

def r2_key_source_video(job_id: str) -> str:
    """Original video downloaded from YouTube."""
    return f"jobs/{job_id}/source.mp4"


def r2_key_audio(job_id: str) -> str:
    """Extracted audio track used for diarization."""
    return f"jobs/{job_id}/audio.wav"


def r2_key_snippet(job_id: str, speaker_label: str) -> str:
    """
    Short identification clip for one diarized speaker.

    `speaker_label` is whatever pyannote returns, e.g. "SPEAKER_00".
    """
    return f"jobs/{job_id}/snippets/{speaker_label}.mp3"


def r2_key_final_video(job_id: str) -> str:
    """Final stitched video of only the selected speaker."""
    return f"jobs/{job_id}/final.mp4"


def r2_key_transcript(job_id: str) -> str:
    """
    Structured transcript of the whole source: all WhisperX segments
    (including no-speaker ones) with {start, end, speaker, text}.

    Written at diarize time as the complete, lossless record of what was
    said. Source of truth for the render-time plain-text transcript and the
    fuel for Phase-2 (LLM insights).
    """
    return f"jobs/{job_id}/transcript.json"


def r2_key_transcription(job_id: str) -> str:
    """User-facing plain-text transcript of the final rendered video."""
    return f"jobs/{job_id}/transcription.txt"


def r2_key_recommendations(job_id: str) -> str:
    """User-facing CSV of stock recommendations extracted from the transcript."""
    return f"jobs/{job_id}/recommendations.csv"


# ---------------------------------------------------------------------------
# User-facing error messages shared between API and Worker
# ---------------------------------------------------------------------------

# Returned by the M3 worker (and any future code path) when yt-dlp's
# metadata reports is_live=True for the supplied URL. URL-pattern-based
# livestream detection is NOT used — see worker/tasks/dummy.py for the
# rationale.
LIVE_STREAM_REJECT_MESSAGE = (
    "This video is currently live. "
    "Please wait until the stream ends and try again."
)
