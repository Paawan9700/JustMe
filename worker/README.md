# JustMe — Worker Deployment

The worker is the GPU-bound half of JustMe. It runs WhisperX +
pyannote.audio for diarization, plus ffmpeg for ingest / audio /
snippet / render — all of which need either a GPU or a beefy CPU
machine. It does **not** run on the Emergent preview environment;
deploy it separately on Modal or RunPod.

---

## Architecture recap

```
[Emergent: React + FastAPI + MongoDB]
                |
                | Celery messages (job_id) over Upstash Redis (TLS)
                v
[YOUR GPU BOX:  worker/celery_app.py  (this folder)]
                |
                | reads/writes large files
                v
[Cloudflare R2 (S3-compatible)]
```

The worker:
1. Picks up `process_video` / `render_video` tasks from Upstash Redis.
2. Reads job docs from the same MongoDB the API writes to.
3. Reads/writes large artifacts (source.mp4, audio.wav, snippets, final.mp4) in R2.
4. Logs progress back to the job doc; the React frontend polls
   `GET /api/jobs/{id}` and renders state.

---

## 0. Prerequisites (one-time, before either platform)

You need accounts + credentials for **all** of these:

| Service      | What you need                                                                                              |
|--------------|------------------------------------------------------------------------------------------------------------|
| MongoDB      | `MONGO_URL` (atlas SRV string is easiest) + `DB_NAME` (e.g. `justme`)                                       |
| Upstash      | Redis `REDIS_URL` (starts with `rediss://default:...@<host>.upstash.io:6379`)                              |
| Cloudflare R2| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`, `R2_ENDPOINT_URL` (the **base** account URL)  |
| HuggingFace  | `HF_TOKEN` (read scope)  AND  license accepted on **both** of the gated models below                       |

### HuggingFace setup is the gotcha — do it carefully
The pyannote 3.1 pipeline depends on `pyannote/segmentation-3.0`. You
**must** accept **both** licenses on the same account, otherwise the
worker fails at model-load with HTTP 403.

1. Sign in / sign up at <https://huggingface.co>.
2. Visit <https://huggingface.co/pyannote/speaker-diarization-3.1> →
   fill in the access form → "Agree and access repository".
3. Visit <https://huggingface.co/pyannote/segmentation-3.0> → same.
4. Go to <https://huggingface.co/settings/tokens> → create a token →
   **Type: read** → copy it (starts with `hf_`).
5. Quick check from any shell with curl installed:
   ```bash
   HF=hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   for m in pyannote/speaker-diarization-3.1 pyannote/segmentation-3.0; do
     curl -sL -o /tmp/x.bin -w "$m -> %{http_code}\n" \
       -H "Authorization: Bearer $HF" \
       "https://huggingface.co/$m/resolve/main/config.yaml"
   done
   ```
   Both lines should print `200`. A `403` means the license isn't
   accepted on this account.

### Recommended specs
| Resource       | Minimum               | Comfortable                |
|----------------|-----------------------|----------------------------|
| GPU            | NVIDIA T4 (16 GB)     | RTX 4090 / A40 / A10G       |
| GPU VRAM       | 12 GB                 | 16 GB+                      |
| System RAM     | 16 GB                 | 32 GB                       |
| Disk           | 50 GB                 | 100 GB                      |
| Network out    | 100 Mbps              | 1 Gbps+ (long downloads)    |

The pipeline peaks at ~10 GB GPU VRAM on a 60-minute video; concurrency
is intentionally pinned to 1 to keep one job per GPU at a time.

---

## A. Deploy to **Modal** (recommended for MVP)

Modal is serverless — pay per second, no Docker push. Files in this
folder are already wired up via `worker/modal_app.py`.

### 1. Install the CLI and authenticate

```bash
pip install modal
modal token new       # opens browser, generates a token
```

### 2. Create the Modal secret

This is one command — Modal injects all of these as env vars into the
worker container at runtime.

```bash
modal secret create justme-secrets \
  MONGO_URL='mongodb+srv://USER:PASS@cluster.mongodb.net' \
  DB_NAME='justme' \
  REDIS_URL='rediss://default:AAA@host.upstash.io:6379' \
  R2_ACCESS_KEY_ID='xxxx' \
  R2_SECRET_ACCESS_KEY='xxxx' \
  R2_BUCKET_NAME='justme-r2bucket' \
  R2_ENDPOINT_URL='https://ACCOUNTID.r2.cloudflarestorage.com' \
  HF_TOKEN='hf_xxxx' \
  MAX_VIDEO_HOURS='15'
