import React from "react";
import { Routes, Route, Link } from "react-router-dom";
import { AuthProvider, RequireAuth } from "./lib/auth";
import TopBar from "./components/TopBar";
import Home from "./pages/Home";
import JobStatus from "./pages/JobStatus";
import Login from "./pages/Login";
import MyJobs from "./pages/MyJobs";
import Privacy from "./pages/Privacy";

export default function App() {
  return (
    <AuthProvider>
      <div className="min-h-screen flex flex-col" data-testid="app-shell">
        <TopBar />

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
