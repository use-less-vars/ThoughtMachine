# R4: CheckSystem — Runtime Environment Introspection Tool

## Overview

`CheckSystem` is a diagnostic tool that inspects the ThoughtMachine runtime environment — permissions, Docker container status, workspace metadata, agent configuration, and network connectivity. It's designed for debugging, monitoring, and understanding the current execution context.

**Tool file:** `tools/workspace/check_system.py`
**Tool class:** `CheckSystem(ToolBase)`
**Tool definition:** `tool = "CheckSystem"`
**Output:** JSON string (indented, with `default=str` for non-serializable types)

---

## 1. Query Types

### 1.1 `effective_permissions` — Session × Workspace Permissions

**Purpose:** Returns the merged effective permissions after applying workspace capability restrictions on top of session permissions.

**Sources:**
- `self.session_permissions` (from the session context)
- `workspace_id/capabilities.json` (from workspace definition)

**Resolution logic:**
```
1. Load workspace capabilities from ~/.thoughtmachine/workspaces/<ws_id>/capabilities.json
2. If security gate available:
     Build SessionPermissions from session_permissions dict
     Build WorkspaceCapabilities from capabilities.json fields
     Call get_effective_permissions(session_obj, caps_obj)
3. Fallback: return raw session_permissions dict
```

**Response shape:**
```json
{
  "effective_permissions": {
    "container": true,
    "network": true,
    "filesystem": "write",
    "system": "read",
    "git": "read",
    "execution": "banned"
  },
  "workspace_capabilities": {...},
  "workspace_id": "ws-xxx",
  "source": "gate" | "session_fallback"
}
```

### 1.2 `container_status` — Docker Container Status

**Purpose:** Reports the Docker container status for the current workspace.

**Resolution:**
- Uses `docker_executor.get_container_status(workspace_path, session_permissions)` if available
- Falls back to "unavailable" if Docker executor not available or no workspace path

**Response shape:**
```json
{
  "status": "running" | "stopped" | "unavailable" | "error",
  "container_id": "...",
  "image": "...",
  ...
}
```

### 1.3 `workspace_info` — Workspace Metadata

**Purpose:** Returns comprehensive workspace metadata.

**Sources** (from `~/.thoughtmachine/workspaces/<ws_id>/`):
- `config.json` → capabilities + domain_allowlist
- `workers.json` → worker definitions
- `mcp_servers.json` → MCP server configs

**Response shape:**
```json
{
  "workspace_id": "ws-xxx",
  "capabilities": {...},
  "domain_allowlist": [...],
  "workers": [...],
  "mcp_tools": [...]
}
```

### 1.4 `my_config` — Agent Configuration Snapshot

**Purpose:** Returns the agent's current configuration as injected by ToolExecutor.

**Security:** API key is masked as `"***"`.

**Response shape:**
```json
{
  "provider": "deepseek",
  "model": "deepseek-v4-flash",
  "timeout_seconds": 600,
  "max_turns": 200,
  "enabled_tools": ["FileEditor", "ApplyEdits", ...],
  "temperature": 1.0,
  "system_prompt": "...",
  "session_permissions": {...},
  "token_monitor_warning_threshold": 65000,
  "token_monitor_critical_threshold": 80000,
  "restriction_reason": null,
  "reasoning_effort": null,
  "base_url": "https://api.deepseek.com/v1/",
  "api_key": "***",
  "raw_config": {...}
}
```

### 1.5 `network_diagnostics` — Connectivity Checks

**Purpose:** Tests DNS resolution and HTTP connectivity to common endpoints.

**Method:**
- Uses DockerExecutor (if available) to run `nslookup` and `curl` inside the container
- Targets: `pypi.org`, `api.github.com`

**Response shape (container available):**
```json
{
  "pypi.org": {
    "dns": "ok",
    "http": "200"
  },
  "api.github.com": {
    "dns": "ok",
    "http": "200"
  }
}
```

**Fallback (no container):**
```json
{
  "container": false,
  "message": "No container running"
}
```

### 1.6 `workers` — All Worker Definitions

**Purpose:** Lists all worker definitions from the workspace's `workers.json`.

