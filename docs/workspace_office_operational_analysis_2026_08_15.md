# Workspace Office — Operational Analysis

**Date:** 2026-08-15
**Scope:** Read-only audit of the workspace-office runtime (worker lifecycle, timeouts, docker-in-worker, multi-worker, context input guard, dual-use tools, container registry, cleanup, vault/workspace state, no-Docker fallback, summarization, bridge/server modularity).
**Method:** Three read-only evidence passes over the source tree (no code modified, no files committed other than this report). All evidence is file:line from branch `feat/sec-rce-upgrade` @ HEAD `580d9cb`. Items that could not be verified are marked UNKNOWN.
**Verdict legend:** PASS / GAP / NOT IMPLEMENTED / UNKNOWN per section.

---

## A. Worker lifecycle & timeouts

**VERDICT: GAP** — A coherent lifecycle exists (spawn/check/query/stop, pause/resume, persisted context), but cancellation is cooperative only: threads are daemonized, join budgets are capped (5 s / 30 s / max(30, timeout)), and a spawn waits a fixed 600 s with no preemption.

Evidence:
- `tools/workspace/worker.py` (2887 L) is the single worker implementation: `WorkerSessionLifecycle` stub L362-385, `WorkerThread` L537-1992, `Worker` tool L1998-2887. No separate `worker_thread.py` / `worker_session_lifecycle.py` exist. `worker_registry.py` (152 L); `infra/workspace_lifecycle_manager.py` (804 L) holds `WorkerSupervisor` + `ExecutionTracker` (SOFT_TIMEOUT=300, HARD_TIMEOUT=600, EXEC_KILL_GRACE=10, L69-71). `agent/core/worker_context.py` (213 L) is a lightweight Session stand-in (L39-43).
- `agent/core/agent.py` `process_query` L835-837 is a **synchronous generator**; `agent/core/tool_executor.py` `execute` L341 is single-threaded/blocking. There is no async execution model (A5).
- A1 Spawn: `Worker` tool `required_categories=[]` L2010 (ungated by design, comment L2008-2009); `_action_spawn` L2344-2631; force-respawn across sessions L2353-2389; duplicate-name guard is session-scoped L2392-2406; paused-worker auto-resume + FIFO re-queue L2408-2454; `_WORKER_BLOCKLIST` L396-402 (Worker, EditDockerfile, MCPValidator, CheckSystem, KnowledgeBaseTool); per-tool footprint validation L2475-2510; effective timeout = spawn > worker-def > 600 L2525-2529. Spawn **blocks** on `output_queue.get(SPAWN_QUEUE_TIMEOUT=600)` L2596-2624; on Empty it returns "did not respond within 600s … still alive — query again" L2604-2612 and the thread is **not** killed.
- A2 Thread: daemon thread L565; per-worker `_input_queue`/`_output_queue` L644-645; `_stop`/`_pause`/`_resume` events L646-648; `run()` L1240-1689 creates a per-worker EventBus L1257, registers it L1258, and calls `EventLogger.attach_worker_bus` L1260-1264; main loop L1368-1380 (`while not _stop_event: _poll_command(); _input_queue.get(2.0); None→break`); reply envelope L1587-1628 `{content,status,confidence,meta,telemetry,query_id}` with status forced to "timeout" when `timeout_triggered` L1596-1597; exceptions → status "error" L1631-1660; `finally` L1683-1689 unregisters the bus and saves context.
- A3 Persistence: `_context_path` = `workers/<name>/context.json` L1693-1694; `to_persistable_dict` L185-194 `{session_id, worker_name, turn_count, conversation, total_input_tokens, total_output_tokens}`; `_save_context` L1894-1959 writes tmp + `os.replace` under `session/lock.py` `FileLock` (fcntl.flock non-blocking + poll, 10 s timeout); merged runtime ctx_data L1924-1935; `status.json` via `_write_status_file` L1853-1892 (`{runtime_status,current_task,last_heartbeat,error,session_id,current_context_tokens,max_context_tokens}`).
- A4 Control: `stop` L813-830 (WLM stop if enabled; `_stop_event.set()`; `input_queue.put(None)`); pause L832-850; resume L852-864; `_poll_command` L866-903 (command.json stop/pause/resume, unlink); `_action_stop` L2764-2887 (cross-session stop-all L2775-2830; join budget max(30, `_timeout`) in 2 s steps L2845-2870; force-pop L2865-2870); `_action_query` L2705-2762 (send_query timeout=300; heartbeat age > 600 s → "appears hung" L2742-2756); `_action_check` L2633-2703 (cross-session scan).
- A5 Async model: **not async** — one query in flight per worker; `_run_tool_loop` iterates the synchronous generator at worker.py L1074; `send_query` default wait 120 s L709-742.

