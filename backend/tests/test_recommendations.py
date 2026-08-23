"""
Unit tests for the pure functions in `backend/app/services/recommendations.py`:
`build_recommendations_csv`, `merge_duplicate_stocks`, `partition_by_type`.

Mirrors the repo's existing self-running test style (see
worker/tests/test_transcript.py) — no pytest harness is wired up. We set dummy
env vars BEFORE importing so app.core.config.Settings() validates without real
credentials, and we never touch the network (the OpenAI/R2 calls live inside
generate_for_job, which we don't invoke here). Run with:

    ./venv/bin/python backend/tests/test_recommendations.py
"""

import csv
import io
import json
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

from app.services.recommendations import (  # noqa: E402
    CSV_COLUMNS,
    _parse_stocks_payload,
    _numbers,
    build_recommendations_csv,
    flag_inconsistent_cmp,
    merge_duplicate_stocks,
    partition_by_type,
    require_trade_fields,
)


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


# ---------------------------------------------------------------------------
# merge_duplicate_stocks
# ---------------------------------------------------------------------------

def test_merge_same_stock_latest_value_wins():
    merged = merge_duplicate_stocks([
        {"stock_name": "Titan", "type": "recommendation", "cmp": "2020",
         "targets": "T1: 2070"},
        {"stock_name": "Titan", "type": "recommendation", "cmp": "2025",
         "targets": "T1: 2080; T2: 2120"},
    ])
    assert len(merged) == 1
    assert merged[0]["cmp"] == "2025"
    assert merged[0]["targets"] == "T1: 2080; T2: 2120"


def test_merge_keeps_earlier_value_when_not_restated():
    merged = merge_duplicate_stocks([
        {"stock_name": "Titan", "type": "recommendation", "stoploss": "1990",
         "cmp": "2020"},
        {"stock_name": "Titan", "type": "recommendation", "stoploss": "",
         "targets": "T1: 2120"},
    ])
    assert len(merged) == 1
    assert merged[0]["stoploss"] == "1990"  # empty later value must not erase
    assert merged[0]["cmp"] == "2020"       # key absent later keeps earlier
    assert merged[0]["targets"] == "T1: 2120"


def test_merge_is_case_and_whitespace_insensitive():
    merged = merge_duplicate_stocks([
        {"stock_name": "Titan", "type": "view", "cmp": "2020"},
        {"stock_name": "  TITAN ", "type": "view", "stoploss": "1990"},
    ])
    assert len(merged) == 1
    assert merged[0]["stock_name"] == "Titan"  # first-mention spelling kept
    assert merged[0]["stoploss"] == "1990"


def test_merge_promotes_type_to_recommendation():
    # View first, recommendation later: promote, evidence follows the rec.
    a = merge_duplicate_stocks([
        {"stock_name": "Titan", "type": "view",
         "evidence": "accha lag raha hai", "cmp": "2020"},
        {"stock_name": "Titan", "type": "recommendation",
         "evidence": "le lo, SL 1990", "stoploss": "1990"},
    ])
    assert a[0]["type"] == "recommendation"
    assert a[0]["evidence"] == "le lo, SL 1990"
    assert a[0]["cmp"] == "2020"  # the view mention still fills the blank
    # Recommendation first, view later: must NOT demote; evidence stays.
    b = merge_duplicate_stocks([
        {"stock_name": "Titan", "type": "recommendation",
         "evidence": "le lo, SL 1990", "stoploss": "1990"},
        {"stock_name": "Titan", "type": "view",
         "evidence": "maine pehle bola tha", "cmp": "2030"},
    ])
    assert b[0]["type"] == "recommendation"
    assert b[0]["evidence"] == "le lo, SL 1990"
    assert b[0]["cmp"] == "2030"


def test_merge_does_not_merge_futures_variant():
    merged = merge_duplicate_stocks([
        {"stock_name": "Axis Bank", "type": "view"},
        {"stock_name": "Axis Bank June Futures", "type": "recommendation"},
    ])
    assert [m["stock_name"] for m in merged] == [
        "Axis Bank", "Axis Bank June Futures",
    ]


