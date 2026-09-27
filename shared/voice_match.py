"""
Voice-print math shared by the worker (building prints), the backend
(matching a user's favourite voices against a new video's speakers) and
the calibration tooling.

A voice print is a speaker-embedding vector from
`pyannote/wespeaker-voxceleb-resnet34-LM` (256 floats). Every vector is
L2-normalised BEFORE it is stored, so similarity between two stored prints
is a plain dot product (== cosine).

Pure Python on purpose: importable in the API image (no torch), the worker
image and the local test venv (no numpy). Vectors are stored in Mongo as
little-endian float32 bytes — 1 KB per 256-d vector instead of ~3.2 KB for
a BSON array of doubles.
"""

from __future__ import annotations

import math
import struct
from typing import Iterable, Sequence

# Embedding dimension of wespeaker-voxceleb-resnet34-LM.
DIM = 256


def normalize(v: Sequence[float]) -> list[float] | None:
    """Unit-length copy of `v`; None for empty / zero / non-finite input."""
    if not v:
        return None
    try:
        n = math.sqrt(sum(float(x) * float(x) for x in v))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(n) or n <= 0.0:
        return None
    return [float(x) / n for x in v]


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    """Dot product (== cosine for unit vectors); 0.0 on a length mismatch."""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def mean_unit(vectors: Iterable[Sequence[float]]) -> list[float] | None:
    """Normalised mean of already-unit vectors (the centroid direction)."""
    vs = [v for v in vectors if v]
    if not vs:
        return None
    dim = len(vs[0])
    acc = [0.0] * dim
    for v in vs:
        if len(v) != dim:
            continue
        for i in range(dim):
            acc[i] += v[i]
    return normalize(acc)


def pack(v: Sequence[float]) -> bytes:
    """One vector -> little-endian float32 bytes."""
    return struct.pack(f"<{len(v)}f", *v)


def unpack(b: bytes) -> list[float]:
    """Inverse of `pack`."""
    return list(struct.unpack(f"<{len(b) // 4}f", bytes(b)))


def pack_many(vectors: Sequence[Sequence[float]]) -> bytes:
    """Several same-length vectors -> one float32 blob (row-major)."""
    return b"".join(pack(v) for v in vectors)


def unpack_many(b: bytes, dim: int = DIM) -> list[list[float]]:
    """Inverse of `pack_many` for `dim`-length rows."""
    flat = unpack(b)
    if dim <= 0 or len(flat) % dim:
        return []
    return [flat[i:i + dim] for i in range(0, len(flat), dim)]


# ---------------------------------------------------------------------------
# Matching a user's favourite voices against a video's speakers
# ---------------------------------------------------------------------------
# Thresholds measured 2026-09-27 on 24 past videos (worker/scripts/
# calibrate_voiceprints.py): the right person scored 0.755-0.99 against a
# favourite saved from ONE other video (0.87+ once it had three samples) and
# always ranked #1; the closest DIFFERENT person scored 0.636. Re-measure
# before changing these, and whenever the voiceprint recipe changes.
SHOW_THRESHOLD = 0.70        # speaker goes in the "Your favourites" box
STRONG_THRESHOLD = 0.80      # badge a match "strong" rather than "likely"
LEARN_THRESHOLD = 0.80       # picking the speaker teaches the favourite
LEARN_MIN_SPEECH_SEC = 60.0  # ...but only from a well-sampled speaker


def similarity_matrix(
    rows: Sequence[Sequence[float]], cols: Sequence[Sequence[float]],
) -> list[list[float]]:
    """rows x cols dot products; numpy when available, pure Python otherwise."""
    if not rows or not cols:
        return [[] for _ in rows]
    try:
        import numpy as np
    except ImportError:
        return [[dot(r, c) for c in cols] for r in rows]
    return (np.asarray(rows, dtype=np.float64) @ np.asarray(cols, dtype=np.float64).T).tolist()


def match_speakers(
    speakers: dict[str, Sequence[float]],
    favourites: Sequence[tuple[str, Sequence[Sequence[float]]]],
    *,
    show: float = SHOW_THRESHOLD,
    strong: float = STRONG_THRESHOLD,
) -> dict[str, dict]:
    """
    `speakers` maps label -> unit centroid; `favourites` is a list of
    (favorite_id, [unit sample centroids]). A speaker's score against a
    favourite is its best similarity to any of that favourite's samples.

    Returns {label: {"favorite_id", "score", "strength"}} for every speaker
    whose best favourite clears `show`. A speaker is only ever assigned its
    single best favourite, but one favourite may claim several speakers: the
    diarizer sometimes splits one person into two labels, and both belong in
    the box.
    """
    owners: list[str] = []
    samples: list[Sequence[float]] = []
    for favorite_id, vecs in favourites:
        for v in vecs:
            owners.append(favorite_id)
            samples.append(v)
    labels = [lb for lb, c in speakers.items() if c]
    if not labels or not samples:
        return {}
    sims = similarity_matrix([speakers[lb] for lb in labels], samples)
    out: dict[str, dict] = {}
    for label, row in zip(labels, sims):
        best = max(range(len(row)), key=row.__getitem__)
        score = float(row[best])
        if score >= show:
            out[label] = {
                "favorite_id": owners[best],
                "score": round(score, 4),
                "strength": "strong" if score >= strong else "likely",
            }
    return out
