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
  **Open** — bug, high. `agent/core/llm_client.py:201` `ensure_system_prompt()` deletes, in
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

- **Tool defects noted (C32 class, not scheduled).** `git_read show_file` ignores its `ref`
  argument and silently returns HEAD; `git_read ls_tree` ignores `prefix` and dumps the
  whole tree; `Worker(action="spawn")` rejects a call that omits `worker_name` although
  its documentation says it defaults. The first two are the dangerous ones: reads of
  authority files that quietly answer a different question than the one asked. A fourth
  report — worker briefings arriving stripped — is filed as **C45** in the Generation 64
  section above, and is no longer "under probe and not filed".

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

- **C35: Prove the Host Port is sealed.** **Open.** Create a CI test (AST scan or grep) that proves subprocess and docker invocations only occur inside the approved wrappers. No tool may bypass the kill switch.
- **C36: Verify Worker Trust Boundary.** **Open.** Write a test proving that a worker is bound to its session workspace_id and cannot execute tools using permissions of a different session if borrowed.

- **C35: Prove the Host Port is sealed.** **Open.** Create a CI test (AST scan or grep) that proves subprocess and docker invocations only occur inside the approved wrappers. No tool may bypass the kill switch.
- **C36: Verify Worker Trust Boundary.** **Open.** Write a test proving that a worker is bound to its session workspace_id and cannot execute tools using permissions of a different session if borrowed.
