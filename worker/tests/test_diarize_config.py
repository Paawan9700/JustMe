"""
Unit tests for the WhisperX VAD recall config in diarize.py.

These lock in the recall-first VAD tuning that stops WhisperX from dropping
clearly-audible speech (the bug where an analyst's stop-loss/targets/reasoning
were discarded before Whisper ever saw them). They assert INVARIANTS relative to
the WhisperX defaults (vad_onset=0.500, vad_offset=0.363), not exact values, so
the numbers can be tuned during validation without breaking the tests — what
must never regress is the direction: onset/offset strictly BELOW the defaults.

Loaded by file path (like worker/tests/test_transcript.py) so it runs in the
dev/test venv without whisperx/torch (those imports are lazy inside diarize).
Run with:

    ./venv/bin/python worker/tests/test_diarize_config.py
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

# WhisperX library defaults we are tuning away from.
DEFAULT_VAD_ONSET = 0.500
DEFAULT_VAD_OFFSET = 0.363


def test_vad_options_exist_with_expected_keys():
    vad = diarize.VAD_OPTIONS
    assert isinstance(vad, dict)
    assert {"vad_onset", "vad_offset", "chunk_size"} <= set(vad)


def test_vad_is_recall_first_relative_to_defaults():
    # Lower onset/offset than the library defaults => more speech kept. This is
    # the whole point of the fix; if either creeps back to the default the
    # regression (dropped words) returns.
    assert diarize.VAD_OPTIONS["vad_onset"] < DEFAULT_VAD_ONSET
    assert diarize.VAD_OPTIONS["vad_offset"] < DEFAULT_VAD_OFFSET


def test_vad_thresholds_are_sane_probabilities():
    # Hysteresis thresholds are probabilities in (0, 1); a 0 would flag
    # everything (all-noise transcription), so keep them strictly positive.
    for key in ("vad_onset", "vad_offset"):
        val = diarize.VAD_OPTIONS[key]
        assert 0.0 < val < 1.0, f"{key}={val} out of (0,1)"


def test_whisper_model_unchanged():
    assert diarize.WHISPER_MODEL == "large-v3"


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