## B. Timeout enforcement layers

**VERDICT: GAP** — Layered but inconsistent: agent-internal soft timeout (forced envelope), opt-in WLM hard kill (Docker-unaware), per-tool 30 s Docker exec kill, and a 600 s heartbeat heuristic. No single authoritative timeout for a worker query; defaults differ across layers (120 s / 300 s / 600 s / 30 s).

Evidence:
- `_timeout_seconds` precedence spawn > worker-def > 600 s (worker.py L589-599); `SPAWN_QUEUE_TIMEOUT=600` L55; `_build_agent_config` L909-1014 (timeout L983; `time_warning_threshold=max(5, 80%)` L986-989; max_turns 100 L1007; worker_mode L1010).
- Layer 1 — agent internal time monitor: CRITICAL → `_timeout_triggered` L1219-1231 → envelope status "timeout" L1596-1597; stop-check in agent.py L941-955.
- Layer 2 — WLM (opt-in only): `WorkerSupervisor.process_query` wait_bound = timeout or SOFT_TIMEOUT=300 L496-560; deadline → BUSY→TIMED_OUT + `_terminate_all` (kills tracked docker execs/containers/subprocesses L176-320) + TimeoutError; stale replies discarded by query id.
- Layer 3 — Docker hard kill: `docker_executor.py` `_exec_with_timeout` L678, thread join(30) L704 → `container.kill()` L710 + recreate L715, exit_code -2 L662-664; `infra/container_manager.py` exec L639 (timeout 30), kill L682 + `_drop_container` L685 + TimeoutError L686; `tools/docker_code_runner.py` timeout default 30 L125-128, idle_timeout 600 L169-171. **No 30-minute exec exists anywhere** (30 s default); `EXEC_OUTPUT_LIMIT_BYTES=100KB` L98.
- Layer 4 — heartbeat: query heartbeat age > 600 s → hung (worker.py L2742-2756).
- B3 Wiring gap: WLM flag default OFF L784-800; `WorkerSupervisor(container_manager=None, resource_container_manager=None)` L760-767 — **WLM cannot kill Docker**; `ExecutionTracker.register` only when flag on L1152-1166.
- B4 Mismatch: spawn wait fixed 600 s never preempted; `send_query` 120 s vs `_action_query` 300 s.

## C. Docker-in-worker

**VERDICT: GAP** — Workers can reach Docker through child tools (DockerCodeRunner is not blocked) and get a 30 s exec kill per call, but the WLM cannot kill worker containers externally, containers are workspace-scoped and outlive workers, and there is no session-scoped container reuse in workers.

Evidence:
- `WorkerSupervisor(container_manager=None, resource_container_manager=None)` L760-767 → `terminate_all` cannot touch Docker.
- Only the *tool* surface is gated: `_WORKER_BLOCKLIST` L396-402 blocks Worker/EditDockerfile/MCPValidator/CheckSystem/KnowledgeBaseTool; **DockerCodeRunner is NOT blocked**; `DockerCodeRunner.required_categories=["filesystem:write","container:true"]` L47; spawn-time footprint validation L2475-2510; `tool_executor` injects `session_permissions` + `agent_config` L266-268 / L293-297.
- Containers are workspace-scoped, survive session close, and are swept only by `cleanup_workspace()` (container_manager.py L13-18, L1369-1380; stop timeout=5 L756). Exec timeout path kills the container (container_manager.py L639-686).
- No session-scoped registry reuse in workers (workspace-scoped ContainerManager; per-session config data only, worker.py L293-297).

## D. Core input guard (what enters agent context)

**VERDICT: GAP** — No global max-context hard enforcement. Guards that exist: per-tool `token_limit` truncation, 20k-char summary truncation, and provider `token_limit_exceeded` → emergency retry → hard error. **User query, tool results and worker replies have NO size cap at their append points**; worker replies explicitly bypass the only framework truncation guard.

