import React from "react";
import { Routes, Route, Link } from "react-router-dom";
import Home from "./pages/Home";
import JobStatus from "./pages/JobStatus";

export default function App() {
  return (
    <div className="shell" data-testid="app-shell">
      <header className="shell-top">
        <Link to="/" className="brand" data-testid="brand-home-link">
          just<span className="dot">.</span>me
        </Link>
        <span className="brand-small">v0.2 — m2</span>
      </header>

      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/jobs/:jobId" element={<JobStatus />} />
        <Route
          path="*"
          element={
            <main data-testid="not-found">
              <p className="helper-text">Nothing here. <Link to="/">Go home</Link>.</p>
            </main>
          }
        />
      </Routes>
    </div>
  );
}
