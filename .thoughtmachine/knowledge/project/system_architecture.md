# System Architecture
Architecture notes, components, and system design. **Restructured 2026-08-16**: dated investigation logs before 2026-08-01 moved to `archive_arch_a.md` / `archive_arch_b.md`; evergreen reference sections + recent deep-dives retained.
## Current Status (2026-08-16)
- Evergreen reference sections retained (verbatim).
- Dated investigation logs (2026-05-06 → 2026-07-31) archived: `archive_arch_a.md` (≤ 2026-07-15), `archive_arch_b.md` (2026-07-16 → 2026-07-31 + stale dev guides + completed roadmap phases).
## Reference (evergreen)
## Current Status

## Current Status
- ✅ Comprehensive architecture documentation covering all system layers
- Sections: System Assessment, Pruning & Context Management, System Notifications, Message Metadata, Security Layer, RAG System, State Machine, Session Architecture, Logging, Config System, Web UI, and more
- Last major update: 2026-05-28 (Tool Output Truncation framework, Respond tool architecture)

## Components
(To be populated)

## Permission Categories (required_categories) — 2026-07-10

Every tool now declares a `required_categories` ClassVar (e.g. `["filesystem:read"]`, `["filesystem:write"]`), which specifies what session permissions the tool needs to run.

- Base class: `ToolBase.required_categories` defaults to `[]` (no special permissions).
- `ToolBase.get_required_categories(params)` classmethod returns the static list by default.
- Subclasses can override `get_required_categories()` for operation-level granularity (e.g., FileEditor returns `["filesystem:read"]` for read ops, `["filesystem:write"]` for write/delete ops).
- `tool_executor.py` calls `tool_class.get_required_categories(arguments)` before execution, passing the result to `_check_permissions()`.
- `_check_permissions()` uses `DEFAULT_SESSION_PERMISSIONS` (container=false, network=false, filesystem=read, security=read, execution=banned) as fallback when `session_permissions` is None.
- All 23 tool subclasses now have `required_categories` defined or `get_required_categories` overridden.

## Data Flow
(To be populated)

## Comprehensive System Assessment (2026-05-04)

## Comprehensive System Assessment — 2026-05-04

### Architectural Layers

#### 1. Agent Core (`agent/core/`)
- **`agent.py`** (coordinator) — Delegates to TokenCounter, LLMClient, ConversationManager, ToolExecutor, TurnTransaction, DebugContext
- **`llm_client.py`** — LLM communication (OpenAI, Anthropic, etc.). Imports HistoryProvider from session.
- **`tool_executor.py`** — Tool execution orchestration
- **`token_counter.py`** — Token estimation
- **`conversation_manager.py`** — Conversation history management. Imports DebugContext.
- **`debug_context.py`** — Debug helper. Used by agent.py and conversation_manager.py. Gate: `os.environ.get('DEBUG_CONTEXT')`.
- **`turn_transaction.py`** — Turn lifecycle management

#### 2. Logging (`agent/logging/`)
- **`__init__.py`** — Re-exports `log()` from unified.py
- **`unified.py`** — Defines `log()` function using AgentLogger
- **`debug_log_adapter.py`** — Adapts debug logging
- **Not imported anywhere**: `logging_helpers.py` (dump_messages utility)

#### 3. Configuration (`agent/config/`)
- **`models.py`** — `AgentConfig` Pydantic model (CentralConfig refactored)
- **`loader.py`** — Config file loading
- **`service.py`** — Config service layer  
- **`presets.py`** — Configuration presets
- **`provider_profiles.py`** — LLM provider profile definitions
- All actively imported throughout the codebase. Note: top-level `config/` directory is dead/unused.

#### 4. MCP (`tools/mcp_client.py`, `tools/mcp_manager.py`, `tools/mcp_validator.py`)
- **`mcp_client.py`** — Active MCP client. Imports from mcp_manager.
- **`mcp_manager.py`** — Manages MCP server lifecycle. Imports mcp_validator.
- **`mcp_validator.py`** — Validates MCP server configurations.
- **DEAD**: `tools/mcp_client_new.py` — Unused, replaced by mcp_client.py.

#### 5. Docker (`docker_executor.py`, `tools/docker_code_runner.py`)
- **`docker_executor.py`** — Core Docker executor class. Imported by security.py and docker_code_runner.py (via lazy imports).
- **`tools/docker_code_runner.py`** — The DockerCodeRunner tool. Imports docker_executor lazily.

#### 6. Sessions (`session/`)
- **`store.py`** — Session store (SQLite-based). Imports Session from models.
- **`models.py`** — Session Pydantic model
- **`context_builder.py`** — Context building strategies (ContextBuilder ABC, SummaryBuilder, TurnBuilder). Uses DEBUG_CONTEXT env var.
- **`history_pruner.py`** — History pruning logic
- **`history_provider.py`** — HistoryProvider (implements HistoryProviderInterface). Used by llm_client.py.
- **`utils.py`** — Utility functions (normalize_conversation_for_hash). Used by agent.py.
- **DEAD**: `session/event_schema.py` — Parallel events system not imported anywhere. Conflicts with `agent/events.py`.

