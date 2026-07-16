"""
Render-time speaker-attribution reclamation (recall-first).

WHY THIS EXISTS
---------------
pyannote diarization occasionally assigns a turn of the selected speaker to
ANOTHER speaker's cluster. Observed on job bc5ce57c: the analyst's Titan
stop-loss/targets turn (2990.8-3016.2s) was labeled as the anchor while the
analyst was speaking continuously — so the render, which trusts labels,
silently dropped his words. That violates the product's one hard guarantee:
the final video must contain ALL of the selected speaker's words (extra
words from others are acceptable; missing words are not).

No render-side timing heuristic can fix this class of error: a confidently
mislabeled 25-second turn is indistinguishable, in timing terms, from a real
turn by the other speaker. The fix has to re-check WHO IS ACTUALLY SPEAKING.

WHAT IT DOES
------------
At render time — when the selected speaker is known — re-verify attribution
with speaker embeddings (voice prints):

  1. Build a reference voice-print (robust centroid of window embeddings)
     for the selected speaker from their own diarized segments, and one for
     every other speaker from theirs.
  2. Slide windows across every OTHER speaker's segments and embed them.
  3. Reclaim a window when it matches the selected speaker's voice-print
     BETTER than every other speaker's (including, leave-one-out, the
     cluster it was assigned to — a stolen turn must not certify itself).
  4. Merge passing windows into time ranges and hand them to the render as
     extra selected-speaker segments. DB labels are untouched, so every
     other speaker's own video is unaffected (ranges may appear in several
     speakers' videos — that is explicitly acceptable).

Verification beats the original clustering here because it has strictly
more information: clustering had to partition 15+ unknown voices with no
reference; we have a known target voice with minutes of confirmed speech.

Additive-only by construction: a false positive adds a few extra words
(acceptable); a false negative is today's status quo. Reclamation can never
REMOVE anything. Every model/IO failure degrades to "no reclamation" — the
render then behaves exactly as it did before this module existed.

MODEL
-----
`pyannote/wespeaker-voxceleb-resnet34-LM` — the same embedding model the
pyannote speaker-diarization-3.1 pipeline uses internally, so it is already
in the worker's HF cache after any diarize run. Ungated (CC-BY-4.0): no new
HuggingFace license to accept, no new pip dependency (pyannote.audio is
already in worker/requirements.txt). Verified against pyannote.audio 4.0.7:
`PretrainedSpeakerEmbedding(model, device=...)` -> callable taking float32
waveforms of shape (batch, 1, samples) at 16 kHz and returning a numpy
(batch, 256) array (NaN rows for too-short inputs).

Heavy imports (torch, pyannote, numpy) happen lazily inside functions —
this module stays importable on dev boxes without them, and the pure logic
is unit-testable with an injected `embed_fn` (see tests/test_reclaim.py).
"""

from __future__ import annotations

import logging
import math
import os
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from worker.utils.ffmpeg import run_ffmpeg

logger = logging.getLogger(__name__)

# The exact embedding model speaker-diarization-3.1 uses internally.
EMBEDDING_MODEL = "pyannote/wespeaker-voxceleb-resnet34-LM"

# audio format contract — worker/tasks/audio.py produces 16 kHz mono s16le,
# and the embedding model expects 16 kHz input.
SAMPLE_RATE = 16000

# Windowing. 4 s is comfortably above the model's reliable minimum and short
# enough to localise a label flip; 2 s hop gives 2 s reclaim granularity.
RECLAIM_WINDOW_SEC = 4.0
RECLAIM_HOP_SEC = 2.0
RECLAIM_MIN_WINDOW_SEC = 1.5   # skip audio spans shorter than this

# Decision rule. A window is reclaimed when its similarity to the selected
# speaker (a) clears an absolute floor — so junk audio (music, crosstalk)
# with uniformly low similarities is never pulled in — and (b) beats the
# best non-selected voice-print by `margin`. Margin 0.0 = strictly better:
# recall-first, per the product rule "never fewer words; extra acceptable".
RECLAIM_MIN_SIMILARITY = 0.15
RECLAIM_MARGIN = 0.0

