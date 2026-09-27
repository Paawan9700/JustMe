"""
Per-speaker voice prints for the "Favourite voices" feature.

WHY THIS EXISTS
---------------
Diarization labels (SPEAKER_07, ...) are assigned fresh for every video, so
"the analyst I want" cannot be remembered as a label. It is remembered as a
voice print instead: a speaker-embedding vector that describes how someone
sounds. At the end of the snippet stage we compute one print per speaker and
store it in the `voiceprints` collection; the API compares those prints with
the user's saved favourites to fill the "Your favourites in this video" box
on the selection page (shared/voice_match.py holds the math).

RECIPE (bump RECIPE_VERSION whenever any of this changes — prints from
different recipes are never compared)
------
  1. Speech spans come from the RAW pyannote turns (diarization.json), not
     from `segments`: segments are WhisperX sentences merged across gaps
     < 1.5 s and padded by 0.5 s, so they carry other people's interjections.
     Regions overlapping any other speaker's turn are removed and every span
     is trimmed by EDGE_TRIM_SEC per edge. Without diarization.json we fall
     back to `segments` shrunk by FALLBACK_SHRINK_SEC per edge.
  2. 4 s windows (2 s hop, 2 s minimum), at most MAX_WINDOWS spread evenly
     over the whole video.
  3. Embed with the same model reclaim.py uses, drop NaN rows, L2-normalise
     each window, robust centroid (drop the 20% least typical windows),
     normalise again.
  4. No print for speakers with < MIN_SPEECH_SEC of clean speech or fewer
     than MIN_VECTORS usable windows — too little audio to trust.

Best-effort by contract: the caller (snippets.py) treats any exception as
"no prints for this job". The job itself never fails because of this module;
the selection page simply shows no favourites box.

The pure planning/aggregation helpers take an injected `embed_fn`, so they
are unit-testable without torch (see worker/tests/test_voiceprints.py).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from worker.tasks.reclaim import (
    EMBEDDING_MODEL,
    SAMPLE_RATE,
    evenly_sample,
    merge_ranges,
    plan_windows,
    robust_centroid,
    subtract_ranges,
)
from worker.utils.ffmpeg import run_ffmpeg
from shared import voice_match

logger = logging.getLogger(__name__)

RECIPE_VERSION = 1

WINDOW_SEC = 4.0
HOP_SEC = 2.0
MIN_WINDOW_SEC = 2.0
MAX_WINDOWS = 64          # embedded per speaker
KEEP_WINDOWS = 16         # stored per speaker (for re-tuning without re-embedding)
EDGE_TRIM_SEC = 0.25      # raw pyannote turns: trim per edge
FALLBACK_SHRINK_SEC = 0.5 # segments fallback: shrink per edge (undoes PAD_SEC)
MIN_SPEECH_SEC = 15.0
MIN_VECTORS = 3
TRIM_FRAC = 0.2

EmbedFn = Callable[[list[tuple[float, float]]], list[list[float] | None]]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _shrink(spans: list[tuple[float, float]], by: float) -> list[tuple[float, float]]:
    out = []
    for s, e in spans:
        s2, e2 = s + by, e - by
        if e2 - s2 >= MIN_WINDOW_SEC:
            out.append((s2, e2))
    return out


def clean_spans_from_turns(
    turns: list[dict[str, Any]],
    label: str,
    edge_trim: float = EDGE_TRIM_SEC,
) -> list[tuple[float, float]]:
    """
    `label`'s speech with every overlap against another speaker removed,
    trimmed by `edge_trim` per edge, dropping pieces shorter than a window.
    """
    mine = merge_ranges(
        [(float(t["start"]), float(t["end"])) for t in turns if t.get("speaker") == label],
        join_gap=0.0,
    )
    if not mine:
        return []
    others = [
        {"start": float(t["start"]), "end": float(t["end"])}
        for t in turns if t.get("speaker") != label
    ]
    pieces = subtract_ranges([{"start": s, "end": e} for s, e in mine], others)
    return _shrink([(p["start"], p["end"]) for p in pieces], edge_trim)


def clean_spans_from_segments(
    segments: list[dict[str, Any]],
    label: str,
    shrink: float = FALLBACK_SHRINK_SEC,
) -> list[tuple[float, float]]:
    """Fallback when diarization.json is unavailable."""
    mine = sorted(
        (float(s["start"]), float(s["end"]))
        for s in segments if s.get("speaker") == label
    )
    return _shrink(mine, shrink)


def plan_print_windows(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    windows = [
        w
        for s, e in spans
        for w in plan_windows(s, e, window_sec=WINDOW_SEC, hop_sec=HOP_SEC,
                              min_sec=MIN_WINDOW_SEC)
    ]
    return evenly_sample(windows, MAX_WINDOWS)


def build_print(vectors: list[list[float] | None]) -> dict[str, Any] | None:
    """Unit centroid + a few kept unit windows, or None if too few vectors."""
    unit = [u for u in (voice_match.normalize(v) for v in vectors if v) if u]
    if len(unit) < MIN_VECTORS:
        return None
    centroid = voice_match.normalize(robust_centroid(unit, TRIM_FRAC) or [])
    if centroid is None:
        return None
    return {
        "centroid": centroid,
        "windows": evenly_sample(unit, KEEP_WINDOWS),
        "n_windows": len(unit),
    }


def compute_prints(
    spans_by_label: dict[str, list[tuple[float, float]]],
    embed_fn: EmbedFn,
) -> dict[str, dict[str, Any]]:
    """{label: {centroid, windows, n_windows, speech_sec}} for usable speakers."""
    out: dict[str, dict[str, Any]] = {}
    for label, spans in spans_by_label.items():
        speech = sum(e - s for s, e in spans)
        if speech < MIN_SPEECH_SEC:
            continue
        windows = plan_print_windows(spans)
        if len(windows) < MIN_VECTORS:
            continue
        p = build_print(embed_fn(windows))
        if p is None:
            continue
        p["speech_sec"] = round(speech, 2)
        out[label] = p
    return out


def spans_for_labels(
    labels: list[str],
    turns: list[dict[str, Any]] | None,
    segments: list[dict[str, Any]],
) -> tuple[dict[str, list[tuple[float, float]]], str]:
    """Clean spans per label, preferring raw turns; returns (spans, source)."""
    if turns:
        return {lb: clean_spans_from_turns(turns, lb) for lb in labels}, "turns"
    return {lb: clean_spans_from_segments(segments, lb) for lb in labels}, "segments"


# ---------------------------------------------------------------------------
# Model / I/O layer (worker only — heavy imports are lazy)
# ---------------------------------------------------------------------------

def extract_wav(source_path: Path, wav_path: Path) -> None:
    """16 kHz mono s16le — the exact format worker/tasks/audio.py produces."""
    run_ffmpeg([
        "-i", str(source_path),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", str(SAMPLE_RATE),
        "-ac", "1",
        str(wav_path),
        "-y",
    ])


def pyannote_version() -> str | None:
    try:
        import pyannote.audio  # type: ignore

        return str(getattr(pyannote.audio, "__version__", None))
    except Exception:  # noqa: BLE001
        return None


def compute_prints_for_wav(
    wav_path: Path,
    spans_by_label: dict[str, list[tuple[float, float]]],
) -> dict[str, dict[str, Any]]:
    """Load the embedder once, embed every speaker's windows, build prints."""
    import torch  # lazy — GPU worker only

    from worker.tasks.reclaim import _WavWindowReader, _load_embedder, _make_embed_fn

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    reader = None
    embedder = None
    try:
        embedder = _load_embedder(device, os.environ.get("HF_TOKEN") or None)
        reader = _WavWindowReader(wav_path)
        embed_fn = _make_embed_fn(reader, embedder, torch, device)
        return compute_prints(spans_by_label, embed_fn)
    finally:
        if reader is not None:
            reader.close()
        del embedder
        if device.type == "cuda":
            try:
                torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass


