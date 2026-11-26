# Design: resources — ceiling, grant, gate, audit

**Status:** design document, decision-driven. Branch `docs/design-resources`, base `dev` `b7084c44`.
**Consistency obligation:** every structural claim here must agree with `docs/evidence/resources-ceilings.md` (316 lines) and `docs/evidence/resources-bindings.md` (621 lines). Those two files are MEASURED readings of the code; where this document disagrees with them, this document is wrong and must be corrected.
**Evidence base:** the two inventories are tracked beside this document; the `.scratch/resources/` copies are working notes and may rot.
**Legend:** `MEASURED` = the code was read and is quoted with a `path:line` anchor. `INFERRED` = reasoning from names, docstrings or adjacent code, not from control flow. `UNMEASURED` = not establishable from this repository. A sentence without an anchor or one of those two words is a rumour and does not belong in this file.

---

## 1. What a resource is

A **resource is capacity that is consumed**. It is not a capability: a capability answers *may this agent do X* and is called a permission grain; a resource answers *how much of X is left* and is called a ceiling. The two meet in exactly one object — a credential, which is simultaneously the right to use a secret and a quota over time, rate and lifetime. That is why credentials are the first band of this work and not a later one (section 7).

Four nouns and two verbs, used with these meanings everywhere below.

**Template** — the declared shape of a capacity: name, unit, ceiling, who owns the ceiling, and what happens when it is exhausted. A template is immutable once shipped; changing it produces a different template, not an edit. This is the rule the container already obeys ("the container envelope is its birth certificate, not a setting" — `scripts/gen_handoff.py:218`, MEASURED).

**Binding** — a template materialised against something concrete: this worker's wall-clock budget, this session's MCP connection, this container's network and workspace mode, this model endpoint's key. A binding is a live reference with a lifetime of its own.

**Grant** — the allowance actually in force for one use, derived ceiling -> template -> grant. A grant may narrow a ceiling. A grant may never widen one, and no code path may construct a grant from anything but a template.

**Gate** — the check the consuming code performs at the moment of use, against its own inputs, immediately before spending.

**Audit** — the record written when a grant is made, a ceiling is reached, a use is refused, or a binding is revoked. A structured record with a stable shape, not a log line the model reads.

The invariant under all of it, inherited from the charter and not negotiable per surface:

> **No snapshot of authority may outlive the moment it was true.**

In resource terms that is two sentences, and every defect in section 2 is one of them:

1. no capacity may be spent against a declaration that has since changed;
2. no binding may be used after the moment it died.

---

## 2. Evidence

Each row below is transcribed from `.scratch/resources/ceilings.md` and `.scratch/resources/bindings.md`, and a row without a `Source` is a rumour; because ceilings.md answers five questions and bindings.md six, column (6) quotes bindings.md where it answers and is derived from the READ/ENFORCED anchors where it does not.

