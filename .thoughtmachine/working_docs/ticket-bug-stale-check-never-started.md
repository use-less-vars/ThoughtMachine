# Ticket: bug/stale-check-never-started — the stale-worker detector exists but nothing starts it

**Filed:** 2026-11-01, implementation pass over `tools/workspace/` (phase A: announce-only watchdog).
**Severity:** MEDIUM — a stale/hung worker is never noticed by the machine; detection, not termination, is the missing piece.
**Status:** open for phase B; phase A (announce-only watchdog) is implemented and tested.

## Finding

The tree contains a stale-worker detector and a stale-worker *checker thread*, and both are
reachable only by hand.

- `WorkerManager.start_stale_check()` — `tools/workspace/worker_manager.py:809`. It builds
  and starts a `PeriodicStaleCheck` (`:135`) which, on its interval, calls
  `WorkerLifecycleObserver.check_stale_transitions()` (`tools/workspace/worker_lifecycle.py:199`)
  and hands each stale transition to a callback that **interrupts** the worker.
- `grep -rn "start_stale_check(" --include="*.py" .` returned exactly **one** hit before this
  pass — the `def` itself at `worker_manager.py:809`. No production caller ever ran it:
  not the server lifespan, not `get_manager()`, not worker spawn.
- The observer's own subscription (`_get_worker_lifecycle_observer()`,
  `tools/workspace/worker_thread.py:324`) routes stale transitions to `_on_worker_stale`
  (`:262`), which terminates tracked executions
  (`_terminate_tracked_executions`, `:1912`). So the *machinery* terminates; the *trigger*
  is what never runs.

The practical consequence: `HEARTBEAT_INTERVAL_S = 30` / `HEARTBEAT_STALE_AFTER_S = 600`
(`agent/config/defaults.py`) are declared thresholds that nothing polls against in
production. `WORKER_HUNG_GRACE_S = 0` (`agent/config/defaults.py:84`) is likewise read only
by `WorkerLifecycleConfig` (`worker_lifecycle.py:31,51`), which is only constructed by the
checker that never starts.

## Blast radius

- A worker that stops heart-beating is invisible. It keeps its registry entry
  (`(session_id, worker_name, instance_id)`) and therefore keeps counting against the
  session spawn cap; the operator's only signal is a query that eventually soft-times-out.
- Nothing distinguishes "busy and slow" from "alive but unreachable" — the exact state gap
  already recorded in `ticket-bug-worker-hard-timeout.md`.
- Any future feature that assumes "stale workers eventually get cleaned up" is building on
  a trigger that does not exist.

## What is not covered

- **Termination is deliberately not wired in phase A.** The new
  `tools/workspace/worker_stale_watchdog.py` only *announces* (`logger.warning` + an
  optional callback); a source-level test asserts no `kill`/`cancel`/`interrupt`/
  `terminate_*` name is reachable from the module. The observer pass is announce-only
  in *both* directions: its `stale_callback` is `None`, and the observer is built on a
  private `EventBus`, so the `WORKER_TIMEOUT` event it publishes
  (`worker_lifecycle.py:255`) is consumed by nobody. On the shared `global_event_bus`
  that same publication reached `worker_thread`'s observer, whose callback called
  `terminate_executions()` / `terminate_all()` -- an announcement turned into an action
  by a subscriber the watchdog does not own. Phase B is the pass that decides the action
  -- ping-ack first (see below), then cancel -> kill -- and turns on
  `WORKER_STALE_PROBE_TIMEOUT_S` / `WORKER_STALE_CANCEL_TIMEOUT_S` /
  `WORKER_STALE_KILL_TIMEOUT_S` (`agent/config/defaults.py`, all `15`, currently unread).
- **There is no ping-ack.** `worker_runtime_protocol.py:392` defines a `Ping` frame kind,
  registered in `FRAME_TYPES` (`:581`) and `DIRECTION_TO_RUNTIME` (`:599`), but there is no
  `Pong`/`PingAck` kind, no inbound handler, and no `ProcessWorkerRuntime.ping()`. The
  control surface is only `CONTROL_ACTIONS = ("stop","pause","resume","kill")` (`:130`),
  validated at `:375-377`. Phase B cannot probe without adding the reply frame.
- **The legacy checker still interrupts.** `PeriodicStaleCheck` is left byte-for-byte in
  place (now idempotent + reachable via `ensure_stale_monitor()` at
  `worker_manager.py:838`), but the watchdog never uses it: its observer is built with
  `stale_callback=None`, deliberately not `_get_worker_lifecycle_observer()`, so the
  announce path cannot terminate by construction.
- `WorkerManager.__init__` is intentionally untouched — starting a thread there would leak
  a watchdog into every unit test that builds a fake-registry manager.

## Dedupe

- `ticket-bug-worker-hard-timeout.md` — adjacent, different: that one is the missing
  *terminating path* for a wedged thread. This one is the missing *start* of the detector
  that would find it. Both are inputs to phase B.
- `ticket-hygiene-worker-silent-timeout.md` — the soft timeout's *silent* return. Unrelated
  to detection.
- `ticket-design-worker-job-lifecycle.md` — owns the state model (`stale` vs `unreachable`);
  phase B should land under it rather than invent a second vocabulary.
