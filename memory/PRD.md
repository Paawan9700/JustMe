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
- M2: React UI
- M3–M6: Worker tasks (ingest, audio, diarize, snippets, render),
  Celery wiring, Redis + HF token

---

### M1: Job Lifecycle API — ✅ DONE (Jan 2026)
Implemented:
- **Models** (`backend/app/models/job.py`): JobCreateRequest, JobCreateResponse,
  SelectSpeakerRequest, SelectSpeakerResponse, JobResponse (with hydrated
  presigned URLs), JobProgress, JobError, SpeakerInfoResponse.
- **State machine** moved into `shared/constants.py` (`ALLOWED_TRANSITIONS`,
  `is_legal_transition`) — single source of truth for both API (async) and
  Worker (sync). Same-status updates are progress-only (no transition
  validation). ANY -> FAILED always legal.
- **job_service.py**: async CRUD against Motor + atomic transitions
  (`find_one_and_update` with CAS on current status):
    * `create_job(youtube_url)` -> doc with status=QUEUED, task_id=null
    * `set_task_id(job_id, task_id)`
    * `get_job_raw / get_job_hydrated` (hydrated injects snippet_urls when
      status >= AWAITING_SELECTION, download_url when status == DONE).
    * `transition_status / update_progress`
    * `select_speaker(job_id, label)` -> atomically sets
      `selected_speaker` AND moves AWAITING_SELECTION -> RENDERING.
- **queue.py**: Celery client; `enqueue_process_video`, `enqueue_render_video`
  by task NAME (API does not import worker code). Handles `rediss://` SSL
  param injection automatically.
- **api/jobs.py** (mounted under `/api/jobs`):
    * `POST /api/jobs`  -> 201 + {job_id, status}; URL validation (host
      allowlist for youtube.com / youtu.be / m.youtube.com / shorts /
      embed); rejects `/live/`.  Enqueues `process_video`. On enqueue
      failure marks job FAILED + returns 502.
    * `GET /api/jobs/{id}` -> 200 hydrated JobResponse, 404 if missing.
    * `POST /api/jobs/{id}/select-speaker` -> 200/RENDERING, 404, 409
      (wrong state), 400 (speaker not in job).
- **Worker** (`/app/worker`):
    * `celery_app.py` — loads `/app/backend/.env` if present; Celery
      pointed at Upstash with SSL param injection; task_acks_late + low
      prefetch.
    * `db.py` — sync pymongo helper.
    * `tasks/dummy.py` — `process_video` and `render_video` simulate the
      full pipeline with sleeps + atomic transitions. Each fake speaker
      has `snippet_key: null` per spec (snippet_url renders null too).
- **Backend entrypoint** (`backend/server.py`): inserts `/app` into
  `sys.path` so `shared` package is importable.

Verified end-to-end (curl):
- POST /api/jobs (valid) -> 201
- POST /api/jobs (vimeo) -> 400 "URL is not a YouTube URL"
- POST /api/jobs (/live/) -> 400 "Livestreams are not supported"
- GET /api/jobs/{id} -> watched status walk:
  QUEUED -> DOWNLOADING -> EXTRACTING_AUDIO -> DIARIZING ->
  GENERATING_SNIPPETS -> AWAITING_SELECTION -> RENDERING -> DONE
- POST select-speaker while DOWNLOADING -> **409** (key acceptance)
- POST select-speaker with bogus label -> **400** "Speaker SPEAKER_99 not found"
- POST select-speaker valid -> **200** + status=RENDERING
- POST select-speaker while RENDERING -> 409
- GET unknown id -> 404
- External preview URL (youtu.be shortlink) -> 201

Infrastructure:
- Upstash Redis URL stored in `/app/backend/.env`
  (`rediss://...giving-muskox-75960.upstash.io:6379`).
- Celery worker started locally as background process (NOT supervised —
  the supervisor config is read-only on Emergent and the production worker
  ships externally). Restart with:
    `cd /app && nohup /root/.venv/bin/celery -A worker.celery_app worker --loglevel=info --concurrency=2 > /var/log/justme/worker.log 2>&1 &`

Deferred (waiting on user to specify the milestone before building):
- M2: React UI

---

### M2: Frontend UI — ✅ DONE (Jan 2026)
Implemented:
- **Routing** (react-router-dom v6): `/` (Home), `/jobs/:jobId` (JobStatus),
  `*` (not-found).
- **API client** (`src/lib/api.js`): thin fetch wrapper around
  `${REACT_APP_BACKEND_URL}/api/*`, surfaces `detail` from FastAPI as the
  error message.
