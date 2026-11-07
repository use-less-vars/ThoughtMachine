/*
 * apiBase.js — the SINGLE source of truth for API + WebSocket URLs (Wave 3a).
 *
 * The app is always served from the SAME origin as the API and WS endpoints:
 *   - dev:  Vite proxies /api and /ws to the FastAPI backend (vite.config.js).
 *   - prod: a reverse proxy serves the built assets and the API together.
 * So request URLs are same-origin RELATIVE PATHS — no hostname, no scheme
 * should be assembled anywhere else in the codebase.
 */

// Same-origin relative path. No scheme and no `//hostname`: the browser
// resolves it against the page origin, which the proxy routes to the backend.
export function apiUrl(path) {
  return path
}

// WebSocket URL for the session bridge, on the page's own host.
export function wsUrl() {
  const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${scheme}//${window.location.host}/ws`
}
