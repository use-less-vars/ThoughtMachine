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