- **Home page** (`src/pages/Home.jsx`):
    * single URL input + "Get Started" button
    * client-side disabled state on empty/submitting
    * server error rendered inline (e.g. "URL is not a YouTube URL",
      "Livestreams are not supported")
    * on success: `navigate(/jobs/{job_id})`
- **JobStatus page** (`src/pages/JobStatus.jsx`):
    * polls `GET /api/jobs/{id}` every 3s; stops on DONE / FAILED / 404
    * also triggers an immediate poll right after select-speaker so the
      UI flips to RENDERING fast, not after up to 3s of latency
    * five visual states all implemented and verified via Playwright:
      - **A Processing**: stage tag, human-readable message per
        `STAGE_MESSAGE` map, animated `<ProgressBar percent>`, dismissable-
        tab hint
      - **B Awaiting Selection**: "We found N speakers..." heading, grid
        of `<SpeakerCard>` (one per speaker, label "Speaker 1" etc.
        derived from `SPEAKER_00`), audio player when `snippet_url` is
        present else "No preview available" placeholder, "This is me ✓"
        button with per-card loading state
      - **C Rendering**: spinner + "Cutting and stitching..." message
      - **D Done**: "Your video is ready! 🎉", optional stats
        ("Extracted X minutes of your speaking from a Y-hour video" —
        builds gracefully when duration_sec is 0 from dummy worker),
        Download Video button when `download_url` present else "will
        appear once the real renderer is wired (M6)" placeholder,
        "Process another video" link
      - **E Failed**: error title + `error.message`, "Try Again" button
        back to `/`
- **Design**: warm-neutral dark UI with a single amber accent (#d6a35a),
  monospace brand mark + ui-sans body, generous whitespace, mobile-
  friendly (single-column layout under 480px). No purple gradients, no
  generic fonts.
- **Test IDs**: every interactive element + critical UI element has a
  unique `data-testid` (per project rules).

Verified (Playwright E2E, full preview URL):
- Home renders, accepts URL.
- Invalid URL (`vimeo.com/123`) -> 400 -> red error box on Home.
- Valid URL -> 201 -> navigates to `/jobs/{id}`.
- Watched live status walk on dummy worker:
  QUEUED -> DOWNLOADING -> ... -> AWAITING_SELECTION (2 fake speakers
  with "Spoke for X minutes, N segments" meta).
- Clicked "This is me ✓" on SPEAKER_00 -> RENDERING -> DONE
  (with "Extracted 3 minutes of your speaking" stats; download disabled
  placeholder because dummy worker leaves final_video_key null).
- Manually set a job to FAILED in Mongo -> page renders State E with the
  exact `error.message`.
- 404 on bogus job id -> State "Job not found" with Back to home button.
- "Process another video" / "Try Again" / "Back to home" all return to `/`.

Frontend dependency additions: `react-router-dom@6`. Lint clean.

---

### Bug fix — URL validation (Jan 2026)
**Issue**: API was rejecting all URLs containing `/live/`, but YouTube uses
the same `/live/<id>` path for both ongoing AND completed past streams.
Completed streams are fully downloadable and must be allowed.
`https://www.youtube.com/live/THcrvo5Dz7M` (a completed past stream) was
incorrectly returning 400.

**Fix applied to `backend/app/api/jobs.py`**:
- Removed the blanket `/live/` URL-level rejection.
- `_YT_VIDEO_PATH` regex now accepts `/watch`, `/live/<id>`, `/embed/<id>`
  (Shorts removed from the allowed list).
- Added dedicated rejections for **playlists** (`path == /playlist` OR
  `list=` present in query string) and **Shorts** (`/shorts/<id>`).
- True is-live detection is now correctly deferred to M3 via yt-dlp's
  `is_live` metadata.

**Other touches**:
- `shared/constants.py` now exports `LIVE_STREAM_REJECT_MESSAGE` for M3
  to use when yt-dlp reports `is_live=True`:
  "This video is currently live. Please wait until the stream ends and
  try again."
- `worker/tasks/dummy.py` docstring now documents the M3 detection
  contract (is_live True / False+was_live).

**Verified** (curl): `/live/THcrvo5Dz7M` → 201; Shorts → 400 with new
message; `/playlist?list=` and `/watch?v=...&list=` → 400 with playlist
message; `/watch?v=...`, `/embed/...`, `youtu.be/...` all still 201; Vimeo
still 400 host check.

Deferred (waiting on user to specify the milestone before building):

## Next Action Items
- Wait for user to send Milestone 1.