| Surface | (1) declared once? | (2) owner | (3) reader == reporter | (4) enforced at | (5) record on fire | (6) spine | R-verdict | Source |
|---|---|---|---|---|---|---|---|---|
| 1. Wall-clock turn budget | No — five independent 600 s literals with no import edge: `thoughtmachine/timebudget.py:67`, `agent/config/defaults.py:33`, `:43`, `:61`, `:131`; 300 s three ways: `thoughtmachine/timeout_constants.py:28`, `:30`, `agent/config/defaults.py:130`; and one config key with two defaults — `agent/config/models.py:223` (300) vs `tools/workspace/check_system.py:647` (600) | `thoughtmachine.timebudget.DECLARED_CEILING` is a real import target, but the prose owner is a STRING — `CEILING_OWNER = "agent.config.defaults.HARD_TIMEOUT (thoughtmachine.timebudget.DECLARED_CEILING)"` (`thoughtmachine/timebudget.py:72-75`), and `timebudget.py:59-63` imports no `defaults` | Not the same number: dispatch reports what it resolves (`tools/workspace/worker_thread.py:6721-6722`), but the reported `effective_timeout_seconds` excludes the 60 s grace actually waited (`tools/workspace/worker_timeout.py:105`), and the soft budget the model is told about (`agent/core/state.py:255-257`) cuts nothing | `TimeBudget.resolve` refuses at `strict=True` (`tools/workspace/worker_thread.py:6684-6699`); clamps at `thoughtmachine/timebudget.py:171-179`; the caller deadline cuts at `tools/workspace/worker_thread.py:9163-9164`; the agent loop does not cut (`agent/core/agent.py:1743-1745`) | `logger.warning("query wait: %s", …)` `tools/workspace/worker_timeout.py:107`, `:130`; envelope keys `thoughtmachine/timebudget.py:206-216`; refusal envelope carries `declared_ceiling_seconds` and `timeout_ceiling_owner` (`tools/workspace/worker_thread.py:6689-6696`) | AT USE — re-read from the module constant on every resolve (`thoughtmachine/timebudget.py:156-179`; call sites `tools/workspace/worker_thread.py:6677-6682`, `:8014-8019`); no snapshot is named in the inventory | R1 broken (five 600 s literals, string owner); R2 broken (grace excluded, F-2); R4, R6 satisfied | ceilings.md §A1, A3, A4, A5, A6, E1, E3, F-1, F-2 |
| 2. Token / context ceiling | No — the thresholds are declared once (`agent/config/models.py:189-190`), but the context window is declared three times: `agent/core/token_counter.py:150-171` (dict at `:158`), a hand-maintained copy at `tools/workspace/worker_thread.py:2481-2490`, and a capability key `max_context_length` at `thoughtmachine/workspace_capabilities.py:77-78`, `:107` | `AgentConfig` fields, HOT_SWAPPABLE (`agent/config/models.py:101-102`), for the thresholds; two owners for one window — the `TokenCounter` method and the `WorkerThread` property (`tools/workspace/worker_thread.py:2476` is a docstring mention, not a call) | No — the thresholds are read at `agent/core/state.py:126-131` and passed to tools at `agent/core/tool_executor.py:771-784`, while the number reported as the limit is the capability key at `tools/workspace/check_system.py:1420-1421`; the window pair is report-only in ~15 envelopes (`tools/workspace/worker_thread.py:4453`, `:4894`, `:5034`) | NONE for the window — no comparison site exists (negative search of `worker_thread.py`, B3 row 4). The thresholds are soft only: `agent/core/state.py:186-192` sets `restrictions_active`; the real cut is the provider error `agent/core/llm_client.py:398-408` then the trim at `session/context_builder.py:330-334` | Warning and recovery events `agent/core/state.py:168-172`, `:174-185`; emergency retry `log('WARNING','core.agent','Token limit exceeded – emergency retry')` `agent/core/agent.py:2033-2034`; the configured thresholds are not in the payload (INFERRED, B1 row 5) | AT USE for the thresholds — `update_token_state` re-reads `config.token_monitor_*_threshold` per turn (`agent/core/state.py:126-131`); the window has no gate at all to snapshot (B3 row 4) | R1 broken (three window declarations); R2 broken (the reported limit is a different key); R4 not satisfied for the window (no gate) | ceilings.md §B1, B3, B4, E2, E3, F-4, F-5 |
| 3. Model output budget | No — declared twice into the same object and read nowhere: `DEFAULT_RESPONSE_TOKENS = 4096` (`agent/config/defaults.py`, re-bound at `agent/core/agent.py:52`, `:540`) and `SAFETY_MARGIN = 1000` (`agent/core/agent.py:539`) | `agent.config.defaults` module constant, shadowed by the `Agent` class attribute (same object, `agent/core/agent.py:539-540`) | NONE FOUND — no read site; a search of `agent/**/*.py` for `DEFAULT_RESPONSE_TOKENS`, `SAFETY_MARGIN` or `max_tokens` returns only the import and the two assignments (B5 row 3) | NONE, and no substitute: `chat_kwargs` carries no output bound (`agent/core/agent.py:1844-1846`) and `agent/core/llm_client.py` has 0 occurrences of `max_tokens` or `max_completion_tokens` (B5 row 4) | NONE — output tokens are only observed after the fact (`agent/core/agent.py:1869`) | AT USE — `chat_kwargs` is rebuilt per call and carries nothing from the two constants (`agent/core/agent.py:1844-1846`); nothing is captured because nothing reaches the wire | R2, R4 broken (nothing reported, nothing cut, no record); R9 — 4096 is declared and UNMEASURED | ceilings.md §B5, E2, F-3 |
| 4. Per-tool output caps | No — nine caps declared at their own sites with no shared owner: `BaseTool.token_limit` (`tools/base.py:53`) fed from `agent/config/models.py:212`; per-tool ClassVars (`tools/file_search_tool.py:19-24`, `tools/file_preview_tool.py:13-16`); job previews `agent/config/defaults.py:109`, `:111`; exec bytes `agent/config/defaults.py:269` | Mixed — `AgentConfig.tool_output_token_limit` for the shared truncator, per-tool ClassVars for the rest; `BaseTool.token_limit` is a delivery channel whose own default is `None` (`tools/base.py:53`) | Partly — five of nine announce (shared truncator `tools/base.py:485-488`; FileSearch pinned by `tests/test_file_search_output_grouping.py:113-125`; container exec `infra/container_manager.py:621`, `:4658-4660`; git show_file `tools/git_info_tool.py:2440-2444`; FileSummary `tools/file_summary_tool.py:173-174`), four are silent (FilePreview `tools/file_preview_tool.py:99-112`; job previews `tools/workspace/job_registry.py:108-120`; git log clamp `tools/git_info_tool.py:1803-1806`; ci_read clamp `tools/ci_read_tool.py:293-299`) | `tools/base.py:457-488` (`_truncate_output`; `limit is None` or `<= 0` ⇒ no truncation), `tools/file_search_tool.py:457-463`, `infra/container_manager.py:611-621`; NOT enforced for `FilePreview.MAX_OUTPUT_CHARS` (`tools/file_preview_tool.py:16` — no reader, no test) | No log call: announced in-band in the tool result text (`tools/base.py:485-488`); sliced previews carry no marker (`tools/workspace/job_registry.py:108-120`) | SNAPSHOT — `BaseTool.token_limit` is set once per tool instance at `agent/core/tool_executor.py:548-549` and read at cut time (`tools/base.py:457-461`); ceilings.md records no re-read of it | R1 broken (nine independent sites; 10 MB declared two ways at `tools/file_preview_tool.py:23` and `tools/file_search_tool.py:19`); R2 broken (four silent caps); R9 (the unit is `len//4`, F-5) | ceilings.md §C1, C2, E1, E2, F-5, F-6, F-7, F-9 |
| 5. MCP server list | No — three independent declarations, none derived from another: `mcp_config.json` in the process CWD (`tools/mcp_manager.py:79`, again `:355`), vault `~/.thoughtmachine/mcp_servers.json` — a dict keyed by name (`tools/mcp_server_connect.py:36`) — and workspace `mcp_servers.json`, a bare array (`web_ui/backend/workspace_routes.py:1142`, `tools/workspace/check_system.py:1509`) | Two of three are STRINGS: the caller's CWD path resolved inside `MCPConfig.load` (`tools/mcp_manager.py:79-80`); the module constant `REGISTRY_PATH` (`tools/mcp_server_connect.py:34-36`); a filename string repeated at `web_ui/backend/workspace_routes.py:1142` and `tools/workspace/check_system.py:582`, `:1509` | No — the file shown as `mcp_servers` is never read by code that creates a tool or a process (0 hits in `tools/mcp_manager.py`, `tools/mcp_client.py`, `agent/`, `session/`); only `mcp_config.json` starts servers (`tools/mcp_manager.py:336-338`) | Nothing cross-checks the three lists; per-entry transport validation only (`tools/mcp_manager.py:48-66`); the `mcp:connect` grain gates the call, not the spawn — `subprocess.Popen` at `tools/mcp_client.py:249-262` with no check between `start_all` and it | Per-server lines only, never the list (`tools/mcp_manager.py:293`, `:373`); missing config ⇒ `logger.info` and no error (`:81-83`); malformed ⇒ `logger.warning` swallowed in the background thread (`:363-374`) | SNAPSHOT — `MCPConfig.load()` (`tools/mcp_manager.py:79-88`) held in `MCPServerManager.config` (`:262-263`), loaded once per process through `_manager` (`:322-338`) | R1, R4 broken (snapshot); R5 broken (missing or malformed config runs degraded in silence, `tools/mcp_manager.py:81-83`, `:363-374`); R8 broken (`stop_all` has no caller, `:301-314`) | bindings.md §A.1, A.4, A.5, A.7 |
| 6. MCP tool set | No — manufactured per `tools/list` entry by `create_tool_class` (`tools/mcp_manager.py:167-244`, class name built `:186-190`), then held again in two global registries (`tools/__init__.py:75-85`, re-bound `:41-60`) | Live objects, not names: the client instance captured by closure (`tools/mcp_manager.py:206-220`), `MCPServerManager.tool_classes` (`:262-263`, appended `:294`), and the agent's own `list` copy (`agent/config/models.py:429`) | No — the agent copies the list at `agent/core/agent.py:602-613` *before* background registration, the provider sees `self.tool_definitions` (`:1829`), the executor iterates its own copy (`agent/core/tool_executor.py:365-372`), and no log line names the agent that can see the tools (A.3 row 5) | Nothing re-reads the registry after startup; the only rebuild sites are the config-change hot swap gated on `enabled_tools` (`agent/core/agent.py:918-923`) and the restart path (`:1071-1079`); a late class is also absent from `enabled_tools` computed at config construction (`agent/config/models.py:214`, filter `:434-442`) | No visibility record — `logger.info(f"Registered {count} MCP tool(s) in background")` (`tools/mcp_manager.py:373`); a dead server returns a *string* and is recorded as a non-failed tool result (`tools/mcp_manager.py:322-327`, `agent/core/tool_executor.py:981-990`) | SNAPSHOT — the `tools/list` response captured at `tools/mcp_manager.py:291` (0 hits for `list_changed`), and `list(SIMPLIFIED_TOOL_CLASSES)` at `agent/config/models.py:429` → `agent/core/agent.py:602` | R2, R4 broken (no re-read, no visibility record); R8 broken (no removal path, `tools/mcp_manager.py:301-314`) | bindings.md §A.2, A.3, A.5, A.7 |
| 7. Credentials / vault secrets | No — one store layout (`agent/credentials/injector.py:1-9`, dirs `:68-69`) but two `Secret` types with different masks and APIs (`agent/config/secret.py:33-70` renders `<secret>`; `agent/credentials/injector.py:33-49` renders `***` with no `reveal()`), plus a cleartext provider key on disk (`agent/config/provider_profile.py:13-25`) and an env fallback (`llm_providers/factory.py:91-95`) | The vault directory tree, addressed by a workspace-id STRING (`agent/credentials/injector.py:64`): no secret-store object, no index, no encryption — the file NAME is the credential name (`:1-9`); the provider profile is addressed by the `provider_id` string (`agent/config/models.py:484-486`) | Yes on P1, where the same call resolves and records (`agent/core/tool_executor.py:923-924` → `:929-960`); no on P2 (success silent, failure logs the key name, `agent/core/agent.py:1010-1011`) and on P3 (records `CREDENTIAL_USE` with `credential_name='parent_api_key'` but gates nothing, `agent/core/tool_executor.py:795-812`) | P1 only: `credential:use` keyed off the collected placeholder names (`agent/core/tool_executor.py:868-882`, gate `:883-913`); P2 `agent/core/agent.py:1015` and P3 `agent/core/tool_executor.py:791-793` have no gate; P4 writes cleartext with no `0o600` (`agent/config/provider_profile.py:63-75`) | `CREDENTIAL_USE` per distinct name (`agent/core/tool_executor.py:929-960`, types `agent/events.py:98-100`), `_EVENT_SYSTEM_AVAILABLE`-gated and inside `try/except` (`:931-960`); the store logs nothing (no logger import, `agent/credentials/injector.py:11-18`) | AT USE for the store and P1 — no cache, every `resolve()` hits disk (`agent/credentials/injector.py:73-99`), gate re-read per call (`agent/core/tool_executor.py:650-666`); SNAPSHOT for P2 `AgentConfig.api_key` @ `agent/core/agent.py:1015`, P3 `self.config.api_key` @ `agent/core/tool_executor.py:791-793`, P4 `ProviderManager._profiles` @ `agent/config/provider_profile.py:36-61` | R4 broken for P2, P3, P4 (snapshots); R7 broken (the audit row is skipped when the event bus fails, `agent/core/tool_executor.py:931-960`); R3 satisfied on P1 only | bindings.md §B.1, B.2, B.3, B.4, B.5, B.9, B.10, B.11 |
| 8. Container envelope | No — the envelope is hashed into the container NAME and re-derived at each call site: `build_container_name` (`infra/containers/executor.py:267-293`) called at `:481`, `:800`, `:1285`, `:1409`, `:1553`, plus `infra/container_manager.py:1590` and `thoughtmachine/security.py:726` | A STRING, not an import: the container name is the pool key, recomputed from `(workspace_path, lifecycle_class, session_id, network_mode, workspace_mode)`, and no module holds a registry of names (INFERRED, `infra/containers/executor.py:267-293`); the live object belongs to `DockerExecutor.container` (`:649-682`) | Yes — the comparison and the audit read the same live values from `container.attrs` (`infra/containers/executor.py:762-767`, `:840-847`) and report `CONTAINER_REUSE_OK` / `CONTAINER_DRIFT_REFUSED` (`:786-788`, `:865-870`) | Re-checked before every exec: the cache-hit path reaps and recreates (`infra/containers/executor.py:757-790`), the name-match path routes through `ContainerLifecycle.reconcile` (`:838-853`); but the fast path never consults the destroy gate (`:786`) and a drift verdict still executes on the drifted container (`:854-872`) | `audit_event` at `:768-773`, `:817-819`, `:865-870`, `:874-877`, `:893-897`, `:900-903`, plus `emit_destroy_audit` (`:1156-1160`) | AT USE — desired config recomputed per `_ensure_container` call through `_resolve_container_config_via_gate(use_disk=True)` (`infra/containers/executor.py:296-350`, `:683-704`) and compared to the live `container.attrs`; nothing on this path is SNAPSHOT-TRUSTED (C.2 row 4) | R1 broken (name re-derived at seven sites); R4 satisfied; R5 broken (the drift verdict is not fail-closed, `infra/containers/executor.py:854-872`) | bindings.md §C.0, C.1, C.2 |
| 9. Worker incarnation (permissions, registry) | No — one permission set travels under three names: `session_permissions`, the declared `permission_footprint` (`agent/config/models.py:226-242`) and the legacy alias `worker_permissions` (`agent/models/worker_definition.py:41-46`), read together at `tools/workspace/worker_thread.py:2207-2209` | `WorkerThread._session_permissions` (`tools/workspace/worker_thread.py:2203`) plus the supervisor's `WorkerRegistry` keyed `(session_id, worker_name, instance_id)` (`tools/workspace/worker_registry.py:241-265`); ownership of the object is severed by copy at spawn (`tools/workspace/worker_thread.py:8045-8050`) | No — the mirror is read at `agent/core/tool_executor.py:551-553`, the declared cap travels separately (`tools/workspace/worker_thread.py:4108-4115`), and the gate then replaces both with the vault record (`security/security_gate.py:1220-1224`, `:1253-1255`) | Per tool call in disk mode: `get_effective_permissions` (`agent/core/tool_executor.py:653-658` → `security/security_gate.py:1133-1265`), fail-closed on an unreadable store (`:1243-1244`); the runtime mode fails closed at spawn (`tools/workspace/worker_runtime.py:361-372`) | Spawn journal `log_worker_event(worker_name,'spawned',…)` (`tools/workspace/worker_registry.py:264`); disk-read failure logged and carried into the denial (`security/security_gate.py:1230-1242`); TTL kill announce plus `audit_event` (`tools/workspace/worker_manager.py:249-251`, `:271`) | AT USE for the grant — re-read from disk on every tool call (`security/security_gate.py:1220-1224`); SNAPSHOT for the mirror — `WorkerThread._session_permissions` frozen at `tools/workspace/worker_thread.py:2203` and declared non-authoritative at `:1472-1481` | R1 broken (three names for one permission set); R4 satisfied; R8 broken (the stale reaper is disabled by default, `tools/workspace/worker_manager.py:978-1015`, and a registry entry outlives the kill, `:27-29`) | bindings.md §E.1, E.2, E.3, E.4 |
| 10. Model / provider binding | No — four independent layers each able to supply provider, model, base URL and key: `agent/config/models.py:140-168`; `agent/config/session_config.py:184-206`; `~/.thoughtmachine/providers.json` (`agent/config/provider_profile.py:13-25`); environment (`llm_providers/factory.py:88-101`, `agent/core/llm_client.py:28-33`) | Two are STRINGS: the provider class is looked up by name in `ProviderFactory._providers` (`llm_providers/factory.py:28-42`, indexed `:73`, `:82`), the profile by `provider_id` (`agent/config/models.py:484-486`); the live instances belong to `LLMClient.provider` (`agent/core/llm_client.py:280`) | No — the recorded `model_name` echoes the *requested* string (`openai_compatible.py:1041` → `agent/core/llm_client.py:483-486`), so a server-side alias or redirect is invisible | Two fail-closed refusals naming the owner: unknown `provider_type` (`llm_providers/factory.py:82-86`) and absent key (`:97-101`); the restart classification that would force a re-bind has no reader (`AgentConfig.FIELD_CATEGORIES`, `agent/config/models.py:90-129` — 2 grep hits, both definition and docstring) | `LLMError(error_type='authentication_error')` mapped at `agent/core/llm_client.py:403-409` and surfaced as a user-role `[SYSTEM NOTIFICATION]` plus an error event (`agent/core/agent.py:2038-2071`); no retry, no fallback | SNAPSHOT — `ProviderConfig` built at `llm_providers/factory.py:104-114` and held as `provider.config` (`llm_providers/base.py:185-190`), re-taken only by `Agent.restart()` (`agent/core/agent.py:1068`) | R1 broken (four layers); R4 broken (snapshot, and the restart policy is metadata with no reader); R6 satisfied (`llm_providers/factory.py:82-86`, `:97-101` name the layer and the remedy) | bindings.md §F.1, F.2 |

