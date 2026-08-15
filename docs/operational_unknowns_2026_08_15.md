# Operational Unknowns — Investigation Report

- **Date:** 2026-08-15
- **Branch:** feat/sec-rce-upgrade
- **Head:** 580d9cb
- **Method:** Read-only source inspection. Every claim carries `file:line` evidence gathered by grep/read of the repository. Items that could not be verified are marked **UNKNOWN**.
- **Scope:** Sections A–E of the task brief.

## Top Findings

1. **The "30-minute exec timeout" claim is FALSE.** No `1800`/`3600` timeout literal exists anywhere in `infra/`, `tools/`, or the root `docker_executor.py`. The largest bound found is **600 s** (`workspace_lifecycle_manager.py:70 HARD_TIMEOUT`); the default container exec timeout is **30 s** and is overridable per call.
2. **`_timeout_triggered` is sticky for the life of the worker thread** in the legacy path: it is set once (`tools/workspace/worker.py:1229`) and never reset. It is not part of the per-query reset block (`worker.py:1437-1451`).
3. **Legacy `send_query` has no query-id correlation and nothing drains the output queue** (`worker.py:709-742`) — a stale envelope from a timed-out query is returned to the *next* caller. The stale comment at `worker.py:715` ("query clears the queue") is false.
4. **No tool class appends tool output to the live conversation history.** The only context mutations are agent-core summarization (`agent.py:1317`) and worker context assembly (`worker.py:1293-1300`, `1348-1352`).
5. **EditDockerfile is vestigial**: it is referenced by exactly 4 files (class, registration, worker blocklist, tests); no runtime dispatch path enumerates it. Removal is safe.
6. **Vault git hooks are latent/dead code**: the only consumer is `git_info_tool.py:675-725` (`_run_vault_hooks`), and nothing in the codebase ever creates `~/.thoughtmachine/hooks/`. The directory must be hand-created by an operator or hooks silently never run.

## Section A — Why can a worker stop mid-journey and never emit a final Respond?

**A1. What is `_timeout_triggered`, where is it set, and is it reset per query?**
- `agent/core/agent.py` has **no** `_timeout_triggered` attribute (full-repo grep: matches only in `infra/workspace_lifecycle_manager.py`, `tools/workspace/worker.py`, `tests/test_worker_timeout_audit.py`).
- `worker.py:610` — initialised `False`; **`worker.py:1229`** — the *only* set-True site (guarded by `worker.py:1224-1227`: `agent_state.time_state.value == "CRITICAL" and restriction_reason == "timeout"`, at the end of `_run_tool_loop`). Reads: `worker.py:1568` (`_last_completed_query` guard), `1590` (telemetry), `1596-1597` (forces envelope status `"timeout"`), `1623`.
- Per-query reset block `worker.py:1437-1451` resets **agent state only**: `current_turn=0` (1441), `turn_state`/`last_turn_warning_state` (1443-1444), `restrictions_active/pending=False` (1445-1446), `restriction_reason=None` (1447), `time_start=time.time()` (1449), `last_time_warning_state` (1451). It does **not** reset `_timeout_triggered` (a worker-thread attribute, not agent state) and does **not** reset token counters.
- **Verdict: GAP** — sticky flag; once set, every subsequent query in that thread inherits `"timeout"` status semantics when the agent ends without a Respond.

**A2. What happens at CRITICAL time state — is the agent killed or restricted?**
- `agent/core/state.py:391-406` `get_allowed_tools`: when `restrictions_active`: `'timeout' → ['Respond']` (400-401); `'turn' → ['Respond']` (402-403); `'token' → ['Respond', 'SummarizeTool']` (405); else `[]` (406). `is_tool_allowed` (408-413): empty list ⇒ all tools allowed.
- `agent.py:973-976`: at CRITICAL the agent only logs a WARNING and `log_agent_end('timeout', …)` — **no break/return**; the turn loop continues (soft restriction).
- Worker main loop `worker.py:1368-1380`: `while not self._stop_event.is_set(): _poll_command(); stop→break; input_queue.get(2.0); Empty→continue; None/stop→break`. No reference to `_timeout_triggered` in the loop body — the thread **keeps polling** after putting a timeout envelope.
- Turn-limit path: `agent.py:932` `for turn in range(self.config.max_turns)`; `state.py:259-319` `update_turn_state` reaches CRITICAL at `max_turns-5` (276) → `restriction_reason='turn'` (308-311). If the loop exhausts without a Respond, envelope status stays `None` (not `"timeout"` — `worker.py:1596` requires `_timeout_triggered`, which is only set for *time* exhaustion).
- **Verdict: GAP** — soft restriction only; loop continues; envelope may carry `status: null` which the parent can misread.

