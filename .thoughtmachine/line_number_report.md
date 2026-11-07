# Line-Number Report: Config Flow & Security Architecture

> **Generated:** 2026-07-16  
> **Scope:** 8 key files across the ThoughtMachine codebase  
> **Focus:** Permission/injection chain, session lifecycle, container security

---

## 1. `web_ui/backend/server.py` — WebSocket Server & Config Translation

| Item | Line(s) | Description |
|------|---------|-------------|
| WebSocket handler dispatch | 469 | `if command == "start_session":` — dispatches to bridge.start_session |
| WebSocket handler dispatch | 506 | `elif command == "continue_session":` — dispatches to bridge.continue_session |
| WebSocket handler dispatch | 581 | `elif command == "apply_config":` — dispatches to bridge.apply_config |
| `_translate_frontend_config()` | 1909 | Frontend → AgentConfig format translation |
| `_frontend_config_from_bridge()` | 1968 | Bridge AgentConfig → frontend format (reverse) |
| `_FALLBACK_FRONTEND_CONFIG` | 1989 | Hardcoded fallback frontend config dict |
| `_load_global_defaults()` | 2062 | Loads `~/.thoughtmachine/agent_config.json` |
| `_default_frontend_config()` | 2113 | Returns config merged from fallbacks + global defaults |

---

## 2. `web_ui/backend/bridge.py` — Session Bridge (95.5 KB)

| Item | Line(s) | Description |
|------|---------|-------------|
| `continue_session()` | 996 | Submit follow‑up query; calls `apply_config()` then `controller.request_config_update()` |
| `apply_config()` | 1149 | Validate, merge, persist full config; calls `controller.request_config_update()` (lines 1214, 1217) |
| `request_config_update` call | 1015 | From `continue_session()` → pushes to controller |
| `request_config_update` call | 1214 | From `apply_config()` → pushes validated config to controller |
| `request_config_update` call | 1217 | From `apply_config()` → pushes validated config to controller |

---

## 3. `agent/core/agent.py` — Core Agent

| Item | Line(s) | Description |
|------|---------|-------------|
| `request_config_update()` | 152 | Receives config via mailbox pattern; applies at next `process_query()` boundary |

---

## 4. `agent/core/tool_executor.py` — Tool Execution & Permission Injection

| Item | Line(s) | Description |
|------|---------|-------------|
| `class ToolExecutor` | 61 | Main tool executor class |
| `DEFAULT_SESSION_PERMISSIONS` | 52 | Fallback perms when no SessionPermissions model available |
| `execute_tool_calls()` | 86 | Entry point for executing tool calls from assistant message |
| `_execute_single_tool()` | 183 | Executes a single tool instance with argument injection |
| Session permissions injection | 230 | `session_perms_obj = self.config.session_permissions` |
| Session permissions injection | 258 | `tool_args['session_permissions'] = session_perms_obj.to_dict()` |
| Session permissions fallback | 260 | Falls to `DEFAULT_SESSION_PERMISSIONS` if no SessionPermissions object |
| Permission check | 125 | `if not self.state.is_tool_allowed(tool_name):` — gate before execution |

---

## 5. `agent/core/state.py` — Agent State Management

| Item | Line(s) | Description |
|------|---------|-------------|
| `get_allowed_tools()` | 350 | Returns list of allowed tool names based on current state |
| `is_tool_allowed()` | 362 | Boolean check: is a specific tool allowed? |

---

## 6. `agent/presenter/session_lifecycle.py` — Session Lifecycle

| Item | Line(s) | Description |
|------|---------|-------------|
| `start_session()` | 75 | Starts a new agent session with optional config/preset |
| `continue_session()` | 150 | Continues an existing session with a new query |

---

## 7. `agent/presenter/state_bridge.py` — State Bridge

| Item | Line(s) | Description |
|------|---------|-------------|
| `update_config()` | 68 | Update configuration with partial updates |

---

## 8. `thoughtmachine/security.py` — Centralized Security Layer

| Item | Line(s) | Description |
|------|---------|-------------|
| `class SessionPermissions` | (model) | Pydantic model with fields: container(bool), network, filesystem, system, git, execution |
| `VALID_PERMISSION_LEVELS` | ~136 | `("banned", "ask", "read", "write")` — note: "full" intentionally excluded |
| `PERMISSION_SCHEMA` | ~137 | Dict of key→valid values for coercion |
| `SAFE_DEFAULTS` | 143 | `{container:False, execution:banned, filesystem:read, git:read, network:banned, system:read}` |
| `coerce_session_permissions()` | ~154 | Validates/cleans raw permission dicts against schema + defaults |

---

## 9. `agent/config/loader.py` — Config Loader

| Item | Line(s) | Description |
|------|---------|-------------|
| `update_config()` | 465 | Merges partial updates into current config dict |

---

## 10. `agent/controller/__init__.py` — Agent Controller

| Item | Line(s) | Description |
|------|---------|-------------|
| `update_config()` | 146 | Sets pending config update; forwards to `agent.request_config_update()` at line 159 |
| `request_config_update()` | 407 | Forwards config to agent if available; stores as pending otherwise (line 420) |
| `continue_session()` | 329 | Deprecated wrapper; use `process_query()` instead |

---

## Summary: Config Flow (Frontend → Tool Execution)

```
ConfigPanel.jsx --WS--> server.py:581 (apply_config)
                            ↓
                         bridge.py:1149 (apply_config)
                            ↓
                         controller:146 (update_config)
                            ↓
                         agent.py:152 (request_config_update)
                            ↓  [picked up at next process_query boundary]
                         tool_executor.py:86 (execute_tool_calls)
                            ↓
                         state.py:362 (is_tool_allowed)  ← permission gate
                            ↓
                         tool_executor.py:258 (inject session_permissions into tool_args)
                            ↓
                         [tool receives session_permissions dict]
```

**Key permission defaults (safe-restrictive):**
- `SAFE_DEFAULTS` (security.py:143): network=banned, filesystem=read, container=False, execution=banned
- `DEFAULT_SESSION_PERMISSIONS` (tool_executor.py:52): mirror the above
- `SessionPermissions` model defaults: network=banned, filesystem=read, container=False, execution=banned
- `VALID_PERMISSION_LEVELS` (security.py:~136): `("banned", "ask", "read", "write")` — "full" is intentionally excluded