### 2.1 What the table says

- Rows 2 (the window half), 3 and one cap in row 4 have no enforcement site at all: the window numbers are report-only in ~15 envelopes (`tools/workspace/worker_thread.py:4453`), the response budget has no read site (`agent/core/agent.py:1844-1846`), and `FilePreview.MAX_OUTPUT_CHARS` has no reader and no test (`tools/file_preview_tool.py:16`).
- Rows 1, 8 and 9 re-read their input at the moment of use (`thoughtmachine/timebudget.py:156-179`; `infra/containers/executor.py:683-704`; `security/security_gate.py:1220-1224`), while rows 5, 6 and 10 name a captured snapshot instead (`tools/mcp_manager.py:79-88`; `tools/mcp_manager.py:291`; `llm_providers/factory.py:104-114`), as does the cap half of row 4 (`agent/core/tool_executor.py:548-549`).
- The `(2) owner` cell is a string rather than an import in rows 1, 5, 8 and 10 (`thoughtmachine/timebudget.py:72-75`; `tools/mcp_server_connect.py:34-36`; `infra/containers/executor.py:267-293`; `llm_providers/factory.py:28-42`).
- R2 is marked broken in rows 1, 2, 4, 6 and 10: the number a caller is told and the number that cuts or is served come from different hops (`tools/workspace/worker_timeout.py:105`; `tools/workspace/check_system.py:1420-1421`; `tools/git_info_tool.py:1803-1806`; `agent/core/agent.py:602`; `openai_compatible.py:1041`).
- R1 is marked broken in eight rows (1, 2, 4, 5, 7, 8, 9, 10), each time on a count the inventory gives: five 600 s literals in row 1, three window tables in row 2, nine cap sites in row 4, three MCP server lists in row 5, two `Secret` classes in row 7, seven name-derivation sites in row 8, three permission names in row 9, four config layers in row 10.
- R8 is marked broken in rows 5, 6 and 9: `stop_all` has no caller (`tools/mcp_manager.py:301-314`), no removal path exists for a tool class (`tools/mcp_manager.py:301-314`), and the stale reaper that would end a detached worker is off by default (`tools/workspace/worker_manager.py:978-1015`).

