"""
Real speaker diarization (WhisperX + pyannote.audio).

IMPORTANT SETUP: Before this works, you must:
1. Create a free account at huggingface.co
2. Accept the license on BOTH of these gated models (the diarization
   pipeline loads segmentation as a dependency, so one isn't enough):
     - https://huggingface.co/pyannote/speaker-diarization-3.1
     - https://huggingface.co/pyannote/segmentation-3.0
   Click "Agree and access repository" on each, signed in to the same
   HF account that owns HF_TOKEN.
3. Go to https://huggingface.co/settings/tokens and create a token with
   read access.
4. Set HF_TOKEN environment variable to that token.

Runs on the GPU worker only. The heavy imports (whisperx, torch) are
done lazily inside `run_diarization()` so the module is safely importable
on machines without those dependencies (e.g. the Emergent dev container).
Missing deps raise `DiarizationError("MISSING_DEPS", ...)`, which the
orchestrator can choose to handle (dev) or propagate as a job failure
(production).

Public entry point: `run_diarization(job_id, audio_r2_key, job_dir,
duration_sec)`. Pure post-processing logic is in
`post_process_segments(...)` (deterministic, unit-testable, no I/O).
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from worker.db import get_db
from worker.state import progress, transition
from worker.utils.storage import download_file, upload_file
from shared.constants import JobStatus, r2_key_transcript

logger = logging.getLogger(__name__)


class DiarizationError(Exception):
    """User-facing diarization failure with an error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# Post-processing tunables — exported as constants so they show up in
# tests and dashboards. Values come from the M4 spec.
MERGE_GAP_SEC = 1.5    # merge adjacent same-speaker segments closer than this
MIN_SEGMENT_SEC = 0.5  # drop segments shorter than this
PAD_SEC = 0.5          # pad each segment start/end by this many seconds
                       # (render.py adds further padding at cut time)

# ---------------------------------------------------------------------------
# WhisperX transcription config (transcript ACCURACY tuning)
# ---------------------------------------------------------------------------
# large-v3 recognises proper nouns / multilingual (Hinglish) speech notably
# better than large-v2, at the same model size (no extra VRAM).
WHISPER_MODEL = "large-v3"

