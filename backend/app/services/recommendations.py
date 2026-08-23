"""
Stock-recommendations feature.

Given a job's final video (`final.mp4` in R2), call a multimodal LLM (Gemini) to
transcribe the speaker's Hinglish audio and extract the stock recommendations they
made, producing a downloadable CSV (`recommendations.csv` in R2). The job's own
status is never touched — this is an independent sub-resource that runs after the
job is DONE.

Design:
  * The LLM returns STRUCTURED JSON, not raw CSV. We build the CSV ourselves with
    Python's `csv` module so escaping (commas/quotes/newlines in REASONING) is
    deterministic, and so the same JSON shape can fuel Phase-2 (prompt-driven
    insights) later.
  * Pass 2 CLASSIFIES every named stock as "recommendation" or "view" (with a
    verbatim `evidence` quote); Python merges duplicate stocks and keeps only
    the recommendations that carry a complete trade setup (see
    `require_trade_fields`) — views/incomplete rows are logged and dropped,
    and `evidence` never reaches the CSV.
  * `build_recommendations_csv`, `merge_duplicate_stocks` and `partition_by_type`
    are pure functions (unit-tested). `generate_for_job` is the async background
    task that does the I/O.
  * Best-effort: any failure records recommendations.status=FAILED with a message;
    it never raises into the caller or affects the video/transcript.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import random
import re
import tempfile
import time
from typing import Any

import anyio

from app.core.config import settings
from app.services import job_service
from app.services.storage import get_storage
from shared.constants import (
    r2_key_final_audio,
    r2_key_final_video,
    r2_key_recommendations,
)

logger = logging.getLogger(__name__)

# CSV column order is the contract with the user — keep stable.
CSV_COLUMNS = ["DATE", "STOCKNAME", "BUY/SELL", "CMP", "STOPLOSS", "TARGETS", "REASONING"]

# Maps a CSV column -> JSON item key. The LLM is asked to use these keys.
_FIELD_BY_COLUMN = {
    "DATE": "date",
    "STOCKNAME": "stock_name",
    "BUY/SELL": "action",
    "CMP": "cmp",
    "STOPLOSS": "stoploss",
    "TARGETS": "targets",
    "REASONING": "reasoning",
}

# Gemini File API: video uploads go PROCESSING -> ACTIVE before they can be used.
# Poll with a generous ceiling (processing time scales with clip length).
_GEMINI_FILE_ACTIVE_TIMEOUT_S = 300
_GEMINI_FILE_POLL_INTERVAL_S = 2

# Pass-2 retry budget. The typed response_schema makes malformed JSON
# near-impossible by construction, but a response cut off mid-generation still
# parses as broken JSON — retry just the cheap text call (the upload and the
# transcript are reused), never the whole pipeline.
_EXTRACT_JSON_ATTEMPTS = 3

# Transient-failure retry budget for the generate_content calls themselves.
#
# Gemini returns 503 UNAVAILABLE ("high demand") when its serving capacity can't
# accept a request right now. This is NOT a model outage and NOT a bug here: a
# trivial prompt succeeds against the same model in the same second while a
# heavy transcription request is shed. Measured 2026-08-14: gemini-3.6-flash
# answered a "Say OK" probe 3/3 in ~2s, then 503'd 12s into the real request.
#
# These spikes clear in seconds, so waiting and re-asking is the whole cure.
# Delays are 2s, 4s, 8s, 16s plus up to 50% jitter (~30-45s of patience total).
# Jitter matters because several jobs shed at the same instant would otherwise
# all retry on the same tick and collide again.
_LLM_RETRY_ATTEMPTS = 5
_LLM_RETRY_BASE_S = 2.0

# HTTP codes worth retrying: 429 = rate limited, 500 = transient server error,
# 503 = capacity. Anything else (400 bad request, 403 permission, 404 unknown
# model) is a real fault that retrying would only delay.
_RETRYABLE_STATUS = (429, 500, 503)

# Item key order doubles as the schema's property_ordering: evidence comes
# BEFORE type so the model quotes the transcript before it classifies.
_STOCK_ITEM_FIELDS = [
    "stock_name", "evidence", "type", "action",
    "cmp", "stoploss", "targets", "reasoning", "date",
]

TRANSCRIBE_SYSTEM_PROMPT = """\
You transcribe a Hinglish (mixed Hindi and English) audio/video of stock-market \
talk. Produce a faithful, VERBATIM transcript — do NOT summarise or translate.

Rules:
- Write what is said, in the order it is said. Add nothing that is not spoken.
- SCRIPT: write everything in the Latin/Roman alphabet — romanise Hindi words, and \
keep company/stock names and English words in their standard English spelling \
(e.g. "Bharat Forge", "Kirloskar Brothers"). Do NOT output Devanagari.
- NUMBERS: write every number digit-for-digit exactly as spoken. Indian speakers \
mix Hindi/English and may say digits in groups (e.g. "twenty-one twenty" / \
"इक्कीस सौ बीस" = 2120). Never round, merge, split or "tidy" a number, and never \
add decimals that were not spoken.
- CUT-OFF / UNCLEAR: if a word or number is interrupted, cut off, or you cannot \
make out all of it, write the part you DO hear immediately followed by the literal \
marker [CUT OFF] — e.g. "...stop loss is 166[CUT OFF]". Do NOT complete or guess \
the missing part from context or from your own market knowledge.
- Start a new line at each change of speaker, prefixed "SPEAKER: ".
- Use ONLY what is audible. NEVER insert a company/stock name, price or fact that \
is not actually spoken — even when the surrounding talk would let you guess it \
(e.g. do not name a stock merely because its price levels are mentioned).

