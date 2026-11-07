# Brief 2: The Thread-Death Leak

**Goal:** Fix Seam A from `docs/design-concurrency-and-affinity.md`.
**File:** `agent/controller/__init__.py`
**Change:** Look at the restart path (`:224-244`). Wrap the thread loop in a `try/finally`. In the `finally`, if the agent has a loop, close it.
**Test:** A unit test that forces a thread restart, then asserts the old loop is closed (mock `loop.close()` and assert it was called).
