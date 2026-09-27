import React, { useEffect, useState } from "react";
import { Link, NavLink } from "react-router-dom";
import { AnimatePresence, MotionConfig, motion } from "framer-motion";
import { ArrowRight, AudioLines, Film, Heart, LogOut } from "lucide-react";
import { useAuth } from "../lib/auth";

// The three plain-language steps of what Alphavox does, in journey order.
// Shown as a "signal path" next to the brand so a first-time visitor gets the
// pitch at a glance: each step lights up in turn, then the loop restarts.
const PITCH_STEPS = ["Paste a link", "Pick your voice", "Get just you"];
const STEP_MS = 2400;
const EASE = [0.22, 1, 0.36, 1];

const FOCUS_RING =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 focus-visible:ring-offset-2 focus-visible:ring-offset-ink-950";

function Divider({ className = "" }) {
  return <span aria-hidden="true" className={`h-6 w-px shrink-0 bg-white/10 ${className}`} />;
}

// Wide screens: all three steps on one line, joined by connectors that fill
// as the signal travels from one step to the next.
function PitchPath({ active }) {
  return (
    <ol className="hidden items-center xl:flex" aria-label="How Alphavox works">
      {PITCH_STEPS.map((label, idx) => {
        const isActive = idx === active;
        const isDone = idx < active;
        return (
          <li key={label} className="flex items-center">
            {idx > 0 && (
              <span aria-hidden="true" className="relative mx-3 h-px w-7 overflow-hidden rounded-full bg-white/10">
                <motion.span
                  className="absolute inset-0 origin-left bg-gradient-to-r from-accent to-accent-blue"
                  initial={false}
                  animate={{ scaleX: idx <= active ? 1 : 0 }}
                  transition={{ duration: idx <= active ? 0.55 : 0.3, ease: EASE }}
                />
              </span>
            )}
            <span className="flex items-center gap-2">
              <span
                aria-hidden="true"
                className={`relative h-1.5 w-1.5 rounded-full transition-[background-color,box-shadow] duration-300 ${
                  isActive
                    ? "bg-accent shadow-[0_0_10px_2px_rgba(124,92,255,0.6)] delay-[350ms]"
                    : isDone
                      ? "bg-accent/60"
                      : "bg-white/20"
                }`}
              >
                {isActive && (
                  <span className="absolute inset-0 rounded-full bg-accent motion-safe:animate-ping" />
                )}
              </span>
              <span
                className={`whitespace-nowrap font-mono text-[11px] font-medium transition-colors duration-300 ${
                  isActive ? "text-white delay-[350ms]" : isDone ? "text-slate-400" : "text-slate-500"
                }`}
              >
                {label}
              </span>
            </span>
          </li>
        );
      })}
    </ol>
  );
}

// Medium screens: progress ticks plus the current step's name.
function PitchTicker({ active }) {
  return (
    <div className="hidden items-center gap-2.5 md:flex xl:hidden" aria-hidden="true">
      <span className="flex items-center gap-1">
        {PITCH_STEPS.map((label, idx) => (
          <span
            key={label}
            className={`h-1 rounded-full transition-all duration-500 ${
              idx === active ? "w-4 bg-accent" : idx < active ? "w-1.5 bg-accent/60" : "w-1.5 bg-white/15"
            }`}
          />
        ))}
      </span>
      <span className="relative inline-grid min-w-[104px] overflow-hidden">
        <AnimatePresence mode="wait" initial={false}>
          <motion.span
            key={active}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            transition={{ duration: 0.32, ease: EASE }}
            className="col-start-1 row-start-1 whitespace-nowrap font-mono text-[11px] font-medium text-slate-300"
          >
            {PITCH_STEPS[active]}
          </motion.span>
        </AnimatePresence>
      </span>
    </div>
  );
}

function PitchLoop() {
  const [active, setActive] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setActive((prev) => (prev + 1) % PITCH_STEPS.length), STEP_MS);
    return () => clearInterval(id);
  }, []);
  return (
    <>
      <PitchPath active={active} />
      <PitchTicker active={active} />
    </>
  );
}

function MyVideosLink() {
  return (
    <NavLink
      to="/jobs"
      className={({ isActive }) =>
        `group inline-flex h-9 items-center gap-2 rounded-xl border px-3 text-[13px] font-semibold no-underline transition-all duration-200 sm:h-10 sm:px-4 sm:text-sm ${FOCUS_RING} ${
          isActive
            ? "border-transparent bg-accent-grad text-white shadow-glow-accent"
            : "border-accent/30 bg-accent/10 text-white hover:-translate-y-px hover:border-accent/60 hover:bg-accent/20 hover:shadow-glow-accent"
        }`
      }
      data-testid="nav-myjobs-link"
    >
      {({ isActive }) => (
        <>
          <Film
            className={`hidden h-4 w-4 min-[400px]:block ${isActive ? "text-white" : "text-accent-soft"}`}
          />
          My Videos
          {!isActive && (
            <ArrowRight className="-ml-0.5 hidden h-3.5 w-3.5 text-accent-soft transition-transform duration-200 group-hover:translate-x-0.5 sm:block" />
          )}
        </>
      )}
    </NavLink>
  );
}

