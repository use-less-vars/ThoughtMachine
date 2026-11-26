# RESOURCE BINDINGS — anchored inventory

Second half of the evidence base for `docs/DESIGN_RESOURCES.md`.
Companion to `.scratch/resources/ceilings.md` (permission ceilings: DECLARED / OWNER / READ / ENFORCED / LOGGED).

**Scope rule for this file:** a *binding* is a durable link from the running system to a
resource that lives OUTSIDE the agent's own process (an MCP server process, a secret, a
container, a filesystem root, a worker thread, a model endpoint). Each binding is audited
with the same five questions as `ceilings.md` section A, plus one spine question:

| # | question | what it means here |
|---|----------|--------------------|
| (1) | **DECLARED** | where the binding is written down (field, file, dict, ClassVar) |
| (2) | **OWNER** | which object/module holds it at runtime; explicitly say when the owner is a **STRING/name**, not an import |
| (3) | **READ** | every site that reads it |
| (4) | **ENFORCED** | what actually consumes/checks it, and what happens if the check is skipped |
| (5) | **LOGGED** | where it appears in logs/telemetry/events/audit |
| (6) | **LIFETIME** | when created; when it can stop being valid; and — the spine — does code **RE-CHECK at moment of use** or **TRUST A SNAPSHOT** (the snapshot is then named) |

Evidence tags: **MEASURED** (read in the file at the cited line), **INFERRED** (a conclusion
drawn from measured code, not itself a line), **UNMEASURED** (could not be established).
Read-only audit: no fixes, no recommendations.

---

## A. MCP SERVERS

### A.0 Files read for this section (MEASURED)

- `tools/mcp_manager.py` (378 lines) — config model, dynamic tool-class factory, manager, lazy singleton, background registration.
- `tools/mcp_client.py` (505 lines) — transports; stdio spawn via direct `subprocess.Popen`.
- `tools/mcp_server_connect.py` (130 lines) — the `MCPServerConnect` tool (vault registry + `SandboxedExecution`).
- `tools/__init__.py` — global tool registries `TOOL_CLASSES` / `SIMPLIFIED_TOOL_CLASSES` and the accessors.
- `agent/core/agent.py` (startup tool snapshot), `agent/config/models.py` (filter), `agent/core/tool_executor.py` (per-call gate).
- `web_ui/backend/workspace_routes.py`, `tools/workspace/check_system.py` — the per-workspace `mcp_servers.json` readers.
- `security/sandboxed_execution.py` — the gated runner `MCPServerConnect` uses.
- `tests/test_mcp_client_uses_host_port.py` — the pin that records the deferred migration.

---

### A.1 THE SERVER LIST — there are **three** of them, in three shapes, in three places

**(1) DECLARED** — three independent declarations, none derived from another:

| # | location | shape | written by |
|---|----------|-------|-----------|
| (i) | `mcp_config.json` in the process **CWD**, overridable by env var `MCP_CONFIG_PATH` — `tools/mcp_manager.py:79` (`config_path = path or os.environ.get("MCP_CONFIG_PATH", "mcp_config.json")`), again verbatim at `tools/mcp_manager.py:355` | `MCPConfig` = `{"servers": [ {name, transport, command, args, env, url, api_key_env_var, headers} ]}` — a **list** under `servers` (`tools/mcp_manager.py:70-73`, item schema `:29-47`) | an operator/checkout: the file is CWD-relative and is **not** in the vault |
| (ii) | `~/.thoughtmachine/mcp_servers.json` (vault) — `tools/mcp_server_connect.py:36` `REGISTRY_PATH = os.path.expanduser("~/.thoughtmachine/mcp_servers.json")` | `{"servers": {"<name>": {command, args, transport, env}}}` — a **dict keyed by name**, read at `:81` (`servers = registry.get("servers", {})`) | operator only; docstring `tools/mcp_server_connect.py:20-22`: "The registry lives in the vault (`~/.thoughtmachine/`); the agent cannot write to it." |
| (iii) | `<vault>/workspaces/<ws_id>/mcp_servers.json` — `web_ui/backend/workspace_routes.py:1142`, `tools/workspace/check_system.py:1509` | a bare JSON **array** (`return []` when absent, `:1143-1147`) | seeded as `[]` by workspace auto-registration (`web_ui/backend/tests/test_workspace_auto_register.py:55,149-153`) |

MEASURED for all three locations and all three shapes.

**(2) OWNER** — three different owners, and two of them are **strings, not imports**:
- (i) owned by the **caller's CWD** (a path string resolved at call time inside `MCPConfig.load`, `tools/mcp_manager.py:79-80`) — no module owns it;
- (ii) owned by the **module constant** `tools.mcp_server_connect.REGISTRY_PATH` — a string; the module comment at `:34-35` says "Tests monkeypatch this module constant -- never hardcode the path anywhere else";
- (iii) owned by the **workspace-vault directory layout** (`_workspace_dir(ws_id) / "mcp_servers.json"`), i.e. a filename string appearing independently in `web_ui/backend/workspace_routes.py:1142` and `tools/workspace/check_system.py:582,1509`.
MEASURED.

**(3) READ** — who reads which:
- (i) `MCPConfig.load` → `get_mcp_manager` → `MCPServerManager.start_all` (`tools/mcp_manager.py:336-338`, `:265-299`). This is the **only** path that actually starts servers and generates tools.
- (ii) `MCPServerConnect.execute` only (`tools/mcp_server_connect.py:76-81`) — one-shot `initialize` handshake, **no tool generation**, no registration.
- (iii) two *display* readers only: `workspace_routes.get_mcp_servers` (`:1138-1147`) and `check_system._query_mcp_servers` (`:1505-1516`, also embedded in the config blob at `:580-586`).
- **DISAGREEMENT (MEASURED):** the file the agent *shows* as "mcp_servers" (iii) is never read by any code that creates a tool or a process — no reader of `mcp_servers.json` exists in `tools/mcp_manager.py`, `tools/mcp_client.py`, `agent/`, or `session/` (per-directory searches, 0 hits). The file that actually defines executable servers is (i), which is **not** in the vault and **not** the one displayed.
- **DISAGREEMENT (MEASURED):** `web_ui/backend/workspace_routes.py:4-5` advertises "GET/PUT endpoints for workspace files (… mcp_servers)". Only the GET exists (`@router.get` at `:1138`); there is no `put_mcp_servers` in the file (0 hits for `put.*mcp_servers`).

**(4) ENFORCED** — nothing validates that the three lists agree; nothing cross-checks (i) against (ii)/(iii). No code path rejects a server present in one list and absent in another. The only enforcement attached to a server entry is:
- per-entry transport validation in `MCPServerConfig.validate_transport_params` (`tools/mcp_manager.py:48-66`): stdio requires `command`, http/sse require `url`; raises `ValueError` naming the server (`:54-56`, `:60-62`). MEASURED.
- the **permission grain at call time** (see A.4) — which is applied to the *generated tool class*, not to the server entry.

**(5) LOGGED** — none of the three lists is ever logged as a whole; per-server lines only (A.6).

**(6) LIFETIME** —
- (i) is read **exactly once per process**: `_manager` is a module global (`tools/mcp_manager.py:322-323`) and `get_mcp_manager` only loads config when `_manager is None` (`:334-338`). Editing `mcp_config.json` after the first registration has **no effect** until the process restarts. **SNAPSHOT-TRUSTED — the snapshot is `MCPConfig.load()` (`tools/mcp_manager.py:79-88`) held in `MCPServerManager.config` (`:262-263`).**
- (ii) is re-read on **every** `MCPServerConnect.execute()` (`tools/mcp_server_connect.py:72-77`) — **re-checked at moment of use**.
- (iii) is re-read per call by the display readers (`workspace_routes.py:1146`).
MEASURED.

**MALFORMED / MISSING:**
- (i) missing → `MCPConfig(servers=[])` with `logger.info` and **no error** (`tools/mcp_manager.py:81-83`); `register_mcp_tools` short-circuits before spawning even the thread (`:355-358`). Malformed JSON → `json.load` raises inside the background thread and is swallowed as `logger.warning(f"Background MCP tool registration failed: {e}")` (`:363-374`) — the agent runs with **no MCP tools and no user-visible error**. MEASURED.
- (ii) missing → tool returns the string `"Error: No MCP server registry found. Add servers to ~/.thoughtmachine/mcp_servers.json"` (`tools/mcp_server_connect.py:72-73`); malformed → `"Error: Invalid JSON in MCP server registry: {path}"` (`:78-79`); unknown server name → error that enumerates the available names (`:82-86`). A server entry missing the `command` key raises an uncaught `KeyError` at `:88` (`command = cfg["command"]`) → surfaces through the generic tool-exception handler as `'Error executing tool: {e}'` (`agent/core/tool_executor.py:988-990`). MEASURED.
- (iii) malformed → `[]` (silent) at `web_ui/backend/workspace_routes.py:1147` and `{"error": …}` at `check_system.py:1515-1516`. MEASURED.

---

### A.2 DISCOVERY / REGISTRATION — the tool-class factory

**(1) DECLARED** — a tool class is manufactured per `tools/list` entry:
`create_tool_class(server_name, tool_def, client)` (`tools/mcp_manager.py:167-244`) builds a pydantic model via `create_model(class_name, __base__=ToolBase, **fields)` (`:203`), where each JSON-Schema property becomes a field (`json_schema_to_field`, `:106-165`) and a `tool` literal identifies the class (`:200-202`). Class name is `f"MCP{server_name}_{tool_name}"` progressively narrowed to alphanumerics (`:186-190`). MEASURED.

**(2) OWNER** — the **client instance**, captured by closure in the generated `execute` (`:206-220`: `result = client.call_tool(tool_name, arguments)`), and the class object itself is owned by `MCPServerManager.tool_classes` (`tools/mcp_manager.py:262-263`, appended at `:294`) and then by the global registry through `register_tool_class(cls)` (`:367-372`). The owner is a **live object**, not a name. MEASURED.

**(3) READ** — the tool list comes from the server: `tools = client.list_tools()` (`tools/mcp_manager.py:291`) → `_send_request("tools/list")` (`tools/mcp_client.py:160-167`). MEASURED.

**(4) ENFORCED** — two different grains are attached to the *generated class*, depending on the transport:
- stdio: `ToolModel.required_categories = ["mcp:connect"]` (`tools/mcp_manager.py:225-231`, comment: "stdio clients launch a host process");
- http/sse: replaces `get_required_categories` with a classmethod returning `["network:outbound"]` (`:233-238`).
Both are read by the gate through inherited `ToolBase.get_required_categories` (`tools/base.py:153-169`) at `agent/core/tool_executor.py:653`.
**The pin for this is a test, not production code:** `tests/test_mcp_client_uses_host_port.py:4-15` states the grain contract ("the same grain `MCPServerConnect` uses, because a stdio MCP server is an executable launched on the host"). MEASURED.
Caveat (MEASURED): the two branches are independent `if` statements with an intervening comment that repeats "Add get_required_categories" (`:225-233`); for a stdio client only the first applies, for http/sse only the second — behaviour is correct but the duplicated comment shows the branches were edited separately.

**(5) LOGGED** — `logger.info(f"Server {name} ({transport}) provided {len(tools)} tools")` (`tools/mcp_manager.py:293`); `logger.info(f"Registered {count} MCP tool(s) in background")` (`:373`); `logger.info("MCP registration running in background …")` (`:378`); `initialize` result (including `serverInfo`) logged at `tools/mcp_client.py:157`; server-initiated **notifications are logged with their full payload**: `logger.info(f"Server notification: {msg}")` (`tools/mcp_client.py:88-90`) — **potential leak site** if a server echoes a token back (INFERRED). MEASURED lines.

