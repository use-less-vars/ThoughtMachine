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

- **C26: Resume notice is symmetric with the pause notice.** **Landed** `3f839766` (branch `fix/gen61-control-plane-ui-cases`; not yet in `dev` at T0). The pause notice was one-way, so a resumed agent kept obeying the pause instruction from earlier in its own history. `Agent.leave_reading_mode()` is the only `clear_restriction()` caller outside `state.py`, so every resume path funnels through it: the flag is armed there and `_apply_resume_notice` materialises the `Message` on the turn thread and writes it straight to history rather than into `_pending_warnings`, because every flush site for that buffer runs after the LLM call and a buffered notice would miss the first post-resume call — which was the whole bug. Guarded on `prior_reason == 'pause'`, consumed once. 11 tests.

- **C27: `Controller.resume()` never left `ExecutionState.PAUSED`.** **Landed** `e5260677` (branch above; not yet in `dev` at T0). Resume released the pause flags but emitted no `execution_state_change`, so no `state_changed` reached the UI and the banner stayed grey until F5. Resume now settles to `RUNNING` (turn in flight) or `READY` via `_set_execution_state`. A pinned test asserting "resume emits NO events" was **superseded** under operator ruling: it died at `AttributeError` because the fixture stubbed `SimpleNamespace(state=…)` without the method, so the claim it made had never been exercised. It was replaced by resume-from-READY = no events plus a parametrised resume-from-PAUSED/PAUSING = exactly one `state_changed`, against a real `AgentState` parked through the real `set_execution_state()`.

- **C28: One tool call rendered as two bubbles.** **Landed** `dc7b0899` (backend) + `d8d65dd6` (frontend), branch above; not yet in `dev` at T0. One stored call yields two rows — the assistant `tool_calls` array and the `{"role": "tool", "tool_call_id": …}` result — and the chat had no pairing step. The normalizer's branches are mutually exclusive (`continue`) and `agent/` never persists `role: "tool_call"` at all (it is synthesised at emit), so the row count was never going to be one. Both emit paths (`bridge.py` and `session_manager.py`) now carry `tool_call_id`, with position-derived `legacy:{msg}:{tc}:{name}` / `legacy:result:{msg}` fallbacks for pre-existing history (an id-less result is never paired). The frontend folds on `pairId` inside `useStore` across replace/append/prepend, including a prepend that delivers the call after the result. Deliberately not `mergeToolMessages`, which stamps a panel-only `_seq` and would render the args JSON as the outcome.

- **C29: Worker pause never reached container exec.** **Landed** `9082e2cd` (in `dev`). `bind_pause_event` was bound only for the main agent, so a paused worker's in-flight container command ran to completion. It is now bound at the worker turn boundary via `_park_event` and polled in both container managers, sharing `_interrupted_payload(reason="paused")`.

- **C31: Pre-commit gate covers ~630 of ~6482 tests.** **Open.** File only. The pre-commit hook runs a subset; ~90% of the suite is not gated at commit. Quantify the gap and decide whether that is intentional — do not change the hook here.

- **C32: `git_read` selectors silently ignored.** **Open.** File only. `git_read log`, `git_read rev_parse` and `git_read show_file` ignore `commit`/`ref` and return HEAD (except `rev_parse`, which honours `ref` but not `commit`). Produces plausible wrong history — an agent can report SHAs that never contained the work.

- **C33: `gen_handoff.py` open-item parser mis-files and re-slices the queue.** **Open** — all three defects below are fixed in branch `docs/gen62-rules-of-engagement`; this entry stays open only until the merge SHA is known. Three defects in one loop: (a) the filter tested the *bold title* capture (`body`) for the done-marker word, but the regex `^- \*\*(C\d+):\s+(.*?)\*\*` stops at the closing `**`, so the marker sits after the capture and the test never fired — the handoff consequently advertised **C1** (closed) as its "First task", and would have re-advertised C26-C29 had they sat early enough in the file; (b) an item whose bold title **wraps across two lines** was dropped from `open_items` entirely, because the regex was anchored to one physical line — **C10 at lines 43-44** is written that way, and was saved from being a live bug only by being closed; (c) `open_items[:3]` at the "First task" emit was hardcoded, so the handoff showed an arbitrary three-item slice of a 16-item queue and the other thirteen were unreachable unless you already knew to open PRIORITIES.md. **Residual closed:** the first cut of the fix scanned the whole line for the bare marker word, so an open item whose prose quoted it was dropped as closed — this entry collided with itself and the parse fell to 12 items instead of 13; the filter now matches the marker as a bold status token, which is how the count reached 13. **Evidence the defect was live, not theoretical:** fixing (b) raised the parse from 15 to 16 open items, and the extra item is **C11** (ContainerManager exec cleanup) -- open the whole time, and silently invisible to every generated handoff because its title wraps. A queue that cannot see an item cannot prioritise it. **Fix landed:** a block, not a physical line, is now the unit of parsing and the whole queue is emitted; the parser is exercised by a direct probe (`_parse_open_items` against a synthetic wrapped item), because no test in `tests/` covers the generator at all.

- **C34: `scripts/gen_handoff.py` cannot run without a `git` binary.** **Open.** File only — do not fix. The generator shells out to `git rev-parse --show-toplevel`, `git rev-parse --short dev`, `git log -1 --format=%s dev` and `git log -5 --oneline dev`. In any sandbox with no `git` on `PATH` (agent container, worker sandbox) every call returns 127 and the generator dies before writing a byte, so the mandated "run the generator to refresh" instruction is unfollowable exactly where the handoff is most needed. Gen 62 got round it by putting a throwaway `.gen62-shim/git` stub on `PATH` (never committed). A real fix reads the four values out of `.git` directly, or accepts them as CLI arguments with the git calls as fallback.

- **C35: Worker force-summarisation drops pending dispatch payloads.** **Open.** File only. Gen 62: a worker dispatched with a two-part brief (a one-line source fix plus two verbatim ~250-word KB entries) completed part 1, then its own context hit the ceiling and was force-summarised mid-turn. The verbatim payload was gone from its memory, existed nowhere on disk, and part 2 was silently not done — while the envelope still returned `status: "final"`. That is a dispatch-integrity violation: a worker can report success on steps 1..n-1 and drop step n with no signal to the dispatching agent. Mitigation in use, not a fix: write long verbatim payloads to a `WorkingDocument` or workspace scratch file first so a summarised worker can re-read them, and send large inserts as their own follow-up query. Real fix not designed.

- **C36: `personal/bugs_and_fixes.md` is ~81,000 tokens and effectively unreadable.** **Open.** File only. Appending to it now trips the knowledge-base tool's own size warning. The file cannot be read in one pass — reading it costs an entire context window — so the bug log has quietly become write-only, which defeats the point of keeping it. Needs splitting by theme or era, or archiving closed entries into a separate file, plus a size discipline for future appends. `personal/lessons_learned.md` is smaller but growing on the same pattern and will hit the same wall.

