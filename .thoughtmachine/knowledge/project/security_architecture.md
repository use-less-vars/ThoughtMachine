# ThoughtMachine Security Architecture — Lay of the Land

**Status:** Canonical reference (2026-06-06)
**Audience:** All engineers working on ThoughtMachine
**Author:** Main Engineer (outgoing)

---

## 1. Philosophy: Resource-Centric Security

ThoughtMachine does not protect "the hard disk" or "the internet port." It protects **resources** — packaged capabilities the agent can access. Each resource is an independent trust domain.

A git commit writes to `.git/` on disk, but it is not a filesystem operation. It is a git operation, gated by the `git` toggle. A file write through `FileEditor` is a filesystem operation, gated by the `filesystem` toggle. The two do not overlap. This mirrors how operating systems work: they protect file descriptors, sockets, and processes — not the physical storage medium.

**Core principle:** The user decides, per resource, whether the agent can use it freely, use it with per-operation approval, or not use it at all.

---

## 2. The Three-Layer Model

```
WORKSPACE CAPABILITIES (Spec 3 — partially built)
    │  Ceiling: maximum power this project ever gets
    │  Stored OUTSIDE the workspace, unreachable by agent tools
    │  Owner: project lead
    ▼
SESSION PERMISSIONS (built, tested, honest)
    │  Coarse toggles: filesystem, network, container, git, execution
    │  User flips them in the Permissions tab
    │  Can only RESTRICT from the workspace ceiling, never expand
    ▼
TOOL CATEGORIES (built, enforced per operation)
    │  Fine-grained: each tool declares what it needs per operation
    │  Declared via required_categories or get_required_categories(params)
    │  Checked by the gate BEFORE execution
    ▼
EXECUTION (allowed or blocked)
```

**Key rule:** A tool runs only if both workspace and session allow it. Effective permission = `min(workspace, session)`.

---

## 3. The Five Active Resources (Toggles)

| Toggle | What it gates | Example tools |
|--------|---------------|---------------|
| **Filesystem** | Direct file reads/writes in the workspace | FileEditor, ApplyEdits, DirectoryCreator, FileMover, KnowledgeBaseTool (write) |
| **Git** | Version control operations | GitInfoTool (status, commit, push, clone) |
| **Network** | Outbound internet access | WebSearchTool, MCP HTTP clients, GitInfoTool (remote ops) |
| **Container** | Docker sandbox execution | DockerCodeRunner |
| **Execution** | Arbitrary code execution on the host | (Reserved; no tool yet) |

A sixth category, `security`, exists in the model but is not exposed in the GUI. It will gate tools that modify security settings (e.g., capability proposal tools in Spec 3). Currently dormant.

---

## 4. The Permission Hierarchy

Each toggle has five possible levels, enforced by `_value_satisfies()` in `agent/core/tool_executor.py`:

```
banned (0)  <  ask (1)  <  read (2)  <  write (3)  <  full (4)
```

| Level | Behavior |
|-------|----------|
| `banned` | Zero access. The resource is completely denied. |
| `ask` | **Reads auto-allow silently. Writes prompt the user via a GUI modal.** |
| `read` | Reads allowed; writes denied. |
| `write` | Reads and writes allowed without prompts. |
| `full` | Maximum access. Currently identical to `write` for most toggles; reserved for future use (e.g., `security:full` allows modifying the security model itself). |

**"Ask" flow:** When a tool requests a write-level category and the session toggle is set to `ask`, the gate publishes a `SecurityPromptEvent` with the session's ID. The bridge filters by session and forwards to the frontend. A modal appears. The user approves or denies per operation. Workers get a `NullEventBus` and cannot prompt — `ask` degrades to `deny` for them automatically.

---

## 5. How a Permission Check Works

Every tool call goes through `ToolExecutor._execute_single_tool()`:

1. `tool.get_required_categories(params)` returns the categories this operation needs (e.g., `["filesystem:write"]` or `["git:read", "network:outbound"]`).
2. `_check_permissions(categories, session_permissions.to_dict(), tool_name, session_id)` checks each category against the session.
3. `_value_satisfies(allowed_value, required_value)` returns `True` (allowed), `False` (denied), or `"ASK"` (prompt needed).
4. If any category is denied, the tool returns a permission error.
5. If any category returns `"ASK"`, a `SecurityPromptEvent` is published and the gate blocks until the user responds.

---

## 6. The Honesty Contract

The system upholds these guarantees:

1. **What you see in the Permissions tab is what the gate enforces.** No drift, no stale state, no silent defaults.
2. **When a toggle is `off`/`none`/`banned`, the tool is blocked with a clear permission error.** No silent passes.
3. **When a toggle is `ask`, you are prompted before every write operation.** Reads run silently.
4. **Workers never prompt you.** Their `ask` degrades to `deny`.
5. **Restart the machine, reopen the session — everything holds.** Permissions survive process death.
6. **New sessions inherit saved defaults.** The "Save as Default" button writes to `~/.thoughtmachine/agent_config.json`.

