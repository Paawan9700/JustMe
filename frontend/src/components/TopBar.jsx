import React, { useEffect, useState } from "react";
import { Link, NavLink } from "react-router-dom";
import { MotionConfig } from "framer-motion";
import { ArrowRight, AudioLines, Film, Heart, LogOut } from "lucide-react";
import { useAuth } from "../lib/auth";

const FOCUS_RING =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 focus-visible:ring-offset-2 focus-visible:ring-offset-ink-950";

function Divider({ className = "" }) {
  return <span aria-hidden="true" className={`h-6 w-px shrink-0 bg-white/10 ${className}`} />;
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

// Secondary to My Videos, so it's an icon button with a hover label — the
// same pattern as Sign out.
function FavoritesLink() {
  return (
    <NavLink
      to="/favorites"
      aria-label="Favourite voices"
      className={({ isActive }) =>
        `group relative grid h-9 w-9 shrink-0 place-items-center rounded-xl border transition-all duration-200 sm:h-10 sm:w-10 ${FOCUS_RING} ${
          isActive
            ? "border-rose-400/40 bg-rose-400/15"
            : "border-white/10 bg-white/[0.03] hover:border-rose-400/40 hover:bg-rose-400/10"
        }`
      }
      data-testid="nav-favorites-link"
    >
      {({ isActive }) => (
        <>
          <Heart className={`h-4 w-4 text-rose-300 ${isActive ? "fill-current" : ""}`} />
          <span
            aria-hidden="true"
            className="pointer-events-none absolute left-1/2 top-[calc(100%+8px)] -translate-x-1/2 whitespace-nowrap rounded-lg border border-white/10 bg-ink-800 px-2.5 py-1 font-mono text-[11px] text-slate-200 opacity-0 shadow-glass transition-opacity duration-150 group-hover:opacity-100 group-focus-visible:opacity-100"
          >
            Favourite voices
          </span>
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
