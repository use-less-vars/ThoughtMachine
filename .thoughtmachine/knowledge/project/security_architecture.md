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
