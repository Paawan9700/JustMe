/**
 * Tiny fetch wrapper around the JustMe API.
 *
 * REACT_APP_BACKEND_URL is the external base URL (without trailing slash).
 * All API routes live under /api per Emergent's ingress contract.
 *
 * Every call carries the session token as `Authorization: Bearer ...`
 * (lib/session.js). A 401 means the session is gone — expired, revoked, or
 * never there — so it is cleared and AuthProvider sends the user to /login.
 */

import { clearToken, getToken, notifyUnauthorized } from "./session";

const BASE = process.env.REACT_APP_BACKEND_URL;

const JSON_HEADERS = { "Content-Type": "application/json" };

function withAuth(headers = {}) {
  const token = getToken();
  return token ? { ...headers, Authorization: `Bearer ${token}` } : headers;
}

async function handle(resp) {
  let body = null;
  try { body = await resp.json(); } catch { /* not JSON */ }
  if (!resp.ok) {
    if (resp.status === 401) {
      clearToken();
      notifyUnauthorized();
    }
    const msg = (body && body.detail) ? body.detail : `Request failed (${resp.status})`;
    const err = new Error(msg);
    err.status = resp.status;
    err.body = body;
    throw err;
  }
  return body;
}

export async function createJob(youtubeUrl) {
  const r = await fetch(`${BASE}/api/jobs`, {
    method: "POST",
    headers: withAuth(JSON_HEADERS),
    body: JSON.stringify({ youtube_url: youtubeUrl }),
  });
  return handle(r);
}

export async function getJob(jobId) {
  const r = await fetch(`${BASE}/api/jobs/${jobId}`, { headers: withAuth() });
  return handle(r);
}

// `everyone` is the admin-only view of every user's jobs; for anyone else
// the API ignores it and returns their own.
export async function listJobs({ everyone = false } = {}) {
  const qs = everyone ? "?scope=all" : "";
  const r = await fetch(`${BASE}/api/jobs${qs}`, { headers: withAuth() });
  return handle(r);
}

export async function selectSpeaker(jobId, speakerLabel) {
  const r = await fetch(`${BASE}/api/jobs/${jobId}/select-speaker`, {
    method: "POST",
    headers: withAuth(JSON_HEADERS),
    body: JSON.stringify({ speaker_label: speakerLabel }),
  });
  return handle(r);
}

export async function generateRecommendations(jobId) {
  const r = await fetch(`${BASE}/api/jobs/${jobId}/generate-recommendations`, {
    method: "POST",
    headers: withAuth(JSON_HEADERS),
  });
  return handle(r);
}

// Exchange the ID token Google Identity Services gave us for a session.
export async function loginWithGoogle(credential) {
  const r = await fetch(`${BASE}/api/auth/google`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ credential }),
  });
  return handle(r);
}

// Today's limits for the signed-in user; null limits = unlimited (admin).
export async function getUsage() {
  const r = await fetch(`${BASE}/api/usage`, { headers: withAuth() });
  return handle(r);
}

export async function getMe() {
  const r = await fetch(`${BASE}/api/auth/me`, { headers: withAuth() });
  return handle(r);
}