#### 7. GUI (`qt_gui/`)
- **Active modules**: conversation_panel.py, input_panel.py, main_window.py, output_panel.py, settings_panel.py, thinking_indicator.py, utils.py
- **Potentially stale**: qml_gui/ (QML-based GUI, likely an earlier attempt)
- **DEAD**: `output_panel_phase1.py` in qt_gui/ — Unused backup of output_panel.py

#### 8. Tools Registry (`tools/__init__.py`)
- Defines `SIMPLIFIED_TOOL_CLASSES` list — official registry of available tools
- Each tool is a class with name, description, parameters schema
- `get_filtered_tool_classes()` on AgentConfig filters by enabled/disabled tools

#### 9. Security (`thoughtmachine/security.py`)
- Sandbox environment validation, Docker setup with security policies
- Imports docker_executor lazily

#### 10. Events (`agent/events.py`)
- Event system for agent lifecycle events
- **DEAD**: `session/event_schema.py` has a parallel events definition, unused

### Dead Code & Cleanup Candidates

| File | Status | Notes |
|------|--------|-------|
| `tools/mcp_client_new.py` | DEAD | Replaced by mcp_client.py |
| `qt_gui/output_panel_phase1.py` | DEAD | Backup of output_panel.py |
| `config/` (top-level directory) | DEAD | Files like preset_loader.py, generic_provider.py, etc. Unused. |
| `preset_loader.py` (top-level) | DEAD | Duplicate of agent/config/presets.py |
| `llm_providers/orchestrator.py` | DEAD | Unused orchestration layer |
| `session/event_schema.py` | DEAD | Parallel events system, unused |
| `qml_gui/` | STALE | QML GUI attempt, likely superseded by pyqt GUI |
| `agent/logging/logging_helpers.py` | DEAD | dump_messages utility not imported |
| `agent/core/prompts.py` | ACTIVE | System prompts used by agent |
| Various `*_orig.py`, `*.bak`, `*.orig` files | DEAD | Backup files from refactoring |

### Key Insights
- Agent-core is well-modularized (7 specialized classes)
- Session layer has parallel history_provider and context_builder — sometimes redundant with agent/core logic
- DEBUG_CONTEXT flag (env var) controls extensive debug logging across multiple files
- Lazy imports used strategically to avoid circular dependencies (docker_executor, security)
- Configuration has been migrated from flat config/ to agent/config/ with Pydantic models

## 2026-KB-AUDIT — Corrections to the above assessment

### `logging_helpers.py` — NOW ACTIVE (not DEAD)
The `dump_messages` utility from `agent/logging/logging_helpers.py` is now actively imported and used by:
- `agent/core/agent.py` (line 23, direct import) — called in process_query() debug logging
- `session/history_provider.py` (line 22, try/except fallback import) — called at line 131
- `session/context_builder.py` (line 30, try/except fallback import) — called at line 334

The dead code table row `| agent/logging/logging_helpers.py | DEAD | dump_messages utility not imported |` should read:
**| `agent/logging/logging_helpers.py` | ACTIVE | dump_messages utility imported by agent.py, history_provider.py, context_builder.py |**

### `config/` (top-level directory) — REMOVED (not "DEAD")
The top-level `config/` directory has been deleted from disk. The table entry `| config/ (top-level directory) | DEAD | Files like preset_loader.py, ...` should read:
**| `config/` (top-level directory) | REMOVED | Directory and all files deleted from disk |**


## Pruning & Context Management

## Pruning & Context Management

*(Migrated from docs/pruning_system.md — Last validated: 2026-05-05)*

### Overview
ThoughtMachine maintains two parallel representations of a conversation:
- **user_history** — append-only list of every message (user, assistant, tool, system, warnings, summaries). This is the ground truth for GUI and LLM context reconstruction.
- **LLM context** — sliding window built from user_history, sent to the LLM. Excludes pruned/summarized messages.

### Core Concepts
- **Turns**: One round-trip interaction with the LLM. A user message or an assistant message with tool_calls starts a new turn. Tool results belong to their calling turn. Summaries are inserted at turn boundaries.
- **user_history**: Append-only. Messages are never deleted — full history preserved for auditing/GUI. Each message has a sequential index (idx).
- **LLM Context**: Built by `SummaryBuilder.build()` in `session/context_builder.py`. Always contains: system prompt + latest summary + messages after summary + relevant warnings.

