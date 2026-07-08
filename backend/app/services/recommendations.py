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
  * `build_recommendations_csv` is a pure function (unit-tested). `generate_for_job`
    is the async background task that does the I/O.
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
You are given a TRANSCRIPT of a single stock analyst speaking (Hinglish). Extract \
ONLY the stock recommendations explicitly present IN THE TRANSCRIPT TEXT. Work \
solely from the transcript — do NOT use outside market knowledge, and rely on \
nothing that is not written in it.

Return ONLY a JSON object of this exact shape:
{"recommendations": [
  {"date": "", "stock_name": "", "action": "", "cmp": "", "stoploss": "", "targets": "", "reasoning": ""}
]}

Rules:
- One object per stock that is BOTH recommended AND explicitly NAMED in the \
transcript. The transcript often has price levels/targets/chart talk with NO stock \
name attached (the analyst commenting on someone else's call) — DROP those entirely: \
emit no row for a set of levels whose stock name does not appear in the transcript. \
If there are no named recommendations, return {"recommendations": []}.

- "action" must be exactly "BUY" or "SELL":
    * Use "SELL" when the speaker recommends selling or shorting the stock. Signals \
include: the speaker explicitly saying "sell", "short", "exit", or "book"; or \
referring to the instrument as a futures/derivatives contract in a bearish context \
(e.g. "Axis Bank June Futures"). Weigh the overall context, not just keywords.
    * Otherwise use "BUY". This is the default — if it is genuinely unclear, choose \
"BUY", because most recommendations are buys.

- "stock_name": copy the analyst's OWN name for the instrument from the transcript, \
preserving any futures/month/derivative qualifier as written — e.g. keep "Axis Bank \
June Futures", do NOT shorten it to "Axis Bank"; never ADD a qualifier the transcript \
lacks. The name MUST be one that appears in the transcript, output in its standard \
English spelling (e.g. "Bharat Forge"). NEVER infer, guess or supply a name from price \
levels, chart levels, ticker prices, or your own market knowledge — if the transcript \
has no name for a set of levels, DROP it (per the first rule).

- Put first and second targets together in the single "targets" field, e.g. \
"T1: 1500; T2: 1600". If only one target is given, just include that one.

- Derive "date" from the video title ONLY if a date appears there; otherwise use "".

- "reasoning": summarise the analyst's rationale for THAT call in clear, concise \
ENGLISH (translate it from the Hinglish transcript — do NOT leave it in Hindi). \
Cover the technical/fundamental points they actually make — chart levels, breakout/\
breakdown, support/resistance, trend, volume, results/earnings, news or catalysts, \
sector view, risk-reward — faithfully; do not pad and do NOT invent anything not in \
the transcript. If no reason is given, leave it "".

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
    "Extract the recommendations from the transcript now. HARD RULES: (1) include a "
    "stock ONLY if its name appears verbatim in the transcript — never infer a name "
    "from price levels or from your own knowledge; (2) copy numbers exactly as "
    "written, with no invented decimals; (3) never complete a number marked "
    '"[CUT OFF]" — output the digits shown + "*" (e.g. "166*") or leave it blank.'
)


def build_recommendations_csv(items: list[dict]) -> str:
    """
    Serialise a list of recommendation objects to a CSV string with the fixed
    6-column header. Tolerant by design: any missing field becomes "" so we
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


async def _extract_recommendations(video_path: str, video_title: str | None) -> list[dict]:
    """
    Two passes over Gemini, sharing one uploaded file:
      1. Transcribe the final video's (Hinglish) AUDIO to a faithful verbatim
         transcript, marking anything not fully audible as "[CUT OFF]".
      2. Extract structured recommendations from that TRANSCRIPT TEXT only (no
         audio), so the model can't fill acoustic gaps from its own market
         knowledge — i.e. invent stock names from price levels or complete
         cut-off numbers. Pass 2 is held to a checkable rule: the stock name
         must appear verbatim in the transcript.

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

        # ---- Pass 1: faithful verbatim transcription from the AUDIO ----
        tx_resp = await client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=[uploaded, TRANSCRIBE_USER_DIRECTIVE],
            config=types.GenerateContentConfig(
                system_instruction=TRANSCRIBE_SYSTEM_PROMPT,
                temperature=0,
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
                temperature=0,
            ),
        )
        content = ex_resp.text or "{}"
        data = json.loads(content)
        items = data.get("recommendations", []) if isinstance(data, dict) else []
        return [i for i in items if isinstance(i, dict)]
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
        # extract the recommendations in one call (off the event loop for the
        # download). An empty result comes back as {"recommendations": []} ->
        # a header-only CSV, so there's no empty-input special case here.
        await anyio.to_thread.run_sync(
            storage.download_file, r2_key_final_video(job_id), video_path
        )
        items = await _extract_recommendations(video_path, video_title)

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
