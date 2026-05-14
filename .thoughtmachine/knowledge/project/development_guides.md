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
## Logging API Reference

*(Migrated from docs/logging_manual.md — Last validated: 2026-05-05)*

### Adding a Log Statement
```python
from agent.logging import log

log(level: str, tag: str, message: str, data: dict = None)
```

| Parameter | Description | Example |
|-----------|-------------|---------|
| `level` | "DEBUG", "INFO", "WARNING", "ERROR" | `"DEBUG"` |
| `tag` | Hierarchical component name (area.component) | `"tools.file_editor"` |
| `message` | Human-readable description | `"Writing file"` |
| `data` | Optional dict (auto-truncated) | `{"path": p, "size": n}` |

Example:
```python
log("DEBUG", "core.pruning", "Pruning context", {"kept": 5, "removed": 2})
```

### Tag Naming Convention
Use `area.component` format:
- **core** — agent core, session, pruning, config
- **tools** — file_editor, docker, search
- **llm** — anthropic, openai, stepfun
- **ui** — presenter, output_panel, events
- **session** — history_provider, context_builder

### Console Output Control
| Variable | Effect | Default |
|----------|--------|---------|
| `TM_LOG_LEVEL` | Minimum console level | WARNING |
| `TM_LOG_TAGS` | Comma-separated tags to show at DEBUG/INFO | (empty) |
| `DEBUG_<COMP>` | Legacy flag for a single component | – |
| `THOUGHTMACHINE_DEBUG=1` | Firehose (all debug) | – |

Examples:
```bash
# Debug only pruning and all tools
export TM_LOG_LEVEL=DEBUG
export TM_LOG_TAGS=core.pruning,tools.*

# Quick single-component debug
export DEBUG_EVENTBUS=1

# Back to quiet (default)
unset TM_LOG_LEVEL TM_LOG_TAGS DEBUG_EVENTBUS
```

### File Logging (JSONL)
`TM_LOG_FILE_LEVEL` controls minimum level written to JSONL file (default: DEBUG). All logs go to `logs/agent_<session_id>.jsonl`. Rotation: 10 MB, 5 backups.

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

### Best Practices
- Use **DEBUG** for temporary instrumentation (won't spam unless explicitly enabled)
- Use **INFO** for normal noteworthy events
- Choose a **specific tag** (e.g., `"tools.my_new_tool"`)
- Provide a **data dict** even for minimal context

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

### New Methods Added (3 methods, ~220 lines)
1. **`_create_agent_branch(repo_root)`** — Creates `agent_{base}_{suffix}` branch from base, validates suffix format, checks for duplicates
2. **`_switch_branch(repo_root)`** — Switches to existing branch (agent branches + readonly branches allowed)
3. **`_cleanup_agent_branch(repo_root)`** — Stashes changes, switches to merge target, safe-deletes branch

### New Fields (3 Pydantic fields)
- `branch_suffix: Optional[str]` — Suffix for new branch names
- `base_branch: str = "dev"` — Source branch for branching
- `branch_name: Optional[str]` — Target for switch/cleanup

### New Literal Operations
- `"create_agent_branch"`, `"switch_branch"`, `"cleanup_agent_branch"`

### Config Used
- `.thoughtmachine/git_config.json`: `agent_branch_prefix`, `readonly_branches`, `allowed_merge_targets`

### Protection
- All writes blocked on `dev`/`main`/`master` via `_assert_not_readonly_branch()`
- `_switch_branch` only allows agent/readonly branches
- `_cleanup_agent_branch` only operates on agent-prefixed branches
- Safe delete (`-d` flag) — only if fully merged

## 2026-05-07 — ## Phase 2 Complete: Commit on Agent Branches

**New Pydanti...

## Phase 2 Complete: Commit on Agent Branches

**New Pydantic fields added:**
- `commit_message: Optional[str]` — commit message (required, validates format)
- `file_paths: Optional[List[str]]` — list of files to `git add`
- `add_all: bool = False` — if True, runs `git add -A` instead

**New operation:** `"commit_on_agent_branch"`

**Method `_commit_on_agent_branch()` validates:**
1. `commit_message` is non-empty
2. Current branch is an agent branch (starts with `agent_` prefix from config)
3. Message matches `<type>: <description>` format where type is one of `allowed_commit_types` (configurable, defaults: fix, feat, refactor, chore, docs, test, perf, ci)
4. If `add_all=False`, `file_paths` must be provided
5. If `add_all=True`, runs `git add -A` ignoring file_paths

**Staging flow:**
- `add_all=True` → single `git add -A`
- `add_all=False` → iterative `git add <path>` for each path in file_paths

**Commit:** `git commit -m "<message>"` via `_run_git_write`

**Safety:** Blocked on non-agent branches, protected by `_assert_not_readonly_branch()`. All writes through `_run_git_write` with error_context.

**Cleanup:** Moved `import re` from local scope in `_create_agent_branch` to module-level import.

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