// Secondary to My Videos: icon-only on phones, labelled from sm up.
function FavoritesLink() {
  return (
    <NavLink
      to="/favorites"
      aria-label="Favourite voices"
      title="Favourite voices"
      className={({ isActive }) =>
        `inline-flex h-9 items-center gap-2 rounded-xl border px-2.5 text-[13px] font-semibold no-underline transition-all duration-200 sm:h-10 sm:px-3.5 sm:text-sm ${FOCUS_RING} ${
          isActive
            ? "border-rose-400/40 bg-rose-400/15 text-white"
            : "border-white/10 bg-white/[0.03] text-slate-300 hover:border-rose-400/40 hover:text-white"
        }`
      }
      data-testid="nav-favorites-link"
    >
      {({ isActive }) => (
        <>
          <Heart className={`h-4 w-4 text-rose-300 ${isActive ? "fill-current" : ""}`} />
          <span className="hidden sm:inline">Favourites</span>
        </>
      )}
    </NavLink>
  );
}

// Google profile photo in a brand-gradient ring, falling back to an initial
// if it fails to load.
function Avatar({ user }) {
  const [failed, setFailed] = useState(false);
  const initial = (user.name || user.email || "?").trim().charAt(0).toUpperCase();
  return (
    <span className="shrink-0 rounded-full bg-accent-grad p-[1.5px] shadow-[0_0_18px_-6px_rgba(124,92,255,0.7)]">
      {user.picture && !failed ? (
        <img
          src={user.picture}
          alt=""
          referrerPolicy="no-referrer"
          onError={() => setFailed(true)}
          className="block h-8 w-8 rounded-full border-2 border-ink-950 bg-ink-900 object-cover"
        />
      ) : (
        <span className="grid h-8 w-8 place-items-center rounded-full border-2 border-ink-950 bg-ink-800 text-xs font-semibold text-slate-100">
          {initial}
        </span>
      )}
    </span>
  );
}

function Account() {
  const { user } = useAuth();
  return (
    <div className="flex min-w-0 items-center gap-3" title={user.email} data-testid="user-chip">
      <Avatar user={user} />
      <div className="hidden min-w-0 flex-col leading-tight lg:flex">
        <span className="max-w-[150px] truncate text-sm font-medium text-slate-100">
          {user.name || user.email}
        </span>
        {user.name && (
          <span className="max-w-[150px] truncate font-mono text-[11px] text-slate-500">
            {user.email}
          </span>
        )}
      </div>
    </div>
  );
}

function SignOutButton() {
  const { logout } = useAuth();
  return (
    <button
      type="button"
      onClick={logout}
      aria-label="Sign out"
      className={`group relative grid h-9 w-9 shrink-0 place-items-center rounded-xl border border-white/10 bg-white/[0.03] text-slate-400 transition-all duration-200 hover:border-bear/40 hover:bg-bear/10 hover:text-bear-soft sm:h-10 sm:w-10 ${FOCUS_RING}`}
      data-testid="sign-out-btn"
    >
      <LogOut className="h-4 w-4 transition-transform duration-200 group-hover:translate-x-0.5" />
      <span
        aria-hidden="true"
        className="pointer-events-none absolute right-0 top-[calc(100%+8px)] whitespace-nowrap rounded-lg border border-white/10 bg-ink-800 px-2.5 py-1 font-mono text-[11px] text-slate-200 opacity-0 shadow-glass transition-opacity duration-150 group-hover:opacity-100 group-focus-visible:opacity-100"
      >
        Sign out
      </span>
    </button>
  );
}

// Solidifies once the page scrolls under it, so it reads as part of the page
// at rest and as a distinct bar while content passes beneath.
function useScrolled(threshold = 8) {
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > threshold);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [threshold]);
  return scrolled;
}

export default function TopBar() {
  const { status } = useAuth();
  const authed = status === "authed";
  const scrolled = useScrolled();

  return (
    <MotionConfig reducedMotion="user">
      <header
        className={`sticky top-0 z-30 backdrop-blur-xl transition-[background-color,box-shadow] duration-300 ${
          scrolled ? "bg-ink-950/80 shadow-[0_12px_32px_-16px_rgba(0,0,0,0.8)]" : "bg-ink-950/40"
        }`}
        data-testid="top-bar"
      >
        <div className="mx-auto flex h-16 w-full max-w-6xl items-center justify-between gap-4 px-5 sm:h-[72px] sm:px-8">
          <div className="flex min-w-0 items-center gap-5">
            <Link
              to="/"
              className={`group flex shrink-0 items-center gap-2.5 rounded-xl no-underline ${FOCUS_RING}`}
              data-testid="brand-home-link"
            >
              <span className="grid h-9 w-9 place-items-center rounded-xl bg-accent-grad shadow-glow-accent transition-transform duration-200 group-hover:scale-105">
                <AudioLines className="h-5 w-5 text-white" strokeWidth={2.5} />
              </span>
              <span className="text-lg font-bold tracking-tight text-white">
                Alpha<span className="text-accent-soft">vox</span>
              </span>
            </Link>
            <Divider className="hidden md:block" />
            <PitchLoop />
          </div>

          {authed && (
            <nav className="flex shrink-0 items-center gap-2 sm:gap-3" aria-label="Primary">
              <FavoritesLink />
              <MyVideosLink />
              <Divider className="mx-1 hidden sm:block" />
              <Account />
              <SignOutButton />
            </nav>
          )}
        </div>
        <span
          aria-hidden="true"
          className="absolute inset-x-0 bottom-0 h-px bg-gradient-to-r from-transparent via-accent/35 to-transparent"
        />
      </header>
    </MotionConfig>
  );
}
