# Development Guides
Coding conventions and workflows. **Restructured 2026-08-16**: stale/superseded guides moved to `archive_arch_b.md`; active guides kept here.
> **LOST (2026-08-16, S2 incident):** the `DURABLE MEMORY REQUIREMENT` section (operator mandate) was uncommitted in the working tree at restore time and is not recoverable from git/workspace. Essence survives in `personal/task_tracker.md` → OPERATOR HANDOFF (durable files DECISIONS.md / BACKLOG.md / ROADMAP.md in repo root, every item statused: done / in progress / decided / deferred / superseded / needs verification; KB kept in sync). Please re-supply the section from host-side if available.
## Current Status (2026-08-16)
- DURABLE MEMORY REQUIREMENT section: LOST (see note above); essence preserved in task_tracker.md OPERATOR HANDOFF.
- Active guides kept: Logging API, DockerCodeRunner (status note added), FileEditor usage, permission toggles, test infrastructure, worker tool rules, event checklist, cross-session worker panel, 10 Sacred Rules, second-instance port, install & run scripts, serve-frontend flag.
## Archived
Stale guides → `archive_arch_b.md` (`## SOURCE: development_guides.md — archived (stale/superseded)`).
## Logging API Reference

*(Migrated from docs/logging_manual.md — Last validated: 2026-05-22)*

### Adding a Log Statement
```python
from agent.logging import log

log(level: str | LogLevel, tag: str, message: str, data: dict = None)
```

| Parameter | Description | Example |
|-----------|-------------|---------|
| `level` | `"DEBUG"`, `"INFO"`, `"WARNING"`, `"ERROR"` | `"DEBUG"` |
| `tag` | Hierarchical component name (area.component) | `"tools.file_editor"` |
| `message` | Human-readable description | `"Writing file"` |
| `data` | Optional dict (auto‑truncated) | `{"path": p, "size": n}` |

Example:
```python
log("DEBUG", "core.pruning", "Pruning context", {"kept": 5, "removed": 2})
```

### Console Output Format
Every console line includes an `[HH:MM:SS]` timestamp:
```
[14:23:01] DEBUG    [core.pruning] Pruning context | {"kept": 5, "removed": 2}
[14:23:02] INFO     [server.config] Config loaded
[14:23:05] WARNING  [llm.stepfun] Rate limit approaching
```

### Tag Naming Convention
Use `area.component`. Components are organized as:

| Area | Components |
|------|------------|
| `core` | session, pruning, config, controller, context_builder, turn_transaction, token_counter |
| `tools` | file_editor, docker_code_runner, docker_executor (container, policy, build), search |
| `llm` | anthropic, openai, stepfun |
| `server` | config, bridge |
| `ui` | presenter, output_panel, events |
| `session` | history_provider, context_builder |

### Console Output Control
| Variable | Effect | Default |
|----------|--------|---------|
| `TM_LOG_LEVEL` | Minimum console level | `INFO` |
| `TM_LOG_TAGS` | Comma‑separated tags to show (empty = only WARNING+) | _(empty)_ |
| `DEBUG_<COMP>` | Legacy flag for a single component | – |
| `THOUGHTMACHINE_DEBUG=1` | Firehose (all debug) – use sparingly | – |

**Filtering logic:**
1. If `TM_LOG_TAGS` is empty → only WARNING, ERROR, CRITICAL shown
2. If `TM_LOG_TAGS` is set → matching tags shown at `>= TM_LOG_LEVEL`
3. Wildcard: `server.*` matches `server.config`, `server.bridge`, etc.
4. Per‑component `DEBUG_*` env vars override everything
5. Runtime `set_log_tags()` / `set_log_level()` override env vars at runtime

**Workflow examples:**
```bash
# See everything in the server layer (INFO + DEBUG)
export TM_LOG_TAGS=server.*

# Focus: only server config + bridge + controller lifecycle
export TM_LOG_TAGS=server.*,core.controller

# Firehose: everything at DEBUG (warning: very verbose!)
export THOUGHTMACHINE_DEBUG=1

# Quick single-component debug (legacy)
export DEBUG_EVENTBUS=1

# Back to quiet
unset TM_LOG_LEVEL TM_LOG_TAGS THOUGHTMACHINE_DEBUG
```

