# Audit: Logging, Testing Infra, CheckSystem Introspection, Container Architecture

**Date:** 2026-08-13
**Branch:** feat/workspace-panel @ commit c793834 (HEAD; working tree includes small uncommitted changes — not part of this audit)
**Scope:** read-only audit. Evidence is file:line on the working tree.

---

## A. Logging / output inventory

### A.1 Logging-library mix
| Mechanism | Where | Notes |
|---|---|---|
| Custom `_AgentLogger` (`from agent.logging import log`) | dominant across `agent/`, `tools/workspace/worker.py` | `agent/logging/__init__.py` L38-46 (`LogLevel`, `LogCategory`), L95 `_AgentLogger`, L728-733 `create_logger` (max_file_size_mb default 10) |
| stdlib `logging` | `agent/core/message.py` L14; `agent/knowledge/global_kb.py` L24; `agent/config/config_manager.py` L12; `tools/file_summary_tool.py` L9; `tools/read_file_tool.py` L19; `tools/mcp_client.py` L34; `tools/mcp_manager.py` L17; `tools/workspace/check_system.py` L104; `tools/workspace/worker.py` L43 (import fallback when `agent.logging` unavailable); `agent/logging/__init__.py` L157 `python_logging.getLogger(f'agent_{session_id}')` | **No `basicConfig`** anywhere in `agent/`. No `RotatingFileHandler`/`TimedRotatingFileHandler`/`maxBytes`/`backupCount` matches in `agent/`. Manual stdlib handler setup in `session/history_provider.py` L41-44 (`handlers=[]`, `addHandler`, `propagate=False`, `setLevel(WARNING)`) |
| `print()` / `_safe_err_print` / `_safe_stderr_print` | see A.2 | all error-path / CLI / debug-gated |
| **loguru** | **ZERO matches** in `agent/` (50 files), `tools/` (40 files), `llm_providers/` (7 files), `docker_executor.py`, `thoughtmachine_entry.py`, `live_smoke_docker.py` | loguru is NOT used anywhere in the Python codebase |

### A.2 print() call sites
- `agent/startup_health_check.py` L15 (docstring example only), L330 (CLI `__main__`).
- `agent/logging/unified.py` L375 (invalid log level → stderr), L548 (`console_msg` → stderr, guarded try/except), L589 (LOGGING ERROR forward failure → stderr).
- `agent/logging/debug_log_adapter.py` L125, L155-156 (CLI `analyze`/`migrate`).
- `agent/logging/__init__.py` L11-20 `_safe_err_print` (print → stderr, guarded), called at L194 (open-log failure), L223 (lock timeout), L248 (rotation failure), L256 (write failure), L733.
- `agent/cli/rag_commands.py` L36,41,45,50,51,54,55,59,61,62,70,72,78,81,123,128,132,138,139,145,150,153 (CLI progress/errors; several `file=sys.stderr`).
- `tools/test_audit.py` L228-235 (CLI).
- `tools/base.py` L22-31 `_safe_stderr_print`; guarded stderr prints at L140,158,176,194,212,239 under `THOUGHTMACHINE_DEBUG == 1`.
- `tools/workspace/check_system.py` L509, L511 — inside the *heredoc probe script string* passed to `manager.exec()` (Docker-side output, not host stdout).

### A.3 Agent session-log files (`_AgentLogger`)
- `agent/logging/__init__.py`:
  - L124-126: `log_dir` defaults to `~/.thoughtmachine/logs` (config/env overridable), `os.path.abspath`.
  - L153-155: `os.makedirs(log_dir)`, `_cleanup_old_logs(log_dir)`, `_prune_logs_by_size(log_dir)` at init.
  - L156: file = `agent_{session_id}.jsonl` (jsonl) or `.log`.
  - L191: `open(self.log_file_path, 'a', encoding='utf-8')` (guarded L189-195; failure → `enable_file_logging=False`).
  - L218-262 `_write_jsonl`: lock acquire timeout 5s (L221-223); size-based rotation at `max_file_size_bytes` (default 10 MB via `create_logger` L728-733): close, `os.replace(log_file_path, archived_path)` with timestamp suffix, reopen, `_prune_logs_by_size(log_dir)` (L236-246); fallback write `agent_fallback.jsonl` (L257-261).
  - L179-188 `_initialize_logging`: stdlib `py_logger` gets only a `NullHandler` so nothing leaks to stderr via `lastResort`.