# Voice-print construction.
RECLAIM_MAX_REF_WINDOWS = 120  # cap per speaker (anchor has ~1h of speech)
RECLAIM_TRIM_FRAC = 0.2        # robust centroid: drop least-similar refs once
RECLAIM_MIN_REF_VECS = 3       # abort if the SELECTED voice-print has fewer

# Merge passing windows separated by <= this into one reclaimed range.
RECLAIM_JOIN_GAP_SEC = 1.0

# Ignore leftover slivers shorter than this after interval subtraction.
_MIN_SLIVER_SEC = 0.05


# ---------------------------------------------------------------------------
# Pure helpers (no numpy/torch — unit-testable in any venv)
# ---------------------------------------------------------------------------

def plan_windows(
    start: float,
    end: float,
    window_sec: float = RECLAIM_WINDOW_SEC,
    hop_sec: float = RECLAIM_HOP_SEC,
    min_sec: float = RECLAIM_MIN_WINDOW_SEC,
) -> list[tuple[float, float]]:
    """
    Fixed-length windows covering [start, end]. A span shorter than
    `window_sec` yields itself as a single (shorter) window if it is at
    least `min_sec`, else nothing. A final flush-with-the-end window is
    added so the last seconds are always scored (that is where the
    observed truncations live).
    """
    dur = end - start
    if dur < min_sec:
        return []
    if dur <= window_sec:
        return [(start, end)]
    out: list[tuple[float, float]] = []
    t = start
    while t + window_sec <= end + 1e-9:
        out.append((t, t + window_sec))
        t += hop_sec
    if out and out[-1][1] < end - 1e-9:
        out.append((end - window_sec, end))
    return out


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity; 0.0 for degenerate (zero/empty) inputs."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (na * nb)


def _mean(vectors: list[list[float]]) -> list[float]:
    n = len(vectors)
    dim = len(vectors[0])
    out = [0.0] * dim
    for v in vectors:
        for i in range(dim):
            out[i] += v[i]
    return [x / n for x in out]


def robust_centroid(
    vectors: list[list[float]],
    trim_frac: float = RECLAIM_TRIM_FRAC,
) -> list[float] | None:
    """
    Mean embedding, robust to contamination: compute the plain mean, drop
    the `trim_frac` of vectors least similar to it (bleed from neighbouring
    speakers inside padded/merged segments), and re-average the rest.
    """
    if not vectors:
        return None
    if len(vectors) <= 2 or trim_frac <= 0.0:
        return _mean(vectors)
    c0 = _mean(vectors)
    ranked = sorted(vectors, key=lambda v: cosine(v, c0))
    drop = int(len(ranked) * trim_frac)
    kept = ranked[drop:]
    return _mean(kept) if kept else c0


def evenly_sample(items: list, cap: int) -> list:
    """At most `cap` items, evenly spaced across the input (order kept)."""
    if cap <= 0 or len(items) <= cap:
        return list(items)
    step = len(items) / cap
    return [items[int(i * step)] for i in range(cap)]


