# Tool Categories & Permission System

> **File:** `.thoughtmachine/knowledge/tool_categories.md`
> **Last updated:** 2025-07-03

---

## 1. Overview — How Tool Categories Work

Every tool in ThoughtMachine declares **permission categories** — strings like
`"filesystem:write"` or `"container:true"` — that represent the access the tool
requires. These are checked at runtime by the **security gate**
(`security/security_gate.py::check_required_categories()`) before the tool is
allowed to execute.

### Two mechanisms for declaring categories

| Mechanism | Defined in | Description |
|-----------|-----------|-------------|
| **Static `required_categories`** | `tools/base.py::ToolBase.required_categories` (ClassVar) | A fixed list set on the class; applies to all invocations. |
| **Dynamic `get_required_categories()`** | `ToolBase.get_required_categories(cls, params)` classmethod | Override to return different categories based on tool parameters. Default implementation returns the static `required_categories`. |

### How the security gate checks them

1. `ToolExecutor` calls `tool_class.get_required_categories(params)` before
   executing the tool.
2. The resulting list (e.g., `["filesystem:write"]`) is passed to
   `check_required_categories()` along with the session's effective permissions.
3. Each entry is parsed as `"domain:value"`. The value is compared against the
   session's allowed level for that domain (banned < ask < read < write < full).
4. If any required category exceeds the allowed level, the tool is **blocked**
   (or the user may be prompted to approve it, depending on config).

### No `category` field exists

There is **no `category` field** on `ToolBase` or any tool class for grouping
tools. The only `category` field in the codebase is in `KnowledgeBaseTool`,
where it refers to **knowledge base workspace category** (`project` vs.
`personal` / `user` vs. `system`), not tool classification.

---

## 2. Complete Tool Category Table

### Tools with no special permissions (`required_categories = []`)

| Tool Class | File | Notes |
|-----------|------|-------|
| `Thought` | `tools/thought.py` | Read-only introspection tool |
| `Respond` | `tools/respond.py` | **Dynamic**: returns `["filesystem:write"]` when `report_body` is provided |
| `SummarizeTool` | `tools/summarize_tool.py` | Conversation summary; no file access |
| `DateTimeTool` | `tools/datetime_tool.py` | Date/time formatting only |
| `CheckSystem` | `tools/workspace/check_system.py` | Environment diagnostics only |

### Read-only filesystem tools (`["filesystem:read"]`)

| Tool Class | File | Notes |
|-----------|------|-------|
| `ReadFile` | `tools/read_file_tool.py` | **Deprecated** — blacklisted from `SIMPLIFIED_TOOL_CLASSES`, replaced by `FileEditor` |
| `FilePreviewTool` | `tools/file_preview_tool.py` | Preview file contents |
| `DirectoryTreeTool` | `tools/directory_tree_tool.py` | Directory tree listing |
| `GlobTool` | `tools/glob_tool.py` | Glob pattern matching |
| `FileSearchTool` | `tools/file_search_tool.py` | Text search within files |
| `SearchCodebaseTool` | `tools/search_codebase.py` | Codebase-wide search |
| `FileSummaryTool` | `tools/file_summary_tool.py` | Summarise file structure |
| `FieldViewer` | `tools/field_viewer.py` | View tool/field definitions |
| `PaginateTool` | `tools/paginate_tool.py` | Paginate long outputs |

### Write-capable filesystem tools (`["filesystem:write"]`)

These require **write** access, which implies read access (write > read in the
permission hierarchy).

| Tool Class | File | Notes |
|-----------|------|-------|
| `FileEditor` | `tools/file_editor.py` | **Dynamic**: returns `["filesystem:read"]` for read/grep operations, `["filesystem:write"]` for all others |
| `ApplyEdits` | `tools/apply_edits.py` | Search/replace edits |
| `CodeModifier` | `tools/code_modifier.py` | AST-level code modifications |
| `RefactorTool` | `tools/refactor_tool.py` | Refactoring operations |
| `DirectoryCreator` | `tools/directory_creator.py` | Create directories |
| `FileMover` | `tools/file_mover.py` | Move/rename files |
| `ProgressReport` | `tools/progress_report.py` | **Dynamic only** — no static `required_categories`, always returns `["filesystem:write"]` |
| `KnowledgeBaseTool` | `tools/knowledge_base.py` | **Dynamic**: returns `["filesystem:write"]` for `append`, `update`, `create_domain` modes; returns `[]` for read-only modes |

### Container tools

| Tool Class | Required Categories | File | Notes |
|-----------|-------------------|------|-------|
| `DockerCodeRunner` | `["filesystem:write", "container:true"]` | `tools/docker_code_runner.py` | Needs both filesystem write (to write scripts) and container access |
| `EditDockerfile` | `["container:write"]` | `tools/workspace/edit_dockerfile.py` | Only modifies Dockerfiles |

### Git & Network tools