- **Retention:** size-based only (`_prune_logs_by_size` + `_cleanup_old_logs`); no time-based retention; rotation archives with timestamp suffix (not numbered).

### A.4 Event log (`event_log.jsonl`)
- Single writer: `agent/logging/event_logger.py` `EventLogger`:
  - L53-58: path = `~/.thoughtmachine/logs/event_log.jsonl` (always vault path; `workspace_path` param is vestigial — "always use vault path" comment L53).
  - L64-73: `start()` spawns a daemon writer thread; `subscribe_all` (L90-93) subscribes to every `EventType` on the global bus; `attach_worker_bus` (L95-101) subscribes per-worker buses.
  - L76-88 `_maybe_rotate`: >10 MB → shift `event_log.jsonl.2 → .3`, `.1 → .2`, current → `.1` (3 backups).
  - L141-166 `_writer_loop`: queue-drained, `json.dumps(record, default=str)` per line, flush per line.
  - L168-190 `get_tail(n=20)` reads back the file.
  - Attached to worker buses in `tools/workspace/worker.py` L1261-1262 (`EventLogger.instance().attach_worker_bus(self.worker_name, self._event_bus)`).
- CheckSystem `event_log` query tails it via subprocess `tail -n 50` (see C.9).

### A.5 Direct file writes for logs/diagnostics (outside the logger classes)
| Path | Writer | Evidence |
|---|---|---|
| `<repo>/logs/agent_*.jsonl` (e.g. `logs/agent_20260812_162747_339.jsonl`, 3.4 MB — ~50 files) | `_AgentLogger` when `log_dir` resolves to CWD `logs/` | actual files present in repo root `logs/` dir (evidence of non-vault log output at runtime) |
| `<repo>/tmp_2.log`, `tmp_nsm.log`, `tmp_wi.log` | scratch debug logs | present at repo root |
| `status.json` (worker dir) | `WorkerThread._write_status_file` `tools/workspace/worker.py` L1861-1885 (tmp+`os.replace`, L1882-1885) | worker persistence |
| worker context JSON (worker dir) | `WorkerThread._save_context` `tools/workspace/worker.py` L1925-1951 | persists `WorkerContext` + status fields |
| session files `{sanitized}_{short_id}.json` | `session/store.py` `FileSystemSessionStore` L87-91 | session persistence |
| `~/.thoughtmachine/state/session_registry.json` | `session/session_registry.py` L23 | registry |
| `*.lock` alongside targets | `session/lock.py` L59 `FileLock` (fcntl) | lock files |
| working documents JSON | `tools/workspace/working_document.py` (`.thoughtmachine/working_docs/`) | tool |
| `~/.thoughtmachine/workspaces/<id>/container_notes.json` | vault bulletin board for sticky notes (referenced in `tools/container_control.py` docs, `ContainerListTool` L409-457) | container notes |

**UNKNOWN (A):** exact `log_dir` override path used when `logs/agent_*.jsonl` lands in the repo CWD (config key or env var) — needs `git log`/runtime config check; `_worker_dir` concrete location for `status.json`/context.json (vault vs session dir) — worker.py lines referencing `self._worker_dir` not yet resolved to absolute path.