**Resolution:**
1. Try to read from `workspace_id/workers.json`
2. If `ws_id` not available, scan `~/.thoughtmachine/workspaces/*/workers.json` for first valid list (fallback)

**Response shape:**
```json
{
  "workers": [
    {"name": "coder", "tools": [...], "system_prompt": "...", ...},
    {"name": "reviewer", ...},
    {"name": "researcher", ...},
    {"name": "default", ...}
  ],
  "count": 4
}
```

### 1.7 `running_workers` — Active Worker Threads

**Purpose:** Returns currently running (spawned) worker threads with status details.

**Source:** `_worker_registry` (global dict with `_registry_lock`)

**Response shape:**
```json
{
  "running_workers": [
    {
      "name": "coder",
      "session_id": "abc-123",
      "status": "running" | "completed" | "error",
      "alive": true,
      "current_task": "Implementation task",
      "last_heartbeat": 1699000000.123,
      "error": null,
      "conversation_length": 42,
      "elapsed_seconds": 15.3
    }
  ],
  "count": 1
}
```

### 1.8 `worker/<name>` — Specific Worker Detail

**Purpose:** Returns the full definition of a specific worker by name.

**Resolution:** Same as `workers` query but filtered to match the given name.

**Response shape on success:**
```json
{
  "name": "coder",
  "description": "Specialist worker for writing, modifying, and debugging code...",
  "system_prompt": "You are the **coder** sub-agent...",
  "tools": ["FileEditor", "ApplyEdits", ...],
  "permission_footprint": {"filesystem": "write", "execution": "docker"}
}
```

### 1.9 `capabilities` — Environment Capabilities

**Purpose:** Returns what the current environment can do — provider, model, tools, Docker/git availability, OS, token limits.

**Sources:**
- `self.agent_config` → provider, model, enabled_tools
- `shutil.which("docker")` → Docker availability
- `shutil.which("git")` → git availability
- `os.name` → OS platform
- `capabilities.json` → token limits + override flags

**Response shape:**
```json
{
  "provider": "deepseek",
  "model": "deepseek-v4-flash",
  "enabled_tools": ["FileEditor", "ApplyEdits", ...],
  "has_docker": true,
  "has_git": true,
  "os": "posix",
  "token_limits": {
    "max_context_length": 131072,
    "max_conversation_turns": 200,
    "max_file_size_bytes": 10485760
  }
}
```

### 1.10 `dockerfile` — Dockerfile Content

**Purpose:** Returns the current Dockerfile for the workspace.

**Source:** `~/.thoughtmachine/workspaces/<ws_id>/Dockerfile`

### 1.11 `mcp_servers` — MCP Server Configurations

**Purpose:** Lists configured MCP (Model Context Protocol) servers.

**Source:** `~/.thoughtmachine/workspaces/<ws_id>/mcp_servers.json`

### 1.12 `event_bus_status` — EventBus Diagnostics

**Purpose:** Returns EventBus subscriber information.

**Source:** `agent.events.global_event_bus` → inspects `_subscribers` and `_wildcard_subscribers`

**Response shape:**
```json
{
  "subscribers_by_type": {
    "CONFIG_CHANGED": 3,
    "SESSION_STARTED": 2,
    "TOOL_EXECUTED": 1
  },
  "wildcard_subscribers": 1,
  "total_subscriber_types": 8
}
```

### 1.13 `event_log` — Recent Event Log Entries

**Purpose:** Tails the last 50 entries from the EventLogger JSONL file and returns them formatted.

**Method:** Runs `tail -n 50` on the EventLogger's file path (uses subprocess)

**Format:** Each entry shows `#[number]: [event_type] [source] — [data (truncated to 200 chars)]`

### 1.14 `config_snapshot` — Last Saved Config Snapshot

**Purpose:** Loads the last saved config snapshot from disk.

**Source:** Uses `ConfigSnapshot(workspace_path).load()` from `agent.logging.config_snapshot`

---

## 2. Architecture & Dependencies

### Optional Dependencies (graceful fallback)

