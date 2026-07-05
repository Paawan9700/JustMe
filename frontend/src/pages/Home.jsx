import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { motion } from "framer-motion";
import {
  ArrowRight,
  Loader2,
  Link2,
  Mic,
  Scissors,
  TrendingUp,
  AlertTriangle,
} from "lucide-react";
import { createJob } from "../lib/api";

const STEPS = [
  { icon: Link2, title: "Paste a link", body: "Drop any long YouTube video." },
  { icon: Mic, title: "We find the voices", body: "AI diarization separates every speaker." },
  { icon: Scissors, title: "Pick yourself", body: "Keep only your speaking moments." },
  { icon: TrendingUp, title: "Get insights", body: "Turn your words into stock signals." },
];

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
    <main
      className="mx-auto flex w-full max-w-6xl flex-1 flex-col px-5 py-16 sm:px-8 sm:py-24"
      data-testid="home-page"
    >
      <motion.div
        initial={{ opacity: 0, y: 14 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
        className="max-w-3xl"
      >
        <span className="mb-6 inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/[0.03] px-3.5 py-1.5 font-mono text-[11px] uppercase tracking-[0.14em] text-slate-400">
          <span className="h-1.5 w-1.5 rounded-full bg-accent" />
          Voice extraction · stock intelligence
        </span>

        <h1
          className="text-balance text-4xl font-extrabold leading-[1.04] tracking-tight text-white sm:text-6xl"
          data-testid="home-title"
        >
          Just the parts where{" "}
          <span className="bg-gradient-to-r from-accent-soft via-accent to-accent-blue bg-clip-text text-transparent">
            you spoke.
          </span>
        </h1>

        <p
          className="mt-5 max-w-xl text-lg leading-relaxed text-slate-400"
          data-testid="home-subtitle"
        >
          Extract your speaking moments from any long video. Paste a YouTube
          link — we&rsquo;ll detect every speaker, you pick yourself, and we
          stitch your bits into one clean cut.
        </p>
      </motion.div>

      <motion.form
        initial={{ opacity: 0, y: 14 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5, delay: 0.08, ease: [0.22, 1, 0.36, 1] }}
        className="mt-10 max-w-2xl"
        onSubmit={onSubmit}
        data-testid="home-form"
      >
        <div className="glass flex flex-col gap-3 p-2.5 sm:flex-row sm:items-center sm:p-2.5">
          <div className="flex flex-1 items-center gap-2.5 rounded-xl px-3">
            <Link2 className="h-4 w-4 shrink-0 text-slate-500" />
            <input
              type="url"
              className="w-full bg-transparent py-3.5 font-mono text-sm text-slate-100 outline-none placeholder:text-slate-600"
              placeholder="https://www.youtube.com/watch?v=..."
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              disabled={submitting}
              aria-label="YouTube URL"
              data-testid="youtube-url-input"
              autoFocus
            />
          </div>
          <motion.button
            type="submit"
            whileHover={{ y: -2 }}
            whileTap={{ scale: 0.98 }}
            className="btn-primary px-6 py-3.5"
            disabled={submitting || !url.trim()}
            data-testid="get-started-btn"
          >
            {submitting ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" />
                Starting…
              </>
            ) : (
              <>
                Get Started
                <ArrowRight className="h-4 w-4" />
              </>
            )}
          </motion.button>
        </div>

        {error && (
          <motion.p
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            className="mt-3 flex items-center gap-2 rounded-xl border border-bear/30 bg-bear/10 px-4 py-3 text-sm text-bear-soft"
            data-testid="home-error"
          >
            <AlertTriangle className="h-4 w-4 shrink-0" />
            {error}
          </motion.p>
        )}

        <p
          className="mt-4 font-mono text-xs text-slate-600"
          data-testid="home-helper"
        >
          Handles videos up to 15 hours, including past livestreams.
          Currently-live broadcasts aren&rsquo;t supported.
        </p>
      </motion.form>

      <motion.div
        initial="hidden"
        animate="show"
        variants={{
          hidden: {},
          show: { transition: { staggerChildren: 0.07, delayChildren: 0.2 } },
        }}
        className="mt-20 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4"
      >
        {STEPS.map((s, i) => {
          const Icon = s.icon;
          return (
            <motion.div
              key={s.title}
              variants={{
                hidden: { opacity: 0, y: 16 },
                show: { opacity: 1, y: 0 },
              }}
              className="glass group p-5 transition-colors duration-300 hover:border-white/15"
            >
              <div className="mb-4 flex items-center justify-between">
                <span className="grid h-10 w-10 place-items-center rounded-xl border border-white/10 bg-white/[0.03] text-accent-soft transition-colors duration-300 group-hover:border-accent/40 group-hover:bg-accent/10">
                  <Icon className="h-5 w-5" />
                </span>
                <span className="font-mono text-xs text-slate-700">
                  0{i + 1}
                </span>
              </div>
              <h3 className="text-sm font-semibold text-white">{s.title}</h3>
              <p className="mt-1 text-sm leading-relaxed text-slate-500">
                {s.body}
              </p>
            </motion.div>
          );
        })}
      </motion.div>
    </main>
  );
}