### A.6 Event-bus emitters
- `agent/events.py`: `EventType` enum L23-85; typed events L137-345; `create_event`/`convert_from_legacy_format` L350-356; `EventBus` L373 (publish ~L400s, `publish_dict` L413-416); `NullEventBus` L525 (ask()→"deny").
- `agent/controller/__init__.py` L529-536: legacy events converted and `global_event_bus.publish(typed_event)` (best-effort try/except).
- `agent/presenter/gui_integration.py` L66-95: PyQt-style signal emitters (`state_changed`, `tokens_updated`, `context_updated`, `status_message`, `error_occurred`, `config_changed`, `conversation_changed`).
- `tools/workspace/worker.py`:
  - `WorkerBusAdapter._publish` L349-359 (per-worker `_event_bus.publish`, source="worker").
  - Worker loop publishes to `global_event_bus`: `worker_spawned` L1282; status events L1434, L1476, L1517, L1558, L1658, L1680 (worker_status/worker_completed/worker_error-style envelopes with `max_context_tokens` etc.); `_publish_event` helper L1990 (per-worker bus).
- **UNKNOWN (A):** which module publishes `TOKEN_WARNING`/`TURN_WARNING` events (`agent/core/state.py` vs `agent/core/agent.py`) — `create_token_warning_event` exists at `agent/events.py` L369 but the emitter site was not yet traced.

---

## C. CheckSystem implementation + per-field data source

File: `tools/workspace/check_system.py` (750 lines). `class CheckSystem(ToolBase)` L111; `required_categories=["system:read"]` L116; `skip_output_truncation=True` L131; vault allowlist (`get_checksystem_allowlist`) L155-172, enforced for **all** queries L206-218 (`worker/<name>` checks base `workers`); handler map L226-240.

| Query | Source type | Evidence (check_system.py) |
|---|---|---|
| `effective_permissions` | **live compute**: `security_gate.get_effective_permissions(SessionPermissions, WorkspaceCapabilities)` + **disk**: vault `capabilities.json`; fallback raw `session_permissions` | L261-295 (caps file L266-271; gate L274-284; `source: "gate"` L294) |
| `container_status` | **Docker API** via `docker_executor.get_container_status(workspace_path)` | L297-310 (import L56) |
| `containers` | **Docker API**: `ContainerManager.list_containers()` (scoped by `thoughtmachine.workspace_id` label) + `manager.container_summary()` for running ones; notes from vault bulletin board file (**disk**) | L312-366 (permission gate L327-329; manager L337-343; live summary L358-364) |
| `workspace_info` | **disk**: vault `<ws>/config.json`, `workers.json`, `mcp_servers.json` | L368-410 |
| `my_config` | **live memory**: `agent_config` injected by ToolExecutor; api_key redacted `***` (L418, L434); raw_config redaction L435 | L412-439 |
| `network_diagnostics` | **live Docker**: `ContainerManager.start()` throwaway probe container `tm-net-diag-*`, `manager.exec(probe_cmd, timeout=30)`, `manager.stop()` in finally; image-presence check no-pull | L441-537 (probe heredoc L504-513; exec L514; cleanup L531-536) |
| `workers` / `worker/<name>` | **disk**: vault `workers.json`; fallback scan `~/.thoughtmachine/workspaces/*/workers.json` | L541-579, L656-682 |
| `running_workers` | **live memory**: in-process `WorkerRegistry` dict `(session, name) -> WorkerThread`; fields `status/current_task/last_heartbeat/error/alive/conversation_length (len(_worker_ctx.user_history))/elapsed_seconds (_last_elapsed())` | L581-601 (registry import L84-93) |
| `capabilities` | **live memory** (`agent_config`) + **host** (`shutil.which` docker/git) + **disk** (`capabilities.json` token limits) | L603-654 |
| `dockerfile` | **disk**: vault `<ws>/Dockerfile` | L684-695 |
| `event_bus_status` | **live memory**: `global_event_bus._subscribers` / `_wildcard_subscribers` (reads internals under `_lock`) | L697-710 |
| `event_log` | **disk**: `EventLogger.instance().file_path` + subprocess `tail -n 50` (timeout 5) | L712-737 |
| `mcp_servers` | **disk**: vault `mcp_servers.json` | L739-750 |