### Summarization (Pruning) Flow
1. **Trigger**: Agent calls `SummarizeTool` (usually after token warning)
2. **_apply_summary_pruning()** (in `agent/core/agent.py`):
   - Computes insertion index via `_find_summary_insertion_index(keep_recent_turns)`
   - Inserts summary system message at that index (turn boundary)
   - Appends unwarning AFTER the SummarizeTool result (not inserted — preserves chronological order)
3. **Result**: LLM context starts at the new summary. Old messages before it are excluded.

### Token Warning Lifecycle (Simplified)
- Soft warning (~35k tokens) → informs agent of thresholds
- Critical warning (~50k tokens) → sets `restrictions_pending = True`
- Same turn: all tools allowed (agent can SummarizeTool immediately)
- Next turn if still CRITICAL: `restrictions_active = True` → only SummarizeTool, Final, FinalReport allowed
- No countdown logic — the old 5-turn countdown with expiration events was removed
- Unwarning ("Context has been summarized") is appended after SummarizeTool result

### Key Code Locations
| Component | File | Purpose |
|-----------|------|---------|
| _apply_summary_pruning | agent/core/agent.py | Inserts summary, appends unwarning |
| _find_summary_insertion_index | agent/core/agent.py | Finds turn boundary for insertion |
| SummaryBuilder.build | session/context_builder.py | Builds LLM context from user_history |
| AgentState.update_token_state | agent/core/state.py | Generates token warnings |
| _add_to_conversation | agent/core/agent.py | Adds messages to user_history |

### Design Decisions
- Two copies of summary exist: system message (resets LLM context) and tool result (audit record). Both serve different purposes.
- Unwarning is **appended** (not inserted) to preserve chronological order with the SummarizeTool call.
- Stale warnings in the GUI are normal — the GUI shows full history, filtering is a separate UI feature.

## System Notifications

## System Notifications

*(Migrated from docs/system_notifications.md — Last validated: 2026-05-05)*

### Overview
System notifications are internally-generated messages that inform the agent about token usage, turn limits, and context clearing. They appear in user_history and LLM context with `role='user'` and a `[SYSTEM NOTIFICATION]` prefix.

### Why role = "user"
LLM providers (OpenAI, Anthropic) typically ignore system-role messages for agent reaction to warnings. Using `role="user"` ensures the agent "hears" the notification and can act (e.g., call SummarizeTool). This is deliberate.

### Metadata flag: is_system_notification
All new notifications include `"is_system_notification": true` in their message dictionary. Legacy prefixes `[**SYSTEM NOTIFICATION**]` and `[****SYSTEM NOTIFICATION****]` exist only in old sessions and are no longer generated.

### Where the flag is added (agent/core/agent.py)
- Turn warning events (pre-LLM, `_handle_state_event`)
- Token warning events (pre-LLM, `_handle_state_event`)
- Secondary token/turn warning paths
- Post-tool token warnings (`_update_tokens_after_tool`)
- Context cleared (unwarning) after summarization

### Where the flag is used
Only in two internal methods for turn counting and summary placement:
- `_find_summary_insertion_index` — skips flagged messages when calculating insertion point
- `_group_messages_into_turns` — skips flagged messages to prevent empty/spurious turns

### Where it's NOT used
The flag is NOT used in `SummaryBuilder.build()` — notifications after the summary inclusion point are always included in LLM context.

### Lifecycle
1. **Trigger**: `AgentState.update_token_state()` or `update_turn_state()` detects soft/critical threshold
2. **Creation**: Event handler creates a message dict with `role="user"`, `[SYSTEM NOTIFICATION]` content, and `is_system_notification=True`
3. **Injection**: Appended to user_history immediately
4. **Summarization**: Notifications skipped during turn counting → insertion index unaffected
5. **LLM context**: All messages from summary insertion point forward included (including notifications)
6. **GUI**: MessageRenderer recognizes the flag and renders with special styling

### Backward Compatibility
Old sessions without the metadata flag are supported via fallback content-based checks for `[SYSTEM NOTIFICATION]`, `[**SYSTEM NOTIFICATION**]`, and `[****SYSTEM NOTIFICATION****]` substrings.

### Common Pitfalls
- Do NOT change role to "system" — LLM will ignore
- Do NOT use content-string matching alone for new code — use metadata flag
- Do NOT add the flag to normal user messages
- Do NOT rely on countdown logic — it has been removed

## Message Metadata Schema

## Message Metadata Schema

*(Migrated from docs/message_metadata.md — Last validated: 2026-05-05)*

### Overview
Messages in `user_history` can contain metadata fields beyond standard `role`, `content`, `tool_calls`, and `tool_call_id`. These are used internally for sequencing, rendering, pruning, and debugging. They are generally not passed to the LLM (or harmless if passed).

### Standard Metadata Fields
| Field | Type | Description |
|-------|------|-------------|
| `seq` | integer | Sequential index assigned by `Session._get_next_seq()` |
| `created_at` | string (ISO) | Timestamp when message was added |
| `is_system_notification` | boolean | True for system-generated notifications (token warnings, turn warnings, context cleared) |

