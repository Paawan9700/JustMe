import React, { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { motion, AnimatePresence } from "framer-motion";
import {
  Download,
  FileText,
  Sparkles,
  RefreshCw,
  Loader2,
  CheckCircle2,
  AlertTriangle,
  XCircle,
  ArrowLeft,
  Home as HomeIcon,
  Film,
  Check,
  Clock,
  BrainCircuit,
} from "lucide-react";
import { getJob, selectSpeaker, generateRecommendations } from "../lib/api";
import ProgressBar from "../components/ProgressBar";
import SpeakerCard from "../components/SpeakerCard";

const POLL_MS = 3000;

const STAGE_MESSAGE = {
  QUEUED: "Waiting to start...",
  DOWNLOADING: "Downloading video...",
  EXTRACTING_AUDIO: "Extracting audio...",
  DIARIZING: "Detecting speakers with AI (this takes a while)...",
  GENERATING_SNIPPETS: "Preparing speaker samples...",
};

const PROCESSING_STATES = new Set([
  "QUEUED", "DOWNLOADING", "EXTRACTING_AUDIO", "DIARIZING", "GENERATING_SNIPPETS",
]);

// Visual pipeline for the tracking panel. GENERATING_SNIPPETS shares the
// "Detecting speakers" phase. Index used to mark steps done / active / pending.
const PIPELINE = [
  { key: "QUEUED", label: "Queued" },
  { key: "DOWNLOADING", label: "Downloading" },
  { key: "EXTRACTING_AUDIO", label: "Extracting audio" },
  { key: "DIARIZING", label: "Detecting speakers" },
  { key: "AWAITING_SELECTION", label: "Select your voice" },
  { key: "RENDERING", label: "Rendering cut" },
  { key: "DONE", label: "Ready" },
];
const PHASE_ORDER = {
  QUEUED: 0, DOWNLOADING: 1, EXTRACTING_AUDIO: 2, DIARIZING: 3,
  GENERATING_SNIPPETS: 3, AWAITING_SELECTION: 4, RENDERING: 5, DONE: 6,
};

export default function JobStatus() {
  const { jobId } = useParams();
  const navigate = useNavigate();

  const [job, setJob] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [selecting, setSelecting] = useState(null); // label currently being selected
  const [selectError, setSelectError] = useState(null);
  const [generating, setGenerating] = useState(false); // recommendations in flight
  const [genError, setGenError] = useState(null);
  const timerRef = useRef(null);
  const tickRef = useRef(null);
  const recsTimerRef = useRef(null);

  // ---- polling loop -----------------------------------------------------
  // We stop polling on AWAITING_SELECTION (in addition to DONE/FAILED):
  // nothing changes there until the user picks a speaker, so polling would
  // just churn the network — every tick mints a NEW presigned snippet URL
  // server-side and re-renders the cards. onSelectSpeaker restarts polling
  // once the user acts (the job moves to RENDERING).
  useEffect(() => {
    let cancelled = false;

    async function tick() {
      try {
        const data = await getJob(jobId);
        if (cancelled) return;
        setJob(data);
        setLoadError(null);
        if (
          data.status === "AWAITING_SELECTION" ||
          data.status === "DONE" ||
          data.status === "FAILED"
        ) {
          if (timerRef.current) {
            clearInterval(timerRef.current);
            timerRef.current = null;
          }
        }
      } catch (err) {
        if (cancelled) return;
        setLoadError(err);
        if (err.status === 404 && timerRef.current) {
          clearInterval(timerRef.current);
          timerRef.current = null;
        }
      }
    }

    tickRef.current = tick;
    tick();
    timerRef.current = setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [jobId]);

  // ---- speaker selection -----------------------------------------------
  async function onSelectSpeaker(label) {
    if (selecting) return;
    setSelectError(null);
    setSelecting(label);
    try {
      await selectSpeaker(jobId, label);
      // Trigger an immediate poll so the UI flips to RENDERING quickly,
      // instead of waiting up to 3s for the next interval tick.
      try {
        const data = await getJob(jobId);
        setJob(data);
      } catch { /* polling will catch up */ }
      // Polling was stopped when we entered AWAITING_SELECTION. The job is
      // now RENDERING, so resume it to track render -> DONE.
      if (!timerRef.current && tickRef.current) {
        timerRef.current = setInterval(tickRef.current, POLL_MS);
      }
    } catch (err) {
      setSelectError(err.message || "Failed to select speaker");
      setSelecting(null);
    }
  }

  // ---- stock recommendations -------------------------------------------
  // The job stays DONE while recommendations generate, so the main poller
  // (which stops at DONE) won't track this. We run a dedicated poll loop
  // that ends as soon as recommendations_status leaves GENERATING.
  useEffect(() => () => {
    if (recsTimerRef.current) clearInterval(recsTimerRef.current);
  }, []);

  async function pollRecs() {
    try {
      const data = await getJob(jobId);
      setJob(data);
      if (data.recommendations_status !== "GENERATING") {
        clearInterval(recsTimerRef.current);
        recsTimerRef.current = null;
        setGenerating(false);
      }
    } catch (err) {
      clearInterval(recsTimerRef.current);
      recsTimerRef.current = null;
      setGenerating(false);
      setGenError(err.message || "Failed to check recommendation status");
    }
  }

  async function onGenerateRecommendations() {
    if (generating) return;
    setGenError(null);
    setGenerating(true);
    try {
      await generateRecommendations(jobId);
      // Immediate poll so the UI reflects GENERATING without waiting POLL_MS.
      const data = await getJob(jobId);
      setJob(data);
      if (data.recommendations_status === "GENERATING" && !recsTimerRef.current) {
        recsTimerRef.current = setInterval(pollRecs, POLL_MS);
      } else {
        setGenerating(false);
      }
    } catch (err) {
      setGenError(err.message || "Failed to start generation");
      setGenerating(false);
    }
  }

  // ---- render: load / error states -------------------------------------
  if (!job && !loadError) {
    return (
      <div
        className="flex min-h-[60vh] flex-col items-center justify-center gap-4 text-slate-500"
        data-testid="job-loading"
      >
        <Loader2 className="h-7 w-7 animate-spin text-accent-soft" />
        <span className="font-mono text-sm">Loading job…</span>
      </div>
    );
  }
  if (loadError && loadError.status === 404) {
    return (
      <main className="mx-auto w-full max-w-5xl flex-1 px-5 py-12 sm:px-8" data-testid="job-not-found">
        <div className="glass mx-auto max-w-xl p-8 text-center">
          <span className="mx-auto mb-5 grid h-14 w-14 place-items-center rounded-2xl border border-bear/30 bg-bear/10">
            <XCircle className="h-7 w-7 text-bear" />
          </span>
          <h2 className="text-2xl font-bold text-white">Job not found</h2>
          <p className="mt-3 text-slate-400">
            We couldn&rsquo;t find a job with id{" "}
            <code className="rounded-md bg-white/5 px-1.5 py-0.5 font-mono text-sm text-slate-300">
              {jobId}
            </code>
            .
          </p>
          <button
            type="button"
            className="btn-ghost mx-auto mt-6"
            onClick={() => navigate("/")}
            data-testid="back-home-btn"
          >
            <ArrowLeft className="h-4 w-4" />
            Back to home
          </button>
        </div>
      </main>
    );
  }
  if (loadError && !job) {
    return (
      <main className="mx-auto w-full max-w-5xl flex-1 px-5 py-12 sm:px-8" data-testid="job-load-error">
        <div className="glass mx-auto max-w-xl p-8 text-center">
          <span className="mx-auto mb-5 grid h-14 w-14 place-items-center rounded-2xl border border-bear/30 bg-bear/10">
            <AlertTriangle className="h-7 w-7 text-bear" />
          </span>
          <h2 className="text-2xl font-bold text-white">Something went wrong</h2>
          <p className="mt-3 text-slate-400">{loadError.message}</p>
        </div>
      </main>
    );
  }

  // ---- render by state -------------------------------------------------
  const status = job.status;

  return (
    <main
      className="mx-auto w-full max-w-5xl flex-1 px-5 py-10 sm:px-8 sm:py-14"
      data-testid={`job-status-page-${status}`}
    >
      <AnimatePresence mode="wait">
        <motion.div
          key={status}
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -8 }}
          transition={{ duration: 0.4, ease: [0.22, 1, 0.36, 1] }}
        >
          {PROCESSING_STATES.has(status) && <Processing job={job} />}
          {status === "AWAITING_SELECTION" && (
            <AwaitingSelection
              job={job}
              onSelect={onSelectSpeaker}
              selecting={selecting}
              selectError={selectError}
            />
          )}
          {status === "RENDERING" && <Rendering job={job} />}
          {status === "DONE" && (
            <Done
              job={job}
              onGenerate={onGenerateRecommendations}
              generating={generating}
              genError={genError}
            />
          )}
          {status === "FAILED" && <Failed job={job} />}
        </motion.div>
      </AnimatePresence>
    </main>
  );
}

