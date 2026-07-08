"""
Unit tests for the pure CSV serializer in
`backend/app/services/recommendations.py::build_recommendations_csv`.

Mirrors the repo's existing self-running test style (see
worker/tests/test_transcript.py) — no pytest harness is wired up. We set dummy
env vars BEFORE importing so app.core.config.Settings() validates without real
credentials, and we never touch the network (the OpenAI/R2 calls live inside
generate_for_job, which we don't invoke here). Run with:

    ./venv/bin/python backend/tests/test_recommendations.py
"""

import csv
import io
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(ROOT))      # for `shared`
sys.path.insert(0, str(BACKEND))   # for `app`

# Hermetic config: satisfy the required Settings fields with dummies (only if
# not already set), so importing the service doesn't need backend/.env.
for var in (
    "MONGO_URL", "DB_NAME", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET_NAME", "R2_ENDPOINT_URL",
):
    os.environ.setdefault(var, "test")

from app.services.recommendations import build_recommendations_csv, CSV_COLUMNS  # noqa: E402


def _parse(csv_text):
    return list(csv.reader(io.StringIO(csv_text)))


def test_header_columns_and_order():
    rows = _parse(build_recommendations_csv([]))
    assert rows == [
        ["DATE", "STOCKNAME", "BUY/SELL", "CMP", "STOPLOSS", "TARGETS", "REASONING"]
    ]
    assert rows[0] == CSV_COLUMNS


def test_empty_list_is_header_only():
    rows = _parse(build_recommendations_csv([]))
    assert len(rows) == 1  # just the header


def test_full_row_mapping():
    items = [{
        "date": "2026-06-20",
        "stock_name": "RELIANCE",
        "action": "BUY",
        "cmp": "2900",
        "stoploss": "2820",
        "targets": "T1: 3000; T2: 3120",
        "reasoning": "Breakout above resistance with volume.",
    }]
    rows = _parse(build_recommendations_csv(items))
    assert rows[1] == [
        "2026-06-20", "RELIANCE", "BUY", "2900", "2820",
        "T1: 3000; T2: 3120", "Breakout above resistance with volume.",
    ]


def test_missing_fields_become_empty_no_fabrication():
    # Only stock_name present — every other column must be "".
    rows = _parse(build_recommendations_csv([{"stock_name": "TCS"}]))
    assert rows[1] == ["", "TCS", "", "", "", "", ""]


def test_none_values_become_empty():
    rows = _parse(build_recommendations_csv([{
        "date": None, "stock_name": "INFY", "action": None, "cmp": None,
        "stoploss": None, "targets": None, "reasoning": None,
    }]))
    assert rows[1] == ["", "INFY", "", "", "", "", ""]


def test_buy_sell_action_column():
    rows = _parse(build_recommendations_csv([
        {"stock_name": "RELIANCE", "action": "BUY"},
        {"stock_name": "Axis Bank June Futures", "action": "SELL"},
    ]))
    idx = CSV_COLUMNS.index("BUY/SELL")
    assert rows[1][idx] == "BUY"
    assert rows[2][idx] == "SELL"


def test_sell_futures_name_preserved():
    # A SELL call named as a futures contract must keep its full name.
    rows = _parse(build_recommendations_csv([
        {"stock_name": "Axis Bank June Futures", "action": "SELL"},
    ]))
    assert rows[1][CSV_COLUMNS.index("STOCKNAME")] == "Axis Bank June Futures"
    assert rows[1][CSV_COLUMNS.index("BUY/SELL")] == "SELL"


def test_commas_quotes_newlines_are_escaped_and_roundtrip():
    tricky = 'Buy on dip; target "high", then\nhold long term'
    items = [{"stock_name": "HDFC", "reasoning": tricky}]
    text = build_recommendations_csv(items)
    rows = _parse(text)  # csv.reader must reconstruct the field exactly
    assert rows[1][CSV_COLUMNS.index("STOCKNAME")] == "HDFC"
    assert rows[1][CSV_COLUMNS.index("REASONING")] == tricky


def test_targets_single_column_preserved():
    rows = _parse(build_recommendations_csv([{
        "stock_name": "WIPRO", "targets": "T1: 250; T2: 270",
    }]))
    assert rows[1][CSV_COLUMNS.index("TARGETS")] == "T1: 250; T2: 270"


def test_uncertainty_asterisk_rides_inline():
    # The LLM appends "*" to numbers it isn't sure it heard correctly. The
    # asterisk must survive the CSV builder intact (it's not a CSV special char,
    # so no quoting) and .strip() must not eat it.
    rows = _parse(build_recommendations_csv([{
        "stock_name": "Bharat Forge", "action": "BUY", "cmp": "2020",
        "stoploss": "1990", "targets": "T1: 2070; T2: 2120*",
    }]))
    assert rows[1][CSV_COLUMNS.index("TARGETS")] == "T1: 2070; T2: 2120*"
    # A whole flagged value round-trips too.
    rows2 = _parse(build_recommendations_csv([{"stock_name": "X", "cmp": "2050*"}]))
    assert rows2[1][CSV_COLUMNS.index("CMP")] == "2050*"


def test_non_dict_items_skipped():
    rows = _parse(build_recommendations_csv([
        {"stock_name": "A"}, "garbage", None, 42, {"stock_name": "B"},
    ]))
    names = [r[1] for r in rows[1:]]
    assert names == ["A", "B"]


def test_values_are_trimmed():
    rows = _parse(build_recommendations_csv([{"stock_name": "  SBIN  ", "cmp": " 600 "}]))
    assert rows[1][CSV_COLUMNS.index("STOCKNAME")] == "SBIN"
    assert rows[1][CSV_COLUMNS.index("CMP")] == "600"


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
