# Ticket: bug/provider-apikey-read-unauthenticated

**Filed:** 2026-10-25, from the Gen 50 client-claim red team pass at `24012a89` (base commit).
**Severity:** CRITICAL
**Status:** open; verified by read at `24012a89` — serialiser, route, store path, CORS policy and the pinning test read; `UNRUN: no live session`. Not to be fixed in this wave.

## Finding

`GET /api/providers` (`web_ui/backend/provider_routes.py:70-77`) returns the plaintext `api_key` of every stored provider profile. The emission is `provider_routes.py:57` (`"api_key": profile.api_key`) inside `_safe_profile` (`:50-61`), reached by the comprehension at `:77` over `ProviderManager.list_profiles()` (`:74`). The route has no auth: `router = APIRouter(prefix="/api")` (`:34`) carries no `dependencies=`, the handler `def list_providers():` (`:71`) takes no parameters and so no `Depends`, and the module imports nothing from a security surface (`:19-31` — `APIRouter`, `JSONResponse`, `BaseModel`, `ValidationError`). It is mounted bare, `app.include_router(provider_router)` (`web_ui/backend/server.py:3641`), and the app has no auth layer at all: "This app has no auth layer -- no cookie, no Authorization header" (`server.py:34`, restated at `:1190`). One unauthenticated GET to the loopback port yields every provider key on the machine.

The contradiction is local and sharp. The handler's docstring reads `"""Return all provider profiles as a bare array (safe fields only)."""` (`:72`); the array it returns is built by a function named `_safe_profile` (`:50`) whose body emits the credential at `:57`. "Safe" is used twice, and both times it names a payload carrying a secret.

The claim is not single, and not merely stale. The same misnomer recurs on the WebSocket twin: `server.py:2057` heads the `get_providers` branch `# Return list of provider profiles (safe fields only)`, accumulates into a variable named `safe_profiles` (`:2074`, `:2125`, `:2175`) and puts `"api_key": p.api_key` in it (`:2079`, `:2130`, `:2180`).

What makes this a contract contradiction rather than drift is that the module states the opposite of `:72` openly — "Secret values (``api_key``) are echoed back to the caller on GET/POST — the same fields the WebSocket surface already returns — so the provider management UI can round-trip them" (`:14-16`) — and the echo is pinned by a test: `tests/test_providers_crud.py:79`, `assert listing[0]["api_key"] == "sk-test-key"`. One file therefore asserts both "safe fields only" and "secrets are echoed back deliberately", and the test enforces the second. The wire read is not executed here: `UNRUN: no live session`. The request that would prove it end-to-end is `curl -s localhost:8000/api/providers | jq '.[].api_key'`; the read is *inferred* from `:57`/`:77` plus the pinning test at `:79`, not observed.

## Sites affected

- **HTTP:** `GET /api/providers` — `list_providers` `provider_routes.py:71`, `_safe_profile` emission `provider_routes.py:77`, handler docstring claim `provider_routes.py:72`, module disclosure `provider_routes.py:14-16`.
- **WS:** `get_providers` branch — `server.py:2057` comment "# Return list of provider profiles (safe fields only)", emissions `server.py:2079`, `:2130`, `:2180`.

One ticket, two transports. A fix that touches only the HTTP handler leaves the WS surface live. The fix shape must name both.

## Bearing

Read side. The existing provider tickets cover write and delete (`provider_routes.py:80-84`, `:111-121`) — surfaces that let a caller replace or destroy credential material. This one exfiltrates it. "Cannot mutate" is not "cannot exfiltrate": a caller who cannot write a provider can still read the live key and spend the operator's quota against the upstream vendor, out of band, with no footprint on this host. The pinned test (`tests/test_providers_crud.py:79`) is the load-bearing part for triage: the echo cannot be fixed without editing a green test, so it will survive any hardening pass graded on a clean run.

Tag: `secret-echo-named-safe` — the redaction boundary is asserted in a name and a docstring and implemented nowhere, and the wider contract is pinned by a test written in the same pass that introduced the narrow one.

## Blast radius