---

## 3. The six questions

Every resource surface must be able to answer these six about itself, and each answer must be *a place in the code*, not a habit.

| # | Question | A bad answer sounds like |
|---|----------|--------------------------|
| 1 | Where is the ceiling declared, once, with an owner that is an import? | "it is 600 in four files and a config default" |
| 2 | What owns the declaration at runtime? | "the label string says it is owned by HARD_TIMEOUT" |
| 3 | Which code reads it, and is that the same code that reports it to the caller? | "the spawn hop reports it, the state machine cuts it" |
| 4 | What refuses, clamps or truncates when it is exceeded, at which line? | "it is soft" |
| 5 | What record is written when it fires? | "it shows up in the envelope sometimes" |
| 6 | Does the gate re-read at the moment of use, or trust a snapshot? | "it was true when we started" |

Question 6 is the spine. The rest of this document exists because several surfaces answer 6 with a snapshot and cannot say so.

---

## 4. Rulings

### R1 — One declaration, one owner that is an import

Every ceiling is declared once, and its owner is an importable name. Where a number must appear twice — a public default and a safety cap, say — one site imports the other and a test asserts they are equal. A string that *names* the owner is documentation, not ownership: it cannot fail when it drifts, which is precisely why it will.

### R2 — The reporting layer is the enforcing layer

