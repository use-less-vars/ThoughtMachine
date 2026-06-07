# ThoughtMachine Capabilities — Complete Tool Reference

All tools are available to the agent during a session. The agent chooses which
tools to use based on the task.

---

## File Operations Tools

### FileEditor
The primary file editing tool. Supports: read, write, insert, append, replace,
delete, and grep operations. Can operate on single files or batches.

**When to use**: Most file operations. The go-to tool for reading, writing, and
editing files. Use `line_number` + `context_lines` for reading with context,
`line_numbers: "start-end"` for ranges.

### FilePreviewTool
Shows the beginning and end of a file with line numbers. Great for quickly
understanding a file's structure without reading the whole thing.

**When to use**: When you need to see a file's head/tail, or specific line ranges.

### FileMover
Move files and directories. Supports single file, batch via list, glob patterns,
and structure-preserving moves.

**When to use**: Renaming or reorganizing files.

### DirectoryCreator
Create directories (including parents).

**When to use**: Setting up project structure.

---

## Code Modification Tools

### ApplyEdits
Search/replace editing with resilient matching. Supports regex, multiline edits,
batch editing across multiple files via `file_pattern`, and preview mode. Edits
are applied sequentially; if any find fails, the file is unchanged.

**When to use**: **Preferred** for most code changes. Best for:
- Search/replace across single or multiple files
- Changing specific patterns in code
- When you need preview mode to verify changes

### CodeModifier
Structural Python code modification using AST. Supports: add_function,
add_method, add_import, add_class, replace_function_body, modify_function.

**When to use**: Second choice for Python-specific changes. Best for:
- Adding methods to classes
- Replacing function bodies
- Structural changes where precise placement matters

### RefactorTool
Apply CodeModifier operations to ALL files matching a glob pattern. Supports
preview mode and atomic application (all files succeed or none are written).

**When to use**: Cross-file structural changes, like adding a method to all
classes across a codebase.

---

## Code Analysis Tools

### FileSearchTool
Search for patterns across files with regex, multiline matching, context lines,
and line numbers. Supports `(?s)` flag for dot-matches-newline regex.

**When to use**: Finding text patterns, function definitions, imports, etc.

### SearchCodebaseTool
Semantic code search using vector embeddings (RAG). Finds code by meaning rather
than exact text matches. Returns relevance-scored snippets.

**When to use**: Understanding code purpose, finding implementations, discovering
architecture. Best when you know WHAT you're looking for but not the exact text.

### FileSummaryTool
Extract structural elements from code files using AST parsing — classes,
functions, imports.

**When to use**: Quick understanding of a file's structure.

### DirectoryTreeTool
Show directory structure as a tree or flat list. Supports recursion limits,
pattern matching, and size display.

**When to use**: Understanding project layout, finding files.

### GlobTool
Find files using glob patterns with pagination and directory exclusion.

**When to use**: Finding files by name pattern.

### FieldViewer
Parse Python files, find Pydantic model definitions, and display fields with
types and docstrings.

**When to use**: Understanding data models and schemas.

---

## Execution Tools

### DockerCodeRunner
Execute code, scripts, and shell commands in a secure Docker container.
Features:
- Runs Python, bash, or any shell command
- Container pooling for performance (reused within 600s idle timeout)
- No network by default (configurable via security policy)
- Writable home directory for `pip install --user` (policy-controlled)
- Read-only root filesystem — system cannot be modified
- Template variables: `{workspace}`, `{timestamp}`, `{date}`, `{time}`, `{random_id}`

**When to use**: Running code, testing scripts, installing packages, running
build tools.

---

## Git Tools

### GitInfoTool
Read-only Git repository information. Operations: status, diff, log, branch,
show, remote, blame, config.

**When to use**: Understanding git state, viewing changes, examining history.
All operations are read-only — no commits or pushes.

---

## Knowledge Tools

### KnowledgeBaseTool
Persistent project notebook for storing and retrieving information. Two scopes:
- `scope=workspace` — project-local KB in `.thoughtmachine/knowledge/`
- `scope=global` — user-wide KB in `~/.thoughtmachine/knowledge/`

**Modes**: list, read, append, update, status, search, create_domain, summary

**Domains** (workspace scope): system_architecture, development_guides, roadmap,
bugs_and_fixes, lessons_learned, task_tracker

**When to use**: Before starting tasks (status), recording findings, storing
architecture decisions, logging bugs.

---

## MCP Tools

### MCPValidator
Validate MCP server configurations and test connectivity. Supports stdio and
HTTP/SSE transports.

**When to use**: Setting up or debugging MCP server connections.

---

## Utility Tools

### DateTimeTool
Get current date/time, format timestamps, parse strings, calculate differences.

**When to use**: Any time-related operation.

### PaginateTool
Wrap another tool's execution with pagination for large result sets.

**When to use**: Getting more results from GlobTool, DirectoryTreeTool, etc.

### ProgressReport
Write a timestamped progress report during long-running tasks without stopping
agent execution.

**When to use**: During batch jobs or long operations to document intermediate
progress.

### Thought
Write down reasoning notes without taking any other action. Useful for planning
and analysis.

**When to use**: When you need to think through a problem before acting.

---

## Communication Tools

### Respond
The unified response tool (replaces Final, FinalReport, RequestUserInteraction).
Sends messages to the user with optional downloadable report attachments.

**Parameters**: `content` (required), `response_type` ("answer" or "question"),
`report_body` + `report_title` (optional report file).

### SummarizeTool
Write a summary of the conversation and specify how many recent turns to keep.
Older turns are replaced with the summary, freeing context window space.

**When to use**: When approaching token limits. The only tool (alongside Respond)
available during critical token restriction.

---

## Architecture Overview

```
                    ┌──────────────────────┐
                    │     LLM Provider      │
                    │ (OpenAI/Anthropic/etc)│
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │    Agent Core         │
                    │  (agent.py)           │
                    │  ┌─────────────────┐  │
                    │  │ ToolExecutor     │  │
                    │  │ TokenCounter     │  │
                    │  │ ConversationMgr  │  │
                    │  │ TurnTransaction  │  │
                    │  └─────────────────┘  │
                    └──────────┬───────────┘
                               │
          ┌────────────────────┼────────────────────┐
          ▼                    ▼                    ▼
   ┌──────────┐         ┌──────────┐         ┌──────────┐
   │  Tools   │         │ Sessions │         │  Config  │
   │(20+ tools)│         │ (persist)│         │(Pydantic)│
   └──────────┘         └──────────┘         └──────────┘
```

## Data Flow

1. **User sends message** → `Respond` tool or WebSocket input
2. **Agent Core** adds message to session, updates token counts
3. **LLM Request** — agent sends conversation context to LLM
4. **LLM Response** — may contain tool calls, reasoning, or final answer
5. **Tool Execution** — if tool calls present, agent executes them
6. **Response** — tool results fed back to LLM or final answer sent to user
7. **Save** — session is persisted to disk after each significant change

## Tool Preference Hierarchy

For code changes, the agent prefers:
1. **ApplyEdits** — search/replace, supports regex, batch, preview
2. **CodeModifier** — structural Python changes (AST-based)
3. **FileEditor** — simple line-by-line operations (fallback)

For code understanding, the agent prefers:
1. **FileSearchTool** — exact text search with context
2. **SearchCodebaseTool** — semantic search (RAG)
3. **FileSummaryTool** — structural overview
4. **FilePreviewTool** — preview head/tail of files