### Tool Message Fields
- `tool_calls` (list): Standard OpenAI-compatible tool call objects with `id`, `type`, `function`
- `tool_call_id` (string): Matches the `id` of the corresponding tool call
- `content` (string): The output of the tool

### Summary/Pruning Fields
On summary system messages (inserted by `_apply_summary_pruning`):
| Field | Type | Description |
|-------|------|-------------|
| `summary` | boolean | Always True — indicates this is a summary |
| `pruning_keep_recent_turns` | integer | The `keep_recent_turns` value from SummarizeTool |
| `pruning_discarded_msg_count` | integer | Number of pruned messages before this summary |
| `pruning_insertion_idx` | integer | Index in user_history where summary was inserted |

### Where Metadata is Stripped (or Not)
- **SummaryBuilder.build** (`session/context_builder.py`): Copies ALL fields from user_history — does not strip any metadata. Acceptable because most LLM APIs ignore unknown fields.
- **Qt GUI**: Reads `user_history` directly, uses only `role`, `content`, and `is_system_notification` (for styling).
- **Session persistence**: `to_persistable_dict` / `from_persistable_dict` preserve all fields.

### Guidelines for Adding New Fields
1. Use descriptive `lower_snake_case` names. Prefix with `_` if strictly internal.
2. New fields should be optional for backward compatibility.
3. Document in this file and related component docs.
4. If the field affects LLM behavior, consider whether a separate role or content pattern is more appropriate.

## Security Layer

## Security Layer

*(Migrated from docs/security_layer.md — Last validated: 2026-05-05)*

### Status
Partially implemented; **disabled in v1.0** by setting `default_policy: "allow"`. Workspace validation and Docker sandbox remain active.

### Architecture
**Components:**
- `thoughtmachine/security.py` — Core security logic (CapabilityRegistry, policy evaluation, prompting)
- `session/models.py` — `Session.security_config` stores per-session policies
- `agent/core/tool_executor.py` — Calls `is_allowed()` before tool execution
- `agent/events.py` — EventBus for SECURITY_PROMPT / SECURITY_RESPONSE events
- **GUI** — No subscription to security events yet (planned for v2.0)

**Data Flow:**
```
ToolExecutor → CapabilityRegistry.check() → is_allowed()
    ↓ (if "ask")
_request_security_prompt() → publish(SECURITY_PROMPT) → wait on queue.Queue (timeout 300s)
    ↓ (response or timeout)
return (approved, remember) → update session.security_config if remember
```

### Security Profiles
Defined in `security.py`:
| Profile | Behavior |
|---------|----------|
| `default` | default_policy: "ask" |
| `read_only` | Forces read_only: True |
| `file_editor` | Allows only fs:read, fs:write |
| `sandboxed` | Ask for Docker/MCP/git |
| `permissive` | default_policy: "allow" (v1.0 default) |
| `restricted` | default_policy: "deny" |

### Capability Registry
Tools declare `requires_capabilities` class attribute (list of strings). Examples: `["fs:read"]`, `["fs:write"]`, `["container:exec"]`, `["mcp:access"]`, `["git:access"]`. Registry built at import time by scanning ToolBase subclasses.

### Known Issues (Pre-v1.0)
- **No GUI subscription**: SECURITY_PROMPT events published but never handled → main thread blocks on queue.Queue.get(timeout=300) → times out after 5 minutes → denies tool
- **Default policy "ask"** causes every tool to trigger a prompt → system hangs. Workaround: changed default_policy to "allow"
- **Blocking in main thread**: agent's main thread waits for user input. Proper implementation requires async handshake or separate prompt thread

### Future Work (v2.0)
- Add GUI dialog for SECURITY_PROMPT events (PyQt6 modal)
- Replace blocking queue with event-based handshake
- Expose security panel in GUI (read-only toggle, network domains, tool overrides)
- Migrate workspace restriction from agent_config.json into security_config
- Implement hierarchical policies (global → session → agent → tool)

## RAG System (Codebase Memory)

## RAG System (Codebase Memory)

*(Migrated from docs/rag_for_code.md — Last validated: 2026-05-05)*

### Overview
Provides codebase memory via Retrieval-Augmented Generation. Indexes source files into ChromaDB vector database and exposes `SearchCodebaseTool` for semantic code discovery.

### Architecture
```
Source Files → [AST Chunker] → chunks → [Embedding Model] → embeddings → [ChromaDB] → [SearchCodebaseTool]
```
- **Embedding model**: `BAAI/bge-small-en-v1.5` (33M params) via `sentence-transformers`
- **Vector store**: ChromaDB, persistent at `~/.thoughtmachine/rag/`
- **Chunking**: AST-aware via `tree-sitter` (with line/paragraph fallback)
- **Search**: Cosine similarity with score thresholding; supports intents (`exact`, `broad`, `file`) and path filtering

