# Ticket: bug/unauthenticated-routes-beyond-health

**DO NOT FIX IN THIS WAVE.** Filed as a pointer only.

**Filed:** 2026-10-21, noticed while implementing F30(d) (refuse non-loopback bind).
**Severity:** HIGH — the Web UI API has no authentication at all; process locality is the only boundary.
**Status:** open; pointer, **not** an exhaustive map — Cluster 2 owns the full route inventory.

## Finding

F30 spends its effort protecting the **least** sensitive endpoint in the app. `GET /health`,
`/api/health` and `/api/health/containers` now answer localhost callers only; everything else on
the same uvicorn process is still reachable by any client that can route to the port, with no
credential, token, or session check.

F30(d) (`--allow-remote-bind`) makes the size of that blast radius explicit at startup instead of
leaving it implicit, but it does not shrink it.

## Candidate routes (verify by reading; not exhaustive, not authoritative line numbers)

- `/ws` websocket — `web_ui/backend/server.py` (~`:1100-1130`): the agent bridge; a connected client
  drives sessions directly (start/continue/pause/stop, config apply, session save/load).
- `/api/vault/status` — `web_ui/backend/server.py` (~`:3308`, immediately below `/api/health`):
  the first route *after* the F30 gate block, i.e. gated for visibility but not for access.
- `/api/vault/*` and `/api/containers*` — vault read/write and container lifecycle routes; several
  appear in the same module and in `web_ui/backend/routes/*.py`.
- Path-browsing routes (`/api/browse`, `/api/browse/create`) — see `tests/test_host_api_hardening.py`,
  which documents the path-confinement rules but no auth requirement.

Every item above needs confirming by read before it is quoted as fact. This ticket exists so the
question is not lost, **not** to enumerate it.

## What this is not

- Not a claim that F30 is wrong. F30's gate is correct and cheap; it just guards the least valuable
  target, and it should not be read as "the Web UI is protected".
- Not a duplicate of the F30 work. F30 = "do not publish the API on a non-loopback interface by
  accident"; this = "when it *is* published, nothing authenticates the caller".

## Disposition

Needs a decision above this ticket: either (a) authentication in front of the API (token file in
the vault, checked by a single dependency on every router), or (b) an explicit, documented statement
that the Web UI is a localhost-only appliance and any remote exposure is the operator's problem —
in which case F30(d)'s refusal is the whole policy and this ticket closes as "documented".

Cross-reference: F30, `ticket-hygiene-inconsistent-localhost-check-pattern.md`.

---

## R3 sweep update (2026-10-31) — the gate is on 3 surfaces, not 1

This ticket was filed against `/health`. The W3-R3 residue sweep widened the question to *every* HTTP/WS surface in `web_ui/backend/` and the answer is broader than filed: **the gate exists on three surfaces, and 82 of the 85 route decorators plus the whole `/ws` command surface have none.**

Gated (loopback peer required; parity across the implementations is pinned by `web_ui/backend/tests/test_local_gate_shared.py`):
- `GET /health` — `server.py:4071` route, gate `server.py:4073` (imported shared `local_gate.localhost_only`, import at `server.py:224`)
- `GET /api/health/containers` — `server.py:5896` route, gate `server.py:5910` (same shared import)
- `GET /api/system/health` — `health_routes.py:215` route (`@router.get("/health")` on the `prefix="/api/system"` router, `:20`), gate call `health_routes.py:234` (helper defined at `:196`). This module is FROZEN for W3 and does **not** import `web_ui/backend/local_gate.py` — it still defines its own private `_LOCAL_CLIENT_HOSTS` (`:187`), `_TEST_HARNESS_CLIENT_HOSTS` (`:193`) and `def _localhost_only` (`:196`), and the route calls that local copy. Only `server.py` imports the shared module. That third, independent copy is the accepted residual of the R2 work: **pinned, not eliminated**, by `web_ui/backend/tests/test_local_gate_shared.py` (it imports all three implementations and requires identical verdicts and identical 403 bodies)

Not gated (no peer check anywhere on the path):
- all other HTTP routes — the machine-wide inventory routes (`/api/global/summary`, F11), the global credential-name list (`/api/credentials`), the unfiltered `/api/session/list` (and the id-only `/api/session/{id}`, `/{id}/permissions` GET/PUT), `/api/container-records` and the record-id actions, and every workspace-config CRUD route;
- `WS /ws` (`server.py:1260`) — the handshake never reads `request.client`, so every §5.1 command is reachable by any caller that can open a socket.

