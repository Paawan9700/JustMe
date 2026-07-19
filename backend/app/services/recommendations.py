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
    the recommendations — views are logged and dropped, and `evidence` never
    reaches the CSV.
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
import tempfile
import time

import anyio

from app.core.config import settings
from app.services import job_service
from app.services.storage import get_storage
from shared.constants import r2_key_final_video, r2_key_recommendations

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
- "cmp": the price the stock is trading at NOW, as he states it ("abhi 2020 \
chal raha hai", "CMP is 2020").
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
    "once -> ONE object with the latest stated values. Every object MUST have "
    '"type" and a short verbatim "evidence" quote.'
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


async def _extract_recommendations(video_path: str, video_title: str | None) -> list[dict]:
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
            file=video_path,
            config=types.UploadFileConfig(mime_type="video/mp4"),
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

        # Gemini 2.x: pin temperature=0 (the battle-tested decode behavior).
        # Gemini 3+ thinking models: leave temperature at its default — Google
        # explicitly recommends against lowering it (it can degrade or loop
        # the output). None means the field is simply not sent.
        temperature = 0 if settings.gemini_model.startswith("gemini-2") else None

        # ---- Pass 1: faithful verbatim transcription from the AUDIO ----
        tx_resp = await client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=[uploaded, TRANSCRIBE_USER_DIRECTIVE],
            config=types.GenerateContentConfig(
                system_instruction=TRANSCRIBE_SYSTEM_PROMPT,
                temperature=temperature,
                media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
            ),
        )
        transcript = (tx_resp.text or "").strip()
        if not transcript:
            logger.warning("recommendations: pass 1 returned an empty transcript")
            return []

        # ---- Pass 2: extract structured recs from the TRANSCRIPT TEXT only ----
        # No file/audio here — the model can only use the transcribed words, so it
        # cannot invent a name from price levels or complete a "[CUT OFF]" number.
        ex_resp = await client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=[
                f"Video title: {video_title or ''}\n\n"
                f"TRANSCRIPT:\n{transcript}\n\n{EXTRACT_USER_DIRECTIVE}"
            ],
            config=types.GenerateContentConfig(
                system_instruction=EXTRACT_SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=temperature,
            ),
        )
        content = ex_resp.text or "{}"
        data = json.loads(content)
        raw = []
        if isinstance(data, dict):
            # Tolerate the pre-rename envelope key from older prompt versions.
            raw = data.get("stocks") or data.get("recommendations") or []
        items = [i for i in raw if isinstance(i, dict)]
        logger.debug("recommendations: pass 2 items: %s", items)
        return items
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
    video_path = os.path.join(tmp_dir, "final.mp4")
    csv_path = os.path.join(tmp_dir, "recommendations.csv")

    try:
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")

        job = await job_service.get_job_raw(job_id)
        if job is None:
            logger.warning("recommendations[%s] job vanished; aborting", job_id)
            return
        video_title = job.get("video_title")

        # Download the final video, then let Gemini transcribe its audio and
        # classify/extract every named stock (off the event loop for the
        # download). Duplicates are merged, then only "recommendation"-typed
        # items reach the CSV — views are logged and dropped. An empty result
        # still yields a header-only CSV.
        await anyio.to_thread.run_sync(
            storage.download_file, r2_key_final_video(job_id), video_path
        )
        raw_items = await _extract_recommendations(video_path, video_title)
        merged = merge_duplicate_stocks(raw_items)
        if len(merged) < len(raw_items):
            logger.info(
                "recommendations[%s] merged %d duplicate item(s) by stock name",
                job_id, len(raw_items) - len(merged),
            )
        items, views, invalid = partition_by_type(merged)
        for bad in invalid:
            logger.warning(
                'recommendations[%s] item with missing/unknown "type"=%r '
                "treated as view: %s",
                job_id, bad.get("type"), bad.get("stock_name") or "<unnamed>",
            )
        dropped = views + invalid
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
        for path in (video_path, csv_path):
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
