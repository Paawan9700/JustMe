"""
Unit tests for worker/tasks/reclaim.py — the render-time voice-print
reclamation that pulls back turns diarization assigned to the wrong
speaker (job bc5ce57c: the analyst's Titan stop-loss/targets turn was
labeled as the anchor and silently vanished from the final video).

All tests are pure: embeddings come from an injected `embed_fn` built on a
synthetic ground-truth timeline, so no torch/pyannote/numpy is needed.
Same self-running style as the other worker tests. Run with:

    ./venv/bin/python worker/tests/test_reclaim.py
"""

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load(modname, relpath):
    spec = importlib.util.spec_from_file_location(modname, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


reclaim = _load("reclaim_under_test", "worker/tasks/reclaim.py")

# Three orthogonal "voices" — cosine between different voices is 0, so the
# similarity floor (0.15) cleanly separates match from non-match.
ANALYST = [1.0, 0.0, 0.0]
ANCHOR = [0.0, 1.0, 0.0]
GUEST = [0.0, 0.0, 1.0]
# A voice acoustically CLOSE to the analyst (cos = 0.6) — for the precision
# test: similar-but-different speakers must NOT be reclaimed as long as
# their own cluster matches them better.
NEAR_ANALYST = [0.6, 0.8, 0.0]


def make_embed_fn(truth):
    """
    truth: list of (start, end, voice_vector) — who is REALLY speaking when.
    A window's embedding is the mean voice over 8 sample points (so windows
    straddling a real turn change get mixed vectors, like real audio).
    Windows over un-covered time return None (model couldn't embed).
    """
    def voice_at(t):
        for s, e, v in truth:
            if s <= t < e:
                return v
        return None

    def embed(windows):
        out = []
        for ws, we in windows:
            pts = [ws + (we - ws) * (i + 0.5) / 8.0 for i in range(8)]
            vs = [voice_at(p) for p in pts]
            vs = [v for v in vs if v is not None]
            if not vs:
                out.append(None)
                continue
            dim = len(vs[0])
            out.append([sum(v[i] for v in vs) / len(vs) for i in range(dim)])
        return out

    return embed


def _covers(ranges, lo, hi):
    """True if [lo, hi] is fully inside the union of `ranges`."""
    t = lo
    for s, e in sorted(ranges):
        if s > t + 1e-6:
            break
        t = max(t, e)
        if t >= hi - 1e-6:
            return True
    return t >= hi - 1e-6


def _intersects(ranges, lo, hi):
    return any(e > lo + 1e-6 and s < hi - 1e-6 for s, e in ranges)


# ---------------------------------------------------------------------------
# plan_windows
# ---------------------------------------------------------------------------

def test_plan_windows_short_span_skipped():
    assert reclaim.plan_windows(10.0, 11.0, 4.0, 2.0, 1.5) == []


def test_plan_windows_subwindow_span_is_single_window():
    assert reclaim.plan_windows(10.0, 13.0, 4.0, 2.0, 1.5) == [(10.0, 13.0)]


def test_plan_windows_covers_to_the_end():
    wins = reclaim.plan_windows(0.0, 25.0, 4.0, 2.0, 1.5)
    assert wins[0] == (0.0, 4.0)
    # flush tail window so the last seconds are always scored
    assert wins[-1] == (21.0, 25.0), wins
    assert all(abs((e - s) - 4.0) < 1e-9 for s, e in wins)


# ---------------------------------------------------------------------------
# cosine / robust_centroid / evenly_sample / merge_ranges
# ---------------------------------------------------------------------------

def test_cosine_basics():
    assert abs(reclaim.cosine(ANALYST, ANALYST) - 1.0) < 1e-9
    assert abs(reclaim.cosine(ANALYST, ANCHOR)) < 1e-9
    assert reclaim.cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert reclaim.cosine([], [1.0]) == 0.0


def test_robust_centroid_resists_contamination():
    vecs = [ANALYST] * 8 + [ANCHOR] * 2
    c = reclaim.robust_centroid(vecs, trim_frac=0.2)
    assert reclaim.cosine(c, ANALYST) > 0.99, c
    assert reclaim.robust_centroid([], 0.2) is None


def test_evenly_sample_caps_and_passthrough():
    assert reclaim.evenly_sample([1, 2, 3], 5) == [1, 2, 3]
    sampled = reclaim.evenly_sample(list(range(100)), 10)
    assert len(sampled) == 10
    assert sampled[0] == 0 and sampled[-1] >= 80


def test_merge_ranges_joins_within_gap():
    merged = reclaim.merge_ranges([(0, 2), (2.5, 4), (10, 12)], join_gap=1.0)
    assert merged == [(0, 4), (10, 12)]


# ---------------------------------------------------------------------------
# subtract_ranges
# ---------------------------------------------------------------------------

def test_subtract_no_overlap_keeps_segment():
    segs = [{"speaker": "A", "start": 0.0, "end": 10.0}]
    out = reclaim.subtract_ranges(segs, [{"start": 20.0, "end": 30.0}])
    assert out == segs


def test_subtract_hole_inside_splits_and_keeps_speaker():
    segs = [{"speaker": "A", "start": 0.0, "end": 10.0}]
    out = reclaim.subtract_ranges(segs, [{"start": 4.0, "end": 6.0}])
    assert out == [
        {"speaker": "A", "start": 0.0, "end": 4.0},
        {"speaker": "A", "start": 6.0, "end": 10.0},
    ]


def test_subtract_full_cover_drops_segment():
    segs = [{"speaker": "A", "start": 5.0, "end": 8.0}]
    assert reclaim.subtract_ranges(segs, [{"start": 4.0, "end": 9.0}]) == []


def test_subtract_edge_trim_and_sliver_drop():
    segs = [{"speaker": "A", "start": 0.0, "end": 10.0}]
    out = reclaim.subtract_ranges(segs, [{"start": 0.0, "end": 9.99}])
    assert out == []  # 0.01s sliver dropped


# ---------------------------------------------------------------------------
# score_candidates — the Titan scenario (mirrors job bc5ce57c's geometry)
# ---------------------------------------------------------------------------

def _titan_setup():
    # Ground truth: the analyst REALLY speaks 2954-2988 AND 2990.8-3016.2,
    # but diarization labeled the second turn as the anchor (SPEAKER_11).
    truth = [
        (2855.0, 2934.0, ANCHOR),
        (2954.0, 2988.6, ANALYST),
        (2990.8, 3016.2, ANALYST),   # <-- the stolen turn
        (3018.5, 3070.0, GUEST),
        (3076.0, 3084.0, ANCHOR),
        (4800.0, 4915.0, ANCHOR),
    ]
    selected = [{"speaker": "S13", "start": 2954.0, "end": 2988.6}]
    others = [
        {"speaker": "S11", "start": 2855.0, "end": 2923.0},
        {"speaker": "S11", "start": 2931.0, "end": 2934.0},
        {"speaker": "S11", "start": 2990.8, "end": 3016.2},  # mislabeled
        {"speaker": "S11", "start": 3076.0, "end": 3084.0},
        {"speaker": "S11", "start": 4800.0, "end": 4915.0},
        {"speaker": "S02", "start": 3018.5, "end": 3044.0},
    ]
    return truth, selected, others


def test_titan_stolen_turn_is_reclaimed():
    truth, selected, others = _titan_setup()
    ranges, stats = reclaim.score_candidates(
        selected, others, make_embed_fn(truth),
    )
    # The heart of the stolen turn must be recovered (window quantisation
    # may shave < hop_sec at the edges; render padding absorbs that).
    assert _covers(ranges, 2991.0, 3016.0), (ranges, stats)


def test_titan_genuine_turns_stay_out():
    truth, selected, others = _titan_setup()
    ranges, _ = reclaim.score_candidates(selected, others, make_embed_fn(truth))
    assert not _intersects(ranges, 2855.0, 2923.0), ranges   # anchor's own
    assert not _intersects(ranges, 4800.0, 4915.0), ranges   # anchor's own
    assert not _intersects(ranges, 3018.5, 3044.0), ranges   # guest's own


def test_similar_voice_not_reclaimed_when_own_cluster_matches_better():
    # NEAR_ANALYST has cosine 0.6 to the analyst — above the floor — but its
    # own cluster matches it at 1.0, so it must NOT be reclaimed.
    truth = [
        (100.0, 160.0, ANALYST),
        (200.0, 260.0, NEAR_ANALYST),
        (300.0, 360.0, NEAR_ANALYST),
    ]
    selected = [{"speaker": "S1", "start": 100.0, "end": 160.0}]
    others = [
        {"speaker": "S2", "start": 200.0, "end": 260.0},
        {"speaker": "S2", "start": 300.0, "end": 360.0},
    ]
    ranges, _ = reclaim.score_candidates(selected, others, make_embed_fn(truth))
    assert ranges == [], ranges


def test_single_segment_cluster_cannot_certify_itself():
    # A whole spurious cluster whose ONLY segment is really the analyst:
    # leave-one-out removes its self-reference, the rival centroids lose,
    # and the segment is reclaimed.
    truth = [
        (100.0, 160.0, ANALYST),
        (200.0, 230.0, ANALYST),    # spurious cluster S9, truly the analyst
        (300.0, 360.0, ANCHOR),
    ]
    selected = [{"speaker": "S1", "start": 100.0, "end": 160.0}]
    others = [
        {"speaker": "S9", "start": 200.0, "end": 230.0},
        {"speaker": "S11", "start": 300.0, "end": 360.0},
    ]
    ranges, stats = reclaim.score_candidates(
        selected, others, make_embed_fn(truth),
    )
    assert _covers(ranges, 201.0, 229.0), (ranges, stats)
    assert not _intersects(ranges, 300.0, 360.0), ranges


def test_insufficient_selected_reference_aborts():
    # Selected speaker too short to build a voice-print -> no reclamation,
    # explicit reason (render then proceeds exactly as today).
    truth = [(100.0, 102.0, ANALYST), (200.0, 260.0, ANCHOR)]
    selected = [{"speaker": "S1", "start": 100.0, "end": 102.0}]
    others = [{"speaker": "S11", "start": 200.0, "end": 260.0}]
    ranges, stats = reclaim.score_candidates(
        selected, others, make_embed_fn(truth),
    )
    assert ranges == []
    assert stats.get("reason"), stats


def test_unembeddable_windows_are_skipped_not_reclaimed():
    # Windows the model can't embed (None) must simply be ignored.
    truth = [(100.0, 160.0, ANALYST)]     # nothing covers 200-260
    selected = [{"speaker": "S1", "start": 100.0, "end": 160.0}]
    others = [{"speaker": "S11", "start": 200.0, "end": 260.0}]
    ranges, _ = reclaim.score_candidates(selected, others, make_embed_fn(truth))
    assert ranges == [], ranges


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
