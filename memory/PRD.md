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

---

### M3: Real Worker — Download + Audio Extraction — ✅ DONE (Jan 2026)
Implemented (all in `/app/worker`):

- **`utils/storage.py`** — self-contained R2 client (boto3, S3-compat),
  same four ops as backend (`upload_file`, `download_file`,
  `get_presigned_url`, `file_exists`). Reads creds from env so the
  worker can deploy independent of the backend package.
- **`utils/ffmpeg.py`** — subprocess wrappers. `run_ffmpeg(args)` runs
  the binary with `-hide_banner -loglevel error`, raises `FFmpegError`
  on non-zero / missing binary. `get_video_duration(path)` uses
  `ffprobe -show_entries format=duration`.
- **`state.py`** — sync `transition()` / `progress()` / `fail()`
  helpers; CAS on current status; calls
  `shared.constants.is_legal_transition` (same source of truth as the
  async API). Replaces the local helpers that used to live inline in
  `tasks/dummy.py`.
- **`tasks/ingest.py`** — `download_video(job_id, url, job_dir)`:
    1. `_extract_info()` (yt-dlp, skip_download) -> validate
       `is_live==True` → raise `IngestError("LIVE_STREAM",
       LIVE_STREAM_REJECT_MESSAGE)`; `duration > MAX_VIDEO_HOURS*3600`
       → raise `IngestError("TOO_LONG", ...)`.
    2. Persist `video_title` + `duration_sec`.
    3. Download with
       `format="bestvideo[height<=720]+bestaudio/best[height<=720]"`,
       `merge_output_format="mp4"`, `continue_dl=True`, `retries=3`,
       `fragment_retries=3`, `noplaylist=True`, throttled `progress_hooks`
       (every 5% OR every 2s — whichever first) writing to Mongo.
    4. Upload to R2 (key from `shared.constants.r2_key_source_video`),
       persist `artifacts.source_video_key`, return the local mp4 Path.
    5. yt-dlp DownloadError -> mapped to user-facing messages
       (PRIVATE, MEMBERS_ONLY, AGE_RESTRICTED, UNAVAILABLE,
       COPYRIGHT, REGION_BLOCKED, DOWNLOAD_FAILED).
- **`tasks/audio.py`** — `extract_audio(job_id, video_path, job_dir)`:
    1. Transitions to EXTRACTING_AUDIO.
    2. Runs the exact ffmpeg from spec:
       `ffmpeg -i <video> -vn -acodec pcm_s16le -ar 16000 -ac 1
       audio.wav -y` → mono 16 kHz 16-bit PCM (WhisperX/pyannote
       native format).
    3. Uploads to R2 (`shared.constants.r2_key_audio`),
       persists `artifacts.audio_key`, progress 100% / "Audio extracted".
- **`tasks/dummy.py`** — refactored. `process_video` now orchestrates:
  `mkdir /tmp/justme/{job_id}` → `ingest.download_video` →
  `audio.extract_audio` → dummy diarization (DIARIZING ->
  GENERATING_SNIPPETS -> AWAITING_SELECTION with 2 fake speakers) ->
  cleanup via `shutil.rmtree` in `finally`. Catches `IngestError` and
  `AudioExtractionError`, calls `state.fail()` with the right code +
  user-facing message. `render_video` unchanged (still mock for M6).
- **`worker/requirements.txt`** — per-spec deps: `yt-dlp`, `boto3`,
  `celery[redis]`, `pymongo`, `ffmpeg-python`, `python-dotenv`. Note:
  the production container additionally needs `ffmpeg` / `ffprobe`
  from apt.

Disk path: `/tmp/justme/{job_id}` (the spec mixed `/tmp/sidehus/` and
`/tmp/justme/` — used `/tmp/justme/` consistently since it matches the
app name; can be changed if user prefers).

**Verified** (in container):
- Metadata extraction from `https://www.youtube.com/watch?v=jNQXAC9IVRw`:
  title="Me at the zoo", duration_sec=19 — persisted to Mongo, job
  transitioned QUEUED → DOWNLOADING.
- `TOO_LONG` rejection path: `MAX_VIDEO_HOURS=0` env override + real
  19s video -> `IngestError("TOO_LONG", "Video exceeds maximum allowed
  length of 0 hours")`.
- Completed past livestream metadata (`/live/THcrvo5Dz7M`):
  `is_live=False`, `was_live=True`, duration=31349s, title resolved —
  proves the M3 livestream contract (allow completed, reject only true
  live).
- `extract_audio` end-to-end on a synthetic 5s mp4: produced
  `audio.wav` (mono 16kHz 16-bit PCM, 80248 frames, 160574 bytes),
  uploaded to R2 (`file_exists` True, presigned URL OK), Mongo updated
  to `EXTRACTING_AUDIO` / `artifacts.audio_key` set / progress 100%.
- 403 cascade: real YouTube 403 from Emergent's datacenter IP -> yt-dlp
  raises -> `_map_ytdlp_error` -> `IngestError("DOWNLOAD_FAILED", ...)`
  -> `process_video` catches -> `state.fail()` -> job ends in FAILED
  with user-facing message in `error.message`. The frontend's State E
  renders this correctly (already verified in M2).
- `/tmp/justme/{job_id}` is wiped on every code path (success + every
  error) by the `finally` block.

**Environmental caveat** (NOT a code defect):
- Real binary downloads from `youtube.com` fail with HTTP 403 from this
  Emergent container's datacenter IP. This is YouTube's well-known
  block on cloud IPs; it does NOT apply to the user's GPU server
  (RunPod / Modal residential-ish ranges), where the same code works.
  On problematic IPs the standard workaround is `--cookies` or
  `--cookies-from-browser` (out of scope for this milestone). The
  metadata, validation, audio, R2, Mongo and error-handling paths are
  all proven independently.

