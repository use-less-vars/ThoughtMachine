ThoughtMachine Logging – AI Quick Reference (v2.1)
Adding a Log Statement
python

from agent.logging import log

log(level: str, tag: str, message: str, data: dict = None)

Parameter	Description	Example
level	"DEBUG", "INFO", "WARNING", "ERROR"	"DEBUG"
tag	Hierarchical component name (area.component)	"tools.file_editor"
message	Human-readable description	"Writing file"
data	Optional dict (auto‑truncated)	{"path": p, "size": n}

Example:
python

log("DEBUG", "core.pruning", "Pruning context", {"kept": 5, "removed": 2})

Tag Naming Convention

Use area.component. Common areas:

    core – agent core, session, pruning, config

    tools – file_editor, docker, search

    llm – anthropic, openai, stepfun

    ui – presenter, output_panel, events

    session – history_provider, context_builder

Console Output Control
Variable	Effect	Default
TM_LOG_LEVEL	Minimum console level	WARNING
TM_LOG_TAGS	Comma‑separated tags to show at DEBUG/INFO	(empty)
DEBUG_<COMP>	Legacy flag for a single component	–
THOUGHTMACHINE_DEBUG=1	Firehose (all debug) – use sparingly	–

Examples:
bash

# Debug only pruning and all tools
export TM_LOG_LEVEL=DEBUG
export TM_LOG_TAGS=core.pruning,tools.*

# Quick single‑component debug
export DEBUG_EVENTBUS=1

# Back to quiet (default)
unset TM_LOG_LEVEL TM_LOG_TAGS DEBUG_EVENTBUS

File Logging (JSONL) Control
Variable	Effect	Default
TM_LOG_FILE_LEVEL	Minimum level written to JSONL file	DEBUG
TM_LOG_DIR_MAX_MB	Hard limit on total log directory size (0 = unlimited)	50

All logs are written to logs/agent_<session_id>.jsonl. Rotation: 10 MB, 5 backups.
Total directory size is capped at TM_LOG_DIR_MAX_MB (default 50 MB).
When exceeded, the oldest files (by modification time) are deleted until
under the limit. Set to 0 to disable size-based pruning.
Truncation (Prevents Bloat)
Variable	Default	Applies To
TM_DEBUG_TRUNCATE_LENGTH	100	Generic debug messages, dump_messages preview
TM_TOOL_ARGUMENTS_TRUNCATE	100	Tool call arguments
TM_TOOL_RESULT_TRUNCATE	100	Tool call results
TM_RAW_RESPONSE_TRUNCATE	100	Raw LLM responses
TM_CONSOLE_DATA_TRUNCATE	200	Structured data printed to console (when hint is provided)
TM_CONVERSATION_CONTENT_TRUNCATE	10000	Conversation messages in JSONL
TM_DOCKER_OUTPUT_TRUNCATE	10000	Docker sandbox output

Note: TM_DEBUG_TRUNCATE_LENGTH controls generic debug console output. For structured data with a hint (e.g., tool_arguments), the type‑specific limit is used. The JSONL file receives data truncated only once by type‑specific limits; console applies an additional truncation for readability.
Best Practices

    Use DEBUG for temporary instrumentation – it won't spam unless explicitly enabled.

    Use INFO for normal noteworthy events.

    Choose a specific tag (e.g., "tools.my_new_tool").

    Provide a data dict even for minimal context.