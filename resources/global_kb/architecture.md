# ThoughtMachine Architecture

## High-Level Structure

```
ThoughtMachine/
├── agent/              # Core agent logic
│   ├── core/           # Agent engine (agent.py, llm_client.py, tool_executor.py, etc.)
│   ├── config/         # Configuration models and loading
│   ├── logging/        # Structured logging system
│   ├── knowledge/      # RAG / codebase indexing
│   ├── presenter/      # Event processing and session lifecycle
│   ├── controller/     # Thread management and event bus
│   └── cli/            # Command-line interface commands
├── session/            # Session management
│   ├── store.py        # FileSystemSessionStore (CRUD for sessions)
│   ├── models.py       # Session Pydantic model
│   ├── context_builder.py  # LLM context construction
│   └── history_pruner.py   # Conversation pruning
├── tools/              # All tool implementations (20+ tools)
├── resources/          # Global defaults and knowledge
│   ├── global_kb/      # Self-knowledge about ThoughtMachine
│   └── worker_templates/  # Specialized agent configurations
├── web_ui/             # Web-based frontend
│   ├── backend/        # FastAPI server + WebSocket bridge
│   └── frontend/       # React SPA
├── qt_gui/             # Qt6 desktop GUI
├── llm_providers/      # LLM provider implementations
├── docker/             # Dockerfile for code execution sandbox
└── thoughtmachine/     # Bootstrap and security modules
```

## Agent Core (`agent/core/`)

The brain of ThoughtMachine. Key components:

### agent.py (Coordinator)
- Processes user messages through the LLM loop
- Manages conversation history and session state
- Handles tool execution lifecycle
- Coordinates token monitoring and summarization
- Runs in a background thread via Controller

### llm_client.py
- Communicates with the configured LLM provider
- Handles request/response formatting
- Manages streaming responses
- Imports HistoryProvider from session module

### tool_executor.py
- Executes tool calls from the LLM
- Applies tool output truncation (per-tool and global limits)
- Calls security layer for capability checks
- Handles tool call errors gracefully

### conversation_manager.py
- Builds LLM context from session history
- Manages the conversation flow

### turn_transaction.py
- Manages turn lifecycle (start → LLM call → tool execution → finish)
- Tracks turn state transitions
- Handles turn limits

### token_counter.py
- Estimates token usage with tiktoken
- Tracks running totals vs. model context windows
- Generates token warnings at thresholds

### state.py
- Three-state state machine: RUNNING, PAUSING, READY
- Tracks token state, turn state, execution state
- Generates system notifications at thresholds

## Session Management (`session/`)

### Session Model (`models.py`)
- Pydantic model for conversation sessions
- Stores: session_id, metadata, user_history, config, token counts
- Supports `to_persistable_dict()` / `from_persistable_dict()` for JSON serialization
- `user_history` is append-only — full transcript preserved

### Session Store (`store.py`)
- `FileSystemSessionStore` — JSON files in `~/.thoughtmachine/sessions/`
- Full CRUD: save, load, list, delete
- Lightweight metadata files (`_meta_*.json`) for fast sidebar listing
- In-memory caching with TTL for listing and path resolution
- Open sessions tracking (`open_sessions.json`)
- Current session marker (`.current_session`)

### Context Builder (`context_builder.py`)
- Builds LLM context from user_history
- `SummaryBuilder`: includes system prompt + latest summary + messages after summary
- `TurnBuilder`: includes last N turns
- Handles token limit awareness

### History Pruner (`history_pruner.py`)
- Prunes old summarization cycles to keep session files compact
- Preserves the two most recent summary cycles

## Configuration System (`agent/config/`)

### AgentConfig (`models.py`)
- Pydantic model for all agent configuration
- Fields: model, provider, temperature, max_turns, token thresholds,
  logging settings, RAG settings, tool enable/disable, etc.

### Config Loading (`loader.py`)
- Loads from `~/.thoughtmachine/config.json`
- Falls back to resources/default_config.json
- Environment variable overrides

### Config Service (`service.py`)
- Manages config lifecycle
- Provides config to all components