### Key Files
| File | Role |
|------|------|
| `agent/knowledge/codebase_indexer.py` | Core indexing + CLI commands |
| `agent/knowledge/embedder.py` | `embed_chunks_batched()` |
| `agent/knowledge/chunker.py` | AST-based chunker |
| `agent/config/models.py` | `AgentConfig` RAG fields |
| `agent/cli/rag_commands.py` | CLI: index-codebase, update-index |
| `tools/search_codebase.py` | `SearchCodebaseTool` |

### Configuration
In `AgentConfig` and `agent_config.json`:
- `rag_enabled` (default: true)
- `rag_embedding_model` (default: `BAAI/bge-small-en-v1.5`)
- `rag_vector_store_path` (default: `~/.thoughtmachine/rag/`)
- `rag_chunk_size` (1500), `rag_chunk_overlap` (200), `rag_batch_size` (16), `rag_truncate_dim` (256)

**Note**: The `ConfigService._SCHEMA` duplication mentioned in the original doc has been resolved — that attribute no longer exists. Config is solely managed via `AgentConfig` Pydantic model.

### Indexing Workflow
- **Full index** (`index-codebase`): Walk workspace → filter files → AST chunk → embed → store in ChromaDB
- **Incremental update** (`update-index`): Compare `.index_state.json` → re-chunk changed files → delete old chunks → add new ones
- Each workspace has a separate ChromaDB collection identified by `codebase_{workspace_hash}`

### Search Tool (`SearchCodebaseTool`)
- **Parameters**: `query` (str), `top_k` (int, default 5), `intent` ("exact"/"broad"/"file"), `restrict_to_path` (optional)
- **Execution**: Embed query → query ChromaDB → filter by score → format as Markdown with file paths, line numbers, scores
- **GUI**: Checkbox visible only when `rag_enabled=True`
- **System prompt**: Rule 8 promotes usage

### Known Issues
- No automatic re-indexing — user must run `update-index` manually
- No multi-project UI — workspace switching requires restart
- Embedding model downloaded on first use (first run may be slow)
- `sentence-transformers` logs harmless `UNEXPECTED` key warning for `position_ids`

### Future Enhancements (Planned)
1. Session Notebook Memory (KB tool partially fulfills this — without vector search)
2. Staleness detection on agent startup
3. GUI workspace switcher

## Project Historical Trajectory

## Project Historical Trajectory (March–April 2026)

### Phase 0: Origins — Construction Cleanup & Tool Foundation (March 1–3)
- **Initial architecture**: "Construction workspace" model — changes made in `./construction/` directory, then "promoted" to stable. Replaced with **git worktree architecture** (separate `ThoughtMachine` and `ThoughtMachine-dev` directories).
- **First major task**: Remove all construction workspace references from 6 tools (FileEditor, FileLister, DirectoryCreator, FileMover, BatchFileEditor, CodeModifier), system prompt, and AI docs.
- **Tool ecosystem genesis**: Original tools were basic (FileEditor, BatchFileEditor, FileLister). Agent inefficiency identified as the core problem — agents spent excessive turns reading files line-by-line.
- **CodeModifier vision** (March 3): Proposed as the "premier code modification tool" using LibCST for AST-based operations. Initial operations: add_function, add_method, add_import, add_class, replace_function_body, modify_function.
- **File System Access Improvement Plan** (March 1): Proposed 5 new code understanding tools: FileSummaryTool, FileSearchTool, DirectoryTreeTool, FilePreviewTool, FileMetadataTool — all later implemented.

### Phase 1: Tool Ecosystem Buildout (March 4–8)
- **Logging system**: Structured JSONL logging implemented (`agent_logging.py`), with turn-by-turn data, tool calls, token usage.
- **Task Manager**: Recurrence feature for task management.
- **Tool redundancy analysis** (March 6): Identified BatchFileEditor as fully superseded by FileEditor. Also analyzed: FileSearchTool vs FileEditor grep, FilePreviewTool vs FileEditor read. Conclusion: keep specialized tools, deprecate BatchFileEditor.
- **ApplyEdits upgrades**: Enhanced with regex support, resilient matching, batch editing.
- **LLM Interaction Analysis** (March 6): Studied agent conversation patterns, token usage, tool call efficiency.
- **Context Window Load Analysis**: Studied system prompt and tool schema token consumption.
- **Tool output truncation**: Implemented to prevent context overflow from large tool outputs.
- **Log Viewer**: ThoughtMachine Log Viewer for browsing structured logs.
- **Token Counter and Session Management**: Initial token counting with session-level fixes.

