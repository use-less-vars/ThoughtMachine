# Ticket: bug/live-view-container-stale-no-push

**Filed:** Gen 43, 2026-10-14 at `d378c206`
**Source:** operator observation during live use — not a dispatched read.
**Severity:** Medium — visibility only, no data loss, no security impact.
**Status:** filed, not dispatched. Candidate for the UI sprint.

## Defect

New containers do not appear in the live view panel until the operator
manually reloads (F5). The panel shows stale state — a container can be
created and the panel does not reflect it.

## Bearing

- Layer 4 (Interface). Inv.5-adjacent: the UI does not show what the
  core knows. The agent (and operator) cannot see container state without
  a manual refresh.
- Related to but distinct from `bug/checksystem-containers-denied-by-
  allowlist` (agent-side visibility) — this is the operator-side surface.
- Same family as the turn-start reap's diagnostic gap: container state is
  invisible from multiple angles.

## Not established

- Whether the panel lacks a WebSocket push, a poll, or both.
- Whether this is a known WS under-test (status.md names WS as
  under-tested, "dead buttons live here").
- Whether the fix is a WS push, a poll interval, or a manual refresh
  affordance made honest.

## Suggested placement

UI sprint. Not Wave 9. Not blocking 8b or 9.

## Amendment 2026-10-20 (gen-47 run)

**Report cross-ref:** `.thoughtmachine/working_docs/report-gen47-container-ops-run-2026-10-20.md` §Step 2 (line 65), §Step 5 (line 109).

Confirmed live 2026-10-20 on current build (`124477b7`): a newly created ephemeral container did not appear in the panel live view until an explicit refresh. During the run the container set changed twice (create at §Step 2, removal at §Step 9) and the panel showed neither without a manual reload — the stale window is not tied to one transition type. Same root as `ticket-hygiene-panel-polls-no-subscribe.md`.
