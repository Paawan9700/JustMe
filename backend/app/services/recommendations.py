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

RECS_SYSTEM_PROMPT = """\
You are given an AUDIO/VIDEO recording of a SINGLE speaker talking in Hinglish \
(mixed Hindi and English) who discusses and recommends stocks. FIRST transcribe \
what the speaker says — paying special attention to NUMBERS (prices, stop-losses, \
targets) — THEN extract the stock recommendations they make. When recommending a \
stock the speaker typically states, in order: the stock name, then CMP (current \
market price), then a stop-loss, then one or more targets, then the reasoning.

Return ONLY a JSON object of this exact shape:
{"recommendations": [
  {"date": "", "stock_name": "", "action": "", "cmp": "", "stoploss": "", "targets": "", "reasoning": ""}
]}

Rules:
- One object per stock the speaker ACTUALLY recommends. If the speaker recommends \
no stocks, return {"recommendations": []}.

- "action" must be exactly "BUY" or "SELL":
    * Use "SELL" when the speaker recommends selling or shorting the stock. Signals \
include: the speaker explicitly saying "sell", "short", "exit", or "book"; or \
referring to the instrument as a futures/derivatives contract in a bearish context \
(e.g. "Axis Bank June Futures"). Weigh the overall context, not just keywords.
    * Otherwise use "BUY". This is the default — if it is genuinely unclear, choose \
"BUY", because most recommendations are buys.

- "stock_name": use the speaker's OWN name for the instrument and PRESERVE any \
futures/month/derivative qualifier exactly as said — e.g. keep "Axis Bank June \
Futures", do NOT shorten it to "Axis Bank". Conversely, never ADD a "futures"/month \
qualifier the speaker did not actually say.

- Put first and second targets together in the single "targets" field, e.g. \
"T1: 1500; T2: 1600". If only one target is given, just include that one.

- Derive "date" from the video title ONLY if a date appears there; otherwise use "".

- "reasoning": capture the speaker's FULL rationale for the call, faithfully and in \
their own framing. Include the technical and/or fundamental points they actually \
mention — e.g. chart levels, breakout/breakdown, support/resistance, trend, volume, \
moving averages, quarterly results/earnings, news or catalysts, sector view, and \
risk-reward. Be thorough and specific, but do not pad and do NOT invent anything the \
speaker did not say. If the speaker gave no reason, leave it "".

- NEVER fabricate or guess values. If the speaker did not state a field, leave it "".

NUMERIC ACCURACY (the audio is Hinglish and numbers are easy to mishear):
- Transcribe every number digit-for-digit. Indian speakers often say prices \
digit-by-digit or mix Hindi and English (e.g. "इक्कीस सौ बीस" / "twenty-one twenty" \
= 2120). Do NOT drop or merge digits (e.g. do not collapse "2120" into "21").
- Before returning each recommendation, run an internal PLAUSIBILITY check:
    * For a BUY, targets are normally ABOVE the CMP and the stop-loss BELOW it.
    * For a SELL/short, targets are normally BELOW the CMP and the stop-loss ABOVE it.
    * CMP, stop-loss and targets should share the same order of magnitude. A target \
of 21 next to a CMP of 2000 almost certainly means "2100" was misheard as "21".
  If a value fails this check, re-listen and correct it if you can.
- FLAG uncertainty: append a single trailing asterisk "*" to ANY numeric value you \
are not fully confident you heard correctly — and ONLY to that specific number. \
Examples: "cmp": "2050*", "stoploss": "1980", "targets": "T1: 2120*; T2: 2200". \
Confident numbers get no asterisk. Do NOT add any other confidence commentary; the \
asterisk is the only signal.
"""


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
    Upload the final video to Gemini, wait for it to become ACTIVE, then ask the
    model to transcribe the (Hinglish) audio and return the recommendations JSON.

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

        resp = await client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=[uploaded, f"Video title: {video_title or ''}"],
            config=types.GenerateContentConfig(
                system_instruction=RECS_SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=0,
                media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
            ),
        )
        content = resp.text or "{}"
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