Evidence:
- D1 Sources: user query appended `agent/core/agent.py:842-848` (no size guard); tool results `tool_executor.py:343-345` truncated via `_truncate_output` unless `skip_output_truncation`; summary as system message `agent.py:1443-1448` (MAX_SUMMARY_LENGTH=20000 chars, marker `... (truncated)` L1493-1496); system prompt `llm_client.py:62-95` (no cap); tool definitions built agent.py:119.
- D2 Budget: computed `agent.py:669-696`; model windows `token_counter.py:63-84` (gpt-4 8192 / gpt-4o 128000 / claude 200000); `session/context_builder.py` SummaryBuilder L215-340 logs token count only L303-315, emergency mode drops oldest 20% non-system L274-278, default keep_turns=5 L210-213 — **no budget enforcement in the builder**; on exceed → LLMError `token_limit_exceeded` → emergency retry `agent.py:1107-1125` → after 2 retries hard error "Context too large. Please summarise manually and retry." L1114-1118; `agent/core/turn_transaction.py:64-135` commits to `session.user_history` with no size caps.
- D3 Tool output: `session/models.py` + `session/event_schema.py` have no truncation caps; `ContainerManager` caps stdout/stderr at 100KB (container_manager.py:98, `_truncate_output` L699-700); KnowledgeBase read has optional `max_tokens` (tools/knowledge_base.py:205-210).
- D4 Worker replies: `Worker.skip_output_truncation=True` (worker.py:2070) → **worker replies bypass the tool-executor guard**; envelope L1599-1629 and `_action_query` response L2734-2741 have no cap.
- D5 Summarization output: entered as a system message (agent.py:1443-1448), 20k-char cap.
- D6 Per-turn injection is only context_builder output (context_builder.py:215-340); worker-mode banner injection point UNKNOWN.

## E. Dual-use tools & read-only enforcement

**VERDICT: PASS (with noted gaps)** — Op-level permission granularity exists for FileEditor, GitInfoTool, KnowledgeBase, Respond; worker context is auto-denied at the security gate; no read-path-bypass of the gate was found; vault paths are blocked in `validate_path`. Gaps: the Worker tool itself is ungated by design; `EditDockerfile` requires a `container:write` category absent from the gate level map (likely deny, UNKNOWN); GitInfoTool executes vault hooks without path validation.

Evidence:
- E1 READ-only: FilePreviewTool, DirectoryTreeTool, PaginateTool, FileSummaryTool, ReadFile, FileSearchTool, SearchCodebaseTool, GlobTool, FieldViewer (filesystem:read); DateTimeTool ([]); CheckSystem (system:read, tools/workspace/check_system.py:116). WRITE-only: CodeModifier (fs:write L22), ApplyEdits (L218), RefactorTool (L66), FileMover (L10), DirectoryCreator (L9), ProgressReport (fs:write L19-21), WorkingDocument (fs:write always, working_document.py:37-39), EditDockerfile (container:write L52), ContainerExecTool (container:true+fs:write, container_control.py:250). DUAL: FileEditor (read/grep→fs:read L16-18, else fs:write L19); GitInfoTool (status/diff/log/branch/show/blame/config→git:read L49; remote→git:read+network L40-41; commit/init→git:write L42-43; clone→git:write+network L44-45; push/pull/fetch/merge/rebase→git:write+network L46-47); KnowledgeBase (read/search/list→fs:read L165-166; append/update/create_domain→fs:write L167-168; status/summary→[] L169); Respond ([] unless report_body→fs:write L51-55); DockerCodeRunner (fs:write+container:true L47); ContainerStart/Stop/Status/List/Build/Logs (container:true L85).
- E2 Flow: `tool_executor.py:241-281` resolve_workspace_id → get_workspace_capabilities → get_effective_permissions → check_required_categories. Gate `security/security_gate.py:88-117` (workspace filesystem_write downgrade L95-98; container = session ∧ workspace.allow_docker L106; system has no workspace cap L111); `check_required_categories` L264-360 (worker `permission_footprint` overrides L279-288; **worker/NullEventBus context denied without prompt** L311-317; "ask" denied by policy without prompt, `check_atomic_operation` L231-260). Workspace caps from `~/.thoughtmachine/workspaces/{id}/capabilities.json` (workspace_capabilities.py:159-166; defaults fully-permissive L94-104). Container mounts enforce read-only: `security_gate.get_expected_container_config` L134-171 → container_manager.py:449-451 (workspace ro/rw), resource manager L513-554.
- E3 Subcommand granularity: `get_required_categories(params)` per call (tools/base.py:82-98); FileEditor/GitInfoTool/KB/Respond override per-op. A read-only worker calling a write subfunction → gate deny without prompt (security_gate.py:311-317) + spawn-time footprint validation (worker.py:2491-2498).
- E4 Gaps: (a) Worker tool ungated at spawn (worker.py:2010, deliberate); (b) EditDockerfile `container:write` category absent from gate level map (banned0/ask1/read2/connect3/write3/full4, security_gate.py:169-227) — behavior inferred deny, UNKNOWN; (c) GitInfoTool vault hooks `~/.thoughtmachine/hooks/` executed without `validate_path` (git_info_tool.py:64-68 — see H1); (d) path confinement in `tools/base.py:315-350` → `thoughtmachine/security.py:315+` `validate_path` (null-byte reject L336-346, vault block L361-370, workspace confinement); KnowledgeBase storage-location confinement UNKNOWN; (e) resource containers bind the linked-worktree main repo rw + `.venv` ro (resource_container_manager.py:528-554) — outside workspace by design.

