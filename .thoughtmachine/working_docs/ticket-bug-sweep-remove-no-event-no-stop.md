# Ticket: bug/sweep-remove-no-event-no-stop

**Filed:** Gen 43, 2026-10-14 at `d378c206`
**Source:** Wave 9 read-first (spare agent).
**Severity:** Medium — sibling of 9a, same family.
**Status:** filed, not dispatched.

## Defect

`sweep_exited_workspace_containers()` uses `remove(force=True)` — more
destructive than the explicit `ContainerStopTool`, which uses `stop()`.
The automatic path is *more* destructive than the deliberate one.

And: the sweep emits no lifecycle event — only an audit line. Creation
emits one. The asymmetry is why this class of bug keeps being
re-discovered.

## Fix shape (not written)

- `remove(force=True)` → `stop()` where recoverable; `remove()` only if
  the class permits.
- Emit a lifecycle event on teardown, with reason.

## Note

The sweep is NOT the turn-start reap. It hard-skips non-exited
containers. This ticket is a sibling, filed for the same family.

