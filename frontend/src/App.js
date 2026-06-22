import React from "react";
import { Routes, Route, Link } from "react-router-dom";
import { Activity } from "lucide-react";
import Home from "./pages/Home";
import JobStatus from "./pages/JobStatus";

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
              <Activity className="h-5 w-5 text-white" strokeWidth={2.5} />
            </span>
            <span className="text-lg font-bold tracking-tight text-white">
              just<span className="text-accent-soft">.</span>me
            </span>
          </Link>

          <div className="flex items-center gap-3">
            <span className="hidden items-center gap-1.5 rounded-full border border-bull/25 bg-bull/10 px-3 py-1 font-mono text-[11px] font-medium text-bull-soft sm:inline-flex">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-bull" />
              AI · live
            </span>
            <span className="font-mono text-xs text-slate-600">v0.2 — m2</span>
          </div>
        </div>
      </header>

      <Routes>
        <Route path="/" element={<Home />} />
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
          JustMe.ai — your words, your edit, your edge.
        </p>
      </footer>
    </div>
  );
}
