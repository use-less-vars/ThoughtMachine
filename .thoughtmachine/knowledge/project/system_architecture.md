# System Architecture

Key architectural decisions, component relationships, and data flow patterns.

## Current Status
## Current Status
- ✅ Comprehensive architecture documentation covering all system layers
- Sections: System Assessment, Pruning & Context Management, System Notifications, Message Metadata, Security Layer, RAG System, State Machine, Session Architecture, Logging, Config System, Web UI, and more
- Last major update: 2026-05-28 (Tool Output Truncation framework, Respond tool architecture)

## Components
(To be populated)

## 2026-06-03 — ## Permission Categories (required_categories) — 2026-07-10
...

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

## 2026-07-03 — ## 2026-KB-AUDIT — Corrections to the above assessment

### ...

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

## 2026-05-06 — ## Core State Machine Simplification Plan (2026-05-06)

**Go...

## Core State Machine Simplification Plan (2026-05-06)

**Goal:** Reduce ExecutionState to 3 values (RUNNING, PAUSING, READY), remove synthetic pause events, and make the agent loop return cleanly with a `stop_reason` event.

**Overview:** A 5-phase overhaul to simplify the agent's state machine. Full spec stored in conversation history and summarized below.

**Key Changes:**
- Phase 1: ExecutionState enum reduced to RUNNING/PAUSING/READY; process_query() returns cleanly with stop_reason
- Phase 2: Controller no longer synthesizes paused events; uses stop_reason for UI feedback
- Phase 3: GUI/SessionLifecycle adapt to new states
- Phase 4: Turn limit fix becomes trivial
- Phase 5: Integration testing and regression checks

**Status:** Plan received, awaiting implementation go-ahead.

## 2026-05-06 — ## Phase 1: State Simplification Complete

The ExecutionStat...

## Phase 1: State Simplification Complete

The ExecutionState enum has been simplified to just 3 values: RUNNING, PAUSING, READY.

**Files modified across codebase:**

1. **agent/core/state.py** — ExecutionState enum reduced, AgentState default READY
2. **agent/core/agent.py** — process_query() simplified, all terminal state transitions removed, stop_reason added
3. **agent/controller/__init__.py** — Event loop simplified, synthetic paused events removed
4. **agent/presenter/event_processor.py** — All PAUSED/WAITING_FOR_USER → READY
5. **agent/presenter/session_lifecycle.py** — Default IDLE→READY, guard conditions updated
6. **agent/presenter/agent_presenter.py** — on_user_input checks for READY, differentiates via current_session
7. **agent/presenter/gui_integration.py** — Default IDLE→READY
8. **qt_gui/session_tab.py** — on_state_changed, run_agent, update_buttons all updated for READY-only model

**Key design decisions:**
- READY is the universal non-running state (replaces IDLE, PAUSED, WAITING_FOR_USER, FINALIZED, STOPPED, MAX_TURNS_REACHED)
- When user provides input in READY state: checks self.presenter.current_session to decide start vs continue
- PAUSING and STOPPING retained as transitional states for UI feedback

## 2026-05-06 — ## Turn Limit Simplification Complete (2026-05-06)

### Chan...

## Turn Limit Simplification Complete (2026-05-06)

### Changes Made
1. **agent/core/state.py**: Simplified `TurnState` enum to only `LOW`/`WARNING` (removed `CRITICAL`). `update_turn_state()` uses fixed `max_turns - 3` warning threshold with immediate `restrictions_active = True`. `get_allowed_tools()` returns only `['Final', 'FinalReport']` when restricted.

2. **agent/config/models.py**: Removed `turn_monitor_warning_threshold` and `turn_monitor_critical_threshold` from FIELD_CATEGORIES and field declarations.

3. **agent/core/agent.py**: Removed turn threshold fields from `config_data` dict in preset creation.

4. **agent/presenter/state_bridge.py**: Removed threshold entries from `direct_mappings`.

5. **qt_gui/panels/agent_controls.py**: Removed turn_monitor_row widget, turn timers, all turn-related signal connections and methods.

6. **qt_gui/session_tab.py**: Removed signal connections for removed turn monitor controls.

7. **agent/core/tool_executor.py**: Updated rejection message to remove SummarizeTool references (only Final/FinalReport available).

8. **agent_config.json**: Removed stale `turn_monitor_warning_threshold` and `turn_monitor_critical_threshold` fields.