If the number a caller is told and the number that actually cuts are produced by different layers, the reported number is a rumour. Either join the two, or stop reporting it and report what is actually known. A budget that is granted, reported as unclamped, and then overrun without a cut or a notice is not a budget; it is a number with a comment.

### R3 — Grants narrow, never widen

The effective allowance is the minimum over ceiling, template and grant. A grant is derived, never authored at the point of use. This already works on exactly one surface, and that surface is the model to copy.

### R4 — The gate is at the moment of use and re-reads its input

A check that reads a value captured at bind time is a snapshot, not a gate, and must be called a snapshot in the code and in this document. The distinction is not stylistic: a snapshot cannot notice the moment its own declaration changed, which is the invariant at the top of this file.

### R5 — A gate that cannot validate must refuse, not substitute

When a binding can no longer be validated at use, the caller receives a refusal that names the layer. The caller must never receive a *different* binding that happens to be available, because a substituted binding is an authority nobody granted. Equally, a surface that cannot load its declaration must fail visibly rather than run degraded in silence.

### R6 — Every refusal names its layer and its recovery, from the same table as the gate

The refusal text is derived from the enforcement table, never authored beside it, so the advertisement cannot contradict the gate. Recovery is one imperative line. This shape already ships for turn restrictions; it is the shape of the only refusal in the tree that a reader can act on.