### Runtime API
These functions change filtering at runtime without restarting:

```python
from agent.logging import set_log_level, set_log_tags, show_log_config

set_log_level("DEBUG")                              # string form
set_log_level("INFO")                                # back to normal

set_log_tags("core.*,tools.file_editor")             # string (comma-separated)
set_log_tags(["core.session", "llm.stepfun"])         # list
set_log_tags("*")                                     # firehose (all tags)
set_log_tags("")                                      # back to default (WARNING+ only)

config = show_log_config()   # returns dict: log_level, log_tags, truncation, env_vars
```

### File Logging (JSONL)
| Variable | Effect | Default |
|----------|--------|---------|
| `TM_LOG_FILE_LEVEL` | Minimum level written to JSONL file | `DEBUG` |
| `TM_LOG_DIR_MAX_MB` | Hard limit on total log directory size (0 = unlimited) | `50` |

All logs written to `logs/agent_<session_id>.jsonl`. Rotation: 10 MB, 5 backups.
Total directory size capped at `TM_LOG_DIR_MAX_MB` — oldest files deleted when exceeded.

### Truncation (Prevents Bloat)
| Variable | Default | Applies To |
|----------|---------|------------|
| `TM_DEBUG_TRUNCATE_LENGTH` | 100 | Generic debug messages, dump_messages preview |
| `TM_TOOL_ARGUMENTS_TRUNCATE` | 100 | Tool call arguments |
| `TM_TOOL_RESULT_TRUNCATE` | 100 | Tool call results |
| `TM_RAW_RESPONSE_TRUNCATE` | 100 | Raw LLM responses |
| `TM_CONSOLE_DATA_TRUNCATE` | 200 | Structured data printed to console |
| `TM_CONVERSATION_CONTENT_TRUNCATE` | 10000 | Conversation messages in JSONL |
| `TM_DOCKER_OUTPUT_TRUNCATE` | 10000 | Docker sandbox output |

> **Note:** JSONL files receive data truncated once by type‑specific limits; console applies an additional truncation pass for readability.

### Best Practices
- Use **DEBUG** for temporary instrumentation – it won't spam unless explicitly enabled.
- Use **INFO** for normal noteworthy events.
- Choose a **specific tag** (e.g., `"tools.my_new_tool"`).
- Provide a **data dict** even for minimal context.
- Use `set_log_tags()` and `set_log_level()` during development to toggle filters.
- Use `show_log_config()` in diagnostic output or `/debug` endpoints.

## DockerCodeRunner Usage
> **STATUS NOTE (2026-08-16)**: DockerCodeRunner is LIVE in current code — tools/docker_code_runner.py; agent/core/tool_executor.py:267; infra/container_manager.py:97. Older guide kept for reference.

*(Migrated from docs/docker_usage.md — Last validated: 2026-05-05)*

### Overview
`DockerCodeRunner` executes shell commands inside a secure, isolated Docker container. Designed for speed and agent-friendliness — especially for runtime `pip install` without rebuilding the image.

### Key Capabilities
- Run any shell command (bash, python, pip, etc.)
- Runtime `pip install --user` — install packages on the fly (seconds, not minutes)
- Persistent container — reused across sequential calls (up to idle timeout, default 600s)
- Network on demand — controlled by JSON policy file (default: no network)
- Writable home directory — `/home/agent` is a tmpfs mount (when `writable_home: true`)
- Read-only root filesystem — system directories cannot be modified

### Security Model
| Feature | Default | Policy-controlled |
|---------|---------|-------------------|
| Network | none | bridge if `docker_network_allowed: true` |
| Home directory | read-only | writable tmpfs if `writable_home: true` |
| Root access | none (user agent) | root not available |
| Capabilities | all dropped (`cap_drop=["ALL"]`) | – |
| Workspace mount | `/workspace` (read-write) | always mounted |