### Verification
- Zero references to removed fields or `TurnState.CRITICAL` remain in agent/*.py or qt_gui/*.py.
- Config file cleaned of stale fields (safe via `extra='ignore'` but removed for cleanliness).

## 2026-05-07 — Phase 5: Token Counting & Output Truncation Audit completed

## Phase 5 Audit Results — Token Counting & Output Truncation

### Token Counting Architecture

**Three token counters exist, each serving different purposes:**

1. `self.state.current_conversation_tokens` — Running estimate of conversation tokens
   - Updated by: user message estimates (tiktoken), LLM response.usage.prompt_tokens (ground truth), tool result estimates (`len(str(result)) // 4`), system notification estimates
   - Overwritten each turn by `response.usage.prompt_tokens` (line 827)
   
2. `self.total_input_tokens` / `self.total_output_tokens` — Running totals (persistent across turns)
   - Property-backed: reads from session.total_input_tokens / _token_counts dict
   - Accumulated each turn: `self.total_input_tokens += input_tokens`
   
3. `token_counter.estimate_tokens()` / `estimate_request_tokens()` — Pure estimation using tiktoken
   - Used for: warning threshold checks, context calculation

### Key Findings

**A. LLM response.usage.prompt_tokens as ground truth (line 827)**
- `self.state.current_conversation_tokens = input_tokens` overwrites with actual API-counted tokens
- This is the correct approach — resets accumulated estimate drift each turn
- After overwrite, tool results and system notifications are added on top (estimates)

**B. Tool result token estimation**
- Uses `len(str(tool_result)) // 4` in tool_executor.py (lines 90, 133)
- This is a rough character-based estimate (~4 chars per token), not tiktoken
- Results in ~4x overestimation compared to tiktoken for typical results
- Called via `update_token_func` → resolves to `agent._update_tokens_after_tool`

**C. Warning injection in `_update_tokens_after_tool` (lines 549-573)**
- After adding tool_tokens, calls `state.update_token_state()` which may return warning events
- Warning messages are injected as [SYSTEM NOTIFICATION] and their token estimates are also tracked
- Works correctly but there's a minor style issue: no blank line before @property on line 574

**D. Consistency analysis — three counters compared**
| Counter | Source | Consistency |
|---------|--------|-------------|
| `current_conversation_tokens` | Mix of API ground truth + estimates | Moderate — resets to truth each turn, drifts between turns |
| `total_input_tokens` | Accumulated from API `prompt_tokens` | High — pure accumulation from API |
| `token_counter.estimate_tokens()` | Pure tiktoken estimation | High (deterministic) |

**E. Output truncation handling**
- ContextBuilder's `_truncate_to_max_tokens()` handles input context truncation (two modes)
- No explicit handling for LLM response exceeding context_window (the API enforces this via max_tokens param)
- `_get_max_context_tokens()` reserves room for response via `SAFETY_MARGIN` + `max_tokens`/`DEFAULT_RESPONSE_TOKENS`

**F. Flow per turn:**
1. User message: estimate → add to current_conversation_tokens
2. Turn warnings: estimate → add to current_conversation_tokens
3. `_update_conversation_token_estimate()` (line 520) — full re-estimate using context_builder
4. Token state: `state.update_token_state(current_conversation_tokens)` → inject warnings if needed
5. Context built: `context_builder.build(conversation, max_tokens=max_context_tokens)`
6. LLM call: `response = llm_client.chat_completion(messages)`
7. **Reset**: `current_conversation_tokens = response.usage.prompt_tokens` (ground truth)
8. Tool execution: each tool result adds `len(str(result)) // 4` via `_update_tokens_after_tool`
9. Summary pruning: full re-estimate via `_update_conversation_token_estimate()`

### Issues Found

1. **Tool token estimation is rough**: `len(str(result)) // 4` is a crude estimate that overcounts. No tiktoken fallback for tool results.
2. **Missing blank line before `@property`** in `_update_tokens_after_tool` (line 574). Not a bug but violates PEP 8.
3. **`_update_tokens_and_yield` removed**: Old code had a generator version that did full re-estimate after each tool. Current code uses `_update_tokens_after_tool` which only does a rough addition. The old version was more accurate but also more expensive.
4. **No mechanism to handle `TokenLimitExceededError`**: If the LLM response exceeds the context window, the error is caught as `ProviderError` in the provider layer, but there's no retry or context-trimming logic.


## 2026-05-09 — ## Summarization and Pruning System — Deep Audit (2025-01)

...

## Summarization and Pruning System — Deep Audit (2025-01)

### Architecture: Append-Only History + On-Demand Context Assembly
The system maintains a purely append-only `session.user_history` (all messages ever exchanged). When the LLM context is needed, `SummaryBuilder.build()` assembles a **derived view**: main system prompt + latest summary + recent turns (after summary) + system warnings. This avoids destructive edits to the canonical history.

### Key Components
- **SummaryBuilder** (session/context_builder.py:194-350): Assembles LLM context from user_history. Finds main prompt, latest summary, groups non-system messages into turns, selects turns to keep (post-summary content), truncates by max_tokens, cleans orphaned tool messages.
- **HistoryProvider** (session/history_provider.py:46-280): Orchestrates caching (_cached_context), token-limit checking (80% warning / 95% prune), and summary creation. Cache cleared by add_message(), clear_cache(), session setter, TurnTransaction.commit(), and _apply_summary_pruning().
- **_apply_summary_pruning()** (agent/core/agent.py:1037-1119): The main pruning action — computes insertion index via _find_summary_insertion_index(), creates summary_msg with metadata (pruning_keep_recent_turns, pruning_insertion_idx, pruning_discarded_msg_count), inserts it and a [SYSTEM NOTIFICATION] message, clears cache, updates tokens. Falls back to _apply_summary_pruning_fallback() if session is None.
- **ObservableList** (session/models.py:18-90): Wraps session.user_history, notifies _on_conversation_changed on any mutation.
- **Turn Grouping** (agent/core/message_utils.py): Shared utility for turn grouping. Rules: user msg starts turn, assistant-with-tool_calls starts turn, tool results attach if assistant-with-tool_calls anywhere in turn.
- **_cleanup_orphaned_tool_messages()** (session/context_builder.py:108-192): Called twice (SummaryBuilder.build + get_context_for_llm). Handles orphaned tools, non-matching IDs, incomplete sequences, duplicates.

### Token Limits
- `_get_max_context_tokens()`: context_window - 1000 (SAFETY_MARGIN) - config.max_tokens (default 4096)
- Default 8000 if no token_limit specified (LLMClient.create_context_builder)
- Default keep_turns = 5 (DEFAULT_KEEP_TURNS in history_provider.py)

### Edge Cases Noted
1. Fallback path (no session) uses MAX_SUMMARY_LENGTH=4000 vs main path 20000
2. Fallback path does NOT clear _cached_context explicitly
3. _cleanup_orphaned_tool_messages runs twice (redundant but safe)
4. Multiple summaries: only the latest (rightmost) is used; older summaries remain in history as dead metadata
5. Token truncation with summary: remove_from_end=True (newest messages removed first) — preserves original kept turns but may truncate beyond what's expected
6. Pause/error interrupts summarization flow — summary_text is only set if SummarizeTool completed successfully

## 2026-05-10 — ## Docker Executor — User Site-Packages Fix (2026-05-10)

**...

## Docker Executor — User Site-Packages Fix (2026-05-10)

**Problem**: `pip install --user` installed packages to `/home/agent/.local/lib/python3.11/site-packages/` but `ENV PYTHONNOUSERSITE=1` in `docker/executor.Dockerfile` disabled user site-packages, making imports fail silently.

**Fix**: Commented out `ENV PYTHONNOUSERSITE=1` and rebuilt the Docker image with `build=True`. Now `pip install --user <package>` works seamlessly without needing `PYTHONPATH` workarounds.

**Files changed**:
- `docker/executor.Dockerfile` — removed `ENV PYTHONNOUSERSITE=1`

## 2026-05-11 — ## Web UI Backend — Added (2025-07-16)

**Files created:**
-...

## Web UI Backend — Added (2025-07-16)

**Files created:**
- `web_ui/backend/bridge.py` — `WebAgentBridge`: Pure-Python thread-safe wrapper around Agent with start/pause/resume/stop lifecycle. Maps Agent events to frontend protocol (state_changed, tokens_updated, context_updated, conversation_changed, status_message). No Qt dependencies.
- `web_ui/backend/server.py` — FastAPI WebSocket server with `/ws` endpoint supporting commands: start_session, continue_session, pause_session, resume_session, stop_session, get_config, get_conversation, update_config. Health check at `/health`.

**Config translation:**
- Frontend sends `provider: 'openai'|'anthropic'|'local'` → translated to `provider_type: 'openai_compatible'|'anthropic'|'openai'`
- Frontend sends `tools: [{name, enabled}]` → translated to `enabled_tools: ['name1', ...]`
- Reverse translation exists for sending config back to frontend

**Protocol:** Client sends JSON commands, server streams JSON events (state_changed, tokens_updated, context_updated, conversation_changed, config_changed, status_message).

**Startup:** `python -m web_ui.backend.server` (uvicorn on :8000)

## 2026-05-11 — ## Controller-Bridge Integration (2025-07-16)

**Problem:** ...

## Controller-Bridge Integration (2025-07-16)

**Problem:** `AgentController` uses `pyqtSignal(dict)` for `event_occurred` — requires a running Qt event loop to deliver signals from the agent thread. The Web UI backend has no QApplication.

**Solution:** Added a plain-Python callback path to `AgentController`:
- `agent/controller/__init__.py`:
  - New `_event_callbacks: List[Callable]` attribute in `__init__`
  - New `set_event_callback(callback)` method to register non-Qt consumers
  - Modified `_emit_event()` to also iterate and call all registered callbacks (wrapped in try/except)
  - Qt signal emission wrapped in try/except RuntimeError for graceful fallback when no QApplication exists

- `web_ui/backend/bridge.py`:
  - New `set_controller(controller)` method — attaches an `AgentController`, registers `_on_controller_event` as the callback
  - All lifecycle methods (`start`, `pause`, `resume`, `stop`, `continue_session`, `get_config`, `get_conversation`) delegate to controller when set
  - `_on_controller_event` feeds controller events into existing `_map_and_emit` pipeline

- `web_ui/backend/server.py`:
  - Creates an `AgentController` per WebSocket connection
  - Passes it to the bridge via `bridge.set_controller(controller)`

## 2026-05-11 — ## Logging — Size-based hard pruning (2026-05-11)

Log direc...

## Logging — Size-based hard pruning (2026-05-11)

Log directory growth is now bounded by two mechanisms:
1. **Age-based**: `_cleanup_old_logs()` — removes files older than `TM_LOG_MAX_AGE_DAYS` (default 7 days)
2. **Size-based**: `_prune_logs_by_size()` — deletes oldest log files when total directory size exceeds `TM_LOG_DIR_MAX_MB` (default 50 MB)

Size pruning is invoked on logger initialization and after each file rotation. It only targets agent log files (`agent_*.jsonl*`, `agent_*.log*`), leaving non-log files untouched.

## 2026-05-11 — ## Log Rotation Refactored — Timestamp-Based Archiving

Repl...

## Log Rotation Refactored — Timestamp-Based Archiving

Replaced the old numbered-backup rotation scheme (`_rotate_log_file()`) with inline timestamp-based archiving:

- **Before**: `_rotate_log_file()` used numbered backups (`.1`, `.2`, ...`.N`) with `max_backup_files` config. Renamed files with shifting indices.
- **After**: When `_current_file_size >= max_file_size_bytes`, the current file is closed, renamed with a timestamp (`agent_<session>.jsonl.<YYYYMMDD_HHMMSS>`), and a fresh file is opened. `_prune_logs_by_size()` handles cleanup by deleting old archived files when total exceeds `MAX_LOG_DIR_SIZE_MB`.
- **Removed**: `max_backup_files` from AgentConfig model, `_AgentLogger.__init__`, `create_logger()`, agent.py config_data, and service.py fields_to_remove.
- **Removed**: Entire `_rotate_log_file()` method.
- **Kept**: `_cleanup_old_logs()` (cleans stale session dirs) and `_prune_logs_by_size()` (size-based pruning).

## 2026-05-11 — ## Web UI Config — aligned with old GUI pattern (2025-07-16)...

## Web UI Config — aligned with old GUI pattern (2025-07-16)

`web_ui/backend/server.py` now mirrors the `qt_gui/session_tab.py` `run_agent()` logic:

**Before**: Had `_default_config()` with hardcoded `system_prompt`, `enabled_tools: []`, env-var-based api_key/base_url/model. Then translated frontend format.

**After**: Removed `_default_config()`. Frontend config fields are translated (provider→provider_type, tools→enabled_tools) and passed as overrides on top of `preset_name="Default"`. The preset system handles all defaults (api_key, model, system_prompt, etc.).

**start_session vs continue_session**: Frontend's `QueryBar.jsx` already handles the distinction (IDLE/WAITING → start_session, PAUSED → continue_session), matching the old GUI's state-check logic.

## 2026-05-11 — ## ConfigPanel Engineer Reference (2025-07-16)

**Summary**:...

## ConfigPanel Engineer Reference (2025-07-16)

**Summary**: Comprehensive reference documenting AgentConfig fields, widget mappings, session store API, preset handling, and bridge gaps for rebuilding the frontend ConfigPanel.

See full report in conversation history.

## 2026-05-11 — ## Old PyQt6 GUI — Session Run Action & State Tracking

### ...

## Old PyQt6 GUI — Session Run Action & State Tracking

### 1. Run Button Handler (`run_agent()` at session_tab.py:474)
- Bound at line 270: `self.query_panel.run_btn.clicked.connect(self.run_agent)`
- Conditional logic:
  1. Checks `self.presenter.state == ExecutionState.READY`
  2. If READY **and** `self.presenter.controller.is_running` (thread alive): calls `self.presenter.continue_session(query)` — meaning an agent thread is alive after completing a prior turn
  3. If READY **and** NOT `controller.is_running` (thread dead): calls `self.presenter.start_session(query, config_dict, preset_name=preset_name)` — first run or after manual stop
- If NOT READY state: shows warning dialog "Cannot run agent in current state: {state}"

### 2. Session-Active Tracking
- **No** `_session_active` boolean exists in the GUI
- Uses two signals to derive active state:
  - `presenter.state` (ExecutionState enum: READY/RUNNING/PAUSING) — from session_lifecycle._state
  - `presenter.controller.is_running` (property at controller/__init__.py:105) — checks `_running` flag AND thread alive/dead
- `current_session_id` (Optional[str]) is also available but not used for active/inactive decisions

### 3. After-Turn State
- When agent finishes a turn: `_process_terminal_event()` in event_processor.py sets `self.session_lifecycle.state = ExecutionState.READY` (lines 148-180)
- `on_state_changed()` fires with READY → calls `update_buttons(running=False)`
- `update_buttons()` (line 534): enables Run button (`setEnabled(True)`), disables Pause button (`setEnabled(False)`)
- **Button text never changes** — always shows "RUN" (set in query_panel.py:38). No "Continue" or "New Session" text swapping.
- After manual stop (`new_session()` at line 510): same behavior — calls `update_buttons(running=False)`, Run enabled, Pause disabled
- After `stop`/`request_stop`: event_processor sets state back to READY, same button result

### 4. Session Persistence Hooks (main_window.py + session_tab.py)
- **File > Save Session As...** (line 206-208): calls `self.current_tab().save_session_as()` → session_tab.py:875
- **File > Open Session...** (line 209-211): calls `self.current_tab().open_session()` → session_tab.py:967
- **Tab close** (session_tab.py:1138 closeEvent): calls `self.presenter.save_session()` on tab close
- **Window close** (main_window.py:242 closeEvent): calls `self.save_open_sessions()` then triggers all tab closeEvents
- **Presenter methods used**:
  - `presenter.save_session()` → session_lifecycle.save_session() → session_store.save_session(session)
  - `presenter.load_session_by_id(id)` → session_lifecycle.load_session_by_id()
  - `presenter.export_session(filepath)` → session_lifecycle.export_session()
  - `presenter.list_sessions()` → session_lifecycle.list_sessions()
  - `presenter.rename_session(id, name)` → session_lifecycle.rename_session()
  - `presenter.delete_session(id)` → session_lifecycle.delete_session()
- **Auto-save**: event_processor calls `auto_save_current_session()` after every terminal event (final/stopped/max_turns/error)

## 2026-05-11 — # Multi-Tab Architecture (Refactoring)

**Date**: 2025-01-15...

# Multi-Tab Architecture (Refactoring)

**Date**: 2025-01-15

## Architecture Change
Replaced single-session architecture with multi-tab support. Each tab creates its own WebSocket connection for truly independent session interaction.

## Component Tree
```
App.jsx (hub WS for sessions list)
├── TabBar (props-driven: tabs, activeTabId, callbacks)
├── SessionTab (manages own WS + local state)
│   ├── StatusBar (props: status, tokensIn/Out, contextLength)
│   ├── ConfigPanel (props: config, sendCommand)
│   ├── ChatPanel (props: history)
│   └── QueryBar (props: sendCommand, status, isRunning, config)
└── SessionList (props: sessions, callbacks for open/delete/rename)
```

## Key Decisions
- **Per-tab WebSockets**: Each SessionTab has its own WS connection, enabling simultaneous independent sessions.
- **Hub WS**: App.jsx maintains a single "hub" WS for session list management (list, save, delete, rename).
- **Local state per tab**: SessionTab uses useState instead of Zustand for session-specific state.
- **Zustand store simplified**: Now only holds `sessions` list (shared across tabs).
- **Save via registry**: App keeps a `tabActionsRef` map (tabId -> `{sendCommand}`) so SessionList's Save button can trigger save on the active tab.
- **Tab lifecycle**: New tabs start with `sessionId=null`, which gets populated when the backend creates a session (`session_loaded` event).


## 2026-05-11 — ## Keep-All-Tabs-Mounted Fix (2025-01-15)

Changed App.jsx t...

## Keep-All-Tabs-Mounted Fix (2025-01-15)

Changed App.jsx to render ALL SessionTab components simultaneously instead of only the active one. Inactive tabs are hidden with `style={{ display: 'none' }}` on a `.tab-wrapper` div.

### Close flow (3-step)
1. User clicks X → `initiateCloseTab(tabId)` sends `close_session` over that tab's own WS
2. Backend acknowledges → `session_closed` event → SessionTab calls `onClose()`
3. App's `removeTab(tabId)` removes from `tabs` array → React unmounts → WS cleanup runs

### Key invariants
- `display: none` does NOT trigger React unmount — components stay mounted
- SessionTab's `useEffect([], [])` ties WS to component lifecycle, not visibility
- `closedRef.current` gate prevents double-close (both `session_closed` handler and WS `onclose`)
- `tabActionsRef` map survives tab switches (stored in a ref, not state)


## 2026-05-13 — ## Remove preset-based config loading from Web UI backend

T...

## Remove preset-based config loading from Web UI backend

The `WebAgentBridge.start()` method no longer accepts or uses `preset_name`. Configuration is now built from three layers:
1. **Global config** (from `agent_config.json` via `ConfigService`) — replaces preset-based defaults
2. **Session config overrides** (from a loaded session's metadata) — restores saved session config
3. **Frontend config_dict** (from WebSocket message) — runtime overrides from the UI

Provider resolution and API-key env-var fallback happen once, after all three layers are merged. The `_build_global_agent_config()` method was added to build an `AgentConfig` from `agent_config.json`, mirroring what the PyQt GUI's `create_agent_config()` does.

Files changed:
- `web_ui/backend/bridge.py`: Added `ProviderManager`/`create_agent_config_service` imports, added `_build_global_agent_config()` method, rewrote `start()` to remove `preset_name` param and preset-based system_prompt fallback
- `web_ui/backend/server.py`: Removed `preset_name="Default"` from both `bridge.start()` calls, updated comment

## 2026-05-13 — ## Config System — Hot-Swap vs Restart (2025-01-16)

### Ove...

## Config System — Hot-Swap vs Restart (2025-01-16)

### Overview

The configuration system uses a **mailbox pattern**: config changes are queued and applied at the next turn boundary (start of `process_query()`). This ensures thread-safe, atomic updates that never interrupt an agent mid-turn.

### The Mailbox Flow

```
User clicks "Apply" in GUI
    → session_tab._on_apply_runtime_params(config)
        → controller.request_config_update(config)          # Qt variant
        → bridge.send_command("update_config", config_dict)  # Web UI variant
            → agent.request_config_update(new_config)        # queues in _pending_config
                → (next turn) _apply_pending_config()
                    → _can_hot_swap(config)? YES → _hot_swap()
                                              NO  → _restart_with_config()
```

### Hot-Swap — What Actually Happens

When `_can_hot_swap()` returns `True` (meaning only HOT_SWAPPABLE fields changed), `_hot_swap()` does:

1. **Updates `runtime_params`**: Sets `self.runtime_params.temperature` and `self.runtime_params.max_tokens` — these are the params passed to the LLM on the *next* API call
2. **Replaces config reference**: `self.config = new_config` — the config object itself is swapped atomically
3. **Propagates downstream**: `self.state.config = new_config` and `self.tool_executor.config = new_config` — so token thresholds, workspace_path, etc. take effect immediately
4. **Rebuilds tools if needed**: If `enabled_tools` changed, the `ToolExecutor` is closed and recreated with the new tool set
5. **Does NOT** close the LLM client — the connection to the API remains open, the same model/provider continues serving

**Effect**: The agent thread keeps running. The next LLM call uses the new temperature/max_tokens/tool set. You don't lose conversation context, the thread doesn't restart, and you can continue chatting *instantly*.

**Fields that hot-swap**: temperature, max_tokens, max_turns, token/turn monitor settings, detail, tool_output_token_limit, enabled_tools.

### Restart — What Actually Happens

When `_can_hot_swap()` returns `False` (e.g., provider, model, system_prompt, workspace changed), `_restart_with_config()` calls:

1. **`self.restart(new_config)`** — the big restart method:
   - **Saves old references**: logger, system_event_logger, config, llm_client, tool_executor
   - **Closes old resources**: `self.llm_client.close()`, `self.tool_executor.close()`
   - **Re-initialises**: Calls `self.__init__(new_config, ...)` equivalent — creates a fresh LLM client for the new provider/model, creates a new ToolExecutor
   - **Restores preserved state**: Copies back conversation history (`self.conversation`), token counts (`total_input_tokens`, `total_output_tokens`), session reference, logger, system_event_logger from old references
   - **On failure**: Restores all old references and returns `False`

2. **Does NOT restart the Python process** — no subprocess, no fork, no reload. The agent thread continues running as the same Python object.

3. **Conversation is preserved** — all messages, token counts, and session state stay intact. The user sees no interruption.

**Fields that require restart**: provider_type, model, api_key, base_url, system_prompt, workspace_path, stop_check, provider_config, tool_classes, ALL RAG settings, ALL logging settings (GLOBAL_STATIC), kb_enabled, kb_path, provider_id, model_override.

### What the Categories Mean

| Category | Meaning | Example Fields |
|----------|---------|---------------|
| `HOT_SWAPPABLE` | Can be changed mid-session without disruption | temperature, max_tokens, enabled_tools |
| `RESTART_REQUIRED` | Needs LLM client re-creation | provider, model, system_prompt, workspace_path |
| `SESSION_IDENTITY` | Immutable per-session identity (set once at creation) | session_id, initial_conversation |
| `GLOBAL_STATIC` | Global settings that are read at startup and rarely change | log_dir, log_level, log_categories |

### Important Nuances

1. **API key validation before restart**: Before calling `_restart_with_config()`, the system checks `_has_api_key(new_config)` — if no API key is available (neither in config nor via env var), the restart is **skipped** and `_pending_config` is **preserved** for retry on next turn. This prevents bricking the agent by restarting with no credentials.

2. **Workspace path is special**: Though it's categorized as `RESTART_REQUIRED`, it's propagated to `self.state.config` and `self.tool_executor.config` during hot-swap too — so the **new workspace path is available** to downstream components immediately, even though the agent needed restart to fully switch tool contexts.

3. **`_pending_config` is never lost on error**: If anything fails (no API key, restart failure), `_pending_config` is **not cleared** — it remains for retry on the next `process_query()` turn.

4. **It does NOT restart the Docker container**: The Docker executor container has its own lifecycle (pooled containers with idle timeout). Config changes to the agent don't affect Docker containers directly.

5. **The GUI's "old" restart button was removed**: Originally there was a separate "Restart" button that was redundant — changing provider/model/workspace and clicking Apply already triggers the restart path. The restart button was removed per the config_architecture.txt plan.

## Session Architecture & Data Flow
## Session Architecture & Data Flow

### Data Model (session/models.py)

**Session**: Core dataclass holding conversation state:
- `user_history: List[Dict[str,Any]]` — Wrapped in `ObservableList` via `__post_init__()`
- `session_id`, `created_at`, `updated_at`, `runtime_params`, `metadata`
- `_conversation_changed_callbacks` — list of callbacks fired on any mutation
- `total_input_tokens`, `total_output_tokens` — cumulative token tracking
- `summary` — optional summary of earlier conversation (for pruning)
- `conversation_version`, `conversation_hash` — auto-updated on changes
- `create_agent(config)` → `Agent(config, session=self)` — ties agent to session

**ObservableList(list)**: Custom list subclass that calls `callback()` on every mutation (append, extend, __setitem__, pop, remove, clear, etc.)
- `__post_init__()` wraps `user_history` in ObservableList with `callback=self._on_conversation_changed`
- `connect_conversation_changed(callback)` registers external callbacks
- `_on_conversation_changed()` fires all registered callbacks, updates hash/version

### TurnTransaction (agent/core/turn_transaction.py)
- Atomic turn commit/rollback buffer
- `__init__(session, context_builder)` — session can be None
- `commit()` — extends `session.user_history` if session exists, else does NOTHING
- **BUG**: When session=None, assistant/tool_call/tool_result messages are silently discarded

### Agent.conversation property (agent/core/agent.py)
- Getter returns `session.user_history` if session exists, else `self._conversation`
- Setter replaces `session.user_history` in-place if session exists, else assigns to `self._conversation`

### Flow Paths

**Qt GUI Path (works correctly):**
1. `SessionLifecycle.start_session()` creates Session + calls `controller.start(query, session=session)`
2. `AgentController._run()` → `Agent(run_config, session=self._session)` — session is NOT None
3. `TurnTransaction(self.session, ...)` — session exists → commit works

**Web UI Controller Path (works correctly):**
1. `WebAgentBridge.start()` → `self._controller.start(query, config, session=session_arg)` — session passed
2. Same as Qt path from step 2

**Web UI Standalone Path (BROKEN):**
1. `WebAgentBridge.start()` → `self._agent = Agent(config)` — NO session passed
2. `Agent.__init__()` → `self._session = None`, `self._conversation = []`
3. `process_query()` → `TurnTransaction(self.session, ...)` — session is None
4. `TurnTransaction.commit()` → `if self.session:` is False → skips extending anything
5. `self._conversation` stays as `[system, user]` — no assistant/tool/tool_result messages ever added
6. Conversation stays stuck at [system, user] every turn

### Key Files:
- `session/models.py` — Session, ObservableList, RuntimeParams, ContainerMetadata
- `agent/core/turn_transaction.py` — TurnTransaction with the bug at line 90
- `agent/core/agent.py` — Agent.__init__ (line 54), conversation property (line 423), process_query (line 663), _add_to_conversation (line 535)
- `agent/core/conversation_manager.py` — ConversationManager.add_message (line 30) — has fallback when session=None (appends to conversation list directly)
- `agent/controller/__init__.py` — AgentController.start (line 124), _run (line 348), dual event path
- `agent/presenter/session_lifecycle.py` — SessionLifecycle.start_session (line 74), load_session (line 248), save/export
- `web_ui/backend/bridge.py` — WebAgentBridge.start (line 164), load_session (line 361), controller vs standalone paths
- `session/store.py` — FileSystemSessionStore for persistence
- `session/context_builder.py` — SummaryBuilder/ContextBuilder for context window management

## 2026-05-14 — ## 2026-05-15 — Logging tag namespace: server.*

Added `serv...

## 2026-05-15 — Logging tag namespace: server.*

Added `server.*` namespace for web server layer diagnostics:

- `server.config` — config translation in `web_ui/backend/server.py` (INFO: brief summary, DEBUG: full dump)
- `server.bridge` — bridge config forwarding in `web_ui/backend/bridge.py` (INFO: tools+overrides summary, DEBUG: full keys)

**Design rationale**: Two-level split per module:
- INFO: one-liner showing *what happened* (always-on under `TM_LOG_TAGS=server.*`)
- DEBUG: full diagnostics showing *all details* (opt-in with `TM_LOG_LEVEL=DEBUG`)

This lets users see operational status at a glance, then drill into detail when troubleshooting. All other migrated debug prints (controller, agent, llm) remain at DEBUG level under their existing namespaces (`core.*`, `llm.*`).

## 2026-05-14 — ## Logging system v2.2 — Runtime setters + timestamps + name...

## Logging system v2.2 — Runtime setters + timestamps + namespace cleanup

### New runtime API (in `agent/logging/unified.py`)
- **`set_log_level(level)`**: Change `CURRENT_LOG_LEVEL` at runtime. Accepts `str` or `LogLevel` enum.
- **`set_log_tags(tags)`**: Change `_LOG_TAGS` filter at runtime. Accepts comma-separated string or list of patterns.
- **`show_log_config()`**: Returns dict with `log_level`, `log_tags`, `truncation`, `env_vars` for diagnostics.

### Timestamps on console output
- Every console log line now starts with `[HH:MM:SS]` (e.g., `[14:23:01] DEBUG [core.pruning] ...`).
- Implemented via `datetime.now().strftime("[%H:%M:%S]")` in the `log()` function.

### Namespace fixes
- `docker.build` → `tools.docker_executor.build` (in `docker_executor.py`, 8 occurrences)
- `debug.unknown` → `core.turn_transaction` (in `agent/core/turn_transaction.py`, 5 occurrences)
- `**debug.unknown**` (old double-asterisk format) was also fixed as it used the same tag value.

### Exports
- `set_log_level`, `set_log_tags`, `show_log_config` exported from `agent/logging/__init__.py`
- Updated `__all__` list accordingly.

### Documentation
- `docs/logging_manual.md` updated to v2.2 with: timestamp format, runtime API docs, corrected tag table, runtime workflow examples.

## 2026-05-14 — **Session Management Architecture Update**

The WebSocket en...

**Session Management Architecture Update**

The WebSocket endpoint (`server.py`) now uses a dual pattern:

1. **Hub commands** (session listing, open sessions, delete, rename) use a standalone `FileSystemSessionStore` instance created at the start of the hub handler. These do NOT require a `WebAgentBridge`.

2. **Per-tab commands** (start_session, continue_session, load_session, save_session, etc.) use the `WebAgentBridge` which wraps a controller+agent. These commands still check `bridge is None` and return errors if no session is active.

This separation allows the hub WebSocket (App.jsx) to list sessions and auto-load open tabs without needing an active agent session.

## 2026-05-14 — ## 2026-05-15 — Auto-Load Sessions on Hub WS Connect

**What...

## 2026-05-15 — Auto-Load Sessions on Hub WS Connect

**What changed:**
- Added `loadTab` function in `App.jsx` — opens a tab for an existing session with de-duplication (reuses already-open tabs). Uses `setTimeout(0)` to avoid calling `setActiveTabId` inside `setTabs` updater.
- `open_sessions` handler now calls `loadTab(s.session_id)` instead of `handleOpenTab`
- `handleOpenTab` (called from SessionList sidebar) now delegates to `loadTab`
- `SessionTab.jsx` now tracks `currentSessionId` state, updated from `session_loaded` event
- `onRegister` exposes `getSessionId: () => currentSessionId` for parent access

**Flow:**
1. Hub WS connects → sends `get_open_sessions`
2. Server responds with `{ type: "open_sessions", sessions: [...] }`
3. `loadTab()` creates tabs with pre-set `sessionId` → sets `activeTabId`
4. Each `SessionTab` WS connects → sends `load_session` with the `sessionId`
5. Backend loads session → emits `session_loaded` with the ID
6. SessionTab updates `currentSessionId` → ready for `continue_session`

## 2026-05-16 — Dead code removal: orchestrator.py, legacy config/, agent/utils/, qml_gui/, AI_Tasks/

## Dead Code Removal (2026-05-15)

Removed the following dead/orphaned code:

- **`llm_providers/orchestrator.py`** (359 lines) — Old provider fallback/chaining orchestrator. Unused. Only imported from orphaned top-level `config/` package.
- **Top-level `config/` package** (`__init__.py`, `models.py`, `loader.py`, ~18 KB) — Legacy configuration system (`FallbackConfig`, `BudgetConfig`, `LLMConfig`, `ConfigLoader`). Superseded by `agent/config/models.py` (`AgentConfig`).
- **`agent/utils/__init__.py`** — Empty package, unused.
- **`qml_gui/`** (7 files, 52 KB) — QML-based GUI, superseded by `web_ui/` + React frontend.
- **`AI_Tasks/`** (12 files, 64 KB) — Old task documents migrated to Knowledge Base.

## 2026-05-16 — ## 2026-05-16 — Frontend migrated from top-level `frontend/`...

## 2026-05-16 — Frontend migrated from top-level `frontend/` to `web_ui/frontend/`

The React frontend (previously at `/frontend/`) has been moved to `web_ui/frontend/` to sit alongside the backend at `web_ui/backend/`. This consolidates all web UI code under a single `web_ui/` directory.

Structure:
```
web_ui/
├── backend/          # FastAPI + WebSocket server
│   ├── __init__.py
│   ├── bridge.py
│   └── server.py
├── frontend/         # Vite + React frontend
│   ├── public/
│   ├── src/
│   │   ├── components/
│   │   ├── store/
│   │   ├── App.jsx
│   │   ├── main.jsx
│   │   └── styles.css
│   ├── index.html
│   ├── package.json
│   └── vite.config.js
└── __init__.py
```

## 2026-05-16 — ## WebSocket Connection Sequencing (2026-05-16)

**Goal**: E...

## WebSocket Connection Sequencing (2026-05-16)

**Goal**: Eliminate connection storm on page load (multiple tabs hammering the server simultaneously)

**Changes**:

### App.jsx — Hub WebSocket
- Added `hubConnectingRef` (useRef) — guards against duplicate hub WS from StrictMode double-mount
- Added `hubReady` (useState) — set to `true` only after hub receives `open_sessions` event
- Hub `onclose` resets `hubReady` to `false`, preventing tab connections while hub is down

### SessionTab.jsx — Per-tab WebSockets  
- Added `tabConnectingRef` (useRef) — guards against duplicate tab WS from StrictMode
- Added `hubReady` prop — tab only connects after hub has synced sessions
- Added `staggerMs` prop — each tab delays its connection by `index * 200ms` (tab 0: 0ms, tab 1: 200ms, tab 2: 400ms...)
- `useEffect` now depends on `[hubReady, staggerMs, connectSessionWs]` instead of running once

**Result**: StrictMode double-mount only creates one WS per connection. Tabs connect sequentially (0ms, 200ms, 400ms...) only after the hub is ready. Total time from page load to all tabs connected should be under ~1.2s for 6 tabs.

## 2026-05-17 — ## API key resolution flow in Web UI bridge

**Primary**: `P...

## API key resolution flow in Web UI bridge

**Primary**: `ProviderManager().resolve_config(merged_config)` in `bridge.py:start()` looks up the `provider_id` in `~/.thoughtmachine/providers.json` and fills in `api_key`, `provider_type`, `base_url`, and `model` from the stored profile. Uses `setdefault()` so explicit frontend overrides take precedence.

**Secondary** (Agent internal): `Agent._has_api_key(config)` checks `config.api_key`, then `{provider_type}_API_KEY` env var, then `OPENAI_API_KEY` (for openai/openai_compatible).

**The bridge no longer duplicates env var logic** — it relies on `ProviderManager.resolve_config()` for stored keys and the Agent's `_has_api_key()` for env var fallback.

**Config layering** (from bottom to top):
1. Global config from `agent_config.json` via `ConfigService`
2. Session config overrides from loaded session metadata
3. Frontend `config_dict` from `start_session` WebSocket command
4. Provider profile resolution (fills api_key/base_url from stored profile)


## 2026-05-17 — ## Config & Key Handling — Full Reference (2026-05-17)

### ...

## Config & Key Handling — Full Reference (2026-05-17)

### 1. The Two Config Files

| File | Purpose | Contains keys? | Editable by |
|------|---------|----------------|-------------|
| `agent_config.json` (project root) | Factory defaults for new sessions. All settings a user might want as a starting point. | **No** — keys are stripped automatically. | User (hand‑edit) or GUI "Save as Global Default" button. |
| `~/.thoughtmachine/providers.json` (home directory) | Named provider profiles with credentials. | **Yes** — API keys live here exclusively. | GUI "Manage Profiles" dialog or hand‑edit. |

The key principle: **keys never leave `providers.json`**. No other file on disk stores them. No session file, no project file, no log.

### 2. How Keys Move Through the System (Lifecycle)

#### At app startup
1. `ProviderManager` loads `~/.thoughtmachine/providers.json` into memory.
2. The GUI populates the profile dropdown.
3. The last active profile is restored.
4. If a session is being reloaded, its `provider_id` and `model_override` are read from session metadata. The corresponding profile is loaded, and the flat fields (`provider_type`, `base_url`, `model`, `api_key`) are filled in — **in memory only**. No key is written to the session file during load.

#### When the user clicks Apply
1. The GUI calls `ProviderManager.resolve_config(profile_id, model_override)`.
2. This returns a dict: `{provider_type, api_key, base_url, model}`.
3. Those values are merged into the `AgentConfig` object that's about to be sent.
4. The `AgentConfig` is persisted to session metadata via `model_dump(exclude={'api_key'})` — so **the key is stripped** before writing to disk.
5. The `AgentConfig` object (with key still in memory) is sent to the agent mailbox.
6. The agent creates an `LLMClient` using that key. The key resides in the `LLMClient` instance in RAM, never on disk.

#### When a session is reloaded later
1. Session metadata contains `provider_id`, `model_override`, `provider_type`, `base_url`, `model` — but **no key**.
2. The GUI reads `provider_id`, loads the corresponding profile from `providers.json`, and fills the key back into the `AgentConfig` object in memory.
3. If the profile file is missing (different machine), the key is left empty and the user must reconfigure.

#### When "Save as Global Default" is used
1. The GUI builds an `AgentConfig` and calls `model_dump(exclude={'api_key'})`.
2. The resulting dict is written to `agent_config.json`. Keys are never present.
3. This action **does not** touch `providers.json`.

#### When a tab is closed
1. Only the session file is saved.
2. `agent_config.json` is **never** written on tab close.
3. `providers.json` is **never** written on tab close.

### 3. Responsibility Matrix

| Component | What it owns | What it NEVER does |
|-----------|-------------|-------------------|
| **`ProviderManager`** (`agent/config/provider_profile.py`) | Load/save `providers.json`, CRUD profiles, resolve config for a profile. | Never touches session files or global config. |
| **`AgentConfig` model** (`agent/config/models.py`) | Schema definition. `api_key` field marked `exclude=True`. | Never writes itself to disk; callers use `model_dump(exclude={'api_key'})`. |
| **`AgentControlsPanel`** (`qt_gui/panels/agent_controls.py`) | `get_config()` — builds `AgentConfig` from UI, resolves profile via `ProviderManager`. `set_config()` — populates UI from `AgentConfig`. "Save as Global Default" button handler. | Never writes `providers.json`. |
| **`ProviderSelector`** (`qt_gui/panels/provider_selector.py`) | Dropdown widget, communicates with `ProviderManager`. | No persistence. |
| **`ProviderDialog`** (`qt_gui/panels/provider_dialog.py`) | Table dialog for CRUD operations on profiles. Calls `ProviderManager` methods. | No direct file access. |
| **`SessionTab`** (`qt_gui/session_tab.py`) | `load_config()` — layered merge (global → session metadata). `_on_apply_runtime_params()` — triggers persistence. `closeEvent()` — saves session only, not global config. | Never writes global config on close. |
| **`StateBridge`** (`agent/presenter/state_bridge.py`) | Holds `current_config: AgentConfig` in memory. `create_agent_config()` — resolves profiles when building config for agent. | No longer has mutable `_config` dict. |
| **`Agent`** (`agent/core/agent.py`) | Mailbox pattern (`_pending_config`). `_can_hot_swap()` / `_hot_swap()` / `_restart_with_config()`. | Never sees `providers.json` — receives flat `AgentConfig` with key already resolved. |

### 4. Key Stripping — Where Exactly It Happens

The single mechanism is Pydantic's `Field(exclude=True)` on `AgentConfig.api_key`:

```python
api_key: str = Field(default='', exclude=True)
```

Every call to `model_dump()` anywhere in the codebase automatically drops `api_key`. The primary persistence points that use `model_dump()`:
- `_save_config_to_session()` — writes to `session.metadata['agent_config']`
- `_on_save_global_default()` — writes to `agent_config.json`
- `save_config()` (via Ctrl+S) — writes to `agent_config.json`

No special stripping code is needed anywhere. The model enforces it.

### 5. Migration from Old Sessions

Old sessions that had plain‑text `api_key` in metadata:
- On load, Pydantic will parse the old dict. If `api_key` is present, it will populate the field in memory.
- On the very next save (Apply or close), `model_dump(exclude={'api_key'})` will drop it.
- The key is lost from the session file at that point. The user must have a profile configured to supply the key on next reload.

This is by design — old keys in session files are a liability we eliminate on first rewrite.

### 6. Security Boundaries

| Boundary | Protection |
|----------|-----------|
| Git repository | `agent_config.json` contains no keys. `providers.json` is in `~/.thoughtmachine/` (outside the project root) and should be in global `.gitignore`. |
| Session files | No keys — stripped by Pydantic `exclude=True`. |
| Log files | Logging does not log the full `AgentConfig`; only specific fields are traced. `api_key` is never logged. |
| Memory | Key is present in `AgentConfig` object during runtime. Standard process memory protections apply. |
| GUI display | The profile dialog masks the key (`••••••`) with a show/hide toggle. |

### 7. Design Rationale (Why We Did This)

**Before:** API key was a plain text field in the GUI, stored in `agent_config.json` and in session files. Switching providers meant re‑typing the key manually. Keys leaked into git, session shares, and backups.

**After:**
- **Single storage location** — `~/.thoughtmachine/providers.json`. No duplication.
- **Automatic exclusion** — Pydantic field attribute ensures keys can't leak through serialization.
- **Named profiles** — switch providers with a dropdown, no re‑typing.
- **Minimal complexity** — no encryption, no OS keyring, no background threads. The threat model is accidental leakage, not targeted attack. Encryption can be added later without changing the architecture.

## 2026-05-18 — ## 2026-05-18: AgentConfig Audit — Corrected Scope

### Cont...

## 2026-05-18: AgentConfig Audit — Corrected Scope

### Context
Config engineer proposed adding logging/RAG/KB fields to AgentConfig, but they were **already present** (added by a previous developer). Proposed removing `logging_format`, `logging_file`, `logging_level` — these don't exist anywhere. Proposed adding `stop_check_enabled`/`stop_check_timeout` — these don't exist.

### Genuine gaps found
1. `max_backup_files: int = 5` — exists in `agent_config.json` but missing from `AgentConfig` model
2. `create_agent_config_service()` in `agent/config/service.py` strips model fields and injects non-model field names (`warning_threshold`, `critical_threshold`, `tool_output_limit`) — creates a parallel config representation
3. `model_dump()` calls lack `exclude={'api_key'}, exclude_none=True` — causes null propagation in session metadata
4. No guardrails against stray keys in write paths
5. No load-time null backfill from global config

### Corrected plan (being executed)
1. Add `max_backup_files` to AgentConfig model + FIELD_CATEGORIES
2. Fix `create_agent_config_service()` to stop stripping/injecting fields
3. Change all `model_dump()` calls to `exclude={'api_key'}, exclude_none=True`
4. Add stray-keys assertion after every save-write
5. Add load-time null backfill from global config in session lifecycle

## 2026-05-18 — ## Config Variable Elimination — Complete

The `_loaded_conf...

## Config Variable Elimination — Complete

The `_loaded_config_overrides` intermediate storage has been fully eliminated from bridge.py, simplifying config flow:

**Old flow (3 paths):**
1. `apply_config()` → writes to both `_loaded_config_overrides` AND `_config`
2. `load_session()` → writes to `_loaded_config_overrides` (not `_config`)
3. `start()` → merges `_loaded_config_overrides` into `merged_config`

**New flow (1 path):**
1. `apply_config()` → writes directly to `_config`
2. `load_session()` → writes `agent_config_raw` directly into `_config` via `validate_config()`
3. `start()` → uses `self._config` directly (no merge needed)

**Backend server.py changes:**
- `get_config` handler now uses `_frontend_config_from_bridge(bridge)` instead of `_config_to_dict(cfg)` — consistently converts backend AgentConfig fields (provider_type, enabled_tools) to frontend format (provider, tools).
- Dead `_overrides_to_frontend_config()` function deleted.

**Frontend changes:**
- `SessionTab.jsx`: `INITIAL_STATE.config` set to `null` (no more hardcoded defaults)
- `SessionTab.jsx`: `config_changed` handler uses direct assignment (`update({ config: msg.config })`) instead of merging — backend is the single source of truth
- `ConfigPanel.jsx`: handles null config safely (`config ?? {}`)
- `QueryBar.jsx`: handles null config in `start_session` (`config: config ?? {}`)

## 2026-05-19 — ## 2026-05-19: Incremental Assistant Message Commit

**Task ...

## 2026-05-19: Incremental Assistant Message Commit

**Task 4 — Commit Flow Change**

Modified TurnTransaction to support partial (assistant-only) commit:

- Added `_assistant_committed` flag to track whether assistant message was committed separately.
- Added `commit_assistant_only()` method: commits just the assistant message to user_history immediately, clears context builder cache, keeps transaction open for tool results.
- Modified `commit()`: when `_assistant_committed` is True, only tool results in `_tool_calls_buffer` are committed; otherwise full atomic commit as before.

In `agent.py` `process_query()` loop:
- Before `execute_tool_calls()`, calls `turn_transaction.commit_assistant_only()` so the assistant message is visible in user_history before tool execution begins.
- Subsequent `turn_transaction.commit()` after tool execution commits only tool results.

This prevents assistant message loss if tool execution triggers pause/interrupt.

## 2026-05-19: Session-Load Orphan Tool Message Cleanup

**Task 5 — Session Load Flow Change**

In `session_lifecycle.py`:

- Added import: `from session.context_builder import ContextBuilder`
- In `load_session()`: after creating/binding the session, calls `ContextBuilder._cleanup_orphaned_tool_messages()` on `session.user_history` to remove any orphaned tool messages that may have been persisted.
- In `load_session_by_id()`: same cleanup after loading session from store.
- Both log a WARNING when orphaned messages are found and cleaned.

## 2026-05-19 — ## 2026-05-19: Config Pipeline Clarification — Qt Desktop Ap...

## 2026-05-19: Config Pipeline Clarification — Qt Desktop App, Not Web UI

The codebase at `/home/jojo/PycharmProjects/ThoughtMachine-dev` is a **PyQt6 desktop application**, not a web-based UI. There is no `web_ui/backend/server.py`, no `ConfigPanel.jsx`, no WebSocket protocol, and no `_translate_frontend_config` / `_frontend_config_from_bridge` translation layer.

**Config flow:**
1. `AgentControlsPanel.get_config()` → returns `AgentConfig` (Pydantic model)
2. `AgentControlsPanel.apply_to_agent_requested.emit(config)` → pyqtSignal
3. `SessionTab._on_apply_runtime_params(config)` → `controller.request_config_update(config)`
4. Agent internally processes the config update

**Key difference from web UI:**
- No field name translation needed — frontend and model use the same field names
- Config changes flow as `AgentConfig` objects, not raw dicts
- Error handling is via logging + system messages in chat, not `status_message` events
- The "Apply" button is always active (desktop app, no connection state)

## 2026-05-20 — Web UI Config Pipeline — Cleanup & New Fields

## Web UI Config Pipeline — Cleanup & New Fields (2026-05-20)

**Changes made to align Web UI with the cleaned-up AgentConfig model:**

### Removed
- `context_length` from:
  - `server.py` `_default_frontend_config()` defaults
  - `bridge.py` `apply_config()` validation loop
  - `ConfigPanel.jsx` `getSafeDraft()` and General tab input

### Added to `_default_frontend_config()` (server.py)
- `token_monitor_enabled: True`
- `token_monitor_warning_threshold: 35000`
- `token_monitor_critical_threshold: 50000`
- `workspace_path: ""` (replaces context_length slot in General tab)

### New `_load_global_defaults()` helper (server.py)
- Loads `~/.thoughtmachine/agent_config.json` if it exists
- Merges its values into `_default_frontend_config()` fallback, allowing file overrides
- Parses JSON, logs warning on failure

### Token monitor validation (bridge.py)
- Removed `context_length` from positive-integer validation
- Added validation for `token_monitor_warning_threshold` and `token_monitor_critical_threshold` (must be non-negative int)

### Frontend (ConfigPanel.jsx)
- General tab: Workspace Path text input replaces old Context Length number input
- Advanced tab: Token Monitor checkbox + Warning/Critical threshold number inputs

### System notification flag fix (server.py)
- `get_conversation` handler now preserves `is_system_notification` flag on page reload

## 2026-05-20 — ## 2026-05-20 — Agent Core Stabilization — Engineer's Final ...

## 2026-05-20 — Agent Core Stabilization — Engineer's Final Report

A comprehensive stabilization push resolved 15 critical issues in the agent core. The system is now in a significantly more reliable state.

### Key Architectural Decisions Enforced

**1. Notification Visibility in Turn Grouping**
System notifications (is_system_notification=True) must remain inside their parent turns during message grouping — not stripped out or counted as separate turns. Both `_group_messages_into_turns()` variants (context_builder.py, message_utils.py) now include all notification roles alongside user/assistant/tool.

**2. Temporal Ordering via Pending Warnings**
Token warnings are now buffered into `_pending_warnings` and flushed at the start of the *next* turn (after the pruner has run). This prevents mid-turn warning injection that could disrupt context assembly or confuse the agent mid-thought.

**3. Deduplication of Critical Warnings**
`last_token_warning_state` is now updated for ALL states including CRITICAL (removed the `if new_state != TokenState.CRITICAL:` guard). This prevents duplicate CRITICAL warnings from being emitted across multiple call sites.

**4. Error Path Cache Invalidation**
Three error paths (RateLimitExceeded, ProviderError/LLMError, Unexpected Exception) now add proper [SYSTEM NOTIFICATION] messages to the conversation AND clear the HistoryProvider's `_cached_context` so the error is reflected in the next LLM context build.

**5. Fallback Summary Pruning**
The fallback path (`_apply_summary_pruning_fallback` for no-session mode) now clears `_cached_context` explicitly and respects `MAX_SUMMARY_LENGTH=20000` consistently with the main path.

**6. Dynamic Tool Rejection Messages**
`ToolExecutor` rejection messages now dynamically list the actual allowed tools from `state.get_allowed_tools()` — no more hardcoded lists that could be out of sync.

**7. Unified Turn Grouping Logic**
Turn grouping extracted from context_builder.py into `agent/core/message_utils.py` as a shared utility. Rules: user message starts turn, assistant-with-tool_calls starts turn, tool results attach to nearest prior assistant-with-tool_calls within same turn. This eliminates duplication between context_builder.py and summarization.py.

**8. Tiktoken-Based Tool Token Counting**
Replaced `len(str(tool_result)) // 4` crude estimation with proper tiktoken encoding in tool_executor.py (via `agent.token_counter.estimate_tokens(tool_result)` with //4 fallback if agent is None).

**9. Truncation Awareness**
When the pruner truncates tool outputs, it now injects a "[SYSTEM NOTIFICATION: Tool output was truncated]" message into the conversation, making the agent aware that content was dropped.

**10. `is_busy` Property on Controller**
Added `controller.is_busy` property — returns True if agent thread is alive AND processing (not just idling between turns). Used by GUI for accurate run/continue routing.

**11. Incremental Tool Visibility (commit_assistant_only)**
TurnTransaction now supports `commit_assistant_only()` — commits the assistant message to user_history BEFORE tool execution begins. This prevents assistant message loss if a pause/interrupt occurs during tool execution. Tool results are committed separately via the subsequent `commit()` call.

**12. Orphan Cleanup on Session Load**
Both `load_session()` and `load_session_by_id()` in session_lifecycle.py now call `ContextBuilder._cleanup_orphaned_tool_messages()` after loading a session to remove any orphaned tool messages that may have been persisted.

**13. GUI Notification Pipeline Restored (Path A Re-enabled)**
EventProcessor had disabled the main-thread `emit_conversation_changed()` signal (Path A), leaving only the fragile ObservableList callback (Path B). Path A is now re-enabled: `self.gui_integration.emit_conversation_changed()` runs for all non-token-update events, providing a robust dual-path notification mechanism.

**14. Commit-Before-Yield Ordering Fixed**
In agent.py, `turn_transaction.commit_assistant_only()` is now called BEFORE `yield turn_event`. Previously, the turn event was yielded before data was committed, causing GUI handlers checking `conversation_version` to find no change.

**15. ObservableList Destruction Prevented (__setattr__ Guardrail)**
Plain list assignment to `session.user_history` (e.g., in session_lifecycle.py's orphan cleanup) bypassed the ObservableList wrapping, severing the callback chain. Fixed: (a) changed assignments to slice assignment `session.user_history[:] = ...` (in-place mutation), and (b) added `__setattr__` override to Session dataclass that automatically wraps any plain list assigned to `user_history` into an ObservableList with the correct callback.

### Current State After Stabilization
- Dual-path GUI notification (Path A: main-thread signal + Path B: ObservableList callback) provides redundancy
- Turn data is committed to user_history BEFORE yielding events — no more data races
- Session loading properly preserves ObservableList integrity
- Token warnings are deferred, deduplicated, and accurate
- Error paths leave proper trail in conversation history
- The system handles pause/resume, session load, and multi-turn conversations reliably

## 2026-05-21 — ## Dual-Stream Bridge (Agent State → GUI Wiring)

### Overvi...

## Dual-Stream Bridge (Agent State → GUI Wiring)
## Dual-Stream Bridge (Agent State → GUI Wiring)

### Overview
The "Dual-Stream Bridge" refers to two parallel event delivery paths from AgentController to the GUI/presenter:

**Path A — Event Queue + Callbacks (primary):**
- `AgentController._emit_event()` puts events into `self.event_queue` and calls plain Python callbacks registered via `set_event_callback()`
- The event processor (`event_processor.py`) consumes events from the queue and dispatches them to the GUI via `gui_integration` signals (state_changed, conversation_changed, etc.)
- This path works without Qt — used by Web UI bridge
- No Qt signals are used in AgentController itself

**Path B — Event Queue (direct consumption):**
- The WebSocket bridge (`ws_bridge.py`) polls `controller.event_queue` to forward events to WebSocket clients
- Each event gets `session_id` injected before being queued

### Event Flow
1. Agent background thread (`_run()`) calls `agent.process_query(query)` which yields events
2. Each event goes through `_emit_event(event)` 
3. `session_id` is injected into each event
4. Event is placed in the event queue and dispatched to all registered plain callbacks
5. Presenter's event processor reads from the queue and triggers `gui_integration` signals for UI updates

## 2026-05-21 — System Notification Injection Points & Dual-Stream Convergence

## System Notification Injection Points

### 9 injection points across 2 files:

**session/context_builder.py (1):**
- `_truncate_to_max_tokens()` line 481 — "Context truncated: X older message(s) removed"

**agent/core/agent.py (8):**
- `_update_tokens_after_tool()` line 581 — Token warning (buffered)
- run loop line 767 — Turn warning (direct `_add_to_conversation`)
- run loop line 781 — Token warning (direct `_add_to_conversation`)
- run loop line 882 — Rate limit exceeded
- run loop line 897 — ProviderError
- run loop line 910 — UnexpectedError
- `summarize_and_prune()` line 1141 — "Context has been summarized" (session path)
- `summarize_and_prune()` line 1190 — "Context has been summarized" (fallback path)

### Dual-Stream Convergence (Path A + Path B)

- **Path A**: Agent events → AgentController._emit_event() → pyqtSignal (queued to main thread) → EventProcessor.process_event() → gui_integration.emit_conversation_changed()
- **Path B**: Agent._add_to_conversation() → session.user_history (ObservableList) → _notify() → Session._on_conversation_changed() → registered callbacks → display_conversation_from_history()
- Both converge at GUI layer. Path A is main-thread-safe signal, Path B is worker-thread ObservableList callback.
- Key convergence: EventProcessor line 55-56 explicitly marks "Path A" with logging. Error/pause handlers explicitly call emit_conversation_changed() as fallback when Path B unreliable.


## 2026-05-22 — Message(dict) class with derived is_system_notification property

## Message Class — `agent/core/message.py`

Created a `Message(dict)` subclass that derives `is_system_notification` from `role` and `content`. The flag is computed on-the-fly: `True` iff `role == 'user'` and `content` starts with `'[SYSTEM NOTIFICATION]'`. This eliminates the need to manually set `is_system_notification: True` at 16 injection sites across agent.py, agent_presenter.py, and context_builder.py.

Key design decisions:
- Inherits from `dict` for JSON serializability and backward compatibility
- Overrides `__getitem__`, `__setitem__`, `get`, `__contains__`, `pop`, `__delitem__` to make the key read-only
- Setting `msg['is_system_notification']` logs a warning and is ignored
- Provides `to_dict()` which returns a plain dict with the derived flag included
- Provides `from_dict()` class method for creating from existing dicts
- Empty Message objects (no role/content) have `is_system_notification = False`
- The validation in `_add_to_conversation` still works for both Message objects and plain dicts

## 2026-05-22 — Controller API unification: added set_session(), update_config(), process_query() — deprecated start() and continue_session()

## Controller API Unification (Task 3)

Three new methods added to `AgentController` in `agent/controller/__init__.py`:

1. **`set_session(session, config)`** — Stores session and AgentConfig for later use by `process_query()`. Also clears `_agent_override` so `_run()` falls through to the config-based agent creation path.

2. **`update_config(config)`** — Sets a pending config update. If agent exists, forwards via mailbox pattern (`agent.request_config_update()`). Always stores the config for next thread start.

3. **`process_query(query)`** — Unified entry point handling three scenarios:
   - **No agent**: Starts new thread (resets events, queues query, spawns thread)
   - **Thread alive**: Resumes agent + queues query (equivalent to old continue_session)
   - **Thread dead**: Cleans up dead state, restarts fresh

`start()` and `continue_session()` are now deprecated wrappers with WARNING-level deprecation logs, pointing users to `set_session() + process_query()`.

## 2026-05-25 — ## 2026-06-01 — Architectural Issues Investigation

### Issu...

## 2026-06-01 — Architectural Issues Investigation

### Issue 1: Tool Output Truncation at Framework Level (NOT enforced)

**Current design (broken):**
- Every tool implements `self._truncate_output()` voluntarily — OPT-IN, easy to miss
- KnowledgeBaseTool (tools/knowledge_base.py, 679 lines) NEVER calls `_truncate_output()` — returns raw strings from all 9 mode methods directly
- ToolExecutor._execute_single_tool() just calls `tool_class(**tool_args).execute()` and stores the result — NO truncation enforcement
- ToolBase._truncate_output() at tools/base.py:192 uses `self.token_limit` (default None = no limit)
- token_limit is populated from config.tool_output_token_limit (default 10,000) at tool_executor.py:166

**Tools verified to use _truncate_output:** FileEditor, FilePreviewTool, DirectoryTreeTool, DateTimeTool, FileSummaryTool, GitInfoTool, CodeModifier, Thought, RequestUserInteraction
**Tools that MAY bypass it:** KnowledgeBaseTool (confirmed), SearchCodebaseTool (needs check), PaginateTool, MCP tools, DockerCodeRunner, FieldViewer

**Root cause:** ToolExecutor at agent/core/tool_executor.py:116 calls tool.execute() and stores the result raw. All truncation happens inside individual tools' execute() methods, not at the framework level.

**Fix needed:** Move truncation enforcement into ToolExecutor.execute_tool_calls() AFTER the tool executes, wrapping the result string in a post-execution truncation pass.

### Issue 2: Context Builder Truncation — Wrong Mechanism

**Current design (broken):**
1. **Wrong max_tokens source** (agent/core/agent.py:627-635):
   - Uses `model_context_window - SAFETY_MARGIN(1000) - max_tokens/DEFAULT_RESPONSE_TOKENS(4096)`
   - Model context window comes from a hardcoded dict in token_counter.py:86
   - Ignores user's configured token_monitor_critical_threshold (default 50,000)

2. **Wrong truncation direction after summarization** (session/context_builder.py:315):
   - Line 315: `remove_from_end=summary_msg is not None`
   - When a summary EXISTS, it removes the NEWEST messages first — DROPPING THE CURRENT TURN
   - When no summary exists, it correctly removes from BEGINNING (oldest)
   - The design intent (comment line 207-208): "preserve originally-kept turns" — but this is WRONG because the current turn is what the LLM needs

3. **No emergency recovery** — if truncation can't get under limit, there's no fallback

**Correct design needed:**
- Use user's configured critical token limit (token_monitor_critical_threshold) as the max for context
- Always truncate from OLDEST messages first (remove_from_end=False always)
- When a summary exists, drop summary content/preserved turns before dropping current turn
- Add an emergency recovery: if removal from oldest can't get under limit, generate a forced summary and restart

## 2026-05-25 — ## 2026-06-01 — MCP Registration Made Non-Blocking

- **Prob...

## 2026-06-01 — MCP Registration Made Non-Blocking

- **Problem:** `register_mcp_tools(timeout=5.0)` blocked agent startup for up to 5 seconds. Three cascading timeouts: agent → ThreadPoolExecutor (5s) → per-request queue.get (5s). Orphaned subprocesses on timeout.
- **Fix:** Made MCP registration fully asynchronous:
  - `tools/mcp_manager.py:register_mcp_tools()` now checks for `mcp_config.json` first — if absent, returns instantly (no thread, no delay)
  - If config exists, spawns a **daemon background thread** that handles server startup and tool registration
  - Background thread also calls `_update_simplified_toolset()` after registration
  - Removed `import concurrent.futures` dependency, replaced with `threading.Thread`
  - `agent/core/agent.py`: simplified to just call `register_mcp_tools()` without timeout, no sync steps
- **Impact:** App start is never delayed by MCP. MCP tools become available asynchronously after registration completes.

## 2026-05-26 — ## Removed `token_monitor_enabled` and `max_tokens` config p...

## Removed `token_monitor_enabled` and `max_tokens` config params (2025-07-17)

### `token_monitor_enabled` → always-on
The token monitor checkbox was removed. Token monitoring is now always active. Removed from:
- Config model (agent/config/models.py), state logic (agent/core/state.py), agent config passthrough (agent/core/agent.py)
- UI bindings (agent/presenter/state_bridge.py, web_ui/backend/server.py)
- Frontends: ConfigPanel.jsx (removed checkbox), agent_controls.py (removed checkbox, update method, layout, config read/write)

### `max_tokens` → removed as config param
The LLM response token limit was removed as a user-facing config. Removed from:
- Config model (agent/config/models.py), RuntimeParams dataclass (session/models.py), agent RuntimeParams/chat_kwargs (agent/core/agent.py)
- Provider creation (agent/core/llm_client.py, llm_providers/factory.py)
- Provider internals: openai_compatible.py (no longer sets from config), anthropic_provider.py (uses hardcoded 4096 fallback)
- ProviderConfig dataclass (llm_providers/base.py) — removed max_tokens field
- Web UI validation (web_ui/backend/bridge.py) — removed from apply_config validation
- Docstrings/tooltips updated

`max_tokens` still exists in the context builder (session/context_builder.py, session/history_provider.py) for context window truncation — that is a separate concern.

## 2026-05-26 — **Provider Management (Web UI)**

The Web UI now supports fu...

**Provider Management (Web UI)**

The Web UI now supports full provider CRUD via the Model tab in ConfigPanel:
- **Backend**: `save_provider` and `delete_provider` WS commands in `server.py` use `ProviderManager.add_profile()/.delete_profile()` to manage `~/.thoughtmachine/providers.json`
- **Frontend**: `ManageProvidersModal` (table + Add/Edit/Delete) and `ProviderEditModal` (form with ID, Label, Type, Base URL, API Key, Default Model, Models list, Timeout)
- **Data flow**: ManageProvidersModal calls `sendCommand('save_provider'/'delete_provider')` → backend persists + broadcasts `providers_list` → SessionTab updates providers state → ConfigPanel re-renders dropdowns
- API key is stored in providers.json; masked in UI with `type="password"`

## 2026-05-26 — ## Config-failure notification chain (traced 2025-05-26)

Th...

## Config-failure notification chain (traced 2025-05-26)

The full notification chain for config-failure errors flows through 6 layers:

### Layer 1 — Agent (agent/core/agent.py)
- Line 717: Creates `Message(role='user', content='[SYSTEM NOTIFICATION] ...', is_system_notification=True)`
- Line 718: Calls `_add_to_conversation(notif_msg)` 
- `_add_to_conversation` → `conversation_manager.add_message()` → appends to `session.user_history`
- Yields `{'type': 'error', 'error_type': 'invalid_config', 'message': ..., 'stop_reason': 'error'}`

### Layer 2 — ObservableList → Session version bump (session/models.py)
- `ObservableList.append()` → `_notify()` → `Session._on_conversation_changed()`
- Bumps `_conversation_version`, recomputes `conversation_hash`, fires all registered callbacks

### Layer 3 — Controller → Bridge (agent/controller/__init__.py → web_ui/backend/bridge.py)
- Controller's `_run()` loop receives the event → calls `_emit_event(event)` → bridge's `_on_controller_event()`
- Bridge's `_on_controller_event()` calls `_map_and_emit(raw_event)`

### Layer 4 — Bridge event mapping (web_ui/backend/bridge.py lines 833-940)
- **Step 1** (lines 847-854): Checks `conversation_version` difference → if message was added, emits `{"type": "conversation_changed", "messages": normalized_history}`
- **Step 2** (lines 927-940): For `error` type specifically:
  - Emits `{"type": "status_message", "text": "⚠ Error: ..."}` 
  - Also syncs conversation_changed again (redundant but harmless)
- `_normalize_for_frontend()` (line 462): Copies `is_system_notification` from backend messages; also has prefix-based fallback check

### Layer 5 — WebSocket delivery (web_ui/backend/server.py)
- `_emit()` → `self._event_callback(event)` → server.py's `event_callback()` closure
- `event_callback`: `asyncio.run_coroutine_threadsafe(send_event(event), _loop)`
- `send_event`: `ws.send_json(event)` — sends JSON over WebSocket

### Layer 6 — Frontend handling (web_ui/frontend/src/components/SessionTab.jsx)
- `conversation_changed` case (line 278): Replaces `history` with server messages (which include the system notification as `role: 'user', is_system_notification: true`)
- `status_message` case (line 299): Appends `{role: 'system', content: '⚠ Error: ...', is_system_notification: true}` to history

### Key finding: duplicate notification
The notification appears **twice** in the frontend history:
1. Once as a `user`-role message with `is_system_notification: true` (from `conversation_changed`)
2. Once as a `system`-role message (from `status_message`)
This is because `_add_to_conversation()` adds the message to the session (triggering `conversation_changed`), AND the error handler in `_map_and_emit()` also emits a separate `status_message`. The duplication is visible in the chat UI.

## 2026-05-26 — Session naming lifecycle documented: ensure_name(), new_session missing name in response

## Session Naming Lifecycle

### Summary
Session names are auto-generated by `Session.__post_init__()` calling `ensure_name()` which creates names in format `"Session YYYY-MM-DD HH:MM"` using the session's `created_at` timestamp.

### Key Findings
- `ensure_name()` is called on EVERY `Session()` construction (via `__post_init__`), but only sets name if `metadata['name']` is empty/missing
- New sessions get proper names internally, but the `new_session` WebSocket response (`session_loaded`) only sends `session_id` (UUID), NOT the `name` field — this is likely why the frontend shows a "hash" on creation
- Sessions are NOT saved to disk on creation; they're only saved via explicit `save_session`, `apply_config`, or `close_session` commands
- On page reload, `get_open_sessions` returns the proper display name from session metadata (loaded from disk)
- The name changes from "hash" (UUID) to "Session YYYY-MM-DD HH:MM" after reload because `get_open_sessions` includes the `name` field

### Fix
Add `name` to the `session_loaded` response in `new_session` handler at `server.py` line ~696:
```python
await ws.send_json({
    "type": "session_loaded",
    "session_id": new_session.session_id,
    "name": new_session.metadata.get('name', 'Session'),
})
```

## 2026-05-27 — ## Save-before-switch in server.py

Added auto-save before b...

## Save-before-switch in server.py

Added auto-save before bridge replacement in `new_session` and `load_session` WebSocket handlers (server.py). Pattern: `if bridge is not None and bridge.session is not None: bridge.save_session()` before `bridge.stop()`.

Added `@property session` to `WebAgentBridge` that returns `self._session or self._loaded_session`, providing a clean public API for accessing the active session without reaching into private attributes.

Note: `close_session` already had save logic inside `bridge.close_session()`, so no change needed. `continue_session` does not replace the bridge, so no change needed there.

## Session panel CSS widened

Changed `.session-list-panel` width from 320px → 400px (min-width 260px → 300px) and `.session-sidebar.open` width from 320px → 400px in `styles.css`.

## 2026-05-27 — ## Batch 4: Session Actions Panel & UI Cleanup

### Changes ...

## Batch 4: Session Actions Panel & UI Cleanup

### Changes Made
1. **TabBar.jsx** — Removed save button, save dots, dirty state tracking, savingTabs state. Added cogwheel (⚙️) button with `onCogwheelClick` prop for opening the Session Actions panel.
2. **SessionTab.jsx** — Removed `versionRef`, `hasUnsavedChanges`, `onDirtyChange` prop, dirty-tracking logic in `conversation_changed` handler, and `session_saved` dirty reset. Kept `onSessionSaved` callback forwarding.
3. **App.jsx** — Removed `dirtyStates`, `handleDirtyChange`, `handleSaveTab`, `handleSaveActiveTab` functions and all prop passing. Added `sessionPanelOpen` state, `handleToggleSessionPanel`, `handleOpenSessionFromPanel`, `handleDeleteFromPanel` callbacks. Renders `SessionActionsPanel` conditionally.
4. **SessionList.jsx** — Removed `onSave` and `saveEnabled` props and the "Save Current" button.
5. **SessionActionsPanel.jsx** (NEW) — Slide-in panel with: Save As… (with name input dialog), Delete Session (with two-step confirmation), and Saved Sessions list (sorted by updated_at, click to open).
6. **styles.css** — Removed `.tab-save-dot`, `.tab-save-btn`, `.tab-save-pulse`, `.btn-save` styles. Added `.tab-cogwheel-btn`, panel styles (`.session-actions-*`), and button variants (`.btn-accent`, `.btn-danger`).

### Architecture
- Save button and dirty dots removed from tab bar (auto-save handles persistence).
- Cogwheel icon opens a slide-in panel from right side (z-index 201).
- Panel provides Save As (rename+save), Delete (with confirm), and quick session switching.
- After delete, the panel closes automatically.


## 2026-05-27 — ## Tool Output Truncation — Framework-Level with Opt-Out (Im...

## Tool Output Truncation — Framework-Level with Opt-Out (Implemented 2025-07-16)

**Problem:** Previously, output truncation was opt-in — each tool voluntarily called `_truncate_output()`. 8 out of 12 tools didn't, meaning their output was never truncated.

**Solution:** Moved truncation to the framework level in `ToolExecutor._execute_single_tool()`. After `tool_instance.execute()`, the result is automatically truncated unless the tool opts out.

**Changes made:**
1. **`tools/base.py`** — Added `skip_output_truncation: ClassVar[bool] = False` on `ToolBase`. Also added an early return in `_truncate_output()` if `self.skip_output_truncation` is True.
2. **`agent/core/tool_executor.py`** — After `tool_result = tool_instance.execute()`, added: `if not tool_instance.skip_output_truncation: tool_result = tool_instance._truncate_output(tool_result)`
3. **Opt-out tools** — Set `skip_output_truncation: ClassVar[bool] = True` on `Final`, `FinalReport`, `SummarizeTool`, `RequestUserInteraction`. Also removed the manual `_truncate_output` call from `RequestUserInteraction.execute()`.

## 2026-05-27 — ## Batch 5 — Tool Output Token Limit Config UI + Save as Def...

## Batch 5 — Tool Output Token Limit Config UI + Save as Default Config (2026-05-27)

### Changes Made

**1. web_ui/frontend/src/components/ConfigPanel.jsx**
- Added `tool_output_token_limit` field to `getSafeDraft()`, defaulting to 10000
- Added dirty tracking via `lastAppliedConfig` state:
  - `lastAppliedConfig` stores a deep-clone of the last successfully applied config
  - `isDirty` derived state compares current draft vs lastAppliedConfig
  - `isApplying` boolean for button feedback during save
  - `applyError` string for timeout-based error feedback (10s timeout)
- Added `<label>` and `<input type="number">` for `tool_output_token_limit` in the Tools tab section
- Apply button: disabled when not dirty, shows spinner + "Applying…" when applying, shows "Unsaved changes" indicator
- `lastAppliedConfig` is reset whenever new config arrives from server (inside the `config` useEffect)

**2. web_ui/frontend/src/components/SessionActionsPanel.jsx**
- Added `onSaveAsDefault` callback prop
- Added "Save as Default Config" button with optimistic feedback:
  - On click, calls `onSaveAsDefault?.()` and sets `defaultSaved` state to true
  - Shows checkmark feedback for 2.5 seconds before reverting

**3. web_ui/frontend/src/App.jsx**
- Added `onSaveAsDefault` prop to `<SessionActionsPanel />`:
  - Finds the active tab entry from `tabs` by `activeSessionId`
  - Sends `set_default_config` command via the tab's WebSocket actions

**4. web_ui/backend/server.py**
- Added `set_default_config` WebSocket command handler:
  - Reads the active session's config from `bridge._config`
  - Creates `~/.thoughtmachine/` directory if needed
  - Serializes config to JSON and atomically writes to `~/.thoughtmachine/agent_config.json`
  - Sends `{type: "default_config_saved", status: "ok"}` on success
  - Sends `{type: "default_config_saved", status: "error", message: ...}` on failure

**5. web_ui/frontend/src/styles.css**
- Added `@keyframes config-spin` animation and `.config-spinner` CSS class


## 2026-05-27 — ## Phase B Complete — Legacy Tools Replaced with Unified Res...

## Phase B Complete — Legacy Tools Replaced with Unified Respond

**Date:** 2025-05-27

**Summary:** 
- Deleted `tools/final.py`, `tools/final_report.py`, `tools/request_user_interaction.py`
- Updated `tools/__init__.py` — removed Final/FinalReport/RequestUserInteraction registrations
- Updated `agent/core/tool_executor.py` — removed imports, switched isinstance checks to Respond
- Updated `agent/core/agent.py` — removed Final and RequestUserInteraction imports
- Updated `agent/core/turn_transaction.py` — switched check from old tools to 'Respond'
- Updated `agent/core/state.py` — get_allowed_tools() now returns ['Respond', 'SummarizeTool']
- Updated `web_ui/backend/bridge.py` — FINAL_TOOL_NAMES now only {"Respond"}
- Updated `agent_config.json` — replaced Final/FinalReport with Respond, removed RequestUserInteraction
- Updated `tools/base.py` — comment mentions Respond instead of Final
- Verified: zero remaining references to Final/FinalReport/RequestUserInteraction in active code

**Impact:** The agent's tool completion system is now fully unified under the single Respond tool. Agent state handling and frontend pause/resume detection now check for "Respond" instead of the three legacy tools.

## 2026-05-27 — ## Respond stop signal unification (2024)

The `Respond` too...

## Respond stop signal unification (2024)

The `Respond` tool previously generated **two distinct stop signals** depending on `response_type`:
- `response_type="answer"` → `tool_type="final"` → `event_type="final"` with `stop_reason` → syncs conversation + IDLE state
- `response_type="question"` → `tool_type="user_interaction"` → `event_type="user_interaction_requested"` without `stop_reason` → **no conversation sync** + WAITING_FOR_USER state

This caused two bugs: (1) conversation not synced for questions, (2) state flicker WAITING_FOR_USER→IDLE from session_stop.

### Fix: Unified `"agent_responded"` event

**3 files changed:**

1. **`agent/core/tool_executor.py`** — `_execute_single_tool` now returns `tool_type="respond"` with `response_type` and `content` for all Respond calls. `execute_tool_calls` produces a single `respond_result` dict instead of separate `final_content` and `user_interaction_message`.

2. **`agent/core/agent.py`** — Two separate if-blocks (final_detected, user_interaction_message) collapsed into one that yields `{"type": "agent_responded", "response_type": ..., "content": ...}`.

3. **`web_ui/backend/bridge.py`** — `"user_interaction_requested"` and `"final"`-within-tuple handlers replaced by a single `"agent_responded"` handler that **always syncs conversation first**, then decides state based on `response_type` (WAITING_FOR_USER for question, IDLE for answer).

**Downstream consumers updated:** `agent/events.py` (added `AGENT_RESPONDED` enum), `session/event_schema.py` (added to Literal), `agent/presenter/event_processor.py` (added `agent_responded` to terminal event handler).

**Not changed:** The non-tool-call path still emits `"final"` (direct LLM answer without tool calls) — this is correct.

All 90 tests pass.

## 2026-05-27 — ## Phase 1 Audit — Lifecycle & State Management (2025-07-16)...

## Phase 1 Audit — Lifecycle & State Management (2025-07-16)

### 1. ExecutionState Enum
**File:** `agent/core/state.py:20-24`
**Members:** `RUNNING = 'running'`, `PAUSING = 'pausing'`, `READY = 'ready'`
**No STOPPED state.** The system conflates "not running" and "finished" under READY. The `session_stop` event (emitted in controller's `process_query()` finally block) is the signal consumers use to know execution ended.

### 2. Controller Finally Block (state→READY)
**File:** `agent/controller/__init__.py:646-653`
The `finally` block of the event-processing loop in `_run()`:
- Sets `_processing_query = False`
- Resets `agent.state.set_execution_state(ExecutionState.READY)`
- Emits `session_stop` event with `stop_reason` (or `'completed'` default)
This is the **canonical reset point** — the only place READY is set after a query completes from the controller side.

### 3. Event Types Yielded by agent.process_query()
- `user_query` (line 699) — user's input added to conversation
- `turn` — per-LLM-turn data with token/context info
- `tool_call`, `tool_result` — tool execution events
- `token_update` — real-time token count updates
- `agent_responded` (line 1070) — contains `response_type` ('answer'|'question') and `content`
- `final` (line 1129) — terminal event with `stop_reason: 'final'`, usage stats, reasoning
- `error` — config failure notifications (injected as system messages to conversation)
- `execution_state_change`, `session_state_change` — state transitions

### 4. Config Mailbox Pattern
**Agent** (`agent/core/agent.py`):
- `request_config_update(new_config)`: sets `self._pending_config = new_config`
- `_apply_pending_config()`: called at start of `process_query()` (line 702). If `_can_hot_swap()` → `_hot_swap()` (updates temperature, config refs, tools). Else → `_restart_with_config()`. Preserves `_pending_config` on failure for retry.
- `_can_hot_swap()` checks: provider_type, model, api_key, base_url, system_prompt, workspace_path
- `_hot_swap()` updates: runtime_params.temperature, self.config, state.config, tool_executor.config; if enabled_tools changed, rebuilds tool_classes/tool_definitions/ToolExecutor

**Controller** (`agent/controller/__init__.py`):
- `update_config(config)`: stores in `self._config`, calls `agent.request_config_update()` if agent exists
- `request_config_update(config)`: directly delegates to agent

**Bridge** (`web_ui/backend/bridge.py`):
- `apply_config()`: validates via `validate_config()`, resolves provider, stores, calls `controller.request_config_update()`
- `continue_session()`: calls `apply_config()` then pushes to controller

### 5. Tool Executor — No Stop Signal
**File:** `agent/core/tool_executor.py` — pure synchronous dispatch. No stop/cancel mechanism. All stop control is at the controller level via `threading.Event` (`stop_event`, `pause_event`), checked by `should_stop()` callback injected as `config.stop_check` into agent.

### 6. Event Processor Terminal Handling
**File:** `agent/presenter/event_processor.py:159-192`
- `final`/`agent_responded`: sets state→READY, saves final_content/reasoning/timestamp to session
- `max_turns`: sets state→READY
- `stopped`/`thread_finished`: sets state→READY (unless `_restarting`)
- `paused`: does NOT transition to READY (deferred to `session_stop` event)
- All terminal branches: `auto_save_current_session()`

### 7. Legacy Event Type Mapping
**File:** `agent/events.py:325-328` — maps legacy string types to EventType enum values. Includes: tool_call, tool_result, token_warning, turn_warning, agent_responded, final, stopped, max_turns, thread_finished, paused, error, turn, token_update, user_interaction_requested, user_query, rate_limit_warning, execution_state_change, session_state_change.


## 2026-05-27 — ## Generic Config Diff in `_notify_config_change()`

**Appli...

## Generic Config Diff in `_notify_config_change()`

**Applied:** 2026-05-27

**What changed:** Replaced the hardcoded 5-field comparison in `Agent._notify_config_change()` (`agent/core/agent.py` line 212) with a programmatic iteration over all `AgentConfig.model_fields`. This ensures any new fields added to `AgentConfig` are automatically included in config change notifications without manual updates.

**Fields excluded from diff:**
- `api_key` — sensitive credential
- `stop_check` — `Callable`, not comparable with `!=`

**Notification format:** Uses friendly display names for `enabled_tools` ("tools updated"), `system_prompt` ("system_prompt updated"), `provider_type` ("provider=..."), `workspace_path` ("workspace=..."). All other changed fields are reported as `field_name=value`.

**Context changes (Fixes 1 & 2 from same session):**
- Fix 1: `process_query()` reordered so `_apply_pending_config()` runs before yielding `user_query`. On config failure: yields error + returns early. On success: yields `user_query` with notification.
- Fix 2: `apply_config()` in `web_ui/backend/bridge.py` now emits `config_changed` WebSocket message after persisting config changes.

## 2026-05-28 — ## RAW TOOL CALL LOGGING

Added a `log()` call in `agent/cor...

## RAW TOOL CALL LOGGING

Added a `log()` call in `agent/core/agent.py` at line 1032-1037 (right after `response.tool_calls` is extracted) to log raw tool call arguments from the LLM response. This is placed at the closest point to the LLM response, before any transformation. The log uses `truncate_hint=None` to avoid truncation, ensuring full arguments are captured for debugging (e.g., verifying PaginateTool arguments from other sessions).

- Tag: `core.agent`
- Message: `'RAW TOOL CALL ARGUMENTS from LLM response'`
- Data: `{'tool_calls': tool_calls}` (the full list of tool call dicts from `response.tool_calls`)
- Truncation: disabled via `truncate_hint=None`

## 2026-05-28 — ## Bootstrap subsystem

Created `thoughtmachine/bootstrap.py...

## Bootstrap subsystem

Created `thoughtmachine/bootstrap.py` with `ensure_user_defaults()` that copies bundled default resources from `resources/` to `~/.thoughtmachine/` on first run. Resources include config.json, system_prompt.txt, providers.json, security_policy.json, and .version.

**Resource files** live in the project root `resources/` directory:
- `.version` — single version string
- `default_config.json` — AgentConfig defaults as JSON
- `default_system_prompt.txt` — the standard system prompt
- `default_providers.json` — empty providers template (`{"profiles": [], "active_profile_id": null}`)
- `default_security_policy.json` — secure defaults for Docker security policy

**Package data** is declared in `pyproject.toml` under `[tool.setuptools.package-data]` with `"*" = ["resources/..."]` entries.

**Startup wiring**: `web_ui/backend/server.py` lifespan handler calls `ensure_user_defaults()` before accepting connections. The `main()` function also benefits from this since lifespan runs on every uvicorn startup.

## 2026-05-28 — ## 2026-05-28 — Sysprompt & Worker Templates Finalization

*...

## 2026-05-28 — Sysprompt & Worker Templates Finalization

**Tasks Completed:**
1. **Updated `resources/default_system_prompt.txt`** — Revised prompt with:
   - Clean `Respond` tool rules (no Final/FinalReport/RequestUserInteraction mentions)
   - Anti-loop guidance (Rule 13): stop repeating same tool calls, analyze before retrying
   - KB `scope` parameter documented (global scope via `scope=global`)
   - Streamlined formatting

2. **Added `@field_validator('system_prompt')` to `AgentConfig`** in `agent/config/models.py`:
   - Auto-loads from `resources/default_system_prompt.txt` when field is `None` or empty string
   - Falls back to minimal default text on file-not-found errors
   - Custom prompts pass through unmodified

3. **Created `resources/worker_templates/` directory** with 3 JSON templates:
   - `coder.json` — code writing agent with file/code tools (temp=0.2)
   - `reviewer.json` — code review agent with read-only/analysis tools (temp=0.1)
   - `researcher.json` — codebase analysis agent with search/KB tools (temp=0.3)

4. **Verification** — All files pass: JSON validity, Python syntax, config loading tests, bootstrap resource integrity

## 2026-05-28 — ## Bootstrap Alignment Fixes

Applied 4 alignment fixes to t...

## Bootstrap Alignment Fixes

Applied 4 alignment fixes to the bootstrap/resource system:

1. **`resources/default_config.json`** — Regenerated from `AgentConfig().model_dump()` at runtime. Previously had stale values (`"base_url": "https://api.deepseek.com/v1/"`, `"model": "deepseek-v4-flash"`, `"enabled_tools": []`). Now matches the actual Pydantic model defaults exactly (`base_url: "https://api.deepseek.com"`, `model: "deepseek-reasoner"`, full enabled_tools list).

2. **`resources/default_security_policy.json`** — Regenerated from `get_default_security_config()` (session schema). Previously had an incorrect schema (`{"default": {"docker_network_allowed": false, "writable_home": false}}`). Now matches the actual session-file schema: `{"version": 1, "session_policy": {...}, "agent_overrides": {}}`.

3. **`~/.thoughtmachine/config.json` → `~/.thoughtmachine/agent_config.json`** — Renamed in all code references:
   - `agent/presenter/state_bridge.py` line 26
   - `agent/config/loader.py` lines 153, 157
   - `web_ui/backend/server.py` was already using `agent_config.json`

## 2026-05-31 — ## Event type duality — final vs agent_responded

**Discover...

## Event type duality — final vs agent_responded

**Discovered:** The event system has a persistent duality:
- **`'final'`** is emitted on the "no tool calls" direct-answer path (`agent.py:1188`), when the LLM produces content without invoking any tool.
- **`'agent_responded'`** is emitted when the LLM calls the `Respond` tool (`agent.py:1134`).

The old `'user_interaction_requested'` was properly removed (replaced by `Respond` with `response_type='question'`), but `'final'` remains on the direct-answer path. This may be intentional (signal "agent completed autonomously" vs "agent used a tool to respond") but the retiring engineer's handoff suggests it was supposed to be unified. **Potential inconsistency — needs architectural decision.**

## 2026-05-31 — ## SessionPermissions Model — Landed 2026-06-02

### What wa...

## SessionPermissions Model — Landed 2026-06-02

### What was done
1. **`SessionPermissions`** Pydantic model added to `thoughtmachine/security.py` with 5 fields matching `DEFAULT_SESSION_PERMISSIONS` in `tool_executor.py`:
   - `container: bool = False`
   - `network: bool = False`
   - `filesystem: Literal['banned','read','write','full'] = 'read'`
   - `security: Literal['banned','read','write','full'] = 'read'`
   - `execution: Literal['banned','read','write','full'] = 'banned'`
   - Includes `to_dict()` helper and `model_config = ConfigDict()`

2. **`AgentConfig.session_permissions`** field added to `agent/config/models.py`:
   - Type: `Optional[SessionPermissions]`, defaults to `None`
   - Category: `HOT_SWAPPABLE`
   - When `None`, `tool_executor` falls back to `DEFAULT_SESSION_PERMISSIONS`

3. **WebSocket pipeline** wire-up in `web_ui/backend/server.py`:
   - `_FALLBACK_FRONTEND_CONFIG` now includes `session_permissions` with default values
   - `_translate_frontend_config()` passes the dict through transparently (no special mapping needed)
   - `_backend_to_frontend_config()` also passes through transparently
   - Pydantic's `model_dump()` already converts nested models to dicts correctly
   - `validate_config()` → `AgentConfig(**config_dict)` correctly coerces dict → `SessionPermissions`

### Verified
- All model construction, coercion, serialization, and translation pipeline works end-to-end
- Tested with both default and custom permission values

## 2026-06-02 — **Repository**: `git@github.com:use-less-vars/ThoughtMachine...

**Repository**: `git@github.com:use-less-vars/ThoughtMachine.git`

## 2026-06-03 — **Config System Report (2025-07-16)**: Completed a comprehen...

**Config System Report (2025-07-16)**: Completed a comprehensive analysis of the entire configuration system. Key findings documented in downloadable report. See notes on: AgentConfig model (35+ fields, FIELD_CATEGORIES), loader.py (self-healing load/save, legacy field migration, atomic writes), config paths (project-root global + ~/.thoughtmachine/ user), hot-swap vs restart logic, session persistence bridge, frontend-backend translation layer, and bootstrap setup.

## 2026-06-03 — **2025-07-17**: Removed all Qt dependencies from `AgentContr...

**2025-07-17**: Removed all Qt dependencies from `AgentController` class:
- Removed `QObject` base class, `pyqtSignal` declarations (`event_occurred`, `conversation_updated`), and `super().__init__()` 
- Removed the PyQt6 fallback dummy classes (`_DummySignal`, dummy `QObject`, `pyqtSignal` function)
- All signal emits replaced by event queue + plain callback mechanism only
- The `gui_integration.py` retains its own Qt signals (state_changed, tokens_updated, etc.) for the GUI layer
- Event flow: Agent thread → `_emit_event()` → event_queue + plain callbacks (no Qt signal path)

## 2026-06-03 — **2025-07-17**: Event type unification — direct-answer path ...

**2025-07-17**: Event type unification — direct-answer path now yields `agent_responded` instead of `final`:
- Changed `agent/core/agent.py` line 1188: `{'type': 'final', ...}` → `{'type': 'agent_responded', 'response_type': 'answer', ...}`
- Removed all `'final'` event-type handling from `event_processor.py` (MESSAGE_EVENT_TYPES, state_event_types, conditionals)
- Removed `'final'` from bridge.py event-type sync group
- All terminal agent responses (Respond tool + direct answer) now produce identical `agent_responded` events
- `FINAL = 'final'` enum value kept in `events.py` for backward compat mapping only

## Permission Categories — Design Notes

## 2026-06-04 — Permission Categories — Design Notes

Permission Categories — Design Notes


## 2026-06-04 — **2025-03-25** — Security model three-layer design (from pro...

**2025-03-25** — Security model three-layer design (from product owner):
1. **Workspace** (Spec 3 — not started): Per-project sandbox container with `capabilities.json` defining the maximum ceiling (allowed domains, git permissions, installed packages). Owner defines this.
2. **Session** (current): User toggles further restrict from the workspace ceiling (e.g., "no internet", "read-only filesystem"). These are the `session_permissions` in `AgentConfig`.
3. **Tools**: Each tool declares its required categories via `required_categories: ClassVar` or `get_required_categories()`. The permission gate (`_check_permissions` in `tool_executor.py`) enforces at execution time.

Priority order (P0 = highest):
1. **Fix config panel sync** (P0) — root cause: shallow merge in config persistence pipeline wiped partial updates (e.g. just sending `{"session_permissions": {"filesystem": "read"}}` nuked other fields). `deep_merge` utility exists and is used in `apply_config`/`load_session` but was never integration-tested. **Now tested** — see `tests/test_bridge_permissions_sync.py`.
2. **Verify tool permissions** (P0) — raw evidence collected in separate audit (R1-R11).
3. **Manual GUI verification** (30m) — toggle permissions, close/reopen session, confirm GUI matches gate.
4. **Workspace container** (Spec 3) — depends on sync being solid.
5. **Multi-agent** (Spec 4) — depends on container.

Key principle: "The machine must never exceed what you see on the Permissions tab."

## 2026-06-06 — ## Security Architecture Verification (2025-06-05)

Verified...

## Security Architecture Verification (2025-06-05)
## Security Architecture Verification (2025-06-05) — CORRECTED

**IMPORTANT**: Earlier verification (2025-06-05) claimed discrepancies that were FALSE NEGATIVES from FileSearchTool missing results across large directory trees. Corrected findings below:

All engineer's claims are VERIFIED CORRECT:

1. ✅ **`get_required_categories()` EXISTS** on ToolBase (tools/base.py:46) returning `cls.required_categories` default, and is OVERRIDDEN on:
   - FileEditor (tools/file_editor.py:15) — returns `filesystem:read`/`filesystem:write` based on operation
   - GitInfoTool (tools/git_info_tool.py:18) — returns dynamic permissions based on git operation
   - KnowledgeBase (tools/knowledge_base.py:155) — returns `filesystem:write` for write modes
   - MCPManager (tools/mcp_manager.py:226) — dynamic permissions for HTTP/SSE MCP clients
   - MCPValidator (tools/mcp_validator.py:24) — returns `network:outbound` for HTTP/SSE connections
   - ProgressReport (tools/progress_report.py:17) — always returns `filesystem:write`
   - Respond (tools/respond.py:38) — returns `filesystem:write` only when report_body provided

2. ✅ **`_translate_frontend_config()` EXISTS** in web_ui/backend/server.py:1113, with tests in web_ui/backend/tests/test_config_pipeline.py. Also `_backend_to_frontend_config()` at line 1280.

3. ✅ **SecurityPromptEvent IS published** from tool_executor.py:161-173 — a `SecurityPromptEvent` is created and published when tool requires user permission.

4. ✅ **`required_categories: ClassVar[List[str]]` IS defined** on ToolBase (line 39) and used across 15+ tool classes.

**Key Lesson**: FileSearchTool can produce false negatives when searching broad directory trees. Always narrow searches to specific subdirectories to verify negative findings.

## 2026-06-07 — ## Interruptible Prompt Queue
**Status**: Fully implemented ...

## Interruptible Prompt Queue
**Status**: Fully implemented and tested.
**Location**: `thoughtmachine/security.py` — `_prompt_cancelled` Event (line 47), timeout loop (lines 570-575), `cancel_pending_prompts()` (lines 993-1011).
**Integration**: `agent/controller/__init__.py` — called from `pause()` (line 471) and `shutdown()` (line 305).
**Tests**: 4 tests in `TestCancelPendingPrompts` class, `tests/test_permissions_roundtrip.py` (lines 610-696).

## security_permissions

## 2026-06-08 — ## Security / Permissions System — Architecture Documentatio...

## Security / Permissions System — Architecture Documentation

Completed a comprehensive investigation of the entire security/permissions system. Documented in detail in the report file `security_permissions_architecture_report.md`.

**Architecture Summary:**
- Two-tier model: SessionPermissions (coarse booleans, hot-swappable) + security config (fine-grained rules with per-target overrides, in-memory only)
- Permission categories: `"domain:action"` strings declared by each tool via `required_categories` or `get_required_categories(params)`
- Check order: Session permissions → Security config → Ask dialog
- WebSocket protocol: `security_prompt` (backend→frontend) + `security_response` (frontend→backend)
- Frontend: SecurityDialog.jsx (modal), ConfigPanel.jsx (permissions toggles), SessionTab.jsx (event handler)

**Key files:** thoughtmachine/security.py, tools/base.py, agent/core/tool_executor.py, agent/events.py, web_ui/backend/bridge.py, web_ui/backend/server.py, web_ui/frontend/src/components/{SecurityDialog,SessionTab,ConfigPanel}.jsx

**Suggested improvements documented in report.**



## 2026-06-08 — ## Container rebuild background thread (2026-06-08)

`rebuil...

## Container rebuild background thread (2026-06-08)

`rebuild_container()` in `docker_executor.py` now runs the Docker build in a **background daemon thread** and returns immediately with `{"status": "building", ...}` instead of blocking.

- Results are stored in `_background_build_results` (keyed by normalised path)
- `get_container_status()` merges those results into its return (step 7)
- The `image` field (`_compute_image_tag(normalised)`) was added to all 3 return sites of `get_container_status()` plus shown in ContainerPanel.jsx
- Server endpoints `/api/container/status` and `/api/container/rebuild` changed from `async def` to `def` (FastAPI runs sync routes in thread pool, avoids blocking event loop)

## 2026-06-09 — ## Policy-aware capabilities & read-only mounts (2026-07-10)...

## Policy-aware capabilities & read-only mounts (2026-07-10)

Committed in `58bc025`:
- Workspace volume is now mounted **read-only** (`ro`) in containers — containers can no longer modify host files via the volume.
- Added `_compute_effective_capabilities()` which merges workspace capabilities with a Docker security policy, taking the most restrictive value per field.
- `get_container_status()` now uses `_compute_effective_capabilities()` instead of raw `_load_capabilities()`.

## Resource Deployment Map

## 2026-06-10 — ## Resource Deployment Map — `resources/` → User Config (202...

## Resource Deployment Map — `resources/` → User Config (2026-06-09)

The `resources/` directory in the ThoughtMachine repo is the **source of truth / blueprint** for all user-level configuration. When the agent debugs config issues, it should look in `resources/` first — these files are what get deployed to `~/.thoughtmachine/` on first run.

### Explicit RESOURCE_MAP (defined in `thoughtmachine/bootstrap.py`)

| Repo Source (`resources/`) | Deployed To (`~/.thoughtmachine/`) | Description |
|---|---|---|
| `default_config.json` | `agent_config.json` | Agent configuration (model, provider, keys, tool settings) |
| `default_system_prompt.txt` | `system_prompt.txt` | System prompt for the agent |
| `default_providers.json` | `providers.json` | Provider profile definitions |

### Additional Deployed Resources

| Source | Destination | Trigger |
|---|---|---|
| `resources/worker_templates/*.json` | `~/.thoughtmachine/worker_templates/` | Copied if destination dir is empty (first run) |
| `resources/global_kb/*.md` | `~/.thoughtmachine/knowledge/system/` | Synced on version mismatch via `ensure_global_kb()` |
| `resources/global_kb/.version` | `~/.thoughtmachine/knowledge/.version` | Version marker for sync detection |

### How First-Run Deployment Works

1. **`bootstrap.py::ensure_user_defaults()`** is called at server startup (`server.py` lifespan)
2. Creates `~/.thoughtmachine/` + subdirs (`sessions/`, `state/`, `knowledge/`, `worker_templates/`)
3. For each entry in `RESOURCE_MAP`, copies source→destination **only if** destination doesn't exist (unless `overwrite_existing=True`)
4. Copies `worker_templates/` only if destination is empty
5. Calls `ensure_global_kb()` which copies `global_kb/*.md` → `knowledge/system/` if version changed

### Config Loading Flow (at runtime)

1. **`agent/config/loader.py::load_config()`** merges defaults (from `AgentConfig()` pydantic model) with user's `~/.thoughtmachine/agent_config.json`
2. Defaults come from `AgentConfig()` pydantic model — NOT from `default_config.json` at this point
3. `default_config.json` is only used on **first run bootstrap** — after that, the user's file is authoritative

### Why This Matters for Debugging

When investigating config-related bugs:
- **Can't find a config field?** → Check `resources/default_config.json` — it's the blueprint
- **User's `agent_config.json` has weird values?** → Check if `resources/default_config.json` changed between versions
- **Fake model/provider appearing?** → Check `resources/default_config.json` first (this is where `deepseek-v4-flash` came from)
- **Missing provider profiles?** → Check `resources/default_providers.json`
- **Stale system prompt?** → Check `resources/default_system_prompt.txt`



## 2026-06-10 — ## Permission Toggle Guide Audit (2025-03-25)

Audited the "...

## Permission Toggle Guide Audit (2025-03-25)

Audited the "How to add a new permission toggle" guide against the actual codebase. Key findings:

**What the guide gets right:**
- SessionPermissions model structure (thoughtmachine/security.py) — matches reality
- Tools declare via get_required_categories() — matches reality
- get_effective_permissions() merges workspace caps + session permissions — matches reality
- _value_satisfies() handles level ordering automatically — matches reality
- GUI dropdowns in ConfigPanel.jsx — match reality for filesystem, network, container, git, execution

**Issues found (guide is incomplete/misleading):**

1. **`security` ↔ `system` mismatch**: SessionPermissions has `security: Literal["banned","read","write","full","ask"]` but the GUI has `system: bool`. The gate's `get_effective_permissions()` maps `session.security` → key `"system"`. A multi-level permission is collapsed to a boolean toggle.

2. **`execution` not in gate**: The `execution` field exists in SessionPermissions and has a GUI dropdown, but `get_effective_permissions()` doesn't include it. It's ONLY checked by the old `_check_permissions` path in tool_executor.py, not the new gate path.

3. **Guide says "automatically" for gate merge logic**: This is misleading. Adding a field to SessionPermissions does NOT automatically make the gate handle it. You must add explicit merge logic in `get_effective_permissions()`.

4. **Dual permission paths exist**: The old `_check_permissions` path (agent/core/tool_executor.py) still runs alongside the new gate path. The guide doesn't mention this.

5. **WorkspaceCapabilities for new resources**: The guide says it's automatic via the gate's `min()`, but this only works after you've added merge logic to get_effective_permissions().

## Frontend UX Patterns

## 2026-06-10 — ## Auto-focus query bar on keystroke (2026-06-10)

**Problem...

## Auto-focus query bar on keystroke (2026-06-10)

**Problem**: Clicking a copy button (📋) on a message steals focus from the query bar. User has to manually click back to the textarea before typing their next message. Also, clicking blank space or any non-form element leaves focus in limbo.

**Solution** — two-layer approach:

1. **`CopyButton` (ChatPanel.jsx)**: `onMouseDown={(e) => e.preventDefault()}` prevents the browser's default focus-on-click for `<button>` elements. This keeps focus wherever it was before the click — usually the query bar.

2. **Global keydown listener (SessionTab.jsx)**: Capture-phase `keydown` listener on `document` that redirects printable-character keystrokes to `.query-input` when no form field (`<input>`, `<textarea>`, `<select>`, `contentEditable`) has focus. Keyboard shortcuts (Ctrl/Cmd/Alt) and non-printable keys (arrows, Tab, Enter, Escape) pass through unmodified.

**Files changed**: `ChatPanel.jsx`, `SessionTab.jsx`


## 2026-06-10 — **2025-07-16:** Added `execution` permission to the unified ...

**2025-07-16:** Added `execution` permission to the unified security gate (`security/security_gate.py`). `get_effective_permissions()` now returns 6 keys: filesystem, network, container, git, system, execution. Execution is session-level only (no workspace merge needed). Also cleaned up stale `USE_UNIFIED_GATE` docstring reference.

## 2026-06-10 — ## Graceful Shutdown Handler (added 2026-06-10)

**Location:...

## Graceful Shutdown Handler (added 2026-06-10)

**Location:** `web_ui/backend/server.py` lines 192-265

**Purpose:** Save in-flight WebSocket sessions when the server receives Ctrl+C / SIGINT / SIGTERM.

**Architecture:**
- `_active_bridges: list` — global list tracking all active `WebAgentBridge` instances
- `_shutdown_save()` — iterates `_active_bridges`, saves open sessions, stops bridges. Registered via `atexit`.
- `_get_shutdown_event()` — returns a lazily-created `asyncio.Event` singleton
- `_trigger_shutdown()` — signal handler (SIGINT/SIGTERM) that sets the asyncio event
- **atexit** registration ensures save runs on normal exit (including after uvicorn's Ctrl+C handling)
- **asyncio.Event** is checked in the WebSocket loop (`while True`) so the handler exits promptly and runs its `finally` block

**Bridge lifecycle:**
1. On `start_session`: `_active_bridges.append(bridge)` after bridge creation
2. On disconnect/error in `finally`: `_active_bridges.remove(bridge)` with `ValueError` guard
3. On server shutdown: `_shutdown_save()` iterates remaining bridges and saves/cleans them up

## Container Integrity & Honesty

## 2026-06-10 — ## Container Integrity & Honesty (2026-06-10)

### Overview
...

## Container Integrity & Honesty (2026-06-10)

### Overview
A three-layer system ensuring Docker containers match their expected security configuration:

1. **`docker_executor.py`** — `verify_container_integrity()` compares actual container config (network mode, mount mode) against expected config from security gate. Returns a dict with `action_taken`, `actual`, `mismatch_reason`. Also added `get_integrity_status()` for lightweight API consumption.

2. **`web_ui/backend/bridge.py`** — `_maybe_re_sync_container()` method wraps `verify_container_integrity()` and updates container permissions if mismatch detected. Wired into `apply_config()` as step 7, so every config update triggers a re-sync.

3. **`web_ui/backend/server.py`** — `/api/container/integrity` endpoint accepts `workspace` and optional `permissions` (JSON-encoded) query params. Returns `{"integrity": "ok"|"mismatch"|"removed"|"error", "details": {...}}`.

4. **`ContainerPanel.jsx`** — Fetches integrity status every 5s alongside status. Shows green dot for "ok", red for any error state. Mismatch details expand when integrity === "mismatch".

5. **Integration points**: Session-load integrity checks in `session_lifecycle.py` already exist (lines 291-309, 349-365). The `/integrity` endpoint and WebSocket `apply_config` handler (which calls `bridge.apply_config()`) provide the API pathways.

### Design decisions
- Integrity check is read-only for the API endpoint; writes happen via the re-sync path in `bridge.py`.
- The endpoint can accept `permissions` as JSON to check an expected state without mutating the executor's state.
- ContainerPanel polls both `/api/container/status` and `/api/container/integrity` on the same 5s interval.
- Real-Docker test variant uses `pytest.mark.skipif` with Docker daemon ping to conditionally run.


## 2026-06-12 — ## Windows Installation Saga Documented

Generated `docs/win...

## Windows Installation Saga Documented

Generated `docs/windows_installation_saga.md` — a reverse‑engineered journey document covering all 30 commits of the Windows install-and-run saga. Covers: pre‑Windows fixes (circular deps, session config loss, graceful shutdown), cross‑platform file locking, the 4‑attempt venv activation struggle, Vite positioning debate (separate window → same window → pre-flight checks), the big refactor (Vite‑first launch, direct binaries), cleanup evolution, wait strategy evolution (timeout→ping→PowerShell→user input), and the Ctrl+C saga (separate window→PowerShell handler→syntax fix→foreground fix). Documents 6 key design principles discovered.

## 2026-06-13 — ## 2026-06-12: Workspace bootstrap architecture verified

##...

## 2026-06-12: Workspace bootstrap architecture verified

### Directory layout
- `~/.thoughtmachine/` — top-level user data directory (created by `bootstrap.py::ensure_user_defaults()`)
  - `sessions/`, `state/`, `knowledge/`, `worker_templates/` — created at **top level only** by `bootstrap.py`
  - `workspaces/{id}/` — per-workspace config directory (created by `ensure_workspace_dirs()`)
    - Only 5 files: `capabilities.json`, `Dockerfile`, `domain_allowlist.json`, `workers.json`, `mcp_servers.json`
    - **No subdirectories** (sessions/, state/, knowledge/ are **not** created here)

### Verification results (2026-06-12)
1. **Code audit**: Zero remaining mkdir calls targeting workspace config subdirectories
2. **Fresh bootstrap simulation**: Only 5 files created, no subdirectories, safeguard warns on extras
3. **Test suite**: 56/56 passed (workspace API, bootstrap, tools, trust toggle tests)

All pre-existing failures are environment-specific (missing packages), not related to workspace code.

## 2026-06-13 — ## 2026-06-12 — System prompt updated with explicit Agent/Ho...

## 2026-06-12 — System prompt updated with explicit Agent/Host/Docker architecture

**File:** `resources/default_system_prompt.txt`

**What changed:**
1. Added **Architecture (understand this first)** section at the very top, before Core Rules — explicitly states that the agent runs on the host, Docker is a tool invoked from the host (not the agent's runtime), and the user's app runs separately on their computer.
2. Strengthened **Rule 15** (Capability Transparency) with a reminder line: "You run on the host. Docker is a tool you invoke from the host. The user's app runs on their computer. These are three separate things."

**Motivation:** The agent was getting confused about its own runtime environment — conflating "where I run" (host), "where I run code" (Docker sandbox), and "where the user's app runs" (their computer). The old prompt told the agent to *explain* this distinction to users, but never stated it as a direct fact about the agent itself.

## 2026-06-13 — ## 2026-06-13: Recent changes report compiled — config layer...

## 2026-06-13: Recent changes report compiled — config layering, system prompt custody, session store pagination, workspace panel, lazy connect

Key architectural changes:
1. **Universal config layering** — `load_factory_config()` loads from `resources/default_config.json` as single source of truth, `load_config()` does deep-merge of factory + user overlay, `save_config()` persists only diff from factory defaults
2. **System prompt custody** — moved from `system_prompt.txt` in `MANIFEST.json` to `~/.thoughtmachine/custom_system_prompt.txt`, with migration from legacy path; `AgentConfig` field validator loads from custom file with clear precedence: custom file > explicit value > factory default
3. **Session store pagination** — `load_session()` now accepts `limit`/`offset` parameters, `_fast_extract_metadata()` avoids parsing full `user_history` arrays, `_meta_` files cache metadata separately, cache TTL increased from 5s to 60s
4. **Browser bridge caching** — `_session_bridges` dict replaces `_active_bridges` list; bridges kept alive across tab switches/reconnects, stopped only on explicit `close_session` or server shutdown
5. **Lazy WS connect** — inactive tabs skip WebSocket connection; `isActive` prop triggers connection when tab becomes active
6. **Workspace Panel** — new UI component with Dockerfile viewer, domain allowlist editor, workers list (10s poll), effective permissions display
7. **pyproject.toml fixes** — build-backend fixed to `setuptools.build_meta`, packages include all application modules, version sourced from `resources/.version` instead of bootstrap function
8. **No-op config detection** — `_configs_are_identical()` in agent.py skips spurious restarts when frontend re-sends identical config

## 2026-06-14 — Starting comprehensive audit of session tab lifecycle and br...

Starting comprehensive audit of session tab lifecycle and bridge architecture. Will examine: session creation/loading/saving/closing, open_sessions.json persistence, WebSocket protocol, bridge responsibilities, frontend tab management.

## 2026-06-14 — ## Permission system redesign (completed 2026-06-14)

### Ch...

## Permission system redesign (completed 2026-06-14)

### Changes made to `tools/workspace/worker.py`

1. **Removed private `_value_satisfies` import** (lines 73-79) and `VALUE_SATISFIES_AVAILABLE` flag
2. **Removed manual HIERARCHY fallback** in `_check_tool_permissions`
3. `_check_tool_permissions` now routes through the single gate: `check_required_categories(required, effective, tool_name, tool_args, description, event_bus=None, worker_permissions=self._worker_permissions)`
4. If gate is unavailable, denies all (safe default)

### Changes made to `tools/read_file_tool.py`

1. Added `import os`
2. Path containment check now uses `str(root_path) + os.sep` prefix check + equality guard to prevent false-positive matches on sibling directory names

### Changes made to worker spawn logic (`_action_spawn`)

1. Missing tools are now tracked in a `missing_tools` list
2. Spawn response includes `missing_tools` field and a warning message when tools aren't found
3. Logger warning emitted for missing tools

## 2026-06-14 — ## Worker Tool Workspace Path Resolution (2026-06-14)

**Dec...

## Worker Tool Workspace Path Resolution (2026-06-14)

**Decision:** Worker tool instances inherit the session's project root as their `workspace_path`, not a field from the worker definition.

**Rationale:**
- Worker definitions should be pure configuration (name, system_prompt, permissions, tools)
- Runtime state like filesystem paths should come from the session runtime
- The session already knows the project root from `~/.thoughtmachine/workspaces/<id>/config.json`
- Hardcoding paths in worker definitions creates a maintenance burden (breaks on project move)

**Implementation:**
- `Worker._action_spawn()` reads project root from `ws_dir/config.json` and passes it to `WorkerThread(project_root=...)`
- `WorkerThread._execute_tool()` uses `self._project_root` as the tool's `workspace_path`
- Fallback to `self._worker_dir` only if project_root is unknown

## Worker Tool Safety Assessment

## 2026-06-15 — 
2025-01-15: Complete tool safety assessment for workers.

W...


2025-01-15: Complete tool safety assessment for workers.

Worker blocklist (hardcoded in tools/workspace/worker.py):
- Worker (recursion)
- DockerCodeRunner (container execution)
- EditDockerfile (container config)
- MCPValidator (MCP server management)

Every tool's required_categories determines what permission the worker's 
permission_footprint must include. The security gate enforces at spawn time.

RECOMMENDED "full-blown worker" tool set:
All tools except the blocklisted 4. Tools with permission-sensitive ops
are gated by the worker's permission_footprint.

## Multi-Tab Bridge Lifecycle

## 2026-06-27 — ## Multi-Tab Bridge Lifecycle Design Decision (2025-01-15)

...

## Multi-Tab Bridge Lifecycle Design Decision (2025-01-15)

**Context:** The callback dict fix correctly broadcasts events to multiple WebSocket connections,
but the shared WebAgentBridge object per session means destructive operations (start_session,
new_session, close_session) from any tab stop the shared bridge, freezing other tabs.

**Decision:** Defer the proper fix until Phase 3 bridge refactor.

**Rationale:**
- Option A (no bridge sharing) would lose the "live mirror" experience where a second tab
  shows an ongoing agent run in real-time — exactly what the callback dict was designed for.
- Option B/C (tab-scoped lifecycle with shared event broadcast) are architecturally invasive
  and fit naturally into the planned Phase 3 bridge decomposition (SessionManager + SessionView).
- Current work (multi-agent, container persistence) must not be disrupted.
- Bug is minor: only manifests with second tab + destructive action. Normal single-tab usage
  and multi-tab continue_session are unaffected.

**Planned Phase 3 Architecture:**
- SessionManager: owns the single session controller + history
- SessionView (per-tab): lightweight lifecycle handler that cannot kill the shared core
- This architecture inherently eliminates the shared-mutable-state trap

**References:**
- bridge.py: set_event_callback(), remove_event_callback(), _event_callbacks dict
- server.py: load_session cache-reuse (lines 832-838), start_session (441-446),
  new_session (1041-1057), close_session (999-1016)


## 2026-06-29 — ## Architecture Deep-Dive Completed

Read and analyzed all 4...

## Architecture Deep-Dive Completed

Read and analyzed all 40+ source files across agent/, session/, llm_providers/, tools/, thoughtmachine/, and security/. Comprehensive document built covering:

- Agent facade and component delegation pattern
- process_query turn loop with 3 pause checkpoints
- Mailbox pattern for config hot-swap vs restart
- AgentState machine (READY/RUNNING/PAUSING)
- TurnTransaction buffer-and-commit pattern
- EventBus pub/sub with legacy compatibility
- Session model: ObservableList, conversation_version, append-only user_history
- HistoryProvider > ContextBuilder > HistoryPruner pipeline
- ProviderFactory: OpenAI-compatible + Anthropic
- AgentController: background thread with event queue
- Presenter layer: StateBridge -> EventProcessor -> SessionLifecycle
- ToolExecutor: tool execution with security gate
- FileSystemSessionStore with locking
- Token counting, rate limiting, emergency retry
- Logging subsystem

Still to analyze: security/__init__.py, thoughtmachine/*, web_ui/*

## 2026-06-29 — ## Architecture Investigation: 10 Questions (July 2025)

###...

## Architecture Investigation: 10 Questions (July 2025)

### Q1 — Session attributes accessed by Agent
The `Agent` class in `agent/core/agent.py` accesses these `self.session` attributes via direct attribute assignment (no TurnTransaction):
- `self.session.user_history` (read: lines 555, 1298)
- `self.session.total_input_tokens` (read/write: lines 696, 702)
- `self.session.total_output_tokens` (read/write: lines 708, 714)
- `self.session.conversation_version` (read: line 730)
- `self.session.conversation_hash` (read: line 730)
- `self.session._get_next_seq()` (call: line 751)
- `self.session.session_id` (read: line 836)
- `self.session.summary` (write: lines 1300, 1330)
- `self.session.updated_at` (write: line 1331)

### Q2 — Token/Turn warning injection
Complete flow: (a) `state.update_token_state()`/`update_turn_state()` create typed events; (b) `agent.py` turn loop (lines 885-906) consumes events, creates `[SYSTEM NOTIFICATION]` Messages; (c) `tool_executor.py` (line 122) checks `is_tool_allowed()` before executing; (d) `get_allowed_tools()` returns `['Respond', 'SummarizeTool']` when `restrictions_active=True`. Warning texts documented.

### Q3 — Time-based state
**None exists.** No idle timers, no timeouts, no scheduling. Only event timestamps for display.

### Q4 — Worker tool code
`tools/workspace/worker.py` (1069 lines) — **not stubs.** Full `WorkerThread` class with LLM provider, tool execution, conversation persistence, idle timeout. 5 actions: list, spawn, check, query, stop. Real implementations in `_action_spawn`, `_action_check`, `_action_query`.

### Q5 — Streaming in llm_providers
**No streaming API exists.** `LLMProvider.chat_completion()` is synchronous abstractmethod. No `stream`, `chunk`, `delta` methods. No async code in agent/core/ or llm_providers/. Both `openai_compatible.py` and `anthropic_provider.py` implement synchronous `chat_completion()` only.

### Q6 — `_handle_state_event` processing
Simple event router at `agent.py:565-587`. For `token_warning`/`turn_warning`: no-op (injection happens in turn loop). For `execution_state_change`/`session_state_change`: logs and yields event.

### Q7 — Tool restriction enforcement
`tool_executor.py` line 122: checks `is_tool_allowed()` before each tool. If disallowed, `_create_tool_rejection_message()` (line 314) returns formatted rejection listing allowed tools. Rejection is recorded as tool result.

### Q8 — `update_token_state` full method
`state.py:67-116`. Compares tokens to thresholds → LOW/WARNING/CRITICAL. On first escalation, creates `token_warning` event with formatted message. Critical immediately sets `restrictions_active=True`. Return to LOW clears restrictions.

### Q9 — `update_turn_state` full method
`state.py:118-159`. Compares turn to `max_turns-3`. At threshold, sets `restrictions_active=True`, creates `turn_warning` event. Return to LOW clears restrictions.

### Q10 — `_handle_state_event` (same as Q6)

## Config Loading Architecture

## 2026-06-29 — 2025-07-16: Documented full config loading architecture — fi...

2025-07-16: Documented full config loading architecture — file hierarchy (resources/default_config.json → ~/.thoughtmachine/config.json → profile overrides), bootstrap flow, AgentConfig model, workspace capabilities, how tools get config injected (ToolExecutor passes session_permissions and agent_config_dict), worker config building (with hardcoded 300s timeout), and CheckSystem.my_config. Key gaps: agent_config_dict is partial, Worker timeout default (300) diverges from AgentConfig default (600), no dedicated "read my config" tool exists.


## 2026-06-29 — ## 2026-06-29: Runtime Timeout Override + CheckSystem Extens...

## 2026-06-29: Runtime Timeout Override + CheckSystem Extension

### Worker timeout architecture
- Worker tool class now has `timeout_seconds` field (default None → falls back to definition → 600)
- WorkerThread stores resolved timeout as `self._timeout_seconds` 
- `_build_agent_config` uses `self._timeout_seconds` instead of `definition.get("timeout_seconds", 300)`
- Priority: spawn `timeout_seconds` param > definition `timeout_seconds` > 600

### Elapsed time tracking
- `_run_tool_loop` records `time.monotonic()` start time
- Stored as `_last_elapsed_val` after query completes
- Exposed via `_last_elapsed()` method
- Included as `elapsed_seconds` in Worker query result

### CheckSystem new queries
- `workers`: Reads workers.json from workspace dir, returns all definitions
- `worker/<name>`: Returns specific worker definition by name
- `running_workers`: Queries in-memory `_worker_registry`, returns status/elapsed
- `capabilities`: Combines agent_config (provider, model, tools) with OS, Docker, Git detection, plus capabilities.json
- `dockerfile`: Returns Dockerfile content from workspace dir
- `mcp_servers`: Returns mcp_servers.json config
- `my_config`: Now returns structured JSON with masked API key, raw_config fallback

## 2026-06-29 — ## Worker status reporting (IPC pattern)

Workers run as thr...

## Worker status reporting (IPC pattern)

Workers run as threads inside the agent process, but the web API backend runs as a **separate process**. To bridge status information, `worker.py` writes lightweight `status.json` files into `~/.thoughtmachine/workspaces/{ws_id}/workers/{name}/status.json`.

The web API (`workspace_routes.py:185-203`) reads these files when serving `GET /api/workspace/{ws_id}/workers` to populate `runtime_status`, `current_task`, `last_heartbeat`, and `error` fields.

Fields written:
- `runtime_status`: one of `"idle"`, `"running"`, `"completed"`, `"failed"` (mapped from `WorkerThread.status`)
- `current_task`: truncated query currently being processed (or null)
- `last_heartbeat`: ISO-8601 timestamp of last agent activity
- `error`: error message (or null)

This file is written atomically (temp file + `os.replace`) to avoid partial reads.

## 2026-07-01 — ## 2026-07-02 — Master Vault: Completed & Stable — Core Arch...

## 2026-07-02 — Master Vault: Completed & Stable — Core Architecture

### Tool System
- Tool interface (ToolResult, ToolSpec), ApplyEdits, search tools, FileEditor, DockerCodeRunner
- Tool output truncation (framework-level with opt-out)
- Error handling: clean error returns, no raw tracebacks exposed to agent

### Multi-Provider LLM System
- llm_providers/ with Anthropic, OpenAI, Google (Gemini), OpenRouter
- ProviderManager with automatic failover, unified completion interface
- Config-driven provider selection

### Hub Controller
- Central orchestrator: spawn, route events, manage sessions
- Handles config changes, WebSocket upgrades, agent-worker interactions
- EventBus abstraction with NullEventBus for testing

### Session Lifecycle
- SessionStore, SessionPersistence, session naming, save/load/rename/delete
- Sessions directory per-workspace (config/sessions/)

### Event Streaming
- EventBus → Bridge → WebSocket client
- System notifications, agent messages, tool calls/results
- Event deduplication and type filtering

### WebSocket Bridge
- Real-time bidirectional communication between backend agent and frontend
- Session isolation (per-tab bridges)
- Event types: agent_responded, tool_use, tool_result, system_notification, error, final

### UI Panels
- ChatPanel, ConfigPanel, SessionPanel, WorkspacePanel, ContainerPanel, PermissionPanel
- Component-based architecture with shared hooks (useSession, useBridge)
- Multi-tab session support with lazy WebSocket connection

## 2026-07-01 — ## 2026-07-02 — Master Vault: Completed & Stable — Worker Sy...

## 2026-07-02 — Master Vault: Completed & Stable — Worker System (Transplant)

### WorkerThread Implementation
- WorkerThread extends threading.Thread with dedicated agent loop
- Message-based IPC via queues: command queue (incoming) and event queue (outgoing)
- WorkerContext holds session_id, worker_name, permissions, timeout_seconds
- WorkerToolUseLoop: tool selection, execution, result collection
- Tool stripping: Workers get a restricted toolset (no DockerCodeRunner, no Write, limited permissions)
- Worker permissions model: read, write, execute, network with string levels ('allow', 'deny', 'ask')

### Worker Status & Lifecycle
- Status enum: IDLE, RUNNING, WAITING, COMPLETED, FAILED, TERMINATED
- Worker state machine: spawn → running → paused/completed/terminated
- Workspace-aware path resolution for worker tools
- Worker tool safety assessment completed (all tools mapped)

### Worker IPC Events
- worker_spawned, worker_output, worker_tool_use, worker_tool_result, worker_status_change, worker_completed, worker_error
- Real-time status reporting via WorkerDot (green=idle, yellow=running, red=error, grey=inactive)
- Polling-based context size updates (limitation: not streaming)

### Worker Tool Safety
- All tools assessed for worker safety
- ReadFile: path traversal protection
- Write operations: restricted for workers
- DockerCodeRunner: excluded from worker toolset
- MCP tools: excluded from worker toolset

## 2026-07-01 — ## 2026-07-02 — Master Vault: Completed & Stable — GUI Worke...

## 2026-07-02 — Master Vault: Completed & Stable — GUI Worker Panel

### WorkerOutputPanel
- React component showing worker stdout/stderr in real-time
- Auto-opens when worker spawns (configurable)
- Manual close + reopen support (reopening reconnects to event stream)
- Resize bug fixed: panel height recalculated on content change
- Event Viewer tab: raw worker event stream display

### WorkerDot / WorkerStatusIndicator
- Color-coded status: green (idle/ready), yellow (running), red (error), grey (inactive/stopped)
- Frontend status mapping fixed (running/idle → proper enum values)

### Worker Lifecycle (GUI)
- Spawn: bridge.spawn_worker() → backend creates WorkerThread → GUI shows output panel
- Stop: bridge.stop_worker() → TERMINATED signal → WorkerDot goes grey
- Resume: Wire bridge.resume_worker() into Worker tool's spawn action (DONE)
- Pre-configured workers auto-open output panel on session start

### Known Worker GUI Limitations
- Worker context size updated via polling, not streaming
- Worker panel auto-opening from other tabs required extra event routing

## 2026-07-01 — ## Item 1: Worker Panel State Memory (2025-03-30)

**Problem...

## Item 1: Worker Panel State Memory (2025-03-30)
## Item 1: Worker Panel State Memory (2025-03-30)

**Problem**: Worker panel state was global — switching tabs lost which worker panel was open.

**Solution**: Replaced global `selectedWorker` state with per-session `workerPanelState` map (`{ [sessionId]: { name, workspaceId } | null }`). Derived `selectedWorker` from `activeSessionId`. Persisted in localStorage under key `'workerPanelState'`.

**Key details**:
- `selectedWorker` is now a derived value, computed right after state/ref declarations (before any hooks) to avoid Temporal Dead Zone errors.
- Stale keys are garbage-collected when tabs are removed via a `useEffect` on `tabs`.
- Both `handleSelectWorker` and `handleCloseWorkerPanel` now use `activeSessionId` to key into the map.
- `WorkerAutoOpenWatcher` auto-open works correctly per session since it saves under the active session ID.

**TDZ Fix (2025-03-30)**: `activeSessionId` derivation was moved from after all hooks (line 493) to before any hooks (after state/ref declarations). The `useCallback` dependency arrays `[activeSessionId]` in `handleSelectWorker`/`handleCloseWorkerPanel` are evaluated during render, so `activeSessionId` must be initialized before those hooks run.

## 2026-07-01 — ## Error Boundary Layer (2025-03-30)

**Added**: `web_ui/fro...

## Error Boundary Layer (2025-03-30)

**Added**: `web_ui/frontend/src/components/ErrorBoundary.jsx` wraps `<App />` in `main.jsx`

**Purpose**: Catches any render-phase error (like the TDZ bug that produced a blank white page) and displays a fallback UI with error message, stack trace, and a reload button. Prevents the "blank screen of death" that previously required the user to switch to a stable branch to recover.

**Design**: Class component (React error boundaries must be class components). Inline styles only — no external dependencies. Shows the error message in a monospaced `<pre>` block and a Reload button that calls `window.location.reload()`.

**Commit**: `08f3de0`

## Workspace REST API

## 2026-07-01 — ### Package 2 — Worker CRUD + Dockerfile PUT (2026-07-02)

N...

### Package 2 — Worker CRUD + Dockerfile PUT (2026-07-02)

New endpoints in `web_ui/backend/workspace_routes.py`:

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/workspace/{ws_id}/workers` | Create worker definition (201, 409 on dup) |
| PUT | `/api/workspace/{ws_id}/workers/{name}` | Update worker definition |
| DELETE | `/api/workspace/{ws_id}/workers/{name}` | Delete worker definition (204) |
| PUT | `/api/workspace/{ws_id}/dockerfile` | Atomically write Dockerfile raw text |

GET `/api/workspace/{ws_id}/workers` enhanced with `?name=` query param for single-worker lookup (returns merged config+runtime, or 404).

All writes use the atomic tempfile+os.replace pattern (`_atomic_write_json` or new `_atomic_write_text`).


## 2026-07-02 — WorkerManagementPanel (new component) replaces WorkersSectio...

WorkerManagementPanel (new component) replaces WorkersSection:
- Located at `web_ui/frontend/src/components/WorkerManagementPanel.jsx`
- Consumed by WorkspacePanel.jsx in the Workers section
- Shows all worker definitions (from workers.json) merged with runtime status
- CRUD operations via API: POST/PUT/DELETE /api/workspace/{ws_id}/workers
- Template picker uses GET /api/workspace/templates
- Reuses the auto-open/stop/dismiss logic from old WorkersSection
- Uses inline Catppuccin styles matching the rest of the panel

## 2026-07-02 — ## DockerfileEditor (new component, 2026-07-02)

Created `we...

## DockerfileEditor (new component, 2026-07-02)

Created `web_ui/frontend/src/components/DockerfileEditor.jsx` (194 lines), integrated into `WorkspacePanel.jsx`.

### What it replaces
The old `DockerfileSection` was a read-only `<pre>` block that fetched the Dockerfile via `GET /api/workspace/{id}/dockerfile` and displayed it. No editing, no saving.

### New capabilities
- **Editable textarea** (20 rows, monospace, dark background, spellcheck off)
- **Change detection** — tracks `lastSaved` content separately from current textarea value; shows a persistent amber warning banner when they diverge: *"You've changed the Dockerfile. Rebuild the container for changes to take effect."*
- **Save via PUT** — sends `PUT /api/workspace/{workspaceId}/dockerfile` with `Content-Type: text/plain` and the raw text as body. On success, updates `lastSaved` + shows a "Last saved" timestamp. On error, shows inline error.
- **Smart disabled state** — Save button is disabled/greyed when there are no unsaved changes
- **404 → empty editor** — If no custom Dockerfile exists yet, opens with an empty textarea (instead of showing "(No custom Dockerfile)" placeholder text)
- **Loading / error / retry** — Same pattern as other panels (loading text, error with Retry button)

### API endpoints consumed
| Endpoint | Usage |
|---|---|
| `GET /api/workspace/{workspaceId}/dockerfile` | Fetch current Dockerfile (404 = empty) |
| `PUT /api/workspace/{workspaceId}/dockerfile` | Save Dockerfile (raw text body, text/plain) |

### Styling
Inline Catppuccin matching existing panels. Warning banner uses `rgba(249, 226, 175, 0.15)` background + `#f9e2af` border/text. Save button uses the same `#89b4fa` accent as DomainAllowlist.

## 2026-07-02 — ## DomainAllowlistEditor (new component, 2026-07-02)

Create...

## DomainAllowlistEditor (new component, 2026-07-02)

Created `web_ui/frontend/src/components/DomainAllowlistEditor.jsx` (305 lines), integrated into `WorkspacePanel.jsx`.

### What it replaces
The old `DomainAllowlistSection` was a free-text `<textarea>` that joined domains with newlines, requiring users to type domains manually. Save sent `PUT` with `{"domains": [...]}`.

### New capabilities
- **Structured list display** — Each domain shown as a row (monospace font, alternating row backgrounds) with a **✕ remove button** on the right (red hover effect).
- **Add domain input** — Text input + "Add" button at the bottom. Enter key also triggers add. Validation: non-empty, no duplicates (case-insensitive). Auto-refocuses input after adding.
- **Empty state** — Shows italic "No domains in allowlist. Add one below." when list is empty.
- **Unsaved changes indicator** — Amber dot + "Unsaved changes" text when current list differs from last saved (compares length + element equality).
- **Smart Save button** — Disabled/greyed when no unsaved changes.
- **Loading / error / retry** — Same pattern as DockerfileEditor and other panels.
- **Validation errors** — Shown inline in amber (`#f9e2af`) below the add input.

### API endpoints consumed
| Endpoint | Usage |
|---|---|
| `GET /api/workspace/{workspaceId}/domain_allowlist` | Fetch current allowlist (JSON array) |
| `PUT /api/workspace/{workspaceId}/domain_allowlist` | Save allowlist (`{"domains": [...]}`, `application/json`) |

### Styling
Inline Catppuccin matching existing panels. Same add/save button patterns as DockerfileEditor.

## 2026-07-03 — ## 2026-07-04 — Worker templates merged into workers.json on...

## 2026-07-04 — Worker templates merged into workers.json on bootstrap

`ensure_workspace_dirs()` in `thoughtmachine/workspace_capabilities.py` now writes echo + coder + researcher + reviewer workers from templates.

Key design decisions:
- Manual `_validate_worker_dict()` avoids circular import from `agent.__init__`
- Template load order: `~/.thoughtmachine/worker_templates/` → `resources/worker_templates/`
- Dedup by `name` field; echo always first
- Atomic write via `.tmp` + `os.replace`
- `setup_workspace.py` updated to not overwrite existing workers.json
- 13 new tests in `tests/workspace/test_worker_templates.py`

## 2026-07-05 — ## Workspace Opening Flow (End to End)

### Architecture Lay...

## Workspace Opening Flow (End to End)

### Architecture Layers
1. **Frontend** — React (`App.jsx`, `SessionTab.jsx`, `ConfigPanel.jsx`, `WorkspacePanel.jsx`)
2. **Backend WebSocket (/ws)** — `server.py` → `bridge.py` (WebAgentBridge) → Agent
3. **Backend REST (/api/...)** — `server.py` (browse) + `workspace_routes.py` (workspace files)
4. **Workspace Storage** — `~/.thoughtmachine/workspaces/{workspace_id}/` (Dockerfile, capabilities.json, workers.json, domain_allowlist.json, mcp_servers.json, config.json)
5. **Session Storage** — `~/.thoughtmachine/sessions/` (FileSystemSessionStore)

### Two WebSocket Connections Per Session
- **Hub WS** (in App.jsx) — session management only: list_sessions, new_session, load_session, close_session, delete_session, rename_session, get_open_sessions
- **Tab WS** (in SessionTab.jsx) — agent interaction: start_session, continue_session, pause/resume/stop, get_config, apply_config, get_conversation, get_providers, save_provider, security_response, bootstrap_workspace, get_workspace_capabilities, rebuild_container

### Full Flow: Browse → Select Folder → Apply Config → New Session → Bootstrap Workspace

**Step 1: User clicks "Browse" button** (ConfigPanel.jsx, General tab, line 407-435)
- Fetches `GET /api/browse?path=...` from server.py's REST endpoint (line 1326-1355)
- Lists directory contents in DirectoryBrowser modal

**Step 2: User clicks "Select This Folder"** (ConfigPanel.jsx, line 145/825)
- Sets `draft.workspace_path = selectedPath` (line 826)

**Step 3: User clicks "Apply"** (ConfigPanel.jsx, line 840-844)
- Sends WebSocket command `apply_config` with full draft config including workspace_path (line 844)
- Server processes via `_translate_frontend_config()` then `bridge.apply_config()`
- Bridge persists config to `~/.thoughtmachine/agent_config.json`
- `AgentConfig` model stores workspace_path; workspace_id is derived later

**Step 4: Hub WS receives session_loaded → opens new tab** (App.jsx)
- User clicks "+" tab → `hubSend('new_session')` (App.jsx line 373)
- Hub WS handler in server.py (line 1085-1169):
  1. Saves/clears old bridge
  2. Creates fresh bridge + controller
  3. Extracts `workspace_id` from message (optional, frontend doesn't send it)
  4. Fallback: calls `_resolve_workspace_id(_project_root)` from bridge.py
  5. _resolve_workspace_id scans `~/.thoughtmachine/workspaces/*/config.json` for matching root
  6. Creates new Session() object, assigns workspace_id if found
  7. Caches bridge in `_session_bridges[session_id]`
  8. Saves session via FileSystemSessionStore
  9. Sends `session_loaded` with session_id, session_name, workspace_id

**Step 5: SessionTab connects and user types first query** (SessionTab.jsx)
- SessionTab opens its own WebSocket connection
- Sends `continue_session` with the query
- bridge.start() reads config (including workspace_path) from agent_config.json
- During Agent initialization, workspace is resolved from workspace_path
- Bootstrap: `ensure_workspace_dirs(ws_id)` creates ~/.thoughtmachine/workspaces/{id}/ with Dockerfile, capabilities.json, workers.json, domain_allowlist.json, mcp_servers.json

### Key Files
- `server.py` — WebSocket hub + tab endpoint, REST browse endpoints, config handling
- `bridge.py` — WebAgentBridge, `_resolve_workspace_id()` cache, session lifecycle
- `workspace_routes.py` — REST API for Dockerfile, domain_allowlist, workers, permissions
- `workspace_capabilities.py` — `_workspace_dir()`, `ensure_workspace_dirs()`, `resolve_workspace_id()`
- `ConfigPanel.jsx` — Browse UI, workspace path selection, apply config
- `WorkspacePanel.jsx` — Dockerfile editor, domain allowlist, workers, permissions display
- `App.jsx` — Hub WS management, tab creation via new_session
- `SessionTab.jsx` — Tab WS connection, agent interaction


## 2026-07-09 — ## Session-Scoped Worker Registry (2025-07-17)

### Change S...

## Session-Scoped Worker Registry (2025-07-17)

### Change Summary
- `_worker_registry` keys changed from `str` (worker_name) to `tuple(session_id, worker_name)`
- Worker directory path: `workers/<session_id>/<name>` (when session_id provided), `workers/<name>` (legacy, backward compatible)
- All 5 action methods (list/spawn/check/query/stop) use composite tuple key with `self.session_id or ""`
- `shutdown_workers()` iterates tuple keys, extracting worker name with `key[1]`
- `check_system._query_running_workers()` updated to extract name/session_id from tuple keys
- `get_workers()` in workspace_routes.py updated to handle both legacy and session-scoped directory structures

### Backward Compatibility
- Workers spawned without `session_id` use legacy path `workers/<name>` and registry key `("", name)`
- All existing tests continue to work because they don't pass `session_id`
- Tests patching `_worker_registry` with `{}` still work (tuple key `("", "coder")` not found in empty dict)

### Task 1: send_query() Improvement
- Added fallback to read `last_heartbeat` from `status.json` when in-memory value is None
- Improved `TimeoutError` message to include heartbeat age if available

## 2026-07-10 — ## Research Report: Git Permissions, CheckSystem, Config Loa...

## Research Report: Git Permissions, CheckSystem, Config Loading & Layering (2026-07-10)

Researched 4 questions about the codebase. Full report below.

## 2026-07-11 — ## Logging Architecture (Comprehensive Landscape)

### Overv...

## Logging Architecture (Comprehensive Landscape)

### Overview
ThoughtMachine has **three logging systems** coexisting:

1. **`agent/logging/unified.py`** (Primary facade) — Tag-based `log()` function with `debug/info/warning/error/critical` convenience functions. Provides console output filtering + forwarding to AgentLogger for JSONL file output. Runtime API: `set_log_tags()`, `set_log_level()`, `show_log_config()`. Console output format: `[HH:MM:SS] LEVEL [tag] message | data`.

2. **`agent/logging/__init__.py`** (AgentLogger / JSONL file logger) — `_AgentLogger` class with 40+ fine-grained event methods (log_agent_start, log_turn_start, log_tool_call, log_llm_request, etc). Writes structured JSONL to `logs/agent_<session_id>.jsonl`. 10MB rotation, 5 backups. Thread-safe with RLock. Performance metrics, resource utilization, token tracking built in.

3. **Python stdlib logging** (`logging.getLogger`) — Used in `session/`, `tools/`, `thoughtmachine/` modules. Each uses its own module-level logger.

### Log Levels
DEBUG (10), INFO (20), WARNING (30), ERROR (40), CRITICAL (50)

### Tag Naming Convention
`area.component` format. Areas: core (session, pruning, config, controller, context_builder, turn_transaction, token_counter), tools (file_editor, docker_code_runner, docker_executor, search), llm (anthropic, openai, stepfun), server (config, bridge), ui (presenter, output_panel, events), session (history_provider, context_builder).

### Log File Format
JSONL files in `logs/agent_<session_id>.jsonl`. Each line: `{"type": "...", "level": "...", "message": "...", "data": {...}, "turn": N, "total_input_tokens": N, "total_output_tokens": N, "timestamp": "...", "session_id": "...", "version": "1.0"}`

### 200+ log() Usage Sites
Files using `log('LEVEL', 'tag', 'msg')` pattern: agent/core/agent.py, agent/core/controller.py, agent/logging/unified.py, agent/tools/*.py, web_ui/backend/*.py, tools/workspace/worker.py, docker_executor.py. Many convenience function calls: `debug('tag','msg')`, `info('tag','msg')`, `warning('tag','msg')`, `error('tag','msg')`.

### Environment Variables (20+)
**Core:** TM_LOG_LEVEL (default: INFO), TM_LOG_TAGS (default: empty = WARNING+ only), THOUGHTMACHINE_DEBUG (firehose), AGENT_LOG_CATEGORIES
**Legacy Debug Flags:** DEBUG_CONTEXT, DEBUG_TURN_GROUPING, DEBUG_HISTORY_PROVIDER, DEBUG_PRUNE, PAUSE_DEBUG, DEBUG_OPENAI, DEBUG_EVENTBUS, DEBUG_EXECUTOR, DEBUG_TOOLS, DEBUG_WORKER, DEBUG_LANGCHAIN, DEBUG_STEPFUN
**Truncation:** TM_DEBUG_TRUNCATE_LENGTH (100), TM_TOOL_ARGUMENTS_TRUNCATE (100), TM_TOOL_RESULT_TRUNCATE (100), TM_RAW_RESPONSE_TRUNCATE (100), TM_CONSOLE_DATA_TRUNCATE (200), TM_CONVERSATION_CONTENT_TRUNCATE (10000), TM_DOCKER_OUTPUT_TRUNCATE (10000)
**File Logging:** TM_LOG_FILE_LEVEL (default: DEBUG), TM_LOG_DIR_MAX_MB (default: 50), TM_LOG_MAX_AGE_DAYS (default: 7)

### Console Filtering Logic
1. If TM_LOG_TAGS is empty → only WARNING+ shown
2. If TM_LOG_TAGS is set → matching tags shown at >= TM_LOG_LEVEL
3. Wildcard: `server.*` matches `server.config`, `server.bridge`, etc.
4. Per-component DEBUG_* env vars override everything
5. Runtime set_log_tags()/set_log_level() override env vars

### Truncation System
Two-stage: Type-specific limits truncate for JSONL, additional console-specific truncation for display. `truncate_hint` parameter in log() controls which limit applies.

### Frontend (React)
- **State:** Zustand store (useStore.js) — shared sessions list only
- **Architecture:** Multi-tab with hub WebSocket for session list + per-tab WebSocket for individual sessions
- **Key Components:** ConfigPanel.jsx (37KB — directory browser, provider management, permissions UI), WorkerOutputPanel.jsx (21KB), SessionTab.jsx, TabBar, SessionList, WorkerManagementPanel
- **No existing Settings/Logging panel** — gap identified

### Backend (FastAPI)
- WebSocket protocol: commands include `get_config`, `update_config`, `apply_config`
- REST API: `/api/config/reset`, `/api/config/mode`
- Bridge.py handles agent session lifecycle and event forwarding
- Workspace routes for worker management

### Known Gaps
- No frontend Settings/Logging panel
- 7+ step manual chain for adding new event types
- No formal shared event schema between backend and frontend
- Frontend has zero tests
- EventBus not thread-safe (plain dict, no lock)

## 2026-07-11 — COMPREHENSIVE CODE AUDIT REPORT — 12 Questions Answered

## ...

COMPREHENSIVE CODE AUDIT REPORT — 12 Questions Answered

## Q1: WorkerThread Architecture — Agent + WorkerContext + NullEventBus

**File: agent/core/agent.py (1455 lines) — Agent class**
- Agent is instantiated with `config`, optional `session` (Session or WorkerContext), and optional `event_bus`.
- `process_query(query)` is a **generator** that yields event dicts. The callers (WorkerThread or GUI) iterate over these events.
- During `__init__`, Agent creates: `ToolExecutor(tool_classes, config, None, logger, security_available, agent=self, event_bus=event_bus or global_event_bus)`.
- Agent imports `global_event_bus` from `agent.events` and passes it to ToolExecutor if no custom event_bus is provided.

**File: agent/core/worker_context.py (202 lines) — WorkerContext**
- Designed as a **lightweight Session surrogate** for sub-agent worker loops.
- Provides exactly the attributes Agent reads from Session: `session_id`, `user_history`, `total_input_tokens`, `total_output_tokens`, `conversation_version` (property), `conversation_hash`, `summary`, `updated_at`, `_get_next_seq()`, `_on_conversation_changed()`.
- `compact_after_summary()` implements conversation compaction after summarization.
- `to_persistable_dict()` / `from_persistable_dict()` for JSON persistence of worker state.
- Has NO ObservableList wrapping, NO file I/O, NO persistence callbacks.

**File: agent/events.py (521 lines) — NullEventBus (line 475)**
- `NullEventBus` provides the same interface as `EventBus` but silently discards all publishes.
- Its `ask()` method returns `"deny"` instantly — critical for worker sub-agents where no human is available to answer security prompts.
- Used when Agent is constructed without a real EventBus: `event_bus=event_bus or global_event_bus`. When no event_bus is passed, `global_event_bus` (a real EventBus) is used. NullEventBus would need to be explicitly passed.

**Key Finding:** The Agent constructor has `event_bus=None` default parameter. When constructing Agent for worker threads, callers should pass `NullEventBus()` explicitly to silence event publishing. The pattern is:
```python
agent = Agent(config, session=ctx, event_bus=NullEventBus())
```

## Q2: Event Flow — Process_query Yields vs EventBus

**File: agent/core/agent.py (process_query at line 742)**
- `process_query()` is a generator that **yields plain dict events** — NOT typed EventBus events.
- Events yielded include: `token_update`, `user_query`, `execution_state_change`, `session_state_change`, `turn_warning`, `time_warning`, `token_warning`, `turn`, `tool_call`, `tool_result`, `agent_responded`, `final`, `stopped`, `paused`, `error`, `rate_limit_warning`, `max_turns`.
- These events DO NOT flow through EventBus at all. They are yielded directly to the consumer.

**File: agent/events.py — EventBus (line 315)**
- EventBus is a separate pub/sub mechanism for loose coupling between components.
- ToolExecutor uses EventBus for security prompts: `event_bus=self._event_bus or global_event_bus`.
- The security gate's `check()` method publishes `SECURITY_PROMPT` events via EventBus and waits for a `SECURITY_RESPONSE` from the GUI.
- With NullEventBus (worker context), `ask()` returns `"deny"` instantly, so security prompts are always denied.

**File: agent/core/state.py — AgentState._create_event (line 69)**
- Warning events (token_warning, time_warning, turn_warning) are created via `ev.create_event()` which returns typed `BaseEvent` objects, then converted to legacy dict format via `ev.convert_to_legacy_format()`.
- These legacy-format dicts are yielded directly by process_query.

**Key Finding:** There are TWO distinct event paths:
1. **Generator yield path** (process_query → caller): All agent lifecycle events as plain dicts
2. **EventBus pub/sub path** (ToolExecutor → security gate → EventBus): Security prompts/responses only

## Q3: Bridge/Presenter Layer

**File: agent/presenter/state_bridge.py (375 lines) — StateBridge**
- Manages configuration loading/saving (as minimal diff overlay vs factory defaults).
- Creates AgentConfig from config dictionaries with provider profile resolution.
- Binds Session objects and syncs token totals between StateBridge and Session.
- Handles custom system_prompt file management (~/.thoughtmachine/custom_system_prompt.txt).
- NO separate `bridge.py` file exists; `state_bridge.py` is the bridge module.

**File: tests/test_bridge_permissions_sync.py**
- Tests permission synchronization between bridge and session layers.

## Q4: Session Management — WorkerContext vs Session

**File: agent/core/worker_context.py**
- Session attributes implemented: `session_id`, `worker_name`, `user_history` (plain list), `total_input_tokens`, `total_output_tokens`, `turn_count`, `summary`, `updated_at`, `conversation_version` (property), `conversation_hash`.
- `_on_conversation_changed()`: Increments version, recomputes hash, updates timestamp — mirrors Session behavior.
- `_get_next_seq()`: Returns and increments sequence counter.
- `compact_after_summary()`: Prunes old messages while preserving:
  - Leading system prompts (role='system' before first non-system message)
  - The latest summary message (dict with `'summary': True`)
  - All messages after the latest summary

**File: session/models.py** — Session dataclass (presumed)
- Uses ObservableList wrapping for user_history with change notification callbacks.
- Full persistence machinery (file I/O, serialization callbacks).

## Q5: Token/Time/Turn State Management

**File: agent/core/state.py (361 lines) — AgentState**
- Three independent state machines:
  - `TokenState`: LOW → WARNING → CRITICAL (based on `token_monitor_warning_threshold`, `token_monitor_critical_threshold`)
  - `TimeState`: LOW → WARNING → CRITICAL (based on `time_warning_threshold`, `timeout_seconds`)
  - `TurnState`: LOW → WARNING (based on `max_turns - 3`)
- Each state machine emits warning events on transitions to higher states.
- `restrictions_active` flag is set when any state becomes critical.
- `restriction_reason` records which state triggered restrictions ('token', 'timeout', 'turn').
- `get_allowed_tools()`: Returns ['Respond'] for timeout, ['Respond', 'SummarizeTool'] for token/turn.
- Only warns ONCE per new state (not on every check).

## Q6: Tool Execution and Security Gate

**File: agent/core/tool_executor.py — ToolExecutor**
- `execute_tool_calls()` processes tool calls from assistant messages.
- For each tool call, checks permissions via security gate:
  - Uses `event_bus=self._event_bus or global_event_bus`
  - Security gate's `check()` method uses EventBus to prompt for approval
  - With NullEventBus, `ask()` returns "deny" instantly
- Tools are restricted via `state.is_tool_allowed(tool_name)` before execution.

## Q7: Stop Check and Pause Mechanism

**File: agent/core/agent.py (process_query)**
- `stop_check`: A callable set on `config.stop_check`. Checked in the turn loop between iterations. When it returns True, a 'stopped' event is yielded with `stop_reason='stopped'`.
- `_pause_requested`: Set via `request_pause()`. Checked at three checkpoints:
  1. At the start of each turn
  2. After turn processing (before next LLM call)
  3. After final event
- When paused, yields 'paused' event and returns control to consumer.

## 2026-07-17 — ## 2026-07-17 — Worker-Level Pause/Resume Implementation

Th...

## 2026-07-17 — Worker-Level Pause/Resume Implementation

The worker-level pause/resume system wraps the agent-level `request_pause()` in a full lifecycle managed by `WorkerThread` (`tools/workspace/worker.py`):

**Two-layer signalling:**
1. **In-memory fast path:** `threading.Event` objects (`_pause_event`, `_resume_event`) for instant signalling within the same process
2. **Cross-process file path:** `command.json` with `{"action":"pause"}` or `{"action":"resume"}` for web UI → worker communication

**Checkpoints in the worker run loop (`run()`):**
- After `_run_tool_loop()` returns, checks `_pause_event.is_set()`
- If paused: saves context, sends response, blocks in `_resume_event.wait(1.0)` loop
- If stopped during pause: breaks outer loop (stop wins over pause)
- If resumed: sets status to "ready", clears resume event, continues loop

**API endpoints** in `workspace_routes.py`:
- `POST .../pause` — writes command.json + status.json, calls `thread.pause()`
- `POST .../resume` — writes command.json + status.json, calls `thread.resume()`

The full lifecycle is documented in the "Cooperative Pause/Resume" section of this KB.


## Q8: Error Handling

**File: agent/core/agent.py (process_query lines ~1020-1081)**
- Catches: `ProviderError`, `RateLimitExceeded`, `LLMError`, generic `Exception`
- Each yields an 'error' event dict with `error_type`, `message`, `traceback`, and `stop_reason='error'`
- Rate limiting: Exponential backoff (`rate_limit_backoff_factor=1.2`, max 60s). After all retries exhausted, yields 'stop_reason' with `stop_reason='rate_limit'`.
- Emergency retries: On token limit exceeded, the turn loop can retry (up to `_emergency_retries` limit).

## Q9: Conversation History Management

**File: agent/core/turn_transaction.py — TurnTransaction**
- Atomic commits: Messages are collected in a transaction during a turn.
- `commit_assistant_only()`: Commits assistant message to user_history before yielding events.
- `commit()`: Commits all messages (assistant + tool results) atomically.
- This ensures data is never lost even if consumer pauses between yielded events.

**File: agent/core/agent.py**
- `_add_to_conversation(msg)`: Adds message to conversation via `conversation_manager.add_message_to_session()`.
- Session's `user_history` (or WorkerContext's `user_history`) is the source of truth.
- ContextBuilder is used to build the LLM context window from conversation history.

## Q10: Summarization and Compaction

**File: tools/summarize_tool.py — SummarizeTool**
- Triggered when token state becomes CRITICAL (or explicitly by LLM).
- SummarizeTool generates a summary message with `'summary': True` and a context-cleared notification.
- After summarization, `WorkerContext.compact_after_summary()` prunes old messages.

**File: agent/core/worker_context.py (compact_after_summary)**
- Finds the last summary message in user_history.
- Preserves leading system prompts (role='system' before first non-system message).
- Removes all messages between the system prompts and the summary.
- Keeps the summary + all messages after it.
- Updates conversation_hash and conversation_version.

## Q11: Test Coverage

**File: tests/test_worker_agent_transplant.py (6 test classes)**
1. `TestSmokeMultiTurnTask` — Agent runs multiple turns with tool calls + final response
2. `TestResumeWorkerContinuesConversation` — Sequential process_query() calls preserve history in WorkerContext
3. `TestTimeoutEnforcesRestrictions` — Short timeout triggers CRITICAL time_state, soft restriction
4. `TestTokenCriticalTriggersSummarisation` — Low token threshold triggers CRITICAL token state
5. `TestStopFlagGracefulExit` — stop_check stops agent mid-execution
6. `TestGateDenialInstant` — Security gate denies via NullEventBus instantly
7. `TestCompactAfterSummary` (7 sub-tests) — WorkerContext.compact_after_summary() behavior

**Mock Strategy:** Uses `ScriptedProvider` (a custom `LLMProvider` subclass registered in `ProviderFactory`) that returns pre-configured `LLMResponse` objects. Monkey-patches `ScriptedProvider.__init__` to inject responses.

## Q12: Findings and Recommendations

### Strengths
1. **Clean separation**: Generator pattern (process_query yielding dicts) cleanly separates agent execution from event consumption.
2. **Well-tested**: 6 comprehensive test classes with ScriptedProvider mock covering all key scenarios.
3. **Dual event paths**: Generator yield for lifecycle + EventBus for security prompts is well-designed.
4. **Atomic commits**: TurnTransaction prevents data loss on pause.
5. **State machine design**: Independent token/time/turn states with restriction logic is robust.
6. **NullEventBus.ask()**: Returns "deny" instantly — correct for worker context.
7. **Compaction logic**: Well-tested with edge cases (multiple summaries, empty history, hash/version updates).

### Potential Issues
1. **EventBus default**: Agent.__init__ defaults `event_bus=None`, then uses `global_event_bus` (a real EventBus). Worker callers must explicitly pass `NullEventBus()`. If forgotten, security prompts will hang waiting for GUI response.
2. **Worker code location**: There is NO `worker.py` or dedicated WorkerThread file in the codebase. WorkerThread must be defined elsewhere or not yet migrated. The tests demonstrate the expected interface but the actual worker implementation may be in the `thoughtmachine` package (external to this project).
3. **_pending_events field**: AgentState has `_pending_events: List[Dict[str, Any]] = field(default_factory=list)` but this list is never populated or consumed in the code examined — dead code.
4. **Token estimation**: Process_query uses `self._estimate_tokens()` for system notifications/warnings added to conversation, which is rough but acceptable for tracking.
5. **Session vs WorkerContext property differences**: WorkerContext uses plain `user_history: list`, while Session wraps it in `ObservableList`. The conversation setter in Agent may call `session.user_history[:] = ...` which works for both types but WorkerContext lacks the change notification that ObservableList provides.

### Architectural Summary

```
WorkerThread (external)
    │
    │  creates
    ▼