def test_merge_preserves_first_mention_order():
    merged = merge_duplicate_stocks([
        {"stock_name": "A", "type": "view"},
        {"stock_name": "B", "type": "view"},
        {"stock_name": "A", "type": "recommendation", "stoploss": "10"},
    ])
    assert [m["stock_name"] for m in merged] == ["A", "B"]
    assert merged[0]["type"] == "recommendation"


def test_merge_passes_through_unnamed_and_non_dict_items():
    merged = merge_duplicate_stocks([
        {"type": "view"}, {"stock_name": "", "type": "view"}, "junk", None,
    ])
    # Two empty-name dicts must NOT merge with each other; non-dicts dropped.
    assert len(merged) == 2
    assert all(isinstance(m, dict) for m in merged)


# ---------------------------------------------------------------------------
# partition_by_type
# ---------------------------------------------------------------------------

def test_partition_keeps_only_recommendations():
    recs, views, invalid = partition_by_type([
        {"stock_name": "A", "type": "recommendation"},
        {"stock_name": "B", "type": "view"},
        {"stock_name": "C", "type": "recommendation"},
    ])
    assert [r["stock_name"] for r in recs] == ["A", "C"]
    assert [v["stock_name"] for v in views] == ["B"]
    assert invalid == []


def test_partition_type_normalization():
    recs, views, invalid = partition_by_type([
        {"stock_name": "A", "type": "Recommendation"},
        {"stock_name": "B", "type": " VIEW "},
    ])
    assert len(recs) == 1 and len(views) == 1 and not invalid


def test_partition_missing_or_unknown_type_is_invalid():
    recs, views, invalid = partition_by_type([
        {"stock_name": "X"},                   # missing type
        {"stock_name": "Y", "type": "maybe"},  # unknown type
        {"stock_name": "Z", "type": None},     # non-string type
        "junk",                                # non-dict
    ])
    assert recs == [] and views == []
    assert len(invalid) == 4


def test_partition_preserves_order_and_all_fields():
    items = [
        {"stock_name": "A", "type": "recommendation",
         "evidence": "le lo, SL 90", "cmp": "100"},
        {"stock_name": "B", "type": "recommendation",
         "evidence": "short karo, target 50", "cmp": "60"},
    ]
    recs, _, _ = partition_by_type(items)
    assert recs == items  # same order, all fields (incl. evidence) intact


# ---------------------------------------------------------------------------
# require_trade_fields — completeness gate (Lenskart/Tech Mahindra rule)
# ---------------------------------------------------------------------------

def test_field_gate_buy_requires_full_setup():
    full = {"stock_name": "LIC", "action": "BUY", "cmp": "438.5",
            "stoploss": "425", "targets": "T1: 460; T2: 480"}
    targets_only = {"stock_name": "Lenskart", "action": "BUY",
                    "targets": "T1: 650; T2: 700"}  # no cmp, no SL -> view-ish
    no_cmp = {"stock_name": "Tech Mahindra", "action": "BUY",
              "stoploss": "1490", "targets": "T1: 1525"}
    kept, demoted = require_trade_fields([full, targets_only, no_cmp])
    assert [k["stock_name"] for k in kept] == ["LIC"]
    assert [d["stock_name"] for d in demoted] == ["Lenskart", "Tech Mahindra"]


def test_field_gate_sell_needs_only_one_level():
    exit_call = {"stock_name": "Paytm", "action": "SELL", "stoploss": "850"}
    short_call = {"stock_name": "Axis Bank June Futures", "action": "SELL",
                  "targets": "T1: 1050"}
    bare_sell = {"stock_name": "X", "action": "SELL"}  # no level at all
    kept, demoted = require_trade_fields([exit_call, short_call, bare_sell])
    assert [k["stock_name"] for k in kept] == ["Paytm", "Axis Bank June Futures"]
    assert [d["stock_name"] for d in demoted] == ["X"]


def test_field_gate_blank_or_missing_action_treated_as_buy():
    kept, demoted = require_trade_fields([
        {"stock_name": "A", "cmp": "100", "stoploss": "95", "targets": "110"},
        {"stock_name": "B", "targets": "110"},   # incomplete, no action given
        {"stock_name": "C", "action": "  ", "cmp": " ", "stoploss": "95",
         "targets": "110"},                       # whitespace cmp = missing
    ])
    assert [k["stock_name"] for k in kept] == ["A"]
    assert [d["stock_name"] for d in demoted] == ["B", "C"]