### Security Policy File
Read from `~/.thoughtmachine/security_policy.json` (NOT inside workspace — user-controlled):
```json
{
  "/home/jojo/PycharmProjects/*": {
    "docker_network_allowed": true,
    "writable_home": true
  },
  "default": {
    "docker_network_allowed": false,
    "writable_home": false
  }
}
```
Path patterns support `*` globs. First matching pattern applies; fallback to "default".

### Container Lifecycle
- Deterministic name: `agent-exec-{sha256(workspace_path)[:12]}`
- Reused across calls within same workspace
- Idle timeout (default 600s, configurable)
- State persistence (pip packages) survives only within idle timeout
- Image rebuilt only when `build=True`

### Installing Packages at Runtime
Inside a container with `writable_home: true`:
```python
# First call — install
DockerCodeRunner(command="pip install --user colorama")

# Second call — use
DockerCodeRunner(command="python3 -c 'import colorama; print(colorama.__version__)'")
```
Package goes to `/home/agent/.local`. Available on subsequent calls within idle timeout.

### Configuration (Tool Parameters)
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| command | str | – | Shell command to execute |
| timeout | int | 30 | Max execution seconds |
| working_dir | str | /workspace | Working directory inside container |
| environment | dict | None | Environment variables |
| build | bool | False | Force rebuild Docker image |
| image | str | "agent-executor" | Docker image name |
| mem_limit | str | "512m" | Memory limit |
| cpu_quota | int | 50000 | CPU quota (µs per 100ms period) |
| idle_timeout | int | 600 | Seconds of inactivity before teardown |
| script | str | None | Multi-line script (alternative to command) |
| interpreter | str | "bash" | Interpreter for script |

### Return Value
JSON-formatted string with: `success`, `exit_code`, `stdout`, `stderr`, `command`, `duration`, `timed_out`, `error` (on failure).

### Limitations
- No apt-get or system package installation at runtime (requires `build=True` and root)
- No GUI — container has no display/X11
- No persistent home — pip packages ephemeral (lost on container recreation)
- No multi-container orchestration — one container per workspace at a time
- No Docker socket inside container

### Troubleshooting
| Symptom | Likely Cause |
|---------|--------------|
| `pip install --user` fails with permission error | `writable_home` not true in policy |
| pip cannot reach PyPI | `docker_network_allowed` not true |
| Package disappears between calls | Idle timeout exceeded, or policy mismatch |
| Container name changes between calls | Workspace path not normalized (absolute, no trailing slash) |

## FileEditor `line_number` vs `line_numbers` — subtle distinction

**Problem**: Agents get confused and use `line_numbers` (plural) expecting context lines around the result.

**The rules**:
- `line_number` (singular, int) + `context_lines` (int) → shows the line WITH surrounding context. Best for reading a specific area.
- `line_numbers` (plural, int) → shows ONLY that single line (no context). Use for targeted reads.
- `line_numbers` (plural, string like `"420-451"`) → shows a range of lines. Use `context_lines` for surrounding context.

**Recommended pattern for reading code with context**:
```json
FileEditor(operation="read", filename="path/to/file.py", 
           line_number=440, context_lines=10)
```

**Avoid** `line_numbers: 440` (plural with int) — it gives no context.
**Avoid** `line_numbers: [440]` (plural with list) — same issue.

**For reading a full block/range**, use `line_numbers: "420-451"` (range string) to get all lines.

## Feature: `--serve-frontend` CLI flag (2026-06-02)

Added `--serve-frontend` flag to `web_ui/backend/server.py` main() so the backend can build and serve the React frontend directly, eliminating the need for a separate Vite dev server.

