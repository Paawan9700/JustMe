"""
Modal deployment for the JustMe worker — on-demand functions.

Primary path (zero manual steps per job, ~$0 idle cost):
  The API spawns the deployed `process_video` / `render_video` functions
  directly (backend/app/services/queue.py, QUEUE_BACKEND=modal). A GPU
  container starts when a job is submitted, bills per-second while it
  works, and scales back to zero ~60s after finishing. A deployed app
  with no running containers costs nothing, so the deployment stays
  live 24/7.

One-time setup:

    pip install modal
    modal token new
    modal secret create justme-secrets \\
        MONGO_URL=...  DB_NAME=justme \\
        R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... \\
        R2_BUCKET_NAME=... R2_ENDPOINT_URL=https://<acct>.r2.cloudflarestorage.com \\
        HF_TOKEN=hf_... MAX_VIDEO_HOURS=15
    modal deploy worker/modal_app.py
    modal run worker/modal_app.py     # optional: pre-seed the model cache (~5 min)

Then give the backend a Modal API token (MODAL_TOKEN_ID /
MODAL_TOKEN_SECRET in backend/.env) and it dispatches jobs itself —
no `modal run --detach`, no `modal app stop`, ever. Redeploy only when
worker code changes.

Model caching:
  The image routes HF_HOME/TORCH_HOME to /cache, backed by the
  `justme-hf-cache` Volume, so WhisperX + pyannote weights download
  once and every later cold start reads them from the volume instead
  of re-pulling several GB from HuggingFace.

Legacy fallback (resident Celery worker):
  `run_worker` is kept for rollback only. Set QUEUE_BACKEND=celery on
  the backend and start it with
      modal run --detach worker/modal_app.py::run_worker
  It holds a GPU continuously until `modal app stop justme-worker` —
  the exact cost trap the on-demand functions exist to avoid.
"""

from __future__ import annotations

from pathlib import Path

import modal

# ---------------------------------------------------------------------------
# Image: matches worker/Dockerfile as closely as possible.
# Build from worker/requirements.txt + a separate WhisperX git install
# so the requirements layer caches cleanly.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent  # /app
_REQUIREMENTS = _REPO_ROOT / "worker" / "requirements.txt"

image = (
    modal.Image.from_registry(
        "pytorch/pytorch:2.1.0-cuda11.8-cudnn8-runtime",
        add_python="3.11",
    )
    .run_commands(
        "export DEBIAN_FRONTEND=noninteractive && "
        "apt-get update && "
        "apt-get install -y --no-install-recommends git tzdata curl xz-utils && "
        "rm -rf /var/lib/apt/lists/* && "
        "curl -sL 'https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz' "
        "| tar -xJ --strip-components=2 -C /usr/local/bin --wildcards '*/bin/ffmpeg' '*/bin/ffprobe' && "
        "mkdir -p /opt/conda/bin && "
        "ln -sf /usr/local/bin/ffmpeg /opt/conda/bin/ffmpeg && "
        "ln -sf /usr/local/bin/ffprobe /opt/conda/bin/ffprobe && "
        "ln -sf /usr/local/bin/ffmpeg /usr/bin/ffmpeg"
    )
    # Deno: yt-dlp needs a JavaScript runtime to solve YouTube's "n" signature
    # challenge. Without it yt-dlp logs "n challenge solving failed: Some
    # formats may be missing" and can drop formats or get throttled. Installing
    # the deno binary onto PATH makes yt-dlp's deno JS Challenge Provider
    # available (it auto-detects `deno` on PATH).
    .run_commands(
        "export DEBIAN_FRONTEND=noninteractive && "
        "apt-get update && apt-get install -y --no-install-recommends unzip && "
        "rm -rf /var/lib/apt/lists/* && "
        "curl -fsSL 'https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip' "
        "-o /tmp/deno.zip && "
        "unzip /tmp/deno.zip -d /usr/local/bin && "
        "chmod +x /usr/local/bin/deno && "
        "rm /tmp/deno.zip && "
        "deno --version"
    )
    .pip_install_from_requirements(str(_REQUIREMENTS))
    .pip_install("git+https://github.com/m-bain/whisperX.git")
    # Route every model cache to the shared Volume mounted at /cache:
    # HF_HOME covers faster-whisper + pyannote (diarization, VAD, reclaim's
    # embedder); TORCH_HOME covers the wav2vec2 align model (torch.hub).
    .env({"HF_HOME": "/cache/huggingface", "TORCH_HOME": "/cache/torch"})
    # Bake the worker + shared packages into the image. Modal needs the
    # absolute repo paths so the image gets fresh copies on every deploy.
    # (Local-dir layers must stay last in the chain.)
    .workdir("/app")
    .add_local_dir(str(_REPO_ROOT / "worker"), remote_path="/app/worker")
    .add_local_dir(str(_REPO_ROOT / "shared"), remote_path="/app/shared")
)

