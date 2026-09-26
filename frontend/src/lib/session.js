/**
 * The signed-in session token.
 *
 * Kept in localStorage so a refresh or a new tab stays signed in (the backend
 * issues it for AUTH_TOKEN_TTL_DAYS). Storage can throw — Safari private
 * mode, blocked site data — so every access is guarded and falls back to an
 * in-memory copy that lasts for the current tab.
 */

const KEY = "alphavox.session";
let memoryToken = null;

export function getToken() {
  try {
    return window.localStorage.getItem(KEY) || memoryToken;
  } catch {
    return memoryToken;
  }
}

export function setToken(token) {
  memoryToken = token;
  try {
    window.localStorage.setItem(KEY, token);
  } catch {
    /* storage unavailable — the in-memory copy still works for this tab */
  }
}

export function clearToken() {
  memoryToken = null;
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    /* nothing to clear */
  }
}

// AuthProvider registers a handler here; lib/api.js calls it whenever a
// request comes back 401, so an expired or revoked session anywhere in the
// app lands the user back on the sign-in page.
let unauthorizedHandler = null;

export function onUnauthorized(handler) {
  unauthorizedHandler = handler;
  return () => {
    if (unauthorizedHandler === handler) unauthorizedHandler = null;
  };
}

export function notifyUnauthorized() {
  if (unauthorizedHandler) unauthorizedHandler();
}