**(6) LIFETIME** — the tool *set* is a snapshot taken once, at `start_all`: `tools = client.list_tools()` (`:291`). There is **no re-list, no `tools/list_changed` handling anywhere** (0 hits for `list_changed`; the notification branch at `tools/mcp_client.py:86-90` only logs). **SNAPSHOT-TRUSTED — the snapshot is the `tools/list` response captured at `tools/mcp_manager.py:291`.** A server that adds/removes a tool mid-session keeps advertising the old set until the process restarts. MEASURED.

**MALFORMED / MISSING:** a tool definition with no `inputSchema`/`parameters` is accepted with `input_schema = {}` (`:177-179`) → zero fields → the model accepts only `tool`; unknown JSON-Schema types fall through to `Any` (`:106-165`, no branch → `field_type = Any`). A missing `name` key raises `KeyError` at `:176` (`tool_name = tool_def["name"]`) inside `start_all`'s per-server `try` → logged as `Failed to start MCP server {name}` (`:298-299`), aborting **all** tools of that server. MEASURED.

---

### A.3 HOW MCP TOOLS REACH THE AGENT — registration is fire-and-forget against a snapshot

**(1) DECLARED** — `agent/core/agent.py:602-613`:
`:602-604` `self.tool_classes = config.get_filtered_tool_classes(allowed_names=config.resolved_tool_names)` — taken **before** registration; `:607-611` then calls `register_mcp_tools()`; `:612` `self.tool_definitions = [model_to_openai_tool(cls) for cls in self.tool_classes]`; `:613` `ToolExecutor(self.tool_classes, …)`. MEASURED.

`register_mcp_tools` is non-blocking by construction: it returns immediately and does the work in a daemon thread (`tools/mcp_manager.py:341-378`, thread at `:376-377`); docstring `:344-346` "This function returns immediately and runs MCP server startup in a background daemon thread", and `:348-350` "Called lazily from agent initialization (not during module import) to avoid hangs when MCP servers are unavailable." The registration comment in `tools/__init__.py:324-326` repeats the ban on importing it eagerly (`# BUG001`). MEASURED.

**(2) OWNER** — the **global registries** in `tools/__init__.py`: `TOOL_CLASSES` (appended by `register_tool_class`, `:75-85`) and `SIMPLIFIED_TOOL_CLASSES`, which is **rebound** (not mutated) by `_update_simplified_toolset` (`:41-60`, `SIMPLIFIED_TOOL_CLASSES = simplified` at `:60`). The agent's own copy is `self.tool_classes` / `self.tool_definitions` — a plain `list` copy returned by `get_filtered_tool_classes` (`agent/config/models.py:428-429` `tool_classes = list(SIMPLIFIED_TOOL_CLASSES)`). MEASURED.

**(3) READ** — `self.tool_definitions` is what the provider sees: `tools = self.llm_client.format_tools(self.tool_definitions)` (`agent/core/agent.py:1829`). Dispatch resolves against the executor's copy: `agent/core/tool_executor.py:365-372` iterates `self.tool_classes` (stored at `:228`). MEASURED.

**(4) ENFORCED** — **nothing re-reads the registry after startup.** Consequence chain (MEASURED facts → INFERRED conclusion):
- The background thread appends to `TOOL_CLASSES` and rebinds `SIMPLIFIED_TOOL_CLASSES` (`tools/__init__.py:75-85`, `:41-60`) *after* `agent.py:602` already copied the list.
- `self.tool_classes`, `self.tool_definitions` and the executor's list are never refreshed for this agent. The only rebuild sites are `_hot_swap` — gated on `enabled_tools` actually changing (`agent/core/agent.py:918-923`) — and the control-plane restart path (`:1071-1079`).
- **Second bite:** `enabled_tools` defaults to `[cls.tool_name() for cls in SIMPLIFIED_TOOL_CLASSES]` **at config-construction time** (`agent/config/models.py:214`). A late-registered MCP class is therefore absent from `enabled_tools`, and `get_filtered_tool_classes` filters on it (`:434-442`) — so even a late rebuild drops MCP tools unless `enabled_tools is None`.
- **INFERRED:** MCP tools become visible only to an agent constructed **after** the background registration finished (e.g. the next session, or a later `Agent()`), not to the agent whose start triggered it. This race is timing-dependent (the daemon thread may win before `:602`), which is why the code comment says only "MCP tools become available later once background registration completes" (`agent/core/agent.py:605-606`) without specifying for whom.
- Also MEASURED: `agent/core/agent.py:30` imports `SIMPLIFIED_TOOL_CLASSES` **at module scope**, while `_update_simplified_toolset` rebinds the name (`tools/__init__.py:60`). `from_preset` (`agent/core/agent.py:2556-2560`) and `state_bridge.py:277` iterate that stale module-level binding; `models.py:428` re-imports inside the function, so it sees the fresh one. **Two different call sites can therefore see two different tool sets in the same process** (MEASURED divergence; the user-visible effect is UNMEASURED).

**(5) LOGGED** — startup: `log('WARNING', 'core.agent', f"Failed to start MCP background registration: {e}")` (`agent/core/agent.py:610-611`); registration count (`tools/mcp_manager.py:373`). No log line ever says "these MCP tools are now visible to agent X" — the success path is invisible (MEASURED absence).

**(6) LIFETIME** — binding created at agent construction (`agent/core/agent.py:602`), made stale by the registration thread, refreshed only by a config-change hot swap or a restart. **SNAPSHOT-TRUSTED — snapshot: the `list(SIMPLIFIED_TOOL_CLASSES)` copy taken at `agent/config/models.py:429` and stored on the agent at `agent/core/agent.py:602`/`:612`.**

**MALFORMED / MISSING:** the entire MCP surface can be absent and every layer still reports success — missing config file (`tools/mcp_manager.py:355-358`, debug-level only), failed import of `register_mcp_tools` (`agent/core/agent.py:610`), or a background exception (`:363-374`). There is **no** capability flag the model can inspect to learn that MCP tools exist. MEASURED.

---

### A.4 THE GRAIN — `mcp:connect` gates the CALL, never the SPAWN

**(1) DECLARED** — category `mcp` with levels including `connect` and `full`: authorized values `{"connect", "full"}` in `thoughtmachine/host_port.py:83-101`; canonical six-key schema (`container/network/filesystem/git/mcp/host_bash`) in `web_ui/backend/config_manager.py:100-107` and `web_ui/backend/tests/test_security_coercion.py:30-35`. Tool-side declarations: `tools/mcp_server_connect.py:49` (`required_categories: ClassVar[List[str]] = ["mcp:connect"]`) and `tools/mcp_manager.py:225-231`. MEASURED.

**(2) OWNER** — the tool **class** (ClassVar) for the declaration; the **session permission profile** for the grant (`thoughtmachine/security.py:112-131`, credential-style levels at `:125-126`, field at `:167-170`). MEASURED.

**(3) READ** — per tool call: `required_categories = tool_class.get_required_categories(arguments)` (`agent/core/tool_executor.py:653`) → `check_required_categories(required_categories, effective, …)` (`:757-775`) against `effective = get_effective_permissions(session_perms_obj, caps, session_id=session_id, workspace_id=ws_id)` (`:700-706`). MEASURED.
Second, independent check for `MCPServerConnect`: it passes the grain into its runner — `executor.run([...], required_category="mcp:connect", …)` (`tools/mcp_server_connect.py:104-112`) → `security/sandboxed_execution.py:129-131` `self._check_permission(required_category)` **before** `subprocess.run` (`:167-176`), failing closed with `PermissionError` (`:181-240`). MEASURED.

**(4) ENFORCED** — the gate is on the **tool call**, and it is genuinely re-read per call (see A.8). What it does **not** gate is the process. `StdioMCPClient.start()` spawns the server itself:
```
self.process = subprocess.Popen([self.command] + self.args, …, env=env)   # tools/mcp_client.py:249-262
env = os.environ.copy(); env.update(self.env)                            # tools/mcp_client.py:235-237
```
with **no permission check of any kind** between `start_all` (`tools/mcp_manager.py:265-299`) and that `Popen`. The code says so explicitly (`tools/mcp_client.py:239-248`): "DEFERRED HOST EXECUTION — DO NOT MIGRATE YET … This stdio spawn still calls subprocess.Popen directly and therefore bypasses the thoughtmachine/host_port.py chokepoint. The migration is blocked on the *asynchronous* port entry point … `host_run_async` is a signature-only stub that raises NotImplementedError, and every HostRunHandle method (poll/wait/kill) raises NotImplementedError too." A test pins the deferral (`tests/test_mcp_client_uses_host_port.py:16-27`). MEASURED.
Consequences (MEASURED facts): the child inherits the **full ambient environment** (`os.environ.copy()`, `:235`) — unlike `host_run`, which strips credential-shaped keys unless `credential_inject=True` (`thoughtmachine/host_port.py:103-117`); and the spawn is invisible to `host_port_audit.jsonl` (`thoughtmachine/host_port.py:122-123`). MEASURED.
Also MEASURED: `security/sandboxed_execution.py` is a **second** subprocess chokepoint (`import subprocess` at `:22`, `subprocess.run` at `:167-176`) whose `_build_env` merges caller `extra_env` **with no credential-shape scan** (`:227-237`) — i.e. the `mcp:connect` path used by `MCPServerConnect` merges `cfg.get("env", {})` (`tools/mcp_server_connect.py:90`, `:106-112`) into the child environment unexamined.

**(5) LOGGED** — `SandboxedExecution` logs command[0], cwd and user, "command name only -- never args" (`security/sandboxed_execution.py:168-176`). The unaudited `Popen` logs nothing at all (no logger call in `StdioMCPClient.start`, `tools/mcp_client.py:231-266`). No MCP-specific event type exists; the `CREDENTIAL_USE` event is used for credential placeholders, not for MCP (`agent/core/tool_executor.py:791-811`). MEASURED.

**(6) LIFETIME** — the *grant* is re-checked at every call (A.8, **re-check at moment of use**); the *process* is created once, before any grant is consulted, and lives for the process lifetime (A.5). So a session whose `mcp` grain is revoked mid-session loses the ability to **call** MCP tools, but the already-spawned server keeps running with its ambient environment. MEASURED facts; the security significance is left to the design doc.

**MALFORMED / MISSING:** `SandboxedExecution._check_permission` rejects a requirement string without `:` as `PermissionError: Malformed required category:` (`security/sandboxed_execution.py:184-187`), rejects an unknown/missing category (`:188-191`), and rewrites both `False` and the string `"ASK"` into a refusal (`:192-196`) — i.e. `mcp: ask` is a **deny** on this path (the interactive ask flow lives in the tool_executor gate, not here). MEASURED.

---

### A.5 DEATH, REMOVAL, AND WHAT HAPPENS MID-SESSION

**(1) DECLARED** — lifecycle is `start_all` / `stop_all` only (`tools/mcp_manager.py:265-299`, `:301-314`), plus transport-level `stop` (`tools/mcp_client.py:268-279` stdio: `terminate()` + `wait(timeout=5)`; `:341-346` http: `session.close()`; `:441-443` sse: no-op). MEASURED.

**(2) OWNER** — `MCPServerManager.servers` = `Dict[str, MCPClientBase]` (`tools/mcp_manager.py:262-263`), reached only through the module global `_manager` and `get_mcp_manager()` (`:322-339`). MEASURED.

**(3) READ** — `self.servers` is read only by `stop_all` (`:306-309`). Nothing else in the tree reads it. MEASURED.