WorkerContext(session_id) ──► Agent(config, session=ctx, event_bus=NullEventBus())
    │                              │
    │  process_query(query)        │  __init__ creates:
    │  yields event dicts ─────────┼──► ToolExecutor(tool_classes, config, state, event_bus=NullEventBus)
    │                              │       │
    │                              │       └──► Security Gate (via EventBus.ask() → "deny")
    │                              │
    │                              ├──► LLMClient → LLMProvider
    │                              ├──► ConversationManager
    │                              ├──► TokenCounter
    │                              └──► AgentState (token/time/turn state machines)
    │
    ▼
WorkerThread iterates events, handles responses
```

## NullEventBus

## 2026-07-11 — ## NullEventBus

**Defined in:** `agent/events.py` (line 475...

## NullEventBus

**Defined in:** `agent/events.py` (line 475) — no-op EventBus stub.

**Instantiation:** Single module-level singleton `_NULL_EVENT_BUS = NullEventBus()` in `tools/workspace/worker.py` (line ~73), guarded by try/except ImportError.

**Usage:** Passed as `event_bus=_NULL_EVENT_BUS` when constructing `Agent()` inside `WorkerThread.run()` (line ~361). All worker agents share the same instance.

**Behavior:** Silently discards all publishes; `ask()` returns `"deny"` instantly. This ensures workers cannot prompt the user for approval and all security prompts auto-deny.


## 2026-07-12 — ## Plan: Per-Worker EventBus bridge wiring

**Goal**: Wire p...

## Plan: Per-Worker EventBus bridge wiring

**Goal**: Wire per-worker EventBus events (TOOL_CALL, TOOL_RESULT, TOKEN_WARNING, ASSISTANT_MESSAGE, etc.) through to the WebSocket frontend via the bridge.

**Files to modify**:
1. `tools/workspace/worker.py` — add worker event bus registry + register/unregister in WorkerThread.run()
2. `web_ui/backend/bridge.py` — subscribe to per-worker bus on spawn events, unsubscribe on completion

**Step 1 — worker.py: Add bus registry** (near `_worker_registry`)
- Add module-level `_worker_event_bus_registry: Dict[Tuple[str, str], EventBus]` + `_bus_registry_lock`
- Add `register_worker_event_bus(session_id, worker_name, event_bus)`
- Add `unregister_worker_event_bus(session_id, worker_name)`
- Add `get_worker_event_bus(session_id, worker_name)`

**Step 2 — worker.py: Register/unregister in WorkerThread.run()**
- After `self._event_bus = EventBus()` is created (lazy agent creation block), call `register_worker_event_bus(self.session_id, self.worker_name, self._event_bus)`
- In the finally block (or at thread exit), call `unregister_worker_event_bus(self.session_id, self.worker_name)`

**Step 3 — bridge.py: Import registry functions + add per-worker subscription management**
- Import `get_worker_event_bus` from `tools.workspace.worker`
- Add `_worker_bus_subscriptions: Dict[str, Dict[str, Any]]` tracking dict (maps worker_name -> subs)
- In the WORKER_SPAWNED handler: look up per-worker bus via `get_worker_event_bus(session_id, worker_name)`, subscribe to events TOOL_CALL, TOOL_RESULT, TOKEN_WARNING, ASSISTANT_MESSAGE, WORKER_MESSAGE, etc., store subscription handles
- In WORKER_COMPLETED/WORKER_ERROR handler: unsubscribe from per-worker bus
- Extend `_unsubscribe_worker_events` to also clean up per-worker bus subs

**Step 4 — Test**: Verify imports work, verify subscriptions are created and cleaned up properly.

## 2026-07-12 — 
## Event Pipeline Architecture — Main Agent vs Worker Agent...


## Event Pipeline Architecture — Main Agent vs Worker Agent — 2026-07-12

### Status
First audit complete. Covers web_ui/backend/bridge.py, agent/events.py, agent/core/agent.py, agent/core/worker_context.py, and frontend event routing.

### Overview
The system has **two completely separate event paths** that converge at the Bridge and are forwarded to the frontend over a single WebSocket. This section documents both paths end-to-end, compares them, and identifies gaps.

---

### Path 1: Main Agent (Standalone / Controller) → Frontend

**Producer:** `agent/core/agent.py` — `process_query()` generator

**Format:** Raw Python `dict` objects (no typed EventType enum, no EventBus)

**Event types yielded (raw dicts):**
- `'execution_state_change'` — agent state transitions
- `'token_update'` — token count changes
- `'turn'` — turn count changes
- `'tool_call'` — tool call initiated (with tool name, args, call_id)
- `'tool_result'` — tool result received (with content, call_id)
- `'user_query'` — query received
- `'final'` — final response produced
- `'paused'` — agent paused (security prompt)
- `'stopped'` — agent stopped
- `'max_turns'` — max turns reached
- `'error'` — error occurred
- `'rate_limit_warning'` — rate limit warning
- `'stop_reason'` — LLM stop reason
- `'agent_responded'` — agent produced a response
- `'session_stop'` — session stopped
- `'security_prompt'` — security prompt triggered

**Transport:** Generator iteration (synchronous, blocking)

**Bridge handler:** Two paths:
- **Mode A (Standalone):** `_run_loop()` thread (line ~1780) pulls from `_query_queue`, iterates `self._agent.process_query(query)`, calls `_map_and_emit()` for each dict
- **Mode B (Controller):** `_on_controller_event()` callback (line ~1598) receives dicts from controller's agent, calls `_map_and_emit()`

**Mapping function:** `_map_and_emit()` (line ~1643) transforms dict types → frontend protocol:
| Raw dict type | Frontend event type | Data source |
|---|---|---|
| `execution_state_change` | `state_changed` | dict content |
| `token_update` | `tokens_updated` + `context_updated` | dict content |
| `turn` | `conversation_changed` | Session.user_history |
| `tool_call` | `conversation_changed` | Session.user_history |
| `tool_result` | `conversation_changed` | Session.user_history |
| `user_query` | `conversation_changed` | Session.user_history |
| `final` | `conversation_changed` | Session.user_history |
| `token_warning` | `conversation_changed` | Session.user_history |
| `turn_warning` | `conversation_changed` | Session.user_history |
| `agent_responded` | `conversation_changed` + `state_changed` | Session.user_history + dict |
| `error` | `status_message` + `conversation_changed` | dict + Session |
| `session_stop` | `state_changed` | dict content |
| `security_prompt` | `security_prompt` | dict content |

**Key design choice:** Conversation events always re-read `Session.user_history` — never use event dict content for message data.

**Frontend consumer:** `SessionTab.jsx` routes by event type:
- `state_changed` → agent state machine
- `tokens_updated` / `context_updated` → token/counters
- `conversation_changed` → `setMessages()` → re-render
- `status_message` → flash message
- `security_prompt` → security dialog

---

### Path 2: Worker Agent → Frontend

**Producer:** Worker tool executor code (`tools/workspace/worker.py` — not analyzed in this audit)

**Format:** Typed EventBus events (EventType enum + typed event classes + .data + .metadata)

**Event types published (typed):**
- `WORKER_SPAWNED` — WorkerSpawnedEvent
- `WORKER_STATUS` — WorkerStatusEvent
- `WORKER_COMPLETED` — WorkerCompletedEvent
- `WORKER_ERROR` — WorkerErrorEvent
- `TOKEN_WARNING` — TokenWarningEvent
- `WORKER_MESSAGE` — WorkerMessageEvent
- `TOOL_CALL` — ToolCallEvent
- `TOOL_RESULT` — ToolResultEvent
- `ASSISTANT_MESSAGE` — AssistantMessageEvent
- `SECURITY_PROMPT` — SecurityPromptEvent (potential)

**Transport:** EventBus pub/sub (asynchronous, non-blocking)
- Each worker publishes to its **per-worker EventBus** (keyed by `(session_id, worker_name)`)
- Lifecycle events are also published to `global_event_bus`

**Bridge subscriptions:**
1. **Global EventBus** subscriptions (~line 662):
   - `WORKER_SPAWNED` → `_on_worker_spawned()` — spawns per-worker bus subscription, forwards to frontend
   - `WORKER_STATUS` → forward as `'worker:worker_status'`
   - `WORKER_COMPLETED` → `_on_worker_completed()` — forward + cleanup
   - `WORKER_ERROR` → `_on_worker_error()` — forward + cleanup
   - `TOKEN_WARNING` → `_on_worker_token_warning()` — filter source starts with 'worker:', forward as `'worker:system_notification'`
   - `WORKER_MESSAGE` → forward as `'worker:worker_message'`
   - `SECURITY_PROMPT` → `_security_prompt_handler()` — forward as `'security_prompt'`

2. **Per-worker EventBus** subscriptions (~line 730):
   - `'tool_call'` → forward as `'worker:tool_call'`
   - `'tool_result'` → forward as `'worker:tool_result'`
   - `'token_warning'` → forward as `'worker:token_warning'`
   - `'worker_message'` → forward as `'worker:worker_message'`
   - `'assistant_message'` → forward as `'worker:assistant_message'`

**Filtering:** Bridge only forwards events where `data.get('session_id')` matches `self._session_id`

**Frontend consumer:**
- `SessionTab.jsx` detects `'worker:*'` prefix → dispatches to WorkerOutputPanel
- `WorkerOutputPanel.jsx` maintains worker state (running/completed/error), uses dual WebSocket + polling design
- `adaptWorkerEvent.js` transforms raw worker data into frontend message format (handles worker_name, role, content extraction)
- `MessageBubble.jsx` renders individual messages with role-based styling

---

### Comparison Table

| Aspect | Main Agent Path | Worker Agent Path |
|---|---|---|
| **Event format** | Raw dicts | Typed EventBus (EventType enum) |
| **Transport** | Generator iteration (sync) | EventBus pub/sub (async) |
| **Producer** | `agent/core/agent.py` | Worker executor code |
| **Bus used** | None | global_event_bus + per-worker bus |
| **Context proxy events** | N/A | None (worker_context.py publishes nothing) |
| **Bridge handling** | `_map_and_emit()` direct call | EventBus subscription callbacks |
| **Conversation source** | Session.user_history | Event .data dict |
| **Frontend routing** | By type name | 'worker:{type}' prefix |
| **Message rendering** | SessionTab.jsx directly | WorkerOutputPanel.jsx → MessageBubble.jsx |

---

### Data Flow Diagram (ASCII)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        MAIN AGENT PATH                                  │
│                                                                         │
│  Agent (process_query)      Bridge._run_loop()       Frontend           │
│  ┌─────────────────┐       ┌──────────────────┐     ┌──────────────┐   │
│  │ yield dict       │──────>│ _map_and_emit()   │────>│ SessionTab   │   │
│  │ {type, data, ...}│       │  dict → protocol  │     │ .jsx         │   │
│  │                  │       │  _emit() → ws.send│     │ setMessages()│   │
│  └─────────────────┘       └──────────────────┘     └──────────────┘   │
│         │                                                               │
│         │ Session.user_history (reads back)                             │
│         ▼                                                               │
│  ┌─────────────────┐                                                    │
│  │ Session          │                                                    │
│  │ (mutated in-     │                                                    │
│  │  place by agent) │                                                    │
│  └─────────────────┘                                                    │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                        WORKER AGENT PATH                                │
│                                                                         │
│  Worker Executor      EventBus              Bridge Subscriptions       │
│  ┌────────────────┐   ┌──────────┐         ┌────────────────────┐      │
│  │ publish(        │──>│ per-     │────────>│ _on_worker_spawned │      │
│  │  WORKER_SPAWNED │   │ worker   │         │ _on_worker_completed│     │
│  │  )              │   │ bus      │         │ _on_worker_error   │      │
│  └────────────────┘   └──────────┘         │ _on_token_warning  │      │
│         │                                   └────────┬───────────┘      │
│         │                                    ┌───────┴────────────┐     │
│         └────────────────────────────────────> _emit() → ws.send   │     │
│                                     worker:   │ 'worker:{type}'    │     │
│                                               └───────┬────────────┘     │
│                                                       │                  │
│                                                       ▼                  │
│                                               ┌──────────────────┐      │
│                                               │ Frontend:         │      │
│                                               │ SessionTab detects│      │
│                                               │ worker:* prefix   │      │
│                                               │ → dispatches to   │      │
│                                               │ WorkerOutputPanel │      │
│                                               │ → MessageBubble   │      │
│                                               └──────────────────┘      │
│                                                                         │
│  Global EventBus (lifecycle only)                                       │
│  ┌──────────────────────────────┐                                       │
│  │ WORKER_SPAWNED, COMPLETED,   │                                       │
│  │ ERROR, TOKEN_WARNING         │                                       │
│  └──────────────────────────────┘                                       │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                    PROCESS_QUERY YIELDS (raw dicts)                      │
│                                                                         │
│  {'type': 'execution_state_change', 'state': 'thinking'}                │
│  {'type': 'token_update', ...}                                          │
│  {'type': 'turn', 'turn': 1}                                            │
│  {'type': 'tool_call', 'name': 'ReadFile', ...}                         │
│  {'type': 'tool_result', 'name': 'ReadFile', ...}                       │
│  {'type': 'final', 'content': '...'}                                    │
│  ...                                                                     │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                    EVENTBUS EVENT TYPES (typed)                          │
│                                                                         │
│  EventType.WORKER_SPAWNED → WorkerSpawnedEvent                          │
│  EventType.WORKER_STATUS → WorkerStatusEvent                            │
│  EventType.WORKER_COMPLETED → WorkerCompletedEvent                      │
│  EventType.WORKER_ERROR → WorkerErrorEvent                              │
│  EventType.TOKEN_WARNING → TokenWarningEvent                            │
│  EventType.WORKER_MESSAGE → WorkerMessageEvent                          │
│  EventType.TOOL_CALL → ToolCallEvent                                    │
│  EventType.TOOL_RESULT → ToolResultEvent                                │
│  EventType.ASSISTANT_MESSAGE → AssistantMessageEvent                    │
│  EventType.SECURITY_PROMPT → SecurityPromptEvent                        │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│              BRIDGE MAPPING (raw dict → frontend protocol)               │
│                                                                         │
│  execution_state_change → state_changed                                 │
│  token_update → tokens_updated + context_updated                        │
│  turn / tool_call / tool_result /                                       │
│  user_query / final → conversation_changed (from Session)              │
│  agent_responded → conversation_changed + state_changed                 │
│  error → status_message + conversation_changed                          │
│  session_stop → state_changed                                           │
│  security_prompt → security_prompt (pass-through)                       │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│              FRONTEND EVENT ROUTING (SessionTab.jsx)                     │
│                                                                         │
│  state_changed → updateAgentState(state)                                │
│  tokens_updated → updateTokenCounters(data)                             │
│  context_updated → updateContextLength(data)                            │
│  conversation_changed → setMessages(session.user_history)               │
│  status_message → showFlash(message, type)                              │
│  worker:* → handler → WorkerOutputPanel                                 │
│  security_prompt → showSecurityDialog()                                 │
└─────────────────────────────────────────────────────────────────────────┘
```