Output plain text only (no JSON, no commentary)."""

EXTRACT_SYSTEM_PROMPT = """\
You are given a TRANSCRIPT of a single stock analyst speaking (Hinglish). For \
EVERY stock the analyst NAMES and discusses, decide whether he makes an actual \
RECOMMENDATION or merely shares a VIEW, and extract the fields below. Work \
solely from the transcript — do NOT use outside market knowledge, and rely on \
nothing that is not written in it.

Return ONLY a JSON object of this exact shape:
{"stocks": [
  {"stock_name": "", "evidence": "", "type": "", "action": "", "cmp": "", "stoploss": "", "targets": "", "reasoning": "", "date": ""}
]}

TYPE — the most important field. Set "type" to exactly "recommendation" or "view".
- "recommendation" ONLY if BOTH are true in the transcript:
  (a) the analyst gives a clear instruction to act on THAT stock — buy / sell / \
short / exit / book profit (e.g. "le lo", "kharid lo", "short karo", "exit kar \
jao", "book kar lo", "SL ... kar do"), AND
  (b) he states at least ONE concrete trade level for it: a stoploss OR a \
target. The current market price (CMP) alone does NOT count as a trade level.
- Everything else is a "view": an opinion, chart talk, "accha lag raha hai", \
answering a viewer's question without giving levels, or an instruction with no \
stoploss/target.
- If you are unsure whether it is a recommendation, set "view". NEVER default \
to "recommendation".
- PAST calls: recapping an earlier call ("maine pichhle hafte Titan bola tha, \
target hit ho gaya") is a "view" — UNLESS he gives fresh actionable guidance \
NOW ("abhi bhi le sakte ho, SL 950 rakho" / "ab SL trail karke 980 kar do"): \
then it is a "recommendation" using ONLY the levels he states now.
- Telling existing holders of a stock to exit or book with a level ("jinke paas \
Paytm hai, 850 ke stop loss ke saath exit kar jao") IS a "recommendation", \
action "SELL".
Examples:
  "Titan 2020 pe le lo, stop loss 1990, target 2120" -> recommendation
  "Titan bahut accha lag raha hai, results strong the" -> view (no instruction, no level)
  "Titan abhi 2020 chal raha hai" -> view (CMP alone is not a trade level)
  "Titan le sakte ho" with no stoploss/target given anywhere -> view (no level)

- "evidence": the shortest verbatim transcript snippet(s) (max ~25 words, \
Hinglish exactly as written) that prove the "type" — for a recommendation, the \
words carrying the instruction and the level; for a view, the words showing he \
only discussed it. Copy, never paraphrase.