**(4) ENFORCED** — **there is no removal path.** `stop_all` has **no caller** anywhere in `agent/`, `tools/`, `session/`, or `web_ui/` (per-directory searches; the only `stop_all` hits outside `tools/mcp_manager.py` belong to `tools/workspace/worker_manager.py:600`, a different class for worker threads). `_manager` is never set back to `None` (`:322-323`). There is no `restart`, no `remove_server`, no config-reload entry point. MEASURED (absence).
A dead server is **indistinguishable from a tool result**:
- request timeout → `TimeoutError(f"Timeout waiting for response to {method}")` (`tools/mcp_client.py:137-142`);
- server-sent error → `Exception(f"MCP error {code}: {message}")` (`:144-146`);
- but the generated `execute` swallows both and returns a **string** (`tools/mcp_manager.py:322-327`):
  `except Exception as e: return f"Error calling MCP tool {tool_name}: {str(e)}"`.
`tool_executor` therefore records the call as a normal, **non-failed** tool result (`agent/core/tool_executor.py:981-990` only marks failure when the exception escapes `execute()`). MEASURED.
`health_check()` exists on every transport (`tools/mcp_client.py:181-198`, `:290-298`, `:397-412`, `:455-457`) but **no production caller** was found in `tools/` or `agent/` (searches; test-only reachability UNMEASURED).

**(5) LOGGED** — `logger.error(f"Failed to start MCP server {name}: {e}")` (`:298-299`), `logger.warning(f"Skipping server {name}: {e}")` for `NotImplementedError` (`:296-297`), `logger.error(f"Error stopping server {name}: {e}")` (`:310-311`); stdio reader logs the **raw unparsable line**: `logger.error(f"Failed to parse JSON from server: {line} - {e}")` (`tools/mcp_client.py:286-287`) — **potential leak site** for whatever the server wrote. MEASURED.

**(6) LIFETIME** — created at first registration, never revoked, never reloaded. Invalid when the child dies (`process.poll() is not None`, health check `:294-295`) or the config file changes (never re-read). Call sites **do not re-check**: the only per-call check is the *category* gate of A.4 — liveness is never verified at moment of use. **SNAPSHOT-TRUSTED — snapshot: `self.servers` + the generated tool classes of `MCPServerManager` (`tools/mcp_manager.py:262-263`).** MEASURED.

**MALFORMED / MISSING:** a stdio server whose `command` does not exist raises `FileNotFoundError` from `Popen` inside `start_all`'s `try` → logged (`:298-299`) and that server contributes zero tools; other servers continue (per-server isolation, `:266-299`). A server that starts but never answers `initialize` blocks `client.start()` for the 5.0 s request timeout (`tools/mcp_client.py:139-142`) **inside the daemon thread**, so the agent is unaffected but registration takes up to 5 s per bad server. MEASURED (timeout constant at `:139`; "daemon thread" at `:376`).

---

### A.6 WHAT THE TRANSPORTS DO WITH THE `env` BLOCK (binds A.4 → section B)

**(1) DECLARED** — `MCPServerConfig.env: Optional[Dict[str, str]]` (`tools/mcp_manager.py:43`), vendor docstring "Environment variables for subprocess" (`:41`); the vault registry's own `env` key (`tools/mcp_server_connect.py:14-15`, read at `:90`). MEASURED.

**(2) OWNER** — the config object / registry dict — i.e. a **JSON blob, not a secret-store reference**. MEASURED.

**(3) READ** — stdio: merged onto `os.environ.copy()` (`tools/mcp_client.py:235-237`); connect-tool: passed as `extra_env` into the sandbox runner (`tools/mcp_server_connect.py:106-112`) → merged **after** the stripped env (`security/sandboxed_execution.py:227-237`). MEASURED.

**(4) ENFORCED** — no scan. Neither path applies the credential-shape regex that `host_run` applies (`thoughtmachine/host_port.py:118-120`, refusal at `:103-117`). Values in `env` reach the child verbatim, and (stdio) the child also inherits the whole parent environment. MEASURED.

**(5) LOGGED** — not logged by any of the three sites (no logger call in `tools/mcp_client.py:231-266`, `security/sandboxed_execution.py:227-237`); the *refusal* path in `host_run` is the only place a credential-shaped key produces a log line, and MCP never goes through it. MEASURED.

**(6) LIFETIME** — resolved at `Popen`/`run` time, per spawn. `MCPServerConnect` re-resolves every call (**moment of use**); the manager resolves once per process (**SNAPSHOT-TRUSTED — snapshot: `MCPServerConfig.env` as loaded at `tools/mcp_manager.py:79-88`**). MEASURED.

**MALFORMED / MISSING:** `api_key_env_var` is looked up in the process environment and, when unset, only warns and proceeds **without** a key — not fail-closed (`tools/mcp_manager.py:271-278`); the header is then simply absent (`tools/mcp_client.py:323-325`). MEASURED. SSE is a declared-but-dead transport: `start()` raises `NotImplementedError` (`tools/mcp_client.py:437-439`) and `start_all` catches exactly that (`tools/mcp_manager.py:296-297`), so an `sse` entry is silently skipped with a warning. MEASURED.

---

### A.7 SUMMARY TABLE — MCP (one line per binding)

| binding | (6) LIFETIME verdict | snapshot named |
|---|---|---|
| server list (i) `mcp_config.json` | SNAPSHOT-TRUSTED | `MCPConfig.load()` @ `tools/mcp_manager.py:79-88`, held in `MCPServerManager.config` |
| server list (ii) vault registry | re-checked at moment of use | — (re-read every `execute()`, `tools/mcp_server_connect.py:76`) |
| server list (iii) workspace file | display only, per-call re-read | — |
| shipped tool set | SNAPSHOT-TRUSTED | `client.list_tools()` @ `tools/mcp_manager.py:291` |
| tool classes on the agent | SNAPSHOT-TRUSTED | `list(SIMPLIFIED_TOOL_CLASSES)` @ `agent/config/models.py:429` → `agent/core/agent.py:602` |
| `mcp:connect` / `network:outbound` grain | re-checked at moment of use | — (per-call gate, `agent/core/tool_executor.py:653-775`) |
| server process (stdio) | created once, never gated, never re-checked | `subprocess.Popen` @ `tools/mcp_client.py:249-262` |
| child environment | SNAPSHOT-TRUSTED (manager) / per-call (`MCPServerConnect`) | `MCPServerConfig.env` / registry `env` |

---

## B. CREDENTIALS AND SECRETS

### B.0 Files read for this section (MEASURED)

- `agent/credentials/injector.py` (187 lines) + `agent/credentials/__init__.py` (5) — the store reader, `Secret`, `CredentialError`.
- `web_ui/backend/global_routes.py` (465) — the only writer (`/api/credentials`), plus `CREDENTIAL_ROTATED` / `CREDENTIAL_DELETED` events.
- `agent/core/tool_executor.py` (1140) — placeholder scan, `credential:use` gate, injection, `CREDENTIAL_USE` audit, `inherits_parent_api_key` handover.
- `agent/core/agent.py` (placeholder in `config.api_key`), `agent/config/models.py`, `agent/config/secret.py` — the second `Secret` wrapper and the `api_key` field.
- `agent/config/provider_profile.py` — `~/.thoughtmachine/providers.json` (plaintext key on disk).
- `llm_providers/base.py`, `llm_providers/factory.py`, `llm_providers/openai_compatible.py`, `llm_providers/anthropic_provider.py` — the reveal/egress end.
- `thoughtmachine/git_auth_env.py` — the git credential carrier (`TM_GIT_ASKPASS_SECRET`).
- `thoughtmachine/host_port.py` — `CREDENTIAL_SHAPED`, `credential_inject`, inherited-env stripping.
- `security/sandboxed_execution.py` — the other subprocess chokepoint (env posture).
- `thoughtmachine/security.py` — vault-compartment blocking for file tools; `security/resource_catalog.py` + `security/security_gate.py` — the `credential` grain.
- `agent/logging/redaction.py`, `agent/logging/__init__.py`, `agent/logging/lifecycle.py`, `agent/logging/event_logger.py`, `agent/logging/streams.py` — redaction.
- `tools/mcp_manager.py`, `tools/mcp_client.py`, `tools/mcp_server_connect.py` — the MCP key path (see A.6).

---

### B.1 THE STORE — one directory, two scopes, a writer for only ONE of them

**(1) DECLARED** — `<vault_root>/credentials/<workspace_id>/<key>`, falling back to `<vault_root>/credentials/global/<key>`; "One file per credential key — plain text, no JSON wrapper" (`agent/credentials/injector.py:1-9`). The two dirs are computed from `vault_root()` at `:68-69`. MEASURED.

**(2) OWNER** — the vault directory tree; the runtime owner is two `Path` objects per injector instance, addressed by a **workspace-id STRING** (`workspace_id: str`, `:64`) — there is no secret-store object, no index, no encryption: the file NAME is the credential name. MEASURED.

**(3) READ** — `resolve()` `:73-99` (workspace scope first, global second, `:94-97`); `inject()` `:101-127` rewrites only top-level string values of the args dict and only when the whole value `fullmatch`es `\{\{credential:(.+?)\}\}` (`:53`, `:121`) — a placeholder embedded in a longer string is left **verbatim**. MEASURED.