```

> Note: do **not** include the bucket name at the end of
> `R2_ENDPOINT_URL` — boto3 takes the bucket as a separate argument
> on every call.

### 3. Deploy

From the **project root** (so `modal_app.py` can see both `worker/`
and `shared/`):

```bash
modal deploy worker/modal_app.py
```

You'll see the build run once (apt → pip → whisperx). On success
Modal prints the app URL. The worker is now warm and consuming the
Redis queue.

### 4. Test it

Submit a real video via your deployed frontend (or `curl POST
/api/jobs`) and watch Modal's dashboard:

- <https://modal.com/apps> → `justme-worker` → "Run history"
- Logs in real-time: `modal app logs justme-worker`

### 5. Scale tweaks

Edit `worker/modal_app.py`:

| Want                        | Change in `@app.function(...)`                |
|-----------------------------|------------------------------------------------|
| Scale to zero off-hours     | `keep_warm=0` (cold-start adds ~30s on first job) |
| Faster GPU (paid more)      | `gpu="A100"` instead of `"A10G"`               |
| More RAM for long videos    | `memory=32768`                                  |

Re-run `modal deploy worker/modal_app.py` after any change.

---

## B. Deploy to **RunPod** (always-on, cheaper at scale)

### 1. Build the image

The Dockerfile expects the **project root** as build context (so both
`worker/` and `shared/` get baked in):

```bash
cd /path/to/project-root
docker build -f worker/Dockerfile -t YOURUSER/justme-worker:latest .
```

(First build is slow — pytorch base + whisperx git install. Subsequent
builds reuse the layer cache.)

### 2. Push to Docker Hub (or GHCR)

```bash
docker login
docker push YOURUSER/justme-worker:latest
```

### 3. Create a RunPod pod

1. <https://runpod.io/console/pods> → **Deploy** → **GPU Pod**.
2. **GPU**: RTX 4090 (recommended) or A40 / A10G.
3. **Container image**: `YOURUSER/justme-worker:latest`.
4. **Container disk**: 50 GB minimum (the pytorch image alone is ~7 GB).
5. **Volume**: optional 100 GB volume mounted at `/data` if you want
   persistence between restarts (not required — all artifacts live in R2).
6. **Expose ports**: leave defaults; no ports needed (Redis is outbound only).
7. **Environment variables**: paste each of:
   - `MONGO_URL`
   - `DB_NAME` = `justme`
   - `REDIS_URL`
   - `R2_ACCESS_KEY_ID`
   - `R2_SECRET_ACCESS_KEY`
   - `R2_BUCKET_NAME`
   - `R2_ENDPOINT_URL`
   - `HF_TOKEN`
   - `MAX_VIDEO_HOURS` = `15`
8. **Container start command**: leave empty — the Dockerfile's `CMD`
   already runs `celery -A worker.celery_app worker --loglevel=info
   --concurrency=1`.
9. Click **Deploy**.

### 4. Verify

In the pod's **Logs** tab you should see celery's startup banner ending
with `celery@... ready.` and both task names listed (`process_video`,
`render_video`). Submit a job and watch a new task pop up.

### 5. Cost tip

The pod runs 24/7. For low-traffic apps, RunPod's **spot** pods are
~50–70% cheaper at the cost of occasional preemption; Celery's
`task_acks_late=True` makes any preempted job re-deliver to the next
worker automatically.

---

## Environment variables reference

| Name                   | Used by             | Example / notes                                                |
|------------------------|---------------------|----------------------------------------------------------------|
| `MONGO_URL`            | API + worker        | `mongodb+srv://user:pass@cluster.mongodb.net`                  |
| `DB_NAME`              | API + worker        | `justme`                                                       |
| `REDIS_URL`            | API + worker        | `rediss://default:TOKEN@host.upstash.io:6379` (TLS!)           |
| `R2_ACCESS_KEY_ID`     | API + worker        | from Cloudflare dashboard                                      |
| `R2_SECRET_ACCESS_KEY` | API + worker        | from Cloudflare dashboard                                      |
| `R2_BUCKET_NAME`       | API + worker        | `justme-r2bucket`                                              |
| `R2_ENDPOINT_URL`      | API + worker        | `https://ACCOUNTID.r2.cloudflarestorage.com` (no trailing path)|
| `HF_TOKEN`             | worker (M4 only)    | `hf_...`                                                       |
| `MAX_VIDEO_HOURS`      | API + worker        | `15`                                                           |