### C.1 Stale disk reads (identified)
- `capabilities.json`, `config.json`, `workers.json`, `mcp_servers.json`, `Dockerfile` are read fresh on each query (no caching) — staleness risk only if the files are written by an external process (workspace registration) while the session runs.
- `event_log` tails the JSONL file — includes the in-memory queue's *not-yet-flushed* events only after the writer thread flushes (async writer thread L141-166; potential lag).

### C.2 Where pruning-cycle-count / prune-since-last-query could be sourced
- **Not exposed anywhere today.** `CheckSystem._query_running_workers` (L581-601) exposes only status/current_task/last_heartbeat/error/conversation_length/elapsed_seconds.
- Pruning logic lives in `session/history_pruner.py` (`PruningPolicy` dataclass L39) and `ConversationManager` (`agent/core/conversation_manager.py` L13); worker pruning counters would have to be added on `WorkerThread` (`tools/workspace/worker.py` L~601-609 telemetry fields `_tool_call_count` etc.) or in `WorkerSupervisor` (`infra/workspace_lifecycle_manager.py` L323). **No existing counter for prune-cycle count or prune-since-last-query found.**
- `_AgentLogger` has `CONVERSATION_PRUNE` log event type (`agent/logging/__init__.py` L93 mapping) — prune events are *logged*, so a counter could be derived from the log/event stream, but no numeric field exists.

### C.3 WLM / WorkerSupervisor introspection
- `infra/workspace_lifecycle_manager.py`: `WorkerState` enum L83-89; `ExecutionTracker` L114 (tracks in-flight docker-exec/subprocess/scoped-container executions for termination); `WorkerSupervisor` L323 (state machine + correlation-ID queue; `query_handler` assigned post-construction). Supervisor fields not yet enumerated in detail (see D.5).
- WLM is gated by session config `use_workspace_lifecycle_manager` (`_get_session_config` L96-111).

---

## Section B — Testing Infrastructure

### B.1 Fake-LLM / deterministic multi-turn tool-call tests

**EXISTS.**

- `tests/mocks/puppet_agent.py` — `PuppetLLM`: scenario-based fake (types `assistant`/`tool_call`/`respond`), pops ONE turn per `chat_completion`; deterministic; no built-in tool-loop / token-warning / pruning simulation. Consumed by `tests/test_puppet_agent_basic.py` (`test_assistant_replies_directly` L20, `test_tool_call_then_respond` L34 — real `Agent` + `PuppetLLM`, no real tool execution).
- `tests/test_worker_agent_transplant.py` — `ScriptedProvider` (canned `LLMResponse` list in `config._provider_responses`): real `Agent`+`WorkerThread` multi-turn tool loops (`smoke_multi_turn_task` L167 — Thought call then text; `timeout_enforces_restrictions` L349 — 10 tool-call turns; `stop_flag_graceful_exit` L518).
- `tests/test_worker_loop_spike.py` — `EchoToolProvider` tool-call loop (`process_query_with_tool_call` L278).

### B.2 Async worker timeout: main continues / worker-alive / stale queue / correlation id

**EXISTS, deterministic (mock-based; one real-docker test).**

- `tests/test_worker_timeout_audit.py` (`_RunSafetyPatches`, `make_fake_agent("CRITICAL","timeout")`, tempdirs; no docker): soft-timeout envelope L134; spawn auto-query 600s cutoff `"did not respond within 600s"`+`"still alive"` L174; query-action timeout `is_alive=True` branch L201; force-respawn queues NEW query not old L251; dedupe identical initial context L296; two respawns bounded growth L327; stale query re-put at TAIL L389; stop-path persists uncompacted summary L416; context-doubling prevented by truncation L447; generation guard L490–L549; stop-path compacts L584; WLM flags: query_id envelope on/off L676/L699, pause/resume/stop/prune delegation L717–L750, prune falls back to F1 L783.
- `tests/test_workspace_lifecycle_manager.py` (pytest + `FakeContainerManager`; one real-docker test L600 `wlm_integration_real_docker`): state flow L132; query_id restore on pause/resume L168; discards stale reply L260; wrong query_id times out L271; busy raises L285; auto-resume L292; no-handler timeout L303; soft warning emitted once L522; drain prefix L542; abandoned-on-timeout L552; TTL bounded to 2 L561; real-docker E2E L600 (overrun subprocess killed, stale reply drained, back to IDLE).

