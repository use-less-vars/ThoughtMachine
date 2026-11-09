# PRIORITIES.md — pending C-items

**What this file is.** The active pending-work queue. Ordered. Tracked in git.
**Not** `vision_ledger.md` — that file holds the five standing *commitments* (V1–V5)
the machine must keep. This file holds *work items* (C1–…). Different concept,
different file, different name. Never rename one to the other.

**Rules.**
- One item per bullet. Status line: `open` / `blocked (why)` / `landed (PR/SHA)`.
- Name symbols, not line numbers. When a line number is load-bearing for a bug cite,
  it is a dated snapshot with a `verified at dev@<SHA>` stamp.
- Corrected, disproved, or rescoped items get a `**Correction:**` line in place.
  Do not silently rewrite — the next reader needs to know the premise moved.

---

## Active

- **C1: Stop Signal Propagation (transport).** **Landed** `fea41200`, PR #312.
  Cancel frame crosses the socketpair, `request_id` correlates, ack `accepted`
  within 5s, fake container detaches (not killed). Transport layer proven.
  Semantic layer proven separately by C10.

- **C2: Worker Process Boundary (workspace confinement).** **Blocked.**
  The child runtime boundary accepts six supervisor→runtime kinds:
  `dispatch_query`, `cancel`, `control`, `shutdown`, `config_update`, `ping`.
  None carries a tool/bash/git/read verb — tool execution is one layer down in
  `WorkerThread._run_tool_loop`, needing a live provider + Docker. So confinement
  cannot be tested at the IPC layer; it must be tested at the child's
  `SecurityGate`. **Open question before designing:** does the child's
  `SecurityGate` input include workspace identity? If not, the test is vacuous.
  Read `security/security_gate.py` first. File `tests/test_worker_runtime_cross_workspace.py`
  exists and skips at module level with the reason in the skip text.

- **C3: Host Bash Danger Color.** **Open.** UI. When `host_bash` is enabled on a
  session, the session tab / header must turn red/orange. `web_ui/frontend/src/`.
  Parallel-safe with C10/C11 (different files).

- **C4: Agent Sysprompt Addendum.** **Open.** UI + tooling. The agent must be able
  to write to its own sysprompt addendum.

- **C5: Vision Tool.** **Open.** The agent must be able to view images.

- **C10: "Cancel reaches the runtime" is not "the tool stops" — the promptness
  lives in `ContainerManager`, not the tools.** **Landed** `82589a23`, PR #313.
  C1 proves a cancel reaches a worker blocked in a long tool call and detaches it.
  What makes the *in-flight* call come back is not the tool: `DockerCodeRunner`
  never polls. The promptness comes from `ContainerManager.exec()`
  (`infra/container_manager.py:2498`), which runs `exec_run` on a daemon thread
  and polls `is_interrupted()` every `_WAIT_SLICE_SECONDS = 0.5` (`:2582`),
  returning an `interrupted` payload (`exit_code -3`, `reason "turn_cancelled"`);
  measured 0.501s, 4/4 trials. `sleep` polls separately at its own 0.1s slice.
  The token itself is `tools/cancellation.py:17 threading.local`, bound once per
  turn at `tools/workspace/worker_thread.py:2824`, unbound `:3201`, second site
  `agent/core/agent.py:1385` — **deliberately outside every tool, so never assert
  that a tool class exposes a cancellation API; no tool class does.** Pin the
  behaviour at the seam: a blocked call returns within a poll slice of the cancel
  AND returns the *interrupted* payload, not merely quickly. **Never set an
  assertion margin equal to the production constant it measures** (join(0.5)
  against a 0.5s poll = 3 XPASS / 2 XFAIL in 5 runs); use 3–5× and record why.
  Corollary: repo-wide greps over `bind_turn_cancel` hit `.worktrees/` stale
  checkouts and inflate counts ~3× (57 vs 19) — exclude `.worktrees`.

- **C11: `ContainerManager.exec()` abandons the PID by design; callers own
  cleanup, and three don't.** **Open.** The exec layer's interrupted branch is a
  bare return with an explicit note (`infra/container_manager.py:2611-2628`):
  "Stopped WAITING because the turn was cancelled. The process inside the
  container was NOT killed and may still be running." No kill, no remove, no
  second exec. `container.kill()` + `_drop_container()` at `:2632-2637` are on
  the timeout path only. Recovery relies on the caller's `finally: manager.stop()`
  — `tools/docker_code_runner.py:449-458` has it and it works (stop returns
  `'stopped'`, container `status` → `'exited'`); `tools/git_info_tool.py:1233`
  and `tools/visual_tool.py:486,515` do not. Those paths leave a cancelled
  process in a live container. The design is intentional (comment `:2574-2581`).
  **First sub-task, before the fix:** pin the finally that works —
  hermetic mutation-verified test: token pre-fired → `cm.stopped` contains the id,
  payload `status: interrupted`; removing the finally turns it red. That converts
  an accidental safety into a pinned invariant. Second-order: the payload's own
  note is stale on this path (says "may still be running" after the container
  was destroyed) — cosmetic but misleading.