### Phase 2: Modular Agent Architecture (March 8–9)
- **Agent Core Modularization — Phase 1 & 2**: Split monolithic agent.py into: agent.py (core brain), agent_state.py (state machine with TokenState/TurnState/ExecutionState enums), agent_controller.py (thread management), agent_logging.py (structured logging), agent_core.py (shared data structures).
- **Keep-Alive behavior**: Agent can process multiple queries without restarting via query queue.
- **GUI integration**: Agent runs in background thread, yields events → Controller forwards to event queue → GUI polls and displays.
- **Pause/Stop improvements**: Thread-safe control with synchronization primitives.
- **Architecture overview** (March 9): Documented the 3-layer architecture (Agent → Controller → GUI) with clean separation of concerns.

### Phase 3: Pruning System & Token Management (March 8–11)
- **Pruning System**: Initial implementation with SummarizeTool — LLM generates summary, system message inserted at turn boundary, old messages excluded from context.
- **Token Monitoring**: Warning at 70-80% of context window, critical at 90%, with countdown-based restrictions (later removed).
- **Context Warning System**: System notifications with `[SYSTEM NOTIFICATION]` prefix.
- **DirectoryTreeTool bugs**: Pydantic validation errors, exclude_dirs parameter debugging.
- **Summarization pruning bug**: Grouping logic corrected for turn boundary calculation.
- **FilePreviewTool/FileSearchTool safety**: Max file size limits to prevent large file reads.

### Phase 4: GUI Refactoring & Quality of Life (March 11–16)
- **Token Monitoring GUI**: Warning/critical indicators, real-time token display.
- **Max Turns**: GUI implementation with configurable limits.
- **Collapsible Agent Controls Panel**: UI modernization.
- **Tool Output Truncation**: 12.8KB report on completion.
- **Smart Scrolling**: Auto-scroll behavior, cursor position management.
- **Temperature Configuration**: User-adjustable parameter in GUI.
- **Configuration Persistence**: Agent settings saved across sessions.

### Phase 5: Docker Code Execution (March 14–16)
- **Docker executor**: Secure container-based code execution with dropped capabilities, read-only root FS.
- **Docker Python Executor**: Tool for running Python/shell scripts in isolated container.
- **Container pooling**: Deterministic container names, reuse across executions with idle timeout.
- **Markdown rendering**: Fixes for GUI markdown display.

### Phase 6: Multi-LLM Support & Provider Abstraction (March 16–17)
- **Multi-Provider Architecture**: Provider factory pattern — OpenAI-compatible, Anthropic, etc.
- **Provider profiles**: Per-provider configuration (model names, context windows, API endpoints).
- **GUI Integration**: Provider selection in settings panel.
- **Routeway AI Authentication**: Bug fix for custom provider auth.
- **OpenAI-Compatible Provider Debug**: Raw response debugging.
- **Base URL persistence**: User-entered base URLs correctly used.

### Phase 7: MCP Integration (March 17–20)
- **Model Context Protocol**: External tool integration via MCP bridge.
- **Multi-transport MCP**: Support for stdio and HTTP transports.
- **MCP Config GUI**: Integration with configuration panel.
- **MCP Tool Validation**: Input schema validation for external tools.
- **MCP Echo Tool Debugging**: Connection and message format fixes.

### Phase 8: Security Layering (March 19–20)
- **7-Layer Agentic Stack proposal**: Inspired by OSI network model — formalized security boundaries.
- **Centralized security module**: `thoughtmachine/security.py` (420 lines).
- **Capability Registry**: Tools declare `requires_capabilities` (fs:read, fs:write, container:exec, etc.).
- **Security profiles**: default, read_only, file_editor, sandboxed, permissive, restricted.
- **Status**: Partially implemented; default_policy set to "allow" in v1.0 due to blocking GUI prompt issue.

### Phase 9: Session Management Overhaul (March 21–26)
- **Session Roadmap** (March 21): Comprehensive 4-phase plan for save/load/continue.
- **Key insight**: Separation of `user_history` (append-only, full transcript) vs `agent_context` (pruned/summarized for LLM).
- **Session data model**: session_id (UUID), config (immutable), runtime_params (mutable), user_history (append-only), agent_context (derived).
- **Save/Load implementation**: JSON serialization, SessionStore interface.
- **Session-Agent separation**: Clear boundaries between session persistence and agent execution.
- **ContextBuilder architecture**: Abstract base with LastNBuilder, SummaryBuilder, TurnBuilder strategies.
- **HistoryProvider**: Context building and pruning with token limit awareness.
- **Debug infrastructure**: DEBUG_CONTEXT, DEBUG_HISTORY_PROVIDER env var gates.