- One object per NAMED stock. The name MUST appear in the transcript. The \
transcript often has price levels/targets/chart talk with NO stock name \
attached (the analyst commenting on someone else's call) — emit NO object for \
those, not even as a "view". If the analyst names no stocks at all, return \
{"stocks": []}.

- SPLICED-IN OTHER SPEAKER. The transcript is a CUT of ONE speaker's segments \
lifted out of a longer multi-speaker show, so at a cut seam a few seconds of \
ANOTHER speaker can leak in. Tell the two apart by where the stock's name sits \
relative to his own talk about it. When THIS analyst makes a call he leads into \
the stock: he names it FIRST and then gives the levels, or names it MID-FLOW \
while giving them — and either way he keeps talking about that stock \
afterwards (reasoning, chart, follow-up). Leaked speech has no such shape: it \
appears abruptly at the very END of the transcript, usually as its own last \
line, the stock name arrives with none of this analyst's build-up before it and \
nothing of his after it, and it often stops mid-sentence. Classify such a stock \
as "view" so it is dropped — even when that trailing fragment does carry an \
instruction and levels, because those levels are the other speaker's call, not \
his.
  This does NOT apply just because a stock is the last one discussed. If the \
analyst genuinely works up to a closing call — transitions into it, gives \
CMP/levels, explains it — that is a "recommendation" like any other. Only a \
bare, lead-in-less trailing fragment is a splice.

- If the SAME stock is discussed more than once, emit ONE object for it: for \
each field use the LATEST value he states, and keep an earlier value where it \
is not restated. Its "type" is "recommendation" if ANY of the mentions \
qualifies as one.

- "stock_name": copy the analyst's OWN name for the instrument from the \
transcript, preserving any futures/month/derivative qualifier as written — \
e.g. keep "Axis Bank June Futures", do NOT shorten it to "Axis Bank"; never \
ADD a qualifier the transcript lacks. Output it in its standard English \
spelling (e.g. "Bharat Forge"). NEVER infer, guess or supply a name from price \
levels, chart levels, ticker prices, or your own market knowledge — if the \
transcript has no name for a set of levels, DROP it (per the rule above).

- "action" (recommendations only — leave "" for a view) must be exactly "BUY" \
or "SELL":
    * Use "SELL" when the recommendation is to sell, short, exit or book the \
stock; or the instrument is a futures/derivatives contract in a bearish \
context (e.g. "Axis Bank June Futures"). Weigh the overall context, not just \
keywords.
    * Otherwise use "BUY". If a confirmed recommendation is genuinely unclear \
between buy and sell, choose "BUY".

FIELD MEANINGS — the analyst states these in any order; match by meaning, not \
position:
- "cmp": the price he gives for the stock as its current price ("abhi 2020 chal \
raha hai", "CMP is 2020"). If he states no live price but DOES give a recent \
traded price for THAT stock — a previous/yesterday's close ("kal 4586 ke aas-paas \
closing hui"), or the level it is trading "around" — use that. Prefer the live \
price when both are given, and keep a range as a range ("942-943"). That is the \
only latitude in this field: the number must be one he states as a PRICE OF THAT \
STOCK. NEVER take it from a different stock, and never from a stoploss, target, \
support/resistance or other chart level. If he gives no price for the stock at \
all, leave "" — a genuine recommendation can lack a CMP.
  SANITY-CHECK THE PRICE AGAINST ITS OWN LEVELS. A trade setup has to hold \
together: for a BUY the stoploss sits BELOW the current price and the targets \
ABOVE it; for a SELL/short the stoploss sits ABOVE and the targets BELOW. Use \
this ONLY to choose between numbers the speaker actually stated for THAT stock: \
when more than one candidate price appears and you are unsure which is the CMP, \
discard any candidate that makes the setup impossible — a "price" of 375 \
alongside a stoploss of 1019 and targets of 1065/1080 cannot be that stock's \
price — and keep the one that fits. NEVER use this to calculate, adjust, round \
or invent a number, and never to complete a "[CUT OFF]" value: the arithmetic \
only ever REJECTS a candidate, it never produces one. Do not look up or recall \
any real-world price. If no stated candidate fits, leave "cmp" as ""; if one \
fits but you remain unsure, keep it and append "*".
- "stoploss": the level at which he says to exit if the trade goes against you \
("stop loss", "SL", "950 ka stoploss rakho").
- "targets": the level(s) he expects the price to reach or says to book profit \
at. Put first and second targets together in the single "targets" field, e.g. \
"T1: 1500; T2: 1600". If only one target is given, just include that one.

- "reasoning" (recommendations only — leave "" for a view): summarise the \
analyst's rationale for THAT call in clear, concise ENGLISH (translate it from \
the Hinglish transcript — do NOT leave it in Hindi). Cover the technical/\
fundamental points he actually makes — chart levels, breakout/breakdown, \
support/resistance, trend, volume, results/earnings, news or catalysts, sector \
view, risk-reward — faithfully; do not pad and do NOT invent anything not in \
the transcript. If no reason is given, leave it "".

- Derive "date" from the video title ONLY if a date appears there; otherwise use "".

- NEVER fabricate or guess values. If the speaker did not state a field, leave it "".

NUMBERS (copy them from the transcript — do not re-derive):
- Copy each number EXACTLY as written in the transcript. Do NOT add decimal places \
or precision the transcript lacks ("2020", never "2020.50"); keep a range as a range \
("942-943"); do NOT drop or merge digits.
- If a number in the transcript carries a "[CUT OFF]" marker, or is otherwise \
incomplete, output only the digits present and append "*" (e.g. "166[CUT OFF]" -> \
"166*"), or leave the field "" if no digits are present. NEVER complete, pad or \
guess the missing digits — not from plausibility, not from your own market \
knowledge. (A value that looks implausibly small beside the others is usually cut \
off: flag it, do not "fix" it.)
- FLAG uncertainty: append a single trailing "*" to ANY number you are not confident \
the transcript states correctly, and ONLY to that number (e.g. "cmp": "2050*", \
"targets": "T1: 2120*; T2: 2200"). Confident numbers get none; the asterisk is the \
only signal — add no other commentary.
"""

# Gemini (especially -flash) follows the IMMEDIATE user turn more reliably than a
# long system prompt, so the highest-stakes rules are RESTATED tersely there too.
TRANSCRIBE_USER_DIRECTIVE = (
    "Transcribe this recording now, verbatim. Mark any cut-off or unclear number as "
    'e.g. "166[CUT OFF]", and NEVER guess the missing digits or insert a stock name '
    "that was not actually spoken."
)

EXTRACT_USER_DIRECTIVE = (
    "Classify and extract every NAMED stock from the transcript now. HARD RULES: "
    '(1) "type" is "recommendation" ONLY when the speaker gives a clear '
    "buy/sell/short/exit instruction for that stock AND states a stoploss or "
    "target for it — CMP alone is not enough; anything less, or any doubt, is "
    '"view". (2) include a stock ONLY if its name appears verbatim in the '
    "transcript — never infer a name from price levels or from your own "
    "knowledge; (3) copy numbers exactly as written, with no invented decimals; "
    '(4) never complete a number marked "[CUT OFF]" — output the digits shown + '
    '"*" (e.g. "166*") or leave it blank; (5) same stock discussed more than '
    "once -> ONE object with the latest stated values; (6) a stock whose ONLY "
    "mention is an abrupt fragment at the very END of the transcript, with no "
    "lead-in before it and nothing after it, is another speaker spliced in at a "
    'cut seam -> "view", even if that fragment carries levels — but a closing '
    "call the speaker genuinely builds up to stays a "
    '"recommendation"; (7) "cmp" may be a previous close or an "around" price '
    "he states for THAT stock when he gives no live price — but it must never "
    "be a number belonging to a different stock, nor a stoploss/target/chart "
    "level; if two candidate prices appear, drop the one that makes the setup "
    "impossible (BUY: stoploss below the price, targets above; SELL: the "
    "reverse) — that check only ever REJECTS a candidate, it never invents or "
    'recalculates one. Every object MUST have "type" and a short verbatim '
    '"evidence" quote.'
)


def build_recommendations_csv(items: list[dict]) -> str:
    """
    Serialise a list of recommendation objects to a CSV string with the fixed
    7-column header. Tolerant by design: any missing field becomes "" so we
    never drop a row the LLM returned (recall over precision). Empty list ->
    a header-only CSV.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for item in items or []:
        if not isinstance(item, dict):
            continue
        row = []
        for col in CSV_COLUMNS:
            val = item.get(_FIELD_BY_COLUMN[col], "")
            if val is None:
                val = ""
            row.append(str(val).strip())
        writer.writerow(row)
    return buf.getvalue()


def _parse_stocks_payload(content: str) -> list[dict]:
    """
    Parse pass 2's JSON text into the list of classified stock items.
    Tolerates the pre-rename "recommendations" envelope key and filters
    non-dicts. Raises json.JSONDecodeError on malformed JSON — the caller
    retries the LLM call.
    """
    data = json.loads(content)
    raw = []
    if isinstance(data, dict):
        raw = data.get("stocks") or data.get("recommendations") or []
    if not isinstance(raw, list):
        raw = []
    return [i for i in raw if isinstance(i, dict)]


def _build_extract_schema(types_mod):
    """
    Typed response schema for pass 2 (server-side constrained decoding):
    the API can only emit JSON matching this shape, so unescaped quotes in
    Hinglish `evidence` strings or a missing comma can no longer produce
    unparseable output. "type" is a real enum, and stock_name/evidence/type
    are required. Built lazily because google.genai is imported lazily.
    """
    string = types_mod.Schema(type=types_mod.Type.STRING)
    item = types_mod.Schema(
        type=types_mod.Type.OBJECT,
        properties={
            **{name: string for name in _STOCK_ITEM_FIELDS if name != "type"},
            "type": types_mod.Schema(
                type=types_mod.Type.STRING, enum=["recommendation", "view"]
            ),
        },
        required=["stock_name", "evidence", "type"],
        property_ordering=list(_STOCK_ITEM_FIELDS),
    )
    return types_mod.Schema(
        type=types_mod.Type.OBJECT,
        properties={
            "stocks": types_mod.Schema(type=types_mod.Type.ARRAY, items=item)
        },
        required=["stocks"],
    )


def _normalized_type(value) -> str:
    """Normalise a "type" value for comparison; non-strings normalise to ""."""
    return value.strip().casefold() if isinstance(value, str) else ""


def _merge_item_into(prev: dict, later: dict) -> None:
    """
    Fold a later mention of the same stock into `prev` (in place). Later
    non-empty values win per field; an empty later value never erases an
    earlier one ("latest stated value; unrestated fields keep the earlier
    one"). Exceptions: `stock_name` keeps the first-mention spelling, and
    `type` + `evidence` move as a PAIR — if exactly one mention is a
    recommendation, its type AND evidence win regardless of order (a later
    recap must not demote an earlier recommendation, and the surviving
    evidence must justify the surviving type).
    """
    prev_is_rec = _normalized_type(prev.get("type")) == "recommendation"
    later_is_rec = _normalized_type(later.get("type")) == "recommendation"
    for key, val in later.items():
        if key in ("stock_name", "type", "evidence"):
            continue
        if val is not None and str(val).strip():
            prev[key] = val
        else:
            prev.setdefault(key, val)
    if later_is_rec and not prev_is_rec:
        prev["type"] = later.get("type")
        prev["evidence"] = later.get("evidence")
    elif prev_is_rec == later_is_rec:
        # Same classification on both sides: last non-empty wins per field.
        for key in ("type", "evidence"):
            val = later.get(key)
            if val is not None and str(val).strip():
                prev[key] = val
    # else: prev is the recommendation and later is not — keep prev's pair.


def merge_duplicate_stocks(items: list[dict]) -> list[dict]:
    """
    Backstop for the prompt's one-object-per-stock rule: if pass 2 still emits
    the same stock more than once, collapse the duplicates into one item.
    Matching is exact stock_name only, case/whitespace-insensitive —
    deliberately NO fuzzy matching, so "Axis Bank" never merges into
    "Axis Bank June Futures" (the qualifier is load-bearing). Items without a
    stock_name pass through unmerged; non-dicts are dropped. First-occurrence
    order is preserved. Pure function; input items are not mutated.
    """
    out: list[dict] = []
    by_key: dict[str, dict] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        copy = dict(item)
        key = " ".join(str(item.get("stock_name") or "").split()).casefold()
        if not key:
            out.append(copy)
            continue
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = copy
            out.append(copy)
        else:
            _merge_item_into(prev, copy)
    return out


def require_trade_fields(recs: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Deterministic completeness gate on recommendation-typed items (user rule,
    2026-07-19: a genuine call carries the full trade setup; chart-talk with
    only targets is a view). BUY rows must have cmp AND stoploss AND targets;
    SELL rows only need one of stoploss/targets, because exit/book-profit
    advice to holders ("850 ke SL ke saath exit") legitimately omits cmp and
    targets yet is a locked-in recommendation. Returns (kept, demoted);
    demoted items are dropped and logged like views.
    """

    def _has(item: dict, key: str) -> bool:
        return bool(str(item.get(key) or "").strip())

    kept: list[dict] = []
    demoted: list[dict] = []
    for item in recs:
        action = str(item.get("action") or "").strip().upper()
        if action == "SELL":
            ok = _has(item, "stoploss") or _has(item, "targets")
        else:  # BUY (or unspecified, treated as BUY): full setup required.
            ok = _has(item, "cmp") and _has(item, "stoploss") and _has(item, "targets")
        (kept if ok else demoted).append(item)
    return kept, demoted


# Numbers as the LLM writes them: "1036-1037", "295 296", "T1: 1065; T2: 1080",
# "438.5", "Rs.1905", "166*". The lookbehind is what keeps the "1" out of a "T1:"
# label while still allowing a number after "." or "-" or a currency symbol, and
# the decimal is consumed in one token so "438.5" is 438.5 and not [438, 5].
_NUM_RE = re.compile(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?")


def _numbers(value: Any) -> list[float]:
    """
    Every number in one field, in order. Thousands separators are stripped only
    when the comma sits BETWEEN digits ("1,036" -> 1036), so a comma used to
    separate two values ("1638, 1640") still yields both.
    """
    text = re.sub(r"(?<=\d),(?=\d)", "", str(value or ""))
    return [float(m.group()) for m in _NUM_RE.finditer(text)]


def flag_inconsistent_cmp(recs: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Deterministic backstop for a CMP that contradicts its own trade levels.

    A setup has to hold together: a BUY's stoploss sits below the price and its
    targets above it; a SELL/short is the reverse. A row that violates that is
    self-contradictory — you cannot buy at 375 with the stop at 1019 — and in
    every real instance so far the offending number came from a DIFFERENT stock
    ("1019" was AU Small Finance Bank's stoploss landing in Bharti Airtel's cmp;
    "375 376" was a later stock's price landing in AU's). The prompt asks the
    model to reject such a candidate itself; this catches the runs where it
    doesn't, which a prompt rule alone cannot guarantee.

    NOTHING IS DROPPED. A wrong number is recoverable from the video, a missing
    row is not, so the row is kept and its cmp gets the "*" uncertainty marker
    the prompt already defines. We cannot know WHICH of the three numbers is the
    bad one, so the marker goes on cmp — the field this check exists to protect,
    and the only one whose definition is loose enough to pull in a stray price —
    and the whole row is logged for inspection.

    Deliberately conservative, to keep false positives at zero: a row missing any
    of the three fields is left alone (nothing to compare), and the target bound
    uses the FURTHEST target so a T1 quoted just the wrong side of the price does
    not trip it. Verified against all 14 correct rows seen across the three test
    videos — none flagged — while catching both real bad rows.

    Returns (items, flagged); `items` always has the same length as `recs`.
    """

    def _mark(item: dict) -> dict:
        cmp_text = str(item.get("cmp") or "").strip()
        if not cmp_text.endswith("*"):
            item = {**item, "cmp": f"{cmp_text}*"}
        return item

    items: list[dict] = []
    flagged: list[dict] = []
    for item in recs:
        cmps = _numbers(item.get("cmp"))
        sls = _numbers(item.get("stoploss"))
        tgs = _numbers(item.get("targets"))
        if not (cmps and sls and tgs):
            items.append(item)
            continue
        cmp_lo, cmp_hi = min(cmps), max(cmps)
        if str(item.get("action") or "").strip().upper() == "SELL":
            bad = cmp_hi >= min(sls) or cmp_lo <= min(tgs)
        else:  # BUY (or unspecified, treated as BUY, matching require_trade_fields)
            bad = cmp_lo <= max(sls) or cmp_hi >= max(tgs)
        if bad:
            flagged.append(item)
            items.append(_mark(item))
        else:
            items.append(item)
    return items, flagged


def partition_by_type(items: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Split classified items into (recommendations, views, invalid). Only
    "recommendation"-typed items reach the CSV; "view" is the explicit drop
    bucket; anything else — missing/unknown/non-string type, non-dict item —
    lands in invalid and is treated as a view downstream (safe-drop: the CSV
    must contain only sure recommendations). Order and fields are preserved.
    """
    recs: list[dict] = []
    views: list[dict] = []
    invalid: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            invalid.append(item)
            continue
        kind = _normalized_type(item.get("type"))
        if kind == "recommendation":
            recs.append(item)
        elif kind == "view":
            views.append(item)
        else:
            invalid.append(item)
    return recs, views, invalid


def _is_daily_quota(exc: Exception) -> bool:
    """
    True for a 429 that means "you have spent today's free-tier allowance",
    as opposed to a short per-minute rate limit.

    These are NOT worth retrying: the free tier allows only 20 generate_content
    calls per day per model (quotaId GenerateRequestsPerDayPerProjectPerModel-
    FreeTier), and that window resets in hours, not seconds. Backing off against
    it just delays an inevitable failure — and Google still advertises a short
    `retryDelay` on these, so trusting that field alone would be misleading.
    """
    text = str(exc)
    return "PerDay" in text or "free_tier_requests" in text


def _is_transient(exc: Exception) -> bool:
    """
    True when `exc` is a capacity/rate blip worth re-asking about.

    google-genai surfaces the HTTP status on `.code`, but that isn't guaranteed
    across SDK versions, so fall back to matching the status text that always
    appears in the message (e.g. "503 UNAVAILABLE. {...}").

    A daily-quota 429 is deliberately excluded — see _is_daily_quota.
    """
    if _is_daily_quota(exc):
        return False
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code in _RETRYABLE_STATUS
    text = str(exc)
    return any(str(s) in text for s in _RETRYABLE_STATUS) or "UNAVAILABLE" in text


def _thinking_off(types) -> Any | None:
    """
    ThinkingConfig that turns the model's thinking down as far as it allows, or
    None for a model family that takes no thinking config at all (in which case
    the field must be omitted rather than sent as None-with-a-value).

    PASS 1 ONLY. Pass 2 needs its thinking — see the note at the call site.

    The knob is family-specific and NOT interchangeable: gemini-3 takes
    `thinking_level` and rejects a numeric budget, while gemini-2.5-flash (the
    config default / rollback baseline) takes `thinking_budget=0` and rejects
    `thinking_level`. Sending the wrong one is a 400, so it is chosen by prefix
    and anything unrecognised gets no thinking config — the model's own default,
    i.e. exactly today's behavior.

    `types` is passed in because google.genai is imported lazily (see the caller).
    """
    model = settings.gemini_model
    if model.startswith("gemini-3"):
        return types.ThinkingConfig(thinking_level="low")
    if model.startswith("gemini-2.5"):
        return types.ThinkingConfig(thinking_budget=0)
    return None


async def _generate_with_retry(client, *, model, contents, config, what: str):
    """
    Call generate_content, retrying transient failures with exponential backoff
    and jitter.

    MUST be called while the uploaded Gemini file is still alive, so a retry
    re-sends only the (cheap) inference request. Retrying any further out would
    repeat the R2 download and the file upload — minutes of work — for a hiccup
    that clears in seconds.
    """
    for attempt in range(1, _LLM_RETRY_ATTEMPTS + 1):
        try:
            return await client.aio.models.generate_content(
                model=model, contents=contents, config=config,
            )
        except Exception as exc:  # noqa: BLE001 — re-raised unless transient.
            if attempt == _LLM_RETRY_ATTEMPTS or not _is_transient(exc):
                raise
            base = _LLM_RETRY_BASE_S * (2 ** (attempt - 1))
            delay = base + random.uniform(0, base * 0.5)
            logger.warning(
                "recommendations: %s attempt %d/%d failed transiently (%s); "
                "retrying in %.1fs",
                what, attempt, _LLM_RETRY_ATTEMPTS, exc, delay,
            )
            await anyio.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


async def _extract_recommendations(
    media_path: str,
    video_title: str | None,
    mime_type: str = "video/mp4",
) -> list[dict]:
    """
    Two passes over Gemini, sharing one uploaded file:
      1. Transcribe the final video's (Hinglish) AUDIO to a faithful verbatim
         transcript, marking anything not fully audible as "[CUT OFF]".
      2. Classify + extract EVERY named stock from that TRANSCRIPT TEXT only
         (no audio), so the model can't fill acoustic gaps from its own market
         knowledge — i.e. invent stock names from price levels or complete
         cut-off numbers. Pass 2 is held to a checkable rule: the stock name
         must appear verbatim in the transcript. Each item carries "type"
         ("recommendation"/"view") plus a verbatim "evidence" quote; the
         rec-vs-view filtering itself is done deterministically by the caller.

    Returns ALL classified items (views included), dicts only.

    Owns the full Gemini file lifecycle (upload -> poll -> generate -> delete) so
    the uploaded file is always cleaned up, even on error.
    """
    # Imported lazily so the app boots even if google-genai isn't installed in
    # an environment that doesn't use this feature.
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    uploaded = None
    try:
        uploaded = await client.aio.files.upload(
            file=media_path,
            config=types.UploadFileConfig(mime_type=mime_type),
        )
        # Video uploads go PROCESSING -> ACTIVE; poll until usable (or give up).
        deadline = time.monotonic() + _GEMINI_FILE_ACTIVE_TIMEOUT_S
        while uploaded.state != "ACTIVE":
            if uploaded.state == "FAILED":
                raise RuntimeError(f"Gemini file processing failed: {uploaded.error}")
            if time.monotonic() > deadline:
                raise TimeoutError("Gemini file did not become ACTIVE in time")
            await anyio.sleep(_GEMINI_FILE_POLL_INTERVAL_S)
            uploaded = await client.aio.files.get(name=uploaded.name)

        # THE TWO PASSES NEED OPPOSITE SETTINGS. Measured 2026-08-23; do not
        # collapse them into one config (that regressed pass 2 — see below).
        #
        # Pass 1 is TRANSDUCTION: write down what you hear. Thinking makes the
        # model "tidy" the audio instead of transcribing it. On job 672e5e1a
        # (3-min clip), 13 runs at default vs 7 with thinking low:
        #   default thinking  14-98s; EVERY run differed. Numbers came out split
        #                     ("forty-five eighty-six" -> "45 86") and the stock
        #                     name was dropped 3 times in 13 — the same
        #                     instability that put "Ethos" in a user's CSV where
        #                     the speaker said "Eternal".
        #   thinking low      ~7-17s; 7/7 runs exact (correct name, all 12
        #                     numbers). temperature=0 is safe here precisely
        #                     BECAUSE thinking is off.
        #
        # Pass 2 is REASONING: work out which levels belong to which stock, and
        # whether a mention is a call or a view. Thinking is load-bearing. With
        # thinking low it still filled evidence/type/action/cmp but silently
        # OMITTED "stoploss" and "targets" whenever the levels sat far from the
        # instruction — 2/2 runs on job 1389b48f dropped a genuine APL Apollo
        # Tubes call (SL 1805, targets 1855/1870 quoted in its own evidence!)
        # because require_trade_fields then saw an incomplete BUY. Default
        # thinking filled both fields 2/2. So pass 2 keeps the model's default.
        #
        # And pass 2 must keep its DEFAULT temperature: temperature=0 with
        # thinking enabled loops the reasoning to an internal cap — measured
        # ~62,910 thinking tokens / 214s on pass 2, and ~62,913 / ~195s on
        # pass 1, 3/3 runs. That is what Google's "don't lower temperature on
        # Gemini 3" guidance is about, and it lives in the thinking.
        thinking_pass1 = _thinking_off(types)
        temperature_pass1 = 0
        temperature_pass2 = 0 if settings.gemini_model.startswith("gemini-2") else None

        # ---- Pass 1: faithful verbatim transcription from the AUDIO ----
        # media_resolution only means anything for video input; when we send the
        # audio-only sidecar there are no frames to down-sample.
        tx_config: dict[str, Any] = {
            "system_instruction": TRANSCRIBE_SYSTEM_PROMPT,
            "temperature": temperature_pass1,
        }
        if thinking_pass1 is not None:
            tx_config["thinking_config"] = thinking_pass1
        if mime_type.startswith("video/"):
            tx_config["media_resolution"] = types.MediaResolution.MEDIA_RESOLUTION_LOW
        tx_resp = await _generate_with_retry(
            client,
            model=settings.gemini_model,
            contents=[uploaded, TRANSCRIBE_USER_DIRECTIVE],
            config=types.GenerateContentConfig(**tx_config),
            what="pass 1 (transcribe)",
        )
        transcript = (tx_resp.text or "").strip()
        if not transcript:
            logger.warning("recommendations: pass 1 returned an empty transcript")
            return []

        # ---- Pass 2: extract structured recs from the TRANSCRIPT TEXT only ----
        # No file/audio here — the model can only use the transcribed words, so it
        # cannot invent a name from price levels or complete a "[CUT OFF]" number.
        extract_config = types.GenerateContentConfig(
            system_instruction=EXTRACT_SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=_build_extract_schema(types),
            temperature=temperature_pass2,
        )
        extract_contents = [
            f"Video title: {video_title or ''}\n\n"
            f"TRANSCRIPT:\n{transcript}\n\n{EXTRACT_USER_DIRECTIVE}"
        ]
        last_exc: Exception | None = None
        for attempt in range(1, _EXTRACT_JSON_ATTEMPTS + 1):
            ex_resp = await _generate_with_retry(
                client,
                model=settings.gemini_model,
                contents=extract_contents,
                config=extract_config,
                what=f"pass 2 (extract, try {attempt})",
            )
            content = ex_resp.text or "{}"
            try:
                items = _parse_stocks_payload(content)
            except json.JSONDecodeError as exc:
                last_exc = exc
                finish = None
                try:
                    finish = ex_resp.candidates[0].finish_reason
                except Exception:  # noqa: BLE001 — diagnostics only.
                    pass
                logger.warning(
                    "recommendations: pass 2 attempt %d/%d returned invalid "
                    "JSON (finish_reason=%s): %s; tail=%r",
                    attempt, _EXTRACT_JSON_ATTEMPTS, finish, exc, content[-200:],
                )
                continue
            logger.debug("recommendations: pass 2 items: %s", items)
            return items
        raise RuntimeError(
            "The AI returned malformed output for this video "
            f"({_EXTRACT_JSON_ATTEMPTS} attempts). Please click Generate again."
        ) from last_exc
    finally:
        if uploaded is not None:
            try:
                await client.aio.files.delete(name=uploaded.name)
            except Exception:  # noqa: BLE001 — tolerant; 48h TTL is the backstop.
                logger.warning(
                    "recommendations: failed to delete Gemini file %s",
                    getattr(uploaded, "name", "?"),
                    exc_info=True,
                )


async def generate_for_job(job_id: str) -> None:
    """
    Background task: download the final video, have Gemini transcribe its audio
    and extract recommendations, build the CSV, upload it to R2, and stamp
    recommendations.status=READY (or FAILED on any error).

    The job must already be claimed into GENERATING by the caller
    (`job_service.claim_recommendations_generating`).
    """
    storage = get_storage()
    tmp_dir = tempfile.mkdtemp(prefix=f"recs_{job_id}_")
    media_path = os.path.join(tmp_dir, "final.mp4")  # reassigned below if audio
    csv_path = os.path.join(tmp_dir, "recommendations.csv")

    try:
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")

        job = await job_service.get_job_raw(job_id)
        if job is None:
            logger.warning("recommendations[%s] job vanished; aborting", job_id)
            return
        video_title = job.get("video_title")

        # Prefer the audio-only sidecar the worker writes at render time: pass 1
        # just transcribes speech, so the video frames are dead weight — ~3.6x
        # the input tokens and ~2.7x the latency for an identical transcript,
        # and a much bigger request for Gemini to shed with a 503 under load.
        # Jobs rendered before that sidecar existed (and any render where the
        # best-effort extraction failed) fall back to final.mp4 unchanged.
        if (job.get("artifacts") or {}).get("final_audio_key"):
            media_key = r2_key_final_audio(job_id)
            media_path = os.path.join(tmp_dir, "final_audio.m4a")
            mime_type = "audio/mp4"
        else:
            media_key = r2_key_final_video(job_id)
            mime_type = "video/mp4"
            logger.info(
                "recommendations[%s] no final_audio_key; falling back to "
                "final.mp4 (older job or failed sidecar)", job_id,
            )

        # Download the chosen media, then let Gemini transcribe its audio and
        # classify/extract every named stock (off the event loop for the
        # download). Duplicates are merged, then only "recommendation"-typed
        # items reach the CSV — views are logged and dropped. An empty result
        # still yields a header-only CSV.
        try:
            await anyio.to_thread.run_sync(storage.download_file, media_key, media_path)
        except Exception as exc:  # noqa: BLE001 — translate to a human message.
            # claim_recommendations_generating already checks the object exists,
            # so reaching here means it was swept between the click and now (the
            # R2 jobs/ prefix has a 7-day lifecycle rule). Say so plainly rather
            # than surfacing "404 HeadObject Not Found" to a user.
            raise RuntimeError(
                "This video's files have expired (artifacts are kept for 7 days), "
                "so recommendations can no longer be generated for it. Process the "
                "video again to get them."
            ) from exc
        raw_items = await _extract_recommendations(media_path, video_title, mime_type)
        merged = merge_duplicate_stocks(raw_items)
        if len(merged) < len(raw_items):
            logger.info(
                "recommendations[%s] merged %d duplicate item(s) by stock name",
                job_id, len(raw_items) - len(merged),
            )
        items, views, invalid = partition_by_type(merged)
        items, incomplete = require_trade_fields(items)
        items, inconsistent = flag_inconsistent_cmp(items)
        if inconsistent:
            logger.warning(
                "recommendations[%s] %d row(s) have a cmp that contradicts their "
                "own levels (cmp marked with *, row KEPT): %s",
                job_id, len(inconsistent),
                "; ".join(
                    f"{d.get('stock_name') or '<unnamed>'} "
                    f"cmp={d.get('cmp')!r} sl={d.get('stoploss')!r} "
                    f"tg={d.get('targets')!r}"
                    for d in inconsistent
                ),
            )
        for bad in invalid:
            logger.warning(
                'recommendations[%s] item with missing/unknown "type"=%r '
                "treated as view: %s",
                job_id, bad.get("type"), bad.get("stock_name") or "<unnamed>",
            )
        if incomplete:
            logger.info(
                "recommendations[%s] demoted %d incomplete recommendation(s) "
                "(missing cmp/stoploss/targets): %s",
                job_id, len(incomplete),
                ", ".join(str(d.get("stock_name") or "<unnamed>") for d in incomplete),
            )
        dropped = views + invalid + incomplete
        if dropped:
            logger.info(
                "recommendations[%s] dropped %d view item(s): %s",
                job_id, len(dropped),
                ", ".join(str(d.get("stock_name") or "<unnamed>") for d in dropped),
            )

        csv_text = build_recommendations_csv(items)
        with open(csv_path, "w", encoding="utf-8") as fh:
            fh.write(csv_text)
        await anyio.to_thread.run_sync(
            storage.upload_file, csv_path, r2_key_recommendations(job_id)
        )

        await job_service.set_recommendations_status(
            job_id, "READY", key=r2_key_recommendations(job_id), count=len(items)
        )
        logger.info("recommendations[%s] READY (%d rows)", job_id, len(items))

    except Exception as exc:  # noqa: BLE001 — best-effort; record and move on.
        logger.exception("recommendations[%s] failed", job_id)
        await job_service.set_recommendations_status(
            job_id,
            "FAILED",
            error={"code": "GENERATION_FAILED", "message": str(exc)},
        )
    finally:
        for path in (media_path, csv_path):
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
