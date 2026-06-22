"""
Stock-recommendations feature.

Given a job's final-video transcript (`transcription.txt` in R2), call an LLM to
extract the stock recommendations the speaker made and produce a downloadable CSV
(`recommendations.csv` in R2). The job's own status is never touched — this is an
independent sub-resource that runs after the job is DONE.

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

import anyio

from app.core.config import settings
from app.services import job_service
from app.services.storage import get_storage
from shared.constants import r2_key_recommendations, r2_key_transcription

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

# Defensive cap on transcript size sent to the LLM (a 5-min transcript is a few
# KB; this only guards against a pathological input running up token cost).
_MAX_TRANSCRIPT_CHARS = 120_000

RECS_SYSTEM_PROMPT = """\
You extract stock recommendations made by a single speaker from a transcript of \
their spoken words. When recommending a stock the speaker typically states, in \
order: the stock name, then CMP (current market price), then a stop-loss, then \
one or more targets, then the reasoning.

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


async def _extract_recommendations(transcript: str, video_title: str | None) -> list[dict]:
    """Call Gemini and return the parsed recommendations list (may be empty)."""
    # Imported lazily so the app boots even if google-genai isn't installed in
    # an environment that doesn't use this feature.
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    user_content = (
        f"Video title: {video_title or ''}\n\n"
        f"Transcript:\n{transcript[:_MAX_TRANSCRIPT_CHARS]}"
    )
    resp = await client.aio.models.generate_content(
        model=settings.gemini_model,
        contents=user_content,
        config=types.GenerateContentConfig(
            system_instruction=RECS_SYSTEM_PROMPT,
            response_mime_type="application/json",
            temperature=0,
        ),
    )
    content = resp.text or "{}"
    data = json.loads(content)
    items = data.get("recommendations", []) if isinstance(data, dict) else []
    return [i for i in items if isinstance(i, dict)]


async def generate_for_job(job_id: str) -> None:
    """
    Background task: read the transcript, call the LLM, build the CSV, upload it
    to R2, and stamp recommendations.status=READY (or FAILED on any error).

    The job must already be claimed into GENERATING by the caller
    (`job_service.claim_recommendations_generating`).
    """
    storage = get_storage()
    tmp_dir = tempfile.mkdtemp(prefix=f"recs_{job_id}_")
    transcript_path = os.path.join(tmp_dir, "transcription.txt")
    csv_path = os.path.join(tmp_dir, "recommendations.csv")

    try:
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")

        job = await job_service.get_job_raw(job_id)
        if job is None:
            logger.warning("recommendations[%s] job vanished; aborting", job_id)
            return
        video_title = job.get("video_title")

        # Download the transcript (best-effort tiny file; off the event loop).
        await anyio.to_thread.run_sync(
            storage.download_file, r2_key_transcription(job_id), transcript_path
        )
        with open(transcript_path, encoding="utf-8") as fh:
            transcript = fh.read().strip()

        if not transcript:
            # Transcript exists but is empty: nothing to recommend. Ship an
            # empty (header-only) CSV rather than failing.
            items: list[dict] = []
        else:
            items = await _extract_recommendations(transcript, video_title)

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
        for path in (transcript_path, csv_path):
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