app = modal.App("justme-worker", image=image)

# Persistent model cache shared by every function below. ~5 GiB of
# weights — inside Modal's free storage tier.
hf_cache = modal.Volume.from_name("justme-hf-cache", create_if_missing=True)

# Per-job time limits, mirroring the Celery settings in
# worker/celery_app.py (task_soft_time_limit / task_time_limit).
TASK_SOFT_TIME_LIMIT = 7200   # 2h — raises SoftTimeLimitExceeded in-process
TASK_TIME_LIMIT = 7800        # 2h10m — Modal hard-kills the container

# Env vars the on-demand functions need from `justme-secrets`.
# (REDIS_URL is only needed by the legacy run_worker fallback.)
_REQUIRED_ENV = [
    "MONGO_URL", "DB_NAME",
    "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET_NAME", "R2_ENDPOINT_URL",
    "HF_TOKEN",
]


def _require_env(required: list[str]) -> None:
    """Fail loud at start if anything required is missing from the secret."""
    import os

    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            f"Missing required env vars from `justme-secrets`: {missing}. "
            "Update with `modal secret create justme-secrets ...` and redeploy."
        )


def _arm_soft_timeout(seconds: int) -> None:
    """
    Replicate Celery's soft time limit: after `seconds`, raise
    SoftTimeLimitExceeded inside the running function so
    worker/tasks/pipeline.py's existing handler marks the job
    FAILED/TIMEOUT and returns cleanly (no Modal retry).
    """
    import signal

    from celery.exceptions import SoftTimeLimitExceeded

    def _raise(signum, frame):  # noqa: ARG001
        raise SoftTimeLimitExceeded()

    try:
        signal.signal(signal.SIGALRM, _raise)
        signal.alarm(seconds)
    except ValueError:
        # Not on the main thread — Modal's hard `timeout` still protects us.
        pass


def _disarm_soft_timeout() -> None:
    """Clear any pending alarm — warm containers reuse this process."""
    import signal

    try:
        signal.alarm(0)
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# Primary path: on-demand functions spawned by the API
# ---------------------------------------------------------------------------

@app.function(
    secrets=[modal.Secret.from_name("justme-secrets")],
    gpu="A10G",            # WhisperX large-v3 + pyannote peak ~10 GB VRAM
    cpu=4.0,
    memory=16384,          # 16 GB RAM
    timeout=TASK_TIME_LIMIT,
    # Retries only fire on process death (OOM, preemption, hard timeout) —
    # pipeline.py catches every app-level error and returns a dict. The
    # stage-skip predicates make a retry resume, not restart.
    retries=modal.Retries(max_retries=2, backoff_coefficient=1.0, initial_delay=60.0),
    max_containers=2,      # spend guardrail: 2 parallel jobs, extras queue
    volumes={"/cache": hf_cache},
)
def process_video(job_id: str) -> dict:
    """ingest -> audio -> diarize -> snippets -> AWAITING_SELECTION."""
    _require_env(_REQUIRED_ENV)
    _arm_soft_timeout(TASK_SOFT_TIME_LIMIT)
    try:
        # Import inside the body: the deploying machine has no torch/yt_dlp.
        from worker.tasks.pipeline import run_process_video

        return run_process_video(job_id)
    finally:
        _disarm_soft_timeout()


@app.function(
    secrets=[modal.Secret.from_name("justme-secrets")],
    gpu="A10G",            # reclaim's voice-embedding pass runs on CUDA
    cpu=4.0,
    memory=16384,
    timeout=TASK_TIME_LIMIT,
    retries=modal.Retries(max_retries=1, backoff_coefficient=1.0, initial_delay=30.0),
    max_containers=2,
    volumes={"/cache": hf_cache},
)
def render_video(job_id: str) -> dict:
    """Cut the selected speaker's segments and render final.mp4."""
    _require_env(_REQUIRED_ENV)
    _arm_soft_timeout(TASK_SOFT_TIME_LIMIT)
    try:
        from worker.tasks.pipeline import run_render_video

        return run_render_video(job_id)
    finally:
        _disarm_soft_timeout()


