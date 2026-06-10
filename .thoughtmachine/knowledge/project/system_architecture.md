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

