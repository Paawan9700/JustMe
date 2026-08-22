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
# Layout — TWO top-level prefixes, on purpose:
#
#   ephemeral/  processing intermediates. Deleted eagerly in code as soon as the
#               stage that needs them is done, with a SHORT R2 lifecycle rule on
#               the prefix as a backstop for crashed/abandoned jobs.
#   jobs/       user-facing deliverables. NO lifecycle rule — kept indefinitely.
#
# Why split them: R2 lifecycle rules filter by PREFIX only, and the job id sits in
# the middle of the key, so no rule can express "delete every source.mp4 but keep
# every final.mp4". A single rule on jobs/ deleted the deliverables too — which is
# exactly what wiped every finished job on 2026-08-22. source.mp4 + audio.wav are
# 98% of the bytes (485 MB + 351 MB vs 15 MB for final.mp4), so splitting them out
# takes a job from ~865 MB to ~24 MB and keeps the whole thing inside R2's free tier.
#
#   ephemeral/{job_id}/source.mp4                  (deleted after render)
#   ephemeral/{job_id}/audio.wav                   (deleted after diarization)
#   ephemeral/{job_id}/snippets/{speaker_label}.mp3 (deleted after render)
#
#   jobs/{job_id}/final.mp4
#   jobs/{job_id}/final_audio.m4a    (audio-only copy of final.mp4, for the LLM)
#   jobs/{job_id}/transcript.json     (structured, all segments — Phase-2 source)
#   jobs/{job_id}/diarization.json    (raw pyannote turns — debug/attribution)
#   jobs/{job_id}/transcription.txt   (plain-text transcript of the final video)
#   jobs/{job_id}/recommendations.csv (LLM-extracted stock recommendations)
# ---------------------------------------------------------------------------

def r2_key_source_video(job_id: str) -> str:
    """
    Original video downloaded from YouTube. EPHEMERAL.

    Read by the audio, snippets and render stages; dead once render succeeds
    (there is no re-render path — `select_speaker` only accepts a job in
    AWAITING_SELECTION), so render deletes it.
    """
    return f"ephemeral/{job_id}/source.mp4"


def r2_key_audio(job_id: str) -> str:
    """
    Extracted audio track used for diarization. EPHEMERAL.

    Diarization is its only reader (snippets are cut from source.mp4), so this is
    dead as soon as process_video finishes and gets deleted there.
    """
    return f"ephemeral/{job_id}/audio.wav"


def r2_key_snippet(job_id: str, speaker_label: str) -> str:
    """
    Short identification clip for one diarized speaker.

    `speaker_label` is whatever pyannote returns, e.g. "SPEAKER_00".

    EPHEMERAL: only needed for the pre-render speaker-selection UI, so render
    deletes the whole snippets/ prefix once the user's choice has been applied.
    """
    return f"ephemeral/{job_id}/snippets/{speaker_label}.mp3"


def r2_prefix_ephemeral(job_id: str) -> str:
    """
    The whole ephemeral tree for one job: source.mp4, audio.wav and snippets/.

    Render deletes this prefix wholesale once the final video is safely uploaded.
    Doing it as one prefix delete (rather than key-by-key) also sweeps up an
    audio.wav that process_video failed to remove, so a single cleanup path
    covers every intermediate.
    """
    return f"ephemeral/{job_id}/"


def r2_key_final_video(job_id: str) -> str:
    """Final stitched video of only the selected speaker."""
    return f"jobs/{job_id}/final.mp4"


def r2_key_final_audio(job_id: str) -> str:
    """
    Audio-only track of the final video, stream-copied out of final.mp4 at
    render time (no re-encode, so it is lossless and near-instant).

    Exists purely so the recommendations service can send Gemini AUDIO instead
    of VIDEO. Pass 1 only ever transcribes speech, so the video frames are dead
    weight: measured on an 8:53 clip, video = 48,907 input tokens / 170.7s vs
    audio = 13,730 tokens / 63.9s for an identical transcript. The smaller
    request is also far less likely to be shed with 503 UNAVAILABLE when
    Gemini capacity is tight.

    Written by the worker (which has ffmpeg) rather than the API service, which
    has none. Best-effort: if it is missing, the API falls back to final.mp4.
    """
    return f"jobs/{job_id}/final_audio.m4a"


def r2_key_transcript(job_id: str) -> str:
    """
    Structured transcript of the whole source: all WhisperX segments
    (including no-speaker ones) with {start, end, speaker, text}.

    Written at diarize time as the complete, lossless record of what was
    said. Source of truth for the render-time plain-text transcript and the
    fuel for Phase-2 (LLM insights).
    """
    return f"jobs/{job_id}/transcript.json"


def r2_key_diarization(job_id: str) -> str:
    """
    Raw pyannote speaker turns as produced by the diarization pipeline,
    BEFORE whisperx word-assignment and post-processing: a flat list of
    {speaker, start, end} dicts sorted by start.

    Written at diarize time purely for observability: when a speaker's
    words end up under the wrong label (attribution error), this is the
    artifact that shows whether clustering or assignment was at fault.
    The video pipeline never reads it.
    """
    return f"jobs/{job_id}/diarization.json"


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
