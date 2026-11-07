# Ticket: feat/stop-cascade-detach — stopping a worker says nothing about the workers it spawned

**Filed:** 2026-10-31, read-only pass over `tools/workspace/` at `docs/w4-kb-braindump`.
**Severity:** MEDIUM — orphaned children keep running (and burning) with no owner and no
way to see they were orphaned; the inverse case (deliberate handoff) is indistinguishable
from a leak.
**Status:** open; feature/design gap, verified by read. No runtime repro needed to state it.

## Finding

Workers **can spawn workers**: `WorkerThread._action_spawn(self, workers: list, ws_id)` —
`tools/workspace/worker_thread.py:3873`. The child is spawned from its `workers.json`
definition and registered under the **parent session's** key:
`session_key = self.session_id or _NO_SESSION_KEY` (`worker_thread.py:3896`), with
force-respawn deliberately scoped to *this session's* instances only (`:3896-3910`).

Nothing records *who* spawned whom. The registry's whole identity model is a 3-tuple
`(session_id, worker_name, instance_id)` — `tools/workspace/worker_registry.py:45-57`;
the per-worker event bus uses the same key (`:61`). A tree search for an ownership edge
returns nothing:

```
grep -rn "parent_worker\|parent_id\|child_worker" --include="*.py" tools/workspace/ web_ui/backend/
→ (no hits)
```

Consequently every stop path is a **flat, session-scoped sweep**, none of which walks a
spawn tree:

- `worker_manager.stop_worker` — `tools/workspace/worker_manager.py:347`
- `worker_manager.stop_all` — `:373`
- `worker_manager.stop_all_by_name` — `:417` (iterates the registry filtering on `key[1]`
  name and optional `key[0]` session)
- `WorkerRegistry.shutdown_workers(timeout=5.0, *, session_id)` —
  `worker_registry.py:194-230`; session-scoped by construction, `session_id` is
  keyword-only and required, with an `ALL_SESSIONS` sentinel for the atexit sweep (`:68`).

So there are exactly two available behaviours and neither is a decision anyone made:
stopping a parent leaves its children **running** (accidental detach), and there is no
operation that stops a subtree.

## Blast radius

- Children of a stopped parent are unattributable in the tray: same session, no parent
  link, no "orphaned" flag.
- Cost: a parent stopped because it went wrong leaves children doing the wrong thing at
  full speed.
- The reverse case is just as bad: a *deliberate* handoff ("parent exits, child continues
  the long job") is a supported-looking outcome today with no marker, so an operator
  cannot later distinguish intent from leak.

## What is the actual decision to make

Three semantics, pick one (or one per spawn):

1. **Cascade** — stopping a parent stops descendants. Needs the edge; then `stop` walks it
   depth-first with the existing bounded-join semantics.
2. **Detach** — children survive, but are *marked* orphaned and re-parented to the session,
   visible in the tray as such.
3. **Adopt** — children survive and are re-parented to the caller who stopped the parent.

Whatever is chosen must be recorded at spawn time and be readable from the registry, i.e.
the key or the record grows a parent field. That is the whole ticket: **make the edge
exist and make stop mean something.**

## What is not covered

- No proposal for *where* the edge lives (registry tuple vs a worker record vs the
  session's `workers.json` state) — that is the Worker Lifecycle Design Pass's call.
- `ticket-bug-worker-stop-reset-unscoped-to-session.md` is adjacent but orthogonal: that
  bug is `stop`/`reset` sweeping **by name across sessions**. This ticket is "there is no
  tree to sweep". Fixing that one does not give you this one, and vice versa.
- `ticket-design-worker-job-lifecycle.md` (the job handle/state design) is where the
  vocabulary should be settled so cascade/detach is a *policy on a state machine*, not a
  special case in `stop`.