**A3. Does the caller-side timeout kill or notify the worker? Is there a stale-reply risk?**
- `send_query` `worker.py:709-742`: `put` at 716, `output_queue.get(timeout=timeout)` at 718, returns the raw envelope; **no query_id check**. Comment at 715 ("query clears the queue") is stale — no drain exists in this path.
- `_action_query` `worker.py:2705-2762`: `send_query(timeout=300.0)` at 2732; no qid validation; on `TimeoutError` a heartbeat-stale heuristic (>600 s ⇒ "appears hung", 2748-2754), else "worker thread is still alive — you can query it again" (2757-2762).
- The worker is **not killed or notified**; it keeps running its in-flight query.
- Output-queue sites (complete): init 645; gets 718/773/2437/2603; puts 1394/1403 (config/import errors), 1489 (pause reply), 1629 (envelope), 1639 (error). Grep `drain|clear|empty()`: only `_pause_event/_resume_event.clear()` (842/861/896/1500) and input-queue drains (1521-1529, 2419-2424). **No output-queue drain exists.**
- **Consequence (confirmed):** a stale envelope from a timed-out legacy query is returned by the next `send_query`'s `get()` at 718 — **wrong-reply risk**. Only the WLM fast path drains and correlates (`infra/workspace_lifecycle_manager.py` `transition_busy`, ~L400-403).
- **Verdict: GAP** — no correlation, no drain, worker keeps running; primary suspect for "stopped mid-journey".

**A4. max_turns / time-bound behaviour** — covered in A2; see also `state.py:259-319`.

**A5. What happens on force-respawn / stop?**
- `_action_spawn` `worker.py:2353-2389`: join budget `max(30, _timeout_seconds)` (2362/2798/2852, 2 s steps); if still alive → warning + thread **abandoned** (2369-2373); `_save_context` before spawning the new thread (2381); generation guard (1907-1923) prevents the old thread from clobbering state (generation allocated at 1696-1742); new thread gets **fresh queues** (644-645) — old thread's replies go to a discarded queue. The old thread keeps running its in-flight query.
- `_action_stop` stop-all: `worker.py:2775-2830`.
- Pause: `command.json` handled per `_poll_command` (866-903), pause reply at 1489.
- Exceptions in the run loop: `worker.py:1631-1660` → envelope status `"error"`.
- **Verdict: GAP** — abandoned threads continue running; join budget is best-effort, not a guarantee.

**A6. Likely sequences for "stopped mid-journey, never Responded":**
1. **LIKELY** — Soft timeout → CRITICAL → Respond-only → agent returns non-tool content without Respond → envelope status forced to `"timeout"` (1596-1597) → parent aborts the journey; worker thread stays alive.
2. **LIKELY** — Caller `TimeoutError` (120 s / 300 s / 600 s) → thread not killed, reply lost or stale → wrong-reply to the next query.
3. **POSSIBLE** — Parent force-respawn → old thread abandoned mid-query.
4. **POSSIBLE** — Cross-session stop-all (`_action_stop`, 2775-2830).
5. **POSSIBLE** — Pause via `command.json`.
6. Exceptions (1631-1660) → `"error"` status.

## Section B — Docker exec timeout truth

**Question: was the "30-minute exec timeout" claim accurate?**
**Verdict: FALSE.** No `1800`/`3600` literal exists anywhere in `infra/`, `tools/`, or root `docker_executor.py`. The largest bound is **600 s**; the default exec timeout is **30 s** (overridable). Precedence table:

| Component | Literal | Where |
|---|---|---|
| `ContainerManager.exec` default | 30 s (overridable kwarg) | `infra/container_manager.py:639` |
| `ContainerManager.exec` on timeout | join(30) → `container.kill()` → `_drop_container` → `TimeoutError` | 678, 682, 685-686 |
| `container.stop(timeout=5)` | 5 s | 756, 1349, 1374 |
| `_exec_checked` (never kills) | 10 s | 909 |
| `ContainerRegistry.STOP_TIMEOUT` | 10 s | `container_registry.py:106` |
| destroy grace | 10 s | 503 |
| `WorkspaceLifecycleManager` SOFT/HARD/EXEC_KILL_GRACE | 300 / 600 / 10 s | `workspace_lifecycle_manager.py:69-71` |
| `ResourceContainerManager.exec` default | 30 s (overridable) | `resource_container_manager.py:640` |
| `container.stop(5)` | 5 s | 723, 749 |
| `DockerCodeRunner.timeout` Field default → forwarded to exec | 30 s | `docker_code_runner.py:125-128`, 389-395; timeout → `exit_code=-2, timed_out=True` 429-437 |
| idle pool close | 600 s | 169-171 |
| root `docker_executor.execute` default | 30 s | `docker_executor.py:639` |
| `_exec_with_timeout` | join(30) → `container.kill()` → recreate → `TimeoutError`; `exit_code=-2` | 678, 704, 710, 715-716, 662-664 |
| `container.stop(5)` | 5 s | 329 |
| rebuild endpoint executor `idle_timeout=0` | 0 (ephemeral, no pooling) | 938 |
| `git_info._run_git_raw` | 10 s | `git_info_tool.py:520` |
| mcp client q.get / post | 5 / 30 s | `mcp_client.py:116, 340, 370` |
| `mcp_server_connect` executor.run | 30 s | ~101 |
| `check_system` urlopen / manager.exec | 8 / 30 s | `check_system.py:508, 514` |
| worker `SPAWN_QUEUE_TIMEOUT` | 600 s | `worker.py:55` |
| worker default query timeout | 600 s | 594 |
| input get poll | 2.0 s | 1376 |
| `send_query` (legacy `_action_query`) | 300 s | 2732 |
| spawn/stop join budget | `max(30, timeout)` | 2362, 2798, 2852 |

Worker query-level chain: `worker.py:594` default 600 → `worker_cfg["timeout_seconds"]` (983) → `time_warning_threshold = max(5, 0.8*timeout)` (986-989) → agent time monitor (`agent.py:927`, per-turn check 957-979). This is a **query-level soft bound**, not an exec bound.

## Section C — Context loading/assembly

**C1. Can restoring a session double-load or duplicate context?**
- Restore chain: `agent_presenter.py:242-253` (`load_session`) / 255-263 (`load_session_by_id`) / 265-270 (`load_current_session`) → `session_lifecycle.py:271-336` (`load_session`: auto-saves current first, 283; parse + version, 286-290; `update_from_persistable_dict` or `Session.from_persistable_dict`, 291-298; `ContextBuilder._cleanup_orphaned_tool_messages(session.user_history)` 301-302; `state_bridge.bind_session(session)` 305; `update_external_file_path` 306; container integrity re-verify 311-329). `load_session_by_id` 337+ → `session_store.load_session(session_id)` 346.
- Loads the **full** Session (user_history + summary) — a faithful load that **replaces** the session object (`user_history[:] = cleaned`, 302): **last-load-wins, no double-append**.
- Double-restore guard: **NONE exists** (no "already loaded" flag; `_restarting` at `session_lifecycle.py:32` is not used as a load guard), but replace semantics prevent duplication.
- **UNKNOWN:** the exact `state_bridge.py` `start()`/`continue()` call sites that invoke the load (grep was blocked by token cap).
- **Verdict: PASS (no duplication found); UNKNOWN for bridge call-site wiring.**

**C2. Summary compaction: can it duplicate or drop context?**
- `compact_after_summary` (`agent/core/worker_context.py:133-180`): scans from the end for the last `summary: True` message; keeps leading `role=system` non-summary messages + **only the last summary** + everything after; idempotent.
- If the agent summarized twice, the first summary block is dropped pre-compaction (cumulative content is preserved in the second) — no duplication.
- `_apply_summary_pruning` (`agent.py:1317`): inserts the summary system message with `summary=True` at insertion_idx (1448-1453); `MAX_SUMMARY_LENGTH=20000` (1443); appends the user `[SYSTEM NOTIFICATION] Context has been summarized…` (1455-1457); stores `session.summary` (1459); emits a single `context_summarized` event (1326). Two summarizations ⇒ two summary blocks before the next compaction drops the older one.
- **Verdict: PASS (with the two-summary-blocks pre-compaction nuance).**

