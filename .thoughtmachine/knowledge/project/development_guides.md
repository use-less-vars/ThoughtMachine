# Development Guides

Coding conventions, setup instructions, and development workflows.

## Current Status
- No guides recorded yet.

## Setup
(To be populated)

## Conventions
(To be populated)

## Workflows
(To be populated)

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
## DockerCodeRunner Usage

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

## 2026-05-07 — ## Phase 1 Complete: Branch Creation & Switching

### New Me...

## Phase 1 Complete: Branch Creation & Switching
## Phase 1-3: Git Branch Operations — 🔴 NEVER IMPLEMENTED

**Correction (2026-KB-AUDIT):** The Git branch operations documented below in Phases 1-3 (`create_agent_branch`, `switch_branch`, `cleanup_agent_branch`, `commit_on_agent_branch`, `sync_agent_with_dev`, `merge_agent_to_dev`) were **planned but never implemented** in the actual codebase.

`GitInfoTool` at `tools/git_info_tool.py` only supports: `status, diff, log, branch, show, remote, blame, config, commit, init, clone`. None of the agent-branch-specific operations exist.

If these features are needed in the future, the original design docs are preserved below as a starting point for implementation.

### Archived Design — Phase 1: Branch Creation & Switching (Never Implemented)
**Planned methods (3 methods, ~220 lines):**
1. `_create_agent_branch(repo_root)` — Creates `agent_{base}_{suffix}` branch from base, validates suffix format, checks for duplicates
2. `_switch_branch(repo_root)` — Switches to existing branch (agent branches + readonly branches allowed)
3. `_cleanup_agent_branch(repo_root)` — Stashes changes, switches to merge target, safe-deletes branch

**Planned fields:** `branch_suffix: Optional[str]`, `base_branch: str = "dev"`, `branch_name: Optional[str]`

**Planned operations:** `"create_agent_branch"`, `"switch_branch"`, `"cleanup_agent_branch"`

### Archived Design — Phase 2: Commit on Agent Branches (Never Implemented)
**Planned fields:** `commit_message: Optional[str]`, `file_paths: Optional[List[str]]`, `add_all: bool = False`

**Planned operation:** `"commit_on_agent_branch"`

### Archived Design — Phase 3: Sync and Merge (Never Implemented)
**Planned fields:** `prose_message: Optional[str]` (200 char max)

**Planned operations:** `"sync_agent_with_dev"`, `"merge_agent_to_dev"`

## 2026-05-07 — ## Phase 2 Complete: Commit on Agent Branches

**New Pydanti...

## Phase 2 Complete: Commit on Agent Branches
## Phase 2 Complete: Commit on Agent Branches

**🔴 NOT IMPLEMENTED — See "Phase 1-3: Git Branch Operations" section above for archived design docs.**

## Phase 3 Complete: Sync and Merge
## Phase 3 Complete: Sync and Merge

**🔴 NOT IMPLEMENTED — See "Phase 1-3: Git Branch Operations" section above for archived design docs.**

## 2026-05-07 — ## Phase 3 Complete: Sync and Merge

**New Pydantic fields:*...

## Phase 3 Complete: Sync and Merge

**New Pydantic fields:**
- `prose_message: Optional[str]` — merge commit message (200 char max, required for merge_agent_to_dev)

**New operation Literal values:**
- `"sync_agent_with_dev"`
- `"merge_agent_to_dev"`

**Readonly guard integration:**
- Added `readonly_guarded_ops` set in `execute()` — calls `_assert_not_readonly_branch()` for Phase 1/2 ops but NOT for sync/merge (which legitimately write to dev)

### `_sync_agent_with_dev(repo_root)`
1. Validates on agent branch (not detached HEAD, starts with prefix)
2. Checks no uncommitted changes (`git status --porcelain`)
3. `git fetch origin dev`
4. `git merge origin/dev --no-edit`
5. On `GitWriteError`: `git merge --abort`, then `git diff --name-only --diff-filter=U` to list conflicted files
6. Returns success or conflict report (never auto-resolves)