### B.3 Worker context pruning / summarisation tests

**PARTIAL.**

- `tests/test_history_pruner.py` — 45 pure-unit deterministic tests (no LLM/threads): summary indices L103–L115; turn grouping L136–L164; pruning policies (two-summary L213/L234, final-tool turns L257/L277, no-final fallback L308–L398, system/notifications passthrough L420–L514, assistant-started turns L562–L611, seq numbering L651, multiple summarizations L665–L705, empty old region L723–L736, idempotency L777, FINAL_TOOL_NAMES const L808).
- In-loop summarisation: `tests/test_worker_agent_transplant.py::token_critical_triggers_summarisation` L432 (warning_threshold=10/critical=50 → critical + summarization); `TestCompactAfterSummary` L574–L740; `tests/test_worker_loop_spike.py::summary_and_updated_at_updated` L339.
- **GAP:** no prune-cycle-count / prune-since-last-query tests (fields do not exist in code — see C.2), and no pruning exercised inside a live worker loop.

### B.4 Container separation — worker cannot request resource containers

**EXISTS at unit level.** `tests/test_workspace_lifecycle_manager.py::request_container_rejects_resource_requests` L401 (PermissionError for `RESOURCE_IMAGE_TAG`, name `tm-res-abc123def456-git`, image+name combos, bare tag); `tests/test_container_registry.py::request_resource_guard` L377 (`"Resource container access denied"`); registry delegation in `tests/test_registry_wiring.py` L214–L370. **No real-docker E2E proving separation** (only WLM L600 touches docker at all).

### B.5 Git resource-container parity (worktree/permissions/hooks/network)

**MISSING.** Glob-confirmed absent: `test_git_container_sandbox.py`, `test_resource_container_worktree.py`, `docker/` dir, `docker_integration/` dir. Only the name-format guard (`tm-res-*`) is tested (B.4).

### B.6 CheckSystem introspection tests

**PARTIAL.** Allowlist enforcement IS tested in `tests/integration/test_vault_hardening_end_to_end.py::TestCheckSystemAllowlist` L79 (allowed path L82, disallowed L103, traversal L121, tampering detection L139 — vault-gated `checksystem_allowlist.json` + sha256). `running_workers` query in `tests/tools/test_workspace_tools.py` L477. **Absent:** `test_checksystem_gate.py`, `test_checksystem_allowlist.py`, `tools/test_check_system.py`; no tests for gate denial, live-state field detail, pruning counts, thresholds, container counts.

---

## Section D — Container Architecture Traces

### D.1 Free-use (worker/main) container creation path

- `tools/container_control.py`: `_ContainerControlBase._get_manager` (~L110-124) instantiates `ContainerManager(workspace_path, session_id, workspace_id, session_permissions, image, mem_limit, cpu_quota)` directly. Tool classes: `ContainerStartTool` L131, `ContainerExecTool` L222, `ContainerStopTool` L321, `ContainerStatusTool` L369, `ContainerListTool` L409, `ContainerBuildTool` L460, `ContainerLogsTool` L508. All registered in `tools/__init__.py` L135-149 (`DockerCodeRunner` registered L129-130 from `tools/docker_code_runner.py`).
- `infra/container_manager.py`: `class ContainerManager` (class def ~L191); `start()` ~L396+ — reuse order: in-memory registry → label lookup (`_find_by_labels` L494) → fresh create; labels `{"thoughtmachine.container_name": name, "thoughtmachine.workspace_id": self.workspace_id}` L541-544; with registry active delegates to `registry.request_container` L553-570 (passes labels, env `PYTHONUSERBASE=/home/agent/.local`, workspace bind ro/rw, mem/cpu, oom 1000); `max_containers` from workspace config L238.
- Worker tool filtering: `tools/workspace/worker.py` `_WORKER_BLOCKLIST` L396-402 = `{Worker, EditDockerfile, MCPValidator, CheckSystem, KnowledgeBaseTool}` — comment L390-393 claims container management reserved, but container tools (`ContainerStartTool` etc.) are NOT in blocklist by name (only `EditDockerfile` — "container configuration" — is). `_build_agent_config` L953-964: `enabled_tools = worker_tools minus blocklist`, else `SIMPLIFIED_TOOL_CLASSES` names minus blocklist, then intersected with parent `enabled_tools` (worker cannot exceed parent); permission footprint merge `_restrictive_merge(session_permissions, footprint)` L994 (session is ceiling). Tests: `tests/tools/test_workspace_tools.py` `test_spawn_strips_by_footprint` L1033 (filesystem:read footprint denies FileEditor), `test_spawn_strips_blocklisted_tools` L1078 (Worker stripped, FileEditor/DateTimeTool kept).

