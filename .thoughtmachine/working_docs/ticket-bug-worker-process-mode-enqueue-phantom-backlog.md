# Ticket — process-mode worker can accept a query into a queue nothing consumes (phantom backlog)

**Status:** OPEN, unfixed. Reachability **NOT** established (see "Reachability" below) — read this
whole ticket before touching code.
**Suspected class:** silent hang + phantom queue depth; possibly dead code today.
**Area:** `tools/workspace/worker_thread.py` (`enqueue_query` and its two call sites), surfaced to
the operator by `web_ui/backend/workspace_routes.py` → `GET /api/workspace/{ws_id}/workers/active`.

## Symptom

`enqueue_query` appends to the FIFO bookkeeping and to `_input_queue` with **no runtime-mode check**:

```python
# tools/workspace/worker_thread.py:2651
def enqueue_query(self, job_id: str, query: str) -> None:
    ...
    with self._queued_lock:
        self._queued_job_ids.append(job_id)          # :2662-2663
    self._input_queue.put((job_id, query, None))     # :2664
```

For a worker stamped `MODE_PROCESS` that is the wrong transport. `runtime_dispatch_handle`'s
docstring (`:1562-1566`) states why:

> **process mode** — the body is a child interpreter. The supervisor's thread is *deliberately never
> started* (`WorkerThread.start` returns early rather than run a second body beside the child), so its
> queues have no consumer: a query put there is accepted, never runs, and the caller waits out its full
> budget for an answer that cannot come.

So the item is accepted, nothing ever dequeues it, nothing raises, and the queue never drains.

The operator sees the phantom but not the reason: `queued_query_count` / `queued_query_ids` in the
workers/active response are read straight off `pending_query_jobs()` (`:2666-2668`,
`workspace_routes.py:668` `_worker_queued_queries`), so a process-mode worker can show a permanently
non-zero, never-draining queue depth with no error state anywhere in the UI.

## The two call sites

Both use the same duck-typed shape (`enqueue_query` if present, else poke `_input_queue` directly) and
both swallow everything into a submit error:

1. `action='query'`, inside the `if thread.status == "detached":` branch — `:7820-7824`
   (`enqueue = getattr(thread, "enqueue_query", None)` / `enqueue(job_id, delivered_query)` /
   fallback `thread._input_queue.put((job_id, delivered_query, None)) :7824`);
   any exception becomes `{"error": f"Failed to enqueue query: {exc}"}`.
2. `action='submit_query'` — `:8121-8125` (same three lines, same `except Exception` → same error shape).

## Why "just raise inside `enqueue_query`" is not automatically the right fix

There is already exactly **one** transport decision point in this module, and it already refuses the
impossible case:

- `WorkerQueryTransportMissing` (class `:1508`) — "a worker is stamped for a non-thread runtime but has
  no usable handle… Raising names the bug; the queue fallback the callers used to take would have said
  'did not respond' after blocking for the whole query budget."
- `declared_runtime_mode` `:1519-1549` → `runtime_dispatch_handle` `:1552-1592` →
  `deliver_query_to_worker` `:1595-1623`, which **already raises** `WorkerQueryTransportMissing` at
  `:1616` for a stamped non-thread worker whose handle is missing.

Adding a raise inside `enqueue_query` would therefore create a *second, competing policy* for the same
question ("may this worker receive a query this way?") with its own exception type, its own message and
its own idea of the mode vocabulary. The likely fix is (a) — route these call sites through the existing
accessor (`runtime_dispatch_handle` / `deliver_query_to_worker`) so one predicate governs both
transports — rather than (b) bolting a one-off check onto `enqueue_query`. If (b) is chosen anyway,
reuse `declared_runtime_mode` and the same exception; do not hand-roll a mode test.

Two constraints for whichever route is taken:

- **Do not regress thread mode or unstamped duck-typed doubles.** `declared_runtime_mode`'s docstring
  spells out why the test is *membership in `RUNTIME_MODES`* and not `is None`: `MagicMock` answers
  every attribute access, so a naive check reads a mock as "some non-thread mode"; those were two real
  regressions (`WorkerQueryTransportMissing` naming a `<MagicMock …>` mode, `PicklingError`).
- **A raise here is user-visible**, because both callers map it to `{"error": "Failed to enqueue query: …"}`.
  That is the desired outcome (loud, not a hang) — but it means the blast radius is every submit path,
  so the fix needs the test at the call-site level, not only a unit test of `enqueue_query`.

## Reachability: UNPROVEN — do not call this a live bug yet

I did **not** demonstrate that a process-mode worker ever reaches either call site. Two reasons it may
be unreachable / dead today:

- call site 1 sits behind `thread.status == "detached"`; it is unknown whether a process-mode worker can
  ever carry that status (its supervisor thread is never started, so it may never take the code paths
  that set it);
- call site 2 (`submit_query`) is only reached if process-mode submissions go through this handler at
  all, rather than through the runtime handle.

The defect in the code is proven by reading. Its **reachability is not**. Do not infer severity from the
symptom section above.

## Evidence that would prove reachability (gather before fixing)

1. Instrument `declared_runtime_mode` / `runtime_dispatch_handle` to log `(mode, worker_name)` at every
   call, then check whether a `MODE_PROCESS` worker ever shows up at either call site.
2. Cheapest: instrument `enqueue_query` itself — record `declared_runtime_mode(self)` on every enqueue and
   log loudly if it is ever non-thread. Run the worker test suite plus any live process-mode smoke.
3. A repro: build a worker with `_runtime_mode = MODE_PROCESS` (no thread started), drive `submit_query`
   against it, and observe either an immediate error or a `pending_query_jobs()` entry that nothing ever
   consumes.
4. Static: grep every writer of `status = "detached"` and check whether a process-mode worker can be the
   subject of one.

## Fix requirements

- **Red test first**, at the level that proves the chosen call site refuses / reports instead of queueing.
- Exactly one predicate for "can this worker receive a query over the queue"; reuse `declared_runtime_mode`.
- Re-run the thread-mode and duck-typed-double tests (the `MagicMock` cases named above) as the
  regression gate.

## This ticket does not claim

- It does not claim the UI's 3s poll, or the backend "lock stall" a failed refresh reports, has anything
  to do with this.
- It does not claim any root cause for a hung query; only that a queued item on a process-mode worker has
  no consumer.
- It does not claim reachability, severity, or affected users.