## LLM Providers (`llm_providers/`)

### Architecture
- **Base class**: `LLMProvider` in `base.py`
- **Factory**: `create_provider()` in `factory.py`
- **Implementations**:
  - `openai_compatible.py` — Works with OpenAI, DeepSeek, StepFun, etc.
  - `anthropic_provider.py` — Anthropic Claude
- `tool_converter.py` — Converts tool schemas between formats

### Provider Profiles
- Defined in `agent/config/provider_profiles.py`
- Each profile specifies: provider type, model name, context window, API endpoint
- Used to auto-select correct settings for different models

## Docker Sandbox (`docker/`, `tools/docker_code_runner.py`)

### DockerCodeRunner
- Executes code in an isolated Docker container
- Container pooling for performance (deterministic naming, reused within idle timeout)
- Security: dropped capabilities, read-only rootfs, no network by default
- Policy-controlled: `~/.thoughtmachine/security_policy.json`

### Security Policy
- Controls: network access, writable home directory
- Path-based rules with glob patterns
- Default: no network, read-only home

## Security Layer (`thoughtmachine/security.py`)

- **Capability Registry**: Tools declare required capabilities (fs:read, fs:write, container:exec, etc.)
- **Policy Profiles**: default, read_only, file_editor, sandboxed, permissive, restricted
- **Status**: Partially implemented; default_policy set to "allow" in v1.0
- Security prompts for tool approval are planned for v2.0

## Web UI (`web_ui/`)

### Backend (`web_ui/backend/`)
- FastAPI server with WebSocket support
- `server.py` — Main server: REST endpoints + WebSocket for real-time communication
- `bridge.py` — Event bridge between agent controller and WebSocket clients
- Static file serving for frontend SPA

### Frontend (`web_ui/frontend/`)
- React SPA with Vite build system
- Components: ChatPanel, SessionTab, SessionList, SettingsPanel
- Real-time streaming of agent responses via WebSocket
- Markdown rendering with tool call expand/collapse

## Qt GUI (`qt_gui/`)

- PyQt6 desktop application
- Components: session_tab.py, main_window.py, themes.py
- Panels: conversation, input, settings, thinking indicator
- Event-driven updates from agent controller

## Logging System (`agent/logging/`)

### Unified log() function
- Standardized logging across all components
- Console output with timestamp and tag filtering
- JSONL file logging with rotation (10MB, 5 backups)
- Directory size capping (default 50MB)

### Configuration
| Env Var | Purpose | Default |
|---------|---------|---------|
| `TM_LOG_LEVEL` | Min console level | `INFO` |
| `TM_LOG_TAGS` | Comma-separated tags to show | (empty = WARNING+) |
| `TM_LOG_FILE_LEVEL` | Min file level | `DEBUG` |
| `TM_LOG_DIR_MAX_MB` | Log dir max size | `50` |

### Tag Convention
Tags use `area.component` format: `core.session`, `tools.file_editor`,
`llm.openai`, `server.config`, etc.

## Knowledge Base (`tools/knowledge_base.py`)

- Dual scope: workspace (`.thoughtmachine/knowledge/`) and global (`~/.thoughtmachine/knowledge/`)
- File-based storage (Markdown with YAML frontmatter)
- Domain files for different topics
- 8 modes: list, read, append, update, status, search, create_domain, summary

## RAG System (`agent/knowledge/`)

- Retrieval-Augmented Generation for semantic codebase search
- ChromaDB vector database with BAAI/bge-small-en-v1.5 embedding model
- AST-aware chunking via tree-sitter
- Separate collections per workspace
- CLI: `python -m agent.knowledge.codebase_indexer index`

## Dead Code / Cleanup Notes

| File | Status | Notes |
|------|--------|-------|
| `tools/mcp_client_new.py` | DEAD | Replaced by mcp_client.py |
| `config/` (top-level) | DEAD | Flat config files, unused |
| `session/event_schema.py` | DEAD | Parallel events system, unused |
| `qml_gui/` | STALE | Superseded by pyqt GUI |
| Various `*_orig.py`, `*.bak` | DEAD | Backup files from refactoring |
