# Ticket: bug/cors-wildcard-exposes-api-to-browser-origin

**DO NOT FIX IN THIS WAVE.** Filed as a pointer only.

**Filed:** 2026-10-21, noticed while verifying the unauthenticated-surface claims behind `ticket-bug-unauthenticated-routes-beyond-health.md`.
**Severity:** HIGH — every route on the process is unauthenticated (0 of 82, Cluster 2), so the CORS grant is the only thing standing between a hostile web page and the whole API, and it is configured to allow that page.
**Status:** open; verified by read at `9d5ed869`, not exercised at runtime.

## Finding

The middleware block is four wildcards with no stated policy, at `web_ui/backend/server.py:1103-1112`:

```python
# ── CORS (allow frontend dev server on any port) ────────────────────────────
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

The comment says the intent was "the frontend dev server on any port" — a dev-loop convenience. What it actually grants is: **any** origin, **any** method, **any** header. Combined with the fact that nothing on this server authenticates anything, that means the response body of every route is readable by JavaScript running on any site the user visits while the agent server is up, and every mutating route is reachable from that same script.

`allow_credentials=True` next to `allow_origins=["*"]` is not a separate bug, it is evidence that nobody reasoned about this block: the pair is inert-to-contradictory, because browsers refuse to honour a wildcard origin on a credentialed request. The frontend confirms that nothing needs it — grepping `web_ui/frontend/src` for `withCredentials` returns **zero** occurrences, while 40+ call sites (e.g. `globalApi.js:10,54,67`, `App.jsx:1114,1143`, `WorkerManagementPanel.jsx:608,651`, `RecordContainerPanel.jsx:371`) are plain same-origin `fetch(...)`. (`credentials:` is *not* zero — re-counted at `9d5ed869` it is 11 matches, and every one is a payload/object key rather than a request option: `web_ui/frontend/src/store/workspaceStore.js:104` (`credentials: []` in a draft-state object) and ten JSON fixtures under `components/__tests__/GlobalCredentials.test.jsx` / `WorkspaceSelector.test.jsx` (`jsonOk({ credentials: CREDS })`). An earlier draft of this ticket claimed zero for both spellings; that was wrong. The conclusion is unchanged — no fetch passes an option that would make the browser send credentials cross-origin.) So the credentialed half of this config is dead weight that can be deleted without touching the UI.

The concrete attack does not need a foothold. The user opens the agent web UI (so uvicorn is listening on loopback), then visits any page. That page runs `fetch('http://127.0.0.1:PORT/api/vault/status')` — a simple GET, no preflight, response readable because the server answers `Access-Control-Allow-Origin: *` — and enumerates the surface. Writes are equally open: because `allow_methods` and `allow_headers` are both `*`, a preflight for `PUT`/`POST` with `Content-Type: application/json` is approved, so config writes, workspace registration and container lifecycle calls go through. **Nothing has to be misconfigured further; this is the shipped configuration.**

## Evidence

- `web_ui/backend/server.py:1106-1112` — `allow_origins=["*"]`, `allow_credentials=True`, `allow_methods=["*"]`, `allow_headers=["*"]`.
- `web_ui/backend/server.py:1103` — the comment stating the (dev-only) intent, i.e. the policy was never generalised.
- Frontend: `withCredentials` occurs nowhere under `web_ui/frontend/src`; the 11 `credentials:` hits are object/payload keys, not fetch options (`store/workspaceStore.js:104` plus test fixtures). 40+ `fetch(...)` call sites inspected — the credentialed mode this block advertises is unused.
- Bind hardening default `--host 127.0.0.1` (`web_ui/backend/server.py` ~`:5350`+) — loopback is the *only* remaining control, and CORS is what decides whether a browser on that machine treats loopback as reachable.
- Route inventory: 0 of 82 routes authenticate (Cluster 2, `.thoughtmachine/working_docs/cluster2-http-surface-map.md`).

## What this is not

- Not a claim that another machine can reach the API. It cannot; the default bind is loopback. This ticket is about the user's *own* browser being turned into the transport.
- Not a duplicate of `ticket-bug-unauthenticated-routes-beyond-health.md` (missing auth on routes) nor `ticket-hygiene-inconsistent-localhost-check-pattern.md` (health-gate consistency). This is the cross-origin grant that makes the missing auth exploitable from a web page.
- Not a claim that `allow_credentials=True` currently leaks cookies — the app sets none. It is dead weight *and* a misleading signal, not an active leak on its own.
- Not a WebSocket finding — browser CORS does not apply to `ws://` at all, which is why the socket needs its own ticket (`ticket-bug-ws-commands-unauthenticated.md`).

## Disposition

Someone has to decide whether this API is ever meant to be callable from a different origin. If not — and the same-origin frontend says not — delete the middleware, or replace it with an explicit allowlist of the dev-server origin read from config, and drop `allow_credentials` unconditionally. If yes, that is a much larger decision than a dev-loop convenience, and it needs an auth story first (F30 territory), because an allowlisted origin with no auth is only marginally better than `*`.

Two one-line candidate changes, for whoever picks this up:

- **A. Drop `allow_credentials=True`** (`server.py:1109`), leaving `allow_origins=["*"]`. One line, zero effect on the shipped UI (nothing sends credentials), and it removes a wildcard-plus-credentials pair that browsers refuse to honour anyway. It does **not** narrow who can read the API — that is the point of the next one.
- **B. Keep `allow_credentials`, narrow the origins** to the loopback dev-server shape already compiled for this purpose at `web_ui/backend/vault_repair_routes.py:22-23` (`^https?://(?:localhost|127\.0\.0\.1)(?::\d+)?$`), read from config, and drop `allow_methods`/`allow_headers` to the set the frontend actually uses.

Recommendation: **A as an immediate one-liner, then B**. A alone leaves `allow_origins=["*"]`, so any page can still read every route — it only stops advertising credentials. B is the change that actually reduces reach; it is still not authentication, so it stays a browser-boundary control (see `ticket-bug-ws-handshake-bypasses-cors-origin-check.md` for the transport where even that does not apply).

Cross-reference: F30, `ticket-bug-unauthenticated-routes-beyond-health.md`, `ticket-bug-ws-commands-unauthenticated.md`, `ticket-hygiene-inconsistent-localhost-check-pattern.md`.