**How it works:**
1. `python -m web_ui.backend.server --serve-frontend` auto-builds the frontend via `npm run build` (if `dist/` doesn't exist)
2. Mounts `web_ui/frontend/dist/` as `StaticFiles(html=True)` at `/`
3. Root `/` serves `index.html` instead of the JSON info response
4. SPA catch-all route `/{path:path}` serves `index.html` for client-side routing paths
5. API routes (`/health`, `/api/browse`, `/ws`) still work unaffected
6. Supports `--host`, `--port`, `--reload` flags and env var overrides (`HOST`, `PORT`, `RELOAD`)

**Edge cases handled:**
- Missing Node.js/npm → graceful error message
- Build failure → warns but still starts (shows build-error page)
- Missing `dist/` → triggers auto-build
- API paths under catch-all → returns 404 (not SPA fallback)

## Install & Run Scripts (created 2026-05-30)

Two scripts were added to the project root:

- **`install_thoughtmachine.sh`** — Full install: checks Python >=3.11 & Node.js, creates `.venv`, pip installs requirements, npm installs & builds frontend.
- **`start_thoughtmachine.sh`** — Activates `.venv` and starts server with `--serve-frontend` on `127.0.0.1:8000` (override via `HOST`/`PORT` env vars).

Usage: `./install_thoughtmachine.sh && ./start_thoughtmachine.sh`

## How to add a new permission toggle

Every new trust domain — email, private space, database access, whatever — is just a new resource category. The architecture makes adding one straightforward. Here's the recipe.

**Key principle:** The gate doesn't care what the resource is. It only cares about the level. Adding a new toggle is adding a new key to a dictionary, with the same values as everything else. No archaeology, no hidden files, no new decision points.

### Four places to touch

#### 1. Define the category in the permission model

In `thoughtmachine/security.py`, add the new field to `SessionPermissions`. Give it the same type as the others — `Literal["banned", "ask", "read", "write", "full"]` if it has read/write distinction, or `bool` if it's on/off. Safe default is always the most restrictive.

```python
email: Literal["banned", "ask", "read", "write"] = "banned"
```

#### 2. Add it to the gate's level ordering

In `security/security_gate.py`, the `_value_satisfies` function (or equivalent) has an ordered mapping of levels. Add the new category to that same hierarchy: `banned < ask < read < write < full`. No other gate changes needed — `get_effective_permissions()` and `check_required_categories()` handle new categories automatically.

#### 3. Tools declare they need it

Any tool that reads the resource adds `"<category>:read"` to its `get_required_categories()`. Any tool that writes adds `"<category>:write"`. The gate enforces it. MCP tools declare it in their manifest. No tool executor changes needed.

#### 4. Add the dropdown in the GUI

In `ConfigPanel.jsx`, add a new row in the Permissions tab — a dropdown with the same options as filesystem/git. It binds to the new field. The existing `apply_config` flow sends it to the backend. The backend already validates against the `SessionPermissions` model, so the new field is automatically accepted and enforced.

### If the new resource needs workspace-level control too

If workspaces should have a ceiling on access (e.g., "this workspace can only read email, never send"), add the field to `WorkspaceCapabilities` in `thoughtmachine/workspace_capabilities.py`. The gate's `min(session, workspace)` merge automatically applies it. The Workspace Panel (Spec 3) will expose it for editing.

### If containers need to enforce it

Some resources — like network — are enforced at the container level. For email, you might not need container enforcement (email access is gated at the tool level). But if you did, the Docker executor would read `eff["<category>"]` from the gate and configure something accordingly. That's a per-resource design decision.

### Testing

Add a row to the merge table in the existing gate contract tests (`tests/docker/test_gate_contract.py`). The parametrized tests cover all combinations automatically.


## Second instance port configuration (2025-07-16)

### How to start a second instance
```bash
# Terminal 1 — first instance (default ports)
python -m web_ui.backend.server
# In another terminal:
cd web_ui/frontend && npm run dev

# Terminal 2 — second instance (custom ports)
TM_PORT=8001 python -m web_ui.backend.server
# In another terminal:
cd web_ui/frontend && VITE_PORT=5174 VITE_BACKEND_PORT=8001 npm run dev
```

### Environment variables
| Variable | Default | Scope | Description |
|----------|---------|-------|-------------|
| `PORT` / `TM_PORT` | 8000 | Backend | Backend server port (already supported) |
| `VITE_PORT` | 5173 | Vite dev | Frontend dev server port |
| `VITE_BACKEND_PORT` | 8000 | Frontend | Backend port for WS/API connections |

### Files changed
- `web_ui/frontend/vite.config.js` — env var for port + proxy target
- `web_ui/frontend/src/App.jsx` — env var for WS_URL
- `web_ui/frontend/src/components/SessionTab.jsx` — env var for WS_URL
- `web_ui/frontend/src/components/ConfigPanel.jsx` — env var for API base URL

Backend (`server.py`) already supported `--port` and `PORT` env var from earlier work.

## 2026-07-02 — Master Vault: Design Principles (10 Sacred Rules)

These are the core design principles that guide all architecture decisions. They should not be violated without explicit deliberation.

### Rule 1: Workers are Agents with Restricted Tools
Workers reuse the agent's reasoning loop. They are not scripts or plugins — they are full agents that happen to have fewer tools and a narrower context. This ensures consistency, debugability, and future extensibility.

### Rule 2: Separation of Agent and Host
The agent should never need direct access to the host filesystem, Docker daemon, or network beyond what tools expose. The Host is the boundary. The agent operates inside tool-mediated constraints.

### Rule 3: Events are the Universal Language
All state changes should flow through events. No direct method calls between subsystems. The EventBus is the nervous system. This decouples components and enables real-time UI updates.

### Rule 4: Persistence is Transparent
Sessions, configs, and state should serialize and deserialize without custom migration scripts. If you need a migration, the persistence model is wrong.

### Rule 5: The UI is a Client, Not the System
The Web UI is one client among many (CLI, API, future clients). All logic lives in the backend. The frontend is a rendering layer, not a decision-maker.

### Rule 6: Security Defaults to Deny
Permissions should default to the most restrictive safe state. Users explicitly grant access. This prevents accidental exposure and follows least-privilege principles.

### Rule 7: Config is Code
Configuration files should be treated as code: version-controlled, validated, and structured. No magic strings, no undocumented keys. Config schemas should be explicit and typed.

### Rule 8: One Source of Truth for State
No duplicated state between frontend and backend. The backend owns state; the frontend reflects it. Avoid local state that mirrors server state.

### Rule 9: Fail Closed, Not Open
When in doubt about permissions, connectivity, or state validity, refuse the operation and report the issue. Silent failures are bugs. Defensive checks are welcome.

### Rule 10: Test the Boundaries
Unit test the internals, integration test the boundaries (tool interface, event bus, persistence, WebSocket bridge). E2E tests cover the happy path. Edge cases live in integration tests.

### Deriving Sprints from Principles
- Panel Unification (Sprint 1-3): Upholds Rule 8 (one source of truth) and Rule 5 (UI is client)
- Worker Config Panel: Upholds Rule 1 (workers are agents) and Rule 6 (security defaults)
- Container Persistence: Upholds Rule 4 (persistence is transparent)
- Security Defaults: Upholds Rule 6 (default to deny) and Rule 9 (fail closed)

## Cross-Session Worker Panel Access — Changes Implemented

**Date:** 2026-07-09

**Objective:** Make workers visible across all session tabs in the same workspace for VIEWING (list, panel, events) while keeping CONTROL (query, stop) session-scoped.

### Files Changed:

1. **`web_ui/frontend/src/components/WorkspacePanel.jsx` (line 199)**
   - **Before:** `data.filter(w => !sessionId || w.session_id === sessionId)`
   - **After:** `data` (no filter)
   - **Effect:** WorkerAutoOpenWatcher now auto-opens for ANY worker in the workspace, not just the current session's workers.

2. **`web_ui/frontend/src/components/WorkerOutputPanel.jsx` (line 141)**
   - **Before:** `data.find((w) => w.name === workerName && (!sessionId || w.session_id === sessionId))`
   - **After:** `data.find((w) => w.name === workerName)`
   - **Effect:** fetchWorkerInfo() finds any worker by name regardless of session.

3. **`web_ui/frontend/src/components/WorkerOutputPanel.jsx` (line 288-296)**
   - Added session-scoped guard in `handleStop`:
   - If `workerInfo.session_id !== sessionId`, shows error "Cannot stop worker from another session" and returns early.
   - **Effect:** Control operations are blocked for cross-session workers; viewing still works.

4. **`web_ui/frontend/src/components/WorkerOutputPanel.jsx` (lines 182-206)**
   - Added worker-name filtering on incoming WS events merge:
   - Filters `incomingEvents` by `e.worker_name` or `e.response?.worker_name` matching the current `workerName`.
   - **Effect:** When all sessions' events are passed, only events for the displayed worker are merged in.

5. **`web_ui/frontend/src/App.jsx` (line 671)**
   - **Before:** `incomingEvents={workerEvents[activeSessionId] || []}`
   - **After:** `incomingEvents={Object.values(workerEvents).flat()}`
   - **Effect:** ALL session worker events are passed to the WorkerOutputPanel, which then filters by worker name internally.


## Adding a New Event Type — Checklist

Every new event type must be added to every layer of the pipeline. Missing any step causes silent failures.

### 1. Backend — Event Definition
- [ ] Add enum value to `EventType` in `agent/events.py`
- [ ] Create typed Pydantic event class (with validator if payload required)
- [ ] Add to `event_class_map` in `create_event()` factory
- [ ] Add to `_map_legacy_event_type()` mapping (for backward compatibility)

### 2. Backend — Event Emission
- [ ] Add `_publish_event()` call at the appropriate lifecycle point
- [ ] Ensure data dict includes `worker_name` and `session_id`

### 3. Backend — WebSocket Forwarding
- [ ] Subscribe to the new event type in `bridge.py` `_subscribe_to_worker_events()`
- [ ] Add WebSocket message format to `server.py` docstring (Server→Client types)
- [ ] Add unsubscribe handler in `_unsubscribe_worker_events()`

### 4. Frontend — Event Reception
- [ ] Add case to WS message handler in `SessionTab.jsx` (connectSessionWs switch)

### 5. Frontend — Event Rendering
- [ ] Add case to `adaptWorkerEvent.js` switch statement
- [ ] Add handling in `WorkerOutputPanel.jsx` merge/incoming events logic (if needed)

### 6. Schema & Tests
- [ ] Add event type to `web_ui/shared/worker_event_schema.json`
- [ ] Add test case to `adaptWorkerEvent.test.js`

## Test Infrastructure Summary
- **44 test files** total (43 Python + 1 JavaScript)
- **~486 tests** total (~414 Python + ~72 JavaScript)
- **Runner:** pytest (no config section in pyproject.toml) + vitest (frontend)
- **conftest.py:** 3 files (docker_integration, security, web_ui/backend)
- **Test commands:** `python -m pytest` | `cd web_ui/frontend && npm test`
- **Top files by test count:** test_history_pruner.py (45), test_worker_agent_transplant.py (30), test_worker_loop_spike.py (29), test_permissions_roundtrip.py (25), test_security_coercion.py (20)
- **Full report:** TEST_INFRASTRUCTURE_REPORT.md

## Worker Tool Usage Rules (Critical)

**NEVER use `spawn` with `context={"query": "..."}` for follow-up tasks.** Doing so **deletes the worker's `context.json`** and creates a **fresh agent with an empty conversation**, losing all prior context.

### Correct pattern:
1. **`spawn`**: Use ONCE to start the worker with `context={"query": "initial task"}`. This creates the worker thread + agent + conversation.
2. **`query`**: Use for ALL follow-up tasks by passing `worker_query="follow-up message"`. This preserves the existing Agent, WorkerContext, and all conversation history.
3. **`force=True`**: Only use when the worker is genuinely stuck/hanging (stale from another session). **Never use `force=True` for routine continuation** — it kills the existing worker and starts fresh.

### Why:
- `spawn` with context deletes old `context.json` → fresh `WorkerContext` → empty conversation
- `query` reuses the existing Agent → conversation history preserved
- The worker thread stays alive after completing a task, waiting on `_input_queue` for the next query

### How to check worker state before acting:
- `check` returns `current_context_tokens` and `status`
- If `status="ready"` and `alive=True`, the worker is ready for a follow-up `query`
- If `status="stopped"` or `alive=False`, need to `spawn` fresh