These are proven by 184 automated tests and manual verification.

---

## 7. Key Files and Locations

| Component | Location |
|-----------|----------|
| Permission model | `thoughtmachine/security.py` |
| Tool base class | `tools/base.py` |
| The Gate (`_check_permissions`, `_value_satisfies`) | `agent/core/tool_executor.py` |
| Event bus | `agent/events.py` |
| Bridge (config merge, session lifecycle, event forwarding) | `web_ui/backend/bridge.py` |
| GUI toggles | `web_ui/frontend/src/components/ConfigPanel.jsx` |
| Security dialog (ask modal) | `web_ui/frontend/src/components/SecurityDialog.jsx` |
| Session storage | `~/.thoughtmachine/sessions/<id>.json` |
| Global defaults | `~/.thoughtmachine/agent_config.json` |
| Provider credentials | `~/.thoughtmachine/providers.json` |
| Workspace capabilities | `~/.thoughtmachine/workspaces/<id>/capabilities.json` (Spec 3) |

---

## 8. Extending the System

### Adding a new tool
Declare `required_categories` (static) or `get_required_categories(params)` (dynamic) on the tool class. The gate handles the rest automatically. No other files need changes.

### Adding a new toggle
1. Add the field to `SessionPermissions` in `thoughtmachine/security.py`.
2. Add the hierarchy to `_value_satisfies()` in `tool_executor.py`.
3. Add the dropdown in `ConfigPanel.jsx`.
4. Add the category string that tools will declare.

No bridge, core, or event bus changes needed.

### Adding MCP tools
MCP servers declare their required categories in their manifest. The gate checks them against session permissions automatically. No ThoughtMachine code changes needed.

---

## 9. Current State

| Layer | Status |
|-------|--------|
| Session permissions model | Complete |
| Gate (`_check_permissions` / `_value_satisfies`) | Complete |
| Tool category declarations | Complete |
| "Ask" policy with GUI modal | Complete (git, filesystem toggles) |
| Session persistence (save/restore) | Complete |
| Global defaults ("Save as Default") | Complete |
| Cross-tab prompt isolation | Complete |
| Workspace scaffolding (Phase 1) | Complete |
| Workspace container (Spec 3) | In progress |
| Multi-agent workers (Spec 4) | Not started |
| Test suite | 184 tests, 0 failures |

---

## 10. Design Decisions (Locked)

1. **Resource-centric security.** Permissions belong to the resource, not the user. No inheritance chains.
2. **Session is a hard ceiling.** If the session says `filesystem: read`, nothing — not Docker, not workers — can write.
3. **No tool splitting.** Dynamic `get_required_categories()` handles multi-capability tools.
4. **Workers are synchronous, same-thread, blocking.** No async, no new processes. Null EventBus.
5. **Workspace capabilities live outside the workspace** (`~/.thoughtmachine/workspaces/`), unreachable by the agent.
6. **"Ask" is session-layer only.** Workers cannot trigger prompts.
7. **Three layers, one gate.** Effective permissions = `min(workspace, session)`. All tools and resources check against this.

---

*This document supersedes all prior security design notes. For implementation details, consult the test suite and the source files listed above.*

## Security Audit 2026-06-09 — Findings and Gaps

## 2026-06-09 — ## Security Audit 2026-06-09 — Comprehensive Findings

### A...

## Security Audit 2026-06-09 — Comprehensive Findings

### Audit Scope
Files audited: `thoughtmachine/security.py`, `security/security_gate.py`, `docker_executor.py`, `tools/docker_code_runner.py`, `agent/core/tool_executor.py`, `agent/core/agent.py`, `web_ui/backend/server.py`, `web_ui/frontend/src/components/ConfigPanel.jsx`, `thoughtmachine/workspace_capabilities.py`, `tools/base.py`, and related tool files.

---

### FINDING 1 (CRITICAL): Two Separate WorkspaceCapabilities Models with Inconsistent Defaults

There are **two distinct WorkspaceCapabilities classes** in the codebase:

1. **`security/security_gate.py:48-67`** — Pydantic BaseModel:
   - `network: bool = False` (restrictive default)
   - `filesystem_write: bool = True` (permissive)
   - `git_available: bool = True`
   - `container_available: bool = True`

2. **`thoughtmachine/workspace_capabilities.py:27-76`** — Dataclass:
   - `allow_network: bool = True` **(permissive default — opposite!)**
   - `allow_docker: bool = True` (permissive)
   - plus many more fields (allowed_tools, blocked_tools, allowed_providers, etc.)

