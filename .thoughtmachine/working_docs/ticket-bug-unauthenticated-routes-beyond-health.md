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