- **C37: `scripts/gen_handoff.py` has no test coverage.** **Open.** `grep -rln 'gen_handoff\|First task' tests --include=*.py` returns nothing and `pytest -k handoff` collects zero tests (exit 5: 4 skipped, 6548 deselected), so the onboarding-truth file is verified by nothing. A generator change or a PRIORITIES.md format change can silently drop the whole live queue again -- that is exactly how C11 stayed invisible for a generation and how the dead filter advertised an already-closed C1 as 'First task'. Needs tests/test_gen_handoff.py that runs the parser against a fixture queue and asserts: titles wrapping across two lines are parsed (C11/C10 case); every '**Open.**' block is emitted and no block carrying the done-marker is; the emitted item set equals the parsed set (no [:3] style truncation regression); and the emitted list is non-empty whenever the fixture queue has open items. Must not require a git binary -- monkeypatch the git probe / use a temp git-less fixture (see C34).

- **C25: Worker State Reattachment.** **Open.** Named in the Gen 61 close dispatch as a recommended start after the Control Plane, but it was carried as a name with no scope, and nothing else is known about it. **The first task is to write the scope** — do not infer a design from the title, and do not assume the worker's saved context and its containers are re-attached by the same mechanism until that is read in the code.

---


- **C30: Expose `cpu_quota` on container recreate/restart.** **Open.** Filed by Gen 62, per Main ruling. `infra/container_manager.py` hardcodes `cpu_quota=100000` (0.5 CPU) on the persistent-container create path, so an operator (or EE engineer running scientific solves) cannot raise it. **Ruling in force: the container envelope is its birth certificate — do not mutate cgroups at runtime.** The Stop -> Edit Config -> Restart (recreate) flow is the correct architecture. **Fix path:** allow the caller (`docker_code_runner.py`, or a new operator/agent action) to pass `cpu_quota` when stopping and recreating a persistent container; wire it through `container_manager` create. **Do not fix on the Gen 62 onboarding branch — file only.** This is the container-strand work the EE engineer is blocked on.

- **Correction (Gen 62):** the pre-Gen-62 `HANDOFF.md` referenced "Phase 10 (EM-Solver / Resources) OPEN" and "C25 (Worker State Reattachment) open". **Neither exists in this queue — no C25, no Phase 10 section. The prior handoff hallucinated them.** Do not search the queue for either; if either is real work, it needs a fresh ID and a real scope, not an inheritance.


## Filed by the seat, Gen 63 — MODE_PROCESS follow-ups (scope only; do not fix here)

- **C38: `WorkerRuntimeHandle` still defaults to in-process when built by hand.**
  **Open.** `WorkerRuntimeHandle.__init__` keeps `mode: str = MODE_THREAD` as its default
  (`tools/workspace/worker_runtime.py`), so any caller that constructs a handle directly
  instead of going through spawn silently stays in-process. The Gen 62 flip gated the
  **spawn site** on `resolve_runtime_mode()`; it did not move the constructor's default.
  **Verified at HEAD `c1771222`.** Scope: decide whether the constructor default flips to
  process or direct construction is fenced and refused. Do not change the default here.

- **C39: the "em dash reaches users as a raw u-escape" claim is DISPROVED.**
  **Disproved** — do not re-chase without new evidence. Carried into Gen 63 as a known
  defect with a line citation in `tools/workspace/worker_thread.py`; both halves were
  checked and neither holds. The cited line is, at HEAD `c1771222`,
  `project_root = config_data.get("root")` — unrelated code, so that citation had drifted
  rather than pointing at a bug. A full scan of `tools/**/*.py` and `agent/**/*.py` for
  four-hex-digit `u`-escapes returns **11 hits, every one a single backslash inside a
  non-raw string literal or a comment, and zero doubled-backslash occurrences**. A single
  backslash escape in a non-raw literal is interpreted by Python at parse time, so nothing
  renders to model or operator as a literal escape. **Most likely origin of the report:**
  the em dash arriving in escaped form inside JSON-serialised tool envelopes
  (`ensure_ascii`), which is a display artefact of the envelope, not of the source.

- **C40: no end-to-end test spawns a real child process through the tool spawn path.**
  **Open** — coverage gap, not a bug claim, same standing as C23. The process-mode suite
  exercises `WorkerRuntimeHandle` and the supervisor protocol in isolation; nothing drives
  `Worker(action="spawn")` itself in process mode and asserts the envelope came back from
  the child. Scope: one hermetic test, spawn then dispatch then envelope, red if the
  runtime falls back to in-process. **The claim is filed unverified** — first task is to
  confirm the gap by reading the fixtures. If this test fails, the mode wiring is broken on
  `dev`; if it passes, the tree in the batch-1/2 measurement is the authority and the
  failing tests come from a different branch. Either way the test stands.

- **C41: `docs/` is a catch-all ignore, so every design deliverable needs its own negation line.**
  **Open.** `.gitignore` carries `docs/*` plus roughly sixty `!docs/<file>` negations, one
  per session that needed a doc published; a doc written without one is never staged and
  dies on the next clone, and `git clean -xdf` destroys it first. Verified at HEAD
  `c1771222`. Scope: decide the tier policy — negation for an approved directory, or keep
  the per-file tax and say so in KB_POLICY. Housekeeping; no code.

- **C42: Host Port seal / kill-switch CI contract (fail-closed).**
  **Open.** Resurrects the archived "Prove the Host Port is sealed" scope. A CI test must
  walk `tools/**` by **AST, not grep**, so docstrings and comments cannot false-positive,
  and fail if any module references `subprocess.`, `Popen`, `os.system`, `os.exec*` or
  `os.spawn*` outside the two sanctioned modules `tools/host_resource_policy.py` and
  `tools/host_bash_tool.py`. The default is **fail-closed**: an unlisted hit fails and the
  scan is never widened to make it pass. Legitimate pre-existing hits get an explicit
  allowlist entry with a comment naming why. Proof obligation: the test must be shown to
  **bite** (deliberate violating fixture, or an injected fake module), not merely to pass.
  Scope only — Batch 1a, one commit.

- **C43: Frontend scroll jump on new messages.**
  **Open.** Resurrects the phantom "C37". New WebSocket events currently snap the message
  list to the bottom unconditionally (`useStore.js`, message-array region near the 350s).
  Fix: track `isAtBottom` with a few-pixel threshold; auto-scroll only while pinned; when
  not pinned, surface a "New messages below" affordance that scrolls on click. Frontend
  only, no backend change, no new dependency without a flag. Scope only — Batch 1b, one
  commit.

- **C44: Worker trust boundary under borrowed context.**
  **Open.** Resurrects the archived "Verify Worker Trust Boundary" scope. **Batch 2 — no
  brief yet, do not start before Batch 1 merges.** The scope line stays deliberately thin
  until then, because the archived entry is a historical note, not a specification.

