# Ticket: bug/container-lifecycle-events-missing

**Filed:** Gen 43, 2026-10-14 at `d378c206`
**Source:** sub-finding of bug/container-turn-start-reap-kills-running
**Status:** filed, not dispatched.

## Defect

event_log contains no container_created / container_removed events at all, including across the turn where a running container was destroyed. Inv.3 gap — the action happened, no trace. Actively misled diagnosis of the turn-start reap once (v1 blamed session_stop). Arc 2 (self-knowledge) territory. Minimum ask: emit an event on create and on every teardown, with reason. Surfaced by bug/container-turn-start-reap-kills-running.

## Bearing

Filed alongside the primary ticket. Not to be dispatched until the Wave 9
read-first (turn-start reap) returns — the primary read may reshape these.
