"""
Modal deployment wrapper for the JustMe Celery worker.

This file packages the existing worker (worker/celery_app.py +
worker/tasks/*) into a Modal app that runs the long-lived Celery worker
process on a GPU container.

Why not rewrite everything as @modal.function calls?
The user-facing pipeline (API -> Redis -> Celery worker) was finalised
in M1-M7. Switching dispatch from Celery to Modal Functions would mean
rewriting the API's services/queue.py and every job-status flow.
Wrapping Celery preserves the architecture and gives us GPU on Modal
with one file.

Deploy:
    pip install modal
    modal token new
    modal secret create justme-secrets \\
        MONGO_URL=...  DB_NAME=justme  REDIS_URL=rediss://default:...@...:6379 \\
        R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... \\
        R2_BUCKET_NAME=... R2_ENDPOINT_URL=https://<acct>.r2.cloudflarestorage.com \\
        HF_TOKEN=hf_... MAX_VIDEO_HOURS=15
    modal deploy worker/modal_app.py

Modal will keep one container warm (keep_warm=1) so the celery worker
is always available to drain the Upstash Redis queue. To temporarily
scale to zero (e.g. during off-hours), set keep_warm=0 here and redeploy.
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
        "apt-get install -y --no-install-recommends ffmpeg git tzdata && "
        "rm -rf /var/lib/apt/lists/*"
    )
    .pip_install_from_requirements(str(_REQUIREMENTS))
    .pip_install("git+https://github.com/m-bain/whisperX.git")
    # Bake the worker + shared packages into the image. Modal needs the
    # absolute repo paths so the image gets fresh copies on every deploy.
    .workdir("/app")
    .add_local_dir(str(_REPO_ROOT / "worker"), remote_path="/app/worker")
    .add_local_dir(str(_REPO_ROOT / "shared"), remote_path="/app/shared")
)

app = modal.App("justme-worker", image=image)


@app.function(
    secrets=[modal.Secret.from_name("justme-secrets")],
    gpu="A10G",            # WhisperX large-v2 fits in ~10 GB VRAM
    cpu=4.0,
    memory=16384,          # 16 GB RAM
    timeout=7800,          # 2h10m — matches worker/celery_app.py hard limit
    min_containers=1,           # always 1 instance up to drain the queue
    # @modal.concurrent=1,  # one Modal invocation; celery handles in-process concurrency
)
def run_worker() -> None:
    """
    Long-running entrypoint. Modal wakes this up, then `celery worker`
    blocks indefinitely consuming jobs from Upstash Redis.
    """
    import os
    import subprocess
    import sys

    # Sanity check — fail loud at startup if anything required is missing.
    required = [
        "MONGO_URL", "DB_NAME", "REDIS_URL",
        "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET_NAME", "R2_ENDPOINT_URL",
        "HF_TOKEN",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            f"Missing required env vars from `justme-secrets`: {missing}. "
            "Update with `modal secret create justme-secrets ...` and redeploy."
        )

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
    """Run locally for smoke testing: `modal run worker/modal_app.py`."""
    run_worker.remote()