- **C45: worker briefing is stripped before turn 1.**
  **Done (pending a CI-green confirmation).** The brief now rides a `role="user"` message
  and the sanitizer has nothing to delete; the process-mode hole and the test gap are both
  closed. The original report is kept below, unedited — the file's rule is that a corrected
  item says so in place — and the evidence, including which premises moved, is in the
  **Correction** at the end of this entry.
  **Open (as filed)** — bug, high. `agent/core/llm_client.py:201` `ensure_system_prompt()` deletes, in
  place on `session.user_history`, every `role="system"` message whose content does not start
  with `"Summary of previous conversation:"`. It runs at `Agent.__init__` (`agent.py:554`) and
  every turn (`agent.py:1562`). Worker briefings are appended as exactly such a system message
  (`worker_thread.py:3747` and `3805`), so the brief is erased from persisted history before the
  first turn; queries survive only because they arrive as `user` turns via `_input_queue`.
  **Second, independent hole (process mode only).** `_initial_context` is set after construction
  (`worker_thread.py:6292`) and is missing from `_OPTIONAL_KWARGS`
  (`worker_runtime_process.py:112-127`), so the child builds `WorkerThread(**kwargs)` with
  `_initial_context=None` and the system message is never built at all.
  **Test gap.** `TestBriefingDeliveredInFull` (`tests/test_worker_timeout_audit.py`) asserts only
  the append; nothing runs `ensure_system_prompt` over the constructed history.
  **Fix direction (agreed).** Carry the briefing on the first `user` message instead of adding a
  new survivor prefix to `ensure_system_prompt` — that rule guards token explosion elsewhere.
  The regression test must run the real `ensure_system_prompt()` over the history the worker
  actually builds. Cited at `25be340a`; the fix's first step is to re-verify each line number.
  **Correction (Gen 70 — closed).** Every window was re-read at dev@`7bf3f1ab`, and the
  mechanism described above no longer exists:
  (1) **The brief is not a system message at all.** `initial_context_message()`
      (`tools/workspace/worker_thread.py:1669-1690`) returns
      `{"role": "user", "content": "Initial context: " + <json>}`, and its docstring names
      the exact sanitizer pass it dodges (`agent/core/llm_client.py:229-234`). The erasure
      path had no input left to erase.
  (2) **The process-mode hole is closed.** `_OPTIONAL_KWARGS`
      (`tools/workspace/worker_runtime_process.py:115-127`) carries
      `("initial_context", "_initial_context")`, commented "Without this line the child
      interpreter builds a worker with no brief at all." The child no longer constructs
      `WorkerThread` with `_initial_context=None`.
  (3) **The test gap is closed.**
      `tests/test_worker_timeout_audit.py::TestBriefingSurvivesRealSystemPromptSanitizer::test_briefing_survives_real_ensure_system_prompt`
      (1082-1113) runs the REAL `LLMClient.ensure_system_prompt()` over the history the
      worker actually builds, and asserts that only the real system prompt keeps
      `role="system"` — i.e. it fails if the brief is dropped again.
  (4) **The "fix direction" recorded above is what shipped, not a plan.** Carrying the
      brief on the first `user` message was done; no new survivor prefix was added to
      `ensure_system_prompt`.
  **Status is "Done (pending a CI-green confirmation)" deliberately: no merge SHA is
  claimed.** The change was read on a working tree, not on a commit, and this file does not
  cite a SHA it has not seen.

- **C46: host-port kill-switch scan root is the wrong shape (class fix, not a point fix).**
  **Open.** C42's guard scans `tools/**` only. `thoughtmachine/doctor.py` has `import subprocess`
  plus `subprocess.run` and is covered by neither C42 nor
  `tests/test_tool_import_declaration_match.py`.
  **Ruling: the defect is the scan root, not doctor.** C46 = determine whether `doctor.py` is
  agent-reachable (via any tool or HTTP route) and audit the scan root itself. If reachable,
  route it through `thoughtmachine/host_port.host_run()`. If not reachable, allowlist it with a
  written reason AND a reachability pin (a test that fails the day it becomes reachable).
  **Separately decide** whether `thoughtmachine/**` enters C42 scope or gets its own named
  allowlist. Do not widen C42 inside the C42 commit; that scope decision belongs here.
  **Correction (Gen 70 — re-scoped to the scan-root half only).** Half of this entry has
  landed: the reachability audit of `thoughtmachine/doctor.py` exists, and its reachability
  pin exists. What is still open is the SCAN ROOT. `tests/test_host_port_kill_switch.py:53`
  still reads `SCAN_ROOTS = ("tools",)`, so `thoughtmachine/**` has import coverage (from
  `tests/test_tool_import_declaration_match.py`) but **no kill-switch reachability
  coverage**: a module under `thoughtmachine/` can reach the host process surface without
  the AST guard ever considering it. The remaining scope is exactly the named decision this
  entry was filed to force — does `thoughtmachine/**` enter C42's scope, or get its own
  named allowlist — and it still stays out of the C42 commit.

- **C47: onboarding bundle is a stale snapshot, not a living artifact (delivered on this branch).**
  **Open** — delivered on `feat/gen64-taskB-c47-onboarding`; the entry stays open only until
  the merge SHA is known (same standing as C33). Acceptance contract: the emitted onboarding
  bundle must include `HANDOFF.md` verbatim, and its manifest must cover `MANDATORY_STOP.md`,
  `docs/ARCHITECTURE_MACHINE_MODEL.md` and
  `.thoughtmachine/knowledge/project/architecture_invariants.md`; staleness must be
  content-addressed (sha256), not mtime-based; `--check` mode must exist and exit non-zero when
  the bundle is stale, and it must be wired into CI so staleness fails the build.
  **Maintenance rule.** When a bug reveals a missing rule, the same commit that fixes the bug
  updates `MANDATORY_STOP.md` / `ARCHITECTURE_MACHINE_MODEL.md` / `HANDOFF.md`.

