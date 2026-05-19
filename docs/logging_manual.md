ThoughtMachine Logging – AI Quick Reference (v2.2)

## Adding a Log Statement

```python
from agent.logging import log

log(level: str | LogLevel, tag: str, message: str, data: dict = None)
```

| Parameter | Description | Example |
|-----------|-------------|---------|
| level | `"DEBUG"`, `"INFO"`, `"WARNING"`, `"ERROR"` | `"DEBUG"` |
| tag | Hierarchical component name (area.component) | `"tools.file_editor"` |
| message | Human-readable description | `"Writing file"` |
| data | Optional dict (auto‑truncated) | `{"path": p, "size": n}` |

### Example
```python
log("DEBUG", "core.pruning", "Pruning context", {"kept": 5, "removed": 2})
```

---

## Console Output Format

Every console line includes an `[HH:MM:SS]` timestamp:
```
[14:23:01] DEBUG    [core.pruning] Pruning context | {"kept": 5, "removed": 2}
[14:23:02] INFO     [server.config] Config loaded
[14:23:05] WARNING  [llm.stepfun] Rate limit approaching
```

---

## Tag Naming Convention

Use `area.component`. Common areas:

| Area | Components |
|------|------------|
| `core` | session, pruning, config, controller, context_builder, turn_transaction, token_counter |
| `tools` | file_editor, docker_code_runner, docker_executor (container, policy, **build**), search |
| `llm` | anthropic, openai, stepfun |
| `server` | config, bridge |
| `ui` | presenter, output_panel, events |
| `session` | history_provider, context_builder |

---

## Runtime API (`set_log_tags`, `set_log_level`, `show_log_config`)

These functions let you change filtering at runtime without restarting the process.

### `set_log_level(level)`
```python
from agent.logging import set_log_level

set_log_level("DEBUG")       # string form
set_log_level("INFO")        # back to normal
```

### `set_log_tags(tags)`
```python
from agent.logging import set_log_tags

set_log_tags("core.*,tools.file_editor")           # string (comma-separated)
set_log_tags(["core.session", "llm.stepfun"])       # list
set_log_tags("*")                                   # firehose (all tags)
set_log_tags("")                                    # back to default (WARNING+ only)
```

### `show_log_config()`
```python
from agent.logging import show_log_config

config = show_log_config()
# Returns dict with: log_level, log_tags, truncation, env_vars
```

---

## Console Output Control

| Variable | Effect | Default |
|----------|--------|---------|
| `TM_LOG_LEVEL` | Minimum console level | `INFO` |
| `TM_LOG_TAGS` | Comma‑separated tags to show (empty = only WARNING+) | _(empty)_ |
| `DEBUG_<COMP>` | Legacy flag for a single component | – |
| `THOUGHTMACHINE_DEBUG=1` | Firehose (all debug) – use sparingly | – |

### Filtering logic
1. If `TM_LOG_TAGS` is empty → only WARNING, ERROR, CRITICAL shown
2. If `TM_LOG_TAGS` is set → matching tags shown at `>= TM_LOG_LEVEL`
3. Wildcard: `server.*` matches `server.config`, `server.bridge`, etc.
4. Per-component `DEBUG_*` env vars override everything
5. Runtime `set_log_tags()` / `set_log_level()` override env vars at runtime

### Practical workflow examples
```bash
# See everything in the server layer (INFO + DEBUG)
export TM_LOG_TAGS=server.*

# See server layer at INFO only
export TM_LOG_TAGS=server.*
export TM_LOG_LEVEL=INFO

# Focus: only server config + bridge + controller lifecycle
export TM_LOG_TAGS=server.*,core.controller

# Firehose: everything at DEBUG (warning: very verbose!)
export THOUGHTMACHINE_DEBUG=1

# Quick single‑component debug (legacy)
export DEBUG_OPENAI=1

# Back to quiet (default)
unset TM_LOG_LEVEL TM_LOG_TAGS THOUGHTMACHINE_DEBUG
```

### Runtime examples (no env vars needed)
```python
# In agent code or interactive console:
from agent.logging import set_log_level, set_log_tags

set_log_level("DEBUG")
set_log_tags("core.turn_transaction,tools.docker_executor.build")
```

---

## File Logging (JSONL) Control

| Variable | Effect | Default |
|----------|--------|---------|
| `TM_LOG_FILE_LEVEL` | Minimum level written to JSONL file | `DEBUG` |
| `TM_LOG_DIR_MAX_MB` | Hard limit on total log directory size (0 = unlimited) | `50` |

All logs are written to `logs/agent_<session_id>.jsonl`. Rotation: 10 MB, 5 backups.
Total directory size is capped at `TM_LOG_DIR_MAX_MB` (default 50 MB).
When exceeded, the oldest files (by modification time) are deleted until
under the limit. Set to `0` to disable size-based pruning.

---

## Truncation (Prevents Bloat)

| Variable | Default | Applies To |
|----------|---------|------------|
| `TM_DEBUG_TRUNCATE_LENGTH` | 100 | Generic debug messages, dump_messages preview |
| `TM_TOOL_ARGUMENTS_TRUNCATE` | 100 | Tool call arguments |
| `TM_TOOL_RESULT_TRUNCATE` | 100 | Tool call results |
| `TM_RAW_RESPONSE_TRUNCATE` | 100 | Raw LLM responses |
| `TM_CONSOLE_DATA_TRUNCATE` | 200 | Structured data printed to console |
| `TM_CONVERSATION_CONTENT_TRUNCATE` | 10000 | Conversation messages in JSONL |
| `TM_DOCKER_OUTPUT_TRUNCATE` | 10000 | Docker sandbox output |

> **Note:** `TM_DEBUG_TRUNCATE_LENGTH` controls generic debug console output.
> For structured data with a hint (e.g., `tool_arguments`), the type‑specific limit is used.
> The JSONL file receives data truncated only once by type‑specific limits;
> console applies an additional truncation for readability.

---

## Best Practices

- Use **DEBUG** for temporary instrumentation – it won't spam unless explicitly enabled.
- Use **INFO** for normal noteworthy events.
- Choose a **specific tag** (e.g., `"tools.my_new_tool"`).
- Provide a **data dict** even for minimal context.
- Use `set_log_tags()` and `set_log_level()` during development to toggle filters.
- Use `show_log_config()` in diagnostic output or `/debug` endpoints.