## F. Container registry

**VERDICT: GAP** — Registry has NO free/busy lease, NO idle/stale reclaim, NO heartbeat; it always creates fresh on request; the per-session limit (default 4) check is **NOT atomic with create (TOCTOU)**; worker/exec and resource containers share the same registry when the feature flag is on, else fully separate legacy paths.

Evidence:
- F1 Chain: ContainerStartTool.execute → `_make_manager` (container_control.py:87) → `ContainerManager.start` (container_manager.py:400+) → limit check L440-446 → if registry active: `registry.request_container` (container_manager.py:563-584 → container_registry.py:230-310) → `create_hardened_container` L278 → `register` L284. DockerCodeRunner: execute L267 → same `start()` (docker_code_runner.py:41, 318). Worker: `WorkerSupervisor.start_container` → `container_manager.start` (workspace_lifecycle_manager.py:702-712, resource request rejected L707-711). Resource: `ResourceContainerManager.ensure_container` (resource_container_manager.py:464+) → `create_resource_container` (L575-580 → container_registry.py:366-455).
- F2 Registry tracks only created/destroyed: `_containers` name→state + `_session_map` + `threading.Lock` (container_registry.py:169-176); register L184-210, unregister L212-228; `request_container` **always fresh-creates — no lease/free-busy concept, no reuse, no pooling**.
- F3 Creation: no pool; per-session limit check container_registry.py:266-272 (max_containers default 4, `_get_max_containers` L459-472, clamped ≥1); legacy per-workspace limit container_manager.py:440-446 + `_get_max_containers` L284-302 (workspace config.json L238-239, default 4); at limit → RuntimeError("Container limit reached") → tool error (container_manager.py:585-588); no wait/queue.
- F4 TOCTOU: container_registry.py:266-272 — `with self._lock: current = len(...)` then lock **released** before `create_hardened_container` L278 and re-acquired at `register` L284 → two concurrent requests can both pass the limit check and both create, exceeding the limit. Legacy path container_manager.py:444 also non-atomic.
- F5 No idle/stale reclaim, no heartbeat anywhere in infra/ (grep hits are only WLM query-id stale-reply draining workspace_lifecycle_manager.py:504, 557, 787-796 and label-based cleanup sweep L36, 1056-1061; DockerCodeRunner closes idle pool containers after idle_timeout=600 s, docker_code_runner.py:169-171).
- F6 Stale-but-running: freed only via exec-timeout kill (container_manager.py:639-686, kill L682 + `_drop_container` L685), `ExecutionTracker.terminate_all` (workspace_lifecycle_manager.py:176-181, 798-803), or the label-based orphan sweep `cleanup_workspace` (container_manager.py:1359-1385, stop timeout 5 + remove force=True). **No proactive orphan reaper.**
- F7 Removed-while-in-use: `cleanup_workspace` force-removes regardless of in-flight exec (container_manager.py:1372-1383); exec fetches container by id L641 and would fail NotFound on removal — **no race guard** between exec and cleanup.
- F8 Same registry when flag on: resource containers via `create_resource_container` (container_registry.py:366-455, type "resource", guard bypass sanctioned L370-374); user containers via `request_container` (type "user"); agent-facing listing hides the resource label (container_manager.py:1056-1061). Registry disabled → fully separate legacy paths (container_manager.py:604-637 direct docker run; resource_manager legacy create L560+).

