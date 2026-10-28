# Ticket: hygiene/worker-stream-loop-teardown-leak

**Filed:** Gen 54, 2026-10-28 at `12bd6408`
**Source:** Seam B ("The Worker-Spawn Path") in `docs/design-concurrency-and-affinity.md` §3.B, re-scoped after a read-only trace
**Category:** hygiene
**Severity:** low — leak on thread death; no cross-thread corruption observed
**Status:** filed, not dispatched.

## Summary

Seam B's ownership half is closed: a worker is an in-process daemon thread that lazily builds its own `Agent`, and therefore its own thread-local asyncio loop; it cannot share the controller's loop or its bound httpx pool. What remains open is teardown. When a worker thread dies, or is reset, nothing closes its event loop, its provider, or its `AsyncOpenAI`/httpx client.

## Evidence

- `class WorkerThread(threading.Thread)` — `tools/workspace/worker_thread.py:901`; `super().__init__(daemon=True, name=f"worker-{instance_label}")` — `:939`. In-process, not a separate process.
- Own `Agent`, built on first query on the worker's own thread: lazy import `:2522`, `Agent(config=..., session=..., event_bus=...)` `:2534-2538`. Slot declared `self._agent: Optional[Any] = None` at `:1105`.
- `grep -c asyncio tools/workspace/worker_thread.py` → 0. The loop comes from `agent/core/agent.py:151` (`asyncio.new_event_loop()`), guarded at `:149-150` by `if loop is None or loop.is_closed()`, stored in the thread-local `_STREAM_LOCAL` (defined at `agent/core/agent.py:106`).
- The LLM call is never delegated off the worker thread: `run()` `:2204` → `_run_tool_loop(query)` `:2597` → `self._agent.process_query(query)` `:1875` → `_iter_stream_events(...)` `agent/core/agent.py:1351`. No `ThreadPoolExecutor`, no nested thread, no subprocess for the stream.
- `_close_stream_resources` is referenced only at its definition (`agent/core/agent.py:174`) and at the controller teardown (`agent/controller/__init__.py:784-786`). Zero references anywhere under `tools/workspace/`.
- `WorkerThread.run()`'s `finally` (`worker_thread.py:2925-2965`) does timeout unregister, `unregister_worker_event_bus(...)`, `_save_context()` and `_cleanup_worker_containers()` — no loop, provider or client close.
- `reset()` (`:1584`) sets `self._agent = None` and nothing else. `Agent` has no `close`/`shutdown`/`aclose`; the only closer that exists is `LLMClient.close()` (`agent/core/llm_client.py:344`), which the worker path never calls.

## Why it is a defect

The loop is deliberately stable per thread — `loop.close()` was removed from the per-call path precisely so the loop survives across calls — which makes thread death the only correct teardown point, and thread death is exactly the point nothing hooks. Every worker spawn/teardown cycle therefore strands one asyncio loop plus one provider's httpx pool until GC. `tests/test_core_concurrency.py` covers controller-style threads only (`grep -c WorkerThread` → 0), so this path is unverified rather than verified-safe.

## Suggested fix

At the top of `WorkerThread.run()`'s `finally` (`worker_thread.py:2925`), on the worker's own thread, mirror the controller:

```python
try:
    _close_stream_resources(getattr(getattr(self._agent, 'llm_client', None), 'provider', None))
except Exception:
    pass  # teardown must never raise out of `finally`
```

The same call belongs in `reset()` (`:1584`) before `self._agent` is nulled. It must run on the worker's own thread: `_STREAM_LOCAL` is thread-local, so closing from anywhere else closes the wrong loop, or creates one.

## Pinned by

Nothing yet. Proposed: a worker-specific case in `tests/test_core_concurrency.py` — spawn two real `WorkerThread`s, drive one query on the first, let it die, assert its loop is closed and its provider's `aclose` ran, and assert the second worker's loop is untouched.

## Out of scope

- No re-architecture of `_STREAM_LOCAL`; the thread-local is correct as-is.
- No change to the success path or to streaming behaviour; teardown only.
- Do not fold Seam A (the thread-death restart leak) into this; it was closed by `85eb43ed` / PR #233.

## Filed

Filed during the Gen 54 read-only Seam B trace, alongside `fix/tool-refusal-structure`. Not dispatched: the verdict is closed-as-ownership, open-as-leak, so this is hygiene work, not a blocker.
