import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { Navigate, useLocation } from "react-router-dom";
import { AlertTriangle, Loader2, RefreshCw } from "lucide-react";
import { getMe, loginWithGoogle } from "./api";
import { clearToken, getToken, onUnauthorized, setToken } from "./session";

const AuthContext = createContext(null);

// status:
//   "checking" — a stored token exists and is being validated with /auth/me
//   "authed"   — signed in; `user` is set
//   "anon"     — not signed in
//   "error"    — couldn't reach the server to validate (token kept; retry)
export function AuthProvider({ children }) {
  const [status, setStatus] = useState(() => (getToken() ? "checking" : "anon"));
  const [user, setUser] = useState(null);
  const [error, setError] = useState(null);

  const signOutLocally = useCallback(() => {
    setUser(null);
    setStatus("anon");
  }, []);

  // Any 401 from any API call ends the session.
  useEffect(() => onUnauthorized(signOutLocally), [signOutLocally]);

  const check = useCallback(async () => {
    setStatus("checking");
    setError(null);
    try {
      const me = await getMe();
      setUser(me);
      setStatus("authed");
    } catch (err) {
      // 401: the unauthorized handler already signed us out.
      if (err.status === 401) return;
      setError(err);
      setStatus("error");
    }
  }, []);

  // Validate a token left over from a previous visit.
  useEffect(() => {
    if (getToken()) check();
  }, [check]);

  const login = useCallback(async (credential) => {
    const res = await loginWithGoogle(credential);
    setToken(res.token);
    setUser(res.user);
    setError(null);
    setStatus("authed");
    return res.user;
  }, []);

  const logout = useCallback(() => {
    clearToken();
    try {
      // Stop Google from silently re-selecting this account next time.
      window.google?.accounts?.id?.disableAutoSelect();
    } catch {
      /* Google script not loaded on this page — nothing to reset */
    }
    signOutLocally();
  }, [signOutLocally]);

  const value = useMemo(
    () => ({ status, user, error, login, logout, retry: check }),
    [status, user, error, login, logout, check]
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}

// Route guard: renders the page when signed in, otherwise sends the user to
// /login and remembers where they were headed (so an opened job link still
// lands on that job after signing in).
export function RequireAuth({ children }) {
  const { status, error, retry } = useAuth();
  const location = useLocation();

  if (status === "authed") return children;

  if (status === "checking") {
    return (
      <div
        className="flex min-h-[60vh] flex-col items-center justify-center gap-4 text-slate-500"
        data-testid="auth-checking"
      >
        <Loader2 className="h-7 w-7 animate-spin text-accent-soft" />
        <span className="font-mono text-sm">Checking your session…</span>
      </div>
    );
  }

  if (status === "error") {
    return (
      <main className="mx-auto w-full max-w-5xl flex-1 px-5 py-12 sm:px-8" data-testid="auth-error">
        <div className="glass mx-auto max-w-xl p-8 text-center">
          <span className="mx-auto mb-5 grid h-14 w-14 place-items-center rounded-2xl border border-bear/30 bg-bear/10">
            <AlertTriangle className="h-7 w-7 text-bear" />
          </span>
          <h2 className="text-2xl font-bold text-white">Couldn&rsquo;t reach the server</h2>
          <p className="mt-3 text-slate-400">{error?.message || "Please try again in a moment."}</p>
          <button type="button" className="btn-ghost mx-auto mt-6" onClick={retry}>
            <RefreshCw className="h-4 w-4" />
            Try again
          </button>
        </div>
      </main>
    );
  }

  const next = `${location.pathname}${location.search}`;
  return <Navigate to={`/login?next=${encodeURIComponent(next)}`} replace />;
}