def _load_turns(job_dir: Path, diarization_key: str | None) -> list[dict[str, Any]] | None:
    local = job_dir / "diarization.json"
    if not local.exists() and diarization_key:
        from worker.utils.storage import download_file

        try:
            download_file(diarization_key, str(local))
        except Exception:  # noqa: BLE001
            logger.warning("voiceprints: could not fetch %s", diarization_key)
            return None
    if not local.exists():
        return None
    try:
        turns = json.loads(local.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return turns if isinstance(turns, list) and turns else None


def compute_for_job(job_id: str, job_dir: Path, local_source: Path) -> int:
    """
    Compute and store a print for every speaker of `job_id`. Returns the
    number of prints written. Raises on failure — the caller logs and
    carries on without prints.
    """
    from worker.db import get_db

    t0 = time.perf_counter()
    db = get_db()
    job = db.jobs.find_one(
        {"job_id": job_id}, {"speakers": 1, "artifacts": 1, "_id": 0},
    ) or {}
    labels = [s["label"] for s in (job.get("speakers") or []) if s.get("label")]
    if not labels:
        return 0

    turns = _load_turns(job_dir, (job.get("artifacts") or {}).get("diarization_key"))
    segments = []
    if not turns:
        segments = list(db.segments.find(
            {"job_id": job_id}, {"speaker": 1, "start": 1, "end": 1, "_id": 0},
        ))
    spans_by_label, source = spans_for_labels(labels, turns, segments)

    # diarize.py leaves audio.wav in job_dir for us; a resumed run in a fresh
    # container won't have it, so re-extract from source.mp4 in that case.
    wav = job_dir / "audio.wav"
    temp_wav = None
    if not wav.exists():
        temp_wav = job_dir / "voiceprint_audio.wav"
        extract_wav(local_source, temp_wav)
        wav = temp_wav
    try:
        prints = compute_prints_for_wav(wav, spans_by_label)
    finally:
        if temp_wav is not None:
            try:
                temp_wav.unlink()
            except OSError:
                pass

    now = datetime.now(timezone.utc)
    meta = {
        "model": EMBEDDING_MODEL,
        "recipe_version": RECIPE_VERSION,
        "pyannote_version": pyannote_version(),
        "dim": voice_match.DIM,
        "span_source": source,
    }
    docs = [
        {
            "job_id": job_id,
            "label": label,
            "centroid": voice_match.pack(p["centroid"]),
            "windows": voice_match.pack_many(p["windows"]),
            "n_windows": p["n_windows"],
            "speech_sec": p["speech_sec"],
            **meta,
            "created_at": now,
        }
        for label, p in prints.items()
    ]
    # Replace, never merge: a retry that re-ran diarization has new labels.
    db.voiceprints.delete_many({"job_id": job_id})
    if docs:
        db.voiceprints.insert_many(docs)
    logger.info(
        "voiceprints[%s] %d/%d speakers printed from %s in %.1fs",
        job_id, len(docs), len(labels), source, time.perf_counter() - t0,
    )
    return len(docs)