---

### Gaps, Inconsistencies & Improvement Areas

#### 1. Dual Event Protocol
**Problem:** Main agent uses raw dicts; workers use typed EventBus events. Two different protocols must be maintained and understood.
**Risk:** New developers must learn both systems. The mapping code in `_map_and_emit()` is the only bridge between raw dict format and frontend protocol.
**Suggestion:** Consider migrating main agent events to use EventBus for consistency. This would require adding EventType values for main agent events and changing `process_query()` to publish typed events instead of yielding dicts.

#### 2. Conversation Source Inconsistency
**Problem:** Main agent conversation events read from `Session.user_history` (re-read on every event). Worker conversation events carry message data inline in the event dict.
**Risk:** If `Session.user_history` is out of sync or delayed, the frontend may show stale data for main agent events. Worker events bypass Session entirely for message content.
**Suggestion:** Align approaches — either have all events carry inline message content, or have all events reference Session. The former is more robust for real-time updates.

#### 3. No Typed Events for Main Agent
**Problem:** Main agent events are untyped dicts with string 'type' keys. No schema validation, no metadata, no source tracking.
**Risk:** A typo in a dict key ('execution_state' vs 'execution_state_change') would silently fail to propagate to frontend.
**Suggestion:** Define a `MainAgentEvent` TypedDict or use the EventBus Event base class for main agent events.