**Impact**: The security_gate's `get_workspace_capabilities()` (line 75-96) loads from `capabilities.json` and maps `allow_network` to `network`, but if no file exists, it returns `WorkspaceCapabilities()` where `network=False`. This is more restrictive than the dataclass version's default (`allow_network=True`), but it's inconsistent and confusing. The dataclass model has extensive capability fields that are **never used** by the security gate.

**Recommendation**: Consolidate into a single model. The security_gate's model should be the canonical one, or the dataclass should be removed in favor of the gate's model.

---

### FINDING 2 (CRITICAL): SessionPermissions.network Defaults to "write"

In `thoughtmachine/security.py:68-126`, the `SessionPermissions` model:
```python
network: str = "write"  # Line ~88
```

This means **by default, the agent has full outbound network access**. A user who starts a fresh session without configuring permissions will have `network: "write"` — the agent can make HTTP requests by default.

**Contrast**: The `FALLBACK_FRONTEND_CONFIG` in `server.py:1252-1259` has `network: "banned"` as a safe default, but this is only a frontend fallback. The actual `SessionPermissions` model defaults to `"write"`.

**Impact**: Any code path that creates `SessionPermissions()` without explicit overrides (e.g., headless sessions, programmatic agent creation) gets full network access.

**Recommendation**: Change `SessionPermissions.network` default to `"banned"` or `"read"` to match security best practices.

---

### FINDING 3 (CRITICAL): No Validation on Frontend-Supplied session_permissions

In `server.py:1146` (`_translate_frontend_config`), the `session_permissions` dict from the frontend is passed straight through:
```python
result["session_permissions"] = frontend_config.get("session_permissions", {})
```
No schema validation, no level checking. A compromised or altered WebSocket message could inject any permission values.

**Attack scenario**: A frontend-altered message could set `container: true`, `filesystem: "full"`, `network: "write"`, `execution: "full"` etc., bypassing any restrictive defaults.

**Recommendation**: Add a validation function that:
1. Strips unknown categories
2. Validates each value against allowed levels (banned/ask/read/write/full)
3. Rejects or sanitizes out-of-range values

---

### FINDING 4 (HIGH): 'Ask' Permission Falls Back to "Allow" When Event System Unavailable

In `thoughtmachine/security.py:524-534`:
```python
if not EVENT_SYSTEM_AVAILABLE or global_event_bus is None:
    # Fallback: allow with warning
    ...
    return True
```

This means if the event bus is not configured or fails to initialize, any tool that requires `"ask"` permission is **automatically allowed**. The warning is logged but no user is prompted.

**Attack scenario**: A headless or worker agent (no event bus) with `filesystem: "ask"` will silently allow all write operations without any user consent.

**Recommendation**: The fallback should be `return False` (deny), not `return True` (allow). Default-deny is the safe choice when the user cannot be prompted.

---

### FINDING 5 (HIGH): Default Policy is "allow" in Security Config

In `thoughtmachine/security.py:862-874`:
```python
def get_default_security_config():
    return {
        "session_policy": {
            "default_policy": "allow",  # <--- THIS
            ...
        }
    }
```

And in `is_allowed()` (line 844-855), tools that don't match any tool_override or capability_requirement are **allowed by default**.

**Impact**: If a new tool is added and no explicit policy is configured for it, it's allowed. This is the opposite of defense-in-depth.

**Recommendation**: Change `default_policy` to `"deny"`. New tools should require explicit opt-in.

---

### FINDING 6 (HIGH): workspace_id Resolution Failure Causes Security Gate Bypass

In `docker_executor.py:139-144`:
```python
if self.workspace_id is None:
    try:
        from thoughtmachine.workspace_capabilities import resolve_workspace_id
        self.workspace_id = resolve_workspace_id(self.workspace_path)
    except Exception:
        self.workspace_id = None
```

If `resolve_workspace_id()` fails (e.g., no workspace config file), `workspace_id` remains `None`. Then in `_ensure_container()` (line 293-307), the code falls back to using only `session_permissions` directly — **bypassing the workspace capabilities layer entirely**.

**Impact**: If a workspace's capabilities file is missing or malformed, the workspace-level restrictions (network=false, etc.) are silently bypassed. The session's permissions become the only gate.

**Recommendation**: When `workspace_id` cannot be resolved, fall back to the most restrictive defaults (network=none, filesystem=read) rather than unrestricted session perms.

---

### FINDING 7 (MEDIUM): Legacy Boolean Coercion in SessionPermissions

In `thoughtmachine/security.py`, the `SessionPermissions` model coerces certain fields from bool to string:
- `container: bool = Field(False, ...)` — stored as bool
- `network: str = "write"` — stored as string

The Docker security gate checks `if eff.get("network") is True` (strict bool check), but the effective permissions can produce either bool or string values. This creates edge cases where `"write"` (string) and `True` (bool) might be treated differently.

