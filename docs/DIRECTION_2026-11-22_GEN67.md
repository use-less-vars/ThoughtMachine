SUPERSEDES the record at `docs/DIRECTION_2026-11-22.md`, which was scored at dev `ea59dcda` and committed by 18d7e1c9 (#443). That file stays where it is and is not touched by this one.
This file is itself NEVER EDITED IN PLACE. When the four properties are next scored, a new dated record replaces it; a dated record that claims to be current is the defect it documents.
Measured at dev sha `93af40ccedff7e9c955cd15a6c994d0a1c4d00be` (`git_read rev_parse`, 2026-11-22). Every root below is a `file:line`, a test path, a commit sha or an invariant id, read at that commit.
Where the measurements were taken, because a reproduction will otherwise disagree: they were read in a worktree checked out at `93af40cc` (`.worktrees/direction`), NOT in the default checkout. The default checkout in this environment is not on dev — its HEAD is `98a5c46372c49ec873fdde7f13c3c7e97565822e` and `merge_base(93af40cc, 98a5c463)` is `592f59bbcbdc1a6c85d6ea99137d8ac7395c77d5` (#434) (`git_read rev_parse`, `git_read merge_base`, 2026-11-22). Anyone who re-measures from the default tree is reading a tree roughly ten dev commits behind plus one unmerged commit, and will get different answers.
Status: recorded, not enforced. Sections 2-5 are the measurement; section 6 is the queue.

## 0. The scope of this record

- **The pin is a snapshot.** 93af40cc is dev at the time of reading (`git_read log` at 93af40cc returns, newest first: 93af40cc, 4e83c488, 4698f470, 48106f45, 6634d47a, a38c00cd, 18d7e1c9, 393b3c8c, ea59dcda, 336cd697). It will stop being dev. Re-measure before citing this file as current.
- **A green check certifies its SCOPE, not its SUBJECT.** That is the pattern in section 1 and the reason this record exists. A check that answers a narrower question than the one it is read as answering is a false assurance even when it is honest about its own code.
- **A sentence with no parenthesised root is an opinion.** There are none stated as findings below. Where a claim was read in a prior record and not re-read here, it says so and it is in that property's NOT MEASURED list.
- **A control can be merged and inert.** I7 is the instance: a reaper that raises on its own first loop, swallowed by an `except` that prints, so the census that depends on it asserts over state it never cleared.

Contradictions found while measuring, each with its root:

1. The default checkout is not on dev (§ header above: HEAD `98a5c463`, merge-base `592f59bb` = #434). Everything below was read in the `93af40cc` worktree.
2. The superseded record says the container delete scope is "ENFORCED on the branch, not yet on `dev`". It is now on dev: **6634d47a (#442)**, "the container DELETE path must be as scoped as the read path", is an ancestor of 93af40cc (`git_read log`).
3. The handoff describes roughly a megabyte of knowledge as unpinned. Measured here: **1,530,275 B, i.e. 90.5%** of `.thoughtmachine/knowledge` (1,691,882 B) is outside the nine pinned entries (I1).

## 1. The named pattern: ten measured instances

The pattern: an artefact answers a narrower question than the one it is read as answering, and is honest about its own code while silent about its coverage. Ten instances, each with a root.

**I1 — The knowledge pin covers nine entries and says nothing about the rest.**
`scripts/check_kb_staleness.sh` keeps its digests inside the router itself, "deliberately NOT a second lockfile", and `--update` "recomputes and rewrites those tokens in place, and touches nothing else" (header comment, `scripts/check_kb_staleness.sh`). `.thoughtmachine/knowledge/project/HANDOFF.md:20-28` carries exactly **9** `sha256:` tokens, pinning **161,607 B**: `docs/CONTROL_PLANE.md` 52,818 B; `knowledge/project/MANDATORY_STOP.md` 4,047 B; `architecture_invariants.md` 9,472 B; `docs/ARCHITECTURE_MACHINE_MODEL.md` 22,736 B; `machine_history_and_vision.md` 17,953 B; `vision_ledger.md` 9,061 B; `ORCHESTRATOR_GUIDE.md` 18,543 B; `CI_PROCESS.md` 11,787 B; `INFRA_ONBOARDING.md` 15,190 B (measured at 93af40cc). `.thoughtmachine/knowledge` totals 1,691,882 B and `.thoughtmachine` totals 2,999,088 B (measured at 93af40cc), so **1,530,275 B of knowledge is unpinned**. Advisory entries never fail the check: `KB_ADVISORY_ENTRIES=${KB_ADVISORY_ENTRIES:-PRIORITIES.md}` (`scripts/lib/kb_manifest.sh:44`; twin list at `scripts/gen_handoff.py:41-44`, applied `:150-159`), and advisory entries "print a NOTE and do not fail" (header, `scripts/check_kb_staleness.sh`). The fail-closed direction is right — a non-advisory entry with no usable digest is STALE (same header) — and the scope is nine files while the subject is "is the knowledge base current".

**I2 — A macOS check that is green about nothing.**
`.github/workflows/platform-matrix-macos.yml` is named "Platform Matrix (macOS, advisory)" (:1) and all three jobs carry `continue-on-error: true` at job level: `macos-suite` (:66), `macos-install-smoke` (:106), `macos-docker-probe` (:228). Its own header states it: "All of them are advisory (continue-on-error: true)" (:4) and "Every job here is advisory: a red macOS run is reported, never merge-blocking" (:44). It is red, not green: run 45046273840, job 137281468830 reports `2 failed, 8186 passed, 29 skipped, 6 deselected, 6 xfailed, 1040 warnings in 783.57s` (`.thoughtmachine/pr/macos-advisory-ticket.md`). A merge gate that cannot go red for the platform it names certifies the merge, not the platform.

**I3 — A guard labelled POSIX that does not exclude Darwin.**
`tests/test_execution_kill_target_guard.py:276` defines `POSIX_ONLY = pytest.mark.skipif(...)` with `reason="process groups and /proc state are POSIX-only"` (:278). On Darwin `os.name == "posix"`, so the guard is a no-op and every `/proc`-dependent assertion in that module runs (:276-278).

**I4 — One `None` for two different worlds.**
`tests/test_execution_kill_target_guard.py:668` documents `_proc_state` as "The state character of ``/proc/<pid>/stat``, or ``None`` if unreadable" and returns `None` from three distinct causes: `except OSError` (:681-682), `end == -1`, and an empty field list (:668-683). `_wait_for_zombie` (:686) returns only on `state == "Z"` and otherwise raises "never became a zombie ... (last state None)" (:699-701). The observed failure — `pid 7137 never became a zombie within 10.0s (last state None)` (`.thoughtmachine/pr/macos-advisory-ticket.md`) — therefore cannot distinguish "this platform has no `/proc`" from "the child did not die", and any conclusion drawn from the message inherits that ambiguity.

**I5 — A universal claim asserted only on the gated path.**
The property "the agent must never execute on the host outside a named gate" has no enumerating test. The gate itself is precise: `thoughtmachine/host_port.py:576-580` lists its ordered checks, including (4) "the workspace operator kill switch `allow_host_resources` is JSON `true`" and (5) the effective grant for the grain. Its consumers in non-test code are eight files (grep for `host_resource_policy` at 93af40cc): `tools/git_write_tool.py:12`, `tools/ci_read_tool.py:247`, `tools/git_info_tool.py:388`, `tools/host_bash_tool.py:274`, `tools/workspace/check_system.py:35`, `web_ui/backend/workspace_routes.py:55`, `web_ui/backend/global_routes.py:41`, `security/security_gate.py:1374`. Files that spawn host processes and never name it (grep for `subprocess.Popen|subprocess.run|os.system|check_output` over `tools/`, `security/`, `web_ui/backend/` at 93af40cc): `tools/mcp_client.py`, `tools/workspace/worker_execution.py`, `security/sandboxed_execution.py`, `web_ui/backend/server.py`. The bypass in `tools/mcp_client.py` is documented and test-pinned at :228-251: "DEFERRED HOST EXECUTION — DO NOT MIGRATE YET", "This stdio spawn still calls subprocess.Popen directly and therefore bypasses the thoughtmachine/host_port.py chokepoint", pinned by `tests/test_mcp_client_uses_host_port.py` (8,641 B), blocked on `host_run_async` (`thoughtmachine/host_port.py:854`, raising `NotImplementedError` at :877) and on `HostRunHandle.poll`/`wait`/`kill` (:171, :178, :185, each raising `NotImplementedError`).

**I6 — A display that derives truth from an unanswered field.**
`web_ui/frontend/src/components/workspace/WorkspaceDetailPage.jsx:559` does `setHostAllowed(!!summary.allow_host_resources)`; `undefined` coerces to `false`, and that falsy value then renders "Host resources are forbidden. This workspace can withstand interactions with untrusted content." (:579-581), the safe posture class (:755) and "Host execution disabled" (:763-766). No third state exists in that file at 93af40cc (grep: neither `hostResourcesKnown` nor `wdp-posture-unknown` occurs), so "unread" and "off" render identically. The store has the same shape: `web_ui/frontend/src/store/workspaceStore.js:394-404` takes `ep = permsData && permsData.effective_permissions`, and when `if (ep)` fails it falls into `else if (purpose)` and reports `purpose.defaults.permissions` ceilinged by `PERMISSION_CEILINGS[name] || effective` — values that were never observed.

**I7 — A merged control that certifies only the module that opted in, and whose reaping half is inert.**
The `tests/` half of #448 (93af40cc) is a census that asserts only for modules listed in `_CENSUS_MODULES` — one module at 93af40cc: `{"tests.test_event_bus_subscription_leak"}` (`tests/conftest.py:543-545`; marker `thread_census` at :547; the census stays quiet when the test already failed, :531 and :553-556). So the "thread/bridge census" claim covers one module of a suite that collects more than eight thousand tests (`scripts/ci_assert_collected.py --expected 7600` in the macOS workflow, :85-89).
The `web_ui/backend/` half of the same commit does not work at all. `_webui_reap_worker_threads` (`web_ui/backend/tests/conftest.py:280`) loops `for ident, thread in threading.enumerate():` (:287), but `threading.enumerate()` yields `Thread` objects, so the first iteration raises `TypeError: cannot unpack non-iterable _MainThread object`. The caller is the autouse fixture `_webui_thread_bridge_census` (:321-322), which wraps the call in `except Exception` and prints "thread census: thread reaping failed: %s" (:368) on every teardown, then proceeds with `survivors = []`. **Backend worker-thread reaping is therefore inert in the merged tree: nothing is killed, nothing is joined, and the survivors list is empty by construction rather than by measurement.** The census assertion below it is written correctly and is unaffected — it tests `id(thread) not in threads_before` (:378) — which is why the defect is silent: only the reap is broken, the count still runs.

**I8 (canonical) — The pin named after growth counts files, so growth inside an exempted file is invisible to it.**
`tests/test_host_port_kill_switch.py` is the control for "no new host-execution call site": it parses the scanned modules and looks for `subprocess` / `os.system` spawns, failing the offender unless the file is named in `ALLOWED_OFFENDERS` — a tuple of `(repo-relative path, reason)` pairs, one entry per file, matched by whole string, with no globs and no prefixes (module docstring and the tuple itself). Measured at this revision: **18** entries, **16** of which still contain a live call site, and the module's own suite reports **15 passed**. The exemption's unit is the PATH, so a second, third or tenth spawn added to a file that is already exempt is invisible: a probe that appended a `subprocess.run([...])` call site to `scripts/gen_handoff.py` left the suite at **15 passed**, the same set as before (probe applied by hand, then restored byte-identical). Every allowlisted file is a blind spot with a written reason, and nothing re-measures the reason. This is the canonical instance of the pattern because the control is neither absent nor advisory nor mislabelled — it runs, it is correct about what it reads, and it is SELF-UNDERMINING: the granularity it chose is the one its defect class grows in, so "host execution is pinned" is really "no new FILES spawn", and the difference is not visible from a green tick.

**I9 — A capability claim carried in prose, and a setup guard that admits the case can measure nothing.**
`tests/test_execution_kill_target_guard.py:884-885` puts `@POSIX_ONLY` on `test_unreadable_proc_state_degrades_toward_alive`, whose docstring says the opposite in as many words: "No capability guard: the unreadable condition is INJECTED, so this runs on every platform" (:911-912). The decorator decides, and on Darwin its predicate is satisfied for the reason I3 gives (`os.name == "posix"`), so the case does run there today — by accident, not by construction. Tighten `POSIX_ONLY` to Linux, which is the natural repair for I3, and this case skips silently on the one platform where `/proc` is absent and degradation-toward-alive is the entire subject. The second half is `assert unreadable.attempts > 0` (:933-936 — "the process-state read was never attempted, so nothing was proved about the unreadable case"): an honest guard, and the file's own admission that the seam can be missed; no equivalent guard stands over the group-kill cases in the same module. On Darwin the `FileNotFoundError` parametrisation is also the platform's NORMAL read — an unpatched `open` of `/proc/<pid>/stat` raises the same exception there — so that parameter exercises a real path while being documented as an injected edge, and it cannot distinguish "injected absence" from "no procfs at all": the I4 substitution again, inside the case that exists to prove the platform survives it.

**I10 — The mechanism this repo documents is not the mechanism that blocks the merge.**
`dev` is governed by a GitHub RULESET. `docs/BRANCH_PROTECTION.md` and `scripts/apply_branch_protection.sh` both describe CLASSIC branch protection, and the classic endpoint — `/branches/dev/protection` — answers 404 on this repo; `docs/CONTRACTS.md` records the ruleset reading instead, in its *What CI actually runs* section, so the two documents describe two different mechanisms and only one of them can block a merge. `docs/BRANCH_PROTECTION.md` is honest about its own half of that ("Read that last sentence as *intended*, not *effective*", plus a section on the 404), so the defect is not a lie in a document: it is that one name covers two mechanisms, and the file that owns the subject still sends the reader to the endpoint that answers 404. It is recorded here because it corrects an earlier reading of this repo, in this line of records — the 404 was taken as "no protection". A 404 on one mechanism is not the absence of governance; it is the absence of MEASUREMENT at that endpoint, which is the same substitution of unmeasured for negative that I4 and I6 make.

## 2. F1 — what the operator SEES must not lie about permissions

**What holds.**
- The gate reads the switch strictly: `load_workspace_config(workspace_id).get("allow_host_resources") is True` (`tools/host_resource_policy.py:74`), and the security gate refuses on the same strict read with the workspace policy named as the reason (`security/security_gate.py:1361-1374`). Absent, false and malformed all mean off.
- Two display-side repairs are on dev: `ea59dcda` (#438, "cannot verify must never render as verified clean") and `393b3c8c` (#439, "resolve display capabilities through the gate, not a permissive default"), both ancestors of 93af40cc (`git_read log`).

**What is known broken or degraded.**
- The workspace posture renders a falsy unknown as "forbidden ... can withstand untrusted content" (`WorkspaceDetailPage.jsx:559`, `:579-581`, `:755`, `:763-766`).
- A failed effective-permissions read reports the preset's defaults as if observed (`web_ui/frontend/src/store/workspaceStore.js:394-404`).
- The fix exists off-dev only: branch `fix/f1-unknown-not-false-assurance` = **af9d3dd2** (`git_read rev_parse`, 2026-11-22), unmerged.

**What is not measured.**
- Whether any route actually feeds `summary.allow_host_resources` as `undefined`. The JSX defect is proven from code; the triggering payload was not traced through `web_ui/backend/`. NOT MEASURED.
- Whether any test would go red on the regression. No test suite was run for this record. NOT MEASURED.

## 3. F2 — the agent must not execute on the host outside a named gate

**What holds.**
- The enforcement sites are real and strict: `tools/host_bash_tool.py:259-280` refuses with "Host execution is disabled for this workspace (allow_host_resources=false)"; `tools/git_info_tool.py:367` and `:388-401`; `tools/ci_read_tool.py:239-255`; `security/security_gate.py:1353-1374`.
- `tools/git_write_tool.py` is not a bypass and is named here so it is not accused: it imports `load_workspace_config` only for `git_identity` (:885) and inherits the gate by routing execution through `resolve_git_execution_mode`, imported from `git_info_tool` (:10) and called at :454.

**What is known broken or degraded.**
- `tools/mcp_client.py:228-251` spawns a long-lived stdio child with `subprocess.Popen` and bypasses the chokepoint by design (comment at :228-240), pinned by `tests/test_mcp_client_uses_host_port.py`.
- `security/sandboxed_execution.py` gates only when the caller passes a category (`if required_category is not None:` at :139-140) and documents `workspace_id` as "Optional workspace identifier (informational; reserved for future workspace-capability merging)" (:57-62) — it never reads `allow_host_resources`.
- `tools/workspace/worker_execution.py:387` spawns the docker CLI (`["docker", "exec", container_id, "kill", pid]`) with no policy consult.

**What is not measured.**
- A complete enumeration. The counts above come from four grep patterns over `tools/`, `security/` and `web_ui/backend/`; `infra/`, `agent/` and `thoughtmachine/` are not covered, and argv assembled at runtime cannot be seen statically. NOT MEASURED as an enumeration.
- The spawn sites matched inside `web_ui/backend/server.py` were not inspected. NOT MEASURED.

## 4. F3 — the kill switch must be absolute

**What holds.**
- The kill decision is re-taken before escalation and the grace is honoured rather than assumed: `_escalate_subprocess_kill` re-reads `_kill_decision`, takes `grace = getattr(self, "kill_grace_seconds", EXEC_KILL_GRACE)`, polls `_pid_is_alive` every 0.05 s to the deadline, then SIGKILLs by killpg/kill (`tools/workspace/worker_execution.py`).
- The degradation is conservative, which is the correct direction: unreadable is never read as dead. `_pid_is_alive` returns `True` on `except PermissionError`, on `except OSError: return True  # no /proc (or unreadable): fall back to signal 0`, and on `end == -1` (same module).
- #441 (`336cd697`, "never signal a kill target the model chose") removed the model's choice of target and is an ancestor of 93af40cc (`git_read log`).

**What is known broken or degraded.**
- On a platform without `/proc` the conservative arms cannot distinguish alive from unobservable, so the switch burns the whole grace and then SIGKILLs a corpse; the runtime cost is real and is not measured here (same arms).
- The characterisation test that would pin this behaviour exists only off-dev: `fix/procfs-not-measured` = **755f600b**, parent 382338fc, subject "test(kill-guard): pin the conservative degradation when /proc is unreadable" (`git_read rev_parse`, 2026-11-22), unmerged.
- The test that is on dev asserts the other face and cannot run honestly on Darwin: I3 and I4 above.

**What is not measured.**
- Actual grace-burning on macOS. The red run shows a test failing, not the production path timed (`.thoughtmachine/pr/macos-advisory-ticket.md`). NOT MEASURED.

## 5. F4 — one workspace's authority must not reach another

**What holds.**
- The delete path is now as scoped as the read path: **6634d47a (#442)**, ancestor of 93af40cc (`git_read log`).
- Both scoping checks exist in one module: `_workspace_ownership_refusal` at `tools/container_control.py:719` (status path, used :753) and :1012 (remove path, used :1077).
- The envelope is a birth certificate, not a setting: the container name is derived from the envelope, not reconfigured into it — `build_container_name` takes `workspace_path, lifecycle_class, session_id=None, network_mode="none"` (`infra/containers/executor.py:264`), so an envelope change yields a different container (`.thoughtmachine/knowledge/project/architecture_invariants.md:56` states the ruling; restated at `scripts/gen_handoff.py:218` and `.thoughtmachine/knowledge/project/HANDOFF.md:95`; the open instance is C30, `.thoughtmachine/knowledge/project/PRIORITIES.md:173`).

**What is known broken or degraded.**
- The gap is documentary as well as technical: no invariant in `docs/MACHINE_INVARIANTS_AND_LORE.md` names workspace-authority isolation — cited from the superseded record (18d7e1c9, section 1(b)) and **not re-counted** in this pass (NOT MEASURED here). A reader of the invariant table alone would conclude the delete path needs no scoping.

**What is not measured.**
- Every other destructive verb: stop, restart, prune and the registry paths were not enumerated for the same scoping. NOT MEASURED.
- `build_container_name`'s hashing body was not read; the derivation claim rests on its signature (:264-268) and on the invariant row.

## 6. The queue

**MERGED**, all ancestors of 93af40cc (`git_read log`), newest first:

| sha | PR | what it closed |
|---|---|---|
| 93af40cc | #448 | "test(harness): reap worker threads and bridges per test, and assert a census" — the `tests/` census works for the one module that opts in; the `web_ui/backend/` reaper does not work at all (I7) |
| 4e83c488 | #447 | "feat(web-ui): persist pasted images across a session reload" |
| 4698f470 | #446 | "fix(worker): revalidate the command channel before dispatch" |
| 48106f45 | #445 | "test(worker): kill the worker thread in teardown so the module stops leaking threads" |
| 6634d47a | #442 | "fix(security): the container DELETE path must be as scoped as the read path" — closes F4's named gap |
| a38c00cd | #444 | "test(worker-heartbeat): count only this worker's heartbeat in the idle tests" |
| 18d7e1c9 | #443 | "docs(direction): score the four security properties at dev ea59dcda" — the record this one supersedes |
| 393b3c8c | #439 | "fix(caps): resolve display capabilities through the gate, not a permissive default" |
| ea59dcda | #438 | "fix(envelope): cannot verify must never render as verified clean" |
| 336cd697 | #441 | "fix(security): never signal a kill target the model chose" |

**OPEN**, shas verified by `git_read rev_parse` on 2026-11-22:

- `fix/procfs-not-measured` = **755f600b** — pins the conservative `/proc` degradation (F3, and I4's subject).
- `fix/f1-unknown-not-false-assurance` = **af9d3dd2** — the third display state for F1 (I6).

**NOT STARTED**, each requiring an enumerating artefact rather than a fifth gate:

- The one-line repair of I7: `for thread in threading.enumerate():` at `web_ui/backend/tests/conftest.py:287`, with a test that fails when the reaper returns survivors it did not clear — otherwise the swallow at :368 hides the next regression the same way.
- F2: a test that walks every host-executing site and fails on an un-gated one (I5). The enumerated list is a grep, not a test.
- F4: the same enumeration for destructive verbs other than delete.
- F1: a route-level negative control that supplies an unknown payload and asserts the display names the refusal (I6), not a renderer test on a supplied payload.

## 7. Open defects, measured and unowned

1. **Backend thread reaping is inert in the merged tree.** `_webui_reap_worker_threads` raises `TypeError` on its first loop iteration (`web_ui/backend/tests/conftest.py:287`, `threading.enumerate()` yields `Thread` objects), the autouse fixture `_webui_thread_bridge_census` (:321-322) catches it and prints (:368), and no backend worker thread is ever killed or joined — so the census counts state the reaper was supposed to have cleared. A merged control, silently a no-op. (I7)
2. **The macOS ticket's conclusion is not supported by its evidence.** The message collapses "no `/proc`" and "not a zombie" (`tests/test_execution_kill_target_guard.py:668-701`), and the guard does not exclude Darwin (:276-278), so the run is red for a platform the test cannot test — and the finding was recorded as a product defect. (I3, I4)
3. **`security/sandboxed_execution.py` is a host-subprocess runner with no workspace kill switch.** `workspace_id` is informational (:57-62) and the permission check is skippable (:139-140).
4. **The MCP stdio spawn stays outside the port** until an async primitive exists (`tools/mcp_client.py:228-251`; `thoughtmachine/host_port.py:854`, `:877`, `:171`, `:178`, `:185`).
5. **The knowledge pin's coverage is undeclared** at 90.5% unpinned (I1).

Reported but **not re-verified** in this pass, therefore not asserted: `tests/test_retention_scenarios.py::test_01_active_workspace_is_retained` failing in CI unattributed, and `web_ui/backend/tests/test_bridge_dedup.py` failing collection standalone. NOT MEASURED.

## 8. The rule that keeps this file honest

- Every claim carries a verifier — a test path, a commit sha, an invariant id or a `file:line` — or it is tagged `[N]` for narrative and is not a finding.
- Dated records are superseded, never edited. This file names its predecessor and stays in its own path so that can remain true.
- The arc and the invariant table are cited, never duplicated. Two tables is how two tables start disagreeing.
- Volatile numbers are dated in place or not stated. Every byte count and sha above is dated 2026-11-22 at 93af40cc.
