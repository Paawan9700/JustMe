import React, { useEffect, useState } from "react";
import { Routes, Route, Link } from "react-router-dom";
import { AnimatePresence, motion } from "framer-motion";
import { AudioLines, ListChecks } from "lucide-react";
import Home from "./pages/Home";
import JobStatus from "./pages/JobStatus";
import MyJobs from "./pages/MyJobs";

// The three plain-language steps of what Alphavox does, in journey order.
// Cycled in the header chip so a first-time visitor gets the pitch at a glance.
const PITCH_STEPS = ["Paste a link", "Pick your voice", "Get just you"];

function PitchChip() {
  const [i, setI] = useState(0);
  useEffect(() => {
    const id = setInterval(
      () => setI((prev) => (prev + 1) % PITCH_STEPS.length),
      2400
    );
    return () => clearInterval(id);
  }, []);
  return (
    <span className="hidden items-center gap-2 rounded-full border border-accent/25 bg-accent/10 px-3 py-1 font-mono text-[11px] font-medium text-accent-soft sm:inline-flex">
      <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />
      <span className="relative inline-grid min-w-[104px] place-items-start overflow-hidden">
        <AnimatePresence mode="wait">
          <motion.span
            key={i}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            transition={{ duration: 0.32, ease: [0.22, 1, 0.36, 1] }}
            className="col-start-1 row-start-1 whitespace-nowrap"
          >
            {PITCH_STEPS[i]}
          </motion.span>
        </AnimatePresence>
      </span>
    </span>
  );
}

export default function App() {
  return (
    <div className="min-h-screen flex flex-col" data-testid="app-shell">
      <header className="sticky top-0 z-30 border-b border-white/[0.06] bg-ink-950/60 backdrop-blur-xl">
        <div className="mx-auto flex w-full max-w-6xl items-center justify-between px-5 py-4 sm:px-8">
          <Link
            to="/"
            className="group flex items-center gap-2.5 no-underline"
            data-testid="brand-home-link"
          >
            <span className="grid h-9 w-9 place-items-center rounded-xl bg-accent-grad shadow-glow-accent transition-transform duration-200 group-hover:scale-105">
              <AudioLines className="h-5 w-5 text-white" strokeWidth={2.5} />
            </span>
            <span className="text-lg font-bold tracking-tight text-white">
              Alpha<span className="text-accent-soft">vox</span>
            </span>
          </Link>

          <div className="flex items-center gap-3">
            <Link
              to="/jobs"
              className="inline-flex items-center gap-1.5 rounded-full px-3 py-1 font-mono text-[11px] font-medium text-slate-300 no-underline transition-colors hover:text-white"
              data-testid="nav-myjobs-link"
            >
              <ListChecks className="h-3.5 w-3.5" />
              My Jobs
            </Link>
            <PitchChip />
          </div>
        </div>
      </header>

      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/jobs" element={<MyJobs />} />
        <Route path="/jobs/:jobId" element={<JobStatus />} />
        <Route
          path="*"
          element={
            <main
              className="mx-auto w-full max-w-6xl px-5 py-24 sm:px-8"
              data-testid="not-found"
            >
              <p className="text-slate-400">
                Nothing here.{" "}
                <Link to="/" className="text-accent-soft hover:text-white">
                  Go home
                </Link>
                .
              </p>
            </main>
          }
        />
      </Routes>

      <footer className="mt-auto border-t border-white/[0.05] py-6">
        <p className="text-center font-mono text-[11px] text-slate-700">
          Alphavox — your words, your edit, your edge.
        </p>
      </footer>
    </div>
  );
}