- **C60: Test Isolation Failures.** **FIXED** in `35c11fbc` ("fix(test): restore the
  provider registry between tests"). Four pytest fixtures in
  `tests/test_worker_loop_spike.py` and `tests/test_worker_tool_call_start.py` wrote the
  CLASS attribute `ProviderFactory._providers` by bare assignment with no teardown, which
  permanently disabled the factory's lazy population for the rest of the process; they are
  now `monkeypatch.setattr`-based and self-restoring, plus an autouse
  `_restore_provider_registry` guard in `tests/conftest.py`. The reproducing pair
  (`tests/test_worker_loop_spike.py tests/test_worker_pause_reading_mode.py`) went
  **8 failed / 35 passed -> 43 passed**, and the failure signature
  `ProviderNotFoundError: Provider 'openai_compatible' not found. Available: ['echotool']`
  is gone. Original description kept below. The suite is not order-independent, and
  it does not fit in one process. Evidence, measured on this branch: `pytest tests/`
  collects **6771** tests; a single-process full run is **OOM-killed (exit 137) at ~79%**
  (~5,300 tests, zero failures to that point), so the suite can only be run today by
  chunking into separate processes. The chunked run gives **6697 passed / 10 failed /
  67 skipped**. All 10 failures are order/isolation artifacts and **every one passes in
  isolation**: 8 in `tests/test_worker_pause_reading_mode.py` (they pass in their own
  10-file chunk and standalone), 2 in `tests/test_worker_operation_semantics.py`
  (`TestWorkerPauseResumeSemantics::test_next_operator_turn_clears_stranded_main_pause`
  and `::test_worker_turn_does_not_clear_main_pause`), plus
  `tests/test_worker_stale_watchdog.py::test_stale_check_disabled_by_default` (flaky).
  **Root-cause hypothesis:** a global LLM provider registry polluted by an earlier test
  file in the same process; the symptom is
  `ProviderNotFoundError: Provider 'openai_compatible' not found. Available: ['echotool']`
  raised from `ProviderFactory.create_provider` during `Agent()` construction. File it as
  a hypothesis, not a diagnosis — the polluting file is not yet identified.
  **Acceptance criteria (desired state, not a proposed fix):** the full suite must pass
  in a **single process with no order-dependent failures**, run in either order
  (collection order and reverse), and the run must not exceed the container's memory
  envelope; failing that, a **deterministic chunking harness** must exist — one
  committed entry point that partitions the suite into fixed, documented chunks in
  separate processes, aggregates a single pass/fail summary, and is what CI runs.
  The suite must not be "green only if you know which files to keep apart".

- **C61: In-flight Tool Cancellation & Dispatch Window.** **Open** — proposal /
  hypothesis, not a confirmed mechanism. Two measured defects in the same seam, both
  observed with the TimeBudget live (`thoughtmachine/timebudget.py`, envelope keys
  `requested_timeout_seconds` / `effective_timeout_seconds` / `timeout_clamped` /
  `timeout_clamped_by` / `timeout_source`).
  **Defect 1 — the budget stops NEW tool calls and never an in-flight one.** Measured with
  `effective_timeout_seconds=60`: the worker issued `python3 -c 'time.sleep(200)'`, the call
  completed in **205.63 s** (`timed_out: false`, exit 0) and the worker survived ~210 s.
  The worker's own words: "every other tool was restricted at the 60 s tick, yet the
  already-dispatched sleep ran to 200.0 s undisturbed." The timeout is enforced at
  *admission* time only, so anything already dispatched outlives the deadline by an
  unbounded factor.
  **Defect 2 — the caller's dispatch window is ~2x the effective budget.** Measured:
  effective 60 -> `dispatch_duration_wall` **120.0 s**; effective 240 -> blocked **300 s**;
  effective 300 -> returned **277 s**. `timeout_seconds` therefore bounds the *worker's*
  new-work deadline, not the *caller's* block: the caller can wait twice as long as the
  number it passed in.
  **Fix direction (proposal).** Worker/command execution already runs as a real OS process
  inside the container, so cancel at the process level instead of in the loop: keep a
  handle for the currently-running command (the `Popen` and the `docker exec` session id)
  in the supervisor; on budget expiry send **SIGTERM**, wait a short grace (~2 s), then
  **SIGKILL**; **reap** the child so nothing is orphaned; then return an envelope with
  `status: "timeout"`, the **partial stdout captured so far**, and the five budget keys
  extended with an explicit `timeout_enforced` / `cancelled_in_flight` flag so a caller can
  distinguish "answered in time" from "killed at the deadline". Separately, clamp the
  dispatch window to `effective_timeout_seconds` instead of a multiple of it.
  **Constraints the fix must respect.** No orphaned or leaked processes; the container must
  remain reusable after a cancellation (no wedged exec session); cancellation must be
  **idempotent** (a second expiry/cancel is a no-op, never a second signal to a recycled
  pid); a cancelled command must **not corrupt an in-progress KB/file write**; and the
  mechanism must not be confused with the C12 `interrupt()` sticky-fallback trap.
  **Acceptance criteria.** (1) With `effective_timeout_seconds=60`, a tool that sleeps 200 s
  returns in **<= ~62 s** with `status: "timeout"`, `cancelled_in_flight: true`, a
  non-success exit, and the partial stdout produced so far. (2) Caller wall time for that
  call is `<= effective_timeout_seconds + grace`, never ~2x. (3) After the cancellation the
  container is still usable (a following `echo` succeeds) and no surviving sleep process
  remains in it. (4) A second cancel of the same execution neither raises nor kills an
  unrelated pid. (5) Every timeout envelope carries `timeout_enforced` /
  `cancelled_in_flight`, so "no timeout" and "timeout not enforced" are distinguishable.
  (6) A short command that finishes inside the budget is unchanged (same envelope, no new
  key flipping true).
  **Test sketch.** Seed a worker with `effective_timeout_seconds=60`; dispatch
  `python3 -c 'time.sleep(200)'`; assert the round-trip wall time is `<= 65 s`, the
  envelope says `status: "timeout"` + `cancelled_in_flight: true`, and elapsed time is
  nowhere near 200 s; then run a follow-up `python3 -c 'print(1)'` through the same
  container, asserting it returns 1 (container reusable) and that no `sleep 200` process
  survived; finally invoke the cancel path a second time and assert it is a silent no-op.
  The numbers above are a dated snapshot measured on this branch — re-measure before
  fixing.

- **C68: Fail-closed worker session identity; injected fields off the schema.**
  **Done** `ab102eab` (fail-closed session identity) + `ff9ce1e0` (injected-field schema
  leak). A worker whose session identity cannot be established now refuses the dispatch
  instead of running under an ambient default, so a dispatch can neither widen nor inherit
  an identity it was not given. Separately, the executor-injected fields
  (`session_permissions`, `effective_permissions`, `agent_config`, `is_worker_context`,
  `session_id`) are declared `pydantic.json_schema.SkipJsonSchema` on the tool base model:
  they still validate and stay usable at runtime, but no longer appear in
  `model_json_schema()` or in the `model_to_openai_tool()` parameters, so a model can
  neither see nor forge them. Pinned by `tests/tools/test_tool_schemas.py`.

- **C69: Replay Resistance via Spec Nonce.** **Open** — filed by the agent during the C68
  work; scope only, nothing designed. A dispatch that is replayed or duplicated (a resend,
  a retry, or a second reader of the same envelope) can be executed more than once today,
  and the repeat runs under whatever identity is current rather than the identity the
  dispatch was issued under. Scope: mint a nonce into the worker spec at dispatch and echo
  it in the envelope, so the runtime refuses a nonce it has already executed and a stale
  nonce is rejected instead of replayed under a newer identity. First task is to decide
  where the nonce is minted and for how long it is remembered — do not infer a design
  from this line.

## Numbering (Gen 63 housekeeping)

- **The Archived block reuses two live IDs.** It holds a **C35** ("Prove the Host Port is
  sealed") and a **C36** ("Verify Worker Trust Boundary"), each printed **twice**, while
  the Active list's C35 is worker force-summarisation and its C36 is the
  `bugs_and_fixes.md` size. An ID is not a key in this file any more: quote the title when
  briefing work. Not renumbered here, because archived entries are a historical record.

- **C37 is unassigned and reserved.** The Gen 63 handover names C37 as the message-list
  scroll-jump item ("track at-bottom; auto-scroll only when pinned, else 'New messages
  below'", in the `useStore` message-array region), but no C37 entry exists in this file.
  The seat did not invent one: the scope above is the engineer's wording, not a reading of
  the code, and this file's rule is that a line number is a dated snapshot of something
  read. It is filed when someone reads it.

- **The Gen 62 Correction block contradicts this file.** It states that neither C25 nor a
  Phase 10 section exists, but an active **C25: Worker State Reattachment** entry does sit
  in the Active list (its scope: "the first task is to write the scope"). One of the two
  must go. Not edited here — a ruling decides that, not housekeeping.

- **C42/C43/C44 are fresh scoped items resurrecting archived and phantom scopes, per the
  Infra 63 ruling; active C35/C36 are untouched.** Each new entry quotes the scope it
  resurrects, so the archive collisions stop biting. Do not un-archive or renumber the
  duplicate historical entries.

- **The Gen 62 "Correction" block is stale with respect to C25.** It asserts that no C25
  exists, but an active C25 does, and **the Active list wins.** The block stays as
  historical text; do not delete it.

- **Tool defects noted (C32 class, not scheduled) — partly retracted at the Gen 69 housekeeping
  pass.** The `show_file` half of the note below did not reproduce: `git_read
  show_file(source='committed', ref='49ab4e5a', path='docs/design-worker-presence.md')` honoured
  the `ref` and reported which source it used — the file exists at that commit and not at dev
  HEAD, and the content returned matched the commit. The `ls_tree`/`prefix` claim was not re-tested
  and still stands unverified. Original wording, kept as historical text:
  > `git_read show_file` ignores its `ref` argument and silently returns HEAD; `git_read ls_tree`
  > ignores `prefix` and dumps the whole tree; `Worker(action="spawn")` rejects a call that omits
  > `worker_name` although its documentation says it defaults. The first two are the dangerous
  > ones: reads of authority files that quietly answer a different question than the one asked. A
  > fourth report — worker briefings arriving stripped — is filed as **C45** in the Generation 64
  > section above, and is no longer "under probe and not filed".

- **The `docs/CONTRACTS.md` row request is a design pass, not housekeeping (Gen 69 finding — ruling
  needed).** A Gen 69 punch list asked for `docs/CONTRACTS.md` to grow rows for C76 / C81 /
  C82-C86 / C90 / C92. That file has **no C-id row index**: measured at 293 lines, its headings are
  "Contract 1", "Contract 2", "Contract 3", "Known gaps", "How this document stays honest" and
  "What CI actually runs — and what this document does not claim", and the only two literal C-ids in
  the whole file are C68 at line 20 and C91 at line 270. It is also machine-checked by
  `tests/test_contracts_ledger_parity.py`, which requires every backticked path in it to exist and
  to appear in the pytest list in `.github/workflows/contract-gate.yml` — so adding claim rows is
  not a formatting exercise. Adding such rows is a **design pass** (each claim needs an enforcement
  site plus a mutation-proof test), not housekeeping. C81 and C82-C85 appear nowhere in this file,
  so there is no prior scope to transcribe. Ruling needed.

## Live items recovered from the Archive block (Gen 70 restructure)

These thirteen entries were live, unscoped-for-deletion work that sat BELOW the
`## Archived` heading and were therefore invisible to `scripts/gen_handoff.py`, which used to
slice the file at that heading. Nothing about their content changed here: the bodies below are
moved verbatim, only their position moved. They are still open unless the body says otherwise.

- **C12: `cancellation.interrupt()` sticky-fallback trap.** **Open.** Calling `interrupt()` with no bound token arms a per-thread fallback event. `unbind()` does not clear it, so every subsequent `is_interrupted()` on that thread reads True permanently. This poisons the thread for the rest of the process (found during C11). Fix: clear the fallback on `unbind()`.

- **C14: `visual_tool.py` missing cancellation reporting.** **Open.** An interrupted render drops the `reason` and returns `"visual: the renderer exited with code None."` to the model. This looks like a renderer crash, not an operator cancel, causing the model to hallucinate a failure. Teardown runs correctly (pinned by test). Fix: detect `status == "interrupted"` and return a clear cancellation message. (Discovered during C11').

- **C74** — `tools/mcp_client.py` bypasses the host-port chokepoint. (~11 other unadjudicated `import subprocess` sites).
- **C75** — The AST no-tool-wrapping guard is parked, not enforced. 3 of its 4 own tests fail on an untouched checkout.
- **C77** — `web_ui/backend/tests/test_bridge_dedup.py` poisons `sys.modules` at collection, causing real-bridge tests to skip.
- **C78** — Preflight diagnostics truth: two port probes, FAIL weight on advisory output.
- **C79** — Undeclared load-bearing config key (`git_allow_worktree_commits`) and worker worktree bootstrap failure (crash loop).
- **C80** — `terminate_all` skips subprocesses with no pid during shutdown.

- **C86** — Process-Mode Observability (State Seam). Event seam is fixed, but state seam is leaking. Enforced deadline != reported deadline (10s requested, 70s blocked). No exit-code reader exists. Cache `_last_event` is never read. See audit doc for 12 proposals.
- **C88** — Host Resource Guardrails. System OOM-killed backend/workers under heavy parallel load (3 workspaces). Pre-flight memory check needed before spawning workers/containers. Enforce hard caps on child processes.
- **C89** — Session Autosave. Lossy crashes drop active conversational context and UI state. Needs periodic write-to-disk for session state (beyond just the final prompt).

- **C90** — Exec Reaper SIGTERM. `ContainerExecTool`/`DockerCodeRunner` reaper kills the process group (rc=143) even on low memory. Workaround: run via `setsid` and ignore SIGTERM. See logs/5b_sigterm_finding.md.
  **Later observation — the former `C90 (CRITICAL)` escalation, merged into this entry at the
  Gen 69 housekeeping pass:** Local Exec Reaper is now killing pytest runs at 3-27 seconds.
  `oom_kill=0`, memory is fine. It's killing the process group (exit 143). Local verification of
  large suites is impossible until this is fixed.
  **Workaround correction (measured, Gen 69 housekeeping pass):** `setsid` is not available in
  this container, so the workaround as written cannot be used. The practical rule is instead: run
  each verification as one self-contained command, and retry once if it dies without a summary
  line. `logs/5b_sigterm_finding.md` still stands as the original evidence.

- **C92** — `agent/knowledge/codebase_indexer.py:38-39` installs module-level `SIGINT`/`SIGTERM` handlers on import. This is a latent defect that could hijack the server's signal handling.

## Filed Gen 70 (verification debt)

Debt found while repairing the generator's own blind spot, filed rather than fixed.

- **C95: The onboarding router is pinned to a SHA that is not dev.** **Open.** `tests/test_onboarding_bundle_contract.py:93` holds `PINNED_DEV_SHA = "3eef2b56"`, and `HANDOFF.md`/`MAIN_ONBOARDING.md` quote `dev@3eef2b56` as the current dev tip. Measured this generation: the real dev tip is `7bf3f1ab5c6602f6fe07828937cfdce618311a8d`. The pin is documented as deliberate coupling (comment at :91-92), so this is not a simple stale literal — either the router is meant to describe a frozen point and the prose must say so, or it is meant to describe dev HEAD and the pin must be refreshed. Ruling needed; not changed here.

- **C96: The handoff generator was reading a truncated file, so the live queue was smaller than the file.** **Open.** `scripts/gen_handoff.py` sliced `PRIORITIES.md` at `## Archived` before parsing, so every open item below that heading was dropped: measured 28 open ids from the pre-archive slice against 41 from the whole file (13 invisible: C12, C14, C74, C75, C77, C78, C79, C80, C86, C88, C89, C90, C92). The generator now parses the whole file and the movers are re-headed above. Two halves remain: (a) the C33 half of the same class — its slice and its vocab — was left unrepaired; (b) this entry stays open until the repair has a merge SHA. Do not close it on the strength of a working tree.

- **C97: Claim repair outranks code repair.** **Open.** In this generation the file's own claims were wrong more often than the code was: C45 was recorded open for six generations after the mechanism it describes was already pinned working, and C46 described a guard that does not exist in the scope it named. Both were settled by reading code, not by writing it. Standing rule for the next runner: read the cited file before scheduling the work, and repair the entry before repairing code that may not be broken.

- **C98: The knowledge base advertises domains it cannot serve.** **Open.** The KB tool's domain list names surfaces that do not resolve for an agent session; `MAIN_ONBOARDING` and `project/MAIN_ONBOARDING` were both rejected. Workaround that works: `git_read show_file(source='committed'|'working', path=...)`, which reports which source it used. Not repaired here — the domain list and the readable set must be reconciled, and that is a tool-surface change, not a doc edit.

## Filed Gen 71 — LEDGER audit (branch `docs/ledger-c95-and-audit-tickets`)

**Numbering correction (measured).** The audit brief said "start at C95; C94 is the
high-water mark; C88/C89/C90/C91 are taken". Measured in this file at `c89cb56b`:
**C95, C96, C97 and C98 are all already allocated** under `## Filed Gen 70 (verification
debt)` (`:516`, `:518`, `:520`, `:522`), so C95 is not free; **C93 and C94 have no row at
all** (zero hits for either id); **C91 has no row either** — only a cross-reference at
`:471`. The highest allocated id is **C98**. These tickets are therefore filed as
**C99–C105**. No existing id was renumbered and no gap was back-filled. Every citation
below was read at `c89cb56b`; anything not read there is marked **UNVERIFIED**.

**Id mapping against the audit brief (recorded so the cross-reference resolves).** The brief's working id "C95" for the auth-e2e finding is filed here as **C99** — C95 (`:516`) is the onboarding `PINNED_DEV_SHA` ticket and stays that, and the auth-e2e material does not belong in it. The branch-protection measurement is not C98 either: C98 (`:522`) is the KB-domain-list ticket, so a citation of "C98" for branch protection is a mis-mapping. The stale-declaration finding is carried by **I14** (`docs/MACHINE_INVARIANTS_AND_LORE.md`) plus **C99** and **C105**.

- **C99**: The auth e2e job is a job *id*, not a required-context *string* — promote it to a required status check on `dev` and stop the docs conflating the two. **Open.**
  - `.github/workflows/platform-matrix.yml:175` `  linux-e2e-auth:` (the job id) and `:176` `    name: Linux - auth e2e (browser)` (the required-context string) — both verified byte-exact.
  - `scripts/apply_branch_protection.sh:79` registers the *name*: `  "Linux - auth e2e (browser)"                  # job linux-e2e-auth (platform-matrix.yml)`. So the in-tree protection script already lists it.
  - `docs/CONTRACTS.md:245` and `:264` refer to `linux-e2e-auth`, i.e. a job id, in a position where a required-context string is what GitHub matches. Same conflation class the doc corrections (see the doc-fix commit on this branch) address.
  - **Confirmed against the live ruleset (operator measurement, 2026-11-19).** `Linux - auth e2e (browser)` is **not** among the required contexts: ruleset `24214086` stores 12 entries resolving to 11 unique contexts, and this name is not one of the 11. The instrument is recorded in `docs/BRANCH_PROTECTION.md`. The audit's "asserted only by an in-repo ticket" status is **superseded** by that measurement, not retracted — the ticket was right.
  - Promoting it is safe, and the reason is worth writing down instead of assuming: the job carries no `if:`, so it always reports, and its conditional work self-skips when no auth file changed — a skip reports as success to GitHub. A required-but-skipping context therefore does not block unrelated merges.
  - Done means: a push to `dev` missing only the auth e2e check is rejected, and `docs/CONTRACTS.md` names the context string, not the job id.

- **C100**: The worker worktree resolver fails *open*; it must fail *closed* — a worker that cannot get its own worktree must be refused, not silently handed the main agent's tree. **Open.**
  - `tools/workspace/worktree_defaults.py:9` states the design intent: `Everything here is deliberately FAIL-OPEN and side-effect-scoped:`, and `:15-16` spells out the consequence: "which leaves the caller on the workspace root -- exactly the pre-existing behaviour."
  - `:113` `def resolve_worker_worktree_root(`; `:170` `status = runner(["worktree", "add", "--detach", worktree], root)`; `:171` `except Exception as exc:  # a runner must never break the spawn boundary`; `:177` `if status != 0:`; the failure returns are `:130, :138, :145, :158, :164, :175, :182, :189` (all `return None`); the only non-`None` return is `:191` `return worktree`. **There is no refusal path anywhere in the function.**
  - Caller: `tools/workspace/worker_thread.py:3922` `if not worker_cfg.get("worktree_root"):` — on failure `worktree_root` is simply left unset (`:3927-3933`), so the worker runs on the workspace root and two workers can collide. The caller's own comments admit it: `:3917-3919` `# Fail-open: no workspace root, no git` / ``# binary, no repository, or a failing `git worktree add` all leave`` / `# worktree_root unset`; `:3914` `# An explicitly configured root (the parent session's) always wins.`
  - Coordinate with commit `724dcc89` ("fix(worktree): create the worker worktree on a branch, not detached"), which rewrites the `--detach` at `:170`. Line numbers in `worktree_defaults.py` are ref-sensitive: re-read before citing.
  - Done means: a worker whose worktree cannot be created fails the spawn with a named error, and no code path returns a worker with an inherited root by accident.

- **C101**: The `mode` layer silently overrides `enabled_tools`, and the UI offers no way to tell that a save was discarded — needs a design pass, not a patch. **Open (design pass only).**
  - Coercion at model-construction time: `agent/config/session_config.py:226` `@model_validator(mode='after')`, `:227` `def enforce_mode_presets(self)`, `:242` `preset_tools = get_tools_for_mode(self.mode)`, `:243` `if preset_tools:`, `:244` `object.__setattr__(self, 'enabled_tools', list(preset_tools))`; the custom-mode variant is at `:237`.
  - **Correction to the circulating claim.** The brief said `update_tools` ends in a "bare return". It does not. `:266` `def update_tools(self, new_tools)`, `:272` `if self.mode and self.mode != 'custom':`, `:273` `return  # mode-locked — cannot override`, `:274` `self.enabled_tools = list(new_tools)`. It is a **mode-conditional silent discard**, not an unconditional one: in `custom` mode the assignment does happen. Any ticket text that says "bare return" is wrong and should be corrected wherever it repeats.
  - UI: `web_ui/frontend/src/components/ConfigPanel.jsx:1735` `disabled={!wsConnected || isApplying}` — the Apply button has **no mode term**, so in a locked mode it is enabled, accepts the click, and the write is thrown away. Contrast the per-tool checkbox at `:1449` `disabled={isModeLocked}`, which does model the lock. Apply handlers at `:1733` and `:1752`.
  - Done means: a design note that decides whether the lock belongs in the model (reject), the API (400), or the UI (disable) — and the UI stops offering a control whose effect is conditional.

- **C102**: Branch names are hardcoded as literals in tooling and in a test fixture; the audit's "~40 occurrences" count is not reproducible and must not be quoted. **Open — deferred.**
  - Measured at `c89cb56b` over `agent/`, `scripts/`, `tools/`, `web_ui/backend/` (regex `(['\"])(dev|master|origin/dev|origin/master)\1`, extensions .py/.js/.jsx/.ts/.tsx/.sh/.yml/.yaml/.json/.md/.html, excluding `tests/`, `node_modules`, `dist`): **5 occurrences on 4 lines** — `agent/` 0, `scripts/` 3, `tools/` 2, `web_ui/backend/` 0. The "~40" figure in the audit input is **UNVERIFIED** in that scope; a whole-repo count including `docs/`, `tests/` and `resources/` has not been done.
  - Hits: `scripts/gen_handoff.py:315` `dev_sha = sh("git", "rev-parse", "--short", "dev")`, `:319`, `:323` `recent = sh("git", "log", "-5", "--oneline", "dev")`; `:174` renders ``- `dev` at `{dev_sha}` -- *{dev_subj}*`` into the handoff, `:342` writes it; `tools/git_write_tool.py:163` `_PROTECTED_BRANCHES: ClassVar[tuple] = ("dev", "master", "main")`.
  - Test fixture pins a moving target: `tests/test_onboarding_bundle_contract.py:96` `PINNED_DEV_SHA = "7bf3f1ab"`, asserted at `:278`, guarded by `:427` `def test_pinned_dev_sha_is_an_ancestor_of_dev():` (`:449-450`, `:460-461`).
  - The contract gate that would police this is `contract-gate` — `.github/workflows/contract-gate.yml:38` `  contract-gate:`, `:39` `    name: Contract gate (docs/CONTRACTS.md)` (lowercase `g` in the display name; the two strings differ, so copy both verbatim).
  - Done means: a single source for the integration branch name, and a real count to justify the work.

- **C103**: There is no principal in the tool surface for merging; a merge cannot be performed by the agent at all, only simulated by a human. **BLOCKED.**
  - No code citation can be produced, because the capability does not exist: `tools/git_write_tool.py:196-199` is the operation `Literal` and contains no `merge`, `pr` or pull-request verb; `:181-182` is the refusal comment that enumerates what the tool will not do.
  - Blocked on credentials: the blocking factor is the vault-held principal used for authenticated writes, and the vault is not readable from the workspace container. Do not close this ticket on the basis of repository evidence alone.

- **C104**: Twelve smaller audit findings, each with the evidence and the verification status it actually reached. **Open (batch).**
  - **B1 — a merge cannot be committed.** `tools/git_write_tool.py:1097` `args = ["commit", "-m", self.message, "--"] + rels` — a pathspec is always appended, and `:1094` `add_output = self._git_add(repo_root)` stages everything first; `file_path` is required (`:1046-1049`). git itself refuses a partial commit while `MERGE_HEAD` exists, so a conflicted merge cannot be concluded through this surface. Observed in-session; the git half is upstream behaviour, not a bug here.
  - **B2 — the refusal list is incomplete.** `tools/git_write_tool.py:181-182` names fetch, reset, clean, cherry-pick and rebase as deliberately absent, but **never mentions `merge`** — so the one absent verb that a reader is most likely to look for is the one the docstring omits. The authoritative surface is the `Literal` at `:196-199`.
  - **B3 — the tool's own docstring under-reports the surface.** `:104-105` lists 8 operations; the `Literal` at `:196-199` lists 13. Undocumented in prose: `worktree_add`, `worktree_remove`, `stash_push`, `stash_pop`, `stash_drop`.
  - **B4 — test scratch directories leak into the repo root.** `tmpgc4ksvne/` was present as `??` (untracked) in `git status` during this audit. Untracked means no existing check sees it. Verified by direct observation, single instance, cause not investigated.
  - **B5 — `delete_branch_on_merge` is nowhere in the tree. CLOSED as measured (operator, 2026-11-19).** The repository setting is `delete_branch_on_merge = false`, and `default_branch = dev`. The branch count is **150+**, an operator count with **no instrument recorded**: it is recorded here as approximate and must not be laundered into an exact figure. The in-tree half still stands — grep over `scripts/`, `docs/`, `.github/` returns 0 matches, so nothing in the repo reports or asserts either setting, which is why both were UNVERIFIED in the first place. Standing rule, filed with the correction: record the instrument or do not quote the number.
  - **B6 — a py3.11 flake in `test_worker_tray_truth_fields`.** Reported in the audit input; **UNVERIFIED** — not reproduced in this session, no log captured. Needs a reproduction before it is worth work.
  - **B7 — an out-of-range committed read succeeds silently with an empty body.** `tools/git_info_tool.py:2497-2505` (`_slice_line_window`, whose comment says "an out-of-range `end` simply clamps to the end of the file", ending `return "\n".join(lines[start - 1:end])`); `:2563-2567` always prepends a header and never checks that the body is empty; `:2590` states the contract as a feature: ``* both given: honoured exactly (`end` clamps to EOF);``. Confirmed for `show_file`. **The `show` operation is a different code path** — it shells out to `git -L` at `:2066-2069` and was **not** verified to behave the same: **UNVERIFIED**.
  - **B8 — "`ls_tree` re-roots its output on `path`" is REFUTED, and the refutation has a second half.** `_git_ls_tree` (`tools/git_info_tool.py:2228-2245`) builds `["ls-tree", "-r", treeish, "--", *rels]`: `path` reaches git as a pathspec after `--` (`:2243-2245`), so it **filters** the listing and full paths are preserved — operator-measured, `path=".github/workflows", recursive=true` returns 6 entries with full paths. Two corrections follow.
    - **`prefix=` is not an `ls_tree` parameter at all.** It is declared under `# for_each_ref parameters` (`:578-582`); the `ls_tree` surface is exactly `treeish`, `recursive`, `path` (`:584-596`). The older note that "ls_tree ignores `prefix`" was therefore never observing a dropped filter — it was passing an argument the operation does not have.
    - **What actually re-roots is `treeish`.** `treeish="<rev>:<path>"` makes git emit a tree rooted at that path. `path=` does not, and never did.
    - **The real trap, and the one worth carrying forward:** `path="<dir>"` **without** `recursive=true` returns the single tree entry for that directory — one line, the directory's own object — which reads exactly like "the filter was ignored". `recursive=true` is what turns it into a listing, and `-r` is honoured at `:2235-2236`.
  - **B9 — the worker timeout advisory overshoots, and the overshoot is normal.** Measured in this session: requested 200s, actual elapsed **213.8s**, with `timeout_triggered: true`. Overshoot confirmed. The circulating variant ("150 → 224s with `timeout_triggered` **false**") is **UNVERIFIED** — in this run the flag reported `true`.
  - **B10 — the `Worker` tool's `inspect` action is described but not exercised.** Its description promises runtime mode and child pid, and null without a runtime handle; not reached during this audit: **UNVERIFIED**.
  - **B11 — the remote `master` branch is gone, and this one is proven rather than assumed.** `git fetch --prune` printed `- [deleted] (none) -> origin/master` — the prune removed the stale remote-tracking ref because `refs/heads/master` no longer exists on the remote. Recorded because the `docs/MACHINE_INVARIANTS_AND_LORE.md` §3.1 retraction rests on that ref being absent, and a count of "copies of `master`" is only honest if the tombstone is named: one of the copies is now a deletion notice.
  - **B12 — the write surface has no `amend` and no `reset`.** `tools/git_write_tool.py:196-199` enumerates thirteen operations and neither of them appears; the docstring at `:181-182` does not name them as deliberately absent. So "amend the previous commit" is not merely discouraged, it is unimplementable: a correction lands as a follow-up commit or as an operator rebase. The tool's vocabulary decides the shape of history — a commit cannot be quietly rewritten by anyone who has only the tool.

- **C105**: A job made blocking inside its workflow is not a required context — blocking-ness and gating are independent, and the repo's job names conflate them. **Open.**
  - Two mechanisms, neither implying the other: *blocking-ness* is a property of the workflow job (`continue-on-error`, and whether its failure fails the pull request's workflow verdict), while *gating* is membership in the branch ruleset's `required_status_checks`. A red check that is not in the ruleset fails **its own run** and stops **no merge**.
  - Measured against ruleset `24214086` (the 11 unique required contexts, listed with the instrument in `docs/BRANCH_PROTECTION.md`): **`web-ui-backend-suite` is not among them.** C91 promoted that job to blocking and dropped `(advisory)` from its name — the promotion changed a name, not the ruleset. **`Windows Python 3.14 gate proof`** is not among the 11 either, so a red one proves nothing at the gate; the qualifier lives in the display name and nowhere else.
  - Same family as `(advisory)` being a *name* rather than a policy: a qualifier in a job's display name is prose, and only the ruleset is enforcement. Reading `blocking` or `gate proof` in a workflow and inferring gating is the error this ticket records.
  - Done means: every name meant to gate a merge is in the ruleset, **or** the docs stop calling it blocking — plus a name-level check that fails when a `blocking`, `gate proof` or `(advisory)` qualifier in a job name disagrees with the ruleset's membership set.
  - `docs/BRANCH_PROTECTION.md` and `docs/CONTRACTS.md` already cite C105; this entry is what makes those cross-references resolve rather than dangle.
  - **Measured after this ticket was filed** (operator, `gh api`, the same ruleset 24214086): it now stores **13 entries / 12 unique contexts** — `web-ui-frontend-suite` has been added — while the ruleset page renders only **12 rows**, because the UI **de-duplicates** the repeated `Unit tests - Python 3.12`. Two views of one ruleset disagree by exactly the entry this ticket is about, and only the API reading is truthful.
  - **The predicted consequence has now happened, with a broken default branch attached.** `327d931f` (#424) merged with a RED `web-ui-frontend-suite` run because that context was not required, so nothing stopped it; `dev` carried the failure until a human repaired it (#426, squashed as `b8587e5`, CI green at that commit). Break and repair were both done by hand — the gate never entered into it.

- **C106**: A leaked session bridge contaminates every later test in the same process — and may never release at all, which would make it a production leak with a test symptom. **Open.**
  - Failure: `tests/test_worker_tray_truth_fields.py:154` (`test_paused_manually_true_only_for_manual_only_pause`) failed on `Unit tests - Python 3.14` with a dozen-plus `server.bridge` instances bound to **foreign** sessions still subscribed and logging `DROPPING WORKER_SPAWNED 'w1'`. It passed on re-run (12m27s), which is what makes this a flake rather than a break — and the flake is the smaller half of the finding.
  - Cause: no teardown. The bridges outlive the test. **Open question, and the reason this is filed as more than a test chore: does a session bridge EVER unsubscribe?** If not, a long-lived backend accumulates one subscriber per session and every event is walked past all of them — a cost that grows with uptime and is invisible in any single test run.
  - Verification: the py3.14 failure was observed by the operator; the unsubscribe question is **UNVERIFIED** (the teardown path has not been read). Line number operator-supplied, not re-read on this branch.

- **C107**: `terminalize` failures are downgraded to a warning, so journaling can silently do nothing and leave a record asserting the job is still running, forever. **Open.**
  - Mechanism: `tools/workspace/worker_thread.py:3590` wraps `registry.terminalize(...)` (`:3578-3589`) in `except Exception as exc:` and logs a warning.
  - Observed: when `3a` added `worker_name`/`session_id`/`instance_id`, two test doubles still carried the old signature and raised `TypeError` — which degraded into "journaling silently does nothing" and surfaced as unrelated assertion failures somewhere else entirely.
  - Why it outlives the tests: a `terminalize` that never ran leaves a journal record asserting the job is still running, permanently, and nothing notices. The warning is emitted into a stream nobody reads after the fact.
  - Proposed: narrow the handler so contract drift propagates — `TypeError` and `AttributeError` should not be caught at all — and count failed terminalizations into the response envelope the agent reads, not only into `job_status`, where nothing looks.
  - Verification: source read by the operator; line numbers operator-supplied, not re-read on this branch.

- **C108**: `ContainerStartTool` records an image it ignores, so the record can misstate what is actually running. **Open.**
  - Observed: a container named `git-runner-alpine`, requested with `alpine/git:latest`, came up as **Debian trixie with no `git` in it**, while the record kept the requested name. A record that misstates its own image is worse than a refusal: a refusal is visible at the call, while a misstatement is read back as fact later.
  - Same class, adjacent: container drift is **detected but not remediable**. The remedy text is "recreate the container to restore the desired isolation", and for `persistent` containers recreation is itself out of the agent's hands, because removal is an operator-grain permission. Detection whose only available action is unavailable is a report nobody can act on.
  - Verification: observed live by the operator; not reproduced here.

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

- **Known and deliberately preserved (Gen 69 housekeeping pass).** The reuse of the live ids
  C35/C36 by the Archive block below, and the double-printing of C74-C77 (a long form, then a
  short form a few lines later), are KNOWN and are preserved on purpose as historical record, per
  the ruling in the Numbering block above ("archived entries are a historical record"). Do not
  delete or renumber them, and do not read an id as a key: quote titles, not ids. The collision is
  documented, not resolved.
  **Correction (Gen 70 restructure):** the C12/C14 bodies and the C74/C75/C77 long forms have
  moved UP, out of this block, because they were live work rather than history (see
  `## Live items recovered from the Archive block`). What remains below is the C35/C36 historical
  pair and the CLOSED C76 pair. The C35/C36 lines still read `**Open.**` because that is what the
  record said when it was written — they are record, not status, and the live C35/C36 entries in
  the Active list are the status. `scripts/gen_handoff.py` de-duplicates by id (first occurrence
  wins), so the copies below never reach the generated handoff; that masking is why the double
  printing was survivable, and it is also why it went unnoticed.

- **C35: Prove the Host Port is sealed.** **Open.** Create a CI test (AST scan or grep) that proves subprocess and docker invocations only occur inside the approved wrappers. No tool may bypass the kill switch.
- **C36: Verify Worker Trust Boundary.** **Open.** Write a test proving that a worker is bound to its session workspace_id and cannot execute tools using permissions of a different session if borrowed.

- **C35: Prove the Host Port is sealed.** **Open.** Create a CI test (AST scan or grep) that proves subprocess and docker invocations only occur inside the approved wrappers. No tool may bypass the kill switch.
- **C36: Verify Worker Trust Boundary.** **Open.** Write a test proving that a worker is bound to its session workspace_id and cannot execute tools using permissions of a different session if borrowed.

- **C76** — **CLOSED (Gen 69 housekeeping pass).** The flag is no longer on for the whole suite. The shipped default is `TRUST_TEST_HARNESS = False` at `web_ui/backend/auth_boundary.py:229`, and `conftest.py:160-183` forces the closed value session-wide through `set_suite_trust_value(False)` / `enable_test_harness_trust(False)`. A test opts in by name with `@pytest.mark.trust_harness`, declared at `conftest.py:215-221` and gated at `conftest.py:276`; at least 9 root-tree test files already carry it. This line was true when it was written — the session-wide grant it describes has since been replaced by per-test opt-in — so it is stale, not mistaken.
- **C76** — **CLOSED (Gen 69).** No longer suite-wide: default `TRUST_TEST_HARNESS = False` (`web_ui/backend/auth_boundary.py:229`), closed session-wide by `conftest.py:160-183`; per-test opt-in via `@pytest.mark.trust_harness` (`conftest.py:215-221`, gated at `conftest.py:276`).