def merge_ranges(
    ranges: list[tuple[float, float]],
    join_gap: float = RECLAIM_JOIN_GAP_SEC,
) -> list[tuple[float, float]]:
    """Sort + merge (start, end) pairs whose gap is <= join_gap."""
    if not ranges:
        return []
    rs = sorted(ranges)
    out = [list(rs[0])]
    for s, e in rs[1:]:
        if s - out[-1][1] <= join_gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def subtract_ranges(
    segments: list[dict[str, Any]],
    holes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Interval subtraction: remove `holes` ({start, end} dicts) from
    `segments`, splitting segments where a hole lands inside one. Extra
    keys (e.g. speaker) are preserved on the resulting pieces; slivers
    shorter than _MIN_SLIVER_SEC are dropped.

    Used to take reclaimed ranges OUT of the "other speakers" timeline the
    render consults, so a reclaimed span no longer caps the silence-aware
    extension or counts as other-speaker occupancy in gap bridging.
    """
    hs = merge_ranges(
        [(float(h["start"]), float(h["end"])) for h in holes], join_gap=0.0,
    )
    if not hs:
        return list(segments)
    out: list[dict[str, Any]] = []
    for seg in segments:
        pieces = [(float(seg["start"]), float(seg["end"]))]
        for a, b in hs:
            nxt: list[tuple[float, float]] = []
            for s, e in pieces:
                if b <= s or a >= e:      # no overlap
                    nxt.append((s, e))
                    continue
                if a > s:
                    nxt.append((s, a))    # left remainder
                if b < e:
                    nxt.append((b, e))    # right remainder
            pieces = nxt
        for s, e in pieces:
            if e - s >= _MIN_SLIVER_SEC:
                out.append({**seg, "start": s, "end": e})
    return out


def score_candidates(
    selected_segments: list[dict[str, Any]],
    other_segments: list[dict[str, Any]],
    embed_fn: Callable[[list[tuple[float, float]]], list[list[float] | None]],
    *,
    window_sec: float = RECLAIM_WINDOW_SEC,
    hop_sec: float = RECLAIM_HOP_SEC,
    min_window_sec: float = RECLAIM_MIN_WINDOW_SEC,
    margin: float = RECLAIM_MARGIN,
    min_similarity: float = RECLAIM_MIN_SIMILARITY,
    max_ref_windows: int = RECLAIM_MAX_REF_WINDOWS,
    trim_frac: float = RECLAIM_TRIM_FRAC,
    min_ref_vecs: int = RECLAIM_MIN_REF_VECS,
    join_gap: float = RECLAIM_JOIN_GAP_SEC,
) -> tuple[list[tuple[float, float]], dict[str, Any]]:
    """
    Pure scoring pipeline (embeddings come from the injected `embed_fn`,
    which maps a list of (start, end) windows to a list of vectors — None
    for windows the model could not embed).

    Returns (reclaimed_ranges, stats). A window inside another speaker's
    segment is reclaimed when its similarity to the selected voice-print
    clears `min_similarity` AND beats the best non-selected voice-print by
    `margin`. The candidate's own cluster centroid is computed leave-one-
    out (reference windows overlapping the candidate segment are excluded)
    so a mislabeled turn cannot vouch for itself.
    """
    stats: dict[str, Any] = {"selected_refs": 0, "per_speaker": {}}

    # --- selected speaker's voice-print -----------------------------------
    sel_windows = [
        w
        for s in selected_segments
        for w in plan_windows(
            float(s["start"]), float(s["end"]),
            window_sec=window_sec, hop_sec=window_sec, min_sec=min_window_sec,
        )
    ]
    sel_windows = evenly_sample(sel_windows, max_ref_windows)
    sel_vecs = [v for v in embed_fn(sel_windows) if v is not None]
    stats["selected_refs"] = len(sel_vecs)
    if len(sel_vecs) < min_ref_vecs:
        stats["reason"] = "insufficient selected-speaker reference audio"
        return [], stats
    sel_centroid = robust_centroid(sel_vecs, trim_frac)

    # --- other speakers' reference vectors (kept with their spans for LOO) -
    by_speaker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in other_segments:
        by_speaker[str(s.get("speaker"))].append(s)

    refs: dict[str, list[tuple[tuple[float, float], list[float]]]] = {}
    for sp, segs in by_speaker.items():
        windows = [
            w
            for s in segs
            for w in plan_windows(
                float(s["start"]), float(s["end"]),
                window_sec=window_sec, hop_sec=window_sec, min_sec=min_window_sec,
            )
        ]
        windows = evenly_sample(windows, max_ref_windows)
        vecs = embed_fn(windows)
        refs[sp] = [(w, v) for w, v in zip(windows, vecs) if v is not None]

    # Full (non-LOO) centroids, used when the speaker is NOT the candidate's.
    full_centroid = {
        sp: robust_centroid([v for _, v in rv], trim_frac)
        for sp, rv in refs.items()
    }

    # --- score every other-speaker segment, window by window --------------
    reclaimed: list[tuple[float, float]] = []
    for sp, segs in by_speaker.items():
        sp_stat = {"windows": 0, "passed": 0, "reclaimed_sec": 0.0, "max_sim": 0.0}
        for seg in segs:
            seg_s, seg_e = float(seg["start"]), float(seg["end"])
            cand = plan_windows(
                seg_s, seg_e,
                window_sec=window_sec, hop_sec=hop_sec, min_sec=min_window_sec,
            )
            if not cand:
                continue
            vecs = embed_fn(cand)

            # Leave-one-out centroid of the candidate's own cluster.
            loo_vecs = [
                v for (ws, we), v in refs.get(sp, [])
                if we <= seg_s or ws >= seg_e
            ]
            loo_centroid = robust_centroid(loo_vecs, trim_frac)

            rivals = [c for osp, c in full_centroid.items() if osp != sp and c]
            if loo_centroid:
                rivals.append(loo_centroid)

            passing: list[tuple[float, float]] = []
            for (ws, we), v in zip(cand, vecs):
                if v is None:
                    continue
                sp_stat["windows"] += 1
                sim_sel = cosine(v, sel_centroid)
                sp_stat["max_sim"] = max(sp_stat["max_sim"], sim_sel)
                if sim_sel < min_similarity:
                    continue
                best_rival = max((cosine(v, c) for c in rivals), default=None)
                # No rival evidence at all -> the floor alone decides
                # (recall-first: sounds like the selected speaker, keep it).
                if best_rival is None or sim_sel > best_rival + margin:
                    passing.append((ws, we))
                    sp_stat["passed"] += 1
            for s_, e_ in merge_ranges(passing, join_gap):
                reclaimed.append((s_, e_))
                sp_stat["reclaimed_sec"] += e_ - s_
        stats["per_speaker"][sp] = sp_stat

    return merge_ranges(reclaimed, join_gap), stats


# ---------------------------------------------------------------------------
# I/O + model layer (worker only — heavy imports are lazy)
# ---------------------------------------------------------------------------

class _WavWindowReader:
    """
    Random-access window reader for the mono s16le WAV the worker produces
    (worker/tasks/audio.py). Stdlib `wave` only — no new dependencies.
    """

    def __init__(self, path: Path):
        import wave

        self._wf = wave.open(str(path), "rb")
        if self._wf.getnchannels() != 1 or self._wf.getsampwidth() != 2:
            raise RuntimeError(
                "reclaim expects mono s16le wav, got "
                f"channels={self._wf.getnchannels()} "
                f"sampwidth={self._wf.getsampwidth()}"
            )
        self.rate = self._wf.getframerate()
        if self.rate != SAMPLE_RATE:
            raise RuntimeError(
                f"reclaim expects {SAMPLE_RATE} Hz audio, got {self.rate}"
            )
        self.n_frames = self._wf.getnframes()

    def read(self, start_sec: float, end_sec: float):
        """Float32 numpy array in [-1, 1], or None for empty/out-of-range."""
        import numpy as np

        a = max(0, int(round(start_sec * self.rate)))
        b = min(self.n_frames, int(round(end_sec * self.rate)))
        if b - a <= 0:
            return None
        self._wf.setpos(a)
        buf = self._wf.readframes(b - a)
        return np.frombuffer(buf, dtype="<i2").astype("float32") / 32768.0

    def close(self) -> None:
        try:
            self._wf.close()
        except Exception:  # noqa: BLE001
            pass


def _load_embedder(device: Any, hf_token: str | None) -> Any:
    """
    Load the speaker-embedding model, tolerating the pyannote.audio 3.x /
    4.x constructor rename (use_auth_token -> token). Verified live against
    4.0.7; the 3.1.1 signature comes from its released source.
    """
    from pyannote.audio.pipelines.speaker_verification import (
        PretrainedSpeakerEmbedding,
    )

    try:
        return PretrainedSpeakerEmbedding(
            EMBEDDING_MODEL, device=device, token=hf_token,
        )
    except TypeError:
        return PretrainedSpeakerEmbedding(
            EMBEDDING_MODEL, device=device, use_auth_token=hf_token,
        )


def _make_embed_fn(
    reader: _WavWindowReader,
    embedder: Any,
    torch_mod: Any,
    device: Any,
    batch_size: int = 64,
) -> Callable[[list[tuple[float, float]]], list[list[float] | None]]:
    """Batched (start, end) -> embedding-vector adapter around the model."""
    import numpy as np

    def embed(windows: list[tuple[float, float]]) -> list[list[float] | None]:
        out: list[list[float] | None] = [None] * len(windows)
        arrs: list[Any] = [None] * len(windows)
        by_len: dict[int, list[int]] = defaultdict(list)
        for i, (ws, we) in enumerate(windows):
            arr = reader.read(ws, we)
            if arr is None or len(arr) == 0:
                continue
            arrs[i] = arr
            by_len[len(arr)].append(i)
        for idxs in by_len.values():
            for k0 in range(0, len(idxs), batch_size):
                chunk = idxs[k0:k0 + batch_size]
                batch = np.stack([arrs[i] for i in chunk])
                wav = torch_mod.from_numpy(batch).unsqueeze(1).to(device)
                embs = embedder(wav)  # numpy (B, D); NaN rows for too-short
                for k, i in enumerate(chunk):
                    row = embs[k]
                    if np.all(np.isfinite(row)):
                        out[i] = [float(x) for x in row]
        return out

    return embed


def reclaim_for_render(
    job_id: str,
    source_path: Path,
    job_dir: Path,
    selected: str,
    all_segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Full reclamation pass for one render. Returns extra {start, end} ranges
    that belong in the selected speaker's cut list ([] when there is
    nothing to reclaim). Raises on infrastructure failures — the caller
    (run_render) treats any exception as "render without reclamation".
    """
    mine = [s for s in all_segments if s.get("speaker") == selected]
    others = [s for s in all_segments if s.get("speaker") != selected]
    if not mine or not others:
        return []

    # 16 kHz mono PCM for the embedding model — the exact command
    # worker/tasks/audio.py uses, run on the already-downloaded source so
    # this works even when jobs/{id}/audio.wav has expired from R2.
    wav_path = Path(job_dir) / "reclaim_audio.wav"
    run_ffmpeg([
        "-i", str(source_path),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", str(SAMPLE_RATE),
        "-ac", "1",
        str(wav_path),
        "-y",
    ])

    import torch  # lazy — GPU worker only

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    reader: _WavWindowReader | None = None
    embedder: Any = None
    try:
        embedder = _load_embedder(device, os.environ.get("HF_TOKEN") or None)
        reader = _WavWindowReader(wav_path)
        embed_fn = _make_embed_fn(reader, embedder, torch, device)
        ranges, stats = score_candidates(mine, others, embed_fn)

        if "reason" in stats:
            logger.info(
                "reclaim[%s] skipped: %s", job_id, stats["reason"],
            )
        for sp, st in sorted(stats.get("per_speaker", {}).items()):
            if st["passed"]:
                logger.info(
                    "reclaim[%s] %s: %d/%d windows matched %s better "
                    "(%.1fs reclaimed, max_sim=%.3f)",
                    job_id, sp, st["passed"], st["windows"], selected,
                    st["reclaimed_sec"], st["max_sim"],
                )
        total = sum(e - s for s, e in ranges)
        logger.info(
            "reclaim[%s] result: %d range(s), %.1fs total for %s",
            job_id, len(ranges), total, selected,
        )
        return [{"start": s, "end": e} for s, e in ranges]
    finally:
        if reader is not None:
            reader.close()
        try:
            del embedder
        except Exception:  # noqa: BLE001
            pass
        if device.type == "cuda":
            try:
                torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass
        try:
            wav_path.unlink()
        except OSError:
            pass