### D.2 Resource container creation path

- `infra/resource_container_manager.py`: `RESOURCE_IMAGE_TAG="tm-resource-git"` L96 (vault-managed Dockerfile at `vault_root()/docker/resource`, L97-100; auto-build single-flight L166-207, `_RESOURCE_IMAGE_READY` cache L111-114); `class ResourceContainerManager` L358; container_name = `tm-res-<sha256(workspace_path)[:12]>-git` L443-453; labels L455-461 (`thoughtmachine.workspace_id` + `thoughtmachine.resource=git` + `thoughtmachine.container_name`; constants L390-393); `ensure_container` L464: image ensure L481-486, reuse by workspace_id label filter L490-505, else create.
- Legacy create path L591-609: rw `/workspace` bind L513-519 (documented divergence from executor ro+tmpfs-shadow), linked-worktree main-repo bind (rw) L528-539 via `_resolve_worktree_main_repo` L258 (validates gitdir pointer, refuses vault/root/self paths L312-325), `.venv` bind READ-ONLY L545-554 via `_resolve_venv` L238, tmpfs `/tmp` rw,noexec,nosuid,64m + `/home/agent` rw,exec,256M,uid=1000 L563-566, `cap_drop ALL`, no-new-privileges, `oom_score_adj=500`, `read_only=True`, user 1000:1000, `tail -f /dev/null`, mem/cpu quotas, labels.
- Registry delegation L568-589: `registry.create_resource_container(session_id, workspace_id, network_mode, workspace_path, name, mounts=extras[1:])` — `/workspace` bind always added by registry (rw).
- `exec` L640 (raw argv list, NO `/bin/sh -c` wrapper, `exec_run(list)`); stop L710; remove L730 (stop timeout=5, force remove).

### D.3 Where workers can request containers

- Only via `enabled_tools` filtering in `Worker._build_agent_config` (worker.py L953-964) + blocklist + footprint + parent intersection. Container tools are in `SIMPLIFIED_TOOL_CLASSES` by default so NOT stripped unless parent `enabled_tools`/blocklist excludes them — nuance: blocklist comment says "manage containers" reserved but only `EditDockerfile` is blocked by name.
- `WorkerSupervisor.request_container` (infra/workspace_lifecycle_manager.py L693-735) delegates to `container_manager.start` (L735) or `registry.request_container` (L729-734) when registry active.

### D.4 Can any worker path request a resource container?

NO — guards:
- `WorkerSupervisor.request_container` raises `PermissionError` "Resource containers (git, tm-res-*) are reserved for the main agent and cannot be requested by a worker." via `_is_resource_container_request` check L707-711.
- `ContainerRegistry.request_container` resource guard (tests/test_container_registry.py `request_resource_guard` L377 — `PermissionError` "Resource container access denied" for `RESOURCE_IMAGE_TAG` / `container_type="resource"` / name `tm-res-*`).
- WLM tests `request_container_rejects_resource_requests` L401-411.
- Hidden from listing: `ContainerManager.list_containers` skips `thoughtmachine.resource`-labeled containers L1036-1037; `check_system` is blocklisted for workers anyway.

