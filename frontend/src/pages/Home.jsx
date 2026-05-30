import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { createJob } from "../lib/api";

export default function Home() {
  const [url, setUrl] = useState("");
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const navigate = useNavigate();

  async function onSubmit(e) {
    e.preventDefault();
    if (!url.trim() || submitting) return;
    setError(null);
    setSubmitting(true);
    try {
      const res = await createJob(url.trim());
      navigate(`/jobs/${res.job_id}`);
    } catch (err) {
      setError(err.message || "Something went wrong");
      setSubmitting(false);
    }
  }

  return (
    <main className="home-main" data-testid="home-page">
      <h1 className="home-title" data-testid="home-title">
        Just the parts where you spoke.
      </h1>
      <p className="home-subtitle" data-testid="home-subtitle">
        Extract your speaking moments from any long video. Paste a YouTube
        link — we&rsquo;ll detect every speaker, you pick yourself, and we
        stitch your bits into one clean cut.
      </p>

      <form className="url-form" onSubmit={onSubmit} data-testid="home-form">
        <div className="url-row">
          <input
            type="url"
            className="url-input"
            placeholder="https://www.youtube.com/watch?v=..."
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            disabled={submitting}
            aria-label="YouTube URL"
            data-testid="youtube-url-input"
            autoFocus
          />
          <button
            type="submit"
            className="primary-btn"
            disabled={submitting || !url.trim()}
            data-testid="get-started-btn"
          >
            {submitting ? "Starting…" : "Get Started"}
          </button>
        </div>

        {error && (
          <p className="error-text" data-testid="home-error">
            {error}
          </p>
        )}

        <p className="helper-text" data-testid="home-helper">
          Works with videos up to 15 hours. Livestreams aren&rsquo;t
          supported.
        </p>
      </form>
    </main>
  );
}
