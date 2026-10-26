# Handoffs
## 2026-08-16 — Operator Handoff
See `personal/task_tracker.md` → section "OPERATOR HANDOFF" (2026-08-16): worker-correctness merged (7c4edd4), durable-files mandate, V3 ORDER 1-14, NOT DOING NOW, NEXT DISPATCH = Infra Engineer Vault/Workspace Retention + Pollution Control (8 items + 7 tests + `source .venv/bin/activate && pytest -q`).

## 2026-08-25 — ## 2026-08-25 — V3 KNITTY-GRITTY / EASY-TO-FORGET SNAPSHOT (...

## 2026-08-25 — V3 KNITTY-GRITTY / EASY-TO-FORGET SNAPSHOT (full capture)

SOURCE: operator handoff list, captured verbatim for KB archival. Query this entry for invariants, lifecycle rules, instance rules, pause semantics, timeout mental model, known residuals, UI future items (worker tray, workspace config truth, landing page, free-use-user-panel), security/resource containment, source-control policy, open questions, roadmap order, cleanup tasks.

### 0. Current state snapshot
```text
Branch: dev
State: ahead of origin/dev by 9 commits? (operator must confirm exact count)
Last completed work:
  - worker-instance UI merged
  - source-control feature-branch commit policy merged
Pending:
  - worker tray truth fields (backend branch not yet created)
  - GUI tray/panel design (design-only)
```
No push. No merge to origin. NOTE (added at archival, from terminal transcript of the source-control merge): immediately after `git merge --no-ff feat/source-control-feature-branch-commits`, git reported "Your branch is ahead of 'origin/dev' by 8 commits." — so the count is ≥8; confirm exact count before any push.

### 1. Architecture and mental model
- Global / Vault → Workspace → Session → Workers
- Workspace = office
- Session = active desk/team
- Main agent = manager
- Workers = interns
- Generic workspace containers = rooms/computers
- Resource containers = secured utility rooms
- Vault = filing cabinet outside office
- Permissions = what is allowed
- Logs = security cameras
- Comm Gate = front door and mail room trust

### 2. Non-negotiable invariants
```text
No Thread.kill.
No global container kill in normal cleanup.
Only exact worker-owned generic containers reclaimed.
Resource containers never stopped/removed by worker cleanup.
No cross-worker container cleanup.
Worker permissions inherit downward.
No user/agent query truncation.
Tool outputs may be bounded only by core-side defense.
WLM feature flag remains OFF by default.
Repo resources are factory seeds only.
Vault files are authoritative.
Never disable the host pre-commit hook.
Never use git commit --no-verify manually.
Do not touch shared git dir or host hook.
```

### 3. Worker lifecycle states
```text
ready
busy
paused
completed
error
stopping
stopped
```
Note: `idle` is not a formal state; it is a loop condition.

### 4. Worker instance rules
- Same-name instances become: `default`, `default#2`, `default#3`
- Bare name ≡ instance #1
- `instance_label` = name for #1, else `<name>#<N>`
- Registry key: `(session_id, worker_name, instance_id)`
- Worker dir: `workers/<instance_label>`

### 5. Pause semantics (now fixed)
- Pause Main: main agent pauses, sync workers pause, async workers continue
- Pause All: main + all workers pause
- Pause is deferred to safe tool-loop boundary
- Pause idle worker = parked alive, not killed
- Sync query to normal paused worker auto-resumes
- Manual-only paused worker returns non-destructive `{"status":"paused",...}`
- Manual-only marker cleared on resume (all paths)
- Stop is terminal; pause is not

### 6. Timeout mental model (still to unify)
Four distinct roles:
1. Worker soft cognitive budget — default 300s, agent-overridable, never kills, restricts to Respond
2. Tool execution timeout — planned default 30s, upper clamp 300s
3. Sync manager wait emergency — planned 600s, cooperative stop, reclaim worker-owned generic containers
4. Hard worker kill — dormant behind WLM flag, off by default
Scattered constants remain: 120s, 300s, 600s. Do not unify yet.

### 7. Known residuals / non-blockers

> CONSOLIDATED → **`personal/parking_lot.md` → Section A** (parking-lot consolidation). Original bullets below kept as history.
- GUI busy state not per worker instance
- Web REST worker endpoints name-based, not instance-aware
- Pause state process-local, not persisted
- Join internally polls registry every 0.25s
- `core.history` plain-list warning
- `server.bridge save_open_session` warning
- DockerCodeRunner timeout unbounded until clamp added
- Session tab switching slow (cause unknown)

### 8. Frontend / UI future items

#### 8.1 Worker tray / flexible panels (next)
- Left tray: instance label, real status, worker name, token count, time since last query, pause/resume/stop
- Right area: multiple open worker panels, reorder, resize, maximize, pin
- Must behave like VS Code panels, not fixed slots
- Single-instance behavior must remain unchanged

#### 8.2 Workspace config truth (later)
- Global / Workspace / Session config resolution chain
- Save as Default should save to workspace defaults by default, with explicit promote option
- New session `+` loads workspace defaults, not ambiguous presets
- Workspace creation tiles become templates only
- Every workspace binds folder outside vault
- Workspace tabs: overview / permissions / resources / credentials / tools / containers / workers / session defaults

#### 8.3 Landing page (later)
- Global Resources, Credentials, System Prompts, Worker Templates
- Active Workspaces / Containers / Running Sessions
- Event-driven, not polling

#### 8.4 Free-use-user-panel (end of V3, vague)
- User can ask agent to show arbitrary content in a dedicated panel
- Examples: show diff, make animation, send HTML
- Renderers possibly HTML/canvas/etc.
- Currently only vague idea, no design yet

### 9. Security and resource containment
- Resource containers: label `thoughtmachine.resource`, prefix `tm-res-`, image `tm-resource-git`
- Generic workspace containers: label `thoughtmachine.worker=<session_id>:<worker_name>`
- Resource containers hidden from generic container tools and never touched by worker cleanup
- Execution ceilings: containerized / host_fallback_visible / none
- Network policy: none / allowlist / open (only on/off currently)
- Credentials: global, bound by reference, never copy secrets into workspace
- `.thoughtmachine/` and `workers.json` must eventually be treated as hostile input

### 10. Source-control policy (now implemented as stopgap)
- Agent may commit only on `feat/*` and `fix/*`
- Requires `git_allow_worktree_commits = true` from operator-owned config
- Requires container execution mode
- Requires explicit `file_path` (no `git add -A`)
- No merge, no push, no `--no-verify`, no host fallback
- Protected branches: `dev`, `main`, `master`, `release/*`, `hotfix/*`
- Operator path unchanged
- Eventually migrate to unified permission system

### 11. Open questions / findings from recent work
1. Session initialization source unclear — new session may not pick up operator config changes automatically.
2. `use_workspace_lifecycle_manager` and `use_container_registry` are false in agent config. WLM false is intentional; container registry false likely intentional but verify.
3. Session file soft cap: 2.4 MB over 2 MB cap, `history_over_capacity` never cleared, no true hard cap, assistant-only retention not built.
4. `time_since_last_query` is heartbeat-derived, not actual query time.
5. `started_at` and `last_query_at` do not exist.
6. `paused_manually` not exposed.
7. Job registry does not read `instance_id` from events.
8. `worker_event_schema.json` stale, no instance fields.
9. No WS control commands; control is HTTP-only.
10. Frontend `workerInfo.started` dead code.
11. Pause/resume JSX block inside events.map never renders (WorkerOutputPanel L786-831).

### 12. Future roadmap order (after current source-control merge)
```text
1. Worker tray backend truth fields (started_at, last_query_at, paused_manually)
2. Worker tray/panel frontend design (GUI, design only)
3. Combine into feat/flexible-worker-panels
4. Session performance investigation
5. Session history size cap fix
6. Workspace config truth / defaults / threat-model controls
7. Timeout unification and interruptible tools
8. Network policy, credentials, MCP
9. Container lifecycle and worker retention
10. Final V3 polish (landing page, free-use-user-panel, cleanup)
```

### 13. Cleanup tasks
- Fix ObservableList plain-list warnings
- Remove stale scripts/repo debris
- Remove dead frontend code identified above
- Delete any leftover feature branches after merge
- Rotate potentially exposed credentials from earlier red-team round
- Remove stale resource container `tm-res-0acf09775137-git` if still present