Reachability therefore rests on `_validate_bind_host` (`server.py:4036`, called once from `main()` at `server.py:6224`) — a start-up check on the *listen address*, not a per-request check on the *caller*. It is waived by `--allow-remote-bind`, and it is transparent to any reverse proxy / port-forward / container network / `ssh -L` tunnel that re-originates a remote connection from a loopback peer.

Recorded in `docs/workspace_isolation_matrix.md` §5.5 (new table) and §7 F17. **Not fixed by W3** (out of the residual scope: it is an authentication boundary, not a workspace-scoping one), and the sweep deliberately does not adjudicate authorisation — see §5.5's scope-limits paragraph.

---

## Correction (2026-10-31) — the tree has more checks than the R3 update says, and the residual is narrower

Appended after a later read of the same tree (`dev = 6e01a004`). Nothing above is rewritten; where this section disagrees with it, this section is what was read out of the code. **The correction does not close the ticket** — the unauthenticated-caller finding stands, smaller than filed.

**1. There is an `Origin` guard on every mutating route, and on the socket.**
- 38 of the 39 mutating HTTP routes (POST 24 / DELETE 7 / PUT 6 / PATCH 2) call `_origin_check_request` (`web_ui/backend/origin_policy.py:86`), which refuses a foreign `Origin`/`Referer` with 403.
- The 39th, `POST /api/vault/repair/apply`, calls the stricter `_origin_allowed` (`origin_policy.py:69`) at `vault_repair_routes.py:87`; `GET /api/vault/repair/status` does the same at `:45`.
- The `WS /ws` handshake validates `Origin` **before** `accept()` (`server.py:1266-1269`, via `websocket_origin_allowed` `origin_policy.py:56-66`), rejecting with close 1008.
- Exactly 3 surfaces peer-gate (`server.py:4073`, `:5910`, `health_routes.py:234`).

**2. So the residual is three things, not "no gate anywhere".**
- (i) **42 of the 45 GET routes carry no origin guard** — the exceptions are `GET /api/browse` (`server.py:3901`), `GET /api/{session_id}/addendum` (`addendum_routes.py:318`) and `GET /api/vault/repair/status` (`vault_repair_routes.py:45`). Intersecting with the 3 peer-gated GETs leaves **39 of 45 GETs with neither check**.
- (ii) **No `Host` validation anywhere** (0 hits for a `Host` read, `TrustedHostMiddleware`, `Sec-Fetch-*`). That is what makes DNS rebinding the live bypass: a rebound page is same-origin with the host it dials, so its `GET`s arrive with no `Origin` and are answered, while its mutations carry a foreign `Origin` and are refused. The guard protects the write half, not the read half — and the read half is where the machine-wide inventory (`/api/global/summary`), `/api/session/list` and `/api/credentials` live.
- (iii) **The GET half has no structural enforcement.** The mutating half does: `tests/integration/test_origin_guard_enumeration.py` AST-enumerates every route, asserts each non-GET route calls `_origin_check_request` or is pinned in `STRICT_ORIGIN_NON_GET`, and cross-checks that enumeration against `app.openapi()`. GETs are deliberately exempt (`FILESYSTEM_GET_MUST_GUARD` is a must-cover list, not an only-list, `:302-304`), so an unguarded GET fails no test.

**3. Arithmetic.** 85 decorators = **84 HTTP** + **1 WS**. "82 of the 85 route decorators plus the whole `/ws` command surface" (R3 sweep update, paragraph 1) reads as though `/ws` were outside the 85 — it is `server.py:1260`, one of them. Ungated HTTP = **81**; ungated surfaces = 81 + `/ws` = **82 of 85**. "82 of 85 routes" is right; "82 of 85 *HTTP* routes" (`docs/workspace_isolation_matrix.md` §5.5 and F17) is not, and has been corrected there.

**4. Honest limit.** All of the above is a code read plus greps, not a probe: no DNS-rebinding attempt was run against a listening server, so (ii) is a property of the code ("no check exists, the bypass is available in principle"), not a reproduced exploit.

Cross-references: `docs/workspace_isolation_matrix.md` §7a (the same correction in evidence shape), `ticket-bug-origin-predicate-disagreement.md` (the two predicates behind these guards disagree about a *missing* header, which is a functional defect rather than a hole), `docs/design-auth-boundary.md` §10.

