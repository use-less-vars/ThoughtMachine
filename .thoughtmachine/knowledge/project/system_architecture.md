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

