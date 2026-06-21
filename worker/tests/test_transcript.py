"""
Unit tests for the transcript pure functions:
  - diarize.build_transcript
  - render.select_transcript_text

The repo has no pytest/test harness and worker/tasks/__init__.py imports the
Celery task modules (which pull in yt_dlp, unavailable in the dev/test env).
So we load the two task modules DIRECTLY by file path, bypassing the package
__init__, and run plain asserts. Run with:

    ./venv/bin/python worker/tests/test_transcript.py
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


diarize = _load("diarize_under_test", "worker/tasks/diarize.py")
render = _load("render_under_test", "worker/tasks/render.py")

build_transcript = diarize.build_transcript
select_transcript_text = render.select_transcript_text


# --------------------------------------------------------------------------
# build_transcript
# --------------------------------------------------------------------------

def test_build_transcript_keeps_no_speaker_and_sorts_and_drops_junk():
    result = {
        "segments": [
            {"start": 5.0, "end": 6.0, "speaker": "SPEAKER_01", "text": "second"},
            {"start": 1.0, "end": 2.0, "text": "no speaker here"},          # speaker absent
            {"start": 3.0, "end": 3.0, "speaker": "SPEAKER_00", "text": "zero-len"},  # dropped
            {"start": 4.0, "end": 5.0, "speaker": "SPEAKER_00", "text": "   "},        # empty -> dropped
            {"start": 0.5, "end": 1.0, "speaker": "SPEAKER_00", "text": "  first  "}, # trimmed
        ]
    }
    out = build_transcript(result)
    # zero-len and empty-text dropped -> 3 remain
    assert [s["text"] for s in out] == ["first", "no speaker here", "second"], out
    # sorted by start
    assert [s["start"] for s in out] == [0.5, 1.0, 5.0]
    # no-speaker segment preserved with speaker=None
    assert out[1]["speaker"] is None
    # text is trimmed
    assert out[0]["text"] == "first"


def test_build_transcript_empty():
    assert build_transcript({}) == []
    assert build_transcript({"segments": []}) == []


# --------------------------------------------------------------------------
# select_transcript_text
# --------------------------------------------------------------------------

def _seg(start, end, text, speaker="SPEAKER_00"):
    return {"start": start, "end": end, "speaker": speaker, "text": text}


def test_select_overlap_in_and_out():
    transcript = [
        _seg(0.0, 1.0, "inside"),
        _seg(50.0, 51.0, "outside"),
    ]
    windows = [{"start": 0.0, "end": 10.0}]
    assert select_transcript_text(transcript, windows) == "inside"


def test_select_dedup_across_two_windows():
    # One segment overlaps two different windows -> emitted exactly once.
    transcript = [_seg(9.0, 21.0, "spanning")]
    windows = [{"start": 0.0, "end": 10.0}, {"start": 20.0, "end": 30.0}]
    assert select_transcript_text(transcript, windows) == "spanning"


def test_select_touching_is_excluded_half_open():
    # seg.end == win.start and seg.start == win.end must NOT be pulled in.
    transcript = [_seg(0.0, 5.0, "ends-at-window-start")]
    windows = [{"start": 5.0, "end": 10.0}]
    assert select_transcript_text(transcript, windows) == ""


def test_select_bridged_no_speaker_segment_included_recall():
    # The crux: a no-speaker span that bridging pulled into the video must
    # appear, selected purely by window overlap (not speaker).
    transcript = [
        _seg(0.0, 2.0, "mine A", speaker="SPEAKER_03"),
        _seg(2.0, 4.0, "dropped no-speaker words", speaker=None),
        _seg(4.0, 6.0, "mine B", speaker="SPEAKER_03"),
    ]
    windows = [{"start": 0.0, "end": 6.0}]  # bridged into one continuous cut
    assert select_transcript_text(transcript, windows) == (
        "mine A dropped no-speaker words mine B"
    )


def test_select_orders_by_start():
    transcript = [
        _seg(8.0, 9.0, "third"),
        _seg(1.0, 2.0, "first"),
        _seg(4.0, 5.0, "second"),
    ]
    windows = [{"start": 0.0, "end": 100.0}]
    assert select_transcript_text(transcript, windows) == "first second third"


def test_select_empty_inputs():
    assert select_transcript_text([], [{"start": 0.0, "end": 10.0}]) == ""
    assert select_transcript_text([_seg(0.0, 1.0, "x")], []) == ""


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------

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
