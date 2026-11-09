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