### R7 — Every use, refusal and revocation leaves one structured record

A stable field set — resource, template, binding identifier, requested, granted, effective, gate site, moment, reason verbatim — on its own logger, greppable without parsing prose. Absent facts are `null`, never defaulted: a plausible default is indistinguishable from a measurement, and that is how a rumour enters a report.

### R8 — Revocation is a transition with an owner, not an absence

A binding that can die without anyone noticing is a leak, not a resource. Every revocation names who performs it and who observes it. A distinction that exists in the code but is enforced only when a flag is on is a distinction the default denies.

### R9 — Declared is not measured, so every claim carries its label

Every row in section 2 and every sentence in this document is MEASURED with an anchor, INFERRED, or UNMEASURED. A number that no code enforces is reported as UNMEASURED — never as zero, never as its declared value.

---

## 5. Worked examples

Five surfaces, chosen because between them they cover every answer the table can give. One satisfies the invariant outright, one has the right shape at the wrong address, two trust a snapshot, and one substitutes a binding it should have refused.

### 5.1 MCP servers — three declarations, one of them executable

There are three MCP server lists, in three shapes, in three places: a CWD-relative `mcp_config.json`, a vault registry that one connect tool reads, and a per-workspace file. Only the first creates tools. The third is the one the product displays. Nothing reconciles them, so the list a user edits is not the list that runs, and no code path rejects a server that exists in one and not another. The feature breaks R1 three ways over, breaks R4 because its tool set is one `tools/list` response captured at registration with no re-list and no handling of a change notification, and breaks R5 because a malformed config leaves the agent running with no MCP tools and no visible error — while getting the permission grain right, which is the only part of it that was designed.

**Evidence:** `tools/mcp_manager.py:79` and `tools/mcp_server_connect.py:36` and `web_ui/backend/workspace_routes.py:1142` (three lists); `tools/mcp_manager.py:291` (the captured `tools/list`); `tools/mcp_manager.py:363-374` (malformed config swallowed); `tools/mcp_manager.py:225-238` (the grain, done right).

### 5.2 The turn budget — the ceiling model, almost done

A resolver with a default, a cap, a strict mode that refuses rather than clamps, and a named ceiling owner is the correct shape, and it is the pattern every other surface in this document should copy. What makes it a rumour is not its shape but its address: it runs at the spawn hop, while the turn is actually ended by the agent's own state machine, and nothing joins the two. That is R2 stated as a defect, and it is why a granted budget can be overrun with no cut, no clamp and no notice — the reported number was arithmetic, and the arithmetic was correct.