# ---------------------------------------------------------------------------
# _parse_stocks_payload
# ---------------------------------------------------------------------------

def test_parse_payload_stocks_and_legacy_envelope():
    items = _parse_stocks_payload('{"stocks": [{"stock_name": "A"}, "junk"]}')
    assert items == [{"stock_name": "A"}]  # non-dicts filtered
    legacy = _parse_stocks_payload('{"recommendations": [{"stock_name": "B"}]}')
    assert legacy == [{"stock_name": "B"}]


def test_parse_payload_wrong_shapes_yield_empty():
    assert _parse_stocks_payload("{}") == []
    assert _parse_stocks_payload("[]") == []          # top level not a dict
    assert _parse_stocks_payload('{"stocks": "x"}') == []  # envelope not a list


def test_parse_payload_malformed_json_raises():
    # A truncated/broken response must raise so the caller retries the call.
    broken = '{"stocks": [{"stock_name": "A", "evidence": "le lo'
    try:
        _parse_stocks_payload(broken)
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("malformed JSON did not raise JSONDecodeError")


# ---------------------------------------------------------------------------
# CSV interplay — classification fields must never leak into the CSV
# ---------------------------------------------------------------------------

def test_csv_ignores_type_and_evidence_fields():
    text = build_recommendations_csv([{
        "stock_name": "Titan", "type": "recommendation",
        "evidence": "Titan 2020 pe le lo, SL 1990", "stoploss": "1990",
    }])
    rows = _parse(text)
    assert rows[0] == CSV_COLUMNS  # header still the frozen 7 columns
    assert "recommendation" not in text.splitlines()[1]
    assert "le lo" not in text
    assert rows[1][CSV_COLUMNS.index("STOCKNAME")] == "Titan"
    assert rows[1][CSV_COLUMNS.index("STOPLOSS")] == "1990"


def test_pipeline_view_then_rec_yields_one_row():
    # Compose the real pipeline order: merge -> partition -> CSV.
    raw = [
        {"stock_name": "Titan", "type": "view",
         "evidence": "abhi 2020 chal raha hai", "cmp": "2020"},
        {"stock_name": "Titan", "type": "recommendation",
         "evidence": "le lo, SL 1990 target 2120",
         "action": "BUY", "stoploss": "1990", "targets": "T1: 2120"},
        {"stock_name": "Paytm", "type": "view",
         "evidence": "results ke baad dekhenge"},
    ]
    recs, views, invalid = partition_by_type(merge_duplicate_stocks(raw))
    rows = _parse(build_recommendations_csv(recs))
    assert len(rows) == 2  # header + exactly one row
    row = rows[1]
    assert row[CSV_COLUMNS.index("STOCKNAME")] == "Titan"
    assert row[CSV_COLUMNS.index("CMP")] == "2020"       # from the view mention
    assert row[CSV_COLUMNS.index("STOPLOSS")] == "1990"  # from the rec mention
    assert row[CSV_COLUMNS.index("TARGETS")] == "T1: 2120"
    assert [v["stock_name"] for v in views] == ["Paytm"]
    assert invalid == []


# ---------------------------------------------------------------------------
# flag_inconsistent_cmp — deterministic backstop for a cmp from another stock
# ---------------------------------------------------------------------------

def test_numbers_parses_llm_number_formats():
    assert _numbers("T1: 1065; T2: 1080") == [1065.0, 1080.0]   # "T1" is not a number
    assert _numbers("438.5") == [438.5]                          # decimal stays whole
    assert _numbers("1036-1037") == [1036.0, 1037.0]
    assert _numbers("295 296") == [295.0, 296.0]
    assert _numbers("1638, 1640") == [1638.0, 1640.0]            # comma+space = two values
    assert _numbers("1,036") == [1036.0]                         # comma between digits = separator
    assert _numbers("Rs.1905") == [1905.0]
    assert _numbers("166*") == [166.0]
    assert _numbers("") == [] and _numbers(None) == []