---
This list is not exhaustive of every line of code, but it captures the easy-to-forget decisions, invariants, known gaps, and future ideas.

## 2026-08-25 — ## 2026-08-25 — SOURCE-CONTROL + V3 ROADMAP STATE (operator ...

## 2026-08-25 — SOURCE-CONTROL + V3 ROADMAP STATE (operator system-state doc, filed verbatim)

ARCHIVAL NOTE (added by agent 2026-08-25, after verifying against repo): HEAD verified = 3052d9e "Merge feat/worker-tray-truth-fields into dev"; the three merges listed below are all in dev history. STALENESS FLAG: the "Working tree: clean except known non-source KB files" line is OUTDATED — at archival time there ARE uncommitted frontend changes (M web_ui/frontend/src/App.jsx, M WorkerManagementPanel.jsx, M WorkerOutputPanel.jsx; ?? new __tests__/WorkerManagementPanel.test.jsx), i.e. the worker tray/flexible panels GUI has moved past "design stage (no code yet)" into code. Supersedes the "worker tray truth fields (backend branch not yet created)" line from the 2026-08-25 V3 KNITTY-GRITTY snapshot (same date, earlier entry).

---

# ThoughtMachine System State — Source-Control and V3 Roadmap

## 1. Source-Control Work — Completed

### Problem
Previously the coding agent could not perform any Git commits at all. All commits had to be done manually by the operator. This led to large, infrequent commits with poor history granularity.

### Solution
Implemented a narrow stopgap policy that allows agent commits only on feature branches under strict conditions.

### Mechanism
- Detection: `tools/git_info_tool.py` detects operator-managed worktrees via `.git` file containing `gitdir:`.
- Blocking: `_git_commit()` returned an error for any commit attempt in such worktrees.
- New allow path: `_feature_branch_agent_commit_allowed()` permits a commit only when **all** of the following are true:
  - `agent_config["git_allow_worktree_commits"]` is `True`
  - current branch matches `^(feat|fix)/.+$`
  - execution is in container mode (no host fallback)
  - explicit `file_path` is provided (no `git add -A`)
- Protected branches (`dev`, `main`, `master`, `release/*`, `hotfix/*`) remain blocked.
- Agent still cannot merge, push, use `--no-verify`, or fall back to host Git.

### Flag Wiring
- Added `git_allow_worktree_commits` to:
  - `agent/config/models.py` (`AgentConfig`, default `False`)
  - `agent/config/session_config.py`
  - `agent/core/tool_executor.py` whitelist
- The flag is read from operator-owned config outside the workspace (`~/.thoughtmachine/agent_config.json`). The agent cannot set it.

### Tests
13 focused tests in `tests/test_git_info_tool.py` cover allow/deny paths, branch validation, container mode, explicit path, merge/push rejection, and host fallback.

### Live Acceptance
Passed: agent committed on a throwaway `feat/test-commit-policy` branch with explicit path; merge/push/dev commits denied; pre-commit hook ran.

### Status
Merged into `dev`. This is a stopgap, not the final unified permission system.

---

## 2. Current System State

- **Branch:** `dev`
- **HEAD:** `3052d9e` (merge of worker tray truth fields)
- **Full test suite:** `1971 passed, 10 skipped, 0 failures`
- **Working tree:** clean except known non-source KB files and `working_docs/`  ← STALE at archival (see note above; frontend GUI edits in progress)
- **Recent merges:**
  - `feat/worker-instance-ui` — instance-aware worker UI and pause semantics
  - `feat/source-control-feature-branch-commits` — agent commit policy
  - `feat/worker-tray-truth-fields` — `started_at`, `last_query_at`, `paused_manually`

---

## 3. What Intends to Change — Roadmap

### Immediate (in progress)
- **Worker tray / flexible panels** — GUI design stage (no code yet)  ← now in CODE (see note above)
  - Left tray listing live worker instances
  - Right area with multiple selectable worker panels
  - Support reorder, resize, maximize, pin
  - Event filtering per instance

### Short-term stabilization
- **Timeout unification** — collapse scattered 120s/300s/600s into four roles:
  1. Worker soft cognitive budget (300s)
  2. Tool execution timeout (30s default, 300s clamp)
  3. Sync manager wait emergency (600s, cooperative stop)
  4. Hard worker kill (dormant, off by default)
- **Stop All button** — expose cooperative stop of all workers clearly in UI
- **Worker lifecycle / GC** — prevent uncontrolled worker spawning; add clean-spawn vs resume-spawn; retire stopped workers
- **Session pruning fix** — enforce true hard cap, assistant-only retention, clear `history_over_capacity`

### Mid-term — Workspace config truth
- Define global/workspace/session config resolution chain
- Fix `Save as Default` and new session loading
- Fix session initialization source of truth (new sessions should inherit global defaults)
- Add workspace permission ceilings for resources, network, credentials, execution
- Restructure workspace config pages/tabs

### Long-term — Expansion
- Global landing page with active workspaces/containers/sessions
- Global resources, credentials, system prompt library, worker templates
- MCP servers and Comm Gate (internet allowlist/proxy)
- Hardware/non-containerizable resource handling
- Session performance and frontend polish
- Free-use-user-panel (arbitrary rendering area)

---

## 4. Key Concepts and Invariants

- **Layers:** Global/Vault → Workspace → Session → Workers
- **Workers** inherit permission ceilings from above.
- **Containers:** generic workspace containers vs resource containers (secured).
- **No Thread.kill.** Stop is cooperative.
- **No cross-worker container cleanup.**
- **Resource containers never touched by worker cleanup.**
- **Worker permissions inherit downward.**
- **No user/agent query truncation.**
- **WLM feature flag remains OFF by default.**
- **No agent merge/push; no `--no-verify`; no host fallback for agent commits.**

---

## 5. Known Residuals and Open Items

> CONSOLIDATED → **`personal/parking_lot.md` → Section A** (parking-lot consolidation). Original bullets below kept as history.

- `core.history` plain-list warning
- `server.bridge save_open_session` warning
- Session tab slowness (cause not yet profiled)
- Session file soft cap (2 MB) issues
- Session rename label UI bug
- `time_since_last_query` currently heartbeat-derived, not true query time
- Job registry not instance-aware
- `worker_event_schema.json` stale
- No WS control commands; control is HTTP-only
- Dead frontend code (`workerInfo.started`, unreachable pause JSX)
- Potentially exposed credentials from earlier red-team round
- Stale resource container `tm-res-0acf09775137-git` (if still present)

## 2026-09-28 — [2026-09-29] TASK_TRACKER ARCHIVE PASS ("C" bucket) — STAGED...

[2026-09-29] TASK_TRACKER ARCHIVE PASS ("C" bucket) — STAGED, INDEPENDENTLY VERIFIED, AWAITING HOST-SIDE APPLY + ONE OPERATOR RULING. Source `.thoughtmachine/knowledge/personal/task_tracker.md` (572643 B / 1758 lines, sha 7b6dd6ec…, UNTOUCHED). Cutoff 2026-08-19. Staged in `.thoughtmachine/working_docs/`: `archive_tasks_c.md` (147717 B / 410 lines, sha 16aadbc3…), `task_tracker_new.md` (426960 B / 1363 lines, sha 45efe349…), `task_tracker_archive_result.md` (468 lines), `task_tracker_archive_brief.md` (58 lines — a RECONSTRUCTION, not verbatim: the worker lost the dispatch text to mid-run context compaction; harness defect, disclosed in-file), plus the verifier's `task_tracker_archive_VERIFY_result.md`. COUNTS (two granularities — state the unit!): ENTRIES 252 = META 4 + MOVE 54 + KEEP 193 + OTHER 1; HEADINGS 313 = META 4 + MOVE 68 + KEEP 240 + OTHER 1. The sentinel `54 moved, 194 kept` is an ENTRY count (194 = 193 keep + 1 other; meta unmentioned) — internally consistent, unit ambiguous. Byte arithmetic: 572643 − 426960 = 145683 removed; 426960 + 147717 = 574677 (+2034 = new header + digest + Archived list). VERIFIER VERDICT: apply GO, `VERIFY COMPLETE - 5 of 6 checks confirmed, apply: GO, anomalies: 2` (192.5 s / 9 calls). Confirmed: exact heading partition (nothing missing or duplicated across the three sets; only "extra" is the crafted digest heading); integrity identity `lines[0]+"\n"+lines[1]+"\n"+concat(252 spans) == raw` TRUE (568017 B exact, no loss/duplication); 68/68 moved headings verbatim in source order; 53/54 moved spans byte-exact; Current Status block byte-identical (sha 4174fa92… reproduced); intro change is exactly one insertion ` / archive_tasks_c.md`; `## Archived` = a/b/c; digest = 8 bullets; no file under `knowledge/` has an mtime inside the staging window ⇒ source untouched. TWO ANOMALIES: **A1 (needs ruling)** source L1189 is a merged/malformed heading `## 2026-09-07 — ## 2026-08-16 — PHASE 4 (canonical-resource display) COMMITTED 7507ba8 …` (blank-line-joined with L1191's `## 2026-08-16 — …`): content-dated 2026-08-16 ⇒ MOVED; a strict FIRST-TOKEN date reading would KEEP it (false-move). Disclosed in digest bullet 8 + brief caveat. Ruling needed: content-date vs first-token semantics. If strict: restore that one span (source L1189–1198) to the active tracker. **A2 (cosmetic)** one trailing blank line missing at archive EOF (whitespace only). Non-anomalies noted: archive/digest title range "→ 2026-08-19" is narrower than the true moved range (→ 2026-09-07, because of A1); `_plan_tmp.json` removal (claimed by the worker) has no surviving evidence either way. NEXT (operator/host, one-way door): copy `archive_tasks_c.md` → `.thoughtmachine/knowledge/archive/`, replace `task_tracker.md` with `task_tracker_new.md`; the ORCHESTRATOR then verifies post-move via the KB tool (line/byte/hash + search). REUSABLE LESSON: the partition-integrity assert `header + intro + concat(entry spans) == raw` plus a heading-set partition check is a cheap, strong anti-loss proof for verbatim archive passes — and always state the count UNIT (entries vs headings) or the sentinel will look like it doesn't add up.