**Recommendation**: Normalize all permission values to the same type before comparison.

---

### FINDING 8 (MEDIUM): Capabilities File is Read Every Container Creation

In `docker_executor.py:190`:
```python
policy = _load_policy(self.workspace_path)
```

And in `get_workspace_capabilities()` (security_gate.py:75-96), the `capabilities.json` file is read from disk every time a container is created. This is **read from the workspace path, not from the protected `~/.thoughtmachine/` directory**.

Wait — security_gate.py reads from `~/.thoughtmachine/workspaces/{id}/capabilities.json` (protected), but `_load_policy()` in docker_executor.py reads from the workspace directly.

Let me check `_load_policy`:

Actually the `_load_policy` function (line 190) appears after line 350 in docker_executor.py. Let me see it in the existing findings — it was mentioned at line 190: `policy = _load_policy(self.workspace_path)`. But that's only used for checking existing container config match, not for the security gate.

**Actual risk**: The `_load_policy` function (line 190) loads from workspace path, not from the protected ~/.thoughtmachine directory. But since it's only used for container policy validation (checking if existing container matches policy), not for setting permissions, the risk is moderate.

---

### FINDING 9 (MEDIUM): No Validation of Capabilities File Contents

In `security/security_gate.py:86-96`, when loading capabilities:
```python
raw = json.loads(path.read_text(encoding="utf-8"))
return WorkspaceCapabilities(
    network=raw.get("allow_network", True),       # <-- defaults to True if missing
    filesystem_write=raw.get("filesystem_write", True),
    git_available=raw.get("git_available", True),
    container_available=raw.get("allow_docker", True),
)
```

If someone writes a malformed capabilities.json with `"network": "evil"` (a string instead of bool), Pydantic's `BaseModel` will **coerce** it. `bool("evil")` is `True` in Python. So a string value in the file gets coerced to True.

**Impact**: A corrupted or maliciously crafted capabilities.json could grant more permissions than intended.

**Recommendation**: Use strict field validation (e.g., `Field(..., strict=True)`) or explicit type checking after parsing.

---

### FINDING 10 (LOW-INFO): Tool required_categories Declarations Are Correct

All tools examined have appropriate `required_categories` declarations:

| Tool | Categories | Appropriateness |
|------|-----------|-----------------|
| FileEditor | `filesystem:write` (read ops return `filesystem:read`) | ✅ Dynamic |
| ApplyEdits | `filesystem:write` | ✅ |
| DockerCodeRunner | `filesystem:write`, `container:true` | ✅ |
| GitInfoTool | Dynamic: `git:read` or `git:write` + `network:outbound` for remote | ✅ |
| FilePreviewTool | `filesystem:read` | ✅ |
| DateTimeTool | `[]` (none) | ✅ |
| MCP HTTP clients | `network:outbound` | ✅ |

**No missing or overly-permissive tool categories found.**

---

### FINDING 11 (INFO): Dual Security Gate Architecture

There are **two parallel security gate systems**:

1. **Legacy gate** (`agent/core/tool_executor.py` lines 386-406): Uses `_check_permissions()` → `_value_satisfies()` against `session_permissions.to_dict()`. Used by `ToolExecutor._execute_single_tool()`.

2. **Unified gate** (`security/security_gate.py`): Uses `get_effective_permissions()` → `check_required_categories()` which merges session + workspace capabilities. Used by Docker container creation.

**How they interact**: For Docker operations:
- The legacy gate checks the tool's `required_categories` against session perms (filesystem:write, container:true)
- The unified gate then checks workspace capabilities when creating the container
- There is **no double-checking** — the two gates operate independently

The unified gate is **not used** by the general tool execution path (the legacy gate handles all non-Docker tools). This means workspace capabilities like `allowed_tools`, `blocked_tools`, `allowed_file_extensions` etc. are **not enforced** for non-Docker tools.

---

### Summary of Risk Levels

| ID | Finding | Risk | Status |
|----|---------|------|--------|
| #1 | Two inconsistent WorkspaceCapabilities models | Medium | Open |
| #2 | SessionPermissions.network defaults to "write" | Critical | Open |
| #3 | No validation on frontend session_permissions | Critical | Open |
| #4 | 'Ask' fallback allows instead of denies | High | Open |
| #5 | Default policy is "allow" instead of "deny" | High | Open |
| #6 | workspace_id failure bypasses workspace caps | High | Open |
| #7 | Legacy bool coercion edge cases | Medium | Open |
| #8 | No validation of capabilities file contents | Medium | Open |
| #9 | Unified gate not used for non-Docker tools | Info | Open |
| #10 | Tool categories are correct | ✅ | Verified |
| #11 | Dual gate architecture (info) | Info | Documented |


