"""
Unit tests for worker/tasks/voiceprints.py (per-speaker voice prints for
favourite-voice matching) and the vector helpers in shared/voice_match.py.

Pure: embeddings come from an injected `embed_fn` over a synthetic
timeline, so no torch/pyannote/numpy is needed. Run with:

    ./venv/bin/python worker/tests/test_voiceprints.py
"""

import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from shared import voice_match  # noqa: E402
from worker.tasks import voiceprints as vp  # noqa: E402

ANALYST = [1.0, 0.0, 0.0]
ANCHOR = [0.0, 1.0, 0.0]


def embed_from(truth):
    """Window -> mean voice over 8 sample points (None where nobody talks)."""
    def voice_at(t):
        for s, e, v in truth:
            if s <= t < e:
                return v
        return None

    def embed(windows):
        out = []
        for ws, we in windows:
            vs = [voice_at(ws + (we - ws) * (i + 0.5) / 8) for i in range(8)]
            vs = [v for v in vs if v]
            out.append([sum(c) / len(vs) for c in zip(*vs)] if vs else None)
        return out

    return embed


def test_turn_spans_drop_overlap_and_trim_edges():
    turns = [
        {"speaker": "A", "start": 10.0, "end": 30.0},
        {"speaker": "B", "start": 18.0, "end": 20.0},   # interjection inside A
        {"speaker": "A", "start": 40.0, "end": 41.0},   # too short once trimmed
    ]
    spans = vp.clean_spans_from_turns(turns, "A")
    assert spans == [(10.25, 17.75), (20.25, 29.75)], spans


def test_segment_fallback_shrinks_padding():
    segs = [
        {"speaker": "A", "start": 5.0, "end": 12.0},
        {"speaker": "B", "start": 12.0, "end": 20.0},
        {"speaker": "A", "start": 30.0, "end": 32.5},   # 1.5 s after shrink -> dropped
    ]
    assert vp.clean_spans_from_segments(segs, "A") == [(5.5, 11.5)]


def test_window_plan_is_capped_and_spread():
    spans = [(0.0, 1000.0)]
    ws = vp.plan_print_windows(spans)
    assert len(ws) == vp.MAX_WINDOWS
    assert ws[0][0] == 0.0 and ws[-1][1] > 900.0, (ws[0], ws[-1])


def test_prints_are_unit_and_skip_short_speakers():
    truth = [(0.0, 120.0, ANALYST), (200.0, 210.0, ANCHOR)]
    spans = {"A": [(0.0, 120.0)], "B": [(200.0, 210.0)]}   # B: 10 s < 15 s
    prints = vp.compute_prints(spans, embed_from(truth))
    assert set(prints) == {"A"}, prints.keys()
    c = prints["A"]["centroid"]
    assert abs(math.sqrt(sum(x * x for x in c)) - 1.0) < 1e-9
    assert voice_match.dot(c, ANALYST) > 0.999
    assert prints["A"]["speech_sec"] == 120.0
    assert len(prints["A"]["windows"]) <= vp.KEEP_WINDOWS


def test_unembeddable_windows_do_not_count():
    # Speech span has no audio behind it -> every window embeds to None.
    prints = vp.compute_prints({"A": [(0.0, 60.0)]}, embed_from([]))
    assert prints == {}


def test_turns_preferred_over_segments():
    turns = [{"speaker": "A", "start": 0.0, "end": 30.0}]
    segs = [{"speaker": "A", "start": 0.0, "end": 60.0}]
    spans, source = vp.spans_for_labels(["A"], turns, segs)
    assert source == "turns" and spans["A"] == [(0.25, 29.75)]
    spans, source = vp.spans_for_labels(["A"], None, segs)
    assert source == "segments" and spans["A"] == [(0.5, 59.5)]


def test_pack_roundtrip_and_normalize():
    v = voice_match.normalize([3.0, 4.0])
    assert v == [0.6, 0.8]
    assert voice_match.normalize([0.0, 0.0]) is None
    blob = voice_match.pack_many([[0.5, 0.25], [1.0, -1.0]])
    assert voice_match.unpack_many(blob, dim=2) == [[0.5, 0.25], [1.0, -1.0]]
    assert len(voice_match.pack([0.0] * voice_match.DIM)) == 1024


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