#### 4. Potential Duplicate Events from Dual Bus Subscriptions
**Problem:** The bridge subscribes to both global_event_bus (for lifecycle events like WORKER_COMPLETED) and per-worker buses (for detail events). If a lifecycle event is published to both buses, the bridge could receive and forward it twice.
**Risk:** Duplicate events on the frontend (e.g., two "worker completed" messages).
**Suggestion:** Audit whether lifecycle events are published to both buses. If so, either deduplicate at the bridge or ensure each bus has exclusive event types.

#### 5. Worker Context Proxy publishes No Events
**Problem:** `worker_context.py` is a lightweight Session proxy that does not publish events. The worker tool executor code (`tools/workspace/worker.py`) is responsible for publishing. This separation is undocumented.
**Risk:** If someone modifies worker_context.py to add event publishing, they might produce duplicate events (or miss events they expect).
**Suggestion:** Document the responsibility split clearly, or move event publishing into the context proxy for consistency.

#### 6. Session as Single Source of Truth vs Event Stream
**Problem:** The main agent path fundamentally relies on Session.user_history as the authoritative conversation store. The event stream is a side effect of Session mutations. This creates a hidden dependency — the frontend doesn't "follow events" for conversation; it re-reads the Session.
**Risk:** If the event stream is processed faster than Session writes, the frontend could see stale data. Currently this is unlikely because the generator is synchronous, but if async is introduced, timing bugs could appear.
**Suggestion:** Add explicit synchronization or switch to event-carried message data for conversation updates.

