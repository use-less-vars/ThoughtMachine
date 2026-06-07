# ThoughtMachine Overview

## What is ThoughtMachine?

ThoughtMachine is an AI software engineering agent — a specialized AI system designed
to help with software projects by writing code, understanding codebases, running
commands, and managing development workflows. It operates as a conversational
assistant with access to a rich set of tools for interacting with files, code,
containers, and version control.

## Core Identity

- **Role**: AI agent for software engineering tasks
- **Interface**: Conversational — users give natural language instructions, TM
  responds with actions, analysis, and code
- **Environment**: Works inside any project directory — no special setup required
  beyond configuration
- **Persistence**: Sessions are saved and can be continued across restarts

## Key Features

| Feature | Description |
|---------|-------------|
| **File Operations** | Read, write, edit, search, and preview files with precision |
| **Code Modification** | Structural code changes via AST-aware tools (Python), search/replace for any language |
| **Code Analysis** | Search (text + semantic), file summarization, directory tree visualization |
| **Code Execution** | Run code in a secure Docker sandbox — isolated, no network by default |
| **Git Operations** | Read-only git info (status, diff, log, blame) |
| **Knowledge Base** | Persistent project notebook — architecture notes, bugs, lessons learned |
| **Session Management** | Full save/load/continue — never lose context |
| **Web UI** | Browser-based frontend with real-time streaming, session management |
| **Qt GUI** | Desktop application with rich UI |
| **Multi-LLM** | Support for OpenAI, Anthropic, and any OpenAI-compatible provider |
| **MCP** | Model Context Protocol — integrate external tools via MCP servers |
| **RAG** | Semantic codebase search via vector embeddings |
| **Logging** | Structured JSONL logging with console filtering |

## Overview of Architecture

```
User ←→ LLM ←→ Agent Core ←→ Tools ←→ File System / Docker / Git / etc.
                ↓
            Session Store (persistence)
```

- **Agent Core**: Coordinates LLM communication, tool execution, conversation management
- **Tools**: Specialized functions the agent calls (FileEditor, DockerCodeRunner, etc.)
- **Sessions**: Full conversation history persisted to disk as JSON
- **Config**: Pydantic-validated configuration with file-based overrides

## Who ThoughtMachine Is For

- **Developers** who want an AI pair programmer that works in their project
- **Teams** who need consistent, reproducible AI assistance
- **Anyone** writing software who wants help with code, debugging, refactoring

## Quick Facts

- **Language**: Python 3.11+
- **Config location**: `~/.thoughtmachine/config.json`
- **Sessions location**: `~/.thoughtmachine/sessions/`
- **KB (workspace)**: `.thoughtmachine/knowledge/` in each project
- **KB (global)**: `~/.thoughtmachine/knowledge/`
- **Docker**: Optional — used for secure code execution
- **Frontend**: React SPA served by the backend or standalone Vite dev server