### Phase 10: GUI Modularization & Maturity (March 24–29)
- **Qt GUI modularization**: Split monolithic qt_gui_updated.py into qt_gui/ package with conversation_panel, input_panel, main_window, output_panel, settings_panel, thinking_indicator.
- **Agent Modularization Refactoring**: Further split agent logic — ConfigManager, SessionLifecycle, Presenter extraction.
- **Smart Scrolling**: Multiple iterations to fix scrollbar jumping during streaming output.
- **OutputPanel performance**: QListView experiment reverted to QTextEdit with performance optimization.
- **Event-driven updates**: Real-time token tracking in GUI.
- **Dead code removal**: Cleanup of imported but unused modules.

### Phase 11: Grand System Analysis (April 5)
- **Phase 1**: Session history manipulation analysis — who reads/writes user_history, data formats, ObservableList pattern.
- **Phase 2**: LLM context building deep-dive — SummaryBuilder algorithm, token estimation, edge cases.
- **Phase 3**: Token counting, summary generation flow, debugging infrastructure — TokenCounter class, model context window mapping, debug flags.

### Phase 12: Knowledge Base System (May 4–5)
- **KB Tool implementation**: 8-task Phase 1 for persistent project notebook.
- **6 KB modes**: list, read, append, update, status, search, create_domain.
- **Documentation migration**: All docs/ migrated into KB domains.
- **System assessment**: Comprehensive dead code identification across 9 architectural layers.

## 2026-08 Architecture Notes
## 2026-08-01 — Agent Core Inner Mechanics — Verified Deep-Dive (2026-08-...

## Agent Core Inner Mechanics — Verified Deep-Dive (2026-08-01)

Verified against current code (agent/core/agent.py is 1563 lines, not 1519 as older notes said; tool_executor.py 404 lines; state.py 406 lines).

**Main loop (`process_query`, agent.py:823-1384)** — a Python generator that YIELDS event dicts to the caller (controller → EventBus → bridge → WebSocket). Turn loop: `for turn in range(config.max_turns)`. Per turn:
1. stop_check → PAUSING + stopped event
2. time monitoring (`update_time_state`) → time_warning injected IMMEDIATELY into conversation (LLM sees it)
3. turn monitoring (`update_turn_state`) → turn_warning injected IMMEDIATELY
4. token check (`update_token_state`) → token_warning injected IMMEDIATELY
5. build LLM context via context_builder.build() → `_cleanup_orphaned_tool_messages`
6. rate-limit delay if active, then `llm_client.chat_completion()`
7. **LLM-reported prompt_tokens OVERWRITE the running token estimate** (ground truth; drift >5% logged as warning)
8. assistant message → TurnTransaction → `commit_assistant_only()` BEFORE yielding any event (crash-safety)
9. if tool_calls: tool_executor.execute_tool_calls() → `turn_transaction.commit()` → yield tool_call + tool_result events → flush BUFFERED token warnings (from _update_tokens_after_tool) → if final_detected (Respond) yield agent_responded + return; if summary_text (SummarizeTool) → `_apply_summary_pruning` → yield context_summarized → CONTINUE loop (agent keeps working)
10. if no tool_calls: commit turn, yield agent_responded (answer), return
11. loop exhaustion → stop_reason: max_turns_reached

**Warning injection timing (important asymmetry)**: pre-LLM time/turn/token warnings are added to conversation IMMEDIATELY (LLM can react). Token warnings generated during tool execution (`_update_tokens_after_tool`, agent.py:718-750) are BUFFERED in `_pending_warnings`/`_pending_warning_events` and flushed AFTER turn_transaction.commit() so they land chronologically after tool results.

**TurnTransaction (turn_transaction.py, 196 lines)**: buffers assistant msg + tool calls + tool results; `commit_assistant_only()` commits assistant before events; `commit()` writes everything; `rollback()` on failure. Crash-safety: data committed to user_history BEFORE any event yields.

**ToolExecutor.execute_tool_calls (tool_executor.py:87-202)**: per tool_call: (1) `state.is_tool_allowed()` gate → rejection message if denied; (2) raw args logged to ~/.thoughtmachine/logs/tool_calls_raw_debug.log (2MB capped, truncated); (3) `json.loads` → `fast_json_repair.loads` fallback; (4) tool_class lookup in filtered tool_classes; (5) `_execute_single_tool` (permission categories check via security_gate, `required_categories`, workspace capabilities); (6) tool result added via turn_transaction, tokens estimated + `update_token_func`. Respond tool → final_detected=True + respond_result (response_type/status/confidence/meta); SummarizeTool → summary_text + keep_recent_turns.

**State machine (state.py, 406 lines)**: AgentState dataclass with 5 enums: TokenState (NORMAL/WARNING/CRITICAL), TurnState, ExecutionState (READY/RUNNING/PAUSING/...), TimeState, SessionState. `update_token_state` (83-170), `update_time_state` (172-250), `update_turn_state` (252-330), `get_allowed_tools` (384-399) — restrictions_active gates tool set (SummarizeTool/Final only when CRITICAL next turn).

