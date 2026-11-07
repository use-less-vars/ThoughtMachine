# Ticket: bug/origin-predicate-disagreement

**DO NOT FIX IN THIS WAVE.** Filed as a pointer only.

**Filed:** 2026-10-31, found while correcting the caller-gate finding (`ticket-bug-unauthenticated-routes-beyond-health.md`) against `dev = 6e01a004`.
**Severity:** MEDIUM — functional, not a hole. The stricter predicate is the one on the `vault_repair` surface, so the disagreement *narrows* who can call two routes; it does not open anything. What it costs is a dead end for non-browser clients and a trap for the next person who tries to unify the two helpers.
**Status:** open; both sides are pinned by tests, so this is a decision, not a patch.

## Finding

`web_ui/backend/origin_policy.py` exports **three** origin predicates with **two** different answers to one question: *what does an absent `Origin`/`Referer` mean?*

| Predicate | Line | Absent `Origin` **and** `Referer` | Used by |
|---|---|---|---|
| `websocket_origin_allowed(origin)` | `origin_policy.py:56` | **allowed** — `if not origin: return True` (`:64-65`), deliberately, for the CLI and integration harnesses (`:59-62`) | the `/ws` handshake (`server.py:1267`), after `_ws_origin` (`:1233`) reads the header |
| `_origin_check_request(request, route_name)` | `origin_policy.py:86` | **allowed**, logged — `logger.info("origin_check: route=%s result=allow_absent ...", route_name)` then `return True` (`:108-113`) | 38 of the 39 mutating HTTP routes |
| `_origin_allowed(request)` | `origin_policy.py:69` | **refused** — `raw = origin or referer or ""`; `if not raw: return False` (`:75-77`) | `GET /api/vault/repair/status` (`vault_repair_routes.py:45`) and `POST /api/vault/repair/apply` (`:87`) |

So the same client, making the same request with no headers, gets **200 from 38 mutating routes and from the WebSocket handshake, and 403 from the two `vault_repair` routes.** The divergence is not accidental: `_origin_check_request`'s docstring names it (`:93-99`, "Deliberately differs from `_origin_allowed` in exactly ONE way: an absent `Origin`/`Referer` pair is a **data condition**, not a refusal"), and `_origin_allowed`'s own docstring states its rule plainly (`:73`, "No header -> False"). Two stated policies, one header, both live.

Call-site census at this tree: 40 `_origin_check_request` call sites (38 mutating + `GET /api/browse` `server.py:3901` + `GET /api/{session_id}/addendum` `addendum_routes.py:318`), 2 `_origin_allowed` call sites (the two `vault_repair` routes), 1 `websocket_origin_allowed` call site (the handshake).

## Blast radius

**Browser: works, by the `Referer` fallback.** `_origin_allowed` does not only refuse; when `Origin` is absent and `Referer` is present it reduces the referrer to `scheme://host` and applies the loopback test (`:78-83`). A same-origin `fetch` from the Web UI carries a `Referer` under the default referrer policy, so the frontend reaches `GET /api/vault/repair/status` normally. The route is not broken in the browser.

**Non-browser: 403, always.** `curl http://127.0.0.1:<port>/api/vault/repair/status` (and `curl -X POST .../apply`) send neither header, so both answer `403 {"error": "origin not allowed"}`. Consequences, in order of how much they matter:
1. The **read-only vault health diagnostic** is the one GET in the API that a terminal cannot call. Every other read surface answers a bare `curl`.
2. The **repair runbook** — the write path an operator would plausibly want from a terminal during an incident — is the other. `POST /api/vault/repair/apply` is also the *only* mutating route that refuses a header-less client.
3. Any script, cron entry or integration harness that talks to the API without a browser inherits the 403 on those two paths and nowhere else, which reads as "these routes are broken" rather than "these routes are stricter".

**Not a security hole.** `_origin_allowed` is the stricter of the two: refusing an absent header narrows the caller set. There is nothing here for an attacker to exploit. The risk is the other direction and it is a future one: this surface is the exception, so a "collapse the duplicate predicates" change will most naturally move it toward the majority policy (**allow-absent**), which would *widen* access to a route that writes into the vault. That is why the split should be a decision with a test, not a tidy-up.

