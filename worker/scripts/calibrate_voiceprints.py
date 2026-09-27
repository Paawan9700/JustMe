"""
One-off calibration for favourite-voice matching (Phase 0). NOT deployed.

Past jobs no longer have their audio (ephemeral/ is deleted after render),
but Mongo still has each job's speakers + selected_speaker and R2 still has
jobs/{id}/diarization.json. This re-downloads AUDIO ONLY for a list of past
jobs, then computes a voice print per speaker with the exact production
recipe (worker/tasks/voiceprints.py). The prints are analysed offline to
pick the matching thresholds.

    modal run worker/scripts/calibrate_voiceprints.py \
        --jobs-file /path/jobs.json --out-file /path/prints.json

`jobs.json` is a JSON list of job ids. Output: one record per job with the
per-speaker prints (base64 float32) and a self-consistency check.

Runs as its own Modal app on the worker image — nothing in the deployed
`justme-worker` app changes, and worker/requirements.txt is untouched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make `worker` / `shared` importable both locally (repo root) and inside the
# container (/app, where the worker image copies them).
for _cand in [*Path(__file__).resolve().parents, Path("/app")]:
    if (_cand / "worker" / "modal_app.py").exists():
        if str(_cand) not in sys.path:
            sys.path.insert(0, str(_cand))
        break

import modal  # noqa: E402

from worker import modal_app as _worker  # noqa: E402

app = modal.App("justme-voiceprint-calibration", image=_worker.image)


def _download_audio(youtube_url: str, out_dir: Path) -> tuple[Path, int]:
    """bestaudio via the production yt-dlp options (proxy, clients, cookies)."""
    import time

    import yt_dlp

    from worker.tasks import ingest

    opts = {
        "format": "bestaudio",
        "outtmpl": str(out_dir / "audio.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_args": {
            "youtube": {"player_client": ["tv", "web_embedded", "android_vr"]},
        },
    }
    ingest._maybe_add_proxy(opts)
    ingest._maybe_add_cookies(opts)
    for attempt in range(3):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(youtube_url, download=True)
            break
        except yt_dlp.utils.DownloadError as exc:
            if "Tunnel connection failed" not in str(exc) or attempt == 2:
                raise
            time.sleep(30)
    files = [p for p in out_dir.iterdir() if p.name.startswith("audio.")]
    if not files:
        raise RuntimeError("yt-dlp produced no audio file")
    return files[0], int(info.get("duration") or 0)


def _self_check(prints: dict) -> float | None:
    """Share of kept windows whose nearest centroid is their own speaker's."""
    from shared import voice_match

    if len(prints) < 2:
        return None
    hits = total = 0
    for label, p in prints.items():
        for w in p["windows"]:
            best = max(prints, key=lambda lb: voice_match.dot(w, prints[lb]["centroid"]))
            hits += best == label
            total += 1
    return round(hits / total, 4) if total else None


@app.function(
    secrets=[modal.Secret.from_name("justme-secrets")],
    gpu="A10G",
    cpu=4.0,
    memory=16384,
    timeout=3600,
    # One at a time: the proxy refused a second simultaneous tunnel in the
    # pilot ("Tunnel connection failed: 403"), and production shares it.
    max_containers=1,
    volumes={"/cache": _worker.hf_cache},
)
def calibrate_job(job_id: str) -> dict:
    import base64
    import shutil
    import time
    import traceback

    from shared import voice_match
    from worker.db import get_db
    from worker.tasks import voiceprints
    from worker.utils.ffmpeg import get_video_duration
    from worker.utils.storage import download_file

    t0 = time.time()
    work = Path("/tmp/calib") / job_id
    work.mkdir(parents=True, exist_ok=True)
    rec: dict = {"job_id": job_id}
    try:
        job = get_db().jobs.find_one(
            {"job_id": job_id},
            {"youtube_url": 1, "duration_sec": 1, "speakers": 1,
             "selected_speaker": 1, "artifacts": 1, "_id": 0},
        )
        if not job:
            return {**rec, "error": "job not found"}
        labels = [s["label"] for s in job.get("speakers") or []]
        rec["selected"] = job.get("selected_speaker")
        rec["duration_sec"] = job.get("duration_sec")

        audio, yt_duration = _download_audio(job["youtube_url"], work)
        rec["yt_duration"] = yt_duration
        rec["audio_duration"] = round(get_video_duration(str(audio)), 2)
        if abs(yt_duration - int(job.get("duration_sec") or 0)) > 1:
            return {**rec, "error": "duration mismatch (timeline may have shifted)"}

        wav = work / "audio.wav"
        voiceprints.extract_wav(audio, wav)
        audio.unlink()

        turns_path = work / "diarization.json"
        download_file(job["artifacts"]["diarization_key"], str(turns_path))
        turns = json.loads(turns_path.read_text(encoding="utf-8"))
        spans, source = voiceprints.spans_for_labels(labels, turns, [])
        rec["span_source"] = source

        prints = voiceprints.compute_prints_for_wav(wav, spans)
        rec["self_check"] = _self_check(prints)
        rec["pyannote_version"] = voiceprints.pyannote_version()
        rec["prints"] = {
            label: {
                "centroid": base64.b64encode(voice_match.pack(p["centroid"])).decode(),
                "windows": base64.b64encode(voice_match.pack_many(p["windows"])).decode(),
                "n_windows": p["n_windows"],
                "speech_sec": p["speech_sec"],
            }
            for label, p in prints.items()
        }
        rec["speech_by_label"] = {
            lb: round(sum(e - s for s, e in sp), 2) for lb, sp in spans.items()
        }
        rec["seconds"] = round(time.time() - t0, 1)
        return rec
    except Exception as exc:  # noqa: BLE001
        return {**rec, "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc()[-2000:]}
    finally:
        shutil.rmtree(work, ignore_errors=True)


@app.local_entrypoint()
def main(jobs_file: str, out_file: str):
    job_ids = json.loads(Path(jobs_file).read_text())
    results = []
    for rec in calibrate_job.map(job_ids, return_exceptions=True):
        if isinstance(rec, Exception):
            rec = {"error": f"{type(rec).__name__}: {rec}"}
        status = rec.get("error") or (
            f"{len(rec.get('prints', {}))} prints, self_check={rec.get('self_check')}, "
            f"{rec.get('seconds')}s"
        )
        print(f"{rec.get('job_id', '?')[:8]}  {status}")
        results.append(rec)
    Path(out_file).write_text(json.dumps(results))
    print(f"wrote {len(results)} records -> {out_file}")