**Evidence:** `thoughtmachine/timebudget.py:67` and `:72-75` (ceiling and its string owner); `tools/workspace/worker_thread.py:6721-6722` (the spawn hop that reports); `agent/core/state.py:255-257` (the state that cuts); `tools/workspace/worker_timeout.py:105` (the grace the caller waits and the envelope excludes).

### 5.3 The container envelope — the doctrine, already written in code

A container's identity encodes its envelope, and the envelope is re-derived and re-checked against the live container before every exec. That is R4 satisfied, and the doctrine is already written down in the tree rather than only here. Two branches then undermine it: one returns an existing container after refusing drift, and a cached fast path reaps without reconciling. Both are R5 failures rather than design errors, which is why they are cheap to leave and expensive to discover.

**Evidence:** `infra/containers/executor.py:267-293` (the name that encodes the envelope); `:683-704` (the per-call re-derivation); `:854-872` (drift refused, then executed anyway); `:757-790` (the fast path that skips reconcile).

### 5.4 Worker permissions — layering done right

Three inputs — the incarnation's permission footprint, the live session's grants, and the workspace ceiling — combine by minimum, per tool call, with the grants re-read from disk and the spawn-time copy documented as explicitly *not* the authority. This is R3 and R4 satisfied on the surface that most needed them, and it is the reference implementation to copy when credentials become bindings.

**Evidence:** `security/security_gate.py:1220-1224` (grants re-read from disk); `agent/core/tool_executor.py:653-658` and `:680-705` (the minimum, per call); `tools/workspace/worker_thread.py:2203` and `:1472-1481` (the spawn-time copy, declared non-authoritative).

### 5.5 Model and provider — one build, many uses

The provider config is built once at factory time and held for the life of the object: a key and a model name captured at bind and not re-read. Whether that is reachable in practice depends on whether a key can rotate mid-life, which the inventory does not establish and this document therefore marks UNMEASURED. It is still an R4 violation, and R8 says why it matters: a credential that cannot be re-read at the moment of use cannot notice its own revocation, and the only remaining defence is that nothing ever revokes it.

**Evidence:** `llm_providers/factory.py:104-114` (built once); `llm_providers/base.py:185-190` (held as `provider.config`); `agent/core/agent.py:1068` (the only re-take, on restart); `agent/config/models.py:90-129` (the restart policy that has no reader).

---

## 6. Gaps, and what they cost

Each row is a gap the inventories measured, and the third column is what a user or operator would observe, not what the code intends.