Cross-workspace by construction. Provider profiles are not workspace-scoped: the store is the home-level legacy file, `THOUGHTMACHINE_DIR = Path.home() / '.thoughtmachine'` and `PROVIDERS_FILE = THOUGHTMACHINE_DIR / 'providers.json'` (`agent/config/provider_profile.py:14-15`), selected by `ProviderManager.__init__` when no path is passed (`:36-40`), and `provider_routes.py` passes exactly that file — `PROVIDERS_STORE: Path = PROVIDERS_FILE` (`:37`), `ProviderManager(file_path=PROVIDERS_STORE)` (`:47`). No workspace id, project root or vault root enters the path. Every workspace on the machine shares the same `providers.json`, so a GET issued from, or on behalf of, one workspace returns the keys used by all of them. `list_profiles()` returns the whole loaded dict (`provider_profile.py:105-107` over `:53-54`), unfiltered: `_active_profile_id` is never consulted by the route.

Migration note: `agent/config/schema_manifest.json:46` declares a newer in-vault `system/providers.json` layout, but this route is pinned to the home-level legacy file by `provider_routes.py:37`, so the global exposure stands as written and would become vault-scoped rather than disappear on repointing.

The exposure boundary is network, not application. The app binds loopback by default and refuses a non-loopback host without `--allow-remote-bind` (`server.py:27-40`); CORS answers only `http://localhost|127.0.0.1:<TM_FRONTEND_PORT>` (`web_ui/backend/origin_policy.py:42-45`, wired at `server.py:1194-1200`). That stops a browser on an arbitrary site from reading the body, but CORS is not access control for non-browser clients: any local process, any container on the host network, and any client forwarded over SSH can issue the GET.

## What this is not

- Not the write or delete provider ticket. Those describe mutation of provider records; this is read-only, needs no payload and no id. No dedupe: authenticating `POST`/`DELETE` leaves it open.
- Not the WebSocket ticket. `get_providers` (`server.py:2056-2089`) carries the identical defect and needs the identical redaction, but it is a different transport with a different client contract; this ticket owns `GET /api/providers`.
- Not a proven live exploit. `UNRUN: no live session`. Source (`:57`, `:77`) and the pinning test (`tests/test_providers_crud.py:79`) establish the behaviour; nothing here shows a real key leaving a real host.
- Not a claim that the UI is wrong to round-trip a key. The round-trip is real and consumed — `ProviderEditModal.jsx:75` seeds the field from `provider.api_key`. The defect is that it is served to *every* caller, not that it exists.
- Not drive-by reachable from an arbitrary web page: readable origins are limited to loopback dev spellings (`origin_policy.py:42-45`).

## Fix shape

Make the docstring true rather than weaken it: redact inside `_safe_profile`'s body (`provider_routes.py:50-61`) — replace `:57` with a constant, e.g. `"api_key": ""` beside `"api_key_configured": bool(profile.api_key)` — and leave `:72` as written. Two dependants then move with it: the module docstring (`:14-16`) and the pinning test `tests/test_providers_crud.py:79`, which fails by design and should become an assertion that the key is absent.

The one legitimate consumer traced is the edit modal (`web_ui/frontend/src/components/ProviderEditModal.jsx:75`, re-sent at `:117`), and it can tolerate redaction: `api_key` is not a validated field (`ProviderEditModal.jsx:92-105` checks id, label and baseUrl only, and `:112-121` submits whatever the field holds), and the POST path already preserves the stored key when the incoming value is blank (`provider_routes.py:95-97`, mirrored at `server.py:2110-2116`). A blank key from GET therefore round-trips through save without clobbering the secret. Verified by read of those four sites; the modal's on-save wiring is traced no further than `ProviderEditModal.jsx:112-121`.

If plaintext must stay available to a legitimate consumer, name it and gate it in one place: a dedicated route (e.g. `GET /api/providers/{id}/api_key`) behind an app-wide auth layer, never the list endpoint. UNVERIFIED: no auth primitive exists in the tree today (`server.py:34`), so "behind auth" is a design commitment, not an existing control. The same redaction applies to the WS surface at `server.py:2074-2083`, `:2125-2134`, `:2175-2184`.
