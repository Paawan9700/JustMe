/**
 * Tiny fetch wrapper around the JustMe API.
 *
 * REACT_APP_BACKEND_URL is the external base URL (without trailing slash).
 * All API routes live under /api per Emergent's ingress contract.
 */

const BASE = process.env.REACT_APP_BACKEND_URL;

async function handle(resp) {
  let body = null;
  try { body = await resp.json(); } catch { /* not JSON */ }
  if (!resp.ok) {
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
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ youtube_url: youtubeUrl }),
  });
  return handle(r);
}

export async function getJob(jobId) {
  const r = await fetch(`${BASE}/api/jobs/${jobId}`);
  return handle(r);
}

export async function listJobs() {
  const r = await fetch(`${BASE}/api/jobs`);
  return handle(r);
}

export async function selectSpeaker(jobId, speakerLabel) {
  const r = await fetch(`${BASE}/api/jobs/${jobId}/select-speaker`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ speaker_label: speakerLabel }),
  });
  return handle(r);
}

export async function generateRecommendations(jobId) {
  const r = await fetch(`${BASE}/api/jobs/${jobId}/generate-recommendations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  return handle(r);
}