## 2026-09-28 — [2026-09-29] ARCHIVE-PASS PRE-APPLY PRE-CLEAR (prep for the ...

[2026-09-29] ARCHIVE-PASS PRE-APPLY PRE-CLEAR (prep for the one-way door). Re-verified the staged archive pass against the CURRENT live source: live `.thoughtmachine/knowledge/personal/task_tracker.md` sha256 = 7b6dd6ecad92999d2d0b1f172e1e1f061b542817707abd05bb95bf057c48801c — IDENTICAL to the staging-snapshot hash ⇒ ZERO drift since staging. Authoritative `wc`: source 572643 B / 1758 lines (sha 7b6dd6ec…); staged new 426960 B / 1363 lines (sha 45efe349…); staged archive 147717 B / 410 lines (sha 16aadbc3…). CAUTION FOR FUTURE ME: the KB tool's own readout for task_tracker ("568271 bytes / ~142067 tokens / 1759 lines") uses a DIFFERENT counting metric (character/rendering-normalised) — do NOT read a delta there as file drift; only `wc -c`/`wc -l`/sha256 count. NEW INDEPENDENT EVIDENCE (beyond the earlier verifier): the dated-heading partition is EXACT — lines matching `^## 20` are 267 live = 206 (new) + 61 (archive), zero loss or overlap; `^## ` totals 313 live = 246 new (240 keep + 4 meta + 1 other + 1 crafted digest heading) + 68 archive (move). Byte identity: 426960 + 147717 = 574677 = 572643 + 2034 (the +2034 = new header + digest + Archived list). The post-cutoff CANDIDATE TICKET text is present in BOTH the live source and the staged new file (2 occurrences each) and ABSENT from the archive (0) ⇒ applying `task_tracker_new.md` LOSES NOTHING. Apply is therefore PRE-CLEARED as content-safe. STILL OPEN before/at apply: the A1 anomaly ruling — source L1189 is a merged/malformed heading `## 2026-09-07 — ## 2026-08-16 — PHASE 4 (canonical-resource display) COMMITTED 7507ba8 …`, content-dated 2026-08-16 ⇒ staged as MOVED; a strict FIRST-TOKEN reading (2026-09-07 > cutoff 2026-08-19) would have KEPT it ⇒ if the operator rules strict, restore source L1189–1198 into the active tracker as part of the apply. A2 (cosmetic): one trailing blank line missing at archive EOF. Apply steps (operator/host, one-way door): copy `.thoughtmachine/working_docs/archive_tasks_c.md` into the knowledge archive dir (sibling of personal/ and project/), then replace `.thoughtmachine/knowledge/personal/task_tracker.md` with `.thoughtmachine/working_docs/task_tracker_new.md`; afterwards I verify post-move via the KB tool (line/byte/hash + search) and confirm the tracker drops from ~142 k tokens to ~106 k. NO re-append of the candidate ticket needed post-apply (it is inside the staged new file). NOTE: the apply is mechanically executable IN-CONTAINER by a worker (the workspace holds `.thoughtmachine/knowledge/`, and worker containers mount the repo read-write) — offered to the operator, who reserved it as a one-way door.

## 2026-09-29 — ### 2026-09-29 — TASK_TRACKER ARCHIVE PASS: **APPLIED** (fol...

### 2026-09-29 — TASK_TRACKER ARCHIVE PASS: **APPLIED** (follow-up to the PRE-APPLY PRE-CLEAR entry above)
STATUS: DONE + host-verified. The pre-clear entry above is superseded: the apply has RUN.
RULINGS EXECUTED: A1 = CONTENT-DATE (malformed merged heading MOVED with its content-date; NOT restored into the active tracker; a disclosed LOCAL call within the strand, Main may veto). A2 = ACCEPTED (cosmetic trailing blank line). APPLY = APPROVED in-container, conditional on a byte-identical recovery copy existing first.
PRE-CONDITION (operator procedure, satisfied): a staleness re-check pass re-measured the live source at sha256 `7b6dd6ecad92999d2d0b1f172e1e1f061b542817707abd05bb95bf057c48801c` — byte-identical to the staging snapshot ⇒ zero drift. Partition re-confirmed independently: `^## 20` 267 = 206 + 61; `^## ` 313 = 246 + 68; bytes 426,960 + 147,717 = 574,677 = 572,643 + 2,034. The post-cutoff CANDIDATE TICKET text was present in BOTH live source and staged new file and ABSENT from the archive ⇒ nothing lost.
APPLY (one dispatch, live instance 1, 65.2 s / 18 calls): recoverability scan (os.walk over `.thoughtmachine/`) found 0 byte-identical copies ⇒ recovery copy created FIRST at `.thoughtmachine/working_docs/task_tracker_pre_archive_backup.md` (sha `7b6dd6ec…c48801c`, 572,643 B / 1,758 lines). Then `cp .thoughtmachine/working_docs/archive_tasks_c.md .thoughtmachine/knowledge/archive/archive_tasks_c.md` and `cp .thoughtmachine/working_docs/task_tracker_new.md .thoughtmachine/knowledge/personal/task_tracker.md` (both exit 0).
POST-STATE: tracker = sha `45efe3495ede33ce49a7aa1831106f75298ede21706ca09099f918c1c3180d7d`, 426,960 B / 1,363 lines. archive = sha `16aadbc3e0e79c564d0a91b95da5687c3bb31df4280c4f235542f3f70ffb90a8`, 147,717 B / 410 lines. Both staged sources in working_docs UNCHANGED; recovery copy UNCHANGED. Greps: live tracker `boot-sweep-registry-empty-not-fail-closed`=1, `CANDIDATE TICKET`=2; archive both=0.
HOST-SIDE PROOF (the container write reached the host file the KB tool reads): `task_tracker` now reads ~105,912 tokens / 1,364 lines, down from ~142,067 tokens ⇒ **−36,155 tokens saved on every read**. `## Digest of Archived Work (2026-08-11 → 2026-08-19)` at lines 17–26, `## Archived` at 15–16, and the FINAL section is `## 2026-09-28 — [2026-09-29] CANDIDATE TICKET` (lines 1361–1364) ⇒ candidate ticket SURVIVED. (KB-tool line/byte readout ≠ wc — known metric artifact, NOT drift.)
PLACEMENT: `.thoughtmachine/knowledge/archive/` was the pre-existing tasks-archive dir (already held archive_tasks_a.md, archive_tasks_b.md, archive_bugs_a/b.md, archive_lessons.md) ⇒ archive_tasks_c.md sits with its siblings, matching the tracker's bare-filename `## Archived` reference and the one-insertion intro edit. `knowledge/archive/` is NOT a KB domain dir, so c will not show in KB `mode=list` — precedent-consistent.
CLEANUP/OPEN: (1) recovery copy intentionally retained (undo path); (2) host-side `git status` re-check re-run after the apply — tree clean apart from the 3 known out-of-strand paths; (3) the container runner left helper scripts under `/workspace/tmp/script_*.sh` (gitignored via `.gitignore:29` `tmp/`) — a different component from the DockerCodeRunner path fixed in B1, same litter class; flagged for operator triage, no action taken.
NEXT: queue is archive-pass DONE → `bug/boot-sweep-registry-empty-not-fail-closed` (TICKET, named-held, security-touching, TOP) → `bug/bridge-config-unknown-error-swallow` (HELD, do not start) → design passes.

