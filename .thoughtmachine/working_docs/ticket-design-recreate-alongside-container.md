# Ticket: design/recreate-alongside-container

**Filed:** Gen 44, 2026-10-15, from T1b worker report (wave9a-t1b-report.md).
**Severity:** design — names a latent shape decision.
**Status:** filed, not dispatched. Rule at 9a close.

## The distinction (worker's, sharpened)

Two different situations were conflated in Main's §9 ruling:

- **Situation A — deliberate envelope change (B1).** `self.container` — the
  executor's own live object. Drift because OUR resolved permissions changed.
  Recreating is the EFFECT of the permission change. `tests/trust/test_toggle_live.py`
  pins this. **Recreate.**
- **Situation B — drift found on a container we did not create (B3).**
  `containers.get(name)` — a container found by lookup. May be another session's
  workspace property, or its own drift. **Refuse.**

T1b applies refuse to both. T1b narrows to B3 only. Main ruled the refuse
shape; it maps to Situation B, not A.

## The recreate-alongside idea, parked

Main's §9 said "recreate alongside the drifted one." The worker found this is
NOT implementable in the current code shape:

- Container selection is a single exact-name `containers.get(name)`.
- The create path reuses that same canonical name.
- Falling through collides with / would destroy the drifted container by another
  route.

**Ruling when 9a closes:** do we want a naming strategy that allows two
containers of the same logical identity to coexist briefly? Or is refuse-only
(with explicit reap via `ContainerRemoveTool`) the correct long-term shape?

## MUST NOT TOUCH
Standard list.

## Bearing
Arc 3 (secure execution) Fact 3 (Lifecycle) — this is the "what does end-of-life
mean when the machine disagrees with itself about intent" question.
