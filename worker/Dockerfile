# JustMe Worker — production GPU container.
#
# Build from the PROJECT ROOT so both `worker/` and `shared/` ship into
# the image (the worker imports `shared.constants`):
#
#   docker build -f worker/Dockerfile -t justme-worker .
#
# Run with the env vars from worker/.env.example (MONGO_URL, DB_NAME,
# REDIS_URL, R2_*, HF_TOKEN, MAX_VIDEO_HOURS):
#
#   docker run --rm --gpus all --env-file worker/.env justme-worker
#
# Notes:
# - Base image already ships torch + CUDA. requirements.txt MUST NOT
#   pin torch or you'll risk a CUDA mismatch.
# - concurrency=1 because each job loads multi-GB models onto a single
#   GPU; running two in parallel would OOM.

FROM pytorch/pytorch:2.1.0-cuda11.8-cudnn8-runtime

# OS-level deps
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps — copy requirements first for better layer caching
COPY worker/requirements.txt /app/worker/requirements.txt
RUN pip install --no-cache-dir -r /app/worker/requirements.txt

# WhisperX lives on git head (PyPI lags); install separately so this
# layer doesn't bust on every requirements.txt change.
RUN pip install --no-cache-dir "git+https://github.com/m-bain/whisperX.git"

# Source code: both worker/ and shared/ are required.
COPY worker/ /app/worker/
COPY shared/ /app/shared/

# Celery's `-A` argument is a module path. /app must be on PYTHONPATH
# so `worker.celery_app` resolves; WORKDIR=/app handles that implicitly
# for Python imports.
CMD ["celery", "-A", "worker.celery_app", "worker", "--loglevel=info", "--concurrency=1"]
