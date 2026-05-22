# System Architecture

Key architectural decisions, component relationships, and data flow patterns.

## Current Status
- No architecture notes recorded yet.

## Components
(To be populated)

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

### Overview
The "Dual-Stream Bridge" refers to two parallel event delivery paths from AgentController to the GUI/presenter:

**Path A — Qt pyqtSignal (event_occurred):**
- `AgentController.event_occurred` is a `pyqtSignal(dict)`
- Connected in `RefactoredAgentPresenter._connect_signals()`: `self.controller.event_occurred.connect(self._handle_controller_event)`
- Forwards to `EventProcessor.process_event()` which dispatches by event type
- `GUIIntegration` provides 7 signals: state_changed, tokens_updated, context_updated, status_message, error_occurred, config_changed, conversation_changed
- Captured in `RefactoredAgentPresenter` and re-emitted to consumers
- Requires QApplication event loop (thread-safe due to Qt's queued connections)

**Path B — Plain Python Callbacks (_event_callbacks):**
- `AgentController._event_callbacks: List[Callable]`
- Registered via `set_event_callback(callback)` 
- Works without Qt event loop — for Web UI, CLI, etc.
- Both paths fire in `_emit_event()` which:
  1. Puts event on `event_queue` (for polling)
  2. Emits via `event_occurred.emit(event)` (Path A)
  3. Iterates `_event_callbacks` (Path B)

### Event Flow
1. Agent background thread (`_run()`) calls `agent.process_query(query)` which yields events
2. Each event goes through `_emit_event(event)` 
3. `session_id` is injected into each event
4. Content events (turn, tool_call, tool_result, final, etc.) also emit `conversation_updated` signal

### Token Warning System (demonstrated live)
- `AgentState.update_token_state(total_tokens)` transitions: LOW → WARNING → CRITICAL
- Thresholds from config: `token_monitor_warning_threshold` and `token_monitor_critical_threshold`
- At WARNING: emits `token_warning` event, but restrictions not active yet
- At CRITICAL: sets `restrictions_active=True`, filters tools to SummarizeTool/Final/FinalReport
- The `ToolFilter` in main agent loop uses `AgentState.is_tool_allowed()` to enforce restrictions
- Turn warnings (at max_turns-3) also activate restrictions

### Component Hierarchy
```
AgentController (background thread)
  └─ _emit_event()
      ├─ event_queue.put(event)        [queue for polling]
      ├─ event_occurred.emit(event)    [Path A: Qt signal]
      └─ _event_callbacks callbacks    [Path B: plain Python]
           │
RefactoredAgentPresenter (main thread)
  ├─ StateBridge          [config & session state]
  ├─ GUIIntegration       [Qt signals for GUI]
  ├─ SessionLifecycle     [start/stop/pause/save/load]
  └─ EventProcessor       [routes events to state updates]
       └─ GUIIntegration emit methods → QML/PyQt GUI


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