| Module | Import Name | Used For |
|--------|-------------|----------|
| `thoughtmachine.workspace_capabilities` | CAPABILITIES_AVAILABLE | ws_id resolution, capabilities loading |
| `security.security_gate` | GATE_AVAILABLE | Effective permissions computation |
| `docker_executor` | DOCKER_EXECUTOR_AVAILABLE | Container status, network diagnostics |
| `tools.workspace.worker` | WORKER_REGISTRY_AVAILABLE | Running worker thread inspection |

Each module is imported with a try/except, and the tool degrades gracefully when dependencies are missing.

### Workspace Path Resolution

```
1. session_id → SessionRegistry.get(session_id) → workspace_id
2. workspace_id → WorkspaceRegistry.get_workspace(ws_id) → root_path
3. Fallback: self.workspace_path (deprecated but preserved)
```

---

## 3. Query Routing

```
execute()
  │
  ├── Resolve workspace_path (primary: SessionRegistry → WorkspaceRegistry)
  │   │
  │   └── Fallback: self.workspace_path (deprecated)
  │
  ├── Resolve ws_id from workspace_path
  │
  ├── Handle worker/<name> → dynamic dispatch to _query_worker_detail()
  │
  ├── Lookup handler in static map:
  │     effective_permissions → _query_permissions()
  │     container_status      → _query_container_status()
  │     workspace_info        → _query_workspace_info()
  │     my_config             → _query_my_config()
  │     network_diagnostics   → _query_network_diagnostics()
  │     workers               → _query_workers()
  │     running_workers       → _query_running_workers()
  │     capabilities          → _query_capabilities()
  │     dockerfile            → _query_dockerfile()
  │     mcp_servers           → _query_mcp_servers()
  │     event_bus_status      → _query_event_bus_status()
  │     event_log             → _query_event_log()
  │     config_snapshot       → _query_config_snapshot()
  │
  └── Unknown query → {"error": "...", "valid_queries": [...]}
```

---

## 4. Usage Examples

```python
# Check your current config
CheckSystem(query="my_config")

# Check effective permissions
CheckSystem(query="effective_permissions")

# Check if Docker is running
CheckSystem(query="container_status")

# List all available workers
CheckSystem(query="workers")

# Inspect a specific worker
CheckSystem(query="worker/coder")

# Check running worker threads
CheckSystem(query="running_workers")

# Full workspace metadata
CheckSystem(query="workspace_info")

# Environment capabilities
CheckSystem(query="capabilities")
```

---

## 5. Key Observations

1. **All queries are synchronous** — even network diagnostics, which could take seconds, blocks until complete.

2. **Container-based network diagnostics** are only available when Docker is running. Without it, the tool returns a graceful "no container" message rather than attempting host-level checks.

3. **API key masking** is handled in `_query_my_config()` — the key is replaced with `"***"` before returning.

4. **Workspace scanning fallback** for workers (`_scan_workspace_dirs_for_workers`) iterates all workspace directories and returns the first valid workers list — this is a best-effort fallback when the workspace ID can't be resolved.

5. **Event log** uses `tail` subprocess rather than programmatic JSONL reading — this means it depends on the `tail` command being available on the system.

6. **Graceful degradation** — every query handler wraps its logic in try/except and returns structured error responses rather than raising exceptions.

---

## 6. Coverage Summary

| Query | Source | Dependencies | Performance |
|-------|--------|-------------|-------------|
| `effective_permissions` | session + workspace files | security gate (optional) | Fast |
| `container_status` | docker_executor | Docker executor (optional) | Fast |
| `workspace_info` | workspace config files | workspace utils (optional) | Fast |
| `my_config` | self.agent_config | None | Instant |
| `network_diagnostics` | Docker executor | Docker + executor | Slow (network I/O) |
| `workers` | workspace workers.json | workspace utils (optional) | Fast |
| `running_workers` | _worker_registry | worker module (optional) | Fast |
| `worker/<name>` | workspace workers.json | workspace utils (optional) | Fast |
| `capabilities` | agent_config + shutil | None | Fast |
| `dockerfile` | workspace Dockerfile | workspace utils (optional) | Fast |
| `mcp_servers` | workspace mcp_servers.json | workspace utils (optional) | Fast |
| `event_bus_status` | global_event_bus | agent.events module | Fast |
| `event_log` | EventLogger file | tail command | Fast |
| `config_snapshot` | ConfigSnapshot class | logging module | Fast |