## 2026-09-29 — ### 2026-09-29 — STRAND: `bug/boot-sweep-registry-empty-not-...

### 2026-09-29 — STRAND: `bug/boot-sweep-registry-empty-not-fail-closed` — IMPLEMENTED + INDEPENDENTLY VERIFIED + COMMITTED (awaiting operator push/PR)
STATUS: code complete on branch `bug/boot-sweep-registry-empty-not-fail-closed`, commit **`f029fba`** (2 files, +238/−2), parent `71d7eff` (= `dev` head). **NOT pushed, NOT merged — `gh`/push are the operator's.** `dev` untouched.
SHAPE CHOSEN: **A — boot guard (narrowest).** Justification: mirrors the already-proven periodic guard, leaves the destructed callee permissive for other callers, and couples nothing.
THE SMOKING GUN: the merged periodic fix (`71d7eff`/PR #32) shipped a docstring in `_periodic_registry_has_workspaces` asserting the *startup* pass **could trust a non-empty registry** because auto-registration runs immediately before it. That assumption was wrong — auto-registration is best-effort. The bug was documented as intended behaviour. The fix rewrites that docstring.
PREMISES: **7/7 CONFIRMED** by the independent verifier, with actual symbols/sites: `register_by_root` @ server.py:691 (inside try/except that logs and continues, P2); `_sweep_orphan_resource_containers` def @337; boot call DIRECT @822 (not via `_run_container_sweeps`, P4); callee = `sweep_stale_resource_containers` @ `infra/resource_container_manager.py:2014`, `registered` built @2032, `if ws_id is None or str(ws_id) not in registered: container.remove(force=True)` @2057–2064 ⇒ empty registry force-removes EVERY resource container (P5); `_periodic_registry_has_workspaces` @478 wired only to @547 (P6); ordering `register_by_root` @691 precedes sweep @821 (P7).
CALLER ENUMERATION (Main's SIXTH SURFACE — discharged, not assumed): call sites = def @337, @547 inside `_run_container_sweeps` (guarded by the periodic guard), @822 boot (now guarded). `_run_container_sweeps` is reachable ONLY via `_periodic_container_sweep_loop` @572, which does `await asyncio.sleep(interval_s)` BEFORE `to_thread(...)`, task started @844 ⇒ **not synchronous at boot** ⇒ **no unguarded boot-time path remains.** Scope-of-fix = one writer was correct *this* time, and now proven rather than assumed.
FIX: new `_startup_registry_has_workspaces()` (fail-closed, never raises, WARNING on fault) + boot call gated on it + WARNING when skipped. New test file `tests/test_boot_sweep_registry_fail_closed.py` (4 tests) drives the REAL boot path `async with server.lifespan(server.app)` — empty registry ⇒ sweep not called; faulting registry ⇒ sweep not called; observability; control (non-empty still sweeps).
DOUBLE-LOCK (independent, adversarial): author mutation `6d25a7a5fcd0d582ac7f84080dac24b3917f68006c132082bdd414ebd3179b2f`, RED = 3 failed/1 passed. Verifier **independently reproduced the SAME mutated sha256 byte-identically** (strong provenance) and restored byte-identical. Full suite from the RESTORED tree: **3908 passed / 62 skipped / 10 subtests / exit 0** (verifier 235.6 s; author 270.7 s).
HASHES: `web_ui/backend/server.py` = `f11acafc84d5b435a56f75cb1d33c542e4c4ae45acd8f1dc3f9e4e3941c69ba5`; `tests/test_boot_sweep_registry_fail_closed.py` = `7c84c8b4007564dd180713ea1e365d7cff8897148194fef3d11d167735380502`.
VERDICT: **`VERIFY COMPLETE — apply, anomalies: 3`** (all non-blocking). A1 helper duplication (DRY: new helper is byte-identical to the periodic guard except log text — a candidate follow-up); A2 WARNING on a possibly-benign empty boot (once per boot, not spam); A3 pre-existing stale-but-NON-EMPTY registry wipe not worsened, out of scope, relates to held `bug/orphan-sweep-races-container-reuse-rebind`.
ENV: `.venv/bin/python` is a **BROKEN symlink** in the container ⇒ `python3` (3.11.16) / pytest 9.1.1. Clear `/tmp/pytest-of-agent` before the full suite.
OWED: operator push + PR (branch `bug/boot-sweep-registry-empty-not-fail-closed`), then the standard merge gate.

## 2026-09-29 — ### 2026-09-29 — LITTER READ `bug/worker-exec-scratch-litter...

### 2026-09-29 — LITTER READ `bug/worker-exec-scratch-litter-workspace-tmp` — READ COMPLETE, SUSPECT FALSIFIED, PROPOSAL RE-SCOPED (awaiting Main's ruling)
PARAMS: read-only dispatch, `purpose=researching`, `style=precise`, `timeout_seconds=600`, `force=true` (fresh instance deliberately — the claim's author was a *different* instance and I did not want its belief inherited). 292.3 s / 19 calls, final/high, no timeout.
**LITTER IS REAL.** `/workspace/tmp/` holds **exactly 21** files, all `script_<8hex>.sh`, `-rwxr-xr-x agent:agent`, mtimes **21:54 → 22:22**, dir mtime 22:22:12. `find /workspace -name 'script_*.sh'` → 21, all under `tmp/`. Content confirms the origin family: `script_1b5273fd.sh` = a venv probe; `script_7ede37ed.sh` (5010 B) = a `script=` body beginning `cat > /tmp/v.py <<'PYEOF'` (the task_tracker archive job). So the emitters are **DockerCodeRunner `script=` payloads**.
**SUSPECT FALSIFIED — `ContainerExecTool` IS NOT A WRITER.** The symbol is real (`tools/container_control.py:259`, base @94, `execute()` @313) but `execute()` only calls `manager.exec(self.container_id, command=…, timeout=…, workdir=…, environment=…)`; a targeted grep of that file for `write|open(|script_|.sh|tempfile|mkstemp|/tmp/|exec_run|chmod` hits **only docstring words and the `"filesystem:write"` permission string**. Exec delegates to `ContainerManager.exec` @ `infra/container_manager.py:1912` → `container.exec_run` (raw argv, **no temp file**). Two independent lines of evidence: the code path, and the *content* of the litter matching DockerCodeRunner's heredoc shape. **ContainerExecTool can never have produced these files.** The name came from a worker narrative and was never evidenced.
**THERE IS ONE IN-REPO PRODUCER, NOT TWO.** `tools/docker_code_runner.py: DockerCodeRunner._prepare_script_command` @232–264 (`script_dir` @251, `script_{uuid4().hex[:8]}.sh` @252, mkdir -p + heredoc + chmod +x @259–263), invoked only when `self.script is not None` (the `command=` path writes nothing; there is no cleanup). B1 (`7fb990b`) already retargeted it to `/home/agent/tmp`. Repo-wide, `/workspace/tmp` appears only in the new test's comments and in docs; **no source writer targets it any more**.
**THE DECISIVE TENSION.** `docker_code_runner.py` mtime **15:52:34** (the fix); B1's verification 17:40; litter mtimes **21:54–22:22** — **after** both. Count 327 (pre-fix) → 21, survivors dated post-fix. And **`/home/agent/tmp` does not exist** (nothing has been written to the fix's target).
**RULING.** Litter real; suspect falsified; the single in-repo producer IS fixed. Yet fresh litter still lands on the bind ⇒ **the live emitter is not running the fixed repo source** ⇒ either (a) a **stale pre-fix in-memory module** of the same symbol in a long-lived host process, or (b) a **copy of the code outside the repo** (harness/platform layer). Sentinel: `LITTER READ COMPLETE — producers=1, b1_incomplete=yes, tree=unchanged`. **Read the sentinel precisely: B1 is incomplete IN EFFECT, not IN SOURCE.** The source fix is correct and merged; the effect is not achieved.
**CRITICAL GAP IN THE EVIDENCE (do not overclaim).** The worker's search was **container-scoped**; it confirmed "no second copy of `docker_code_runner.py` anywhere on the container FS". The host FS was **out of its reach**. So "external" is an **inference**, not a proof — the emitter is *unidentified*. Identifying it needs host-side access (`host_bash`, currently `ask`), i.e. it is **not reachable from my layer**.
**CLOCK ODDITY (unresolved, flagged not reasoned about).** The container's "now" was 22:48; the operator reported a purge to 0 of 330 files "at 23:00". 21 files dated 21:54–22:22 nonetheless exist. Either the purge was bounded/did not remove these, litter regrew after it, or there is clock skew between the operator's report and the container. Not resolved; recorded so it is not silently assumed.
**PROPOSAL (do NOT act without Main's ruling).** Re-scope the candidate: **it is a harness/platform ticket, not a repo bug.** Cheap decisive experiment available: count `/workspace/tmp/script_*.sh`, run ONE `script=`-style dispatch, recount — growth ⇒ the emitter writes to the bind (unfixed path confirmed from the outside). Plus record the falsification so nobody chases `ContainerExecTool` again.
TREE: unchanged; only the brief + result files were written. `dev` = `273ef45` (boot-sweep merged, PR #33).

## 2026-10-21 — GEN 47 → GEN 48 HANDOFF

**Read this first, then `project/status.md`, then `project/brain_pour_2026-10-21.md`.**
Note the gap: the previous entry in this file is dated **2026-09-29**. Gen 46 wrote
no handoff, so nothing here inherits from a Gen 46 entry — the three weeks between
are reconstructed from `status.md` and the git log. Treat continuity as *rebuilt*,
not *handed over*.

### Where the tree actually is

`dev` = `origin/dev` = **`d60ffc92`** (#162, event_log scoped + redacted).
Checked-out branch: **`fix/ws-dispatch-ownership` @ `51a87058`**, 11 commits, pushed,
**not merged**.

**Verify this yourself before you write anything about "dev HEAD".** During the Gen 47
write pass the working note carried `dev HEAD = 51a87058` for hours. It was wrong:
that is the *branch* tip, and `git log` shows it first because the branch is checked
out. `git rev-parse dev` is the only thing that answers the question.

### What landed (Gen 47)

Nineteen PRs, #143 → #162 (nothing on `dev` carries #160):

- **Admission / envelope:** #143 class-aware container cap; #151 aggregate envelope
  design pass; #152 default `mem_limit` 1 GB → 512 MB.
- **Boundary:** #153 refuse non-loopback bind; #159 confine client-supplied paths,
  origins, approvals, deep links; #161 validate registry-resolved workspace paths
  before they become mount sources; #157 store layer honours `workspace_id` (six fixes).
- **Scope leaks:** #147 `running_workers` to the calling session; #162 `event_log`
  query scoped + message bodies redacted.
- **KB / vault:** #149 close the global-KB write bypass; #150 global KB read-only for
  agents (F24); #154 re-sync factory-owned vault seeds by *content*, not version.
- **Paper:** #144–#146, #155, #156, #158 — ops protocol, pen-test findings (11 filed
  F11–F20/F22), isolation-audit corpus, two refinement passes.

Then the unmerged branch, which is the actual work of the generation: **ownership is
a property of the socket.** `a48d60bb` `c94e665b` `0821e4ec` `b7a852e9` `ce4bb1ca`
`0f7dd27c` `d55a96d5` `a4c3296f` plus three `test(ws)` commits.

### What this seat learned — five relay errors

Written down because the next seat will make them in the same order.

1. **The log's first line is not `dev`.** (above). Rule: `rev-parse dev`, every time,
   before writing a status block.
2. **Two functions, one name.** The ticket
   `ticket-hygiene-container-create-admission-branch-dead-in-production.md` blamed
   `infra/container_create.py:351 create_hardened_container` for declining admission.
   Two functions carry that name: the **wrapper** `infra/container_registry.py:231`
   *owns* the gate (admission at `:303`), the other is the primitive
   (`admission=None, record=True`), and the real hot-path call is `registry.py:714`.
   Conclusion survived; evidence didn't. Rule: pin `file:line`, never a bare symbol —
   especially when the symbol exists twice.
3. **A bare shell is not the system.** `.githooks/pre-commit` was deleted in
   `85035605` because `core.hooksPath` read as UNSET ⇒ "dormant". The container path
   sets it. One shell's default is not a fact about the machine.
4. **Weight class of a one-way door.** I put `git rm --cached` on ten tracked KB blobs
   in front of the operator as one of two clean ways to make a sentence in
   `KB_POLICY.md` true. That rewrites what every future reader inherits, to fix a
   *documentation* inconsistency. Dropped 2026-10-21. Rule: if a hygiene finding's
   cheapest fix alters history, the finding is not the priority.
5. **"Stale" without a date is an opinion.** `handoffs.md` (2026-09-29),
   `open_items_register.md` (2026-09-21 WIP), `status.md` (Gen 46). The useful
   sentence was never "this is stale" — it was *three generations happened and this
   file does not know about any of them.* A status file that old is not documentation,
   it is a **coin flip with a name on it**.

### What this seat got right — three patterns to keep

1. **Attack the second command.** The three `test(ws)` commits exist because the first
   fixes were written against the one command that reported the bug. Each test then
   attacked a *different* command to see whether the rule held there too — that is how
   `apply_config` was found. Generalise: after any fix, name the sibling entry points
   that share the rule, and test one of them.
2. **Provable beats probable.** "The branch is dead" is arguable. "`AdmissionContext`
   has zero production construction sites — only three test call sites" is *countable*.
   Use counts when the claim is load-bearing.
3. **Check the pointer before calling it captured.** Every CAPTURED row in the
   brain-pour was grepped to a line number first. And the zero-hit greps (`F5`,
   `stove`, banner colour across `knowledge/`) are what turned "probably missing" into
   **"missing"** — absence of evidence made into a claim only because the search was
   defined.

### What the next seat inherits

**Blocked, do not start:** `bug/bridge-config-unknown-error-swallow` (held);
Credential **W2** (blocked behind the crosstalk audit — the ordering is the
operator's ruling, not a preference).

**Blocked on the operator:** the merge of `fix/ws-dispatch-ownership` (eleven commits
built, tested, pushed — it needs the gate, not more code); operator push/PR for
anything else this seat leaves on a branch.

**Open and unowned:** the ~22 thoughts in `brain_pour_2026-10-21.md` §"Thoughts with
no home yet". Highest-value three, in the seat's opinion: **answer "what is the
hostbash bug?"** (a question nobody has answered in writing), the **macOS
clone→install→start smoke test**, and the **lifecycle lens as doctrine** — the last of
which is one paragraph in `working_agreements.md` and would change how every future
component gets reviewed.

**Standing disciplines (unchanged, and they still bite):**
- Never grep `.thoughtmachine` unscoped — `working_docs/*.json` is multi-MB. Always
  `file_pattern: "*.md"` or a narrow directory.
- Never batch `SummarizeTool` with `Respond`. This seat hit the context limit mid-write
  pass and had to summarize with the source material inlined to keep it.
- `personal/*` is **append-only**. Correct by appending a dated correction, never by
  deleting the wrong sentence. The task_tracker "Hook TRUTH ⇒ DORMANT" entry is the
  worked example.
- Do not commit KB files. Ten tracked blobs are pre-existing drift; `git rm --cached`
  is **dropped** as a resolution (`docs/KB_POLICY.md`).

### What is broken

Everything not already ticketed. That is `project/brain_pour_2026-10-21.md`, and it is
the single place to look — one file, per-thought verdicts, pointers only where a
pointer was verified. If you find a thought in the pour that you can close, close it
there too, so the next seat's reading gets shorter instead of longer.


## 2026-10-23 — ## W2 credential grain — COMPLETE, LOCAL ONLY (push pending ...

## W2 credential grain — COMPLETE, LOCAL ONLY (push pending on host)

Branch `feat/w2-credential-resource`, base `b6382aeb`, **4 commits, suite green: 5146 passed / 64 skipped / 0 failed**. All three rulings received and approved.

Commits: `4a5d4acf` (RED-first contract test, 8 tests) → `ca24f8f8` (eighth grain + 3 audit events, 10 files) → `f45f6127` (54 stale literals, 18 files) → `64607d99` (drift tolerant read + trio).

**NOT pushed and no PR exists yet.** The agent has no bash/gh — the push and `gh pr create` block must be run on the host terminal. Until then there is no PR URL and no CI result. Do not describe W2 as merged, pushed, or CI-green.

**Test arithmetic reconciles exactly:** 5082+61+64 = 5207 before, 5146+0+64 = 5210 after; delta is exactly the 3 new tests, skips unchanged at 64 ⇒ nothing deleted, skipped or xfailed to reach green.

### Carry-forward caveat — credential use is OBSERVED, not AUDITED
`CREDENTIAL_USE/ROTATED/DELETED` emit and are tested, but are **not** in `event_class_map`, have **no subscriber**, and are **not** routed to the tenant event log. Do not soften this to "audited" in docs, sysprompt, or PR prose until a subscriber exists — that would re-introduce the `declared-but-not-enforced` class at the highest-stakes surface. Filed: `hygiene/credential-events-unrouted`.

### Open tickets filed this wave
- `hygiene/credential-events-unrouted` — accepted as-is; routing is the event-log wave's concern. Open: global-scope in a per-workspace log? per-injection volume? resolved-vs-succeeded distinction?
- `bug/ci-workspace-ceiling-inert` — pre-existing; `ci` absent from `_WORKSPACE_RESOURCE_MAP`, miss path warns+continues ⇒ fail-open ceiling.
- `hygiene/credential-created-not-audited` — create path returns before the emit; needs `CREDENTIAL_CREATED`, not a reused `ROTATED`.
- `hygiene/test-name-catalog-count-drift` — stale "seven-key"/"nine resources" prose and one lying test name; separate wave because test node IDs are user-visible.

### Disclosed security-signal change (approved)
`drift._permission_drift_finding` now compares `set(recorded) & set(effective)`. Absent key = not recorded, never banned, never padded. Bounded blind spot: a legacy record cannot report drift on a grain it predates until re-saved; self-heal is pinned by test. Also: a record with `permissions={}` (empty, not None) is now quiet rather than alarming.

### Not verified
No JS tests run (sandbox has no `node_modules`) — the `permissionVocab.js` edit is inspection-only. No linter/typechecker.

## 2026-10-23 — W2 credential closeout — CORRECTION + state (2026-10-23, for...

W2 credential closeout — CORRECTION + state (2026-10-23, for next session). Branch `feat/w2-credential-resource` @ `935f0ad0` (base `b6382aeb`), PR #179 open, operator-pushed, CI RED on 1 test. Commits: 4a5d4acf (RED-first contract test) → ca24f8f8 (grain + 3 audit events) → f45f6127 (literal sweep) → 64607d99 (drift tolerant read) → 935f0ad0 (the CI-fix literal, NOT pushed at time of writing). Root cause of the red: the worker's green run used `pytest tests` positionally, overriding testpaths, so `web_ui/backend/tests` (153 nodes) was never collected; `CATALOG_DEFAULTS` in `web_ui/backend/tests/test_permission_routes.py` was a 7-grain literal missing `credential`. Fixed to 8 keys + corrected the stale comment; `CODING_PRESET` in the same file deliberately left at 6 keys (partial overlay, never referenced, not a catalog duplicate). HONEST COUNT CORRECTION: "61 stale literals" was the failing-test count for the `tests/` root only, never repo-wide; a repo-wide AST scan found 46 dict/set/list/tuple literals naming ≥5 of 8 grains missing `credential` (45 after the fix) but only TWO ever named the full 7-grain set (CATALOG_DEFAULTS, now fixed; WIDE_PERMS in tests/test_container_record_drift_display_truth.py:30, deliberate legacy). Do NOT blanket-pad the other 45 — they are green partial grant maps and padding masks drift. Numbers: web_ui/backend/tests alone 148 passed/5 xfailed (matched CI exactly pre-fix); bare pytest both roots 5294 passed/64 skipped/5 xfailed/0 failed in 255s; 5146+148=5294 closes the gap exactly. TODO for operator: `gh pr edit 179 --body ...` (corrected premise) then push 935f0ad0 → CI → merge. I cannot run gh/push/CI.

## 2026-10-23 — DISPATCH SHAPE — long run (operator directive 2026-10-23; ha...

DISPATCH SHAPE — long run (operator directive 2026-10-23; handoff, NOT a ticket). Sequence: W2 (credential grain, PR #179) -> scoping wave -> W2+W3+W4 (credential + env + push) -> **CI loop** -> job primitive -> overnight runs. THE CI LOOP is the first shape in which the agent builds WITHOUT the operator in the per-step loop: agent commits -> agent pushes -> CI runs -> agent reads CI -> agent iterates. It is the hinge of the whole sequence and it depends on W4 (push capability) landing, which is why the three waves before it are not optional. Overnight-run era: **3-4 waves out, not tomorrow**; no wave in between can be skipped or reordered. Related filings from this session (four files in `.thoughtmachine/working_docs/`, base `dev @ bc8c7700`): `design-tool-resource-contract.md`, `design-worker-job-lifecycle.md` (marked core-adjacent), `design-agent-role-control.md` — filed as ONE CLUSTER: what runs / how it returns / who calls it, converging on one handle-state-timeout model — plus `hygiene-worker-silent-timeout.md`.

## 2026-10-23 — TURN LIMIT IS ORTHOGONAL TO RUN DURATION (operator directive...

TURN LIMIT IS ORTHOGONAL TO RUN DURATION (operator directive 2026-10-23). The turn limit is OPERATOR-CONTROLLED and can be raised. What the operator cannot currently control is RUN DURATION: the agent returns after ~5 minutes sometimes, and the cause is the BLOCKING TOOL CALL, not the turn count. Therefore raising the turn limit does not fix long runs, and the job primitive's target is RUN DURATION, not turn count. Two cheap partials exist before the primitive: (1) filed `hygiene/worker-silent-timeout` — name the return reason (`timed_out at Ns`) in the worker return path, ~one line, shippable immediately; (2) on this side, a soft-timeout is detectable at the caller as elapsed_seconds ~= timeout_seconds, which is the tell to shrink scope rather than raise the limit. Do not re-diagnose the 5-minute return as a turn-count problem.

## 2026-10-23 — STATE 2026-10-23 (post-#179-merge). **PR #179 MERGED.** `dev...

STATE 2026-10-23 (post-#179-merge). **PR #179 MERGED.** `dev = 5abd2e47` = "Merge pull request #179 from use-less-vars/feat/w2-credential-resource"; `935f0ad0` verified as an ancestor of dev. W2 credential grain is on dev. Note: (a) there is **no #180** on dev yet, so the operator's "dev = 5abd2e47+ (post-#180)" label is loose — 5abd2e47 IS the tip; (b) the PR-body correction that was ruled BLOCKING-ON-MERGE — I have **no evidence it was applied** before merge (I cannot run `gh`), so the merged PR body may still carry the stale premise. **REGRESSION: the four filed tickets are GONE.** `.thoughtmachine/working_docs/ticket-design-tool-resource-contract.md`, `ticket-design-worker-job-lifecycle.md`, `ticket-design-agent-role-control.md`, `ticket-hygiene-worker-silent-timeout.md` were staged by named path on the feature branch; after the merge/branch-delete the worktree is clean and `git_read show_file` says the path "does not exist in HEAD" ⇒ not committed, not on disk. Content is fully recoverable (I have all four files verbatim in the transcript; ~233 lines total) and must be RE-WRITTEN then committed on dev as one `docs:` commit. Do not re-report them as filed. Worktree is on dev with a clean status.

## 2026-10-23 — ## CORRECTION + Gen48 Block B committed (verified first-hand...

## CORRECTION + Gen48 Block B committed (verified first-hand), 2026-xx

**Correction to an earlier "state" entry in this file:** the job-primitive cluster tickets were NOT lost. They are on `dev` — merged as **PR #180** (`dev` tip = `e7ff09b8`, parent `edafdb3f`). Files: `ticket-design-tool-resource-contract.md`, `ticket-design-worker-job-lifecycle.md`, `ticket-design-agent-role-control.md`, `ticket-hygiene-worker-silent-timeout.md`, `ticket-hygiene-catalog-count-drift.md`. Treat any older entry claiming they were lost as stale.

**Block B — paginate gate bypass: FIXED + COMMITTED.** `0c125856` on branch `fix/paginate-tool-gate-bypass` (parent `e7ff09b8`), 3 files, +328/−5. Not merged, not pushed — operator action.
- Laundering hop: `PaginateTool` declared `required_categories = ["filesystem:read"]` and then constructed+ran any class from the global `tools.TOOL_CLASSES` registry with no second check. Executor gated PaginateTool, never the tool it ran on the caller's behalf. RED evidence pre-fix: a stock filesystem-only profile really did construct `ContainerRemoveTool` and `HostBashTool`.
- Two controls: (1) authority re-gate in-tool via `security_gate.check_required_categories` before instantiation, failing closed on import error / absent `effective_permissions` / ask-policy; (2) `_NEVER_PAGINATE` structural refusal of Worker, CheckSystem, KnowledgeBaseTool, PaginateTool, Respond, SummarizeTool — necessary because those declare no required categories and `_WORKER_BLOCKLIST` is applied only at construction/spawn time, never call time.
- Also: `_NEVER_FORWARD = ("workspace_id",)` (executor owns it), and the not-found error no longer enumerates the registry (was an enumeration oracle).
- Residuals, all fail-closed: `permission_footprint` never injected into tools ⇒ workspace-ceiling cap not re-applied (profile is already the capped merge); no event bus ⇒ ask-policy is a deny, not a prompt; gate-unavailable deployment ⇒ pagination now refused.
- Ticket `.thoughtmachine/working_docs/ticket-bug-paginate-tool-bypasses-required-categories.md` lives on branch `docs/gen48-paginate-gate-bypass` (`400b7de4`), NOT on the fix branch. Both branches still need operator push/PR/merge.

**#153 / `d776f454` ("refuse to start on non-loopback bind") — VERIFIED first-hand, with a correction to prior belief:** it is a **refusal-by-default with an escape hatch**, not an invariant. `_validate_bind_host()` runs in `main()` after parse and `SystemExit(2)`s on a non-loopback `--host`/`HOST`, but `--allow-remote-bind` (default off) waives it and prints a stderr WARNING. Same commit added a *peer-address* gate (`_localhost_only`) on `/health`, `/api/health`, `/api/health/containers` only — its own commit body concedes this cannot stop proxied exposure, and `/api/system/health` was left untouched. So the localhost-only claim still needs the entry-point test before it can be doctrine (Block D / ruling 3).

## 2026-10-23 — GEN 48 → GEN 49 HANDOFF

**Read this first, then `project/status.md`.** Unlike Gen 47 there is no continuity gap to
declare: the entry immediately above this one is Gen 47's handoff and it is still accurate
about Gen 47. What it is not accurate about is anything after `75f51e25`, which is all of
Gen 48. Treat its *Where the tree actually is* section as history.

### Where the tree actually is

`dev` = **`cf10c358`** (#189). Working branches, all local, none pushed, none reviewed:
`docs/gen48-onboarding-contract-tracked` @ `65aac9e2`, `docs/gen48-container-base-ticket`
@ `11782051` (stacks on the first), `docs/gen48-status-and-handoffs` (this commit).

**`git rev-parse dev`, every time.** Gen 47 wrote that rule down because a note in this file
had carried the *branch* tip as `dev HEAD` for hours. Gen 48 hit the same trap from the
other side: while these close-out branches were checked out, `git log`'s first line was the
branch, not `dev`. The rule did not stop the mistake from being available; it only stopped
it from being written down.

Two facts about the merge sequence that look like errors and are not. **No commit carries
#167** — the same gap Gen 47 recorded for #160. And **merge order is not PR order**: #176
landed before #175, #186 landed after #188. Both were read off the first-parent chain, so
the status table is in true merge order, not numeric order.

### What landed (Gen 48)

Twenty-two PRs, #168 → #189. The three that changed the shape of the codebase:

- **The wrapper class is gone** (#181, #182, #184, #185, #189). `PaginateTool` declared
  `required_categories = ["filesystem:read"]` and then constructed and ran *any* class in
  `tools.TOOL_CLASSES` on the caller's behalf — the executor gated the wrapper and never
  the tool the wrapper ran. RED evidence before the fix: a filesystem-only profile really
  did construct `ContainerRemoveTool` and `HostBashTool`. Two controls were written first
  (`0c125856`); then the wave concluded the controls were the wrong shape and deleted the
  tool (`4a4bd692`). The census that followed found thirteen more sites (#185); letters
  (b) and (c) are burned down in `d4bc1e35`.
- **`credential` is the eighth permission grain** (#171, #179, #177). Store reconciled onto
  `vault_root` with a global scope, grain added with three audit events, stale permission
  literals swept. Read the caveat in this entry's *What is broken* before describing it as
  audited.
- **The `workspace_id` scope leaks closed** (#172, #175, #178, #177). Open-session tabs
  bucketed by owning workspace; `WorkingDocument` `doc_id` contained; the per-worker
  unsubscribe guarded against foreign-session completions; executor config made to always
  win over a caller-supplied `workspace_path`.

Then the documents, which are the rest of the list and are not filler: #180 filed the tool
resource contract, the worker job lifecycle and the agent role control as **one cluster** —
what runs, how it returns, who calls it, converging on one handle-state-timeout model —
because three separate tickets would have produced three incompatible answers. #170 is the
workspace isolation matrix. #173 and #169 closed two logging and eventing leaks found while
reading something else.

### What this seat learned — five relay errors

1. **A ticket title is a load-bearing artifact, and the operator's premise can be false.**
   Task 2 arrived as "`_ContainerControlBase` is registered as a tool". It is not — 37
   registry members, `get_tool_class` → `None`, `ImportError` on the direct import. The
   ticket was filed with its **title amended before commit**, because a title asserting a
   fact gets quoted later as the fact. Rule: verify a premise before writing the document
   that will be cited as its proof, and if the premise is wrong, say so in the artifact
   itself, not only in the covering note that nobody re-reads.
2. **A `.gitignore` negation can be vacuous and still look correct.** The literal
   instruction `!.thoughtmachine/knowledge/personal/handoffs.md` would have added nothing:
   `personal/` is a *directory* exclusion, git never descends, the negated line matches no
   path, and the file stays untracked. Three lines were needed (negate the directory,
   re-ignore its contents, re-include the file). This is the **same failure shape** as the
   guard that scanned zero files while its tests passed. Rule: after any ignore-rule
   change, `git status` must be **empty** — a rule that changes nothing is the bug.
3. **A file pattern and a directory pattern are not the same negation problem.**
   `project/**/*.md` is a *file* pattern, so a bare `!file` works and git still descends
   into `project/`. `personal/` is a *directory* exclusion and nothing under it can be
   re-included. Two visually similar rules, one requiring a directory negation and one not.
   One rule, two shapes, decided by a character.
4. **A generic class name matches everywhere.** Grepping `TOOL_CLASSES` hits
   `.thoughtmachine/working_docs/tool_no_wrapping_guard.py` and a multi-hundred-KB `.json`;
   grepping `_ContainerControlBase` hits the base, its import block and nine subclasses.
   Rule: exclude `working_docs/*.py` and `*.json` from registry greps, or you will "find"
   mutation sites that are string literals.
5. **The cheapest independent verification is a second worker told to break the first.**
   The claim being checked was going to contradict the operator's own instruction, so one
   read was not enough. The second worker's entire brief was *"your job is to try to break
   it"*. Two agreeing reads — one ordinary, one adversarial — are what made it safe to
   report the premise false rather than quietly widen the ticket to fit it.

### What this seat got right — three patterns to keep

1. **Prove the edit was surgical, with an oracle the edit did not touch.** A 1195-byte
   `.gitignore` append and a one-line title amendment were both verified by reporting line
   count before and after, the single changed line, and an independent reconstruction of
   the diff. For Task 3 the insertion is provable the same way: `wc -l`, byte delta equal
   to the byte length of the inserted block, `head -1` unchanged, and `grep -c '^---$'`
   still 2.
2. **An empty `git status` is an assertion, and it was the only one available.** Task 1 had
   two claims to make — the seven files are tracked, and the rest of `personal/` is still
   ignored. Staging proves the first; only an empty status proves **both**, because a
   residual `??` would have meant the re-ignore did not hold. Reach for the strongest
   single command, not the pile of weak ones.
3. **Write the contradiction into the artifact instead of around it.** Task 1 makes
   `docs/KB_POLICY.md` false as written. It would have been easy to leave that for the PR
   conversation. It is in the commit message, in the status block, and in this entry,
   because the next seat reads files and does not read PR threads.

### What the next seat inherits

**Blocked on the operator, do not attempt:** pushing the three branches, opening PRs,
reading CI. The agent has no `push`, no `gh`, no CI access. `git write` here has no branch
delete, no reset and no amend, and `branch_create` always bases on the *current* HEAD — so
creating a branch off an earlier commit means checking that branch out first.

**Needs an operator ruling before anyone acts:** the `docs/KB_POLICY.md` amendment. As
written, L110-111 forbids exactly what `65aac9e2` did, and L112-114 forbids citing the
tracked set as evidence of intent in any document *including that one*. The minimal
amendment is a decided carve-out naming the seven files, replacing the "DEFERRED, not
decided" block. Do not edit `KB_POLICY.md` unilaterally; it is a policy document, and a
seat that rewrites policy to match its own commit has inverted the order of authority.

**Open and unowned:** the registry membership invariant (nothing iterates `TOOL_CLASSES` to
assert every member overrides `execute` or that no underscore class is a member — a base
class could be appended tomorrow with a green suite); census phase 3 (B1 `GitWriteTool` into
a non-tool module, 39 of 91 closures call no `super()`; B2 `code_modifier_utils.py` into 11
free functions, 0 self-writes); eleven non-test modules that import `TOOL_CLASSES` directly;
eleven client-claim findings awaiting a keep-or-delete ruling, one of which
(`--allow-remote-bind`) is now partly answered by `d776f454`.

**Standing disciplines, and the new ones:**
- `host_bash` is unavailable in this session. Workers reach the tree through the Docker
  code runner against the `/workspace` mount. The worker **cannot run git** — git is not in
  the runner image either, so every git answer must come from the orchestrator's own read
  tools. Design worker briefs accordingly: they can edit and grep, they cannot diff against
  HEAD.
- Never `git ls-tree -r` on this repo. `status` and `diff --cached` are cheap and decisive;
  the recursive tree listing is tens of thousands of tokens for a question you did not ask.
- Never grep `.thoughtmachine` unscoped, and never grep the registry without excluding
  `working_docs/*.py` and `*.json`.
- `personal/*` is append-only. Correct by appending a dated correction. This entry follows
  that rule and so must yours.
- A tool-interface quirk worth knowing before it costs you a turn: multi-path staging and
  commit reject a **one-element** path array; passing the same path twice works, because
  git dedupes pathspecs.

### What is broken

- **`docs/KB_POLICY.md` contradicts the tree.** Not "stale" — contradictory. It forbids the
  seven files that now ship. Ruling owed.
- **Ten accidentally-tracked KB blobs remain tracked.** `gitignore` does not untrack, and
  `git rm --cached` was deliberately dropped in Gen 47 and stays dropped. They are drift;
  the seven are intent; the difference is not visible in `git ls-files`.
- **No registry invariant test.** The container-base ticket files this at severity LOW and
  proposes the test; the test does not exist yet.
- **A filed ticket cites a deleted file.** `ticket-bug-paginate-tool-bypasses-required-categories.md`
  points at `tools/paginate_tool.py:98/102-105/115-116`, deleted in `4a4bd692`. Recorded in
  the container-base ticket rather than silently repaired, because a filed ticket is a
  historical document.
- **Credential events are observed, not audited.** `CREDENTIAL_USE/ROTATED/DELETED` emit,
  are tested, and are not in `event_class_map`, have no subscriber, and do not reach the
  tenant event log. Say "observed".
- **A pre-existing pydantic warning** at `tools/container_control.py:169` — the field name
  `name` shadows an attribute in the parent. Unrelated to anything above; noted so it is
  not re-discovered as new.
- Everything else not already ticketed lives in `brain_pour_2026-10-21.md`. If you close one
  of its thoughts, close it there too, so the next seat's reading gets shorter instead of
  longer.

## 2026-10-25 — GEN 50 → GEN 51 HANDOFF

You inherit `dev = f69761b4`, five PRs and one design pass closed, one code wave waiting on Main 40's review.

**Read first:** `MANDATORY_STOP.md`, then `working_agreements.md` (four new classes at the end — they are the session's output), then `docs/design-auth-layer.md` (the design you'll implement), then the D3 brief at `.thoughtmachine/working_docs/brief-d3-ownership-refusal.md`. The two analysis documents on disk (`redteam-surface-complete.md`, `vault-ownership-sites.md`) are pointers, not reads — open them when the wave needs a row.

**What closed:** the client-claim red team. Seven tickets on `dev` (F1–F12 plus three new: SSRF, logging-config, provider apikey read). The auth-layer design pass — D1 DECLARED, D2 MUST-BIND, D3 `_ownership_refusal` wins, D4 note. The front chokepoint now has a design.

**What's queued:** D3 code wave first (brief on disk, awaiting Main 40). Then D2, then D1, then D4 is a doc edit. Then credential-delivery, credential-resolution, CI loop. Overnight run is 2–3 waves out.

**What Main 40 holds:** the D3 brief review, the SSRF + logging-config drafts before they commit. Do not dispatch D3 before his go.

**What I got wrong — the corrections you inherit:**
1. Silent corrections, three times. The rule is now in `working_agreements.md`: verbatim edits are disclosed.
2. A stale citation carried into a brief (wildcard CORS). The agent's read disproved it. The rule is now in `working_agreements.md`: briefs name mechanisms from sources the author read.
3. Five terminal-paste corruptions. The rule is now in `working_agreements.md`: heredoc ≤10 lines, agent writes the rest.
4. Two state-claims to Main 40 that were wrong (hygiene tickets "uncommitted", design doc path). Both corrected in the relay. Verify state against disk before relaying it — the agent's claims and mine both need the same check.

**The one thing to carry:** the machine's honesty is in the naming of the gaps, not the absence of them. Every status you write names which boundary and what read verified it. When you don't know, say so.

## 2026-10-26 — GEN 51 → GEN 52 HANDOFF

You inherit `dev = 392e6b7a` with the tree clean: the auth arc is complete, D1 through D4 merged, nothing in flight. The next wave is not another auth round — it is delivery.

**Read first:** `MANDATORY_STOP.md`, then `working_agreements.md` (the nine Gen 51 classes at the end — they are this session's actual output), then `docs/design-auth-layer.md` (§D4 closed the arc by naming caller identity as the predicate's first operand). The four D-briefs in `.thoughtmachine/working_docs/` are done; do not re-dispatch them.

**What closed:** D3 (#207) `_ownership_refusal_ids` and its adapters; D2 (#209) `_bind_workspace` as sole authority, WS handshake binding, two unaudited sites closed; D1 (#210) `_origin_check_request` on all 37 non-GET routes plus the enumeration test and the `/api/browse` guard; D4 (#211) the design note. Four PRs, one arc, zero open threads inside it.

**What's queued:** `feat/fresh-install-diagnostic` first — startup banner plus `install-doctor` — because the friend incident showed the sharpest gap is the one no gate covers. Then credential-delivery, credential-resolution, the CI loop. `host_bash` default-off must surface its state at install time, not fail silently.

**Tickets waiting, unfixed:** #208 filed SSRF (CRITICAL), logging-config (HIGH), the macOS advisory (LOW). Hygiene queued: `route-workspace-mutations-through-bind-authority`, `site-2543-bootstrap-bypass` (the slug names a comment — the real site is `server.py:2582–2587`, see the anchor note in `project/operator_observations.md`), and `hygiene/enable-pr-auto-merge`, which is a repo setting rather than code: `gh pr merge --auto` cannot be used while it is off, which is why verification stays manual.

**What I got wrong — the corrections you inherit:**
1. Two counts asserted in briefs that were wrong, and both times the worker was right. `asserted-count-in-brief` is in `working_agreements.md`.
2. A worker reported PASS on a byte-identical comparison whose expected string carried the same corruption as the file. `self-comparison-assertion` is in `working_agreements.md`.
3. Four Gen 51 entries went into the KB mid-session and five more at close. If you find the close entries missing, the close was interrupted, not skipped.

**The one thing to carry:** verify the claim, not the conclusion. Every PASS this session that turned out wrong was honest. The gates held; what failed was the construction of the check.