# WhisperX runs an internal VAD (voice-activity detector) that gates the audio
# BEFORE Whisper transcribes it — audio the VAD misses is never transcribed and
# so can never reach transcript.json / transcription.txt / the LLM. The library
# defaults (vad_onset=0.500, vad_offset=0.363) are precision-first and were
# dropping clearly-audible speech (measured -25..-30 dB) whenever the speaker
# paused briefly mid-sentence — e.g. an analyst's stop-loss/targets/reasoning.
# We tune recall-first (project rule: "never fewer words; more is acceptable"):
#   vad_onset  lower  -> trigger "speech" more readily (quieter onsets kept)
#   vad_offset lower  -> hold the speech region open through brief 0.4-0.7 s
#                        dips instead of ending it and discarding trailing audio
# chunk_size stays at the library default. Passed as a partial dict — WhisperX
# merges it over its defaults, so only these keys change.
VAD_OPTIONS = {"vad_onset": 0.30, "vad_offset": 0.20, "chunk_size": 30}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_diarization(
    job_id: str,
    audio_r2_key: str,
    job_dir: Path,
    duration_sec: float | None,
) -> list[dict[str, Any]]:
    """
    Full diarization pipeline. Updates job + segments collections.
    Returns the per-speaker stats list that was written to job.speakers.

    Raises DiarizationError on any failure with a user-facing message.
    """
    transition(
        job_id, JobStatus.DIARIZING.value,
        stage="diarizing", percent=0.0,
        message="Loading AI models (first run may take a few minutes)...",
    )

    # ---- 1. Lazy imports + env checks -----------------------------------
    try:
        import torch  # type: ignore
        import whisperx  # type: ignore
    except ImportError as exc:
        raise DiarizationError(
            "MISSING_DEPS",
            "WhisperX/torch not installed. This step runs only on the "
            "GPU worker (see worker/Dockerfile).",
        ) from exc

    hf_token = os.environ.get("HF_TOKEN") or None
    if not hf_token:
        raise DiarizationError(
            "MISSING_HF_TOKEN",
            "HF_TOKEN environment variable is required for diarization. "
            "See setup instructions at the top of diarize.py.",
        )

    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"
    logger.info("diarize[%s] device=%s compute_type=%s", job_id, device, compute_type)

    # ---- 2. Pull audio.wav from R2 --------------------------------------
    local_audio = job_dir / "audio.wav"
    if not local_audio.exists():
        progress(job_id, percent=5.0, message="Downloading audio from storage...")
        try:
            download_file(audio_r2_key, str(local_audio))
        except Exception as exc:  # noqa: BLE001
            raise DiarizationError(
                "AUDIO_DOWNLOAD_FAILED",
                f"Could not download audio from storage: {exc}",
            ) from exc

    # ---- 3. WhisperX transcribe -----------------------------------------
    progress(job_id, percent=10.0, message=f"Loading Whisper {WHISPER_MODEL}...")
    audio = whisperx.load_audio(str(local_audio))

    try:
        model = whisperx.load_model(
            WHISPER_MODEL, device, compute_type=compute_type,
            vad_method="pyannote", vad_options=dict(VAD_OPTIONS),
        )
        result = model.transcribe(audio, batch_size=16)
    except Exception as exc:  # noqa: BLE001
        raise DiarizationError(
            "TRANSCRIBE_FAILED",
            f"Whisper transcription failed: {exc}",
        ) from exc

    progress(job_id, percent=35.0, message="Transcription complete")
    _free(model, torch=torch, device=device)

    # ---- 4. Forced alignment --------------------------------------------
    progress(job_id, percent=40.0, message="Aligning word timestamps...")
    try:
        model_a, metadata = whisperx.load_align_model(
            language_code=result["language"], device=device,
        )
        result = whisperx.align(
            result["segments"], model_a, metadata, audio, device,
            return_char_alignments=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise DiarizationError(
            "ALIGN_FAILED",
            f"Word alignment failed: {exc}",
        ) from exc

    progress(job_id, percent=55.0, message="Alignment complete")
    _free(model_a, torch=torch, device=device)

    # ---- 5. Speaker diarization (pyannote via whisperx wrapper) ---------
    progress(job_id, percent=60.0, message="Identifying speakers...")
    try:
        DiarizationPipeline = (
            whisperx.DiarizationPipeline
            if hasattr(whisperx, "DiarizationPipeline")
            else whisperx.diarize.DiarizationPipeline
        )
        try:
            diarize_model = DiarizationPipeline(use_auth_token=hf_token, device=device)
        except TypeError:
            diarize_model = DiarizationPipeline(token=hf_token, device=device)
        diarize_segments = diarize_model(audio)
        assign_fn = (
            whisperx.assign_word_speakers
            if hasattr(whisperx, "assign_word_speakers")
            else whisperx.diarize.assign_word_speakers
        )
        result = assign_fn(diarize_segments, result)
        _free(diarize_model, torch=torch, device=device)
        del diarize_segments
    except Exception as exc:  # noqa: BLE001
        # Common case: HF token doesn't have model access yet.
        msg = str(exc).lower()
        if "401" in msg or "unauthorized" in msg or "gated" in msg or "access" in msg:
            raise DiarizationError(
                "HF_ACCESS_DENIED",
                "HuggingFace access denied. Accept the license on BOTH "
                "models on the same account as HF_TOKEN: "
                "https://huggingface.co/pyannote/speaker-diarization-3.1 "
                "and https://huggingface.co/pyannote/segmentation-3.0 "
                "(the diarization pipeline depends on the segmentation model).",
            ) from exc
        raise DiarizationError(
            "DIARIZE_FAILED",
            f"Speaker diarization failed: {exc}",
        ) from exc

    progress(job_id, percent=70.0, message="Speaker assignment complete")

    # ---- 6. Extract raw segments ----------------------------------------
    raw = _extract_raw_segments(result)
    logger.info("diarize[%s] raw segments: %d", job_id, len(raw))

    # ---- 7. Post-process ------------------------------------------------
    progress(job_id, percent=80.0, message="Post-processing segments...")
    processed = post_process_segments(raw, duration_sec)
    logger.info("diarize[%s] processed segments: %d", job_id, len(processed))

    # ---- 8. Persist to MongoDB ------------------------------------------
    db = get_db()
    db.segments.delete_many({"job_id": job_id})  # idempotent on retry
    if processed:
        db.segments.insert_many([{"job_id": job_id, **s} for s in processed])

    # ---- 9. Per-speaker stats -> job.speakers ---------------------------
    speakers_doc = _compute_speakers_doc(processed)
    db.jobs.update_one(
        {"job_id": job_id},
        {"$set": {"speakers": speakers_doc}},
    )

    # ---- 9b. Persist the FULL transcript for download + Phase-2 ---------
    # Best-effort: WhisperX already produced this text while transcribing for
    # diarization, so we just stop discarding it. A failure here must never
    # fail the job — the video pipeline does not depend on it.
    try:
        transcript = build_transcript(result)
        transcript_path = job_dir / "transcript.json"
        transcript_path.write_text(
            json.dumps(transcript, ensure_ascii=False), encoding="utf-8",
        )
        transcript_key = r2_key_transcript(job_id)
        upload_file(str(transcript_path), transcript_key)
        db.jobs.update_one(
            {"job_id": job_id},
            {"$set": {"artifacts.transcript_key": transcript_key}},
        )
        logger.info(
            "diarize[%s] transcript saved: %d segments -> %s",
            job_id, len(transcript), transcript_key,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "diarize[%s] transcript persist failed (%s); continuing — the "
            "video is unaffected, the transcript just won't be available",
            job_id, exc,
        )

    progress(
        job_id, percent=100.0,
        message=(
            f"Found {len(speakers_doc)} speaker"
            f"{'s' if len(speakers_doc) != 1 else ''}"
        ),
    )
    total_speaking = sum(
        (s["end"] - s["start"]) for s in processed
    ) if processed else 0.0
    logger.info(
        "diarize[%s] complete: %d speakers, %d segments, %.1fs total speaking",
        job_id, len(speakers_doc), len(processed), total_speaking,
    )

    # Local audio is no longer needed (R2 still has it for M5 snippet gen)
    try:
        local_audio.unlink()
    except OSError:
        pass

    # Full GPU memory purge so the next job starts with a clean slate.
    # Without this, CUDA memory from this job's models stays allocated
    # until Python's GC runs, which can OOM the second job.
    import gc
    gc.collect()
    if device == "cuda":
        try:
            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass

    return speakers_doc


# ---------------------------------------------------------------------------
# Pure helpers (testable without GPU)
# ---------------------------------------------------------------------------

def build_transcript(whisperx_result: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Build the COMPLETE transcript from whisperx's segment list as
    {start, end, speaker, text} dicts, sorted by start.

    Unlike `_extract_raw_segments` (which feeds the recall-tuned render
    pipeline and therefore drops no-speaker segments), this keeps EVERY
    segment that has real text — including ones pyannote never assigned a
    speaker to. Those no-speaker spans are exactly what `bridge_clear_gaps`
    pulls into the final video, so they must be present here for the
    downstream transcript to satisfy the "never fewer words" guarantee.

    `speaker` may be None when whisperx left the segment unassigned.
    """
    out: list[dict[str, Any]] = []
    for seg in whisperx_result.get("segments", []) or []:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(seg["start"])
            end = float(seg["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        out.append(
            {
                "start": start,
                "end": end,
                "speaker": seg.get("speaker"),  # may be None
                "text": text,
            }
        )
    out.sort(key=lambda s: s["start"])
    return out


def _extract_raw_segments(whisperx_result: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull (speaker, start, end) tuples out of whisperx's segment list."""
    raw: list[dict[str, Any]] = []
    for seg in whisperx_result.get("segments", []) or []:
        speaker = seg.get("speaker")
        if not speaker:
            continue
        try:
            start = float(seg["start"])
            end = float(seg["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        raw.append({"speaker": speaker, "start": start, "end": end})
    return raw


def post_process_segments(
    raw: list[dict[str, Any]],
    video_duration_sec: float | None = None,
) -> list[dict[str, Any]]:
    """
    Apply the M4 spec post-processing rules. Pure function, no I/O.

    Order matters (per spec):
      a) MERGE same-speaker segments separated by < MERGE_GAP_SEC
      b) DROP segments shorter than MIN_SEGMENT_SEC
      c) PAD start/end by PAD_SEC, clamped to [0, video_duration_sec]
      d) SORT all segments by start time
    """
    if not raw:
        return []

    # Group by speaker, sort within each group by start.
    by_speaker: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for s in raw:
        by_speaker[s["speaker"]].append((float(s["start"]), float(s["end"])))
    for sp in by_speaker:
        by_speaker[sp].sort(key=lambda x: x[0])

    # a) MERGE within each speaker
    merged: list[dict[str, Any]] = []
    for sp, segs in by_speaker.items():
        cur_start, cur_end = segs[0]
        for start, end in segs[1:]:
            gap = start - cur_end
            if gap < MERGE_GAP_SEC:
                cur_end = max(cur_end, end)
            else:
                merged.append({"speaker": sp, "start": cur_start, "end": cur_end})
                cur_start, cur_end = start, end
        merged.append({"speaker": sp, "start": cur_start, "end": cur_end})

    # b) DROP shorts (post-merge — pre-merge tiny fragments may legitimately
    # be part of a longer span once merged).
    merged = [s for s in merged if (s["end"] - s["start"]) >= MIN_SEGMENT_SEC]

    # c) PAD, clamped to [0, duration]
    upper = float(video_duration_sec) if video_duration_sec else float("inf")
    for s in merged:
        s["start"] = max(0.0, s["start"] - PAD_SEC)
        s["end"] = min(upper, s["end"] + PAD_SEC)

    # d) SORT
    merged.sort(key=lambda s: s["start"])
    return merged


def _compute_speakers_doc(processed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate processed segments into per-speaker stats for job.speakers."""
    agg: dict[str, dict[str, float]] = defaultdict(lambda: {"total": 0.0, "count": 0})
    for s in processed:
        agg[s["speaker"]]["total"] += (s["end"] - s["start"])
        agg[s["speaker"]]["count"] += 1
    return sorted(
        [
            {
                "label": label,
                "total_speaking_sec": round(v["total"], 2),
                "segment_count": int(v["count"]),
                "snippet_key": None,  # filled in by M5
            }
            for label, v in agg.items()
        ],
        key=lambda x: x["label"],
    )


def _free(obj: Any, *, torch: Any, device: str) -> None:
    """Best-effort model + CUDA cache release between pipeline stages."""
    try:
        del obj
    except Exception:  # noqa: BLE001
        pass
    if device == "cuda":
        try:
            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