### D.5 Label filtering for container listing

- `ContainerManager.list_containers` L1010-1068: `client.containers.list(all=True, filters={"label": "thoughtmachine.workspace_id=<ws>"})` L1023-1026; skips containers with label `thoughtmachine.resource` L1036-1037; returns dicts with EXACTLY `container_id`/`name`/`image`/`status`/`uptime_seconds`/`workspace_id`/`note` (note from vault bulletin board `container_notes.json`, not docker labels) L1057-1067.
- `tools/workspace/check_system.py` `_query_containers` L312-346: delegates to `ContainerManager.list_containers` (L343), note from `<vault_root>/workspaces/<ws_id>/container_notes.json` L315-319.
- `ContainerListTool` (container_control L409): "Queries the Docker daemon for every container (running or not) carrying this workspace's `thoughtmachine.workspace_id` label".

### D.6 ContainerManager vs ResourceContainerManager

- DISTINCT classes, distinct instances: `ContainerManager` = per-session free-use manager (infra/container_manager.py ~L191); `ResourceContainerManager` = per-workspace git sandbox (resource_container_manager.py L358) with its own module-level image cache/lock (L111-114).
- Both use `docker.from_env()`; both optionally delegate to the SAME `ContainerRegistry` singleton via `infra.registry_wiring.get_active_registry(session_config)` (container_manager L86-87 + L553; resource_container_manager L106-109 + `_registry_active` L424-433 + `_registry` L435-440).

### D.7 ContainerRegistry

- `infra/container_registry.py` (class ~L185): used by BOTH managers when feature flag on — `ContainerManager.start` → `registry.request_container` (L553-570); `ResourceContainerManager.ensure_container` → `registry.create_resource_container` (L575-589).
- Registry enforces: resource guard, container limits (`DEFAULT_MAX_CONTAINERS` / session config `container_limits.max_containers` — tests L385-394), profiles (user=1000, resource/mcp/proxy=500 oom, network none/bridge), hardening.

### D.8 Docker exec termination

- `ExecutionTracker` (infra/workspace_lifecycle_manager.py L114): `kill_grace_seconds = EXEC_KILL_GRACE = 10` (L71, L124); `register` L126; `terminate_all` L179+: docker_exec → `exec_stop(container_id, exec_id, timeout=EXEC_KILL_GRACE)` when manager exposes it (L231-238) else `container_manager.stop(container_id)` (L239-245; REAL `ContainerManager` has NO `exec_stop` — docstring L18-21, L240); on failure force remove via `remove()` (tests L335); scoped_container → stop (test L344); subprocess → `os.killpg(pid, SIGTERM)` fallback `os.kill(pid, SIGTERM)` L275-283, escalation `_escalate_subprocess_kill` L289-316 (poll `killpg(pid,0)` every 50ms up to grace, then `killpg` SIGKILL, fallback `os.kill` SIGKILL). Tests: `terminate_all_docker_exec_with_exec_stop` L314, `_falls_back_to_stop` L326, `_stop_failure_removes` L335, `_scoped_container_stops` L344, `_subprocess_killpg_fallback_kill` L353, `_unknown_type_skipped` L375.
- `docker_executor.py` (repo ROOT, NOT infra/): `class DockerExecutor` L355 (shared `_run_image_build` helper with `ContainerManager.build_image` L59-64); `execute` L639; `_exec_with_timeout` L678: `exec_run` in daemon thread L693/699-701, `join(timeout)` L704, on timeout `container.kill()` + `container=None` + `_ensure_container()` recreate + raise `TimeoutError` L706-716; `execute` returns exit_code -2 on timeout L663-664; `close` L668-676 (`container.stop()` + `remove()`). NO exec_id-based kill — whole-container kill. `verify_container_integrity` matches only `agent-exec-` names (resource `tm-res-*` excluded, resource_container_manager L15-17).
- Resource container exec: `exec_run(list, no shell)` L640; stop L710 / remove L730.
