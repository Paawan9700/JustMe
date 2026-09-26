import React, { useEffect, useRef, useState } from "react";
import { Link, Navigate, useSearchParams } from "react-router-dom";
import { motion } from "framer-motion";
import { AlertTriangle, AudioLines, Loader2 } from "lucide-react";
import { useAuth } from "../lib/auth";

const CLIENT_ID = process.env.REACT_APP_GOOGLE_CLIENT_ID;
const GSI_SRC = "https://accounts.google.com/gsi/client";

// Load Google Identity Services once per page; later mounts reuse it.
let gsiPromise = null;
function loadGoogleIdentity() {
  if (window.google?.accounts?.id) return Promise.resolve();
  if (!gsiPromise) {
    gsiPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = GSI_SRC;
      script.async = true;
      script.defer = true;
      script.onload = () => resolve();
      script.onerror = () => {
        gsiPromise = null;
        reject(new Error(
          "Couldn't load Google sign-in. Check your connection or ad-blocker, then refresh."
        ));
      };
      document.head.appendChild(script);
    });
  }
  return gsiPromise;
}

// Only in-app paths are allowed as a post-login destination: "/jobs/abc"
// yes; "//evil.com", "https://…" or "/login" itself no.
function safeNext(raw) {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//") || raw.startsWith("/login")) {
    return "/";
  }
  return raw;
}

export default function Login() {
  const { status, login } = useAuth();
  const [params] = useSearchParams();
  const next = safeNext(params.get("next"));
  const buttonRef = useRef(null);
  const [error, setError] = useState(
    CLIENT_ID ? null : "Sign-in isn't configured yet (REACT_APP_GOOGLE_CLIENT_ID is missing)."
  );
  const [busy, setBusy] = useState(false);

  // Google keeps whichever callback the first initialize() received, so route
  // it through a ref that always sees the current `login`.
  const onCredentialRef = useRef(null);
  onCredentialRef.current = async (response) => {
    setBusy(true);
    setError(null);
    try {
      await login(response.credential); // status -> "authed" -> <Navigate> below
    } catch (err) {
      setError(err.message || "Sign-in failed. Please try again.");
      setBusy(false);
    }
  };

  const showButton = CLIENT_ID && status !== "authed" && status !== "checking";

  useEffect(() => {
    if (!showButton) return undefined;
    let cancelled = false;
    loadGoogleIdentity()
      .then(() => {
        if (cancelled || !buttonRef.current) return;
        window.google.accounts.id.initialize({
          client_id: CLIENT_ID,
          callback: (response) => onCredentialRef.current(response),
        });
        window.google.accounts.id.renderButton(buttonRef.current, {
          theme: "filled_black",
          size: "large",
          shape: "pill",
          text: "signin_with",
          width: 280,
        });
      })
      .catch((err) => {
        if (!cancelled) setError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, [showButton]);

  if (status === "authed") return <Navigate to={next} replace />;

  if (status === "checking") {
    return (
      <div className="flex min-h-[60vh] flex-col items-center justify-center gap-4 text-slate-500">
        <Loader2 className="h-7 w-7 animate-spin text-accent-soft" />
        <span className="font-mono text-sm">Checking your session…</span>
      </div>
    );
  }

  return (
    <main
      className="mx-auto flex w-full max-w-6xl flex-1 flex-col items-center px-5 py-20 sm:px-8 sm:py-28"
      data-testid="login-page"
    >
      <motion.div
        initial={{ opacity: 0, y: 14 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
        className="glass w-full max-w-md p-8 text-center sm:p-10"
      >
        <span className="mx-auto mb-6 grid h-14 w-14 place-items-center rounded-2xl bg-accent-grad shadow-glow-accent">
          <AudioLines className="h-7 w-7 text-white" strokeWidth={2.5} />
        </span>
        <h1 className="text-2xl font-bold tracking-tight text-white sm:text-3xl">
          Sign in to Alpha<span className="text-accent-soft">vox</span>
        </h1>
        <p className="mt-3 text-slate-400">
          Sign in with your Google account to extract your voice from long videos.
        </p>

        <div className="mt-8 flex min-h-[44px] justify-center" data-testid="google-signin">
          {busy && (
            <span className="inline-flex items-center gap-2 font-mono text-sm text-slate-400">
              <Loader2 className="h-4 w-4 animate-spin text-accent-soft" />
              Signing you in…
            </span>
          )}
          {/* Stays mounted while busy: Google renders into it only once, so
              unmounting would leave no button after a failed attempt. */}
          {showButton && <div ref={buttonRef} className={busy ? "hidden" : ""} />}
        </div>

        {error && (
          <div
            className="mt-6 flex items-start gap-2 rounded-xl border border-bear/30 bg-bear/10 px-4 py-3 text-left text-sm text-bear-soft"
            role="alert"
            data-testid="login-error"
          >
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        <p className="mt-8 font-mono text-[11px] text-slate-600">
          By signing in you agree to how we handle your data in our{" "}
          <Link to="/privacy" className="text-slate-400 hover:text-white">Privacy Policy</Link>.
        </p>
      </motion.div>
    </main>
  );
}
