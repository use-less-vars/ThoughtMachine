# Ticket: fact5-uptime-seconds-semantics

**Filed:** Gen 45, 2026-10-17, from the manual container protocol.
**Severity:** low — Fact 5 (Observability) label lie.
**Status:** filed.

## Finding

`uptime_seconds` reads as elapsed-since-creation, not running time.
Verified: an `exited` container's `uptime_seconds` continues to advance
(1201 → 1650 → 2034 across the protocol) while the container is stopped.

Field name suggests runtime. It isn't. And it can't tell you when a
container stopped or how long it ran.

## Fix

Either:
- Rename to `created_seconds_ago` (accurate but less useful), or
- Split into `created_at` + `running_since` (accurate and more useful —
  `running_since` is null when not running), or
- Fix the computation to report actual running time (frozen on exit).

Recommend split, per Fact 5's purpose.
