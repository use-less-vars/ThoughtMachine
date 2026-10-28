# Brief — `docs/kb-catchup`

**Base:** `dev` at the merge commit of #234. Fresh worker.
**Goal:** Persist the architectural context and backlog discussed over the last 48 hours into the repository. This is a docs-only wave. Do not touch any code outside of markdown files.

## Task 1: Create the Backlog File
Create `.thoughtmachine/working_docs/backlog-core-integration.md` with the following content:
```markdown
# Core Integration Backlog (Post-Core-Sprint)

## 1. `bug/reasoning-block-pop-in` (Fix in flight)
**Symptom:** Reasoning streams on backend (`reasoning_delta`), but UI shows a blank screen, then streams text, then pops the reasoning block in *after* the text finishes.
**Cause:** The terminal `turn` event replaces the in-flight bubble, bringing the committed reasoning with it. The frontend state doesn't paint `reasoning_delta` live.
**Fix:** Update `SessionTab.jsx` to append `reasoning_delta` to the `inFlight` state's `reasoning_content` field, and ensure `MessageBubble.jsx` renders it inside the `<Thinking>` block when `isStreaming={true}`.

## 2. `bug/worker-stop-propagation` (Fix in flight)
**Symptom:** Hitting "Stop" on an agent that has spawned workers stops the main agent but leaves workers running.
**Cause:** `request_stop()` is called on the main agent's controller, but the `halt_check` or `stop_event` is not propagated down to the worker threads/controllers.
**Fix:** When `request_stop()` is called on the main agent, it must iterate over active worker instances in the session and call `request_stop()` on them as well.

## 3. `feat/interruptible-tool-calls` (Parked for Wave 5)
**Symptom:** Blocking tool calls (like `host_bash` or long-running `docker_exec`) cannot be interrupted by Pause or Stop.
**Cause:** Tools run synchronously inside `execute_tool_calls`. The agent's event loop is blocked while the tool runs.
**Fix:** Introduce a "thin layer" (a wrapper or sub-agent/thread architecture) between the agent and the tool execution. This allows the main loop to keep polling the `halt_check` and interrupt/terminate the tool if a pause or stop is requested. (See `docs/design-streaming-canonical-ir.md` §4).

## 4. `bug/macos-path-normalization` (Parked for CI hygiene)
**Symptom:** macOS CI fails on `/tmp` vs `/private/tmp` path comparisons.
**Fix:** Update `tests/` to use `os.path.realpath` or `pathlib.Path.resolve()` when comparing workspace paths.
```

## Task 2: Create the Agent Mental Model Doc
Create `docs/global-kb-workspace-runtime-guidance.md` with the following content:
```markdown
# Workspace Runtime Guidance

To prevent agent confusion regarding the difference between the Workspace container and the Host machine, the system prompt and global KB must enforce the following mental model:

1. **The Workspace Image (`agent-executor:latest`) is your primary environment.** Do all your work here. This is your home. You have all your tools and dependencies installed here.
2. **The Vault Dockerfile is your blueprint.** If you need a new tool (e.g., `manim`, `ffmpeg`), you edit this file. You do not install tools directly on the host.
3. **Host Bash is strictly for bootstrapping.** You only use `host_bash` to run `docker build` to compile the image from the Dockerfile. Once the image is built, you must turn `host_bash` off and go back inside your container. You live in the container, not on the host.
```

## Task 3: Create the Interruptible Tool Design Doc
Create `docs/design-interruptible-tool-calls.md` with the following content:
```markdown
# Design: Interruptible Tool Calls

## Problem
Currently, a blocking tool call (e.g., `host_bash` running a 10-minute build) cannot be interrupted. The agent's `process_query` generator yields to `execute_tool_calls`, which blocks the thread. Pause and Stop signals are ignored until the tool finishes.

## Proposed Architecture (The "Thin Layer")
We need a layer between the agent loop and the tool executor that allows the main thread to monitor the `halt_check` while the tool runs.

1. **Tool Wrapper:** `execute_tool_calls` should run the actual tool in a separate thread (or sub-agent).
2. **Shared State:** The main agent loop continues yielding heartbeats and polling `halt_check`.
3. **Cancellation:** If `halt_check` becomes `True`, the main agent emits a cancellation signal to the tool thread. The tool thread attempts a graceful shutdown (e.g., sending `SIGTERM` to the Docker container).
4. **Return:** The partial output is captured and returned as the tool result.

This design is heavily coupled to Gen 52's `docs/design-streaming-canonical-ir.md` §4 (Cancellation).
```

## Task 4: Create the macOS CI Ticket
Create `.thoughtmachine/working_docs/ticket-bug-macos-path-normalization.md` with the following content:
```markdown
# Ticket: bug/macos-path-normalization
**Severity:** LOW (CI Hygiene)
**Symptom:** The macOS `Platform Matrix` CI job fails on path comparison assertions.
**Root Cause:** On macOS, `/tmp` is a symlink to `/private/tmp`, and `/var/folders/...` resolves to `/private/var/folders/...`. The test assertions compare hardcoded strings (e.g., `/tmp/...`) against runtime paths that have been resolved by `realpath` (e.g., `/private/tmp/...`).
**Fix:** Update the failing tests in `tests/integration/` to use `os.path.realpath()` or `pathlib.Path.resolve()` on both sides of the assertion.
```

## Deliverable
Commit all four files. Return the branch name and commit SHA. Do not push.