#### 7. Security Prompt Event Path Ambiguity
**Problem:** The main agent's `process_query()` can yield `'security_prompt'` as a raw dict (handled by `_map_and_emit`). The docstring also mentions SecurityPromptEvent from global_event_bus. It's unclear if both paths are active or if one is dead.
**Suggestion:** Audit whether the tool executor publishes SecurityPromptEvent to EventBus. If not, remove the EventBus subscription or add the publishing logic.

#### 8. Frontend Dual-Channel for Workers
**Problem:** WorkerOutputPanel.jsx uses both WebSocket (event-driven) and polling (HTTP GET) for worker output. The polling path is a fallback, but its purpose and trigger conditions are not well-documented.
**Risk:** If WebSocket events are lost, polling may show stale data. If both paths deliver the same data, the frontend may show duplicates.
**Suggestion:** Document when polling is activated and ensure deduplication logic exists.

## Worker System Architecture

## 2026-07-12 — ## Worker Context Accumulation — Investigation (2026-07-12)
...

## Worker Context Accumulation — Investigation (2026-07-12)

**Bug report context was lost during summarization.** Re-investigated the worker system to understand how context persists across queries.

### Architecture Summary

**File:** `tools/workspace/worker.py` (1,935 lines, ~82KB)

1. **Worker class (ToolBase)** — Lines 1207-1935. Registered in `TOOL_CLASSES`. Actions: spawn, check, query, stop, list.

2. **WorkerThread class** — Lines 507-1200. A `threading.Thread` with a persistent event loop:
   - `run()` (line ~1030): loops on `_input_queue.get(timeout=2.0)`
   - Creates Agent lazily on first query (reused for subsequent queries)
   - Uses `WorkerContext` (from `agent/core/worker_context.py`) as a Session surrogate
   - Persists context to `<workspace_dir>/workers/<name>/context.json`

3. **Spawn flow** (`_action_spawn`, line 1458):
   - Creates `WorkerThread`, sets `_initial_context`, starts thread
   - When `context` is provided: deletes stale `context.json` (lines 1620-1623) for a fresh start
   - If context has `"query"` key: auto-queues it and waits for response (lines 1650-1678)
   - Registers thread in `_worker_registry` with key `(session_key, worker_name)`

4. **Query flow** (`_action_query`, line 1759):
   - Looks up thread by `(session_key, worker_name)` in `_worker_registry`
   - Calls `thread.send_query()` → puts query on `_input_queue` → blocks on `_output_queue`
   - Thread's event loop picks up query, processes it with the SAME Agent + WorkerContext

5. **Context persistence:**
   - `_load_context()` (line 1055): reads `context.json` → `WorkerContext.from_persistable_dict()`
   - `_save_context()` (line 1113): writes to `context.json` atomically (temp file + rename)
   - Context saved at end of `run()` in `finally` block and on `stop()`

### Key Finding: Context Accumulates Across Queries

When the main agent calls:
1. `Worker(action="spawn", worker_name="default", context={"query": "do X"})` → spawns thread, fresh WorkerContext, processes query, thread stays alive
2. `Worker(action="query", worker_name="default", worker_query="now do Y")` → **same thread**, **same Agent**, **same WorkerContext** → context accumulates

The registry key `(session_id, worker_name)` prevents duplicate spawns sharing the same key.

### Not Yet Investigated
- How `agent/core/agent.py` processes queries with WorkerContext (the actual context accumulation mechanism at the Agent level)
- The specific bug from the original report (text was lost during summarization)
- Whether CheckSystem uses a different worker mechanism


## 2026-07-12 — ## System Notification Event Flow

### Agent-side (agent/cor...

## System Notification Event Flow

### Agent-side (agent/core/agent.py)
The agent does **NOT** yield `system_notification` type events from `process_query()`. Instead, system notifications are injected as `Message` objects with:
- `role='user'`
- `content='[SYSTEM NOTIFICATION] ...'`
- `is_system_notification=True` (dict key)

**Injection sites:**
1. **Token warnings** (line 671): Buffered in `_pending_warnings`, flushed after `turn_transaction.commit()` (line 1183-1186)
2. **Turn warnings** (line 890): Direct `_add_to_conversation()` call during turn state update
3. **Time warnings** (line 872): Direct `_add_to_conversation()` call during time state update
4. **Config changes** (line 299): Direct `_add_to_conversation()` call
5. **Config change failures** (line 780): Direct `_add_to_conversation()` call
6. **LLM errors** (lines 1037, 1055, 1070, 1083): In error handlers for token_limit, rate_limit, provider_error, unexpected_error
7. **Post-summarization** (lines 1328, 1380): `context_cleared_msg` appended to user_history

### Worker-side (tools/workspace/worker.py)
Worker uses typed EventBus events:
- **Per-worker EventBus**: tool_call, tool_result, token_warning, worker_message, assistant_message
- **Global EventBus**: WORKER_SPAWNED, WORKER_STATUS, WORKER_COMPLETED, WORKER_ERROR, TOKEN_WARNING, WORKER_MESSAGE, SECURITY_PROMPT
- TOKEN_WARNING on global bus is forwarded as `worker:system_notification` if source starts with 'worker:'

### Bridge (web_ui/backend/bridge.py)
Two event paths:
1. **Main Agent**: Generator iteration → `_map_and_emit()` → re-reads Session.user_history → sends `conversation_changed` events
2. **Worker Agent**: EventBus subscriptions → typed event handlers → `_emit()` → sends `worker:{type}` events

### Frontend (SessionTab.jsx)
Detects `'worker:*'` prefix → dispatches to WorkerOutputPanel. Handles: tool_call, tool_result, token_warning, turn_warning, time_warning, assistant_message, worker_spawned, worker_status, worker_completed, worker_error, system_notification, user_message, worker_message.

## 2026-07-12 — ## Worker Lifecycle — Complete End-to-End Flow

### Architec...

## Worker Lifecycle — Complete End-to-End Flow

### Architecture Overview
The worker system has **3 layers**:
1. **Frontend (React):** Sends `start_session`/`continue_session` — NEVER sends `worker:spawn`/`worker:query` directly
2. **Backend Bridge + Server:** Handles WS commands, creates Agent session, subscribes to worker events from global_event_bus
3. **Worker Tool + WorkerThread:** The main agent's `Worker` tool (in tools/workspace/worker.py) spawns/checks/queries worker threads

### Data Flow
```
User types message
  → QueryBar.jsx: sends 'start_session' or 'continue_session' via WebSocket
  → server.py: routes to bridge.start() or bridge.continue_session()
  → bridge.py: creates AgentController + Agent, runs process_query()
  → Agent (main agent): when it decides to delegate, calls Worker tool
  → Worker.execute() (tools/workspace/worker.py):
      - action="spawn": creates WorkerThread, registers in _worker_registry
      - action="query": thread.send_query() blocks until response
  → WorkerThread.run(): creates its own Agent in a daemon thread
      - Uses own WorkerContext persisted to <workspace_dir>/workers/<session_id>/<name>/context.json
      - Runs _run_tool_loop() for each query
      - Publishes events to per-worker EventBus + global_event_bus
  → bridge.py: subscribed to global_event_bus, forwards worker:* events to frontend
  → WorkerOutputPanel.jsx: displays worker output events
```

### Key Components

**WorkerThread (tools/workspace/worker.py):**
- Daemon thread with its own Agent instance
- Inter-thread communication: `_input_queue` (receive queries) + `_output_queue` (return responses)
- Lifecycle: ready → busy → ready (per query), or → completed/error
- Context persisted at `<workspace_dir>/workers/<session_id>/<name>/context.json`
- On `run()`: loads context from disk, creates WorkerContext, merges _initial_context
- Agent created lazily on first query
- Publishes WORKER_SPAWNED, WORKER_STATUS, WORKER_COMPLETED, WORKER_ERROR to global_event_bus

**Worker class (ToolBase subclass):**
- Actions: list, spawn, check, query, stop
- `_action_spawn()`: finds definition in workers.json, builds agent config, creates WorkerThread, optionally waits for auto-query result
- `_action_query()`: calls thread.send_query(), blocks for response
- Threads registered in `_worker_registry` keyed by `(session_id, worker_name)`
- Workers excluded from spawning other workers (Worker in blocklist)

**bridge.py:**
- `_subscribe_to_worker_events()`: subscribes to WORKER_SPAWNED, WORKER_STATUS, WORKER_COMPLETED, WORKER_ERROR, TOKEN_WARNING, WORKER_MESSAGE on global_event_bus
- Forwards events as worker:* type messages to frontend WebSocket
- `close_session()` calls `shutdown_workers()` to persist contexts

### Worker Context Persistence
- Directory: `<workspace_dir>/workers/<session_id>/<worker_name>/`
- Files: context.json, status.json, command.json (for external stop)
- _load_context(): reads context.json, deserializes via WorkerContext.from_persistable_dict()
- _save_context(): atomic write via tempfile+os.replace

### Session ID Flow
- SessionTab creates a new session_id (or loads existing) via WebSocket
- Server creates Session model, stores it, passes to bridge
- Bridge holds _session_id and passes it to Worker tool via session_id field
- WorkerThread creates directory: workspace_dir/workers/<session_id>/<name>/

## 2026-07-12 — ## Token Warning Event Pipeline (Bridge → Frontend)

### Pip...

## Token Warning Event Pipeline (Bridge → Frontend)

### Pipeline Overview

The `token_warning` event reaches the frontend through **three independent paths**, depending on context:

### Path 1: Standalone Agent Loop (`_run_loop` → `_map_and_emit`)
- **File:** `web_ui/backend/bridge.py`, lines 1804-1871
- Agent yields raw event dicts from `process_query()` in the bridge's background thread (`_run_loop`)
- Each raw event is passed to `_map_and_emit()` (line 1833)
- `_map_and_emit` (line 1729) checks: `if event_type in ("token_warning", "turn_warning")`
- Triggers: `conversation_changed` event with full session history snapshot
- **Final frontend type:** `conversation_changed`

### Path 2: Controller Mode (`_on_controller_event` → `_map_and_emit`)
- **File:** `web_ui/backend/bridge.py`, lines 1622-1665
- When bridge uses `AgentController`, the controller sends events via `_on_controller_event`
- Same `_map_and_emit()` is called (line 1665)
- Identical mapping as Path 1
- Also captures session ID and propagates session object from controller's agent

### Path 3: Event Bus → Worker Token Warning
- **Global bus handler** (`_on_worker_token_warning`, line ~1189): Subscribes to `EventType.TOKEN_WARNING` on the global event bus
- Filters: only handles events with `metadata.source.startswith("worker:")`
- Forwards as: `worker:system_notification` with `response.type='token_warning'`
- **Final frontend type:** `worker:system_notification` (with nested `response.type='token_warning'`)

### Path 4: Event Bus → Per-Worker Bus (Detailed Worker Events)
- **Per-worker bus subscription** (`_subscribe_to_worker_bus`, line ~1300): When a worker is spawned, bridge subscribes to the worker's per-worker EventBus
- Subscribed event types include: `token_warning`, `turn_warning`, `time_warning`, `tool_call`, `tool_result`, etc.
- Handler (`_make_bus_handler`) forwards as `worker:{original_type}` (e.g., `worker:token_warning`)
- **Final frontend type:** `worker:token_warning`

### Key Difference: Paths 3 vs 4
- Path 3 (`_on_worker_token_warning` + global bus): Only captures **worker-sourced** warnings (source starts with "worker:"). Emits as `worker:system_notification` with structured `response` payload.
- Path 4 (per-worker bus): Captures **all** token warnings from the worker's own bus. Emits directly as `worker:token_warning`.

### Cross-Session Filtering
All event bus handlers (Paths 3 and 4) check `data.get('session_id') != self._session_id` to avoid forwarding events from other sessions.

## Same vs Different — Main Agent vs Worker Scaffolding

## 2026-07-12 — ## Same vs Different — Main Agent vs Worker Scaffolding (202...

## Same vs Different — Main Agent vs Worker Scaffolding (2026-07-12)

### Overview
This document compares the event propagation architecture of the **Main Agent** (the chat-bridge agent) versus the **Worker Agent** (worker-thread agent). Both run the **identical Agent core** (`agent/core/agent.py`), but differ entirely in how events are forwarded to the frontend.

---

### 1. The Shared Core (IDENTICAL)

Both main agent and worker agents go through the same `Agent.process_query()` generator (agent/core/agent.py:748-1274), which yields these event types:

| Event Type | When Yielded | Line |
|---|---|---|
| `token_update` | After adding user query, at turn start, after summary pruning | 760, 900, 1213 |
| `user_query` | After config is applied | 796-800 |
| `execution_state_change` | READY→RUNNING transition | 822-825 |
| `time_warning` | Each turn, if elapsed > warning threshold | 883-885 |
| `turn_warning` | Each turn, if turn count >= max_turns-3 | 895-897 |
| `token_warning` | Each turn, if token count > threshold | 901-911 |
| `turn` | Assistant response content (text/tool_calls) | 1150-1156 |
| `tool_call` | For each executed tool | 1173-1176 |
| `tool_result` | Result of each tool execution | 1177-1180 |
| `agent_responded` | When Respond tool or "Final" detected | 1191-1204 |
| `system_notification` | N/A — NOT yielded by process_query() | — |
| `stopped` | If stop_check() returns True | 859-861 |
| `error` | Provider errors, unexpected errors | 1038-1086 |
| `rate_limit_warning` | Rate limit exceeded | 1050-1052 |

**Key insight:** `system_notification` is **never yielded** by `process_query()`. The `context_summarized` notification is injected into the conversation (as a message dict) but never yielded as an event. Workers detect it via the `token_update` handler by checking for a ≥40% token drop.

**State management** (agent/core/state.py) is also identical:
- `update_token_state(total_tokens)` — yields `token_warning` when crossing thresholds
- `update_turn_state(turn)` — yields `turn_warning` when near max turns
- `update_time_state(elapsed)` — yields `time_warning` when runtime exceeds warning
- All set `restrictions_active=True` at critical levels, restricting available tools

---

### 2. The Event Propagation Path (COMPLETELY DIFFERENT)

#### 2A. Main Agent Path

```
Agent.process_query()  (runs in bridge thread)
    │
    ▼ yields event dicts
_map_and_emit(raw_event)  (bridge.py:1729-1800)
    │
    ├── token_update  →  'tokens_updated' + 'context_updated'
    ├── token_warning →  'conversation_changed' (full history sync)
    ├── turn_warning  →  'conversation_changed'
    ├── time_warning  →  'conversation_changed'
    ├── agent_responded → 'conversation_changed' + 'state_changed: WAITING_FOR_USER'
    ├── user_query    →  'conversation_changed'
    ├── tool_call     →  'conversation_changed'
    ├── tool_result   →  'conversation_changed'
    ├── turn          →  'conversation_changed'
    ├── error         →  'status_message' + 'conversation_changed'
    └── session_stop  →  'state_changed: IDLE'
    │
    ▼
WebSocket → frontend Hub WS → SessionTab webSocket
    │
    ▼
session.handleEvent()  →  chat ends up in the conversation via message endpoint HTTP calls
```

**Key characteristics:**
- Single-threaded: the bridge thread calls `process_query()` and handles events synchronously
- No EventBus involved for agent events (main agent uses EventBus only for security prompts and worker lifecycle)
- Token warnings are NOT sent as dedicated events — they trigger a `conversation_changed` sync, which means the frontend re-reads the full conversation via HTTP
- The warning text must already be in `session.user_history` (as a system message) for it to appear

#### 2B. Worker Agent Path

```
Agent.process_query()  (runs in worker thread)
    │
    ▼ yields event dicts
WorkerThread._run_tool_loop()  (worker.py:570-841)
    │
    ├── token_warning  →  _publish_event('token_warning', {message, token_count})
    │                   →  _log_event('system_notification', {type:'token_warning', ...})
    ├── turn_warning   →  _publish_event('turn_warning', {message, turn_count})
    │                   →  _log_event('system_notification', {type:'turn_warning', ...})
    ├── time_warning   →  _publish_event('time_warning', {message, elapsed_seconds})
    │                   →  _log_event('system_notification', {type:'time_warning', ...})
    ├── system_notification → NOT YIELDED BY AGENT (detected via token_update handler)
    │                   →  _publish_event('system_notification', {type:'context_summarized'})
    │                   →  _log_event('system_notification', {type:'context_summarized'})
    ├── agent_responded →  _publish_event('worker_message', {content, response_type})
    │                   →  _log_event('worker_message', {content, response_type})
    ├── tool_call      →  _publish_event('tool_call', {tool_name, arguments})
    │                   →  _log_event('tool_call', {tool_name, arguments})
    ├── tool_result    →  _publish_event('tool_result', {tool_name, success, result})
    │                   →  _log_event('tool_result', {tool_name, success, result})
    ├── turn           →  _publish_event('assistant_message', {content})
    │                   →  _log_event('assistant_message', {content})
    ├── user_message   →  _publish_event('user_message', {query})
    │  (logged at start) →  _log_event('user_message', {query})
    └── token_update   →  _log_event('system_notification') if ≥40% token drop detected
                           _publish_event('system_notification') if ≥40% token drop detected
    │
    ▼
    ├── _log_event() → events.jsonl (HTTP polling fallback)
    │
    └── _publish_event() → per-worker EventBus
                            │
                            ▼
                    Bridge subscriber (_subscribe_to_worker_bus)
                            │
                            ▼  {type: 'worker:token_warning', data: {message, token_count}}
                    WebSocket → WorkerOutputPanel incomingEvents
                            │
                            ▼
                    Normalize: 'worker:token_warning' → {event: 'system_notification', response: {type:'token_warning', message, token_count}}
                            │
                            ▼
                    adaptWorkerEvent() → {role:'user', is_system_notification:true, content:'⚠️ ...'}
                            │
                            ▼
                    MessageBubble → effectiveRole='system' → 'message-system-as-user' class → rendered
```

**Key characteristics:**
- Multi-threaded: worker runs in its own daemon thread
- Uses per-worker EventBus for all event forwarding
- Each event type maps to a **dedicated** WebSocket event type (e.g., `worker:token_warning`)
- Dual publication: both `_log_event()` (HTTP polling) and `_publish_event()` (WebSocket)
- Worker's `_publish_event` and `_log_event` are SEPARATE if-blocks (NOT `elif`), so same event can trigger both
- `context_summarized` is detected heuristically in the `token_update` handler (≥40% token drop)

---

### 3. EventBus Architecture (DIFFERENT)

| Aspect | Main Agent | Worker Agent |
|---|---|---|
| **Uses EventBus?** | Only for security prompts, worker lifecycle, and container rebuild events | Yes — per-worker EventBus for ALL events |
| **Which EventBus?** | Global singleton `global_event_bus` | Private per-worker `EventBus` instance |
| **Who subscribes?** | Bridge subscribes to security + worker lifecycle on global bus | Bridge subscribes to per-worker bus in `_subscribe_to_worker_bus()` |
| **When subscribed?** | At bridge init (`__init__`) | When worker spawns (`_on_worker_spawned`) |
| **What events?** | TOKEN_WARNING, WORKER_MESSAGE, security events | tool_call, tool_result, token_warning, turn_warning, time_warning, system_notification, worker_message, assistant_message, user_message |

---

### 4. Bridge Handler Comparison

| Event | Main Agent Handler | Worker Handler (Per-Worker Bus) | Worker Handler (Global Bus) |
|---|---|---|---|
| `token_warning` | `_map_and_emit` → `conversation_changed` (full history) | `_make_bus_handler('token_warning')` → `worker:token_warning` | `_on_worker_token_warning` → `worker:system_notification` with `response.type:'token_warning'` |
| `turn_warning` | `_map_and_emit` → `conversation_changed` (full history) | `_make_bus_handler('turn_warning')` → `worker:turn_warning` | Not subscribed on global bus |
| `time_warning` | `_map_and_emit` → `conversation_changed` (full history) | `_make_bus_handler('time_warning')` → `worker:time_warning` | Not subscribed on global bus |
| `system_notification` | Not applicable (not yielded) | `_make_bus_handler('system_notification')` → `worker:system_notification` | Not subscribed on global bus |
| `agent_responded` | `_map_and_emit` → `conversation_changed` + `state_changed` | `_make_bus_handler('worker_message')` → `worker:worker_message` | Via `WORKER_MESSAGE` global bus → `worker:worker_message` |
| `tool_call` | `_map_and_emit` → `conversation_changed` | `_make_bus_handler('tool_call')` → `worker:tool_call` | Not subscribed on global bus |

**Critical finding:** The main agent's `token_warning` is delivered via `conversation_changed` (full history sync). But the worker's `token_warning` is delivered via `_publish_event()` → per-worker EventBus → `worker:token_warning` WebSocket event. Both paths end up in the frontend, but through completely different mechanisms.

---

### 5. Frontend Rendering (DIFFERENT)

| Aspect | Main Agent Chat | Worker Panel (WorkerOutputPanel) |
|---|---|---|
| **Data source** | Hub WebSocket `conversation_changed` + HTTP `/messages` endpoints | Per-worker WebSocket events (`worker:token_warning`, etc.) + HTTP `/workers` polling |
| **Event types received** | `conversation_changed`, `state_changed`, `tokens_updated`, etc. | `worker:token_warning`, `worker:turn_warning`, `worker:tool_call`, etc. |
| **Message format** | Full `user_history` message objects from session | Normalized internal format → `adaptWorkerEvent()` → MessageBubble props |
| **Token warning display** | Shows as system message in conversation (from history) | Shows as `⚠️ Token usage warning: ... (Tokens: N)` via `tokenWarningMsg()` |
| **Dedup mechanism** | React key on message IDs | `seenEventKeysRef` Map with canonical event names |
| **Rendering component** | `MessageBubble` in chat list | `MessageBubble` in `WorkerOutputPanel` (reuses same component) |

---

### 6. Identified Gaps and Differences

#### Gap 1: Race Condition on Per-Worker Bus Subscription (MOST LIKELY)

**Location:** `worker.py:889-896` (auto-queued initial query) and `worker.py:964` (EventBus creation) vs `bridge.py:_on_worker_spawned` (subscription)

**The problem:** The worker's `run()` method:
1. Lines 889-896: Auto-queues the initial query (from `_initial_context`) — puts it in `self._pending_queries`
2. Line 964: Creates the EventBus and registers it via `register_worker_event_bus()`
3. Lines 967-984: Publishes WORKER_SPAWNED to the global EventBus
4. Line ~990+: The while loop picks up the query and calls `_run_tool_loop()`

Meanwhile, the bridge:
1. Receives WORKER_SPAWNED
2. Calls `_on_worker_spawned()`
3. Looks up the per-worker bus via `get_worker_event_bus()`
4. Calls `_subscribe_to_worker_bus()`

**If step 4 hasn't completed before the worker starts processing, all events from the first query are lost.** This includes token_warning events that occur during the first turn.

**Impact:** Affects ALL per-worker bus events (tool_call, tool_result, token_warning, etc.)

#### Gap 2: No `system_notification` Event from Agent Core

**Location:** `agent/core/agent.py` — nowhere in `process_query()` does it yield `system_notification`