---

## R2 lifecycle rule (one-time, in the Cloudflare dashboard)

Job artifacts (source.mp4, audio.wav, snippets/*.mp3, final.mp4) live
under the `jobs/` prefix. Set an expiry so storage costs stay bounded:

1. <https://dash.cloudflare.com> → **R2** → your bucket → **Settings**.
2. **Object Lifecycle Rules** → **Add rule**.
3. Name: `Delete old job artifacts` · Prefix: `jobs/` ·
   Action: `Delete objects` · Age: `7 days` · Status: `Enabled`.

The API and worker do **not** delete from R2 themselves — lifecycle
rules are the canonical, retry-safe mechanism.

---

## End-to-end test checklist (post-deploy)

Once the worker is running on Modal or RunPod, run this against your
deployed frontend (e.g. `https://<your-app>.preview.emergentagent.com`):

1. **Submit a 5–10 min YouTube URL with 2+ speakers.** Past examples
   that work well: any interview from "Diary of a CEO" or "Lex
   Fridman Podcast" clipped to 10 min.
2. **Watch `DOWNLOADING` progress** climb 0→100% with messages like
   `"Downloading... 45%"` (yt-dlp progress hook is throttled to
   every 5%/2s).
3. **Watch `EXTRACTING_AUDIO`** flash by (a few seconds).
4. **Watch `DIARIZING`**. First-time model load is the slow part
   (~30-60s on cold cache); subsequent jobs are ~1× realtime on an
   A10G (10 min video → ~10 min diarize).
5. **`AWAITING_SELECTION` appears with real speaker cards**, each
   playing a 6-second mp3 sample directly from R2.
6. **Click "This is me ✓"** on your voice → status flips to
   `RENDERING` within ~3 seconds.
7. **Watch `RENDERING`** — usually 30s–2min depending on how many cut
   segments and how long each is.
8. **`DONE` state appears** with a working **Download Video** button.
   Clicking it streams `final.mp4` from R2 via a presigned URL valid
   for 1 hour.
9. **Open the downloaded file** — verify it contains only your voice,
   with audio + video in sync, and the total duration roughly equals
   the sum of the AWAITING_SELECTION screen's "Spoke for X minutes"
   number for that speaker.

If the job ends up in **FAILED** at any stage, the on-page error
message tells you what went wrong (e.g. "This video is currently
live", "HuggingFace access denied — accept both licenses...", etc.).
Common one-time fixes:
- **HF_ACCESS_DENIED**: revisit step 0 above and accept the
  `pyannote/segmentation-3.0` license too.
- **DOWNLOAD_FAILED with 403**: your GPU box's IP got rate-limited by
  YouTube; use a different region/pool or supply `--cookies` (advanced).
- **TIMEOUT**: video is just very long; bump `task_soft_time_limit` in
  `worker/celery_app.py` if 2 hours genuinely isn't enough.

---

## Troubleshooting

| Symptom                                  | Likely cause                                                  | Fix                                                       |
|------------------------------------------|---------------------------------------------------------------|-----------------------------------------------------------|
| Job stuck in `QUEUED`                    | Worker not running OR Redis URL mismatch between API & worker | Modal: `modal app logs justme-worker` · RunPod: pod Logs   |
| `MISSING_DEPS` in `error.code`           | The container doesn't have whisperx (CPU/dev box)             | Use the production Dockerfile / Modal image               |
| `HF_ACCESS_DENIED`                       | Pyannote license not accepted                                 | See "HuggingFace setup" above — both models               |
| Snippets play but final.mp4 is silent    | Stream-copy concat keyframe misalignment (rare)               | Switch render.py final concat to `-c:v libx264 -c:a aac`  |
| Out-of-memory at diarize                 | Long video on small GPU                                       | Bigger GPU (A40/A100) or smaller Whisper model            |
