# Core Integration Backlog (Post-Core-Sprint)

## 1. `bug/reasoning-block-pop-in` (Fix in flight)
**Symptom:** Reasoning streams on backend (`reasoning_delta`), but UI shows a blank screen, then streams text, then pops the reasoning block in *after* the text finishes.
**Cause:** The terminal `turn` event replaces the in-flight bubble, bringing the committed reasoning with it. The frontend state doesn't paint `reasoning_delta` live.
**Fix:** Update `SessionTab.jsx` to append `reasoning_delta` to the `inFlight` state's `reasoning_content` field, and ensure `MessageBubble.jsx` renders it inside the `<Thinking>` block when `isStreaming={true}`.
**Verified (dev @ `2ac3148c`):** #231 is merged, so the backend side is done — `reasoning_delta`
does stream. The remaining gap is purely React state: `SessionTab.jsx` swallows the live deltas
and only replaces them on commit. Do not re-derive a "backend does not stream" premise.

## 2. `bug/worker-stop-propagation` (Fix in flight)
**Symptom:** Hitting "Stop" on an agent that has spawned workers stops the main agent but leaves workers running.
**Cause:** `request_stop()` is called on the main agent's controller, but the `halt_check` or `stop_event` is not propagated down to the worker threads/controllers.
**Fix:** When `request_stop()` is called on the main agent, it must iterate over active worker instances in the session and call `request_stop()` on them as well.

## 3. `feat/interruptible-tool-calls` (Parked for Wave 5)
**Symptom:** Blocking tool calls (like `host_bash` or long-running `docker_exec`) cannot be interrupted by Pause or Stop.
**Cause:** Tools run synchronously inside `execute_tool_calls`. The agent's event loop is blocked while the tool runs.
**Fix:** Introduce a "thin layer" (a wrapper or sub-agent/thread architecture) between the agent and the tool execution. This allows the main loop to keep polling the `halt_check` and interrupt/terminate the tool if a pause or stop is requested. (See `docs/design-streaming-canonical-ir.md` §5).

## 4. `bug/macos-path-normalization` (Parked for CI hygiene)
**Symptom:** macOS CI fails on `/tmp` vs `/private/tmp` path comparisons.
**Fix:** Update `tests/` to use `os.path.realpath` or `pathlib.Path.resolve()` when comparing workspace paths.
**Tracked by:** `.thoughtmachine/working_docs/ticket-bug-macos-tmp-path-tests-assume-symlink-free.md`
— operator ruling 2026-10-28: do not open a duplicate. The fix must resolve *both* sides
(`Path(x).resolve() == Path(y).resolve()`); swapping `resolve()` for `realpath()` changes nothing,
they are the same POSIX machinery (KB lesson, 2026-10-22).