- **C13: Persistent Container Timeout Self-Healing.** **Open.** Filed by Gen 61
  (dispatch 6) under the operator's Ruling 1. `ResourceContainerManager.exec`
  now has three outcomes: success; timeout (`container.kill()` but deliberately
  NOT remove, then `TimeoutError`); interrupted (returns a payload, container
  untouched). The timeout path leaves a **killed-but-retained** container:
  `_container_id()` resolves it by label, so every later git command on that
  workspace targets a dead container and fails until someone intervenes.
  Deliberately NOT fixed in C11-prime: the green tests
  `tests/test_resource_container_exec_timeout_survival.py` and
  `..._recovery.py` pin `container.removed == []` ("survive the timeout, do not
  delete"), and rebuilding the git sandbox on a timeout risks uncommitted work.
  **Ruling in force (Option A):** status quo plus explicit reporting in the
  payload (`container_killed=False`, `container_removed=False`). Do not
  self-heal via `ensure_container()` without a dedicated design pass.
  **Open questions for that pass:** who owns sandbox recreation; can uncommitted
  work be detected/preserved before teardown; should a timeout instead be
  surfaced to the model as a retryable condition rather than a hard failure.

- **C22: `ToolExecutor` is constructed with `state=None`, and the dispatch gate has no `None` guard.** **Open.**
  Filed by Gen 61 (dispatch 13) under the operator's Ruling 4: file it, do not fix it on
  the instrumentation branch. Found while instrumenting the control plane, not while
  looking for it. Construction sites are `agent/core/agent.py:497` (patched `ToolExecutor`
  wired at `:531`) and `agent/core/agent.py:802` (patched at `:807`) — snapshot
  `verified at dev@1fe89e70`, i.e. a dated stamp, not a stable address.
  **Consequence.** Any tool dispatch landing inside either window reaches
  `if not self.state.is_tool_allowed(tool_name)` with `self.state is None` and raises
  `AttributeError: 'NoneType' object has no attribute 'is_tool_allowed'`. That is a crash,
  not a clean refusal — and **not** a silent allow.
  **Why it matters past the crash.** The gate cannot tell "no state yet" from "state says
  allowed", so any future `None`-tolerant variant of that check is fail-open by
  construction — the same class as the `getattr(..., default)` fail-open recorded in
  `docs/CONTROL_PLANE.md` 6.2. Crash is the safe failure; a fix must not trade it for a
  permissive one.
  **Fix direction: deliberately NOT decided.** The dispatch that owns it must choose between
  passing a real `AgentState` at construction (closing the window) and failing closed on
  `None` with an explicit refusal (making the window loud). Deciding here would prejudge
  that design pass.
  **Numbering.** C22 was assigned by operator ruling; C15–C21 are unused.
  **Correction (dispatch 14):** the `state=None` hole does not reproduce on the worker path —
  `Agent.__init__` re-binds `self.tool_executor.state = self.state` (agent.py:531), and the
  worker-path test now fails loudly if `executor.state` ever becomes `None`.

- **C23: Control plane — unprobed paths (worker lazy build, resume-side clear).** **Open.**
  Test-coverage gap, not a bug claim: no failing behaviour is known on any of these paths;
  they simply have no test that reads the instrumentation.
  **Proven identical, therefore ruled out.** Direct path dispatch 13 and worker path
  dispatch 14: loop, executor and dispatch gate all hold ONE `AgentState` — the
  `[CONTROL_PLANE_GATE]` state id equals the `[CONTROL_PLANE_STATE]` mutation id, and the
  worker test asserts `worker._agent.tool_executor.state is worker._agent.state`.
  Identity propagation is not a live defect on either path; what is missing is coverage.
  **Unprobed surfaces (four).** (1) the worker's TRUE lazy build — `Agent(...)` constructed
  inside `run()` when `_agent is None`; (2) the resume-side clear — `Controller.resume()` →
  `agent.leave_reading_mode('pause')`; (3) the restart re-point — `Agent.restart()`
  rebuilding `ToolExecutor` from the new config; (4) the controller's idle pause branch —
  `_agent_enter_reading_mode('request_pause[idle]')`.
  **Why the lazy build needs its own test.**
  `tests/test_control_plane_instrumentation.py::test_worker_pause_executor_observes_the_same_state_object`
  assigns `worker._agent` directly, so it probes the post-build wiring only. A test of the
  real lazy build needs a started worker thread, a stubbed provider, and a
  poll-until-built (not a fixed sleep) before the states' ids can be read.
  **Grep contract for whoever writes those tests:** `[CONTROL_PLANE_STATE]`,
  `[CONTROL_PLANE_PAUSE]`, `[CONTROL_PLANE_GATE]`, `[CONTROL_PLANE_RESTART]`,
  `[CONTROL_PLANE_CONTROLLER]`, `[CONTROL_PLANE_WORKER_CLEAR]`.

---

## Archived (pre-Gen 42 brain dump — do not read as current work)

- **W11 (Resource Composition):** Bins (granular permissions), Dynamic Imports,
  Host vs. Container execution flags.
- **W12 (Worker Instances):** Physical UI for workers, Sleep/Join async tools,
  editable Modes.
- **W13 (Credentials & MCP):** Scoped vault injection, MCP connections,
  Network allowlists.
- **W14 (Tima Rebrand):** The final naming migration.
- **UI Polish:** Session tab syncing, breadcrumb fix, tab persistence (F5 bug),
  resource panel scrollbar.

- **C12: `cancellation.interrupt()` sticky-fallback trap.** **Open.** Calling `interrupt()` with no bound token arms a per-thread fallback event. `unbind()` does not clear it, so every subsequent `is_interrupted()` on that thread reads True permanently. This poisons the thread for the rest of the process (found during C11). Fix: clear the fallback on `unbind()`.

- **C14: `visual_tool.py` missing cancellation reporting.** **Open.** An interrupted render drops the `reason` and returns `"visual: the renderer exited with code None."` to the model. This looks like a renderer crash, not an operator cancel, causing the model to hallucinate a failure. Teardown runs correctly (pinned by test). Fix: detect `status == "interrupted"` and return a clear cancellation message. (Discovered during C11').