**(4) ENFORCED** — `_validate_key` `:161-187` (empty, null byte, `/`, `\`, `..`, `~`, absolute); per-scope symlink containment by `os.path.realpath` + prefix compare `:142-148` (a traversal raises, it does not silently fall through to the global scope). No authorisation inside the store — that lives entirely in the caller (B.2). MEASURED.

**(5) LOGGED** — nothing: the module imports no logger (`:11-18`). Malformed/missing produce `CredentialError` with the **key name only**: `:99` not-found, `:150` traversal, `:155` not-a-regular-file, `:159` read failure. MEASURED.

**(6) LIFETIME** — no cache: every `resolve()` hits the filesystem (**re-read at moment of use**); a rotation is an `os.replace` and the next call sees it. Writer = `POST /api/credentials` → `_write_credential` (`web_ui/backend/global_routes.py:379-403`): dirs `0o700` (`:388-389`), tmp file via `os.open(..., 0o600)` + `os.fchmod` + `os.replace` (`:396-402`). **The writer only ever writes the `global` scope** — `_credentials_dir()` `:356-358` is hard-coded `credentials/global`; no code in `agent/`, `tools/`, `security/` or `web_ui/` writes `<vault>/credentials/<workspace_id>/`, so the per-workspace scope is populated out-of-band only. MEASURED (searches: `credentials` in `agent/`, `tools/`, `web_ui/`, `thoughtmachine/`, `security/`). Name validation for the writer: `validate_single_path_segment(..., max_length=128)` `:346-353` (shared with `thoughtmachine/validation.py:4-45`). GET `:414-429` returns a **bare array of names** (values never serialised); DELETE `:449-465`. MEASURED.

---

### B.2 PATH P1 — `{{credential:<key>}}` inside tool arguments (the only GATED path)

**(1) DECLARED** — the placeholder syntax itself (`agent/credentials/injector.py:53`) and the tool-side contract: a model may pass the literal string `{{credential:name}}` as any string argument.

**(2) OWNER** — `agent/core/tool_executor.py::_execute_single_tool`, resolved per call into a `Secret` wrapped in `tool_args` (`:923-924`). The **resolved value is owned by the tool instance**, not by the executor (the dict is deep-copied by `inject()` `:116`). MEASURED.

**(3) READ** — `:850-851` imports; `:858-866` collects the names *before* replacement (they are unrecoverable afterwards, comment `:859-861`); `:923-924` constructs `CredentialInjector(ws_id)` and injects. `ws_id` comes from `tool_args['session_permissions']['workspace_id']` or the executor-pinned `tool_args['workspace_id']` (`:854-857`). MEASURED.

**(4) ENFORCED** — the `credential` grain at level `use`: `check_required_categories(['credential:use'], effective, ...)` `:883-895`, keyed **off the collected placeholder names, not off `required_categories`** (`:868-882` — the explicit reason: a declaration-based gate lets every tool that declares nothing through). Fail-closed when the gate is unavailable or the profile is `None`: `ok_cred=False` + refusal text `:897-909`, return `:910-913`. Refusal/exception paths never construct the injector (comment `:918-922`); any `CredentialError` becomes a tool failure string `:925-927`. Grain levels: `credential: banned|read|use` (`thoughtmachine/security.py:125-128` docstring, `security/resource_catalog.py:46,58`, default `"banned"` `security/security_gate.py:97`). MEASURED.

**(5) LOGGED** — one `CREDENTIAL_USE` event per **distinct name** (`:929-960`), fields `credential_name` / `tool_name` / `session_id`; values never on the bus (comments `:789-790`, `:930`). Event types declared `agent/events.py:98-100`, registered as routed events `:197-199`. MEASURED.

**(6) LIFETIME** — validated at the **moment of use** (per tool call; `effective` is itself re-read from disk when `session_id and ws_id` are present — `agent/core/tool_executor.py:650-666`). The store read is per call (B.1). SNAPSHOT-TRUSTED only in the narrow sense that the *workspace* used for the store path is the executor-pinned value (`tools/base.py:118-124`, pin at `tool_executor.py:822-828`), not the model's. MEASURED.

**MALFORMED / MISSING:** an unknown key → `CredentialError` → the tool call returns `"Credential injection failed: Credential '<key>' not found"` (`:927`) — a *tool result*, not a permission denial; `credential:read` ("names only") has **no production consumer**: it appears only in `tests/test_credential_resource_grain.py:104-142` (MEASURED absence; `grep -rn "credential:read"` over the tree, worktrees excluded).

---

### B.3 PATH P2 — `{{credential:<key>}}` written into `config.api_key` (provider key from the store)

**(1) DECLARED** — the same placeholder, but stored in the `api_key` field (`agent/config/models.py:140`, `exclude=True`). **(2) OWNER** — the `AgentConfig` instance; the resolved object is a `Secret` from `agent/credentials/injector.py:33`. MEASURED.

**(3) READ** — `Agent._resolve_credential_placeholder` `agent/core/agent.py:978-1015`: no placeholder → `log('INFO','credential','Using direct API key for provider (no credential placeholder)')` and return `:986-988`; no session/workspace → `CredentialError` `:990-994`; regex `:999-1002`; `CredentialInjector(self._session.workspace_id).resolve(key)` `:1005-1008`; `config.api_key = resolved` `:1015`. Callers: `:593` (agent start), `:1067` (restart), `:1118` (old config during restart). MEASURED.

**(4) ENFORCED** — **no `credential:use` check on this path** (no call to `check_required_categories` anywhere in `agent/core/agent.py`; MEASURED by search for `check_required_categories` in that file, which appears only in `tool_executor.py`). The only precondition is that a session/workspace exists. INFERRED consequence: a session whose `credential` grain is `banned` can still have the provider key pulled from the store, because that read is not a tool call.

**(5) LOGGED** — success is silent; failure logs `WARNING 'credential'` with the **key name** and the exception text `:1010-1011`, then re-raises. The resolved value is never logged. MEASURED.

**(6) LIFETIME** — resolved once at agent construction/restart (**SNAPSHOT-TRUSTED — snapshot: `AgentConfig.api_key` set at `:1015`**); rotating the vault file does not change a live agent's key until restart. The assignment at `:1015` also **bypasses pydantic validation**, so the field can hold a plain `str` if `resolve()` ever returned one (documented limit, `agent/config/secret.py:30-34`). MEASURED (the assignment) / MEASURED (the docstring limit).

---

### B.4 PATH P3 — `inherits_parent_api_key` handover (provider key into a child agent)

**(1) DECLARED** — `tools/base.py:126-130`: ClassVar `inherits_parent_api_key: ClassVar[bool] = False`, docstring "True only for tools that build a CHILD AGENT in the same session trust domain". Exactly **one** tool sets it True: `tools/workspace/worker_thread.py:6441`. MEASURED (`grep -rn inherits_parent_api_key`).

**(2) OWNER** — the class attribute (a **boolean flag, not an import/name**); the value is `self.config.api_key`, handed over as a plain field inside the `agent_config` dict. MEASURED.

**(3) READ** — `agent/core/tool_executor.py:791-793` (`tool_args['agent_config']['api_key'] = getattr(self.config, 'api_key', None)`). MEASURED.

**(4) ENFORCED** — **class declaration only**. There is no `credential:use` gate, no name resolution, no store read on this path: if `Worker.inherits_parent_api_key` is True the key is placed in the args before the gate block for P1 even runs (P1 only inspects `{{credential:…}}` placeholders, `:846-857`). MEASURED (asymmetry with P1 is the finding, no recommendation).

**(5) LOGGED** — `CREDENTIAL_USE` with `credential_name='parent_api_key'`, `tool_name`, `session_id`, `source='tool_executor'` (`:795-812`). Value never included. MEASURED.

**(6) LIFETIME** — snapshotted at dispatch from the live config (**SNAPSHOT-TRUSTED — snapshot: `self.config.api_key`**, the object resolved at B.3); the child agent holds its own copy afterwards. MEASURED.

---

### B.5 PATH P4 — the provider key on DISK and in the ENVIRONMENT (not the vault)

**(1) DECLARED** — `~/.thoughtmachine/providers.json`, field `ProviderProfile.api_key: str = ''` (`agent/config/provider_profile.py:13-25`), module docstring "Stored in ~/.thoughtmachine/providers.json — never read by the agent or committed to git. The GUI owns this file exclusively" (`:1-6`). MEASURED.

**(2) OWNER** — `ProviderManager` (`:36-40`), addressed by `PROVIDERS_FILE` (`:15`). MEASURED.

**(3) READ** — `_load()` `:44-61` (`json.load`, `ProviderProfile(**p)`); `resolve_config()` `:127-170` and `AgentConfig.resolve_from_profile()` `agent/config/models.py:462-501` copy `api_key` into the live config when non-empty. MEASURED.

**(4) ENFORCED** — **no mode, no redaction, no vault**: `save()` `:63-75` is `json.dump(..., indent=2)` on `p.model_dump()` — the key is written **in cleartext** and no `0o600` is applied (contrast `web_ui/backend/global_routes.py:396-402`). The file sits at the vault ROOT, which `thoughtmachine/security.py:446-470` does block for file tools (`startswith(vault_root)`, reason `vault_root` when no blocked subdir matches) — so the agent's file tools cannot read it, while the web UI and any host shell can. MEASURED (write/absence of mode, block site).

**(5) LOGGED** — the config audit redacts by key name: `SENSITIVE_KEYS = {api_key, apikey, authorization, password, token, secret}` (`agent/config/audit.py:31-34`), `REDACTED='<redacted>'` `:36`, `redact_config` `:49+`; `vault_drift._SECRET_HINTS` includes `api_key`/`credential` (`agent/config/vault_drift.py:43-44`). `agent/config/loader.py:399-404` pops `api_key` before persisting the *agent* config. MEASURED.

**(6) LIFETIME** — read once at `ProviderManager.__init__` (`:36-40`) — **SNAPSHOT-TRUSTED — snapshot: `self._profiles`**; an edit to the file by the GUI is invisible until a new manager is constructed (INFERRED from the absence of any reload call: `_load` is called only from `__init__`, MEASURED by grep).

**ENV-VAR FALLBACK (second source, outside the vault):** `llm_providers/factory.py:91` `api_key = api_key or config.get("api_key") or os.getenv(env_var)`, `:94-95` `OPENAI_API_KEY` fallback, `:97-100` raise when none. The env var name is derived from the provider string (`f"{provider_type.upper()}_API_KEY"`, INFERRED from `:91` + `:100` text). MEASURED for the lines.

---

### B.6 PATH P5 — the git credential (environment carrier)

**(1) DECLARED** — one env key: `ASKPASS_SECRET_ENV = "TM_GIT_ASKPASS_SECRET"` (`thoughtmachine/git_auth_env.py:48-51`) plus `GIT_ASKPASS` / `GIT_TERMINAL_PROMPT=0` / the `GIT_CONFIG_COUNT` triple-set built in `build_git_auth_env` `:69-160`. MEASURED.

**(2) OWNER** — a **dict of plain `str`** returned to the caller (not a store, not a `Secret`). MEASURED.

**(3) READ** — by the caller that exports it into the git subprocess env; `tools/git_write_tool.py:1213-1219` refuses `push` when `ASKPASS_SECRET_ENV` is absent from `self._git_environment()` — i.e. the presence of the *key name* is the check. MEASURED.

**(4) ENFORCED** — two structural refusals: a `Secret` (str subclass whose `str()` is `***`) is rejected by `_require_plain_str` so the mask can never be sent (`git_auth_env.py:36-44` module docstring, `:80-100`), and `http.extraheader` is refused as a config key (`FORBIDDEN_CONFIG_KEYS` `:54-55`, `:116-124`) which keeps the credential out of argv. `git_info_tool.py:1164-1176` additionally pins `-c credential.helper=` for every invocation. MEASURED.

**(5) LOGGED** — the token appears in exactly one env value (`:69-74` docstring "The token appears in exactly one value"); no log site writes the env dict. The refusal message names the env var only (`tools/git_write_tool.py:1214-1219`). MEASURED for the message, INFERRED for "no log site" (no logger call in `git_auth_env.py`).

**(6) LIFETIME** — built per push/commit call at the call site (**moment of use**); nothing cached in the module (`askpass_path()` only computes a path, `:58-67`). MEASURED.

---

### B.7 PATH P6 — the MCP api_key (cross-reference A.6)

`api_key_env_var` is **declared** per server (`tools/mcp_manager.py` config model) and **read from the process environment** at `start_all` `:269-278`; unset ⇒ `logger.warning("... not set")` and the call proceeds with `api_key=None` (**not fail-closed**, MEASURED). Egress: `Authorization: Bearer <api_key>` set at client construction (`tools/mcp_client.py:317-325` for HTTP, `:429-436` for SSE), plain `str`, no `Secret`. Owner: the `MCPClientBase` instance's `headers` dict; lifetime = the client object (SNAPSHOT-TRUSTED, snapshot: `MCPServerConfig` as loaded once per process — A.1/A.6). The stdio transport never sees `api_key`; it inherits the whole parent environment instead (`tools/mcp_client.py:222-224`). MEASURED.

---

### B.8 PATH P7 — ambient environment into child processes: two chokepoints, two postures

| chokepoint | inherited env | credential-shaped keys | audit |
|---|---|---|---|
| `thoughtmachine/host_port.py::host_run` | `os.environ.copy()` **minus** every key matching `CREDENTIAL_SHAPED` (`:118-120`, strip loop `:757-766`) | caller-supplied keys refused unless `credential_inject=True` (`:714-728`); `credential_inject=True` requires exact `str` values, rejecting `Secret` so the mask cannot be sent (`:730-748`) | PRE/POST JSON lines in `host_port_audit.jsonl` (`:123`, `:769-790`), **names only** for stripped keys |
| `security/sandboxed_execution.py::SandboxedExecution.run` | `strip_env=True` default: `HOME=/dev/null`, fixed `PATH`, `+_GIT_ENV` for git (`:42-53`, `:227-237`); `strip_env=False` ⇒ `os.environ.copy()` | **no scan at all**; `extra_env` merged last, unscanned (`:236-237`) | `logger.info("SandboxedExecution: %s in %s (user=%s)")` — command name + cwd, never args (`:176-183`) |
| `tools/mcp_client.py` stdio spawn | `os.environ.copy()` (`:222`) | **no scan** | nothing |

MEASURED (all cells). Note the deliberate asymmetry: the module docstring of `sandboxed_execution.py:16-18` claims a hermetic env "blocking ambient config/alias/credential injection", which holds only for `strip_env=True` (the default) and not for `strip_env=False` (`:232-233`).

---

### B.9 REDACTION SITES (where a value would be masked)

1. `agent/config/secret.py:33-70` — `Secret` renders `<secret>` for `str()` / `repr()` / `format()`; raw value only via `.reveal()` `:60-68`, module helper `reveal_secret` `:72-81`. Documented holes: `json.dumps`, `str.__str__(secret)`, `.encode()`, `"" + secret`, and pydantic assignment bypass (`:19-34`).
2. `agent/credentials/injector.py:33-49` — a **second, independent** `Secret` class rendering `***` (not `<secret>`), with no `reveal()` method. DIVERGENCE: two `Secret` types with different masks and different APIs; `host_port.py:730-748` and `git_auth_env.py:36-44` describe the `***` one, `agent/config/*` uses `<secret>`.
3. `agent/logging/redaction.py:17-62` — regex redaction (`sk-…`, `ghp_…`, `Bearer …`, `AKIA…`, `api_key=…`-style pairs, PEM blocks) → `<REDACTED>`; applied to whole serialized lines at `agent/logging/streams.py:110-122`, `agent/logging/__init__.py:286-293` and `:320-325` (fallback file), `agent/logging/event_logger.py:206,218`, `agent/logging/lifecycle.py:274,317`.
4. `agent/logging/__init__.py:50-71` — inline base64 data-URL redaction + truncation for user queries (`QUERY_LOG_LIMIT = 2000`, `:47`).
5. `agent/config/audit.py:31-46` — key-name redaction (`<redacted>`) for config audits; `agent/config/vault_drift.py:43-44` for drift reports.

---

### B.10 LEAK SITES (measured places a value can escape redaction)

1. **Tool-call arguments are logged pre-injection and unredacted by field**: `agent/core/tool_executor.py:362` `self.logger.log_tool_call(tool_name, arguments, tool_call['id'])` and `:406` `{'arguments': arguments, ...}`; `log_tool_call` (`agent/logging/__init__.py:441-455`) only **truncates** (`tool_arguments_truncate`) — no field-level redaction; the line reaches disk redacted only via site B.9.3's regex, which cannot know a bare opaque token. Same for `executed_tools` entries returned to the caller. A model that emits a literal secret instead of a placeholder has it recorded.
2. `agent/core/tool_executor.py:931-960` — audit is `_EVENT_SYSTEM_AVAILABLE`-gated and `try/except`: a failing event bus only logs `ERROR 'core.credentials'` and the injection still stands (no audit row). MEASURED.
3. `agent/core/agent.py:1010-1011` / `tool_executor.py:926` — the `CredentialError` text is written to the log and **into the tool result string** (`:927`), which is fed back to the model; it contains the key name.
4. `llm_providers/openai_compatible.py:244` and `:493`, `llm_providers/anthropic_provider.py:230` — `api_key={config.api_key}` inside an f-string. Safe **only because** `ProviderConfig.__post_init__` rewraps a plain key into `Secret` (`llm_providers/base.py:140-151`); a config object that is not a `ProviderConfig` (a plain dict / a namespace) would log the raw key. MEASURED lines, INFERRED safety condition.
5. `agent/config/provider_profile.py:63-75` — cleartext `api_key` in `~/.thoughtmachine/providers.json`, no `0o600` (B.5).
6. `agent/core/tool_executor.py:786-793` — the parent key is placed into `agent_config` **before** any credential gate; any tool declaring the flag receives it, and the value is then inside a dict that tools may echo (`agent_config` is a plain dict field on `ToolBase`).
7. `thoughtmachine/security.py:446-448` — the vault block is a **`startswith` prefix test without a path separator**, so a sibling directory named `<vault>-anything` matches too (over-blocking); conversely nothing blocks a *symlink* placed in the workspace that points into the vault, because `requested_abs = os.path.abspath(path)` does **not** resolve symlinks (`:434-439`, comment "(without following symlinks)") — the realpath is computed only for the comparison root `:444-446`. LEAK CANDIDATE (INFERRED, not exercised): `workspace/link -> ~/.thoughtmachine/credentials/global/x` passes the vault test and then passes the workspace containment test, since `target_rel` is derived from the unresolved path. Also note the workspace deny-list contains only five entries (`.git/config`, `.git/HEAD`, `.ssh`, `.npmrc`, `.env` — `:81-87`) and no generic `*.pem` / `id_rsa`.
8. `tools/mcp_client.py:286-287` (raw unparsable line logged whole) and `:88-90` (server notifications logged whole) — carried over from A.5; an MCP server that prints a token on stderr/stdout gets it into the log.

---

### B.11 SUMMARY TABLE — CREDENTIALS (one line per path)

| path | gate | re-check vs snapshot | snapshot named |
|---|---|---|---|
| P1 `{{credential:}}` in tool args | `credential:use` (`tool_executor.py:883-913`) | moment of use | — |
| P2 placeholder in `config.api_key` | **none** | SNAPSHOT-TRUSTED | `AgentConfig.api_key` @ `agent/core/agent.py:1015` |
| P3 `inherits_parent_api_key` handover | **none** (class flag only) | SNAPSHOT-TRUSTED | `self.config.api_key` @ `tool_executor.py:791-793` |
| P4 `providers.json` | file mode / redaction only (no 0o600) | SNAPSHOT-TRUSTED | `ProviderManager._profiles` @ `provider_profile.py:36-61` |
| P4b env-var provider key | provider factory raise-if-absent | moment of use | — (`llm_providers/factory.py:91-100`) |
| P5 git askpass env | presence of `TM_GIT_ASKPASS_SECRET` + `Secret`-rejection | moment of use | — |
| P6 MCP api_key | warn-if-unset | SNAPSHOT-TRUSTED (manager) | `MCPServerConfig.api_key_env_var` |
| P7 host env | `CREDENTIAL_SHAPED` strip/refuse | moment of use | — |
| store `credentials/<ws>/<key>` | caller-side only | moment of use | — |
| store `credentials/global/<key>` | caller-side only + UI auth boundary | moment of use | — |


---

## C. CONTAINERS (IN PROGRESS — C.1–C.2 written, C.3+ NOT DONE)

### C.0 Files read for this section (MEASURED, partial)

- `infra/containers/executor.py` (1734) — `build_container_name`, `_resolve_container_config_via_gate`, `DockerExecutor._ensure_container` / `_compute_container_config` / `_reap_container`.
- `infra/container_manager.py` (6323) — second caller of `build_container_name` (`:1590`). **NOT YET READ beyond the call site.**
- `infra/container_lifecycle.py`, `infra/container_create.py`, `infra/container_destroy_audit.py`, `infra/container_registry.py`, `infra/container_env.py`, `infra/resource_container_manager.py`, `thoughtmachine/container_record/`, `tools/container_control.py`, `tools/docker_code_runner.py` — **NOT YET READ**.
- The doctrine quote: `scripts/gen_handoff.py:218` — "**The container envelope is its birth certificate.** Stop -> Edit -> Restart (recreate)."; restated `docs/DIRECTION_2026-11-22_GEN67.md:132`; stated as a table row in `.thoughtmachine/knowledge/project/architecture_invariants.md:54-56` ("a container born `ro` can never be reused by an `rw` session … the envelope is the container's *birth certificate*"); the open instance is C30 (`.thoughtmachine/knowledge/project/PRIORITIES.md:173` — `cpu_quota` hardcoded on the persistent create path, "do not mutate cgroups at runtime"). MEASURED (all quotes read).

---

### C.1 THE CONTAINER NAME IS THE ENVELOPE — the envelope is hashed into a **string**, and the string is the pool key

**(1) DECLARED** — `build_container_name(workspace_path, lifecycle_class, session_id=None, network_mode="none", workspace_mode="ro")` (`infra/containers/executor.py:267-269`), docstring `:270-283`: `ws_hash = sha256(normalize_workspace_path(path))[:12]`, `env_hash = sha256(f"{network_mode}:{workspace_mode}")[:8]` (`:284-290`), giving `agent-exec-persistent-{ws_hash}-{env_hash}` or `agent-exec-ephemeral-{ws_hash}-{tag}-{env_hash}` (`:291-293`). Path normalisation is a single function `normalize_workspace_path` = `os.path.abspath(...).rstrip("/")` (`:243-251`). MEASURED.

**(2) OWNER** — the owner is a **STRING, not an import**: the container *name* is the pool key, produced on demand and re-derived at each call site; no module holds a registry of names. Four independent derivation sites (`infra/containers/executor.py:481`, `:800`, `:1285`, `:1409`, `:1553`; `infra/container_manager.py:1590`; `thoughtmachine/security.py:726`) — each recomputes it from `(workspace_path, lifecycle_class, session_id, network_mode, workspace_mode)`. MEASURED (call sites), INFERRED (that no registry exists).

**(3) READ** — (a) as the docker lookup key: `self.client.containers.get(container_name)` (`:829-833`, inside the `NotFound`-tolerant block at `:830-837`); (b) by `_remove_container_by_workspace` (`:1397`), `rebuild_container` (`:1450`), `get_container_status` (`:1520`); (c) by `thoughtmachine/security.py:726`. MEASURED (a, and the def lines of b/c).

**(4) ENFORCED** — the envelope is enforced **by identity of the name**, not by a check: a session whose grant changes `(network_mode, workspace_mode)` resolves a *different* name, therefore `containers.get()` misses, therefore a new container is created (`:823-837` → create path). The doctrine at `docs/DIRECTION_2026-11-22_GEN67.md:132` states this explicitly: "the container name is derived from the envelope, not reconfigured into it". MEASURED (code + doc).

**(5) LOGGED** — the name appears in the `_ensure_container` DEBUG lines (`:749-752` `{"workspace_path", "image", "has_container", "force_rebuild"}`, `:882-885` `{"container_id", "name"}`) and in audit events (C.2). MEASURED.

**(6) LIFETIME** — created per call by hashing; **cannot be invalidated** (it is a pure function of its inputs), but the *container it names* can be reaped and re-created under the same name. No TTL, no expiry field. MEASURED (pure function), INFERRED (no invalidation path in the read range).

**MALFORMED / MISSING:** `_safe_session_tag(None)` returns the literal `"anon"` (`:262-263`), so **every session-less ephemeral container in a workspace collides on one name** — and any two distinct session ids that sanitise to the same 16-char prefix (`:260-261`) also collide. `_hash_executor_build_bytes` (`:93`) is unrelated. MEASURED (the fallbacks), INFERRED (the collision consequence).

---

### C.2 RE-CHECK AT USE, BUT ONLY FOR A LIVE OBJECT — and drift is *refused*, not repaired

**(1) DECLARED** — `DockerExecutor._compute_container_config` (`:683-704`) resolves the desired `(network_mode, workspace_mode)`; `_resolve_container_config_via_gate` (`:296-350`) is the shared adapter, invoked in **DISK-AUTHORITATIVE mode (`use_disk=True`)** with the docstring "the on-disk workspace ceiling is the source of truth and the in-memory `session_permissions` is not trusted as a fallback" (`:308-317`), and it is "Pure, total, fail-closed: a falsy workspace id, a `ContainerConfigError` … or any exception returns the locked-down `("none", "ro")`" (`:318-320`). MEASURED.

**(2) OWNER** — `DockerExecutor` instance (`workspace_path`, `session_permissions`, `workspace_id`, `session_id` attributes, `:649-682`) plus the **live docker object** `self.container`. MEASURED.

**(3) READ** — three reads of the *actual* container, all from `container.attrs`: `HostConfig.NetworkMode` and the `/workspace` mount `Mode` (`:762-767`); the same pair again for a name-matched container (`:840-847`); and image id drift `attrs.get('Image')` vs `client.images.get(self.image).id` (`:878-891`). MEASURED.

**(4) ENFORCED** — **RE-CHECK AT MOMENT OF USE, against the live object, before every exec.** Both the cache-hit fast path (`:757-790`) and the name-match path (`:839-877`) recompute desired config and compare it to the live container's actuals. On mismatch: the fast path reaps and recreates (`:775-789`, audit `CONTAINER_RECREATE_MISMATCH … source=live_object`); the name-match path routes through `ContainerLifecycle.reconcile(existing, self.session_permissions, intent)` (`:838-853`) and, **if the funnel does not destroy, the drifted container is REUSED and the exec continues** — `CONTAINER_DRIFT_REFUSED` audit + `return {"status": "refused", …}` (`:854-872`). So the envelope is re-checked on every use, but a *drift* verdict is not fail-closed: the agent keeps executing in the old container. MEASURED (code), INFERRED (the security consequence). No snapshot is used on this path — **nothing here is SNAPSHOT-TRUSTED**.

**(5) LOGGED** — `audit_event` at `:768-773` (`CONTAINER_RECREATE_MISMATCH`, fast path), `:786-788` (`CONTAINER_REUSE_OK`, `source=live_object`), `:817-819` (`CONTAINER_RECREATE`, `reason=force_rebuild`), `:865-870` (`CONTAINER_DRIFT_REFUSED`, with `reason=decision.reason`), `:874-877` and `:893-897` (`CONTAINER_RECREATE_MISMATCH`), `:900-903` (`CONTAINER_REUSE_OK`, `source=name_match`). Plus structured `log(...)` lines at `:749`, `:776-780`, `:789-792`, `:858-863`, `:870-875`, `:880-886`, `:898-901`. MEASURED.

**(6) LIFETIME** — desired config is computed **per `_ensure_container` call** (moment of use); the *actual* is read from the live container object; no cached capability snapshot participates. Contrast with the doctrine: Stop → Edit → Restart *recreates*, which is the only supported way to change the envelope. MEASURED (within the read range), INFERRED (that no other path exists — `infra/container_manager.py`, `infra/container_lifecycle.py` unread).

**MALFORMED / MISSING:** the fast path (`:757-790`) does **not** consult `ContainerLifecycle.reconcile` or the destroy gate at all — it calls `self._reap_container(self.container)` directly on mismatch (`:786`), i.e. the lifecycle funnel is bypassed on the cached-object path but required on the name-match path. **DISAGREEMENT (MEASURED).** Also `_reap_container` is called at `:817-819` for `force_rebuild` with **no reconcile**, and the drift-refusal branch returns a dict into `_ensure_container`, whose caller treats a truthy return as success — the exec path then runs against `existing` (`:871` `self.container = existing`). INFERRED from `:871-872`.

---

## D. FILESYSTEM ROOTS — **NOT WRITTEN**
## E. WORKERS / SESSIONS AS RESOURCES

### E.0 Files read for this section (MEASURED)

- `tools/workspace/worker_thread.py` (9697) — the worker body: `_restrictive_merge` (`:1472-1530`), footprint canonicalisation (`:1532-1560`), the `_session_permissions` mirror (`:2203`, `:2207-2209`), the `AgentConfig` injection (`:4100-4115`), `stop`/`detach` (`:2868-2899`), the supervisor spawn path (`:8028-8095`).
- `tools/workspace/worker_manager.py` (1052) — spawn/cap/stop facade; detach TTL check; stale-reaper opt-in.
- `tools/workspace/worker_query.py` (441) — `deliver_query_and_block` contract, `WorkerBusyError`, `_resolve_terminal`.
- `tools/workspace/worker_runtime.py` (1730) — the runtime-mode flag, the handle, `worker_is_live`.
- `tools/workspace/worker_registry.py` (539) — in-process thread registry, epochs, `worker_dir()`.
- `tools/workspace/worker_container.py` (207) — worker-container ownership by label **value**.
- `tools/workspace/job_registry.py` (391) — non-blocking job store.
- `security/security_gate.py` (1945) — `get_effective_permissions` disk mode (`:1133-1265`).
- `agent/core/tool_executor.py` (1140) — the per-call gate invocation (`:551-767`); `agent/config/models.py:226-242`; `agent/models/worker_definition.py:41-46`.
- **NOT YET READ:** `worker_runtime_process.py` (1284), `worker_runtime_main.py`, `worker_runtime_protocol.py` (seen only by grep: `worker_runtime_process.py:110` `CANCEL_REASON_OPERATOR_KILL = "operator_kill"`).

---

### E.1 A WORKER IS TWO BINDINGS — a registry entry in the supervisor's memory, and (by default) a body in *another OS process*

**(1) DECLARED** — one flag selects the body's home: `WORKER_RUNTIME_MODE ∈ {thread, process}`, `DEFAULT_RUNTIME_MODE = MODE_PROCESS` (`worker_runtime.py:73-90`; §10 / D11 / C62). It is "read **at spawn time** so a single worker can be moved between modes without restarting the server … resolved on every `WorkerRuntimeHandle.spawn` call and never cached at import time" (`:38-43`). `resolve_runtime_mode` (`:357-379`): absent → the default; present and not *exactly* a mode → `InvalidRuntimeModeError` — "fail closed: no trimming, no case folding, no empty-string-as-default", because "'I did not understand the config' and 'run in-process' are very different statements about isolation" (`:45-49`, `:361-370`). MEASURED.

**(2) OWNER** — the supervisor's `WorkerRegistry`: `_worker_registry[(session_id, worker_name, instance_id)] -> thread` plus `_worker_epochs`, guarded by `_registry_lock` (`worker_registry.py:241-265`). The on-disk half is **path maths only** — `WORKERS_DIRNAME = "workers"` (`:21`), `worker_dir()` = `<workspace>/workers/<session_id>/<instance_label>` (`:204-238`), `instance_label = worker_name or "name#instance_id"` (`:194-202`); a sessionless worker ("an operator or test spawn with no session", `:217`) degrades to the legacy `<workspace>/workers/<label>`. The second binding is the runtime handle, `_RUNTIME_HANDLES` keyed the same way (`worker_runtime.py:109-119`), "POPULATED IN PRODUCTION since S7a-3: `worker_thread._action_spawn` registers the handle". MEASURED.

**(3) READ** — three different questions, three different readers: (a) *is there a reachable body?* — `worker_is_live` is "the ONE liveness predicate for both runtime modes, because the two modes keep their bodies in different places and `is_alive()` answers about only the wrong one of them for a process-mode worker" (`worker_runtime.py:382-400`; a duck-typed raise reads as **LIVE**, "fails OPEN"); (b) *who exists?* — `get_all_workers` (`worker_registry.py:316`), `find_workers_by_name` (`:321`); (c) *what happened to it?* — `thread.status`, written to the status file by the worker itself (`worker_thread.py:2889-2890` `self.status = "detaching"` → `self._write_status_file()`) and read back by `_resolve_terminal` (`worker_query.py:325-330`). MEASURED (the predicates), INFERRED (that no other read exists — the process-runtime modules are unread).

**(4) ENFORCED** — the mode is enforced **per spawn and fails closed**: an unusable flag raises out of `resolve_runtime_mode` before any handle exists (`:367-372`), and `for_worker` calls it as the mode's only source (`:835`). The definition side is equally loud: `_canonicalize_worker_footprint` runs at the definition LOAD boundary (`worker_thread.py:2205-2209`) and "any other unknown category raises `ValueError` naming the key AND its value: an unresolvable definition is a loud load error, not a silent deny" (`:1539-1560`). MEASURED.

**(5) LOGGED** — the mode actually in force is announced at most once per process, with its consequence spelled out ("worker bodies run in a child process and `WorkerThread.start()` deliberately starts no in-process body", `worker_runtime.py:136-174`), and explicitly **diagnostics only** — "nothing is cached, returned or enforced here" (`:142-146`). Registry spawns are journaled via `log_worker_event(worker_name, 'spawned', session_id=…)` (`worker_registry.py:264`). MEASURED.

**(6) LIFETIME** — resolved at each spawn, never memoised (`:360-361`), then pinned for that incarnation by the handle (`:1725-1729` `attach_thread_frame_sink` → `worker._runtime_handle`). Flipping the env var therefore affects the **next** spawn only, and two workers of one session can legitimately run in different modes. MEASURED (per-spawn resolution), INFERRED (the mixed-mode consequence).

**MALFORMED / MISSING:** `_RUNTIME_HANDLES` is a module-global dict guarded by one lock and keyed by the *raw* `session_id` the routes look up with; a miss is "an ordinary answer" meaning "use the command file" (`:113-119`) — so a handle-less worker silently takes a different transport with no error. MEASURED (the fallback), INFERRED (the silence).

---

### E.2 THE SPAWN-TIME PERMISSIONS MIRROR — **SNAPSHOT-TRUSTED**, and explicitly non-authoritative

**(1) DECLARED** — the docstring of `_restrictive_merge` is the doctrine, verbatim (`worker_thread.py:1472-1481`): "This helper only builds the worker's *spawn-time* `session_permissions` mirror — the `SessionPermissions` snapshot its `AgentConfig` is constructed with. It is NOT the runtime cap: the declared worker footprint is passed separately to `AgentConfig.permission_footprint` and applied by the security gate on top of the grants re-read from disk on every tool call, so a grant written after spawn takes effect without respawning the worker. **Never treat this function's result as the authority for what a running worker may do.**" MEASURED.

**(2) OWNER** — `WorkerThread._session_permissions` (`:2203`), assigned once in `__init__` from the constructor argument and never reassigned — the mirror is **frozen** for the incarnation. Ownership of the *object* is deliberately severed at spawn: "Spawn-time snapshot semantics must be enforced by COPY: passing the supervisor's object by reference would leave every worker of a session sharing ONE mutable permissions dict" (`:8045-8050`, `dict(_spawn_permissions)`), handed in at `:8086`. Merged form lives on `worker_cfg["session_permissions"]` (`:4106`) → `AgentConfig.session_permissions`. MEASURED.

**(3) READ** — three readers, and their disagreement *is* the finding: (a) `:4101` `merged = _restrictive_merge(self._session_permissions, self._permission_footprint)` → `AgentConfig.session_permissions` → read back as `self.config.session_permissions` at `agent/core/tool_executor.py:551-553` — **this is the mirror**; (b) the cap travels *separately* (`:4108-4115`: "Inject the DECLARED worker cap separately (do NOT collapse it) … Folding it into `session_permissions` here would re-freeze the session snapshot for the worker's whole lifetime: a grant written to disk after spawn could then never take effect until the worker was respawned"); (c) the gate call `get_effective_permissions(session_perms_obj, caps, session_id=…, workspace_id=…)` (`agent/core/tool_executor.py:653-658`), which in **disk mode replaces its `session` argument** with the vault record (`security/security_gate.py:1220-1224`, assignment `:1253-1255`). MEASURED.

**(4) ENFORCED** — **RE-CHECK AT MOMENT OF USE**, and the mirror is not the cap. The authority model is stated in the config field itself: "the session's live permissions are re-read from disk on every tool call, so a later grant must take effect without respawning the worker … the effective permissions of a worker are `min(permission_footprint, live_session - workspace_ceiling)`" (`agent/config/models.py:230-238`). The gate is handed `permission_footprint` as the cap "and **never** the spawn-time snapshot of the session's permissions (`session_perms_obj`), which would pin the worker to the permissions the session happened to hold at spawn and make a later grant ineffective until respawn" (`agent/core/tool_executor.py:680-705`). Disk mode is authoritative and fail-closed: unreadable store ⇒ `_DISK_FAIL_CLOSED_SESSION` + `_DISK_FAIL_CLOSED_CEILING` (`security_gate.py:1243-1244`), never "no ceiling" (`:1113-1129`). So: **the mirror is SNAPSHOT-TRUSTED but never load-bearing** — with one exception, the id-less executor (`agent/core/tool_executor.py:756-757` `tool_args['session_permissions'] = session_perms_obj.to_dict()`), which is the "legacy behaviour unchanged" path (`security_gate.py:1186-1190`). MEASURED (code), INFERRED (that the id-less path has no worker caller).

**(5) LOGGED** — a disk-read failure is logged **and** surfaced: `logger.warning("get_effective_permissions: vault permission read failed (workspace_id=%s session_id=%s): %s; failing CLOSED", …)` (`:1230-1237`) with the same cause carried into the operator-facing denial as `fail_closed_reason = f"{type(exc).__name__}: {exc}"` (`:1242`) — "fail CLOSED, but never silently" (`:1226-1229`). MEASURED.

**(6) LIFETIME** — created at spawn, frozen (`:2203` is the only assignment), and **not invalidatable**: only kill+respawn would change it — which is precisely why nothing load-bearing reads it (`:1472-1481`). Effective grants, by contrast, have a per-tool-call lifetime. MEASURED.

**MALFORMED / MISSING:** the same permission set travels under three names — `session_permissions`, the DECLARED `permission_footprint` (`agent/config/models.py:226-242`) and the legacy alias `worker_permissions` (`agent/models/worker_definition.py:41-46`), read together at `worker_thread.py:2207-2209`. "Empty (the default) means 'no declared cap' — nothing is narrowed" (`models.py:238`), so a definition that omits the footprint caps **nothing** (`agent/core/tool_executor.py:698-705`). `_restrictive_merge` is fail-closed in the narrowing direction (a category the session does not expose resolves to `_load_safe_defaults()`, never to the worker's own value, `:1487-1491`), and `_canonicalize_session_permissions` silently drops the four legacy grains `git_read`/`git_write`/`system`/`execution` (`:1507-1530`). MEASURED.

---

### E.3 `stop()` DETACHES, `kill()` TERMINATES — and the registry entry outlives both

**(1) DECLARED** — `stop()` is an alias for `detach()`: "Detach the worker: park it without terminating it (PHASE 2.1a). … Termination is `kill`" (`worker_thread.py:2868-2874`). `detach()` docstring (`:2876-2886`): "The run loop is unblocked and parks in the detached state, so the worker stops pulling queries — but the thread, its containers and its registry entry all survive. Only an operator `resume` revives it: a query submitted while the worker is detached is QUEUED … never a resume. Context is persisted and the worker is registered as detached so the stale-container sweep skips it until the detached TTL expires." Manager side: "`thread.stop()` now DETACHES — it parks the worker — so every manager stop path calls `kill()`" (`worker_manager.py:23-27`), and "`stop_worker`/`stop_all` kill threads but leave their registry entries in place …; `cleanup_dead` sweeps non-alive entries" (`:27-29`). MEASURED.

**(2) OWNER** — the thread's own flags, set **synchronously** in `detach()`: `status = "detaching"` + `_write_status_file()` *before* the run loop acknowledges, then `_detach_requested`, `_detached_at = time.time()`, `_park_event.set()`, `_resume_event.clear()`, `_turn_cancel.set()`, `_save_context()` (`:2887-2903`). The TTL that eventually kills it is owned by the manager: `_check_detached_ttl` (`worker_manager.py:208-239`). MEASURED.

**(3) READ** — `_is_detached` reads `thread.status == "detached"` (`worker_manager.py:204-206`); the TTL check reads `thread._detached_at` and **skips** a worker whose stamp is unreadable, "never kill on a guess" (`:213-214`); `_resolve_terminal` reads `thread.status` (`worker_query.py:325-330`). MEASURED.

**(4) ENFORCED** — detach is enforced by the run loop parking, not by a flag check at query time: a graceful stop CANCELS the in-flight turn ("a long-blocking tool must return instead of holding the park back") while `_stop_event` stays clear — "this is the 'Stop now' signal, **NOT** termination" (`:2895-2899`). The TTL kill announces and audits **before** killing (`worker_manager.py:214-215`), then reclaims the detached worker's containers and drops the registry entry (`:216-217`), rejecting that worker's queued queries first (`:281-282`). Loudness on the query path: a busy worker raises `WorkerBusyError` — "sync queries are not queued in W2 — spawn a new worker or retry when it is ready" (`worker_query.py:412-418`); at the cap the manager "refuses loudly instead of silently dropping the request or queueing" (`WorkerCapExceeded`, `worker_manager.py:104`, `:123`). MEASURED.

**(5) LOGGED** — `announces {status: "killed", reason: "detached_ttl_exceeded", detached_age_s}` (`:249-251`) and `audit_event(… "detached_age_s=… ttl_s=WORKER_TIMEOUT_SECONDS")` (`:271`); manager-initiated stops journal `_MANAGER_STOP_JOURNAL_REASON = "stopped-by-manager"` and flip the record to `cancelled` / the job to `stopped` (`:70-73`). MEASURED.

**(6) LIFETIME** — a detached worker survives until its TTL **or** an operator `resume`/`kill`; a killed worker's registry entry survives the kill and is swept only by `cleanup_dead`; the per-session spawn cap counts only LIVE threads (`is_alive()`), so dead entries never block a respawn (`worker_manager.py:17-19`, `:27-29`). Crucially, **the TTL reaper is OFF by default**: `WORKER_STALE_CHECK_ENABLED` unset ⇒ "the stale-worker reaper is DISABLED. Detached workers will NOT be reaped and will stay parked until an operator resumes or kills them by hand" (`:978-1015`) — an operator notice emitted at most once per process, not per request (`:964-967`, "~23k times" in production). MEASURED.

**MALFORMED / MISSING:** by default detach is therefore **unbounded in practice** even though the TTL machinery exists, and the switch that fixes it also arms `_interrupt`, which "kills in-flight executions of heartbeat-stale workers — which the announce-only watchdog deliberately never does" (`worker_manager.py:981-985`): one flag, two unrelated behaviours. Also, `CANCEL_TERMINAL_STATUSES` lists `killed`/`detaching`/`detached`/`error`/`completed` but "`stopped` is deliberately absent: `WorkerThread` never assigns that status (it survives only in a comment, `worker_thread.py:1103`)" (`worker_runtime.py:92-107`) — yet `_resolve_terminal` still carries an explicit `status == "stopped"` branch (`worker_query.py:328-329`). **RESIDUAL VOCABULARY (MEASURED); dead branch (INFERRED).**

---

### E.4 WHAT ELSE A WORKER HOLDS ONTO — containers by label value, jobs in memory

| binding | how it is keyed | how it is read | lifetime | tag |
|---|---|---|---|---|
| worker containers | the label **`thoughtmachine.worker`** compared by **exact value** to `<session_id or 'unknown'>:<worker_name>` — "stale/mismatched values are IGNORED" | `worker_container.py` ownership helpers | torn down with the worker | MEASURED (`worker_container.py:1-40`) |
| resource containers | label `thoughtmachine.resource`, `tm-res-*` names, `tm-resource-git` image | the same helpers | **`never touched during worker teardown`** — shared workspace infrastructure | MEASURED (`:1-40`) |
| worker container objects | `container_manager` built at spawn with `session_permissions=self.effective_permissions or self.session_permissions` | passed into `WorkerThread` (`worker_thread.py:8036-8041`, `:8085`) | per worker | MEASURED |
| jobs | `job_id`, in a bounded in-memory store; mirrors `worker_running` / `worker_partial_result` / `worker_completed` / `worker_timeout` / `worker_error` | `submit_query` returns a `job_id` immediately; full envelope stored | bounded by `JOB_REGISTRY_MAX_JOBS`, terminal statuses | MEASURED (`job_registry.py:1-12`, `:28-39`) |
| registry entries | epoch compare-and-set `(session, worker, instance)` | `register_worker` refuses a stale incarnation, "registry untouched" | survives kill; `cleanup_dead` sweeps | MEASURED (`worker_registry.py:241-258`) |

**Note (MEASURED, `job_registry.py:31-33`):** a cross-session job read is logged at **WARNING** so "probing another session's job ids stays visible to operators" — the only binding in this section whose *read* is itself audited.

---
## F. MODEL / PROVIDER BINDING

### F.0 Files read for this section (MEASURED)

- `agent/core/llm_client.py` (602) — the only provider construction site, error mapping, telemetry meta.
- `llm_providers/factory.py` (137, read in full) — provider registry, key resolution, the two refusals.
- `llm_providers/base.py` (240, `ProviderConfig` + `__post_init__`) — the object that IS the snapshot.
- `llm_providers/openai_compatible.py` (1073, targeted ranges) — where `model` reaches the wire, per-call kwargs merge, retry policy, exception translation, `LLMResponse.model`.
- `agent/config/models.py` (`AgentConfig` provider fields, `FIELD_CATEGORIES`, `resolve_from_profile`), `agent/config/session_config.py` (the session override fields + `to_agent_config`).
- `agent/core/agent.py` — `LLMClient` construction (`:594`), credential placeholder (`:985-1015`), `restart()` (`:1016-1124`), `chat_kwargs` (`:1844-1846`), error surfacing (`:2018-2120`).
- `web_ui/backend/session_manager.py:126-131` (`_resolve_provider_and_model`), `web_ui/backend/config_manager.py:66` (`model_override` default).
- `agent/config/provider_profile.py` — `~/.thoughtmachine/providers.json` (the profile layer; leak analysis in B.5/B.10).

---

### F.1 WHERE MODEL / PROVIDER / BASE URL / KEY COME FROM — four layers, one of which is a **string key into a dict**

**(1) DECLARED** — four independent declaration layers, each able to supply the same four values:

| layer | location | fields |
|---|---|---|
| `AgentConfig` | `agent/config/models.py:140-168` | `api_key: str = Field(default='', exclude=True)` (`:140`, wrapped into `Secret` by the validator at `:142-157`), `base_url: str = 'https://api.deepseek.com'` (`:159`), `model: str = 'deepseek-reasoner'` (`:160`), `provider_type: str = 'openai_compatible'` (`:165`), `model_override: Optional[str]` (`:168`) |
| `SessionConfig` | `agent/config/session_config.py:184-206` | `provider_id`, `model`, `api_key` ("Excluded from serialization", `:192-194`), `base_url`, `model_override`, `provider_config: Dict` |
| provider profile | `agent/config/provider_profile.py` → `~/.thoughtmachine/providers.json` | `provider_type` / `base_url` / `api_key` / `default_model` per named profile |
| environment | `llm_providers/factory.py:88-101`, `agent/core/llm_client.py:28-33` | `{PROVIDER_TYPE.upper()}_API_KEY` (`:90-91`), `OPENAI_API_KEY` (`:94-95`), `LLM_TIMEOUT` / `LLM_MAX_RETRIES` (`:28-33`) |

MEASURED (all four layers read).

**(2) OWNER** — and two of the owners are **STRINGS, not imports**:
- the **provider class** is owned by a string key in `ProviderFactory._providers` — `{"openai_compatible": OpenAICompatibleProvider}` plus `"anthropic"` if its import succeeds (`llm_providers/factory.py:28-42`). `provider_type.lower()` indexes it (`:73`, `:82`); `"openai"` is aliased to `"openai_compatible"` (`:76-77`). The class is looked up **by name at construction time**; no module-level import binds it.
- the **profile** is owned by `provider_id`, a string naming a row in `~/.thoughtmachine/providers.json`; `AgentConfig.resolve_from_profile` calls `manager.get_profile(self.provider_id)` (`agent/config/models.py:484-486`).
- the **provider instance** is owned by the `LLMClient` instance: `self.provider` (`agent/core/llm_client.py:280`), which itself owns the `ProviderConfig` dataclass (`llm_providers/base.py:185-190` `self.config = config`).
- the **live config** is owned by `Agent.config` and rebound only by `Agent.restart()` (`agent/core/agent.py:1064-1068`).
MEASURED.

**(3) READ** — the whole chain is five reads long:
1. `LLMClient.__init__`: `pc = dict(getattr(config, 'provider_config', None) or {})`, then `timeout`/`max_retries` (`llm_client.py:277-279`, env-first per `:28-33`), then **one** call: `self.provider = ProviderFactory.create_provider(provider_type=config.provider_type, api_key=config.api_key, base_url=config.base_url, model=config.model, temperature=config.temperature, timeout=timeout, max_retries=max_retries)` (`:280`). MEASURED.
2. `factory.create_provider`: key resolution waterfall `api_key = api_key or config.get("api_key") or os.getenv(env_var)` (`:91`), then the `OPENAI_API_KEY` fallback for `openai`/`openai_compatible` (`:94-95`); builds `ProviderConfig(api_key=…, base_url=config.get("base_url"), model=config.get("model", ""), …)` (`:104-114`). MEASURED.
3. `ProviderConfig.__post_init__`: a plain-`str` key is upgraded to `Secret` so the provider layer's log lines mask it (`llm_providers/base.py:141-151`). MEASURED.
4. the wire: `completion_kwargs = {'model': self.config.model, 'messages': build_wire_messages(…) }` (`openai_compatible.py:477-483`), same at `:765-768` for the streaming path. MEASURED.
5. telemetry: `_provider_meta` (`llm_client.py:473-490`) and `log_provider_event(..., model_name=…)` (`llm_client.py:551-553` → `agent/logging/lifecycle.py:210-226`). MEASURED.

**(4) ENFORCED** — two fail-closed refusals, and both **name the owner**:
- `Factory.create_provider:82-86` — `raise ProviderNotFoundError(f"Provider '{provider_type}' not found. Available: {list(providers.keys())}")`; names the offending string and enumerates the legal ones.
- `:97-101` — `raise InvalidConfigError(f"No API key found for provider '{provider_type}'. Provide api_key parameter or set {provider_type.upper()}_API_KEY environment variable.")`; names the provider **and** the two remedies. Absent-or-empty key ⇒ **refusal with the owner named**, not a silent degradation. MEASURED.
- `create_from_dict` refuses a dict without `provider_type` (`:134-135`). MEASURED.

**PER-CALL / PER-WORKER OVERRIDE — the mechanism exists, and nothing uses it.**
- Mechanically a per-call override **is** possible: both request paths end with `completion_kwargs.update(kwargs)` (`openai_compatible.py:485` non-streaming, `:772` streaming), so a `model=` kwarg reaching `provider.chat_completion/stream_completion` **replaces** `config.model` for that one request. MEASURED (the merge), INFERRED (that it wins — `update` is ordered after the literal, and `if 'temperature' not in completion_kwargs` guards show the author knew kwargs win).
- No caller supplies one: the only request site builds `chat_kwargs = {'temperature': self.runtime_params.temperature}` plus optionally `top_p` (`agent/core/agent.py:1844-1846`) and passes it at `:1866`. `grep chat_kwargs` returns exactly those three lines. So **no per-call model override path exists in the agent today**. MEASURED.
- No per-worker override either: `agent/models/worker_definition.py` has no `model`/`provider` field (grep: only the pydantic `model_config` at `:27`). A worker inherits the session's `AgentConfig`. MEASURED (absence), INFERRED (inheritance — the worker path is section E's `worker_thread.py`, not traced here).
- The only *supported* per-session override is `model_override` (`models.py:168`, resolved `:498-505`: `model_override > explicit user model > profile.default_model`; same ordering restated in `provider_profile.py:184-187`; forwarded by `session_config.to_agent_config` `:359`), and it is applied by **rewriting `model` at profile-resolution time**, not per call. MEASURED.

**(5) LOGGED / TELEMETRY — and the model identity recorded is NOT the served model.** `LLMResponse` is constructed with `model=self.config.model` (`openai_compatible.py:1041`), i.e. the *requested* string is echoed back as the response's model. `_provider_meta` then prefers `getattr(response, "model", "")` and falls back to `self.config.model` (`llm_client.py:483-486`), so `model_name` in `provider_raw.jsonl` is the **requested** model in both branches. Any server-side alias/redirect (e.g. a provider that serves `deepseek-reasoner` for `deepseek-chat`) is invisible: **the model identity used for a call and the one recorded are the same string by construction** — which is not the same thing as being verified. MEASURED (both lines), INFERRED (the consequence). The key itself is written into the DEBUG line at `openai_compatible.py:244` and `:493` as `api_key={config.api_key}` — masked only by the `Secret` wrapper (B.10 item 4). MEASURED.

**(6) LIFETIME — SNAPSHOT-TRUSTED. The snapshot is the `ProviderConfig` built once at `llm_providers/factory.py:104-114` and held as `provider.config` (`llm_providers/base.py:185-190`); the four `AgentConfig` values are read exactly once in `LLMClient.__init__` (`agent/core/llm_client.py:277-280`).**
- Created: at `Agent` construction (`agent/core/agent.py:594` `self.llm_client = LLMClient(config, session, self.logger)`).
- Re-taken: only by `Agent.restart(new_config)` — `self.llm_client = LLMClient(new_config, self._session, old_logger)` (`agent/core/agent.py:1068`) — and on rollback `LLMClient(old_config, …)` (`:1119`).
- Declared intent: `provider_type`, `model`, `api_key`, `base_url`, `provider_config`, `provider_id`, `model_override` are all `RESTART_REQUIRED` in `AgentConfig.FIELD_CATEGORIES` (`agent/config/models.py:90-129`, constant `:80`).
- **DISAGREEMENT (MEASURED): `FIELD_CATEGORIES` has NO runtime reader.** `grep -rn FIELD_CATEGORIES agent/ web_ui/` returns exactly two hits: the definition (`models.py:90`) and a docstring mention (`agent/core/agent.py:90`). The "restart required" classification therefore does not *enforce* anything — it is metadata nobody consumes. Combined with the snapshot above, the effective behaviour is: a config change that is written anywhere other than through `Agent.restart()` leaves the old provider, old model and old key in use indefinitely. INFERRED from the measured grep + measured single construction site.
- The `{{credential:<key>}}` form of `api_key` is resolved *inside* `restart()` (`agent/core/agent.py:1014` `self._resolve_credential_placeholder(new_config)`, body `:985-1013`); a resolution failure raises `CredentialError`, `restart` catches it (`:1114-1124`), rebuilds the **old** client with the **old** config (`:1119`) and returns `False`. So a bad credential binding cannot half-apply: the previous snapshot survives. MEASURED.

**MALFORMED / ABSENT / EXPIRED — four different behaviours, only two of which are refusals:**

| condition | behaviour | site |
|---|---|---|
| key absent or `''` (the `:140` default) | **refusal naming the provider + the env var** | `factory.py:97-101` |
| `provider_type` unknown | **refusal naming the sandbox of legal values** | `factory.py:82-86` |
| key present but **wrong/expired** | provider raises → `AuthenticationError` → mapped to `LLMError(error_type='authentication_error')` (`llm_client.py:403-409`); the agent logs it, appends a `[SYSTEM NOTIFICATION] Error: AUTHENTICATION_ERROR: …` user-role message and emits `{'type':'error','error_type':…,'stop_reason':'error'}` (`agent/core/agent.py:2038-2071`). **No retry, no fallback.** | MEASURED (mapping + surfacing), INFERRED (that the SDK 401 reaches it — the only translations found are `openai_compatible.py:622,638`, both of which wrap a *connection* error as `AuthenticationError`) |
| model name not served | `ModelNotFoundError` → `'model_not_found'` (`llm_client.py:403`) — same non-retrying error path. **`openai_compatible.py` contains no translation producing `ModelNotFoundError`** (grep: 0 hits) — the mapping entry is unreachable from this provider. | MEASURED (0 hits + mapping present) |
| rate limited | `RateLimitExceeded` is re-raised bare (`llm_client.py:398-399`) and IS retried, with growing backoff capped at 60 s (`agent/core/agent.py:648-653`, `:1821-1825`) | MEASURED |
| stream timeout before first token | retried up to `1 + max_retries` attempts (default 4) inside a total budget; **not** retried once anything reached the caller (`openai_compatible.py:917-936`) | MEASURED |

So: **absent/empty and unknown-provider = refusal naming the owner; wrong/expired = surfaced error with no retry loop; and there is no fallback to a different model or provider anywhere in the read set** (no alternate-`provider_type` construction exists outside `ProviderFactory.create_provider`; `restart`'s rollback restores the *previous* config, not a *different* provider). MEASURED (single construction site), INFERRED (no hidden fallback).

---

### F.2 SUMMARY TABLE — MODEL / PROVIDER (one line per binding)

| binding | (6) LIFETIME verdict | snapshot named |
|---|---|---|
| provider class (string key) | resolved once per `LLMClient` | `ProviderFactory._providers` @ `llm_providers/factory.py:28-42` |
| provider instance | SNAPSHOT-TRUSTED | `self.provider` @ `agent/core/llm_client.py:280` |
| model / base_url / api_key / temperature | SNAPSHOT-TRUSTED | `ProviderConfig` @ `llm_providers/factory.py:104-114` → `provider.config` @ `base.py:189` |
| `model_override` / `provider_id` | applied only at profile resolution | `AgentConfig.resolve_from_profile` @ `agent/config/models.py:462-505` |
| per-call `model=` kwarg | mechanism present, **no caller** | `completion_kwargs.update(kwargs)` @ `openai_compatible.py:485,772` |
| env `{TYPE}_API_KEY` / `OPENAI_API_KEY` | moment of use (read inside `create_provider`) | — (`factory.py:90-95`) |
| `LLM_TIMEOUT` / `LLM_MAX_RETRIES` | moment of use | — (`llm_client.py:28-33`) |
| `FIELD_CATEGORIES` restart policy | **no reader** (metadata only) | `AgentConfig.FIELD_CATEGORIES` @ `agent/config/models.py:90-129` |
| telemetry `model_name` | echoes the requested model | `LLMResponse(model=…)` @ `openai_compatible.py:1041` |


## G. SOLVER / RESOURCE BAND — **NOT WRITTEN**

`G` preliminary only: a repo-wide grep for `solver` / `band` over `tools/ security/ infra/ agent/ session/` returned **no** solver/band resource concept — only incidental substrings (`resolver`, `out-of-band`, `bands\b`, `abandoned`). Treat as **NOT FOUND** pending the documented search set.