# ---------------------------------------------------------------------------
# Cache seeding — `modal run worker/modal_app.py`
# ---------------------------------------------------------------------------

@app.function(
    secrets=[modal.Secret.from_name("justme-secrets")],
    gpu="A10G",
    cpu=4.0,
    memory=16384,
    timeout=1800,
    volumes={"/cache": hf_cache},
)
def seed_cache() -> dict:
    """
    Pre-download every model the pipeline uses into the cache volume and
    verify HF_TOKEN + both gated pyannote licenses
    (pyannote/speaker-diarization-3.1 AND pyannote/segmentation-3.0).
    Costs ~5 min of A10G (~$0.12) once; afterwards cold starts read
    weights from the volume instead of HuggingFace.
    """
    import os
    import time

    _require_env(_REQUIRED_ENV)
    hf_token = os.environ["HF_TOKEN"]
    t0 = time.time()

    import torch
    import whisperx

    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"

    # 1. Whisper large-v3 + pyannote VAD (mirrors diarize.py's load_model).
    print("seed_cache: loading whisper large-v3 (+ pyannote VAD)...")
    model = whisperx.load_model(
        "large-v3", device, compute_type=compute_type, language="en",
        vad_method="pyannote",
    )
    del model

    # 2. wav2vec2 alignment model for English (torch.hub -> TORCH_HOME).
    print("seed_cache: loading align model (en)...")
    model_a, _meta = whisperx.load_align_model(language_code="en", device=device)
    del model_a

    # 3. pyannote speaker-diarization-3.1 — the gated pipeline.
    print("seed_cache: loading pyannote speaker-diarization-3.1...")
    DiarizationPipeline = (
        whisperx.DiarizationPipeline
        if hasattr(whisperx, "DiarizationPipeline")
        else whisperx.diarize.DiarizationPipeline
    )
    try:
        dia = DiarizationPipeline(use_auth_token=hf_token, device=device)
    except TypeError:
        dia = DiarizationPipeline(token=hf_token, device=device)
    del dia

    # 4. reclaim's speaker-embedding model (reuses its compat loader).
    print("seed_cache: loading reclaim embedding model...")
    from worker.tasks.reclaim import _load_embedder

    emb = _load_embedder(torch.device(device), hf_token)
    del emb

    hf_cache.commit()
    took = round(time.time() - t0, 1)
    print(f"seed_cache: done in {took}s — volume committed")
    return {"ok": True, "seconds": took}


# ---------------------------------------------------------------------------
# LEGACY fallback: resident Celery worker (rollback path only)
# ---------------------------------------------------------------------------

@app.function(
    secrets=[modal.Secret.from_name("justme-secrets")],
    gpu="A10G",
    cpu=4.0,
    memory=16384,
    # Modal's per-invocation max is 24h; retries respawn the daemon when
    # the timeout fires or Celery crashes.
    timeout=86400,
    retries=modal.Retries(max_retries=10, backoff_coefficient=1.0, initial_delay=5.0),
    min_containers=0,
    volumes={"/cache": hf_cache},
)
def run_worker() -> None:
    """
    LEGACY / ROLLBACK ONLY — resident Celery worker consuming Upstash
    Redis. Bills the GPU continuously while running. Start it only if
    the backend is switched to QUEUE_BACKEND=celery:

        modal run --detach worker/modal_app.py::run_worker
        modal app stop justme-worker      # to stop the billing

    The primary path is the on-demand `process_video` / `render_video`
    functions above, spawned directly by the API.
    """
    import subprocess
    import sys

    _require_env([*_REQUIRED_ENV, "REDIS_URL"])

    # Hand control to Celery. concurrency=1 because each job loads
    # multi-GB ML models — running two in parallel would OOM the GPU.
    subprocess.run(
        [
            sys.executable, "-m", "celery",
            "-A", "worker.celery_app", "worker",
            "--loglevel=info",
            "--concurrency=1",
        ],
        check=True,
    )


@app.local_entrypoint()
def main() -> None:
    """
    `modal run worker/modal_app.py` seeds the model cache volume.
    (It deliberately no longer starts the resident worker — that
    foot-gun burned GPU-hours; use ::run_worker explicitly if you
    really need the legacy path.)
    """
    result = seed_cache.remote()
    print(f"seed_cache -> {result}")