| Gap | Where (anchor) | What the operator sees | Ruling | Source |
|---|---|---|---|---|
| **F-1** — two defaults for one config key | `agent/core/state.py:102` and `agent/config/models.py:223` (300); `tools/workspace/check_system.py:647` (600) | The same setting reads 300 s in the agent's own progress prose and 600 s in the system-check report, and neither is what ends the turn | R1, R2 | `ceilings.md` F-1 — MEASURED both ends |
| **F-2** — grace excluded from the reported wait | `tools/workspace/worker_timeout.py:105` vs `tools/workspace/worker_thread.py:6721-6722` | `effective_timeout_seconds` in the envelope is 60 s short of the wait the caller actually performs | R2 | `ceilings.md` F-2 — MEASURED |
| **F-3** — no response-length ceiling, though two constants declare intent | `agent/core/agent.py:539-540` (the constants); `:1844-1846` (the call passes none; no `max_tokens` in `llm_client.py`) | A reply runs to whatever length the provider allows, whole and without a notice; the two declared constants bound nothing | R1, R9 | `ceilings.md` F-3 — MEASURED |
| **F-4** — context window is reported, never compared | `tools/workspace/worker_thread.py:4453` (and ~15 envelopes) | A `max_context_tokens` figure that no code compares to anything; the first sign it was passed is a provider error and then emergency trimming | R2 | `ceilings.md` F-4 — MEASURED (negative search for the comparison) |
| **F-5** — unit mismatch between the notice and the monitor | `tools/base.py:452-455` (`len//4`) vs `token_counter.py:44-51` (`token_counter`) | A tool-result notice reports a token count computed one way while the threshold that fires is computed another way, so the notice and the budget describe different quantities | R9 | `ceilings.md` F-5 — MEASURED |
| **F-6** — silent caps | `tools/git_info_tool.py:1803-1806` (1000); `tools/ci_read_tool.py:293-299` (100); `tools/job_registry.py:108-282`; `tools/file_preview_tool.py:99-112` | Output arrives short — 1000 log lines, 100 CI runs, a sliced preview — and nothing in it says it was cut | R5, R7 | `ceilings.md` F-6 — MEASURED (no marker emitted) |
| **F-7** — declared cap with no reader | `FilePreview.MAX_OUTPUT_CHARS = 30_000` in `tools/file_preview_tool.py` | INFERRED — a preview can exceed the 30 000-character budget its own class declares, while `FileSearch`'s equivalent cap is enforced and tested | R1, R9 | `ceilings.md` F-7 — INFERRED (searched: no occurrence, no test) |
| **F-8** — a "hard" ceiling with no kill path | `HARD_TIMEOUT` and `EXEC_KILL_GRACE` imported at `infra/workspace_lifecycle_manager.py:75` and unread there | INFERRED — nothing in the envelope kills an exec at 600 s; it ends when the caller's own deadline expires, and the ladder that does kill is announce-only by construction | R2 | `ceilings.md` F-8 — MEASURED (imports, negative search) plus INFERRED (consequence) |
| **F-9** — announced truncation can be wrong in the other direction | `tools/base.py:457-461` | A `token_limit` of `None` or `<= 0` switches the shared truncator off, so a mis-set limit yields unlimited output and no notice at all | R5 | `ceilings.md` F-9 — MEASURED |
| **B-1** — container names collide on their fallbacks | `infra/containers/executor.py:262-263` (the literal `"anon"` tag); `:260-261` (16-character session prefix) | INFERRED — two session-less ephemeral containers in one workspace resolve to one name and therefore share one container | R1 | `bindings.md` C.1 — MEASURED (fallbacks), INFERRED (collision) |
| **B-2** — the lifecycle funnel is bypassed, and drift is refused and then executed | `infra/containers/executor.py:757-790` and `:786` (fast path reaps directly); `:817-819` (reap with no reconcile); `:854-872` and `:871` (refusal returns truthy; exec continues against `existing`) | The audit records `CONTAINER_DRIFT_REFUSED` and the exec runs anyway, in the container whose envelope no longer matches the workspace | R5, R7 | `bindings.md` C.2 — MEASURED (code), INFERRED (consequence) |
| **B-3** — a handle-less worker silently changes transport | `tools/workspace/worker_runtime.py:113-119` | INFERRED — a worker with no runtime handle takes the command-file path instead, with no error and nothing in its status that says which transport it used | R5 | `bindings.md` E.1 — MEASURED (fallback), INFERRED (the silence) |
| **B-4** — one permission set travels under three names, and an empty one caps nothing | `agent/config/models.py:226-242`; `agent/models/worker_definition.py:41-46`; `tools/workspace/worker_thread.py:2207-2209`; `agent/core/tool_executor.py:698-705` (empty footprint = no cap); `worker_thread.py:1507-1530` (legacy grains dropped) | A worker definition that omits the footprint narrows nothing, and a definition naming `git_read`, `git_write`, `system` or `execution` loses those grains without a message | R1, R3 | `bindings.md` E.2 — MEASURED |
| **B-5** — detached workers are never reaped by default, and one flag arms two behaviours | `tools/workspace/worker_manager.py:978-1015` (reaper disabled when the flag is unset); `:981-985` (`_interrupt` armed by the same flag); `:964-967` (notice once per process) | A worker that was "stopped" stays parked until an operator resumes or kills it by hand, and the notice that says so appears at most once per process rather than per request | R8 | `bindings.md` E.3 — MEASURED |
| **B-6** — `stopped` is absent from the terminal vocabulary yet branched on | `tools/workspace/worker_runtime.py:92-107` (`CANCEL_TERMINAL_STATUSES`); `tools/workspace/worker_query.py:328-329` (the `status == "stopped"` branch) | INFERRED — a job the interface shows as `stopped` is not in the terminal list, so a status the operator reads is not one the code treats as finished | R1, R9 | `bindings.md` E.3 — MEASURED (vocabulary), INFERRED (dead branch) |

### 6.1 What these have in common

Every row has the same shape: a value is declared in one place, reported from a second, and what actually stops, cuts or truncates is a third thing — usually the caller's own deadline or the provider. Where a check does exist it is applied to one path and skipped on the path next to it, so the audit record and the behaviour describe different events. Several surfaces are correct only while a flag is set, and the default is the permissive one. In each case the operator is shown a number or a status produced upstream of the event it claims to describe.

---

## 7. Order of work

Credentials, then workers-as-things, then the resource band.

A worker holds credentials, so binding a secret correctly is a precondition for treating a worker as a resource at all — the reverse order would build a resource model on top of a binding that cannot yet be validated. The resource band (a per-workspace view of what each surface has consumed and what each still owes) is meaningful only once every surface can answer the six questions in section 3; built earlier it would display declarations and call them measurements.

---

## 8. What is not measured

This section exists so that the next reader does not mistake silence for completeness.

- Filesystem roots: not inventoried in this pass. Known open question: does a path check happen at use time or once at bind time?
- Containers after the creating session is gone: partially inventoried (creation, envelope, per-exec re-check measured); the persistent-versus-ephemeral lifetime is not.
- `solver` and `resource band`: no hits under `tools/`, `security/`, `infra/`, `agent/` or `session/`. Recorded as NOT FOUND with the search set, which is not the same as absent.
- Provider-side output limits, and whether any of them are observable from this repository.

---