## G. Multi-worker & cleanup (close/delete/shutdown)

**VERDICT: GAP** — Multi-worker is in-process daemon threads with logical isolation only; shutdown paths exist and are orderly, but four concrete leaks/gaps remain: (1) EventLogger per-worker bus subscriptions never detached (verified accumulating live), (2) registry entries persist after natural finish until an explicit stop/check, (3) no hard kill of threads (daemon abandonment), (4) WLM Docker wiring is a no-op. Containers outlive workers by design.

Evidence:
- Isolation: in-process daemon threads (worker.py L565), own `_input`/`_output` queues L644-645, own EventBus L1257, own lazy Agent+WorkerContext L1388-1412/L1287-1306; registry keyed (session_id, worker_name) → thread AND → EventBus with separate locks (worker_registry.py L27-46); API L75-107; `atexit.register(shutdown_workers)` L43; bridge discovers pre-existing workers on session load (bridge.py L427-443). Isolation is logical only — same process, same GIL.
- Bridge event wiring: `_subscribe_to_worker_bus` L602-765 (~13 event types: tool_call, tool_result, worker_message, assistant_message, context_updated, context_cleared, context_summarized, token_recovery, token_warning, turn_warning, time_warning, user_message, system_notification); `_unsubscribe_worker_bus` L767-783; `_on_worker_completed` L785-793; `_on_worker_error` L795-800 (forward + unsubscribe). Worker `run()` removes the bus from the registry in `finally` (worker.py L1685) but EventLogger has **no detach**.
- Close/delete/shutdown: `bridge.close_session` L1811-1860 (self.stop L1827; join 60 s L1830-1831; save_session L1834; session_manager.close_session L1835; `shutdown_workers(5.0)` L1841-1843; state reset L1850-1860); `server.py delete_session` L1628-1657 (session_store.delete_session L1635; remove_open_session L1636; registry.remove L1639; cached_bridge.stop L1641-1644); close_session command L1749-1768; server shutdown `_shutdown_save` L406-427 + signal handlers L460-480 + lifespan `el.stop()` L364-371; `shutdown_workers` L137-152 (stop + join(5) + compact + save); `bridge.stop` L1210-1222 (+ `_unsubscribe_worker_events` L497-520).
- G4 Findings: (1) **EventLogger bus leak** — `attach_worker_bus` (agent/logging/event_logger.py L98-103) per spawn, no `detach_worker_bus` anywhere; subscriptions persist until `EventLogger.stop()` (server shutdown only; stop() clears L116-140). Live CheckSystem `event_bus_status` showed **57 subscriber types, 15 subscribers each** on token_warning/security_prompt/worker_spawned/worker_status/worker_completed/worker_error/worker_message — consistent with accumulation. (2) Registry entries persist after natural finish until explicit stop/check (lazy cleanup worker.py L2833-2843). (3) No hard thread kill — join budgets 5 s / 30 s / max(30, timeout); daemon threads abandoned. (4) WLM Docker wiring no-op (container_manager=None). (5) Containers outlive workers (workspace-scoped sweep at decommission). (6) Spawn wait fixed 600 s never preempted.

## H. Vault & workspace state

**VERDICT: GAP** — Sound single-host JSON-file design with good host-git hardening and vault-blocking, but three serious residuals: vault hooks execute arbitrary host scripts with no content validation; missing/corrupt vault JSON silently degrades to permissive defaults; config is apply/start-time only (no live reload).

