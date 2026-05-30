# JustMe.ai — PRD

## Problem Statement
A web app where the user pastes a long YouTube URL (up to 10–15h). The system
downloads the video, runs speaker diarization (WhisperX + pyannote.audio),
lets the user identify their own voice from short snippets, then cuts and
stitches a final video containing only that speaker's segments.

## Architecture
- **Hosted on Emergent**
  - React frontend (`/app/frontend`)
  - FastAPI backend (`/app/backend`, served by `server.py` -> `app.main:app`)
  - MongoDB (managed local — Emergent default)
- **External (user-deployed, NOT on Emergent)**
  - GPU Worker (`/app/worker`, Celery + CUDA Docker, code lives in repo but
    is deployed separately)
  - Cloudflare R2 (file storage, S3-compatible via boto3)
  - Upstash Redis (Celery broker / messaging between API and Worker)

API never touches large video files; the Worker handles all heavy lifting
and writes results to R2. API and Worker communicate via Redis only.

## Tech Stack
- Frontend: React 18 + react-scripts
- Backend: FastAPI 0.110, Motor 3.3, pydantic-settings 2.14, boto3 1.43
- DB: MongoDB (Motor async client)
- Queue: Celery + Redis (Upstash) — wired in M3+
- Storage: Cloudflare R2 (S3-compatible)
- Worker AI (future): yt-dlp, ffmpeg (raw, never MoviePy), WhisperX,
  pyannote.audio
- Worker container: CUDA-based Docker (built outside Emergent)

## Folder Layout (locked in M0)
```
/app
├── backend/
│   ├── server.py                 # supervisor entrypoint (re-exports app.main:app)
│   ├── .env
│   ├── requirements.txt
│   └── app/
│       ├── main.py               # FastAPI + /health
│       ├── core/config.py        # pydantic-settings
│       ├── db/mongo.py           # Motor client, indexes, ping
│       ├── models/job.py         # (M1)
│       ├── api/jobs.py           # (M1)
│       └── services/
│           ├── job_service.py    # (M1)
│           ├── storage.py        # R2 (boto3)
│           └── queue.py          # (M3+)
├── frontend/                     # placeholder UI (real UI in M2)
├── worker/                       # NOT hosted on Emergent
│   ├── celery_app.py             # stub
│   ├── tasks/{ingest,audio,diarize,snippets,render}.py
│   ├── utils/{ffmpeg,storage}.py
│   └── Dockerfile                # placeholder
└── shared/constants.py           # JobStatus enum + R2 key helpers
```

## Build Rules (from user)
1. Ask when unsure — no assumptions.
2. Ask for credentials, never hardcode fakes.
3. **Never use MoviePy.** Raw ffmpeg only.
4. One milestone at a time. User provides each milestone explicitly.

---

## Milestones — Status

### M0: Foundations — ✅ DONE (Jan 2026)
Implemented:
- Folder scaffold for backend/frontend/worker/shared (matches spec).
- `shared/constants.py`: `JobStatus` enum (QUEUED, DOWNLOADING,
  EXTRACTING_AUDIO, DIARIZING, GENERATING_SNIPPETS, AWAITING_SELECTION,
  RENDERING, DONE, FAILED) and R2 key helpers
  (`r2_key_source_video`, `r2_key_audio`, `r2_key_snippet`,
  `r2_key_final_video`).
- `backend/app/core/config.py`: pydantic-settings reading MONGO_URL (aliased
  to `mongo_uri`), DB_NAME, R2_* (all four), REDIS_URL/HF_TOKEN (optional),
  MAX_VIDEO_HOURS (default 15).
- `backend/app/db/mongo.py`: Motor client; `init_db()` creates indexes
  (`jobs.job_id` unique, `jobs.status`, `jobs.created_at`,
  `segments.{job_id, speaker}`); `ping()` for /health.
- `backend/app/services/storage.py`: `R2Storage` with `upload_file`,
  `download_file`, `get_presigned_url`, `file_exists`, `ping` (head_bucket
  for /health).
- `backend/app/main.py`: FastAPI + lifespan -> `init_db()`; CORS; `/health`
  and `/api/health` returning `{status, mongo, r2}`.
- Frontend: minimal placeholder so supervisor stays green; real UI deferred
  to M2.
- Worker: empty module stubs only (deployed externally later).

Verified:
- `curl http://localhost:8001/health` → 200 `{"status":"ok","mongo":"connected","r2":"connected"}`
- `curl https://<preview-url>/api/health` → 200, same payload.
- Ruff lint clean across `/app/backend/app` and `/app/shared`.

Credentials supplied by user (stored in `/app/backend/.env`):
- R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME=`justme-r2bucket`,
  R2_ENDPOINT_URL (corrected to base endpoint — user originally appended
  the bucket path, which boto3 doesn't expect).

Deferred (waiting on user to specify the milestone before building):
- M1: Job creation/status API endpoints
- M2: React UI
- M3–M6: Worker tasks (ingest, audio, diarize, snippets, render),
  Celery wiring, Redis + HF token

## Next Action Items
- Wait for user to send Milestone 1.
