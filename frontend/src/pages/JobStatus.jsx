import React, { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { getJob, selectSpeaker } from "../lib/api";
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

export default function JobStatus() {
  const { jobId } = useParams();
  const navigate = useNavigate();

  const [job, setJob] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [selecting, setSelecting] = useState(null); // label currently being selected
  const [selectError, setSelectError] = useState(null);
  const timerRef = useRef(null);

  // ---- polling loop -----------------------------------------------------
  useEffect(() => {
    let cancelled = false;

    async function tick() {
      try {
        const data = await getJob(jobId);
        if (cancelled) return;
        setJob(data);
        setLoadError(null);
        if (data.status === "DONE" || data.status === "FAILED") {
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
    } catch (err) {
      setSelectError(err.message || "Failed to select speaker");
      setSelecting(null);
    }
  }

  // ---- render: load / error states -------------------------------------
  if (!job && !loadError) {
    return <div className="center-load" data-testid="job-loading">Loading job…</div>;
  }
  if (loadError && loadError.status === 404) {
    return (
      <main className="job-main" data-testid="job-not-found">
        <div className="status-card">
          <h2 className="stage-message">Job not found</h2>
          <p className="failed-detail">
            We couldn&rsquo;t find a job with id <code>{jobId}</code>.
          </p>
          <button
            type="button"
            className="ghost-btn"
            onClick={() => navigate("/")}
            data-testid="back-home-btn"
          >
            Back to home
          </button>
        </div>
      </main>
    );
  }
  if (loadError && !job) {
    return (
      <main className="job-main" data-testid="job-load-error">
        <div className="status-card">
          <h2 className="stage-message">Something went wrong</h2>
          <p className="failed-detail">{loadError.message}</p>
        </div>
      </main>
    );
  }

  // ---- render by state -------------------------------------------------
  const status = job.status;

  return (
    <main className="job-main" data-testid={`job-status-page-${status}`}>
      <div className="status-card">
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
        {status === "DONE" && <Done job={job} />}
        {status === "FAILED" && <Failed job={job} />}
      </div>
    </main>
  );
}

/* --------------------------------------------------------------------- */
/* State A — Processing                                                 */
/* --------------------------------------------------------------------- */
function Processing({ job }) {
  const message = STAGE_MESSAGE[job.status] || job.progress?.message || "Working...";
  return (
    <div data-testid="state-processing">
      <p className="stage-label" data-testid="processing-stage-label">
        {job.status.replace(/_/g, " ")}
      </p>
      <h2 className="stage-message" data-testid="processing-message">{message}</h2>
      <ProgressBar percent={job.progress?.percent || 0} testId="processing-progress" />
      <p className="processing-hint" data-testid="processing-hint">
        You can close this tab — we&rsquo;ll keep processing. Come back and
        paste the same link to check status.
      </p>
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
      <h2 className="speakers-heading" data-testid="speakers-heading">
        We found {speakers.length} speaker{speakers.length === 1 ? "" : "s"}.
        Which one is you?
      </h2>
      <p className="speakers-sub" data-testid="speakers-sub">
        Tap a sample to hear each voice, then pick yours.
      </p>

      <div className="speakers-grid" data-testid="speakers-grid">
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
      </div>

      {selectError && (
        <p className="error-text" data-testid="select-error" style={{ marginTop: 18 }}>
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
    <div className="rendering-block" data-testid="state-rendering">
      <div className="spinner" data-testid="rendering-spinner" />
      <h2 className="stage-message" style={{ margin: 0, textAlign: "center" }}
          data-testid="rendering-message">
        Cutting and stitching your video... almost there!
      </h2>
      {job.progress?.percent > 0 && (
        <div style={{ width: "100%" }}>
          <ProgressBar percent={job.progress.percent} testId="rendering-progress" />
        </div>
      )}
    </div>
  );
}

/* --------------------------------------------------------------------- */
/* State D — Done                                                       */
/* --------------------------------------------------------------------- */
function Done({ job }) {
  const stats = buildDoneStats(job);
  return (
    <div className="done-block" data-testid="state-done">
      <h2 className="done-title" data-testid="done-title">
        Your video is ready! 🎉
      </h2>
      {stats && (
        <p className="done-stats" data-testid="done-stats">{stats}</p>
      )}

      {job.download_url ? (
        <a
          className="download-btn"
          href={job.download_url}
          target="_blank"
          rel="noopener noreferrer"
          data-testid="download-btn"
        >
          Download Video
        </a>
      ) : (
        <span className="download-disabled" data-testid="download-pending">
          Download link will appear once the real renderer is wired (M6)
        </span>
      )}

      <Link to="/" className="ghost-btn" data-testid="process-another-link"
            style={{ alignSelf: "flex-start", textDecoration: "none" }}>
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
/* State E — Failed                                                     */
/* --------------------------------------------------------------------- */
function Failed({ job }) {
  const navigate = useNavigate();
  const msg = job.error?.message || job.progress?.message || "Something went wrong.";
  return (
    <div className="failed-block" data-testid="state-failed">
      <h2 className="failed-title" data-testid="failed-title">Job failed</h2>
      <p className="failed-detail" data-testid="failed-detail">{msg}</p>
      <button
        type="button"
        className="primary-btn"
        style={{ alignSelf: "flex-start" }}
        onClick={() => navigate("/")}
        data-testid="failed-try-again-btn"
      >
        Try Again
      </button>
    </div>
  );
}