Evidence:
- H1 Host git is de-fanged: every host git call sets `core.hooksPath=/dev/null`, `core.attributesFile=/dev/null`, `diff.external=`, `core.fsmonitor=`, `filter.clean/smudge=`, `diff.textconv=`, `credential.helper=` (git_info_tool.py:397-419); commit adds `--no-verify` (L418-419). Container-mode git does **NOT** disable repo `.git/hooks` (allowed inside container, L486-488) — the container boundary is the only protection. Vault hooks: only pre-commit is honored (L904-907); path `~/.thoughtmachine/hooks/<workspace_id>/<hook_name>` L697-699; runs via SandboxedExecution with required category `git:write` L704-714; non-zero exit → RuntimeError L715-719; denied `git:write` → PermissionError (fail-closed). **No content validation/signature of hook scripts**; agents cannot write hooks (vault blocked, security.py:361-387). `WORKSPACE_BLOCKED_PATH_PREFIXES` includes `.git/config`, `.git/HEAD`, `.ssh`, `.npmrc` (security.py:74-77); `.git/hooks/pre-commit` is explicitly unblocked (comment L292) — repo-hook writes are permitted by design.
- H2 Readers/sources of truth: registry `~/.thoughtmachine/state/workspace_registry.json` (workspace_registry.py:47-50, 137-157, 160-166; missing/corrupt → `{}` + warning; atomic tmp+os.replace save; register ValueError if exists L194-207; `get_default` cached L120-127). `ensure_workspace_dirs` CREATES on demand: capabilities.json fully-permissive default (workspace_capabilities.py:289-294), Dockerfile seed L301-304, domain_allowlist "[]" L307-310, workers.json from templates L313-317, mcp_servers "[]" L320-323; missing caps → permissive default (security_gate.py:45-54); safeguard allowed-set L434-456. Vault workspace config.json: missing → defaults **written to disk** (container_manager.py:270-276); corrupt → in-memory defaults, file untouched; max_containers default 4 L239. **Verified**: the Docker sandbox has no `~/.thoughtmachine` — the vault is NOT mounted into containers; vault writes from sandbox are impossible.
- H3 Config reload: `config_manager.py` has no mtime/cache reload — `load_global_defaults` L95-105 re-reads per call; session config is baked at start via bridge.py:962 `_build_global_agent_config` / `apply_config` (bridge.py:1307). Config is apply/start-time, **not live**.
- H4 Registration: config.json writer is the registration path (server.py migration/startup + workspace_routes.py:120/138); the registry does not write config.json; `ensure_workspace_dirs` never creates it. Session store lives in vault (`~/.thoughtmachine/sessions` or `workspaces/<id>/sessions`; session/store.py:166-175, list/path caches TTL 60 s/5 s).

## I. Windows / no-Docker fallback

**VERDICT: PASS (Windows) / PASS (degrade paths) / GAP (host git fallback surface)** — File locking is cross-platform (msvcrt/fcntl/PID); Docker-unavailable paths degrade to clear "unavailable" errors; host git fallback uses argv-based SandboxedExecution (no shell). Residual: container-mode git runs repo `.git/hooks` (container-only boundary), and the host fallback depends on the hardened git env from H1.

Evidence:
- `infra/container_manager.py:245` `docker.from_env`; `tools/docker_code_runner.py:31-36, 469` DockerException→Exception fallback (import-guard), `_build_image` L379; degrade paths return "unavailable" (tools/workspace/check_system.py:297-302).
- Host git fallback = SandboxedExecution with **argv list (no shell)**, cwd=repo_root, timeout=30, env GIT_PAGER / GIT_CONFIG_SYSTEM=/dev/null (git_info_tool.py:421-446).
- `session/lock.py:31-46` — cross-platform: msvcrt on Windows, fcntl.flock on POSIX, PID-file fallback (docstring L4-6); Windows stable.
- Container status surfaced via CheckSystemTool (tools/workspace/check_system.py:228, 297) and REST (server.py:2431).
- All non-Docker host exec paths are argv-based, no shell interpolation (server.py:109, sandboxed_execution.py run(), tools/container_control.py:45-51).

## J. Summarization failure modes

**VERDICT: PASS (design is synchronous and failure-tolerant) / GAP (no timeout on the in-turn summary step; compaction only at persistence points)** — SummarizeTool itself is NOT an LLM summarizer (26 lines, no LLM call); the agent summarizes itself on its next turn. Summaries enter as system messages with a 20k-char cap; failure is fail-soft.