/* --------------------------------------------------------------------- */
/* Pipeline tracker — reads the Redis-backed job.status                  */
/* --------------------------------------------------------------------- */
function Pipeline({ status }) {
  const current = PHASE_ORDER[status] ?? 0;
  return (
    <div className="glass p-6" data-testid="pipeline-tracker">
      <p className="label-mono mb-5">Pipeline</p>
      <ol className="relative flex flex-col gap-0">
        {PIPELINE.map((step, i) => {
          const done = i < current;
          const active = i === current;
          const isLast = i === PIPELINE.length - 1;
          return (
            <li key={step.key} className="flex items-stretch gap-3.5">
              <div className="flex flex-col items-center">
                <span
                  className={`grid h-7 w-7 shrink-0 place-items-center rounded-full border transition-colors duration-300 ${
                    done
                      ? "border-bull/50 bg-bull/15 text-bull"
                      : active
                      ? "border-accent bg-accent/15 text-accent-soft animate-pulse-ring"
                      : "border-white/10 bg-white/[0.02] text-slate-600"
                  }`}
                >
                  {done ? (
                    <Check className="h-3.5 w-3.5" strokeWidth={3} />
                  ) : active ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <span className="h-1.5 w-1.5 rounded-full bg-current" />
                  )}
                </span>
                {!isLast && (
                  <span
                    className={`my-1 w-px flex-1 ${
                      done ? "bg-bull/40" : "bg-white/10"
                    }`}
                  />
                )}
              </div>
              <span
                className={`pb-5 text-sm font-medium transition-colors duration-300 ${
                  active
                    ? "text-white"
                    : done
                    ? "text-slate-400"
                    : "text-slate-600"
                }`}
              >
                {step.label}
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function StatusPill({ status }) {
  const text = status.replace(/_/g, " ");
  return (
    <span
      className="inline-flex items-center gap-2 rounded-full border border-accent/30 bg-accent/10 px-3 py-1 font-mono text-[11px] uppercase tracking-[0.12em] text-accent-soft"
      data-testid="processing-stage-label"
    >
      <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />
      {text}
    </span>
  );
}

/* --------------------------------------------------------------------- */
/* State A — Processing                                                 */
/* --------------------------------------------------------------------- */
function Processing({ job }) {
  const message = STAGE_MESSAGE[job.status] || job.progress?.message || "Working...";
  return (
    <div data-testid="state-processing" className="grid gap-5 lg:grid-cols-[1.5fr_1fr]">
      <div className="glass flex flex-col p-7 sm:p-9">
        <StatusPill status={job.status} />
        <h2
          className="mt-5 text-2xl font-bold leading-snug tracking-tight text-white sm:text-3xl"
          data-testid="processing-message"
        >
          {message}
        </h2>
        <div className="mt-7">
          <ProgressBar percent={job.progress?.percent || 0} testId="processing-progress" />
        </div>
        <p
          className="mt-8 flex items-start gap-3 rounded-xl border border-accent/15 bg-accent/[0.05] px-4 py-3.5 text-sm leading-relaxed text-slate-400"
          data-testid="processing-hint"
        >
          <Clock className="mt-0.5 h-4 w-4 shrink-0 text-accent-soft" />
          You can close this tab — we&rsquo;ll keep processing. Come back and
          paste the same link to check status.
        </p>
      </div>
      <Pipeline status={job.status} />
    </div>
  );
}

/* --------------------------------------------------------------------- */
/* State B — Awaiting selection                                         */
/* --------------------------------------------------------------------- */
function AwaitingSelection({ job, onSelect, selecting, selectError }) {
  const speakers = job.speakers || [];
  return (
    <div data-testid="state-awaiting-selection">
      <div className="mb-7">
        <span className="label-mono">Step 4 · identify yourself</span>
        <h2
          className="mt-3 text-2xl font-bold tracking-tight text-white sm:text-3xl"
          data-testid="speakers-heading"
        >
          We found {speakers.length} speaker{speakers.length === 1 ? "" : "s"}.{" "}
          <span className="text-accent-soft">Which one is you?</span>
        </h2>
        <p className="mt-2 text-slate-400" data-testid="speakers-sub">
          Tap a sample to hear each voice, then pick yours.
        </p>
      </div>

      <motion.div
        initial="hidden"
        animate="show"
        variants={{ hidden: {}, show: { transition: { staggerChildren: 0.06 } } }}
        className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3"
        data-testid="speakers-grid"
      >
        {speakers.map((sp, i) => (
          <SpeakerCard
            key={sp.label}
            speaker={sp}
            displayName={displayNameFor(sp.label, i)}
            onSelect={onSelect}
            isSelecting={selecting === sp.label}
            disabled={selecting !== null && selecting !== sp.label}
          />
        ))}
      </motion.div>

      {selectError && (
        <p
          className="mt-5 flex items-center gap-2 rounded-xl border border-bear/30 bg-bear/10 px-4 py-3 text-sm text-bear-soft"
          data-testid="select-error"
        >
          <AlertTriangle className="h-4 w-4 shrink-0" />
          {selectError}
        </p>
      )}
    </div>
  );
}

function displayNameFor(label, fallbackIndex) {
  // SPEAKER_00 -> "Speaker 1", SPEAKER_07 -> "Speaker 8"
  const m = /SPEAKER[_-]?(\d+)/i.exec(label || "");
  const n = m ? Number(m[1]) + 1 : fallbackIndex + 1;
  return `Speaker ${n}`;
}

/* --------------------------------------------------------------------- */
/* State C — Rendering                                                  */
/* --------------------------------------------------------------------- */
function Rendering({ job }) {
  return (
    <div className="grid gap-5 lg:grid-cols-[1.5fr_1fr]" data-testid="state-rendering">
      <div className="glass flex flex-col items-center justify-center gap-6 p-9 text-center">
        <div className="relative grid place-items-center">
          <div
            className="spinner h-16 w-16 border-[3px]"
            data-testid="rendering-spinner"
          />
          <Film className="absolute h-6 w-6 text-accent-soft" />
        </div>
        <h2
          className="text-xl font-bold tracking-tight text-white sm:text-2xl"
          data-testid="rendering-message"
        >
          Cutting and stitching your video… almost there!
        </h2>
        {job.progress?.percent > 0 && (
          <div className="w-full max-w-sm">
            <ProgressBar percent={job.progress.percent} testId="rendering-progress" />
          </div>
        )}
      </div>
      <Pipeline status="RENDERING" />
    </div>
  );
}

/* --------------------------------------------------------------------- */
/* State D — Done                                                       */
/* --------------------------------------------------------------------- */
function Done({ job, onGenerate, generating, genError }) {
  const stats = buildDoneStats(job);
  return (
    <div className="flex flex-col gap-6" data-testid="state-done">
      <div className="glass overflow-hidden p-7 sm:p-9">
        <div className="flex items-start gap-4">
          <span className="grid h-12 w-12 shrink-0 place-items-center rounded-2xl border border-bull/30 bg-bull/10 shadow-glow-bull">
            <CheckCircle2 className="h-6 w-6 text-bull" />
          </span>
          <div className="flex-1">
            <h2
              className="text-2xl font-bold tracking-tight text-white sm:text-3xl"
              data-testid="done-title"
            >
              Your video is ready! 🎉
            </h2>
            {stats && (
              <p className="mt-2 font-mono text-sm text-slate-400" data-testid="done-stats">
                {stats}
              </p>
            )}
          </div>
        </div>

        <div className="mt-7 flex flex-wrap gap-3">
          {job.download_url ? (
            <motion.a
              whileHover={{ y: -2 }}
              whileTap={{ scale: 0.98 }}
              className="btn-download"
              href={job.download_url}
              target="_blank"
              rel="noopener noreferrer"
              data-testid="download-btn"
            >
              <Download className="h-4 w-4" />
              Download Video
            </motion.a>
          ) : (
            <span
              className="inline-flex items-center gap-2 rounded-xl border border-dashed border-white/10 bg-white/[0.02] px-5 py-3 font-mono text-sm text-slate-500"
              data-testid="download-pending"
            >
              <Clock className="h-4 w-4" />
              Download link will appear once the real renderer is wired (M6)
            </span>
          )}

          {job.transcription_url && (
            <motion.a
              whileHover={{ y: -2 }}
              whileTap={{ scale: 0.98 }}
              className="btn-download"
              href={job.transcription_url}
              download
              target="_blank"
              rel="noopener noreferrer"
              data-testid="download-transcript-btn"
            >
              <FileText className="h-4 w-4" />
              Download Transcript
            </motion.a>
          )}
        </div>
      </div>

      <Recommendations
        job={job}
        onGenerate={onGenerate}
        generating={generating}
        genError={genError}
      />

      <Link
        to="/"
        className="btn-ghost self-start no-underline"
        data-testid="process-another-link"
      >
        <HomeIcon className="h-4 w-4" />
        Process another video
      </Link>
    </div>
  );
}

function buildDoneStats(job) {
  const sp = (job.speakers || []).find((s) => s.label === job.selected_speaker);
  const spokeSec = sp?.total_speaking_sec || 0;
  const sourceSec = job.duration_sec || 0;

  if (spokeSec <= 0 && sourceSec <= 0) return null;

  const parts = [];
  if (spokeSec > 0) {
    const spokeMin = Math.round(spokeSec / 60);
    parts.push(`Extracted ${spokeMin} minute${spokeMin === 1 ? "" : "s"} of your speaking`);
  }
  if (sourceSec > 0) {
    const hrs = (sourceSec / 3600).toFixed(1).replace(/\.0$/, "");
    parts.push(`from a ${hrs}-hour video`);
  }
  return parts.join(" ");
}

/* --------------------------------------------------------------------- */
/* Stock recommendations (LLM → downloadable CSV) — Insights Showcase    */
/* --------------------------------------------------------------------- */
function Recommendations({ job, onGenerate, generating, genError }) {
  // Only offered once a transcript exists — there's nothing to analyse otherwise.
  if (!job.transcription_url) return null;

  const recStatus = job.recommendations_status;
  const isReady = recStatus === "READY" && job.recommendations_url;
  const isGenerating = recStatus === "GENERATING" || generating;

  return (
    <div
      className={`glass relative overflow-hidden p-7 sm:p-9 ${
        isGenerating ? "neural-generating" : ""
      }`}
      data-testid="recommendations-card"
    >
      {/* rotating rainbow border — only while the AI is doing the heavy lifting */}
      {isGenerating && <span className="neural-ring" aria-hidden="true" />}

      {/* faint trading-grid backdrop */}
      <div className="pointer-events-none absolute inset-0 bg-grid-faint bg-[length:32px_32px] opacity-60" />
      <div className="relative">
        <div className="flex items-start justify-between gap-4">
          <div className="flex items-center gap-3">
            {/* animated AI icon — gentle float with a softly pulsing glow ring */}
            <span className="relative inline-grid animate-float">
              <span className="grid h-11 w-11 place-items-center rounded-2xl bg-accent-grad shadow-glow-accent animate-pulse-ring">
                <BrainCircuit className="h-5 w-5 text-white" />
              </span>
            </span>
            <div>
              <p className="label-mono">Neural Analysis</p>
              <h3 className="mt-1 text-lg font-bold tracking-tight text-white">
                Neural Alpha Engine
              </h3>
            </div>
          </div>
          {isReady && (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-bull/30 bg-bull/10 px-3 py-1 font-mono text-[11px] uppercase tracking-wider text-bull-soft">
              <span className="h-1.5 w-1.5 rounded-full bg-bull" />
              Ready
            </span>
          )}
          {isGenerating && (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-white/10 bg-white/[0.03] px-3 py-1 font-mono text-[11px] uppercase tracking-wider">
              <Loader2 className="h-3 w-3 animate-spin text-accent-soft" />
              <span className="neural-text">Processing</span>
            </span>
          )}
        </div>

        <p className="mt-4 max-w-lg text-sm leading-relaxed text-slate-400">
          Get stock insights from our advanced AI intelligence based on what you
          have spoken.
        </p>

        <div className="mt-6">
          {isReady ? (
            <div className="recs-block flex flex-wrap items-center gap-3" data-testid="recommendations-ready">
              <motion.a
                whileHover={{ y: -2 }}
                whileTap={{ scale: 0.98 }}
                className="btn-download border-bull/30 bg-bull/[0.06] hover:bg-bull/[0.12]"
                href={job.recommendations_url}
                download
                target="_blank"
                rel="noopener noreferrer"
                data-testid="download-recommendations-btn"
              >
                <Download className="h-4 w-4" />
                Download Recommendations (CSV)
              </motion.a>
              <button
                type="button"
                className="btn-ghost"
                onClick={onGenerate}
                disabled={generating}
                data-testid="regenerate-recommendations-btn"
              >
                <RefreshCw className={`h-4 w-4 ${generating ? "animate-spin" : ""}`} />
                {generating ? "Regenerating…" : "Regenerate"}
              </button>
            </div>
          ) : isGenerating ? (
            <div
              className="recs-generating flex items-center gap-3"
              data-testid="recommendations-generating"
            >
              <Loader2 className="h-5 w-5 animate-spin text-accent-soft" />
              <div>
                <p className="neural-text text-sm font-semibold">
                  Neural engine analysing your transcript…
                </p>
                <p className="mt-0.5 font-mono text-[11px] text-slate-500">
                  Surfacing tickers &amp; themes — this can take a moment.
                </p>
              </div>
            </div>
          ) : (
            <div className="recs-block flex flex-col items-start gap-3" data-testid="recommendations-idle">
              <motion.button
                type="button"
                whileHover={{ y: -2 }}
                whileTap={{ scale: 0.98 }}
                className="btn-primary"
                onClick={onGenerate}
                data-testid="generate-recommendations-btn"
              >
                <Sparkles className="h-4 w-4" />
                Generate Stock Recommendations
              </motion.button>
              {recStatus === "FAILED" && (
                <p
                  className="flex items-center gap-2 rounded-xl border border-bear/30 bg-bear/10 px-4 py-3 text-sm text-bear-soft"
                  data-testid="recommendations-error"
                >
                  <AlertTriangle className="h-4 w-4 shrink-0" />
                  {job.recommendations_error || "Generation failed. Please try again."}
                </p>
              )}
              {genError && (
                <p
                  className="flex items-center gap-2 rounded-xl border border-bear/30 bg-bear/10 px-4 py-3 text-sm text-bear-soft"
                  data-testid="recommendations-gen-error"
                >
                  <AlertTriangle className="h-4 w-4 shrink-0" />
                  {genError}
                </p>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/* --------------------------------------------------------------------- */
/* State E — Failed                                                     */
/* --------------------------------------------------------------------- */

// Map server error codes to friendly user-facing messages (M7).
// Unknown codes fall through to job.error.message (which is itself a
// human-readable string from the worker), and finally to a generic
// fallback. Keep this map small and stable — every entry is an
// intentional override of the API's raw text.
const ERROR_CODE_MESSAGES = {
  PRIVATE: "This video is private or has been removed.",
  UNAVAILABLE: "This video is private or has been removed.",
  MEMBERS_ONLY: "This video is for channel members only.",
  AGE_RESTRICTED: "This video is age-restricted.",
  COPYRIGHT: "This video is blocked for copyright reasons.",
  REGION_BLOCKED: "This video isn’t available in our region.",
  LIVE_STREAM:
    "This video is currently live. Please wait until the stream ends and try again.",
  DIARIZE_FAILED:
    "Could not detect any speakers in this video. Try a video with clearer audio.",
  TRANSCRIBE_FAILED:
    "Could not transcribe the audio. Try a video with clearer speech.",
  TIMEOUT: "Processing timed out. Please try again.",
};

const GENERIC_ERROR_MESSAGE = "Something went wrong. Please try again.";

function friendlyError(job) {
  const code = job.error?.code;
  if (code && ERROR_CODE_MESSAGES[code]) {
    return ERROR_CODE_MESSAGES[code];
  }
  // Unknown codes: prefer the API's specific message (e.g. TOO_LONG
  // includes the actual hour limit dynamically).
  return job.error?.message || job.progress?.message || GENERIC_ERROR_MESSAGE;
}

function Failed({ job }) {
  const navigate = useNavigate();
  const msg = friendlyError(job);
  return (
    <div className="mx-auto max-w-xl" data-testid="state-failed">
      <div className="glass p-8 text-center">
        <span className="mx-auto mb-5 grid h-14 w-14 place-items-center rounded-2xl border border-bear/30 bg-bear/10 shadow-glow-bear">
          <XCircle className="h-7 w-7 text-bear" />
        </span>
        <h2 className="text-2xl font-bold tracking-tight text-bear" data-testid="failed-title">
          Job failed
        </h2>
        <p className="mt-3 leading-relaxed text-slate-400" data-testid="failed-detail">
          {msg}
        </p>
        <motion.button
          type="button"
          whileHover={{ y: -2 }}
          whileTap={{ scale: 0.98 }}
          className="btn-primary mx-auto mt-7"
          onClick={() => navigate("/")}
          data-testid="failed-try-again-btn"
        >
          <RefreshCw className="h-4 w-4" />
          Try Again
        </motion.button>
      </div>
    </div>
  );
}