### `_merge_agent_to_dev(repo_root)`
1. Validates on agent branch
2. Validates `prose_message` is non-empty and ≤200 chars
3. Checks no uncommitted changes
4. `git checkout dev`, `git pull origin dev`
5. `git merge --no-ff <agent_branch> -m "<prose_message>"`
6. On conflict: abort, list conflicted files
7. Post-merge: checks `delete_agent_after_merge` config flag (default `False`), safe-deletes branch if True

**Phase 1-3 complete.** Full write operations: create branch, switch, cleanup, commit, sync with dev, merge to dev.

## 2026-05-09 — ## FileEditor `line_number` vs `line_numbers` — subtle disti...

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

## 2026-05-14 — ## Chat Display Overhaul (2025-01-17)

Replaced `ChatPanel.j...

## Chat Display Overhaul (2025-01-17)

Replaced `ChatPanel.jsx` with a full-featured chat display supporting:
- **Markdown rendering** via `react-markdown` + `remark-gfm` for assistant and reasoning content
- **Tool calls** displayed as expandable `<details>` with 🛠️ icon and formatted JSON args
- **Long tool results** truncated at 500 chars with "▼ Show more" toggle
- **Reasoning blocks** as 💭 Thinking collapsible `<details>` with markdown rendering
- All roles (user, assistant, tool_call, tool_result, system) get distinct bubbles
- Prop renamed from `history` to `messages` to match SessionTab usage
- Added ~200 lines of CSS in `styles.css` for markdown styling, reasoning blocks, tool call details, and truncation toggle

**Files changed:**
- `frontend/src/components/ChatPanel.jsx` — full rewrite (51→137 lines)
- `frontend/src/styles.css` — appended ~200 lines of new CSS
- `frontend/src/components/SessionTab.jsx` — already updated with `messages={state.history}`

## 2026-05-14 — Bridge debug logging added

## Bridge Debug Logging Added (2025-01-17)

Added debug logging to `web_ui/backend/bridge.py`:

1. **`_emit` method** — Logs structured `conversation_changed` events with:
   - Message count and roles array
   - Per-message `reasoning_content` presence flags
   - A `sample_tool_msg` (first tool_call or tool_result message found) for diagnostic inspection

2. **`_on_controller_event` method** — Logs raw controller events before translation for types: `turn`, `tool_call`, `tool_result`, `user_query`, `final`, `execution_state_change`, `token_update`, `reasoning`
   - Includes full event dict and keys list

3. **Truncation** — Both `log()` calls use default `truncate_hint=None`, which means `_truncate_data` passes data through unchanged, preserving full diagnostic data in JSONL file logs. Console output may still truncate the display line per `TM_DEBUG_TRUNCATE_LENGTH`.

## 2026-05-30 — ## Feature: `--serve-frontend` CLI flag (2026-06-02)

Added ...

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

## 2026-05-30 — ## Install & Run Scripts (created 2026-05-30)

Two scripts w...

## Install & Run Scripts (created 2026-05-30)

Two scripts were added to the project root:

- **`install_thoughtmachine.sh`** — Full install: checks Python >=3.11 & Node.js, creates `.venv`, pip installs requirements, npm installs & builds frontend.
- **`start_thoughtmachine.sh`** — Activates `.venv` and starts server with `--serve-frontend` on `127.0.0.1:8000` (override via `HOST`/`PORT` env vars).

Usage: `./install_thoughtmachine.sh && ./start_thoughtmachine.sh`

## Pre-Release Fixes Applied

## 2026-05-31 — **2026-06-02 — Three pre-release fixes applied:**

1. **Fixe...

**2026-06-02 — Three pre-release fixes applied:**

1. **Fixed hardcoded workspace path** (`resources/default_config.json`): Changed `workspace_path` from `"/home/jojo/PycharmProjects/ThoughtMachine-dev"` to `""` — new users no longer get a broken path copied to their config.

2. **Cleaned stale tool names** (`resources/default_config.json`): Removed `"Final"`, `"FinalReport"`, `"RequestUserInteraction"` from `enabled_tools` — these were consolidated into `"Respond"` and no longer exist as tools.

3. **Install script polish** (`install_thoughtmachine.sh`):
   - Added `chmod +x` for both `start_thoughtmachine.sh` and `install_thoughtmachine.sh` at end of install
   - Improved completion message: numbered next-steps, mentions auto-config creation, shows URL

4. **Handbook correction** (`resources/global_kb/handbook.md`): Updated "First Run" section to reflect that config is auto-created by the server bootstrap, not manually.

**Files changed:** resources/default_config.json, install_thoughtmachine.sh, resources/global_kb/handbook.md

## Build Scripts

## 2026-05-31 — ## Build Scripts

**2025-07-14**: Created two build scripts ...

## Build Scripts

**2025-07-14**: Created two build scripts for PyInstaller packaging:

- **`build_thoughtmachine_exe.sh`** (Linux/macOS) — Bash script, 5 steps: (1) build React frontend, (2-4) check/install Python deps, (5) run PyInstaller in one-folder (default via `thoughtmachine.spec`) or one-file mode (`ONE_FILE=1`).
- **`build_thoughtmachine_exe.bat`** (Windows) — Batch script equivalent with same 5 steps. Uses `set ONE_FILE=1` for one-file mode. Uses Windows path separators throughout. Helpers: `:info`, `:ok`, `:warn`, `:err` subroutines.

## Requirements

## 2026-05-31 — ## Requirements Split (2025-07-14)

Split `requirements.txt`...

## Requirements Split (2025-07-14)

Split `requirements.txt` into core + optional RAG to reduce venv bloat:

- **`requirements.txt`** — Core dependencies only (FastAPI, uvicorn, pydantic, openai, anthropic, tiktoken, docker, etc.). ~200 MB venv.
- **`requirements-rag.txt`** — Optional RAG stack (CPU-only PyTorch via `--index-url`, sentence-transformers, chromadb, langchain). ~500 MB extra.
- **`install_thoughtmachine.sh`** — Now accepts `--with-rag` flag to install RAG deps.

Removed from core: `PyQt6` (legacy GUI, not needed for web UI), `sentence-transformers`, `chromadb`, `langchain`, `langchain-community`.


## 2026-06-01 — ## 2026-06-03 — New User Onboarding System (Created)

### Wh...

## 2026-06-03 — New User Onboarding System (Created)

### What was done
1. **Created `user/onboarding_guide.md` in global KB** — A friendly, non-technical guide for new ThoughtMachine users. Explains concepts in plain language (workspace, session, KB). Gives suggested "first things to say." Doesn't assume prior knowledge. Warm, guided tone.

2. **Added Rule 14 to system prompt** — Both `system_prompt.txt` (root, actually loaded) and `resources/default_system_prompt.txt` (template) now contain:
   > *"When interacting with someone who seems new to ThoughtMachine, offer a guided, friendly experience. Do not assume prior knowledge — explain concepts like workspaces, sessions, and the knowledge base in plain language. Check the global KB's `user/onboarding_guide.md` for a ready-to-use friendly introduction. Suggest clear next steps. Invite questions."*

### Still open / not implemented
- **No first-time user detection mechanism** — The agent needs some way to know it's talking to a new user. Options: check for a marker in global KB (e.g., `user/user_profile.md`), or simply run onboarding when the user seems confused.
- **The "View Artifact" tool** — Previously brainstormed idea. Could pair well with onboarding (agent generates a welcome page and presents it visually).


## 2026-06-07 — # 🕵️ The Case of the Missing Panel — Full Investigation Repo...

# 🕵️ The Case of the Missing Panel — Full Investigation Report

**To**: GUI Engineer  
**From**: ThoughtMachine AI  
**Date**: 2026-06-07  
**Subject**: Complete investigation log — ConfigPanel sidebar + Docker panel feature request

---

## 1. How It Started

The user (jojo) reported: *"my Config panel sidebar was once there but not currently."* Meaning: the gray sidebar on the left side of the session view (the one with config tabs like General, Model, Tools, Permissions, etc.) that they remember seeing before, is now absent from the screen.

At this point, we had uncommitted changes in the workspace:
- `agent/config/models.py` — modified
- `agent/config/provider_profile.py` — modified
(These are the "overwrite when non-empty" fix for provider profile resolution.)

## 2. Investigation Phase 1 — Is ConfigPanel Rendering?

We checked the code path:

- **SessionTab.jsx:446** — ConfigPanel IS rendered unconditionally:
  ```jsx
  <ConfigPanel
    config={state.config}
    sendCommand={sendCommand}
    providers={providers}
    ...
  />
  ```
  No `if` guard, no `isDeferred` check. If the tab is loaded, ConfigPanel renders.

- **ConfigPanel.jsx:169** — The component signature:
  ```jsx
  function ConfigPanel({ config, sendCommand, providers, availableTools, panelWidth, wsConnected, ... })
  ```

- **ConfigPanel.jsx:302-306** — The only conditional is the loading state:
  ```jsx
  if (!config) {
    return <div style={{ padding: '1rem', ..., width: panelWidth || 280, ... }}>
      Loading config...
    </div>;
  }
  ```

- **ConfigPanel.jsx:342-343** — The real render:
  ```jsx
  return (
    <div style={{ padding: '1rem', fontFamily: 'sans-serif', background: '#313244',
                  color: '#cdd6f4', width: panelWidth || 280, minWidth: 200, maxWidth: 500,
                  flexShrink: 0, overflowY: 'auto', height: '100%' }}>
  ```

### Key findings:
- ConfigPanel uses **inline styles entirely** — no CSS class like `.config-panel` on the outer div (the `.config-panel` class in `styles.css:100` is unused vestigial CSS)
- The resize handle (`.resize-handle`) sits between ConfigPanel and the chat panel — uses `width: 5px` CSS class
- ConfigPanel is resizable via drag, persisted per tab in `localStorage` key `config-panel-width:{tabId}`

## 3. Investigation Phase 2 — Is ConfigPanel in the DOM?

The user couldn't access browser DevTools (no Elements/Inspector tab available in their Firefox). We worked around this:

- Confirmed via WS message count (907 messages received) that **the tab IS loaded and active**
- The backend sends `config_changed` events after `load_session` (server.py:721-726) — so config should arrive
- No React errors in browser console
- The user eventually found CSS via browser inspection that **exactly matches** ConfigPanel's inline styles:
  ```
  padding: 1rem;
  font-family: sans-serif;
  background: rgb(49, 50, 68);  /* = #313244 */
  color: rgb(205, 214, 244);    /* = #cdd6f4 */
  width: 280px;                  /* = panelWidth || 280 */
  min-width: 200px;
  max-width: 500px;
  flex-shrink: 0;
  overflow-y: auto;
  height: 100%;
  ```
  **Verdict: ConfigPanel IS in the DOM with correct styles.**

## 4. Investigation Phase 3 — Why Is It Not Visible?

This is where it gets tricky. ConfigPanel exists in HTML but the user says it's not visible on screen. Possible causes (not fully resolved):

| Cause | Likelihood | Notes |
|---|---|---|
| **User is looking at deferred tab** | Medium | 4 of 5 tabs are deferred ("Click tab to load conversation") — possible user was on wrong tab |
| **CSS layout clipping** | Low | Parent `.app-main` has `overflow: hidden` but ConfigPanel has `flex-shrink: 0` and fixed width |
| **Browser zoom/scroll** | Low | Could be off-screen to the right |
| **ConfigPanel is there but user didn't notice** | Low | Unlikely given user's certainty |

## 5. Plot Twist — It's Not ConfigPanel!

After the investigation, the user revealed: **"we are looking for the container panel, the docker thing"**

So the entire investigation was a misunderstanding! The user was NOT looking for the Config sidebar. They were looking for a **Docker containers panel** — a UI component that:

- **Does not exist** in the codebase
- Was never built
- Has no placeholder, no route, no component file
- Only Docker-related code is the `DockerCodeRunner` tool listing and a "Container" permission toggle in ConfigPanel's Permissions tab (lines 633-653)

## 6. Current State

### Uncommitted changes (the provider profile fix):
```
 M agent/config/models.py
 M agent/config/provider_profile.py
```

### Branch situation:
- Currently on **detached HEAD** at `4b3dde3`
- Branch `master` exists
- User plans to create a new branch (likely named `docker-panel`), commit the changes, then build the Docker panel from scratch

### The user's plan:
1. ✅ Commit current changes to new branch
2. ❓ Provide full instructions for building the Docker panel UI
3. ❓ Build the Docker panel component

## 7. Technical Notes for the GUI Engineer

### Frontend architecture (relevant parts):
- **Stack**: React (Vite), vanilla CSS (inline styles + some CSS classes in `styles.css`)
- **State management**: Per-tab state via `useState`/`useCallback` in SessionTab — no Redux/Zustand
- **Backend communication**: WebSocket (`sendCommand()` / event listeners)
- **Tab system**: Up to 5 tabs, lazy-loaded (deferred pattern), state persisted per tab
- **Config delivery**: Backend sends `config_changed` event after `load_session`
- **Styling**: Catppuccin Mocha palette (`--bg-primary: #1e1e2e`, `--bg-surface: #313244`, etc.)

### ConfigPanel inline style pattern (for reference when building new panels):
```jsx
<div style={{
  padding: '1rem',
  fontFamily: 'sans-serif',
  background: '#313244',
  color: '#cdd6f4',
  width: panelWidth || 280,
  minWidth: 200,
  maxWidth: 500,
  flexShrink: 0,
  overflowY: 'auto',
  height: '100%'
}}>
```

### The `sendCommand` interface:
```jsx
sendCommand('command_name', { payload })
```
Available commands are handled in `bridge.py` and `server.py`.

---

**End of report.** Ready for Docker panel feature design.

## How to add a new permission toggle

## 2026-06-10 — ## How to add a new permission toggle

Every new trust domai...

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


## 2026-06-10 — ## Windows packaging: "Terminate batch job (Y/N)?" fix (2026...

## Windows packaging: "Terminate batch job (Y/N)?" fix (2026-06-03)

**Problem:** `start_thoughtmachine.bat` used `powershell Start-Process` to launch Python, but `cmd.exe` (the batch file's parent console) still owned the console. When the user pressed Ctrl+C, `cmd.exe` intercepted it and prompted "Terminate batch job (Y/N)?" before the batch could continue.

**Root cause:** `cmd.exe` runs batch files synchronously. When Ctrl+C is pressed in the console, `cmd.exe`'s default handler prompts before executing the next batch line — including `exit /b`.

**Fix:** Replaced `powershell -Command "Start-Process python ... -NoNewWindow -PassThru; $p.WaitForExit(); exit $p.ExitCode"` with `start "ThoughtMachine Backend" /wait python ...`. The `start` command launches Python in a new console window (its own process group), so Ctrl+C only reaches Python, not the parent `cmd.exe`. After Python exits, the new window closes, the batch continues to `exit /b`, and **no prompt appears**.

**Key principles:**
- `start "" /wait` creates a new process in its own console — Ctrl+C isolation
- `start /b` (same-window) makes the app ignore Ctrl+C — NOT what we want
- Vite was already launched in a separate window via `start "ThoughtMachine Vite" cmd /c "npm run dev"` — the backend now follows the same pattern
- The `exit /b %ERRORLEVEL%` after `start /wait` propagates Python's exit code

## 2026-07-01 — ## Second instance port configuration (2025-07-16)

### How ...

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

## 2026-07-01 — ## 2026-07-02 — Master Vault: Design Principles (10 Sacred R...

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

## 2026-07-09 — ## Cross-Session Worker Panel Access — Changes Implemented

...

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