Evidence:
- `tools/summarize_tool.py` (26 L): `required_categories=[]` L5, `skip_output_truncation=True` L11; `execute()` only collapses newlines — **no LLM call**; pruning is the agent's own next-turn response; tool_executor.py:356-357 tags `tool_type="summary"`.
- `agent/core/agent.py:1315-1335` — summary_text → `_apply_summary_pruning`, yields token_update + single `context_summarized` event L1326, then `continue`s the turn loop; **synchronous in-turn, no timeout** on the summarization step.
- `agent/core/worker_context.py:133-174` `compact_after_summary` keeps leading non-summary system msgs + last summary + everything after; returns False if no summary — callers (worker.py:1485, 1574, 2380, 2815, 2837, 2878) simply proceed to `_save_context`; worker context is compacted only at persistence points, so a crash between SummarizeTool and save can persist ~2× history (acknowledged as "F3").
- Events: `agent/events.py:23-60` EventType (AGENT_START…TURN incl. AGENT_RESPONDED, TOKEN_WARNING, DOCKER_SANDBOX, SECURITY_VIOLATION, FINAL); `agent_responded` emitted agent.py:1293-1309; `agent/presenter/event_processor.py:14-337` routes to state bridge/session lifecycle/GUI (synchronous).
- Evidence capture for a next failure: `provider_raw.jsonl` written by `agent/logging/lifecycle.py:13, 188-274` (`log_provider_event`, 500-char content_preview, `redact_line=True` before write, never raises); EventLogger = `agent/logging/event_logger.py` (started server.py:288, writes `event_log.jsonl`); session transcript jsonl = `session/store.py:166-175`. Final-response-not-shown-in-frontend remains an open symptom to correlate with these logs (root cause not located in this audit).

## K. Bridge/server modularity

**VERDICT: GAP (active maintainability risk, not just debt)** — server.py is 2934 L with a ~1700-line websocket endpoint (L524-2249); bridge.py is 2280 L with ~9 responsibility clusters; the synchronous architecture (single-threaded tool executor, synchronous `process_query` generator, blocking spawn/pause, in-process thread-per-worker) is baked into both and is the primary refactor surface for async/UI work.

Evidence:
- Sizes: server.py 2934; bridge.py 2280; worker.py 2887; agent/core/agent.py 1578; session/store.py 932; config_manager.py 668; agent/events.py 570; event_processor.py 337; session/session_registry.py 163; session/lock.py 175; summarize_tool.py 26.
- bridge.py:203-2280 WebAgentBridge — ws-id cache L146-201; security/worker event subscriptions L291-524; worker-bus forwarding L602-843; session lifecycle start/continue/pause/resume/stop L983-1226; controller restart L1241; config apply L1307-1394; session CRUD L1395-1925; event mapping `_map_and_emit` L2033-2280. server.py: FastAPI app; `websocket_endpoint` L524-2249 (~1700 lines); file/health/container endpoints L2289-2736; frontend serving L2751-2864.
- Coupling: bridge.py has NO FastAPI import (pure layer; threading+queue L68-69); server.py imports FastAPI, subprocess L109, threading L115, deferred CORS L502, signal/atexit L389-390; routers L146-151; `EventForwarder`/`_active_tab_bridges` are shared by both (event_forwarder.py) — the coupling point.
- Sync hotspots for async/UI: worker pause blocks on `_pause_event` (worker.py:1485-1492); EventProcessor is synchronous; controller events are pumped through the bridge; single process, thread-per-session/worker; no queue/worker-process boundary for tool execution.

---

## What is safe now

1. **Host git is de-fanged** — `core.hooksPath=/dev/null`, `--no-verify`, argv-based SandboxedExecution, no shell interpolation (git_info_tool.py:397-446).
2. **Vault is not reachable from containers** — verified in-sandbox: no `~/.thoughtmachine` mount; agent/worker code cannot write the vault.
3. **Worker permission gate** — worker context auto-denied without prompt (security_gate.py:311-317); op-level permission granularity for the dual-use tools; spawn-time footprint validation (worker.py:2475-2510, 2491-2498); `_WORKER_BLOCKLIST` prevents nested workers/EditDockerfile/CheckSystem/KB.
4. **Path confinement** — null-byte reject, vault block, workspace confinement (security.py:315-370); read-only enforcement also via container mounts (security_gate.get_expected_container_config L134-171 → container_manager.py:449-451).
5. **Container exec hard timeout** — 30 s default → container.kill() + recreate (container_manager.py:639-686); 100KB output cap; idle pool close at 600 s (docker_code_runner.py:169-171).
6. **Atomic persistence** — tmp + os.replace under cross-platform FileLock (fcntl/msvcrt/PID, session/lock.py:31-46).
7. **Timeout layering** — agent-internal monitor → envelope "timeout"; WLM (opt-in) `terminate_all`; per-tool 30 s Docker kill; >600 s heartbeat hung detection.
8. **Log redaction** — EventLogger/lifecycle redact before writing provider_raw.jsonl (lifecycle.py:188-274); logging never raises.

