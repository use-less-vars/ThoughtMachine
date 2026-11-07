# Ticket: bug/worker-hard-timeout — a wedged worker is never killed, and nothing reaps it

**Filed:** 2026-10-31, read-only pass over `tools/workspace/` at `docs/w4-kb-braindump`.
**Severity:** MEDIUM-HIGH — a worker that stops polling `stop()` has no terminating path at all; the slot it holds is never freed by the system.
**Status:** open; verified by read, **not** reproduced at runtime (see "Unverified").

## Finding

The worker timeout model is **soft only**. The module states the contract in its own
words: a worker soft-times-out at `timeout_seconds`, replies with a timeout envelope, and
the caller must wait `timeout_seconds + QUERY_WAIT_GRACE_SECONDS` so the worker's *own*
envelope wins over a destructive preemption — `tools/workspace/worker_timeout.py:1-14`.

The stop path is explicitly cooperative and explicitly cannot kill:

- `wait_for_worker_exit()` — `tools/workspace/worker_timeout.py:102-139`. Bounded join,
  then on budget expiry it logs *"did not stop within %ds budget after query timeout;
  returning envelope with thread still running (daemon thread)"* (`:130-136`) and returns
  `False`. No kill, no registry action.
- `_hard_deadline_fallback()` — `tools/workspace/worker_timeout.py:141-212`. This is the
  "hard timeout" that exists: it calls the thread's own `stop()`, then a **bounded**
  `join(join_bound)` (`:172-186`), and when the thread is still alive it returns
  `status='timeout'` with the content *"reached its hard deadline and did not exit within
  the bounded join; it is left as a daemon thread"* (`:203-206`). The docstring is
  unambiguous: *"calls the thread's own ``stop()`` (never ``Thread.kill``)"* (`:151`).
- **`_hard_deadline_fallback` has no caller.** `grep -rn "_hard_deadline_fallback"
  --include="*.py" .` returns exactly one hit — the definition at `worker_timeout.py:141`.
  The one hard-timeout primitive in the tree is dead code.

Registry entries are only removed by the thread itself or by a manager pass:
`tools/workspace/worker_thread.py:2952` (self-exit), `tools/workspace/worker_manager.py:408`
(`cleanup_dead`, which skips **live** threads), `:434` (`stop_all_by_name`), `:637`. A
daemon thread that is alive-but-wedged is alive by `_is_alive`, so `cleanup_dead` will
never take it either.

Registry identity is `(session_id, worker_name, instance_id)` —
`tools/workspace/worker_registry.py:45-57` — so a wedged entry keeps counting against the
session's spawn cap (`worker_manager.can_spawn`, `:341`).

## Blast radius

- The worker slot leaks. The only visible escape is a force-spawn, which also just asks
  the same thread to `stop()` (`worker_thread.py:3898-3910`), so a genuinely wedged thread
  is not replaced — it is duplicated.
- The UI keeps showing a worker in a live state that will never answer; there is no state
  that means "alive but unreachable".
- Token/container cost continues to accrue with no owner: nothing in the machine can stop
  it, so the operator's only tool is a process restart.

## What is not covered

- Whether `WorkerThread.stop()` actually sets a flag the in-flight call polls — I did not
  read `stop()`, so "wedged" here means "a call that does not return", not "a call that
  ignores the flag".
- Whether any real tool can block past the join budget (a reproducer is the next step;
  `tools/host_bash_tool.py:290` runs `subprocess.run(..., timeout=timeout)` so it is
  bounded, but container-exec and LLM paths were not inspected).
- Killing a Python thread is not possible without `ctypes`/process isolation; this ticket
  does **not** claim a kill is implementable in-thread. The realistic options are (i) run
  workers off-process, (ii) wire `_hard_deadline_fallback` and add a *registry* verdict
  for "unreachable", (iii) both.

## Dedupe

- `ticket-hygiene-worker-silent-timeout.md` — adjacent, different: that one is the soft
  timeout's return being *silent* (naming the reason). This one is the absence of a
  terminating path and the un-reaped registry entry.
- `ticket-design-worker-job-lifecycle.md` — the design pass that should own the state
  model (`timed out` vs `unreachable`). This ticket is an input to it, not a competing plan.
- `runtime note` the v3_roadmap carries this as a one-liner: *"Worker timeout / output-loss
  — parking lot. Owner to assign."*
