# Design Pass — Concurrency and Affinity

*Written 2026-10-28, after the `_STREAM_LOOP` catastrophe and the Stop Button merge.*

## 1. The Claim
The old core had one thread-affine resource: the controller thread, blocking on an HTTP call. The new core has five: the OS thread, the asyncio event loop, the `Agent`, the provider, and the `AsyncOpenAI` client.

When these five disagree on which thread they belong to, the machine crashes (shared global loop) or silently breaks (per-call loop). The entire concurrency model rests on one invariant: **Every loop-bound resource must share the lifetime of the thread that owns it.**

## 2. The Topology (Measured)
The code enforces this topology:
1. **The Controller Thread** (`controller/__init__.py:549 _run`): Lives for the session's keep-alive window.
2. **The Agent** (`:594`): Created inside `_run`, dies with the thread.
3. **The Provider** (`llm_client.py:111`): Created per Agent, dies with it.
4. **The Client** (`openai_compatible.py:452-460`): `AsyncOpenAI` is cached on the provider on first use, binds to the loop that first uses it.
5. **The Loop** (`agent/core/agent.py:104 _iter_stream_events`): Cached in `_STREAM_LOCAL = threading.local()`.

**The Rule:** Thread death destroys the loop, which destroys the provider, which destroys the client. No sharing. No cross-thread access.

## 3. The Seams (Known Risks)
We know of five concurrency seams that have not been fully tested or closed.

### A. The Thread-Death Restart Leak (High Priority)
**Where:** `controller/__init__.py:224-244` (`process_query: thread dead, cleaning up and restarting`).
**What happens:** When a thread dies and restarts, a new thread is spun up, which creates a new loop and a new `AsyncOpenAI` client. The *old* thread's loop and never-closed client are orphaned.
**Fix:** Need a `finally` block in `_run` that explicitly calls `loop.close()` before the thread exits.

### B. The Worker-Spawn Path (High Priority)
**Where:** `tools/workspace/worker_thread.py`.
**What happens:** We believe the worker sub-agent uses the thread-local loop, but we haven't proven it. The catastrophe ate a worker mid-wave.
**Test:** Spawn a worker, force it to stream text, and assert that `_STREAM_LOCAL` is unique to the worker's thread.

### C. The EventBus Subscriber Locking (Medium)
**Where:** `agent/events.py:475-489`.
**What happens:** `EventBus.publish()` iterates over subscribers synchronously. If two controller threads publish events at the exact same millisecond, are the lists locked? Unread.
**Test:** Spawn 5 threads, publish 100 events each, assert no `RuntimeError: dictionary changed size during iteration`.

### D. Late Delta Without a Turn Guard (Medium)
**Where:** `web_ui/frontend/src/components/SessionTab.jsx`.
**What happens:** `assistant_delta` events are ordered by the WS channel. If the channel drops and reconnects, a late delta could open a second in-flight bubble.
**Test:** Simulate a WS reconnect mid-stream, fire a late delta, assert no duplicate UI bubbles.

### E. Bridge vs. Controller Dual Consumer (Low/Unknown)
**Where:** `bridge.py:3108` vs. `controller/__init__.py:549`.
**What happens:** Both consume `process_query`. Which is live? If a bridge session and a controller session run simultaneously, do they share state?

## 4. The Concurrency Test Wall
We will build `tests/test_core_concurrency.py` to close these seams. It must be a *real* test, not mocks.
- Spawn two real `WebAgentBridge` instances.
- Use a fake provider that streams slowly (`await asyncio.sleep(0.05)`).
- Fire `process_query` on both simultaneously.
- Assert no loops leak, no streams strand, and history is valid.
- Run `pytest` with `-n 2` (xdist) to force parallel execution.