## What must be built before async/UI

1. **Async execution core** — `process_query` synchronous generator (agent.py:835-837) + single-threaded `tool_executor.execute` (L341) + blocking spawn (`output_queue.get` 600 s, worker.py:2596-2624) + blocking pause (`_pause_event`, worker.py:1485-1492) — the whole stack must become event-driven or process-isolated before concurrent UI/async features are safe. Threads cannot be hard-killed today (daemon abandonment); cancellation requires cooperative checks or process boundaries.
2. **Global context budget enforcement** — budget computed (agent.py:669-696) but never enforced; today it relies on provider error → emergency retry → hard error (agent.py:1107-1125). Proactive threshold-based summarization is required, plus size caps on user query and worker replies (remove/bypass the `skip_output_truncation=True` escape at worker.py:2070).
3. **Container registry hardening** — atomic limit check-and-create (F4 TOCTOU, container_registry.py:266-284); lease/free-busy tracking; idle/stale reaper; race guard between `cleanup_workspace` and in-flight exec (container_manager.py:1372-1383).
4. **WLM Docker wiring** — pass real `container_manager`/`resource_container_manager` into `WorkerSupervisor` (currently None, worker.py:760-767) so external termination of worker containers works.
5. **EventLogger bus lifecycle** — add `detach_worker_bus` (event_logger.py:98-103) to stop per-worker bus/subscriber accumulation (57 subscriber types, 15 subscribers each observed live).
6. **Vault hooks validation** — content/signature validation or an explicit opt-in for `~/.thoughtmachine/hooks/<workspace_id>/` (git_info_tool.py:697-719); today they are arbitrary host code protected only by vault write-blocking.
7. **Permissive-default fail-soft** — missing/corrupt capabilities.json silently yields fully-permissive defaults (workspace_capabilities.py:289-294, security_gate.py:45-54); should fail closed or log loudly.
8. **Monolith split** — server.py `websocket_endpoint` (~1700 L, L524-2249) and bridge.py (2280 L) must be decomposed before async/UI work; single-event routing bugs currently hit every session.

---

## Top gaps (ranked, cross-section)

1. **Registry TOCTOU** — per-session limit check-and-create not atomic; concurrent requests can exceed max_containers (container_registry.py:266-284).
2. **No size caps on user query / worker replies**; `Worker.skip_output_truncation=True` bypasses the only framework truncation guard (worker.py:2070).
3. **No global max-context enforcement** — per-tool token_limit + post-hoc emergency retry → hard error after 2 retries (agent.py:1107-1125).
4. **Vault hooks = arbitrary host executables**, zero content validation (git_info_tool.py:675-720).
5. **Missing/corrupt capabilities.json → fully-permissive default** (workspace_capabilities.py:289-294 + security_gate.py:45-54).
6. **EventLogger per-worker bus leak** — no detach; verified 57 subscriber types / 15 subscribers accumulated (event_logger.py:98-103).
7. **WLM Docker wiring no-op** — `WorkerSupervisor(container_manager=None)`; no external kill of worker containers (worker.py:760-767).
8. **No idle/stale/heartbeat container reclamation** — registry always fresh-creates; orphans swept only at workspace cleanup (container_registry.py:230-310; container_manager.py:1359-1385).
9. **Container-mode git permits repo `.git/hooks`** — container boundary only, no `--no-verify` (git_info_tool.py:486-488).
10. **`cleanup_workspace` force-removes containers with in-flight execs** — no race guard (container_manager.py:1372-1383).

---

*Appendix — audit coverage: A worker lifecycle & timeouts; B timeout enforcement layers; C docker-in-worker; D core input guard; E dual-use tools; F container registry; G multi-worker & cleanup; H vault & workspace state; I Windows/no-Docker fallback; J summarization failure modes; K bridge/server modularity. Read-only; only this file was added.*
