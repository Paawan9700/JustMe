"""
Unit tests for worker/tasks/ingest.py::resolve_max_video_hours — the per-job
video-length cap the API stamps on each job (5 h for regular users, higher
for admins), with MAX_VIDEO_HOURS as the fallback for jobs created before the
field existed. Run with:

    ./venv/bin/python worker/tests/test_ingest_limits.py
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from worker.tasks.ingest import resolve_max_video_hours  # noqa: E402


def test_per_job_value_wins_over_env():
    assert resolve_max_video_hours({"max_video_hours": 5}, "15") == 5
    assert resolve_max_video_hours({"max_video_hours": 15}, "5") == 15


def test_old_jobs_fall_back_to_env_then_15():
    assert resolve_max_video_hours({}, "12") == 12
    assert resolve_max_video_hours(None, "12") == 12
    assert resolve_max_video_hours({}, None) == 15
    assert resolve_max_video_hours({}, "") == 15
    assert resolve_max_video_hours({}, "not-a-number") == 15


def test_junk_per_job_values_are_ignored():
    for junk in (0, -3, None, "5", True):
        assert resolve_max_video_hours({"max_video_hours": junk}, "15") == 15, junk


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
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