**The problem:** The `context_summarized` notification is injected into the conversation as a message dict (`[SYSTEM NOTIFICATION] Context has been summarized...`), but never yielded as an event. The main agent shows it via conversation polling (it's in user_history). Workers detect it by monitoring `token_update` events for a ≥40% token drop — a heuristic, not a reliable detection.

**Fix applied (2026-07-12):** Worker's token_update handler now detects the token drop and publishes `system_notification` with `type: 'context_summarized'`. This was confirmed working.

#### Gap 3: Global Bus Handler for Worker Token Warnings is Dead Code

**Location:** `bridge.py:_on_worker_token_warning`

**The problem:** The bridge subscribes to TOKEN_WARNING on the global EventBus with a handler that checks `source.startswith("worker:")`. But workers publish token_warnings to the **per-worker** EventBus, not the global bus. The only code path that publishes TOKEN_WARNING to the global bus is... unclear. This handler is effectively dead code.

**Additionally:** The global bus handler uses `data.get('warning_message', '')` while the per-worker bus handler uses `data.get('message', '')`. This `warning_message` vs `message` key mismatch means even if the dead code path fired, it would produce empty messages.

#### Gap 4: Token Warning Display vs Context Summarized Display

Both end up as `system_notification` events in the frontend, but through different paths:

- `context_summarized`: Detected in `token_update` handler → `_publish_event('system_notification', {type:'context_summarized'})` → `worker:system_notification` → mapped directly to `system_notification` event
- `token_warning`: From `process_query()` yield → `_publish_event('token_warning', ...)` → `worker:token_warning` → normalized to `system_notification` event

Both are rendered via `adaptWorkerEvent()` → `MessageBubble` with `effectiveRole='system'`.

#### Gap 5: Dedup Key Mismatch (Minor)

**Location:** `WorkerOutputPanel.jsx`

The dedup **filter** uses `rawType` (`'token_warning'`) but the dedup **registration** uses `evt.event` (`'system_notification'`). This means:
- First event: filter checks `seenEventKeysRef` for `'token_warning|timestamp'` → not found → passes → normalized → `'system_notification|timestamp'` registered
- Second identical event: filter checks `'token_warning|timestamp'` again → still not found → passes → **DUPLICATE**

This causes potential duplicates rather than drops.

---

### 7. Summary Table

| Feature | Main Agent | Worker Agent | Same/Different |
|---|---|---|---|
| Agent core (process_query) | `agent/core/agent.py:748` | `agent/core/agent.py:748` | **IDENTICAL** |
| Token state machine | `agent/core/state.py:update_token_state` | Same | **IDENTICAL** |
| Turn state machine | `agent/core/state.py:update_turn_state` | Same | **IDENTICAL** |
| Time state machine | `agent/core/state.py:update_time_state` | Same | **IDENTICAL** |
| Conversation storage | Session.user_history (ObservableList) | WorkerContext.conversation (plain list) + context.json | **DIFFERENT** |
| Event consumer | Bridge `_map_and_emit()` | `WorkerThread._run_tool_loop()` | **DIFFERENT** |
| Event forwarding | Direct callback to WebSocket via `_event_callbacks` | Per-worker EventBus → Bridge subscriber → WebSocket | **DIFFERENT** |
| Token warning delivery | Via `conversation_changed` (full history sync) | Via dedicated `worker:token_warning` WS event | **DIFFERENT** |
| Turn warning delivery | Via `conversation_changed` (full history sync) | Via dedicated `worker:turn_warning` WS event | **DIFFERENT** |
| Time warning delivery | Via `conversation_changed` (full history sync) | Via dedicated `worker:time_warning` WS event | **DIFFERENT** |
| System notification delivery | Via conversation (message dict in user_history) | Via dedicated `worker:system_notification` WS event | **DIFFERENT** |
| Context persistence | Session serialization (session store) | `context.json` at `<workspace>/workers/<name>/` | **DIFFERENT** |
| Execution environment | Bridge thread (single-threaded loop) | Daemon thread (per-worker) | **DIFFERENT** |
| Tool restrictions enforcement | Same state machine | Same state machine | **IDENTICAL** |

---

### 8. Diagnostic Recommendations

To diagnose why token warnings don't appear in the worker panel:

1. **Check if worker is generating token_warnings at all:** Look at `events.jsonl` for entries with `"event": "system_notification"` and `"response": {"type": "token_warning"}` — this would confirm the `_log_event()` side works.

2. **Check bridge subscription timing:** Add a 200ms delay in `worker.py:run()` between registering the EventBus and processing the first query, OR add a subscription-confirmation handshake.

3. **Check per-worker bus subscription success:** The bridge logs `'Per-worker EventBus for {name} not found'` if `get_worker_event_bus()` returns None. Check server logs for this warning.

4. **Check WebSocket delivery:** Add a debug log in `_make_bus_handler` to confirm the handler fires when token_warning is published.

5. **Check frontend normalization:** Add a console.log in `WorkerOutputPanel.jsx` in the `incomingEvents` useEffect to see what events arrive and how they're normalized.



## 2026-07-13 — ## Research Task 2/5: State Machine, Token/Warning System — ...

## Research Task 2/5: State Machine, Token/Warning System — Complete

Read and analysed:

1. **agent/core/state.py** — AgentState dataclass, TokenState/TurnState/TimeState enums, warning dedup via `last_*_warning_state`, restriction logic (immediate on CRITICAL, turn-based on WARNING), `_pending_events`, `get_allowed_tools()`, `is_tool_allowed()`, `reset()`.

2. **agent/core/token_counter.py** — TokenCounter with `estimate_tokens()` (tiktoken cl100k_base), `estimate_request_tokens()`, `get_model_context_window()`, `format_tokens()`.

3. **agent/core/tool_executor.py** — `execute_tool_calls()` calls `update_token_func` (agent._update_tokens_after_tool) after each tool result. TurnTransaction buffering support.

4. **agent/core/agent.py** — `_update_tokens_after_tool()` buffers warnings, flushed after turn_transaction.commit(). `_handle_state_event()` generator. process_query() main loop with config apply, LLM call, tool execution, turn commit, warning flush, summarization flow. `_apply_summary_pruning()` inserts summary into append-only history with metadata.

5. **agent/core/conversation_manager.py** — `add_message()` delegates to context_builder or falls back to session.user_history.append.

6. **agent/events.py** — EventType enum, TokenWarningEvent/TurnWarningEvent/TimeWarningEvent, EventBus, `create_event()`, `convert_to_legacy_format()`.

Full research written to `research_2_state_tokens_warnings.md`.

## 2026-07-14 — Complete Token Warning Pipeline Investigation (all 8 tasks c...

Complete Token Warning Pipeline Investigation (all 8 tasks completed)

## Task 1: Full AgentState class (`agent/core/state.py`)
- **TokenState enum**: `LOW` (0) → `WARNING` (1) → `CRITICAL` (2)
- **Thresholds** (from `config.token_monitor_warning_threshold` default 65000, `config.token_monitor_critical_threshold` default 80000):
  - `total_tokens < warning_threshold` → `LOW`
  - `total_tokens < critical_threshold` → `WARNING`
  - `else` → `CRITICAL`
- **Warning message generated only on UPWARD transitions**: uses `state_order` dict (LOW=0, WARNING=1, CRITICAL=2), only emits warning if `state_order[new_state] > state_order[current_state]`
- **`restrictions_active`** set to `True` when entering CRITICAL
- **Other state enums**: ExecutionState (READY/RUNNING/PAUSING/ERROR), SessionState (NEW/CONTINUING/CLOSED), TimeState (LOW/WARNING/CRITICAL), TurnState (LOW/WARNING/CRITICAL)

## Task 2: 7 primary assignments to `current_conversation_tokens`
1. **state.py:86** — `update_token_state(self, total_tokens)`: `self.current_conversation_tokens = total_tokens` (set-and-trigger)
2. **state.py:294** — `reset()`: sets to 0
3. **agent.py:622** — `_update_conversation_token_estimate()`: overwrites with estimated_tokens from context builder
4. **agent.py:999** — `process_query()`: `self.state.current_conversation_tokens = input_tokens` (LLM prompt_tokens overwrite — ground truth)
5. **agent.py:670** — `_update_tokens_after_tool()`: `+= tool_tokens` 
6. **agent.py:686** — `_update_tokens_after_tool()`: `+= warning_tokens` (estimated tokens of warning SYSTEM NOTIFICATION)
7. **agent.py:770** — `process_query()`: `+= estimated_tokens` (user query message estimate)

**Secondary assignments** (warning message estimates in turn loop):
- agent.py:888 — time_warning `+= warning_tokens`
- agent.py:906 — turn_warning `+= warning_tokens`  
- agent.py:922 — token_warning (turn loop) `+= warning_tokens`

## Task 3: `_update_tokens_after_tool` (agent.py:659-686)
- Signature: `def _update_tokens_after_tool(self, tool_tokens=None)`
- Adds tool_tokens to `current_conversation_tokens`
- Calls `self.state.update_token_state(self.state.current_conversation_tokens)`
- **Buffers** resulting warning messages into `_pending_warnings` and `_pending_warning_events` instead of injecting immediately
- Also adds `warning_tokens` (estimate of warning message) to `current_conversation_tokens`
- **No `_update_tokens_after_llm` method exists.** LLM response tokens handled inline in process_query (line 999: overwrites with `input_tokens` = `response.usage['prompt_tokens']`)

## Task 4: Agent Loop (`process_query`, agent.py:759-1300)
1. Add user query to conversation; estimate & add tokens; yield user_query event
2. Apply pending config (hot-swap or restart)
3. Set ExecutionState to RUNNING
4. For `turn in range(max_turns)` (line 852):
   - Check stop/pause signals
   - Time monitoring (update_time_state)
   - Turn monitoring (update_turn_state)
   - Token monitoring (update_token_state at turn level)
   - Build context via context_builder
   - Call LLM (`chat_completion`)
   - **Overwrite** `current_conversation_tokens` with LLM-reported `prompt_tokens` (ground truth)
   - Handle response (tool_calls or final answer)
   - If tool_calls: execute tools → commit → flush warnings
   - If no tool_calls: commit → flush warnings → return final
5. Max turns reached → yield stop_reason event

## Task 5: Warning Flush Mechanism
Warnings are buffered in two lists:
- **`_pending_warnings`** (List[dict]): warning Message objects to add to conversation
- **`_pending_warning_events`** (List[dict]): raw event dicts to yield to event stream

**Flush happens in two branches** after each turn:
1. **Tool branch** (lines 1198-1207): after `turn_transaction.commit()` (which persists tool results to user_history), flush warnings so they land chronologically AFTER tool results
2. **No-tool branch** (lines 1259-1267): after `turn_transaction.commit()`, flush warnings before yielding final event

Both branches: iterate `_pending_warnings` → `_add_to_conversation(warning)`, then yield `_pending_warning_events`, then clear both lists.

## Task 6: Debug Logging Environment Variables
No special `DEBUG_TOKEN` or `TOKEN_DEBUG` env vars exist. Standard logging controls:
- **`TM_LOG_LEVEL`** — default INFO (DEBUG for granular token tracking)
- **`TM_LOG_TAGS`** — default empty, `*` for all tags; `core.token`, `**pipeline.token_update**`, `**pipeline.warning**`, `core.agent` are relevant tags
- **`THOUGHTMACHINE_DEBUG=1`** — backward compat, sets level to DEBUG and tags to `*`
- **`AGENT_LOG_CATEGORIES`** — filter log categories
- **`TM_LOG_FILE_LEVEL`** — file log level override
- **`TM_DEBUG_TRUNCATE_LENGTH`** — truncation length for debug output

Key log tags used in token pipeline:
- `core.token` — detailed token state transitions, estimates, drift detection
- `**pipeline.token_update**` — every token count change with before/after values
- `**pipeline.warning**` — warning buffer operations (buffering, flushing, counts)
- `core.agent` — general agent flow debug

## Additional Findings
- Token drift detection at agent.py:1001-1016: compares pre-call estimate vs LLM-reported prompt_tokens, warns if drift > 5%
- Summary pruning (agent.py:1301+) re-evaluates token state after summarization
- `state.py` `update_token_state` is called from multiple places — each call potentially generates warnings, but the transition check prevents duplicate warnings
- The `_add_conversation_data_to_event` helper (agent.py:731) adds `created_at`, `timestamp`, `seq`, conversation_id, conversation_tokens, conversation_turns to every event

## Token Warning Pipeline — Worker Panel Flow

## 2026-07-14 — ## Complete Token Warning Pipeline: Agent → Worker → Bridge ...

## Complete Token Warning Pipeline: Agent → Worker → Bridge → Frontend

### 9. WORKER PANEL: How token warnings reach the WorkerOutputPanel

#### 9A. Agent (core) — Event Generation
- `agent/core/agent.py`: `TokenCounter` detects token thresholds in `_update_tokens_after_tool()`.
- Token warnings are **buffered** in `self._pending_warnings` (Message objects) and `self._pending_warning_events` (event dicts).
- They are flushed **after turn_transaction.commit()** so they land in correct chronological order after tool results.
- The agent yields `token_warning` event dicts from `process_query()`.

#### 9B. Worker Thread — Event Relaying (worker.py)
- `WorkerThread._run_tool_loop()` iterates `self._agent.process_query(query)` events.
- On `event_type == "token_warning"`, it calls `self._publish_event("token_warning", {...})`.
- The published data includes: `message`, `warning_message`, `token_count`, `old_state`, `new_state`.

#### 9C. _publish_event() — Per-worker EventBus (worker.py:1148)
```python
def _publish_event(self, event_type: str, data: dict) -> None:
    # Auto-injects worker_name
    resolved_type = EventType(event_type)  # EventType("token_warning")
    event = create_event(event_type=resolved_type, data=data, source="worker", session_id=...)
    self._event_bus.publish(event)  # Per-worker EventBus
```
- The per-worker EventBus is created in `run()` (`self._event_bus = EventBus()`).
- Registered via `register_worker_event_bus(session_id, worker_name, self._event_bus)`.
- Also publishes WORKER_SPAWNED to the **global** event bus so bridge can discover it.

#### 9D. Bridge — Subscription & Forwarding (bridge.py)
- **Global bus listener**: `_subscribe_to_worker_events()` subscribes to `EventType.WORKER_SPAWNED` via `_on_worker_spawned`.
- **On WORKER_SPAWNED**: `_on_worker_spawned()` calls `get_worker_event_bus(session_id, worker_name)` to get the per-worker bus, then calls `_subscribe_to_worker_bus(worker_name, worker_bus)`.
- **Per-worker bus subscriptions**: `_subscribe_to_worker_bus()` subscribes to event types: `tool_call`, `tool_result`, `worker_message`, `assistant_message`, **`token_warning`**, `turn_warning`, `time_warning`, `user_message`, `system_notification`.
- **Handler**: `_make_bus_handler(original_type)` creates a Handler that wraps the event dict with `type: 'worker:{original_type}'` (e.g., `'worker:token_warning'`) and calls all registered `event_callbacks`.
- **Global bus TOKEN_WARNING handler**: `_on_worker_token_warning()` also catches token warnings from the global bus, but only for events with `source.startswith("worker:")`.

#### 9E. Server → WebSocket — Event Delivery (server.py)
- The bridge's `event_callbacks` are registered by the WebSocket handler in `server.py`.
- When the bridge callback fires, `server.py` packages the event dict as a JSON message and sends it over the WebSocket connection.

#### 9F. Frontend — Reception (SessionTab.jsx)
- `SessionTab.jsx` receives WebSocket messages and routes by `msg.type`.
- Worker bus events arrive with type `'worker:token_warning'`, `'worker:turn_warning'`, `'worker:time_warning'`, etc.
- All handled in the same switch branch (lines 514-527):
  ```javascript
  case 'worker:token_warning':
  case 'worker:turn_warning':
  case 'worker:time_warning':
  case 'worker:assistant_message':
  case 'worker:worker_spawned':
  case 'worker:worker_status':
  case 'worker:worker_completed':
  case 'worker:worker_error':
  case 'worker:system_notification':
  case 'worker:user_message':
  case 'worker:worker_message':
      // logged and forwarded to WorkerOutputPanel via setWorkerEvents()
  ```
- Events are stored in `workerEvents` state (keyed by sessionId) via `setWorkerEvents()`.
- Deduplication is done by stripping `'worker:'` prefix and normalizing to canonical event type + timestamp.

#### 9G. WorkerOutputPanel — Display
- `WorkerOutputPanel.jsx` receives `incomingEvents` prop (filtered from parent's `workerEvents`).
- Events are processed in a `useEffect` hook:
  - Strips `'worker:'` prefix: `event.type?.replace('worker:', '')`
  - Deduplicates via `makeDedupKey()` using canonical event name + timestamp
  - Maps events to a unified format with `event`, `timestamp`, `request`, `response` fields
- **token_warning case** (line 287):
  ```javascript
  case 'token_warning': {
      const data = e.data || {}
      response = { type: 'token_warning', message: data.warning_message || data.message || '', token_count: data.token_count }
      return { event: 'system_notification', timestamp: e.timestamp, request: {}, response }
  }
  ```
- Token warnings map to `event: 'system_notification'` with `response.type: 'token_warning'`.
- The `system_notification` case (line 324) also handles direct system_notification events with `response.type` from data.
- These are displayed in the worker output panel as system notification entries.
- The main ChatPanel `MessageBubble.jsx` also shows token warnings with a yellow/orange banner style for messages containing "token" or "⚠️" that have `is_system_notification: true`.

#### 9H. Two Paths for Token Warnings from Workers
1. **Per-worker bus path** (primary for worker sub-agents): Worker → `_publish_event("token_warning")` → per-worker EventBus → bridge per-worker bus subscription → `'worker:token_warning'` WS message → WorkerOutputPanel.
2. **Global bus path** (for main agent token warnings): Main agent's process_query yields `token_warning` → bridge's `_on_worker_token_warning()` catches it (only if source starts with "worker:") → `'worker:system_notification'` WS message.



## 2026-07-14 — ## Worker Unified Presenter Pipeline (2026-07-14)

### Probl...

## Worker Unified Presenter Pipeline (2026-07-14)

### Problem
Worker panel's token counter got stuck because `token_update` events from the agent were cached internally by `WorkerThread._run_tool_loop()` but **never published** to the per-worker EventBus. The 12-branch if/elif chain had `token_update` as the sole event type handled only for internal caching — not forwarded to the frontend.

### Solution: WorkerBusAdapter + EventProcessor integration
Gave each worker its own presenter pipeline, mirroring the main agent's architecture:

**New components** (in `tools/workspace/worker.py`):
1. **`WorkerBusAdapter`** — Drop-in for `GUIIntegration` (Qt signals → per-worker EventBus). Implements all `emit_*` methods that `EventProcessor` calls, including `emit_tokens_updated()` and `emit_context_updated()` which publish `token_update` events to the per-worker bus.

2. **`WorkerSessionLifecycle`** — Minimal stub satisfying `EventProcessor`'s dependency (just `state` property + `_restarting` + `auto_save_current_session()`).

**Wiring** (in `WorkerThread._run_tool_loop()`):
- Lazy-initialized on first query: `StateBridge` + `WorkerBusAdapter` + `WorkerSessionLifecycle` + `EventProcessor`
- Every agent event is fed through `event_processor.process_event(event)` **before** the existing forwarding chain
- `WorkerBusAdapter` publishes `token_update` events to the per-worker EventBus → bridge forwards to frontend → token counter updates live

**`get_current_context_tokens()`** updated to prefer `StateBridge.context_length` (live from EventProcessor) over `_cached_context_tokens`.

### What changed
- **`tools/workspace/worker.py`**: +3 classes, ~80 lines added, ~10 lines modified
- **Tests**: Zero regressions (all existing tests pass)

### Remaining work
- The 12-branch if/elif chain in `_run_tool_loop()` still exists alongside the presenter. Future cleanup: replace manual forwarding with presenter-based publishing entirely.
- Bug A (state machine warning reset) to be triaged after pipeline is stable.

## 2026-07-14 — ## Phase 3c Verification (Completed 2025-07-14)

**Scope:** ...

## Phase 3c Verification (Completed 2025-07-14)

**Scope:** Full import chain and integration test for `tools/workspace/worker.py` refactoring.

**Changes verified:**
1. ✅ `WorkerBusAdapter.forward_agent_event()` — exists with proper event type handling (tool_call, tool_result, token_warning, turn_warning, time_warning, system_notification, turn)
2. ✅ `self._worker_bus_adapter = bus_adapter` — set in lazy-init block
3. ✅ `self._worker_bus_adapter: Optional[Any] = None` — initialized at line 519
4. ✅ Event loop simplified — `forward_agent_event()` replaces all manual `self._publish_event()` calls

**Full import chain (all 11 checks passed):**
1. `agent.events` (EventBus, EventType, BaseEvent, ToolCallEvent, ToolResultEvent) — OK
2. `agent.core.state` (ExecutionState) — OK
3. `agent.presenter.state_bridge` (StateBridge) — OK
4. `agent.presenter.event_processor` (EventProcessor) — OK
5. `tools.workspace.worker` (WorkerBusAdapter, WorkerSessionLifecycle, WorkerThread) — OK
6. WorkerBusAdapter.forward_agent_event() — OK
7. All emit_* methods (tokens_updated, context_updated, status_message, error_occurred, config_changed, conversation_changed) — OK
8. WorkerBusAdapter._publish() — OK
9. WorkerSessionLifecycle (state, has_unsaved_changes, mark_clean) — OK
10. StateBridge (update_config, save_config, bind_session) — OK
11. EventProcessor(state_bridge, session_lifecycle, gui_integration) — OK

**Test results:**
- 1 per-worker EventBus bridge test passed
- 22 worker permissions tests passed
- 18 workspace tools worker tests passed (10 pre-existing failures unrelated to changes)
- StateBridge(None) — OK
- StateBridge('') — OK
- EventProcessor init — OK

## 2026-07-15 — ## 2026-07-15 — Comprehensive Architecture Deep-Dive (Curren...

## 2026-07-15 — Comprehensive Architecture Deep-Dive (Current Codebase State)

### Context
This entry documents the findings from a thorough analysis of the current codebase as of 2026-07-15, following a fresh workspace setup and file-by-file inspection of the core system. It captures the actual on-disk structure and component relationships, complementing the historical record above.

### Three-Tier Architecture (Verified)

```
┌─────────────────────────────────────────────────────────┐
│              React/Vite Frontend (:5173)                 │
│  SessionTab.jsx  ·  ConfigPanel.jsx  ·  ProviderPanel   │
│  (Zustand-less — each tab owns its own WebSocket)       │
└──────────────────────────────┬──────────────────────────┘
                               │ JSON over WebSocket (ws://host:8000/ws)
                               ▼
┌─────────────────────────────────────────────────────────┐
│              FastAPI WebSocket Server (:8000)            │
│  server.py  ·  /ws endpoint                             │
│  Routes: start_session, continue_session, pause/stop,   │
│  get_config, get_conversation, save/load/delete session, │
│  update_config/apply_config, get_providers, etc.        │
│  Per-connection: WebAgentBridge instance                 │
└──────────────────────────────┬──────────────────────────┘
                               │ calls into
                               ▼
┌─────────────────────────────────────────────────────────┐
│              Agent Core (agent/core/)                    │
│  agent.py (facade/coordinator)                           │
│  ├─ TokenCounter         — token estimation              │
│  ├─ LLMClient            — LLM API calls                 │
│  ├─ ConversationManager  — history + pruning             │
│  ├─ ToolExecutor         — tool execution + permissions  │
│  └─ DebugContext         — conditional debug logging     │
│  state.py                — AgentState state machine      │
│  events.py               — EventBus + EventType enum     │
└─────────────────────────────────────────────────────────┘
```

### Agent Core Components (Detailed)

**agent.py** (the coordinator):
- `process_query()` — main loop (~500+ lines): token check → LLM call → tool execution loop → event emission
- `_handle_state_event()` — pre-LLM state updates (token warnings, turn warnings, system notifications)
- `_apply_summary_pruning()` — inserts summary system messages at turn boundaries
- `_update_tokens_after_tool()` — post-tool token recalc + warnings
- `_add_to_conversation()` — appends to user_history
- Uses TurnTransaction for turn lifecycle management

**state.py** — AgentState dataclass:
- Five sub-states: TokenState, TurnState, TimeState, ExecutionState, SessionState
- Enums with transition logic (e.g., TokenState: NORMAL → SOFT_WARNING → CRITICAL → RESTRICTED)
- `update_token_state()` — generates token warnings based on thresholds
- `update_turn_state()` — generates turn warnings
- Generates state_changed events consumed by bridge → frontend

**events.py** — Event system:
- `EventType` enum: 30+ types (AGENT_STATE_CHANGED, TOKENS_UPDATED, WORKER_SPAWNED, WORKER_STATUS, WORKER_COMPLETED, WORKER_ERROR, TOOL_EXECUTION, etc.)
- `EventBus` class: pub/sub pattern with subscriber lists per event type
- `BaseEvent` schema: standard event envelope
- Used by both main agent and worker agents (per-worker EventBus instances)

**conversation_manager.py**:
- `add_message()` — adds to user_history with metadata (seq, created_at, is_system_notification)
- `prune_history()` — removes messages when history exceeds limits
- `_group_messages_into_turns()` — groups messages by turn boundaries for context building

### Bridge Layer (web_ui/backend/bridge.py)

**WebAgentBridge** — wraps Agent for WebSocket frontend:
- Thread-safe: uses locks for concurrent access
- Manages Agent lifecycle: start, continue, pause, resume, stop
- Translates WebSocket commands to AgentController calls
- Handles config apply from frontend
- Emits events back over WebSocket via state_bridge

### Event Pipeline Architecture

```
Agent (process_query)
  │
  ├─► AgentState.update_*() ──► state_changed events
  ├─► EventBus.publish()    ──► EventProcessor
  │                               ├─► state_bridge (WebSocket → frontend)
  │                               └─► gui_integration (PyQt6 GUI, legacy path)
  └─► ConversationManager   ──► user_history (source of truth)
```

### Worker System (tools/workspace/worker.py)

**WorkerThread** — isolated execution environment:
- Each worker has its own EventBus (per-worker, not shared with main agent)
- StateBridge: translates worker state to WebSocket-friendly messages
- EventProcessor: dispatches events to state_bridge + gui_integration
- Workers run inside Docker sandbox with workspace path resolution
- Worker status reporting via IPC pattern (worker:* events → bridge → frontend)

### WebSocket Protocol (server.py)

**Client → Server commands** (27 commands):
- Session lifecycle: start_session, continue_session, pause_session, resume_session, stop_session, new_session
- Session persistence: save_session, load_session, delete_session, rename_session, list_sessions
- Config: get_config, update_config, apply_config
- Conversation: get_conversation, load_more_messages
- Providers: get_providers, save_provider, delete_provider
- Other: get_available_tools, get_open_sessions, close_session, set_project, security_response, get_workspace_capabilities, bootstrap_workspace

**Server → Client event types** (20+):
- state_changed, tokens_updated, context_updated, conversation_changed, more_messages
- config_changed, status_message, sessions_list, session_saved/loaded/deleted/renamed
- open_sessions_list, session_closed, session_cleared
- providers_list, provider_saved, provider_deleted
- tools_list, security_prompt
- worker:worker_spawned, worker:worker_status, worker:worker_completed, worker:worker_error

### Frontend Architecture (web_ui/frontend/)

**SessionTab.jsx** — main interactive component:
- Independent WebSocket per tab (no Zustand/global state)
- Local React state via useState/useReducer
- Handles: message display, config editing, provider management, session persistence
- Worker panel: shows spawned workers with status updates
- MessageRenderer: special styling for system notifications, tool calls, errors
- ConfigPanel.jsx: permission toggles, provider config, tool output limits
- ProviderPanel: LLM provider management UI

### Security Architecture

- **Three-layer model**: ToolExecutor category checks → Docker sandbox → Container permissions
- **SessionPermissions**: network (banned/none/bridge), filesystem (read/write), container (bool), execution (banned/read)
- **Docker sandbox**: containers.run() with network=none, volumes=ro workspace, tmpfs for /tmp and /home/agent, .git masked
- **Security prompts**: tool execution can trigger user approval via security_response command
- **Default permissions**: network=banned, filesystem=read, container=False, execution=banned (server enforce)

### Key Design Patterns

1. **Event-Driven Pipeline**: All state changes flow through EventBus → EventProcessor → bridge → frontend (or GUI)
2. **State Machine**: AgentState manages 5 sub-states with explicit transitions; events emitted on every change
3. **user_history = Source of Truth**: Append-only message list; LLM context is a derived sliding window
4. **Summarization**: SummarizeTool inserts summary at turn boundaries; token warnings use metadata flag (is_system_notification) for correct placement
5. **Per-Worker Isolation**: Workers have their own EventBus, StateBridge, and EventProcessor — fully decoupled from main agent
6. **Thread-Safe Bridge**: WebAgentBridge uses locks for concurrent WebSocket → Agent access
7. **Lazy Imports**: docker_executor and security modules use lazy imports to avoid circular dependencies
8. **Debug Gates**: DEBUG_CONTEXT env var controls extensive debug logging across multiple modules

### Key File Locations

| Component | File | Purpose |
|-----------|------|---------|
| Agent coordinator | agent/core/agent.py | Main process_query loop, coordination |
| State machine | agent/core/state.py | AgentState with 5 sub-states |
| Event system | agent/events.py | EventBus, EventType, BaseEvent |
| Conversation mgr | agent/core/conversation_manager.py | History management |
| Token counter | agent/core/token_counter.py | Token estimation |
| Tool executor | agent/core/tool_executor.py | Tool orchestration + permissions |
| LLM client | agent/core/llm_client.py | LLM API communication |
| WebSocket server | web_ui/backend/server.py | FastAPI /ws endpoint |
| Bridge | web_ui/backend/bridge.py | WebAgentBridge |
| Config models | agent/config/models.py | AgentConfig Pydantic |
| Frontend tab | web_ui/frontend/src/components/SessionTab.jsx | Main UI component |
| Worker thread | tools/workspace/worker.py | WorkerThread |
| Event processor | agent/presenter/event_processor.py | Event dispatch |
| Docker executor | docker_executor.py | Container management |
| Session store | session/store.py | SQLite persistence |
| Security gate | thoughtmachine/security.py | Sandbox validation |
| Provider profiles | agent/config/provider_profiles.py | LLM provider definitions |
| Debug context | agent/core/debug_context.py | Conditional debug logging |


## 2026-07-15 — ## worker_state_sync Frontend Pipeline

### Event Flow
1. **...

## worker_state_sync Frontend Pipeline

### Event Flow
1. **Backend**: `WorkerBusAdapter.emit_state_sync()` → EventBus (`type='worker_state_sync'`, payload: `{context_length, token_state, warning_message, critical_threshold, worker_name}`)
2. **Bridge**: `_make_bus_handler` subscribes → forwards as `type='worker:worker_state_sync'` via WebSocket with nested `data` payload
3. **SessionTab.jsx** (line 546-547): Case `'worker:worker_state_sync'` → forwards to `onWorkerEvent(sessionId, msg)`
4. **App.jsx** (line 285-313): `handleWorkerEvent` stores in `workerEvents[sessionId]` with dedup (canonical type + timestamp key)
5. **WorkerOutputPanel.jsx**: Receives `incomingEvents={workerEvents[activeSessionId]}`

### Two Processing Phases in WorkerOutputPanel
- **Phase A — workerInfo state** (lines 228-268): Updates header context counter and warning banner immediately
- **Phase B — Event list** (lines 411-430): Maps to display event with `event: 'worker_state_sync'`, `response: {context_length, token_state, warning_message, critical_threshold}`

### Three Visual Touchpoints
1. **Header** (line 546-549): `ctx: X / Y` using `latestEvent?.current_context_tokens ?? workerInfo?.current_context_tokens`
2. **Warning banner** (lines 552-565): Conditional on `workerInfo?.token_state === 'WARNING' || 'CRITICAL'`
3. **Event log** (lines 568-608): Each event via `adaptWorkerEvent()` → `MessageBubble`

### adaptWorkerEvent.js handler (line 248-280)
- `LOW`/`OK`: ✅ Token state: LOW (X tokens) — `role: 'system'`
- `WARNING`: ⚠️ Token state: WARNING — message (X / Y max) — `role: 'user', is_system_notification: true`
- `CRITICAL`: 🔴 Token state: CRITICAL — message (X / Y max) — `role: 'user', is_system_notification: true`

### Dedup Strategy
- In `App.handleWorkerEvent`: Canonical type `'worker_state_sync'` + timestamp as dedup key
- In `WorkerOutputPanel`: `seenEventKeysRef` with `makeDedupKey(rawType, timestamp)` — worker_state_sync maps to canonical = `'worker_state_sync'` (not mapped to `system_notification`)
- Backend state machine guarantees one-shot escalation per level (WARNING → CRITICAL), preventing duplicate banner renders

## 2026-07-15 — ## 2026-07-15 — Progress: Comprehensive Event Pipeline Full ...

## 2026-07-15 — Progress: Comprehensive Event Pipeline Full Trace (Partial)

### Files Fully Read:
1. **agent/core/state.py** (18,923 bytes) — Complete. TokenState machine fully documented.
2. **agent/presenter/event_processor.py** (18,460 bytes) — Already read from previous session.
3. **agent/logging/unified.py** (21,589 bytes) — Already read from previous session.
4. **tools/workspace/worker.py** (~96,786 bytes) — Full read: WorkerBusAdapter, WorkerSessionLifecycle, WorkerThread (all methods incl. run(), _run_tool_loop()), _restrictive_merge, shutdown_workers, registries.
5. **agent/core/agent.py** (1,516 lines) — Pages 1-3 fully read up to ~line 850 (process_query method). Need to read: process_query completion, _flush_warnings_after_commit, SummarizeTool integration, context_cleared events.

### Files Still to Read:
- **session/history_provider.py** — Event dispatch to frontend
- **web_ui/backend/bridge.py** — Bridge handling of worker events
- **web_ui/backend/server.py** — WebSocket event dispatch to frontend

## 2026-07-15 — ## 2026-07-15 — agent/core/agent.py Complete (1,516 lines)

...

## 2026-07-15 — agent/core/agent.py Complete (1,516 lines)

Fully read all methods. Key findings for event pipeline audit:
- **process_query()** (lines ~620-1337): Core event-yielding generator. Turn loop yields: token_update → turn → tool_call → tool_result → agent_responded. Warnings buffered in _pending_warnings/_pending_warning_events then flushed after turn commit.
- **_update_tokens_after_tool()** (lines ~700-770): Processes state.update_token_state() events, buffers token_warning and token_recovery. Warnings become [SYSTEM NOTIFICATION] messages.
- **_handle_state_event()** (lines ~560-610): Yields events for token_warning, turn_warning, execution_state_change, session_state_change, token_recovery, context_cleared. Adds conversation metadata.
- **_flush_warnings_after_commit()**: Actually happening inline in process_query() at lines 1222-1231 (tool branch) and lines 1296-1304 (non-tool branch).
- **_apply_summary_pruning()** (lines 1338-1415): Inserts summary msg into user_history with metadata (pruning_keep_recent_turns, pruning_insertion_idx), appends [SYSTEM NOTIFICATION] context_cleared message, updates token estimate, returns token_recovery events.
- **config hot-swap/restart**: Full mailbox pattern with _pending_config, _can_hot_swap, _hot_swap, _restart_with_config, _has_api_key, restart().

Still to read: session/history_provider.py, web_ui/backend/bridge.py, web_ui/backend/server.py

## 2026-07-15 — ## Bug Investigation Progress

### Bug 1: emit_state_sync fl...

## Bug Investigation Progress

### Bug 1: emit_state_sync flood
- Found in tools/workspace/worker.py at line 183 — `WorkerBusAdapter.emit_state_sync()`
- Called from `WorkerThread._run_tool_loop()` at line 986 after every event
- Frontend handles it in `WorkerOutputPanel.jsx` — renders token_state badge and warning messages
- Fix plan: Add `_last_published_state` dict to dedup within `emit_state_sync()`

### Bug 2: Old critical warning persists after summary
- `state.py` line 140-145: Already has token_recovery event when transitioning CRITICAL→LOW, resets `_token_warning_has_fired = False`
- `agent.py` line 1392-1400: `_apply_summary_pruning()` appends `[SYSTEM NOTIFICATION] Context has been summarized...` message
- The issue: After summary, `update_token_state()` IS called in `_apply_summary_pruning()` at line 1405, which SHOULD trigger recovery
- Need to check: Is `emit_state_sync()` called AFTER summary completes in the worker pipeline?
- Also need to check `_run_tool_loop()` in worker.py to see the full event handling flow

### Files still to read
- tools/workspace/worker.py: _run_tool_loop method (near line 981+), rest of emit_state_sync
- agent/presenter/event_processor.py

## 2026-07-16 — ## Event Pipeline Complete Trace — 5 Event Types

### Key So...

## Event Pipeline Complete Trace — 5 Event Types

### Key Source Files Analyzed:
- `agent/events.py` — All event types, EventBus, create_event(), event_class_map
- `agent/core/state.py` — AgentState, update_token_state() with WARNING/CRITICAL/LOW transitions, update_turn_state()
- `agent/core/agent.py` — process_query(), _update_tokens_after_tool(), _handle_state_event(), _create_token_update_event(), _apply_summary_pruning()
- `agent/presenter/event_processor.py` — process_event() with all sub-processors, emit_* methods
- `agent/presenter/state_bridge.py` — StateBridge with context_length/token tracking
- `tools/workspace/worker.py` — WorkerThread._run_tool_loop(), WorkerBusAdapter (all emit_* and forward_agent_event), WorkerSessionLifecycle
- `web_ui/backend/bridge.py` — _make_bus_handler(), _subscribe_to_worker_bus(), WebSocket forwarding
- `web_ui/frontend/src/components/chat/adaptWorkerEvent.js` — Frontend event dispatch switch

### Architecture Summary:
Agent.process_query() is a generator that yields event dicts. These are consumed by either:
- **Main agent path**: WebAgentBridge._agent_task() in bridge.py iterates process_query() and dispatches each event to WebSocket callbacks
- **Worker path**: WorkerThread._run_tool_loop() in worker.py iterates process_query() and forwards selected events via WorkerBusAdapter → per-worker EventBus → bridge._make_bus_handler() → WebSocket

### Insights:
- Warnings are **buffered** in _pending_warning_events and flushed after turn_transaction.commit() so they appear chronologically after tool results and before the next assistant message
- Worker events flow through **two parallel bus mechanisms**: the global EventBus (lifecycle events like WORKER_SPAWNED, WORKER_STATUS) and per-worker EventBus (detailed events like tool_call, token_warning)
- The bridge deduplicates worker:context_updated events by comparing formatted display strings (e.g., "12.3K")
- Worker-sourced token warnings are only forwarded via per-worker bus (Path A); global bus handler (Path B) skips source="worker" to avoid duplicates

## 2026-07-17 — ## Cooperative Pause/Resume — Full Codebase Analysis

### Ke...

## Cooperative Pause/Resume — Full Codebase Analysis

### Key Files
- **`tools/workspace/worker.py`** (2140 lines): WorkerThread class (lines 545-1420)
  - `__init__()` (560-624): Has `self._stop_event = threading.Event()` at line 624
  - `stop()` (716-726): Sets `_stop_event`, writes `command.json` with `{"action": "stop"}`, unblocks `_input_queue`
  - `_poll_command()` (728-750): Checks `command.json` for `"action": "stop"`, sets `_stop_event`, unblocks queue
  - `_run_tool_loop()` (843-996): Polls `_poll_command()` on each event, checks `_stop_event.is_set()`, calls `self._agent.request_pause()`, breaks
  - `run()` (1000-1277): Main loop - polls command before blocking on input queue, creates Agent lazily, processes queries. On exception sets `status="error"`. On else/normal exit sets `status="completed"`. No pause handling yet.
  - `status` field: `"ready" | "busy" | "completed" | "error"` (line 599)
  
- **`web_ui/backend/workspace_routes.py`** (659 lines): REST API
  - `stop_worker()` (580-658): Finds worker dir, writes `command.json` with `{"action": "stop"}`, immediately writes `status.json` with `runtime_status: "completed"`, fast-path signals in-memory thread via `thread.stop()`

- **`web_ui/backend/PAUSE_PROPAGATION_DESIGN.md`**: Existing design doc (file-based approach)

### Design Decision: threading.Event-based Approach
Instead of file-based `command.json` approach (which relies on 2-second polling latency), use a dedicated `threading.Event` for pause signalling:
- Add `self._pause_event = threading.Event()` to `__init__()` - clean, instant, race-condition-free
- `_poll_command()`: handle `"action": "pause"` → set `_pause_event` instead of `_stop_event`
- New `pause()` method: set `_pause_event`, write `command.json` for cross-process, unblock `_input_queue`
- New `resume()` method: clear `_pause_event`, write status as `"ready"`
- `_run_tool_loop()`: check both `_pause_event` and `_stop_event`
- `run()`: preserve `"paused"` status in except/else blocks
- API endpoint: `POST /{ws_id}/workers/{name}/pause` and `POST /{ws_id}/workers/{name}/resume`


## 2026-07-17 — ## 2026-07-17 — Implementation Details (Post-Review)

### Wo...

## 2026-07-17 — Implementation Details (Post-Review)

### WorkerThread Changes (`tools/workspace/worker.py`)

**New fields** (all `threading.Event`):
- `self._pause_event = threading.Event()` — set when pause is requested, cleared on resume
- `self._resume_event = threading.Event()` — set on resume, cleared on pause

**New methods:**
- `pause()` — sets `_pause_event`, clears `_resume_event`, writes `{"action":"pause"}` to `command.json`, unblocks `_input_queue` so the run loop can detect the event
- `resume()` — clears `_pause_event`, sets `_resume_event`, sets `self.status = "ready"`, writes status file

**Modified `_poll_command()`** — now handles `"action": "pause"` (same pattern as stop):
  ```python
  elif action == "pause":
      cmd_path.unlink(missing_ok=True)
      self._pause_event.set()
      self._input_queue.put(None)
  ```

**Modified `_run_tool_loop()` (line ~947):** — after the tool loop body, checks `_pause_event.is_set()`:
  1. Calls `self._agent.request_pause()` to signal the agent to yield
  2. Sets `self.status = "paused"`
  3. Writes status file (optimistic UI)
  4. Publishes `worker_paused` event via `_publish_event()`
  5. Returns a pause response JSON to the caller

**Modified `run()` (line ~1222):** — after `_run_tool_loop` returns, if paused:
  1. Preserves `"paused"` status (does NOT overwrite with `"ready"`)
  2. Writes status file
  3. Publishes `worker_paused` event to per-worker AND global event bus
  4. Saves conversation context via `_save_context()`
  5. Sends pause response to `_output_queue`
  6. **Blocks** in a wait loop: `while self._pause_event.is_set() and not self._stop_event.is_set(): self._resume_event.wait(1.0)`
  7. If stopped during pause → loop breaks
  8. If resumed → sets `status = "ready"`, clears `_resume_event`, publishes `worker_resumed` event, continues outer loop

**When NOT paused:** existing flow unchanged (status → `"ready"`, saves context, sends reply, continues loop)

### API Endpoints (`web_ui/backend/workspace_routes.py`)

**`POST /api/workspace/{ws_id}/workers/{name}/pause`** (line 663):
  - Resolves worker directory in both `session/{session_id}/workers/{name}/` and `workers/{name}/` layouts
  - Atomic-writes `{"action":"pause"}` to `command.json`
  - Immediately writes status.json with `runtime_status: "paused"` (optimistic UI)
  - Fast path: if thread found in registry, calls `thread.pause()` directly
  
**`POST /api/workspace/{ws_id}/workers/{name}/resume`** (line 741):
  - Same directory resolution
  - Atomic-writes `{"action":"resume"}` to `command.json` (for consistency; resume uses in-memory path)
  - Immediately writes status.json with `runtime_status: "ready"`
  - Fast path: calls `thread.resume()` directly

### Status Values
The worker status enum now supports: `"ready" | "busy" | "paused" | "completed" | "error"`

### Lifecycle Flow
```
                     pause()
ready ──── query ────► busy ────► paused ──── resume() ────► ready
                        │                                      ▲
                        └── stop() ────► completed              │
                        └── error ────► error                   │
                                                                └── spawn again
```
- Pause is *cooperative*: the agent finishes its current tool call/turn before yielding
- While paused, the worker blocks in the `run()` loop's wait loop, checking `_resume_event` every 1s
- Stop takes priority: if stop is requested while paused, the break condition exits the wait loop
- Context is saved before entering the pause wait, so paused state survives process restart
- Cross-process signalling via `command.json` enables pause/resume from the web UI


## 2026-07-17 — ## Chunk 1 — Forensics Complete: Pause & Stop Button Deep Di...

## Chunk 1 — Forensics Complete: Pause & Stop Button Deep Dive

### Q1: Main Panel Pause Button — End-to-End Trace

**START → QueryBar.jsx (lines 23–65)**
- `handleToggle()` called when user clicks "⏸ Pause" button (rendered at line 102 when `status === 'RUNNING'`)
- Line 55: `sendCommand('pause_session', {})` — empty object payload

**WebSocket transport:**
- QueryBar's `sendCommand` prop is passed down from SessionTab
- SessionTab owns a session-scoped WebSocket to `ws://host:8000/ws`
- sendCommand serializes to JSON `{"command": "pause_session"}` and sends via the WS

**server.py handler (lines 548–552):**
```python
elif command == "pause_session":
    if bridge is not None:
        bridge.pause()
        await ws.send_json({"type": "status_message", "text": "⏸ Paused."})
```

**bridge.py — WebAgentBridge.pause() (lines 1056–1073):**
Three actions:
1. **V2 Controller**: `self._controller.pause()` if controller exists
2. **Legacy agent**: `self._agent.request_pause()` + clears `_pause_event`
3. **ALL session workers**: Iterates `_worker_registry` for matching `session_id`, calls `thread.pause()` on each

**Key insight**: Session-wide pause — main agent AND all sub-workers pause cooperatively.

---

### Q2: Worker Stop Button — End-to-End Trace (two locations)

**Location A — WorkerManagementPanel.jsx (lines 586–617):**
- `handleStop(name)` via REST: `POST /api/workspace/{ws_id}/workers/{name}/stop`
- Optimistic state: sets `runtime_status` to `'stopped'` immediately
- List rows have stop buttons enabled when status is `busy`, `ready`, or falsy

**Location B — WorkerOutputPanel.jsx (lines 610–633 + 813–817):**
- Same REST POST to `/api/workspace/{ws_id}/workers/{name}/stop`
- Has cross-session guard: blocks stop if `workerInfo.session_id !== sessionId`
- `canStop = runtimeStatus === 'busy' || runtimeStatus === 'ready'`
- Stop button rendered in bottom bar: `<button className="worker-output-stop-btn">⏹ Stop</button>`

**workspace_routes.py — stop_worker() (line 580+):**
1. Writes `{"action": "stop"}` to worker's `command.json` (file-based signal, polled by worker thread)
2. Writes `status.json` with `runtime_status: "completed"` for immediate UI feedback
3. Falls back to in-memory stop via thread registry if available

**bridge.py — WebAgentBridge.stop() (lines 1096–1104):**
- Unregisters bridge, unsubscribes security & worker events
- Sets `_stop_event` and `_pause_event` (unblocks if paused)

---

### Q3: Purpose Comparison — Pause vs Stop

| Aspect | Main Pause (`pause_session`) | Worker Stop (REST API) |
|---|---|---|
| Transport | WebSocket (bidirectional) | HTTP POST (request-response) |
| Scope | Session-wide (agent + ALL workers) | Single named worker |
| Semantics | COOPERATIVE — requests pause after current turn | TERMINAL — file signal + thread kill |
| Resumable? | YES — via `continue_session` / `resume_session` | NO — terminal stop |
| UI location | QueryBar "⏸ Pause" button | WorkerManagementPanel rows + WorkerOutputPanel bottom bar |
| Worker registry | Iterates all workers for this session_id | Only the named worker by name |

**Conclusion:** They serve completely different purposes. `pause_session` is a cooperative, resumable suspension of the entire session (agent + workers). Worker stop is a terminal shutdown of a single worker, used when a specific sub-worker is misbehaving or no longer needed.

## 2026-07-17 — ## Chunk 2 — Backend Signal Routing (Complete)

### Q4: Main...

## Chunk 2 — Backend Signal Routing (Complete)

### Q4: Main pause signal routing

**bridge.py pause() — lines 1061-1076 (full method):**
```python
def pause(self) -> None:
    if self._controller is not None:
        self._controller.pause()       # V2 path
    else:
        if not self.is_running:
            return
        self._pause_event.clear()       # Legacy: block the main loop
        if self._agent is not None:
            self._agent.request_pause() # Legacy: tell agent to pause
    # ALWAYS runs (after either branch) — pauses ALL workers for this session:
    if WORKER_BUS_AVAILABLE and _worker_registry is not None:
        with _registry_lock:
            for (sid, wname), thread in list(_worker_registry.items()):
                if sid == self._session_id:
                    thread.pause()
```

**Three actions AFTER the if/else:**
1. Worker iteration via `_worker_registry` (same `(session_id, worker_name)` tuple keys)
2. Guarded by `WORKER_BUS_AVAILABLE and _worker_registry is not None`
3. Matches by `sid == self._session_id` → calls `thread.pause()` on all matching

**Import is FIXED — bridge.py lines 86-99:**
```python
try:
    from tools.workspace.worker import (
        shutdown_workers, get_worker_event_bus, register_worker_event_bus,
        unregister_worker_event_bus, _worker_registry, _registry_lock
    )
    WORKER_BUS_AVAILABLE = True
except ImportError:
    ...
    _worker_registry = None
    _registry_lock = None
    WORKER_BUS_AVAILABLE = False
```
`_worker_registry` is successfully imported from `tools.workspace.worker`, where it's defined at line 467-468 as `_worker_registry: dict = {}` and `_registry_lock = threading.Lock()`. Keys are `(session_id, worker_name)` tuples — consistent with the iteration in `pause()`.

**self._controller is AgentController** from `/workspace/agent/controller/__init__.py`:
- Line 14: `class AgentController`
- Line 475-502: `pause()` and `resume()` methods
- `controller.pause()` (line 475-496): Clears `pause_event`, sets `_pause_requested = True`, emits `execution_state_change` event with `PAUSING`, calls `self.agent.request_pause()`, cleans orphaned tool messages. **DOES NOT touch worker registry** — worker pausing is only in bridge.pause().
- `controller.resume()` (line 502-514): Sets `pause_event`, clears `_pause_requested`, clears `agent._pause_requested`. **No worker logic.**

**Legacy `self._agent.request_pause()`** — called in the else branch of bridge.pause(). Also the V2 path `controller.pause()` calls `self.agent.request_pause()` internally. Both signal the agent to pause after current turn.

### Q5: Worker stop signal routing

**workspace_routes.py stop_worker() — lines 580-658:**
1. **Finds worker directory** — session-scoped (`workers/<session_id>/<name>/`) or legacy (`workers/<name>/`)
2. **Writes `command.json`** with `{"action": "stop"}` — file signal polled by worker thread
3. **Writes `status.json`** with `{"runtime_status": "completed", "current_task": null, ...}` — immediate UI visual update
4. **Calls `thread.stop()`** on matching registry entries (matched by `wname == name`)

```python
with _registry_lock:
    for (sid, wname), thread in list(_worker_registry.items()):
        if wname == name:
            thread.stop()
```

Note: Matches on `wname` only (not session_id), so it can stop workers across sessions.

**WorkerThread.stop() — worker.py lines 719-728:**
```python
def stop(self) -> None:
    self._stop_event.set()
    try:
        cmd_path = self._worker_dir / "command.json"
        cmd_path.write_text(json.dumps({"action": "stop"}), encoding="utf-8")
    except OSError:
        pass
    self._input_queue.put(None)
```
- Sets `_stop_event`
- Writes `command.json {"action": "stop"}` (belt-and-suspenders with file-based signaling)
- Unblocks input queue by putting None
- **Does NOT touch `_pause_event`** — it's a terminal stop, not a resume-from-pause

**WorkerThread.pause() — worker.py lines 731-742:**
```python
def pause(self) -> None:
    self._pause_event.set()
    self._resume_event.clear()
    try:
        cmd_path = self._worker_dir / "command.json"
        cmd_path.write_text(json.dumps({"action": "pause"}), encoding="utf-8")
    except OSError:
        pass
    self._input_queue.put(None)
```

**WorkerThread.resume() — worker.py lines 744-749:**
```python
def resume(self) -> None:
    self._pause_event.clear()
    self._resume_event.set()
    self.status = "ready"
    self._write_status_file()
```

**Polling loop — `_poll_command()` worker.py lines 756-778:**
Reads `command.json`, handles `"stop"` and `"pause"` actions (deletes file, sets events, unblocks queue).

### Q6: Why aren't they unified?

**1. bridge.pause() DOES now call thread.pause() on workers.** The import is fixed. The code:
```python
if WORKER_BUS_AVAILABLE and _worker_registry is not None:
    with _registry_lock:
        for (sid, wname), thread in list(_worker_registry.items()):
            if sid == self._session_id:
                thread.pause()
```
This correctly pauses all workers in the session. ✅

**2. Worker stop is a TERMINAL (HARD) stop.** It sets `_stop_event` and `command.json {"action": "stop"}`. The worker thread loop checks `_stop_event` on every iteration and breaks out. It does NOT set `_pause_event` — no resumption path. It's a one-way door: once stopped, the worker thread exits permanently.

**3. Is there a gap?** The main pause button (`bridge.pause()`) now correctly pauses workers via `thread.pause()` — this is the RESUMPTIVE path. Worker stop (`thread.stop()`) is the TERMINAL path. They are architecturally distinct by design:
- **Pause** = cooperative suspension (set `_pause_event`, clear `_resume_event`, write `command.json {"action": "pause"}`)
- **Stop** = terminal exit (set `_stop_event`, write `command.json {"action": "stop"}`)
- They are NOT redundant and should NOT be unified — they serve different session lifecycle phases.

## 2026-07-17 — ## Chunk 3 — Complete Forensic Investigation

### Q7: proces...

## Chunk 3 — Complete Forensic Investigation

### Q7: process_query() pause checkpoints (3 locations)

**Checkpoint [1] — turn_start (agent.py ~line 883-897):**
```python
# At top of each turn loop iteration
if self.stop_check and self.stop_check():
    events = self.state.set_execution_state(ExecutionState.PAUSING)
    for event in events:
        for yielded_event in self._handle_state_event(event):
            yield yielded_event
    # yields 'stopped' event, returns
```
- Uses `self.stop_check` callable (config.stop_check — set externally by controller)
- Conversation untouched: no turn has started, nothing committed
- Yields: `execution_state_change` (from _handle_state_event) + `stopped`

**Checkpoint [2] — after_llm (agent.py ~line 1138-1179):**
```python
if self._pause_requested:
    if tool_calls:
        # DEFER: _pause_requested stays True, checkpoint [3] catches it later
    else:
        # GRACE TURN: commit assistant message BEFORE pausing
        assistant_msg = {'role': 'assistant', 'content': content, ...}  # no tool_calls
        grace_tx = TurnTransaction(session, context_builder, conversation)
        grace_tx.add_assistant_message(assistant_msg)
        grace_tx.commit()  # → extends session.user_history immediately
        events = self.state.set_execution_state(ExecutionState.PAUSING)
        # yields execution_state_change + paused events, returns
```
- **No tool_calls**: Grace turn committed (assistant message saved to user_history)
- **Has tool_calls**: Deferred — _pause_requested stays True, will be caught at [3]

**Checkpoint [3] — after_turn (agent.py ~line 1277-1300):**
```python
if self._pause_requested:
    events = self.state.set_execution_state(ExecutionState.PAUSING)
    # yields execution_state_change + paused events, returns
```
- At this point `turn_transaction.commit()` already called (tool results + assistant in conversation)
- Full turn data already saved

### Q8: Complete event stream (typical turn with tool calls)

```
user_query (from process_query start)
token_update (after user msg estimate)
[if time warning] time_warning + token_update
[if turn warning] turn_warning + token_update
token_update (after turn state checks)
[if token warning] token_warning + token_update
turn (carries content + tool_calls metadata)
  ── LLM call happens here ──
  [checkpoint 2: pause check]
token_update (after commit_assistant_only)
  ── tool_executor.execute_tool_calls() runs ──
  turn_transaction.commit() (all tool results committed to history)
  for each tool: tool_call event
  for each tool: tool_result event
  [flush pending warnings]
  [if final_detected] agent_responded
  [if summary_text] context_summarized
  [checkpoint 3: pause check]
  [if no tool_calls] agent_responded
```

### Q9: Conversation state when pause is yielded

**Pause at [1] (turn_start via stop_check):**
- Conversation is EXACTLY as it was at end of last turn
- NO new messages added — this check is at the very top of the loop
- Event yields: `execution_state_change` (PAUSING) → `stopped`

**Pause at [2] (after_LLM, no tool_calls):**
- Conversation has: last turn's messages + user query for this turn + assistant response (saved via grace_tx.commit())
- Grace turn: assistant_msg = {'role': 'assistant', 'content': content} — NO tool_calls
- Event yields: state events → `execution_state_change` (PAUSING) → `paused`

**Pause at [3] (after_turn, had tool_calls):**
- Conversation has: all messages from full turn (assistant with tool_calls + tool results)
- `turn_transaction.commit()` already called before checkpoint check
- Event yields: state events → `execution_state_change` (PAUSING) → `paused`

**Pause at [2] deferred (had tool_calls):**
- Same as [3] — full turn committed before yielding pause

### TurnTransaction atomic buffer (turn_transaction.py)

- **Buffered**: assistant_message + tool_calls_buffer (list of tool call/result msgs)
- **Two-phase commit** for tool turns:
  1. `commit_assistant_only()` — commits assistant message immediately after LLM call (before any events)
  2. `commit()` — commits tool results (or everything if assistant not pre-committed)
- **Rollback**: clears buffer; cannot rollback committed transaction
- On pause at [2] (no tools): `commit()` called on grace_tx (assistant only)
- On pause at [3]: normal `commit()` already called before checkpoint

### _add_to_conversation (agent.py ~line 645)
```
def _add_to_conversation(self, message):
    updated = self.conversation_manager.add_message(message, self.conversation)
    self.conversation = updated
    # validates is_system_notification flag consistency
    # invalidates context_builder._cached_context
```
- Delegates to conversation_manager.add_message()
- Updates conversation property (which for session assigns back to session.user_history)
- Validates system notification flags
- Invalidates context_builder cache

### conversation property (agent.py ~line 536-560)
- Getter: returns session.user_history when session exists, else _conversation
- Setter: replaces session.user_history contents in-place and calls _on_conversation_changed()
- Ensures HistoryProvider cache is invalidated on mutation

## 2026-07-18 — ## Workspace UUID Generation — Investigation Results (Partia...

## Workspace UUID Generation — Investigation Results (Partial)

### UUID Algorithm (not UUID4, but SHA-256 hash)
- Workspace "ID" is NOT a UUID4 — it's the first 16 hex characters of `hashlib.sha256(project_path.encode()).hexdigest()[:16]`
- Found in `setup_workspace.py` line 24: `ws_id = hashlib.sha256(PROJECT_ROOT.encode()).hexdigest()[:16]`
- The comment says "same algorithm as thoughtmachine.workspace_capabilities" — but actually `workspace_capabilities.py` does NOT generate IDs, it only has `resolve_workspace_id()` which scans config.json files
- The auto-registration code in `server.py` `apply_config` confirms: `import uuid; ws_id = hashlib.sha256(...).hexdigest()[:16]`

### Workspace config.json Structure
- Located at `~/.thoughtmachine/workspaces/{workspace_id}/config.json`
- Contains: `{"root": PROJECT_ROOT, "capabilities": {}}` (from setup_workspace.py line 32)
- In tests: `json.dumps({"root": f"/projects/{label}"})`

### bridge.py Cache Mechanism
- `_workspace_id_cache: Dict[str, str]` — module-level dict mapping normalized workspace path → workspace_id
- `_build_workspace_id_cache()` — scans `~/.thoughtmachine/workspaces/<id>/config.json`, normalizes root paths (abspath + replace backslash + rstrip /), builds cache
- `_resolve_workspace_id()` — looks up in cache, builds cache on first call
- Protected by `_workspace_cache_lock` (threading.Lock())
- Cache is built once, never invalidated — persistent for bridge instance lifetime

### Dual Resolution Path
1. **bridge.py cache** (`web_ui/backend/bridge.py`) — Web UI path, caches workspace_id by scanning config.json
2. **workspace_capabilities.py** (`thoughtmachine/workspace_capabilities.py`) — same algorithm but no caching, scans every call

### Auto-Registration (server.py apply_config)
When workspace_path changes and no ID is found:
1. Uses `hashlib.sha256(project_path.encode()).hexdigest()[:16]` to generate deterministic ID
2. Calls `ensure_workspace_dirs(ws_id)` to bootstrap workspace files
3. Writes `config.json` with `{"root": workspace_path}`
4. Calls `_build_workspace_id_cache()` to refresh cache

### No Human-Readable Names
- Workspace IDs are purely deterministic hashes with no human-readable alias/naming system
- Frontend receives `workspace_id` as opaque string in `session_loaded` event
- Sessions have human-readable names (via `rename_session`/`metadata['name']`) but workspaces do not


## 2026-07-18 — ## Chunk 1: Core Agent Loop (process_query) - Complete Analy...

## Chunk 1: Core Agent Loop (process_query) - Complete Analysis

### Overall Architecture
The Agent class (`agent/core/agent.py`, 1519 lines, 39 methods) is a **facade coordinator** that delegates to modular components. It was refactored from a monolithic 1972-line class.

### Key Components
1. **TokenCounter** (`agent/core/token_counter.py`, 111 lines) - Token estimation using tiktoken
2. **LLMClient** (`agent/core/llm_client.py`, 172 lines) - Wraps ProviderFactory, handles system prompts, chat completion
3. **ConversationManager** (`agent/core/conversation_manager.py`, 117 lines) - Message addition with cache invalidation
4. **ToolExecutor** (`agent/core/tool_executor.py`, 349 lines) - Executes tool calls, handles SummarizeTool specially
5. **TurnTransaction** (`agent/core/turn_transaction.py`) - Atomic commit/rollback for turn buffering
6. **AgentState** (`agent/core/state.py`) - State machine with TokenState, TurnState, TimeState, ExecutionState, SessionState
7. **DebugContext** (`agent/core/debug_context.py`) - Debug logging helper
8. **message_utils** (`agent/core/message_utils.py`) - Turn grouping utilities

### process_query() Flow (generator, yields dict events)
1. Add user query to conversation first (never lost on config failure)
2. Yield token_update event
3. Apply pending config (mailbox pattern)
4. If config failed: yield error event, return
5. Yield user_query event
6. Set execution state to RUNNING
7. **Turn loop** (for turn in range(max_turns)):
   a. Check stop signal (stop_check callback)
   b. Time monitoring (update_time_state)
   c. Turn state monitoring (update_turn_state)
   d. Token state monitoring (update_token_state) - fires warnings
   e. Build context via context_builder.build()
   f. Call LLM via llm_client.chat_completion()
   g. On LLM response:
      - Use prompt_tokens as ground truth (drift detection)
      - Handle token_limit_exceeded with emergency retry
      - Handle RateLimitExceeded with backoff
   h. Check pause requests at 3 checkpoints
   i. Create TurnTransaction, add assistant message
   j. Commit assistant message immediately (prevents data loss on pause)
   k. Yield token_update, turn events
   l. If tool_calls: execute via ToolExecutor
   m. Commit all tool results via turn_transaction.commit()
   n. Yield tool_call, tool_result events
   o. Flush buffered token warnings
   p. If final_detected (Respond tool): yield agent_responded, return
   q. If SummarizeTool: apply summary pruning, continue loop
   r. If no tool_calls: commit turn, yield agent_responded, return
8. **Max turns exhausted**: yield stop_reason event

### Token Management Strategy
- Truth-based tracking: LLM-reported prompt_tokens = ground truth
- Pre-call estimates tracked for drift detection (>5% warning)
- Buffered warnings (flushed after turn commit for correct chronology)
- Emergency mode: when token_limit_exceeded fires, activation with retries
- Token state machine: LOW -> WARNING -> CRITICAL thresholds

### Context Summarization Flow
- SummarizeTool generates summary text and keep_recent_turns
- _apply_summary_pruning() inserts summary message with metadata
- _find_summary_insertion_index() uses turn-grouping to find position
- Fallback path when no session available
- Post-summary: token estimate reevaluated, restrictions cleared
- Emergency mode reset after successful summary

## 2026-07-18 — ## Chunk 2: Event System — Complete Analysis

### Event Type...

## Chunk 2: Event System — Complete Analysis

### Event Types (agent/events.py)
~55 EventType enum values organized into categories:
- **Agent lifecycle**: AGENT_START, AGENT_END
- **LLM interaction**: LLM_REQUEST, LLM_RESPONSE, RAW_RESPONSE
- **Tool execution**: TOOL_CALL, TOOL_RESULT
- **Conversation**: CONVERSATION_UPDATE, CONVERSATION_PRUNE
- **State monitoring**: EXECUTION_STATE_CHANGE, SESSION_STATE_CHANGE
- **Token/Turn/Time warnings**: TOKEN_WARNING, TURN_WARNING, TIME_WARNING
- **Control flow**: FINAL_DETECTED, FINAL, STOP_SIGNAL, MAX_TURNS, PAUSED, STOPPED
- **Security**: SECURITY_PROMPT, SECURITY_RESPONSE, FILE_ACCESS, SECURITY_VIOLATION
- **Worker lifecycle**: WORKER_SPAWNED, WORKER_STATUS, WORKER_COMPLETED, WORKER_ERROR, WORKER_MESSAGE
- **WorkerBusAdapter**: TOKENS_UPDATED, CONTEXT_UPDATED, STATUS_MESSAGE, ERROR_OCCURRED, WORKER_STATE_SYNC, etc.

### Event Hierarchy (Pydantic v2)
- **BaseEvent**: type + metadata (EventMetadata with event_id, timestamp, source, session_id, turn) + data dict
- Typed subclasses: AgentStartEvent, AgentEndEvent, ToolCallEvent, ToolResultEvent, TokenWarningEvent, TurnWarningEvent, ErrorEvent, TurnEvent, SecurityPromptEvent, SecurityResponseEvent, WorkerSpawnedEvent, WorkerStatusEvent, WorkerCompletedEvent, WorkerErrorEvent, WorkerMessageEvent, AssistantMessageEvent
- Each typed subclass has @validator ensuring required data fields
- ToolCallEvent/ToolResultEvent normalize both 'name' and 'tool_name' keys for backward compatibility

### EventBus (agent/events.py)
- Thread-safe pub/sub with threading.Lock
- Per-type subscriber lists + wildcard subscribers (event_type=None)
- publish() calls subscribers in order, wrapping each in try/except
- publish_dict() for legacy dict format
- NullEventBus: No-op stub for testing/worker contexts (no human), ask() returns "deny" instantly
- global_event_bus = EventBus() — singleton used throughout

### Event Wiring
- **Security**: global_event_bus.subscribe(SECURITY_PROMPT) → WebAgentBridge forwards to frontend WebSocket
- **Worker lifecycle**: global_event_bus.subscribe(WORKER_SPAWNED/WORKER_STATUS/WORKER_COMPLETED/WORKER_ERROR/WORKER_MESSAGE/TOKEN_WARNING) → WebAgentBridge _make_handler() forwards as worker:* events
- **Per-worker buses**: WorkerBusAdapter creates per-worker EventBus; WebAgentBridge subscribes per-worker for tool_call, tool_result, tokens_updated, context_updated, context_summarized, etc.
- **Event dedup**: context_updated uses display string dedup per worker_name to avoid flooding frontend
- **Late-arriving bridge guard**: _discover_existing_workers() on session load finds already-running workers

### Backward Compatibility
- create_event() factory function maps EventType → typed event class
- _map_legacy_event_type() maps old string types to enum values
- convert_to_legacy_format() / convert_from_legacy_format() for interop with legacy dict-based code
- Token/Turn warnings handle 'message'/'warning'/'warning_message' key normalization
- ErrorEvent handles various error_type detection from message prefix

## Chunk 3: Worker Architecture — Complete Analysis

### WorkerThread (tools/workspace/worker.py, ~1000 lines)
- **Runs in daemon thread** with input/output queues (threading.Queue)
- **Lifecycle**: ready → busy → ready
- **Control signals**: threading.Event for stop, pause, resume
- **command.json pattern**: External control via JSON file in worker's state dir
- **Status publishing**: status_changed callback
- **Structured output**: output_handler callback
- **Agent config building**: Builds config from system prompt, tools, permissions
- **EventBus per worker**: WorkerBusAdapter bridges worker events to global bus + per-worker bus
- **Cleanup**: shutdown_workers() for graceful teardown

### Worker (tool entry point, ~770 lines)
- Permission checking (ask policy integration)
- Agent config building from tool params
- Context sanitization
- Delegates to WorkerThread for actual execution

### Key Patterns
- Workers are spawned as tool calls from the main agent
- Each worker gets its own EventBus (per-worker bus) for detailed events
- WorkerBusAdapter publishes to both per-worker bus AND global_event_bus
- NullEventBus used in worker contexts (no human to answer security prompts)
- Worker state is persisted in workspace for session resume

## Chunk 4: Session Model — Complete Analysis

### Session (session/models.py)
- Core dataclass: session_id (UUID), created_at, updated_at, runtime_params (temperature), user_history (ObservableList), total_input/output_tokens, context_length, agent_context, containers, preset_name, version, next_seq, summary, agent_instance, workspace_id, metadata, security_config
- **ObservableList**: Wraps user_history with mutation callbacks (_on_conversation_changed)
- **Conversation change tracking**: _conversation_version (int) + conversation_hash (md5 hexdigest)
- **connect_conversation_changed/disconnect**: External listeners (used by AutosaveMonitor in server.py)
- **Serialization**: to_persistable_dict() / from_persistable_dict() with backward compat for old 'config' key → metadata['agent_config']
- **Security config**: merge_security_config + coerce_session_permissions on load
- **Message normalization**: All messages converted to Message objects on load

### HistoryProvider (session/history_provider.py)
- Wraps Session.user_history, provides get_context_for_llm()
- **Cached context**: _cached_context invalidated on add_message()
- **Delegates context building to SummaryBuilder** (session/context_builder.py)
- **create_summary()**: Adds summary system message with metadata (pruning_keep_recent_turns, pruning_insertion_idx, timestamp)
- **_find_latest_summary()**: Searches backward for summary messages
- **_group_messages_into_turns()**: Groups messages for keep_recent_turns logic
- **Debug support**: DEBUG_HISTORY_PROVIDER, DEBUG_CONTEXT env vars

### ContextBuilder (session/context_builder.py)
- SummaryBuilder: Builds LLM context with summary + recent turns
- _cleanup_orphaned_tool_messages(): Removes tool messages without matching assistant
- Token estimation methods

### SessionStore (session/store.py)
- **FileSystemSessionStore**: JSON files in ~/.thoughtmachine/sessions/
- **Friendly filenames**: {sanitized_name}_{short_id}.json
- **Atomic writes**: Temp file + rename pattern
- **File locking**: FileLock for concurrent access safety
- **Metadata files**: _meta_{session_id}.json for fast sidebar listing
- **In-memory caches**: _cached_list (60s TTL), _cached_paths (5s TTL) for fast listing
- **Fast metadata extraction**: _fast_extract_metadata() reads ~8KB head to avoid full JSON parse
- **Open sessions management**: open_sessions.json, .current_session marker
- **History pruning**: prune_user_history() removes old summarization cycles on save
- **Fallback paths**: CWD → system temp if ~/.thoughtmachine unavailable

## Chunk 5: Bridge & Presenter Layer — Complete Analysis

### AgentController (agent/controller/__init__.py, 719 lines)
- **Thread-based agent runner**: Runs Agent in daemon thread, collects events via callback
- **State machine**: ExecutionState tracking (IDLE, RUNNING, PAUSED, STOPPING, etc.)
- **Synchronization**: threading.Event for stop/pause, queue.Queue for query dispatch
- **Lifecycle**: process_query() → start thread → iterate events → callback → cleanup
- **Config management**: set_session(), update_config(), get_config()
- **Global event bus publishing**: Publishes control events (PAUSED, STOPPED, etc.)

### WebAgentBridge (web_ui/backend/bridge.py, 2140 lines)
- **Thread-safe bridge**: One bridge per tab/session
- **Agent wrapped in daemon thread**: start(query, config) → thread → process_query()
- **Event mapping**: Agent events → frontend events (state_changed, tokens_updated, conversation_changed)
- **Security prompt forwarding**: global_event_bus → WebSocket callback
- **Worker event forwarding**: Per-worker bus subscription for real-time events
- **Session persistence**: load/save sessions via FileSystemSessionStore
- **Multi-tab support**: _active_tab_bridges set, _broadcast_rename()
- **Cleanup**: cleanly_closed flag prevents data loss on disconnect

### Presenter Layer (agent/presenter/)
- **RefactoredAgentPresenter**: High-level orchestrator (config, session, agent lifecycle)
- **EventProcessor**: Processes agent events, delegates to StateBridge/SessionLifecycle
- **SessionLifecycle**: Session CRUD, start/stop/pause, autosave
- **StateBridge**: Config management, tool registration, session binding
- **StateBridge state**: SessionState enum (IDLE, LOADING, ACTIVE, ERROR)

### Server Layer (web_ui/backend/server.py, ~2300 lines)
- **FastAPI + WebSocket**: Single WebSocket endpoint for all real-time communication
- **Protocol**: JSON messages with 'type' field routing
- **Message types**: process_query, pause, resume, stop, update_config, load_session, save_session, rename_session, delete_session, security_response, list_sessions, list_worker_definitions, spawn_worker, worker_command, load_workspace, etc.
- **Session persistence**: AutosaveMonitor with 2s debounce + manual save on close
- **Workspace management**: Load/save workspace state, worker contexts
- **Logging**: Logging routes for real-time log streaming
- **Health**: Health check endpoint for container orchestration
