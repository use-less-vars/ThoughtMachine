# Core Integration Backlog (Post-Core-Sprint)

## 1. `bug/reasoning-block-pop-in` (FIXED — branch unmerged, needs push + PR)
**Symptom:** Reasoning streams on the backend, but the UI showed a blank screen, then streamed text,
then popped the reasoning block in *after* the text finished; the panel also stopped auto-scrolling
while the model was thinking.
**Corrected cause (2026-10-29 — the previous diagnosis in this file was WRONG):** the claim that
"`SessionTab.jsx` swallows the live deltas and only replaces them on commit" is **false at `dev`
@ `adfb5ef6`**: `SessionTab.jsx:623` handles `case 'reasoning_delta'` and appends to the in-flight
bubble's `reasoning_content` with per-frame coalescing (landed in **#231**, `ab94020de`). The two real
defects were:
1. `ChatPanel.jsx` keyed its scroll-follow signature on `messages[last].content` alone. A thinking-phase
   delta grows only `reasoning_content`, so the signature looked unchanged, the panel took the
   restore-`scrollTop` branch, and the view froze mid-thinking.
2. `MessageBubble.jsx` hard-coded `<details className="reasoning-block" open>` while `ChatPanel` keys
   bubbles by **array index**, so a load-more prepend or a `conversation_changed` replace re-used the
   instance for a *different* message and the block re-opened (the "pop-in").
**Fix:** **already committed** on `fix/reasoning-ui-streaming` @ `7c5f1fae` ("Fix reasoning-block pop-in
and autoscroll stall during the thinking phase") — two-field scroll signature, and per-content
collapse state that survives append-only streaming but re-opens for a different message.
**OPEN ACTION:** that branch is **local only — it has no `remotes/origin/` counterpart**. The fix is
stranded until it is pushed and a PR opened. Do not re-implement it; fetch the branch.

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