**C6. Do any tools append their output to the live conversation history?**
- Grep `tools/` for `user_history.append|history.append|messages.append|add_user_message|append_to`: exactly 4 hits — `apply_edits.py:283,287` (string result lists, not conversation) and `worker.py:1297` (fresh-context build: system prompt + `"Initial context: {json}"` system message, 1293-1300) and `worker.py:1353` (merge of initial_context into loaded context, **deduped by exact content match**, 1348-1352). Both worker hits are context assembly, not tool output.
- Session load only **removes** orphaned tool messages (`session_lifecycle.py:300-304`).
- **Conclusion: no tool class appends tool output to the live conversation. Verdict: PASS.**

## Section D — EditDockerfile removal

- Class: `tools/workspace/edit_dockerfile.py:48`; `tool="EditDockerfile"` (51); `required_categories=["container:write"]` (52); `skip_output_truncation=True`; appends `# Added by agent via edit_dockerfile on <iso>\n{instructions}\n`; creates from template `resources/default_dockerfile.txt` with fallback `FROM python:3.11-slim\n` (91-95).
- References (full-repo grep = 18 hits in exactly 4 files):
  1. `tools/workspace/edit_dockerfile.py` (definition)
  2. `tools/__init__.py:237-240` (registration, try/except ImportError)
  3. `tools/workspace/worker.py:398` (blocklist entry — **vestigial**, defense-in-depth)
  4. `tests/tools/test_workspace_tools.py:1155-1274` (TestEditDockerfile incl. `container:write` assert at 1272-1274)
- **Not referenced by** any agent tool list, `_ALL_TOOLS`, server.py, tool_map, or web UI.
- `security_gate`: no `container:write` in the level map (`banned0/ask1/read2/connect3/write3/full4`, `security_gate.py:169-227`) → a boolean True satisfies via `_value_satisfies`.
- **Removal verdict: SAFE** for runtime (no callers beyond registry-based dispatch). Needed changes: delete `edit_dockerfile.py`; remove registration `tools/__init__.py:237-240`; remove blocklist entry `worker.py:398`; update/remove `tests/tools/test_workspace_tools.py:1155-1274`.

## Section E — Vault git hooks

- **Only consumer:** `tools/git_info_tool.py:_run_vault_hooks` (675-725): path `~/.thoughtmachine/hooks/<workspace_id>/<hook_name>` (697-699); **silently skips** if not a file (debug log, 700-703); **never mkdirs**; runs **only `pre-commit`** (904-907); `required_category git:write` conditional (unset when perms are ask/absent, 716-721); `RuntimeError` on non-zero exit (715-719).
- **The hooks directory is never created by the codebase**: `thoughtmachine/vault.py ensure_vault_structure` (51-72) creates only `VAULT_SUBDIRS` (`state`, `logs`, …) — no `hooks` compartment; `infra/` has zero `hooks` matches; no registry/container-manager code touches it. ⇒ `~/.thoughtmachine/hooks/` must be **hand-created by an operator**; until then vault hooks are **dead code** (skip path).
- Host-git fallback hardening (`git_info_tool.py:397-419`): `core.hooksPath=/dev/null`, `core.attributesFile=/dev/null`, `diff.external=`, `core.fsmonitor=`, filter.clean/smudge=, `diff.textconv=`, `credential.helper=`; commit uses `--no-verify` (418-419) — **repo hooks disabled**; vault hooks are the only sanctioned host-side pre-commit injection point.
- Container-git path (448-505): repo `.git/hooks` **allowed** inside the container (486-488, no `--no-verify`); vault hooks do **not** run in the container path (host fallback only).
- If removed/disabled: the host fallback loses its only pre-commit gate, but also loses the arbitrary-host-code execution risk. Nothing in codebase creates or depends on the hooks dir; tests/docs don't reference vault hooks.
- **Verdict: NOT IMPLEMENTED (latent)** — code path exists and would execute operator-created hooks, but no code or installer creates the directory; removal is safe with the caveat that operator-created hooks would silently stop running.

## Appendix — UNKNOWN items
- Exact `state_bridge.py` `start()`/`continue()` call sites that invoke session load (grep blocked by token cap during investigation; restore chain itself verified through `agent_presenter.py` → `session_lifecycle.py`).
- Any runtime configuration that sets worker `timeout_seconds` above 600 s (none found in code defaults; deployment config not inspected).

## Method notes
- Evidence gathered via full-repo greps and line-range reads; line numbers verified against `feat/sec-rce-upgrade` @ 580d9cb.
- Items not verifiable within the read-only investigation are explicitly marked UNKNOWN.
