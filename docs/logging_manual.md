Here's a compact, AI‑optimized version designed for a limited context window. It contains only what an agent needs to know to add and control logging.

```markdown
# ThoughtMachine Logging – AI Quick Reference

## Adding a Log Statement

```python
from agent.logging import log

log(level: str, tag: str, message: str, data: dict = None)
```

| Parameter | Description | Example |
|-----------|-------------|---------|
| `level` | `"DEBUG"`, `"INFO"`, `"WARNING"`, `"ERROR"` | `"DEBUG"` |
| `tag` | Hierarchical component name (`area.component`) | `"tools.file_editor"` |
| `message` | Human-readable description | `"Writing file"` |
| `data` | Optional dict (auto‑truncated) | `{"path": p, "size": n}` |

**Example:**
```python
log("DEBUG", "core.pruning", "Pruning context", {"kept": 5, "removed": 2})
```

## Tag Naming Convention

Use `area.component`. Common areas:

- `core` – agent core, session, pruning, config
- `tools` – file_editor, docker, search
- `llm` – anthropic, openai, stepfun
- `ui` – presenter, output_panel, events
- `session` – history_provider, context_builder

## Console Output Control (Environment Variables)

| Variable | Effect | Example |
|----------|--------|---------|
| `TM_LOG_LEVEL` | Minimum console level (default `WARNING`) | `DEBUG` |
| `TM_LOG_TAGS` | Comma‑separated tags to show at DEBUG/INFO | `core.pruning,tools.*` |
| `DEBUG_<COMP>` | Legacy flag for a single component | `DEBUG_EVENTBUS=1` |
| `THOUGHTMACHINE_DEBUG=1` | Firehose (all debug) – use sparingly | |

**Examples:**
```bash
# Debug only pruning and all tools
export TM_LOG_LEVEL=DEBUG
export TM_LOG_TAGS=core.pruning,tools.*

# Quick single‑component debug
export DEBUG_EVENTBUS=1

# Back to quiet (default)
unset TM_LOG_LEVEL TM_LOG_TAGS DEBUG_EVENTBUS
```

## Truncation (Prevents Bloat)

| Variable | Default |
|----------|---------|
| `TM_DEBUG_TRUNCATE_LENGTH` | 100 |
| `TM_TOOL_ARGUMENTS_TRUNCATE` | 100 |
| `TM_TOOL_RESULT_TRUNCATE` | 100 |
| `TM_RAW_RESPONSE_TRUNCATE` | 100 |
| `TM_CONSOLE_DATA_TRUNCATE` | 200 |

## File Logging (Always On)

- Location: `logs/agent_<session_id>.jsonl`
- Format: JSONL (one JSON object per line)
- Rotation: 10 MB, 5 backups

## Best Practices

- Use `DEBUG` for temporary instrumentation – leave it in; it won't spam.
- Use `INFO` for normal noteworthy events.
- Choose a specific tag (e.g., `"tools.my_new_tool"`).
- Provide a `data` dict even for minimal context.
```

This version is self‑contained, around 60 lines, and includes everything an AI agent needs to implement logging correctly.