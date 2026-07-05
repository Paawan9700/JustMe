import React, { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { motion } from "framer-motion";
import {
  Loader2,
  RefreshCw,
  AlertTriangle,
  ListChecks,
  Mic,
  ChevronRight,
  Plus,
} from "lucide-react";
import { listJobs } from "../lib/api";
import ProgressBar from "../components/ProgressBar";

const PROCESSING_STATES = new Set([
  "QUEUED",
  "DOWNLOADING",
  "EXTRACTING_AUDIO",
  "DIARIZING",
  "GENERATING_SNIPPETS",
  "RENDERING",
]);

// Status -> badge palette (ink/accent/bull/bear), mirroring StatusPill.
function badgeClasses(status) {
  if (status === "DONE") {
    return "border-bull/30 bg-bull/10 text-bull-soft";
  }
  if (status === "FAILED") {
    return "border-bear/30 bg-bear/10 text-bear-soft";
  }
  if (status === "AWAITING_SELECTION") {
    return "border-accent/40 bg-accent/15 text-accent-soft";
  }
  return "border-accent/30 bg-accent/10 text-accent-soft";
}

function StatusBadge({ status }) {
  const text = status.replace(/_/g, " ");
  const pulse = PROCESSING_STATES.has(status);
  const dotColor = status === "DONE" ? "bg-bull" : status === "FAILED" ? "bg-bear" : "bg-accent";
  return (
    <span
      className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 font-mono text-[11px] uppercase tracking-[0.12em] ${badgeClasses(
        status
      )}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${dotColor} ${pulse ? "animate-pulse" : ""}`} />
      {text}
    </span>
  );
}

// Small relative-time helper — no date library needed.
function timeAgo(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins} min${mins === 1 ? "" : "s"} ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.round(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

function jobTitle(job) {
  return job.video_title || job.youtube_url || job.job_id;
}

export default function MyJobs() {
  const [jobs, setJobs] = useState(null); // null = not loaded yet
  const [loadError, setLoadError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    setRefreshing(true);
    try {
      const data = await listJobs();
      setJobs(Array.isArray(data) ? data : []);
      setLoadError(null);
    } catch (err) {
      setLoadError(err);
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await listJobs();
        if (cancelled) return;
        setJobs(Array.isArray(data) ? data : []);
        setLoadError(null);
      } catch (err) {
        if (!cancelled) setLoadError(err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // ---- loading (first paint) -------------------------------------------
  if (jobs === null && !loadError) {
    return (
      <div
        className="flex min-h-[60vh] flex-col items-center justify-center gap-4 text-slate-500"
        data-testid="myjobs-loading"
      >
        <Loader2 className="h-7 w-7 animate-spin text-accent-soft" />
        <span className="font-mono text-sm">Loading your jobs…</span>
      </div>
    );
  }

  // ---- hard error on first load ----------------------------------------
  if (loadError && jobs === null) {
    return (
      <main className="mx-auto w-full max-w-5xl flex-1 px-5 py-12 sm:px-8" data-testid="myjobs-error">
        <div className="glass mx-auto max-w-xl p-8 text-center">
          <span className="mx-auto mb-5 grid h-14 w-14 place-items-center rounded-2xl border border-bear/30 bg-bear/10">
            <AlertTriangle className="h-7 w-7 text-bear" />
          </span>
          <h2 className="text-2xl font-bold text-white">Couldn&rsquo;t load your jobs</h2>
          <p className="mt-3 text-slate-400">{loadError.message}</p>
          <button type="button" className="btn-ghost mx-auto mt-6" onClick={load}>
            <RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} />
            Try again
          </button>
        </div>
      </main>
    );
  }

  return (
    <main
      className="mx-auto w-full max-w-5xl flex-1 px-5 py-10 sm:px-8 sm:py-14"
      data-testid="myjobs-page"
    >
      <div className="mb-7 flex items-end justify-between gap-4">
        <div>
          <span className="label-mono flex items-center gap-2">
            <ListChecks className="h-4 w-4" />
            My Jobs
          </span>
          <h1 className="mt-2 text-2xl font-bold tracking-tight text-white sm:text-3xl">
            Your videos
          </h1>
        </div>
        <button
          type="button"
          className="btn-ghost"
          onClick={load}
          disabled={refreshing}
          data-testid="myjobs-refresh"
        >
          <RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} />
          Refresh
        </button>
      </div>

      {/* Empty state */}
      {jobs && jobs.length === 0 ? (
        <div className="glass mx-auto max-w-xl p-8 text-center" data-testid="myjobs-empty">
          <span className="mx-auto mb-5 grid h-14 w-14 place-items-center rounded-2xl border border-white/10 bg-white/5">
            <ListChecks className="h-7 w-7 text-slate-400" />
          </span>
          <h2 className="text-2xl font-bold text-white">No jobs yet</h2>
          <p className="mt-3 text-slate-400">
            Submit a YouTube video and it&rsquo;ll show up here so you can come back anytime.
          </p>
          <Link to="/" className="btn-primary mx-auto mt-6 no-underline">
            <Plus className="h-4 w-4" />
            Process your first video
          </Link>
        </div>
      ) : (
        <ul className="grid gap-3" data-testid="myjobs-list">
          {jobs &&
            jobs.map((job, i) => {
              const awaiting = job.status === "AWAITING_SELECTION";
              const processing = PROCESSING_STATES.has(job.status);
              return (
                <motion.li
                  key={job.job_id}
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.3, delay: Math.min(i * 0.03, 0.3), ease: [0.22, 1, 0.36, 1] }}
                >
                  <Link
                    to={`/jobs/${job.job_id}`}
                    className={`glass block p-5 no-underline transition-colors hover:border-white/15 ${
                      awaiting ? "border-accent/40 shadow-glow-accent" : ""
                    }`}
                    data-testid="myjobs-row"
                  >
                    <div className="flex items-start justify-between gap-4">
                      <div className="min-w-0">
                        <p
                          className="truncate text-base font-semibold text-white"
                          title={jobTitle(job)}
                        >
                          {jobTitle(job)}
                        </p>
                        <p
                          className="mt-1 font-mono text-xs text-slate-500"
                          title={job.created_at ? new Date(job.created_at).toLocaleString() : ""}
                        >
                          {timeAgo(job.created_at)}
                        </p>
                      </div>
                      <div className="flex shrink-0 items-center gap-3">
                        <StatusBadge status={job.status} />
                        <ChevronRight className="h-4 w-4 text-slate-600" />
                      </div>
                    </div>

                    {processing && (
                      <div className="mt-4">
                        <ProgressBar percent={job.progress_percent || 0} testId="myjobs-progress" />
                      </div>
                    )}

                    {awaiting && (
                      <div className="mt-4 flex items-center gap-2 text-sm font-medium text-accent-soft">
                        <Mic className="h-4 w-4" />
                        Waiting for you to select your voice — click to choose your speaker
                      </div>
                    )}
                  </Link>
                </motion.li>
              );
            })}
        </ul>
      )}

      {/* Non-fatal refresh error (list already shown) */}
      {loadError && jobs && (
        <p className="mt-4 font-mono text-xs text-bear-soft" data-testid="myjobs-refresh-error">
          Refresh failed: {loadError.message}
        </p>
      )}
    </main>
  );
}