| Tool Class | Static | Dynamic | File |
|-----------|--------|---------|------|
| `GitInfoTool` | `[]` | `["git:read"]` for read ops (status, log, diff), `["git:write"]` for write ops (commit, init), `["network:outbound"]` for remote operations (push, pull, fetch) | `tools/git_info_tool.py` |

### MCP tools

| Tool Class | Static | Dynamic | File |
|-----------|--------|---------|------|
| `MCPValidator` | `[]` | `["network:outbound", "filesystem:read"]` for HTTP/SSE test connections; otherwise `["filesystem:read"]` | `tools/mcp_validator.py` |

### Worker tools

| Tool Class | Required Categories | File | Notes |
|-----------|-------------------|------|-------|
| `Worker` | `["execution:read"]` | `tools/workspace/worker.py` | Also performs internal `check_required_categories()` for sub-tools when spawning workers |

---

## 3. Category Domain Reference

The permission system organises categories into **domains** with a **level**
hierarchy:

| Domain | Description | Permitted values |
|--------|-------------|-----------------|
| `filesystem` | File read/write access | `read`, `write` |
| `container` | Container/Docker access | `true` (write-level) |
| `git` | Git repository operations | `read`, `write` |
| `network` | Network outbound access | `outbound` (write-level) |
| `execution` | Sub-agent/tool execution | `read` |

### Level hierarchy (from least to most permissive)

```
banned < ask < read < write < full
```

- `read` — read-only access
- `write` — read + write access (implies read)
- `true` / `outbound` — treated as write-level (3)
- `full` — maximum access (4)
- `ask` — prompt user for approval if required level > read

---

## 4. How Categories Are Used in the System

### Registration (`tools/__init__.py`)

- `TOOL_CLASSES`: A flat list of all 27+ registered tool classes.
- `SIMPLIFIED_TOOL_CLASSES`: Same as `TOOL_CLASSES` minus deprecated file tools
  listed in `FILE_TOOL_BLACKLIST` (FileLineReader, FileLineWriter, etc.).

### Agent configuration (`agent/config/models.py`)

`AgentConfig.enabled_tools` filters which tools are available. The
`get_filtered_tool_classes()` method additionally respects `rag_enabled` and
`kb_enabled` flags. Categories are **not** used for filtering — only the
security gate uses them.

### Worker tool resolution (`tools/workspace/worker.py`)

When a `WorkerDefinition` specifies a tools list, the worker resolves tool
classes from `SIMPLIFIED_TOOL_CLASSES` minus the `_WORKER_BLOCKLIST` (Worker,
EditDockerfile, MCPValidator). The worker also performs its own
`check_required_categories()` check before spawning sub-tools.

### Security gate (`security/security_gate.py`)

```python
def check_required_categories(
    required: List[str],       # e.g. ["filesystem:write"]
    effective: Dict[str, Any], # session permissions
    tool_name: str,
    tool_args: Dict[str, Any],
    description: str,
    event_bus: Any,
    ...
) -> Tuple[bool, str]:
```

Called by `ToolExecutor` for every tool invocation. Returns `(True, "")` if
allowed, or `(False, reason)` if denied.

---

## 5. ToolBase Reference (`tools/base.py`)

```python
class ToolBase(BaseModel):
    # ...
    # Security capabilities required by this tool
    requires_capabilities: ClassVar[List[str]] = []

    # Permission categories required by this tool (e.g., ['container:true'])
    required_categories: ClassVar[List[str]] = []

    # If True, framework-level output truncation is skipped
    skip_output_truncation: ClassVar[bool] = False

    @classmethod
    def get_required_categories(cls, params: dict | None = None) -> list[str]:
        """
        Return the permission categories required for this tool given params.
        Default returns cls.required_categories.
        Subclasses override for operation-level granularity.
        """
        return cls.required_categories
```

---

## 6. Quick Reference — Which Tools Can Read/Write

| Access Level | Tools |
|-------------|-------|
| **No filesystem access** | `Thought`, `SummarizeTool`, `DateTimeTool`, `CheckSystem`, `Respond` (without report_body) |
| **Read-only filesystem** | `ReadFile`*, `FilePreviewTool`, `DirectoryTreeTool`, `GlobTool`, `FileSearchTool`, `SearchCodebaseTool`, `FileSummaryTool`, `FieldViewer`, `PaginateTool`, `MCPValidator` (local), `FileEditor` (read/grep only) |
| **Write-capable filesystem** | `FileEditor` (write/delete), `ApplyEdits`, `CodeModifier`, `RefactorTool`, `DirectoryCreator`, `FileMover`, `ProgressReport`, `KnowledgeBaseTool` (write modes), `DockerCodeRunner` |
| **Container access** | `DockerCodeRunner` (+filesystem), `EditDockerfile` |
| **Git access** | `GitInfoTool` (read/write/network) |
| **Network access** | `GitInfoTool` (remote ops), `MCPValidator` (HTTP/SSE tests) |
| **Sub-agent execution** | `Worker` |

> \* `ReadFile` is deprecated and excluded from `SIMPLIFIED_TOOL_CLASSES`.
