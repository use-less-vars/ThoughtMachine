# Ticket (hygiene): panel polls status endpoints, never subscribes to the bus

**Frame / Tag:** hygiene
**Status:** open (FILE ONLY — no fix in wave 9c)
**Scope:** web UI status panel

## Summary

The web-UI status panel refreshes container/workspace state by **polling**
status endpoints on a timer (e.g. `/api/container/status`,
`/api/workspace/{id}/containers/{name}/status`, and the analogous
`CheckSystem`-backed routes). It never subscribes to the `EventBus` /
websocket, so it cannot receive push updates and depends entirely on the poll
interval for freshness.

## Why it matters (hygiene, not correctness)

- **Latency vs. cost tradeoff is unbounded in the wrong direction.** Making the
  panel feel live forces a short poll interval, which multiplies request volume
  linearly with the number of open panels — all to observe state transitions the
  `EventBus` already emits.
- **Missed/duplicated transitions.** A poll can miss a transient state (e.g. a
  container that starts and exits between ticks) that an event stream would
  deliver.
- **Divergent truth.** Polled status and push-driven state can disagree for up
  to one interval, which is exactly the class of display-truth drift wave 9c is
  otherwise tightening (see M1/M2/M4).

## Evidence / pointers

- Status routes consumed by the panel: `/api/container/status`,
  `/api/workspace/{id}/containers/{name}/status`.
- `CheckSystem` `container_status` / `runtime_state` queries (server-rendered
  status) are polled rather than event-driven.
- `EventBus` exists and publishes container/workspace lifecycle events; the
  panel does not subscribe.

## Proposed direction (for a future ticket, NOT this one)

- Subscribe the panel to the `EventBus` / websocket for container + workspace
  lifecycle events.
- Keep polling only as a low-frequency reconciler (or drop it entirely), so the
  panel is push-first and poll-fallback.

## Non-goals

This ticket is **file-only** documentation. No code change is made in wave 9c.

## Amendment 2026-10-20 (gen-47 run)

**Report cross-ref:** `.thoughtmachine/working_docs/report-gen47-container-ops-run-2026-10-20.md` §Step 2 (line 65), §Step 5 (line 109).

Same reproduction as `ticket-bug-live-view-container-stale-no-push.md`: on build `124477b7` (2026-10-20) a newly created container and a removal were both invisible to the panel until an explicit refresh. Poll-only consumption is the confirmed mechanism; the freshness window is one poll interval and no push path is subscribed.