## Evidence that both sides are deliberate

- `web_ui/backend/tests/test_origin_check_request.py:53-62` pins the default predicate's allow-absent branch **and** its single `INFO` line (`d1_probe`).
- `tests/test_api_vault_repair.py:122` `_HEADERS = {"Origin": "http://localhost:5173"}`, and `:148` calls the status route with `headers=_HEADERS` — the read path is never exercised header-less in the suite.
- `tests/test_api_vault_repair.py:328-344` `test_apply_rejects_disallowed_origins` pins **refuse-on-absent** as the first case (no headers → 403) and foreign-origin as the second. Aligning `vault_repair` to the default predicate therefore requires editing an assertion that exists to say the opposite.
- `tests/integration/test_origin_guard_enumeration.py:37-41` pins `vault_repair_apply` as the *single* entry in `STRICT_ORIGIN_NON_GET`, and `:263-276` refuses an unpinned adoption of `_origin_allowed` by any other non-GET route — so a fourth route quietly choosing the strict predicate fails CI, but a route choosing the lax one does not need to declare anything.
- `web_ui/backend/tests/test_boundary_layer_ingress.py:97-104` asserts `vault_repair_routes` re-exports the shared helper — under the docstring "The REST origin guard must be the SAME code the WS handshake uses". The assertion is module-level (`vrr._origin_allowed.__module__ == "web_ui.backend.origin_policy"`), and the handshake actually calls `websocket_origin_allowed`, a *different function with the opposite absent-policy*. The sharing is real at the module, not at the predicate; the docstring overstates it.

## What this is not

- Not a reason to loosen the `vault_repair` guard. The strict side is the safe side; the defect is that the API answers two different things to the same client.
- Not a duplicate of `ticket-bug-unauthenticated-routes-beyond-health.md`. That ticket is about *who may call at all*; this one is about *two answers to one header shape*, and it survives any credential added in front of the API — an authenticated `curl` still gets 403 unless it sets a header the credential does not require.
- Not a claim that the `Referer` fallback is a good mechanism. It is why the browser half works; it is also a second header doing security work, and `docs/design-auth-boundary.md` §10 item 5 already names the split as a thing that should stop being a per-route question.

## Disposition

Needs one decision; all three options are small.

- **(a) Align `vault_repair` to the default — allow-absent + one `INFO` line.** Matches the 38 mutating routes and the WS handshake; removes the only header-less 403 in the API. Cost: `test_apply_rejects_disallowed_origins`'s first assertion flips to "allowed and logged", and the strict-predicate PIN list shrinks to empty (the census test has to keep working with `STRICT_ORIGIN_NON_GET == []`, or `_origin_allowed` is deleted outright).
- **(b) Keep both, and pay for the exception.** Leave the code alone; make the split explicit — one line in each docstring saying *why* the vault-repair surface refuses an absent header (a destructive, operator-initiated write into the vault), plus a test that pins the divergence itself rather than each side separately, so "unify the predicates" cannot land as an accident. Cheapest, keeps the stricter surface.
- **(c) Make `_origin_allowed` unnecessary.** Rework both `vault_repair` routes onto `_origin_check_request` once a credential exists (`docs/design-auth-boundary.md` §5 S3): with an authenticated identity the absent-`Origin` question stops deciding between two callers and the predicate can be single. This is the option the design pass implies, and it is not available in this wave.

**Recommendation:** (b) now — it costs a docstring line and a test, and it protects the stricter surface from a well-meaning refactor — followed by (c) when the credential lands. (a) is defensible and smaller, but it removes the API's only strict origin check on a route that writes to the vault.

Cross-references: `ticket-bug-unauthenticated-routes-beyond-health.md` (whose 2026-10-31 correction lists which predicates are on which routes), `docs/workspace_isolation_matrix.md` §7a and F17, `docs/design-auth-boundary.md` §10 item 5, `web_ui/backend/origin_policy.py`.

**Limit:** established by reading `origin_policy.py`, both `vault_repair` handlers, the four tests above and the call-site census; no live request was issued against a running server, so "curl gets 403" is read off `_origin_allowed:75-77` rather than observed.