def test_flags_the_two_real_cross_stock_cmp_bugs():
    """Both rows actually shipped to a user; both are arithmetically impossible."""
    # "1019" was AU Small Finance Bank's stoploss, landing in Bharti Airtel's cmp.
    airtel = {"stock_name": "Bharti Airtel", "action": "BUY", "cmp": "1019",
              "stoploss": "1890", "targets": "T1: 1935; T2: 1950"}
    # "375 376" was a later stock's price, landing in AU Small Finance Bank's cmp.
    au = {"stock_name": "AU Small Finance Bank", "action": "BUY", "cmp": "375 376",
          "stoploss": "1019", "targets": "T1: 1065; T2: 1080"}
    items, flagged = flag_inconsistent_cmp([airtel, au])
    assert [f["stock_name"] for f in flagged] == ["Bharti Airtel", "AU Small Finance Bank"]
    assert [i["cmp"] for i in items] == ["1019*", "375 376*"]     # marked, not dropped
    assert len(items) == 2


def test_no_false_positives_on_every_verified_correct_row():
    """The 14 correct rows observed across the three test videos must all pass."""
    good = [
        ("Titan", "4586", "4530", "T1: 4700; T2: 4750"),
        ("Eternal", "295-296", "283", "T1: 320; T2: 330"),
        ("Sun Pharma", "1923-1924", "1905", "T1: 1960; T2: 1985"),
        ("Bharti Airtel", "1905", "1890", "T1: 1935; T2: 1950"),
        ("AU Small Finance Bank", "1036-1037", "1019", "T1: 1065; T2: 1080"),
        ("PB Fintech", "1638 1640", "1620", "T1: 1670; T2: 1685"),
        ("APL Apollo Tubes", "1816", "1805", "T1: 1855; T2: 1870"),
        ("LIC", "438.5", "425", "T1: 460; T2: 480"),
        ("Motilal Oswal", "942", "920", "T1: 975; T2: 990"),
        ("Bandhan Bank", "203.3", "198", "T1: 211; T2: 215"),
        ("Adani Green Energy", "1520", "1500", "T1: 1560; T2: 1590"),
        ("Sobha Limited", "1485", "1430", "T1: 1700; T2: 1750"),
    ]
    recs = [{"stock_name": n, "action": "BUY", "cmp": c, "stoploss": s, "targets": t}
            for n, c, s, t in good]
    items, flagged = flag_inconsistent_cmp(recs)
    assert flagged == [], f"false positives: {[f['stock_name'] for f in flagged]}"
    assert [i["cmp"] for i in items] == [c for _, c, _, _ in good]  # untouched


def test_sell_direction_is_inverted_not_flagged():
    """A short: stoploss ABOVE the price, targets BELOW. Must not be flagged."""
    sell = {"stock_name": "Axis Bank June Futures", "action": "SELL",
            "cmp": "1000", "stoploss": "1030", "targets": "T1: 960; T2: 940"}
    items, flagged = flag_inconsistent_cmp([sell])
    assert flagged == [] and items[0]["cmp"] == "1000"
    # ...but the same numbers labelled BUY are impossible.
    _, flagged_buy = flag_inconsistent_cmp([{**sell, "action": "BUY"}])
    assert len(flagged_buy) == 1


def test_incomparable_rows_are_left_alone():
    """Missing any of the three fields = nothing to compare = no flag, no change."""
    rows = [
        {"stock_name": "A", "action": "BUY", "cmp": "", "stoploss": "10", "targets": "T1: 20"},
        {"stock_name": "B", "action": "BUY", "cmp": "15", "stoploss": "", "targets": "T1: 20"},
        {"stock_name": "C", "action": "SELL", "cmp": "15", "stoploss": "20", "targets": ""},
    ]
    items, flagged = flag_inconsistent_cmp(rows)
    assert flagged == []
    assert items == rows


def test_existing_asterisk_is_not_doubled():
    row = {"stock_name": "X", "action": "BUY", "cmp": "375*",
           "stoploss": "1019", "targets": "T1: 1065"}
    items, flagged = flag_inconsistent_cmp([row])
    assert len(flagged) == 1
    assert items[0]["cmp"] == "375*"


def test_flagging_does_not_mutate_the_caller_dict():
    row = {"stock_name": "X", "action": "BUY", "cmp": "375",
           "stoploss": "1019", "targets": "T1: 1065"}
    flag_inconsistent_cmp([row])
    assert row["cmp"] == "375"


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
