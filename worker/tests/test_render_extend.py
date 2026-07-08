"""
Unit tests for render.silence_aware_extend — specifically the TALK-OVER
recovery (OVERLAP_EXTEND_SEC): when another speaker overlaps a turn boundary,
the selected speaker's own trailing/leading words should be recovered by a
bounded extension INTO the overlap, instead of the cut stopping dead at the
boundary (the bug that clipped an analyst's final stop-loss number).

Same self-running style as test_transcript.py (no pytest). Run with:

    ./venv/bin/python worker/tests/test_render_extend.py
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


render = _load("render_under_test", "worker/tasks/render.py")
extend = render.silence_aware_extend


def _one(seg, others, silences, **kw):
    kw.setdefault("overlap_extend_sec", 3.0)
    kw.setdefault("max_extend_sec", 20.0)
    return extend([seg], others, silences, **kw)[0]


def _close(a, b, tol=1e-6):
    return abs(a - b) <= tol


def test_talkover_end_recovers_bounded():
    # Other speaker starts at 18 (before our end 20) and runs to 40 -> talk-over.
    # No silence -> recover exactly overlap_extend_sec (3.0) into the overlap.
    # (We assert only the END here; the START, with no earlier speaker/silence,
    # extends to the video start — not what this case is about.)
    r = _one({"start": 10.0, "end": 20.0}, [{"start": 18.0, "end": 40.0}], [])
    assert _close(r["end"], 23.0), r


def test_talkover_end_halts_at_silence():
    # A real pause at 21.5 must stop the overlap extension before the 3s cap.
    r = _one({"start": 10.0, "end": 20.0},
             [{"start": 18.0, "end": 40.0}], [(21.5, 25.0)])
    assert _close(r["end"], 21.5), r


def test_clean_turntaking_unchanged():
    # Non-overlap: next speaker starts at 25 (after our end 20). Existing
    # behaviour (extend over non-silent audio up to the next speaker) must be
    # unchanged by the talk-over patch -> end == 25, never crosses into them.
    r = _one({"start": 10.0, "end": 20.0}, [{"start": 25.0, "end": 40.0}], [])
    assert _close(r["end"], 25.0), r


def test_talkover_end_bounded_not_full_maxextend():
    # With a large max_extend but an overlapping speaker, we must NOT run the
    # full max_extend into them — only overlap_extend_sec.
    r = _one({"start": 10.0, "end": 20.0}, [{"start": 19.0, "end": 200.0}], [],
             max_extend_sec=50.0)
    assert _close(r["end"], 23.0), r


def test_talkover_start_recovers_bounded():
    # Other speaker overlaps our START (10..22 vs our start 20) -> recover 3s back.
    r = _one({"start": 20.0, "end": 30.0}, [{"start": 10.0, "end": 22.0}], [],
             duration_sec=100.0)
    assert _close(r["start"], 17.0), r


def test_end_in_silence_no_extension():
    # If our end sits inside a detected silence, do not extend at all.
    r = _one({"start": 10.0, "end": 20.0},
             [{"start": 18.0, "end": 40.0}], [(19.0, 25.0)])
    assert _close(r["end"], 20.0), r


def test_distinct_later_turn_caps_before_overlap_extend():
    # A DISTINCT turn starting 1s after our end must cap us at its start (1s),
    # even though overlap_extend would otherwise allow 3s.
    r = _one({"start": 10.0, "end": 20.0},
             [{"start": 18.0, "end": 40.0}, {"start": 21.0, "end": 22.0}], [])
    assert _close(r["end"], 21.0), r


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