**Config lifecycle**: mailbox pattern — `request_config_update()` queues to `_pending_config`, applied at start of next process_query. `_can_hot_swap` (temperature/top_p/enabled_tools → hot-swap, no restart) vs full `restart()` (preserves conversation + token counts; restores old LLM client on failure). `_configs_are_identical` skips no-op updates; `_notify_config_change` posts [SYSTEM NOTIFICATION].

**Summarization (`_apply_summary_pruning`, agent.py:1385-1462)**: inserts system-role summary message at turn boundary (`_find_summary_insertion_index` via shared `group_messages_into_turns_with_indices`), appends unwarning AFTER SummarizeTool result, sets session.summary, updates token estimate, re-evaluates token state (clears restrictions), resets emergency_mode. Max summary length 20000 chars.

**Emergency recovery**: token_limit_exceeded LLMError → `context_builder.emergency_mode = True` + retry (max 2 retries) → rebuilds context slimmer; exhausted → error event + stop.

**Rate limiting**: RateLimitExceeded → exponential backoff delay (10s base × 1.2^n, cap 60s), adds rate_limit_warning event, sleep, then stop_reason: rate_limit.

**Pause**: `request_pause()` sets `_pause_requested`; checked at 3 checkpoints: [1] turn_start, [2] after_llm (DEFERRED if tool_calls pending — grace turn commits assistant without tool_calls), [3] after_turn. Transition → PAUSING + paused event.

**Event stream**: process_query yields dicts; `_add_conversation_data_to_event` stamps created_at/timestamp/seq/conversation_version/hash/tokens/turns. Event types actually yielded: user_query, stopped, token_update, time_warning, turn_warning, token_warning, turn, tool_call, tool_result, agent_responded, context_summarized, max_turns_reached/stop_reason, paused, error, rate_limit_warning. Many EventType enum entries (AGENT_START, WORKER_*, etc.) are emitted by controller/presenter, not by process_query.

## Archived
`archive_arch_a.md` (≤ 2026-07-15), `archive_arch_b.md` (2026-07-16 → 2026-07-31 + guides + roadmap).

## 2026-08-16 — AUDIT 2026-08-1x: feat/git-resource-overlay (064fa5d) Part A...

AUDIT 2026-08-1x: feat/git-resource-overlay (064fa5d) Part A COMPLETE. Core: auto-build provenance MOVED from vault (docker/resource/Dockerfile, never-overwrite trust anchor) to REPO files (resources/default_dockerfile.txt + resources/git_resource_overlay_dockerfile.txt + requirements.txt) via _ensure_resource_image (infra/resource_container_manager.py L358-526; _prepare_resource_build_context L232-273; _prepare_git_overlay_build_context L276-309; drift = thoughtmachine.build_hash label, _check_resource_image L312-332). In THIS deployment workspace==repo with filesystem=write, so "image definitions cannot be tampered from a workspace" (L85-97) is FALSE: agent-editable repo files feed auto-build (triggered GitInfoTool._resolve_resource_execution → ensure_resource("git") → _ensure_resource_image L1053) which executes RUN steps as host daemon with network. Vault copy = manual-fallback only (MANIFEST: default_dockerfile.txt→docker/resource/Dockerfile never_overwrite; overlay NOT in MANIFEST). Settings: resource containers container_registry.py create_hardened_container L162-182 via create_resource_container L366-456 (cap_drop ALL L169, security_opt L170, read_only L171, user 1000:1000 L172, network none L174/L429, mem 512m/cpu 50000/oom 500); legacy path resource_container_manager.py _create_resource_container L851-956 (/workspace rw bind L874-881 + worktree main-repo rw extra L889-900). Executor/user: docker_executor.py L658-680 (cap_drop L664, security_opt L665, read_only L666, user L667, tmpfs /workspace/.git shadow L622-624, pkg volume L640-655); container_manager.py fresh create L529-637. Agent surface: ContainerBuildTool (tools/container_control.py L492) vault-gated build_image (container_manager.py ~L1094-1195; dex.EXECUTOR_BUILD_HASH_LABEL = docker_executor.py L55); DockerCodeRunner._build_image L447-460 references docker/executor.Dockerfile (ABSENT — stale); GitInfoTool = auto-build trigger. Web UI: PUT /api/workspace/{ws_id}/dockerfile (workspace_routes.py L628-642) writes vault workspaces/<ws_id>/Dockerfile (_workspace_dir = _user_dir()/workspaces/<ws_id> workspace_capabilities.py L154-156); rebuild_container ws cmd server.py L2202-2212. Degraded: tm-resource-git MISSING → GitInfoTool broken; auto-build not yet succeeded. Minor bug: registry _ensure_resource_image_or_raise manual hint single-step omits overlay (no git in default_dockerfile.txt); correct = GIT_OVERLAY_BUILD_CMD two-step (L119-125). NEXT: Part B host commands pending. Full detail in chat transcript.