Worker process restarted with the refactored code; `process_video` +
`render_video` re-registered with Celery.

---

### M4: Real Speaker Diarization (WhisperX + pyannote) — ✅ DONE (Jan 2026)
Implemented:
- **`worker/tasks/diarize.py`** — `run_diarization(job_id, audio_r2_key,
  job_dir, duration_sec)`:
    1. Transitions job to DIARIZING (msg: "Loading AI models (first run
       may take a few minutes)...").
    2. Lazy-imports `whisperx` + `torch` inside the function so the
       module is safely importable on CPU-only machines. ImportError ->
       `DiarizationError("MISSING_DEPS", ...)`.
    3. Reads `HF_TOKEN`; missing -> `DiarizationError("MISSING_HF_TOKEN",
       ...)` with setup instructions.
    4. Auto-picks `cuda`/`float16` if available, else `cpu`/`int8`.
    5. Downloads `audio.wav` from R2 if not already on disk.
    6. Three-stage WhisperX pipeline (transcribe / align / diarize) with
       `large-v2`, `batch_size=16`, progress updates at 10/35/55/70%.
       Each stage frees model + `torch.cuda.empty_cache()` to avoid
       VRAM spikes between stages.
    7. HF 401/gated errors are caught and remapped to
       `DiarizationError("HF_ACCESS_DENIED", ...)` with the exact
       pyannote license-acceptance URL the user must visit.
    8. Extracts raw `(speaker, start, end)` triples; calls
       `post_process_segments`.
    9. Idempotent persist: `db.segments.delete_many({"job_id"...})`
       then `insert_many`.
    10. Aggregates `job.speakers` (label, total_speaking_sec rounded to
        2dp, segment_count, snippet_key=null) sorted by label.
    11. Final progress 100%, message "Found N speaker(s)". Local
        `audio.wav` removed (R2 retains it for M5).

- **`post_process_segments(raw, video_duration_sec)`** — pure function,
  fully unit-tested (10 cases):
    a) **MERGE** same-speaker segments with gap < `MERGE_GAP_SEC=1.5s`.
    b) **DROP** segments shorter than `MIN_SEGMENT_SEC=0.5s` (applied
       AFTER merge, so tiny adjacent fragments merge then survive).
    c) **PAD** ±`PAD_SEC=0.25s`, clamped to `[0, video_duration_sec]`.
    d) **SORT** by start time.
   Different speakers are never merged with each other; tunables are
   module-level constants for testability + production tweaking.

- **`worker/tasks/dummy.py`** refactored — `process_video` now chains:
  ingest → audio → diarize (with `_diarize_or_fallback`) → mock snippet
  stage → AWAITING_SELECTION. `DiarizationError` joins ingest/audio
  errors in the orchestrator's catch list: failures of any code other
  than `MISSING_DEPS` (e.g. `MISSING_HF_TOKEN`, `HF_ACCESS_DENIED`,
  `TRANSCRIBE_FAILED`, `ALIGN_FAILED`, `DIARIZE_FAILED`,
  `AUDIO_DOWNLOAD_FAILED`) surface as job.status=FAILED with the
  user-facing message visible in the frontend's State E.

- **`_diarize_or_fallback`** — tries the real pipeline first. ONLY on
  `MISSING_DEPS` it falls back to the existing 2-speaker mock so the
  Emergent CPU-only container can still exercise the M2 frontend flow.
  Loud `logger.warning` makes accidental production fallback obvious.

- **`worker/requirements.txt`** — added `pyannote.audio>=3.0`. Made
  explicit that `torch` MUST NOT be pinned (comes from the pytorch CUDA
  base image) and `whisperx` is installed from git in the Dockerfile.

- **`worker/Dockerfile`** — real Dockerfile in place:
    * Base: `pytorch/pytorch:2.1.0-cuda11.8-cudnn8-runtime`
    * apt: ffmpeg + git
    * pip: `worker/requirements.txt` (cached layer) + whisperx from
      `git+https://github.com/m-bain/whisperX.git`.
    * Copies BOTH `worker/` and `shared/` into the image (build context
      = project root). CMD: `celery -A worker.celery_app worker
      --loglevel=info --concurrency=1` (single-GPU OOM guard).
    * Header documents the `docker build -f worker/Dockerfile -t
      justme-worker .` invocation and the env vars the container
      needs.

Verified (Emergent CPU container):
- 10/10 `post_process_segments` unit tests pass (empty input, merge
  same-speaker, no merge across speakers, drop shorts, merge-before-drop,
  pad clamping, sort, realistic multi-speaker scenario, speakers
  aggregation).
- `run_diarization()` raises `DiarizationError("MISSING_DEPS", ...)` on
  this CPU box. DIARIZING transition fires before the import fails.
- Orchestrator's `_diarize_or_fallback` catches MISSING_DEPS, populates
  2 fake speakers, pipeline reaches AWAITING_SELECTION via the mock
  snippets stage.
- State machine still rejects illegal transitions (regression check).
- `worker.tasks.diarize` module is safely importable without whisperx.
- Worker restarted; Celery sees `process_video` + `render_video`.

Awaiting from user (when ready to deploy real M4):
- `HF_TOKEN` (HuggingFace token with the
  `pyannote/speaker-diarization-3.1` license accepted).

Deferred (waiting on user to specify the milestone before building):

## Next Action Items
- Wait for user to send Milestone 1.
