import React, { useEffect, useState } from "react";
import { Routes, Route, Link, NavLink } from "react-router-dom";
import { AnimatePresence, motion } from "framer-motion";
import { AudioLines, Film, LogOut } from "lucide-react";
import { AuthProvider, RequireAuth, useAuth } from "./lib/auth";
import Home from "./pages/Home";
import JobStatus from "./pages/JobStatus";
import Login from "./pages/Login";
import MyJobs from "./pages/MyJobs";
import Privacy from "./pages/Privacy";

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

// Google profile photo, falling back to an initial if it fails to load.
function Avatar({ user }) {
  const [failed, setFailed] = useState(false);
  const initial = (user.name || user.email || "?").trim().charAt(0).toUpperCase();
  if (user.picture && !failed) {
    return (
      <img
        src={user.picture}
        alt=""
        referrerPolicy="no-referrer"
        onError={() => setFailed(true)}
        className="h-8 w-8 rounded-full border border-white/10"
      />
    );
  }
  return (
    <span className="grid h-8 w-8 place-items-center rounded-full border border-white/10 bg-white/[0.04] text-xs font-semibold text-slate-200">
      {initial}
    </span>
  );
}

function UserChip() {
  const { user, logout } = useAuth();
  return (
    <div className="flex items-center gap-2" data-testid="user-chip">
      <Avatar user={user} />
      <span
        className="hidden max-w-[160px] truncate text-sm text-slate-300 md:inline"
        title={user.email}
      >
        {user.name || user.email}
      </span>
      <button
        type="button"
        onClick={logout}
        title="Sign out"
        className="inline-flex items-center gap-1.5 rounded-full border border-white/10 bg-white/[0.03] px-3 py-1.5 font-mono text-[11px] font-medium text-slate-300 transition-all duration-200 hover:border-white/20 hover:text-white"
        data-testid="sign-out-btn"
      >
        <LogOut className="h-3.5 w-3.5" />
        <span className="hidden sm:inline">Sign out</span>
      </button>
    </div>
  );
}

// Right side of the header: navigation + account only once signed in.
function HeaderActions() {
  const { status } = useAuth();
  const authed = status === "authed";
  return (
    <div className="flex items-center gap-3">
      {authed && (
        <NavLink
          to="/jobs"
          className={({ isActive }) =>
            `inline-flex items-center gap-1.5 rounded-full border px-3.5 py-1.5 font-mono text-[11px] font-medium no-underline transition-all duration-200 ${
              isActive
                ? "border-accent/40 bg-accent/10 text-white shadow-glow-accent"
                : "border-white/10 bg-white/[0.03] text-slate-300 hover:border-accent/40 hover:bg-accent/10 hover:text-white hover:shadow-glow-accent"
            }`
          }
          data-testid="nav-myjobs-link"
        >
          <Film className="h-3.5 w-3.5" />
          My Videos
        </NavLink>
      )}
      <PitchChip />
      {authed && <UserChip />}
    </div>
  );
}

export default function App() {
  return (
    <AuthProvider>
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

            <HeaderActions />
          </div>
        </header>

        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/privacy" element={<Privacy />} />
          <Route path="/" element={<RequireAuth><Home /></RequireAuth>} />
          <Route path="/jobs" element={<RequireAuth><MyJobs /></RequireAuth>} />
          <Route path="/jobs/:jobId" element={<RequireAuth><JobStatus /></RequireAuth>} />
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
            Alphavox — your words, your edit, your insight.
            {" · "}
            <Link to="/privacy" className="text-slate-600 hover:text-slate-300" data-testid="footer-privacy-link">
              Privacy
            </Link>
          </p>
        </footer>
      </div>
    </AuthProvider>
  );
}
