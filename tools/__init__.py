# tools/__init__.py - Explicit tool registration to avoid circular imports
import logging
from typing import List, Dict, Type, Set
from .base import ToolBase

logger = logging.getLogger(__name__)

# Import-failure registry: no tool import failure is silent anymore.
# Every failure is recorded here (and logged at ERROR level) so the agent
# can surface degraded tooling instead of quietly missing tools.
IMPORT_FAILURES: List[Dict[str, str]] = []


def _record_import_failure(tool_name: str, exc: Exception) -> None:
    """Record a tool import failure and log it loudly."""
    IMPORT_FAILURES.append({"tool": tool_name, "error": str(exc)})
    logger.error(f"Failed to import {tool_name}: {exc}")


def get_import_failures() -> List[Dict[str, str]]:
    """Return a copy of the recorded tool import failures."""
    return list(IMPORT_FAILURES)

# Global tool registries
TOOL_CLASSES: List[Type[ToolBase]] = []
SIMPLIFIED_TOOL_CLASSES: List[Type[ToolBase]] = []

# Define a simplified toolset that excludes redundant file operation tools
# Keep only unified FileEditor and essential file management tools
FILE_TOOL_BLACKLIST: Set[str] = {
    'FileLineReader',
    'FileLineWriter',
    'FileLineInserter',
    'FileLineAppender',
    'FileLineReplacer',
    'FileLineDeleter',
    'FileReader',
    'FileWriter',
}

def _update_simplified_toolset() -> None:
    """Update SIMPLIFIED_TOOL_CLASSES based on current TOOL_CLASSES and blacklist."""
    global SIMPLIFIED_TOOL_CLASSES
    seen_classes: Set[Type[ToolBase]] = set()
    simplified = []
    
    for cls in TOOL_CLASSES:
        if cls.__name__ not in FILE_TOOL_BLACKLIST and cls not in seen_classes:
            seen_classes.add(cls)
            simplified.append(cls)
    
    # Ensure FileEditor is included (in case it wasn't discovered yet)
    try:
        from .file_editor import FileEditor
        if FileEditor not in simplified:
            simplified.append(FileEditor)
    except ImportError:
        pass
    
    SIMPLIFIED_TOOL_CLASSES = simplified

def register_tool(cls: Type[ToolBase]) -> Type[ToolBase]:
    """Decorator to register tool classes and update simplified toolset."""
    if cls not in TOOL_CLASSES:
        TOOL_CLASSES.append(cls)
        _update_simplified_toolset()
    return cls

# Import all tool modules explicitly
# Note: Import order matters for potential dependencies

try:
    from .file_editor import FileEditor
    TOOL_CLASSES.append(FileEditor)
except ImportError as e:
    _record_import_failure("FileEditor", e)

try:
    from .read_file_tool import ReadFile
    TOOL_CLASSES.append(ReadFile)
except ImportError as e:
    _record_import_failure("ReadFile", e)

try:
    from .file_preview_tool import FilePreviewTool
    TOOL_CLASSES.append(FilePreviewTool)
except ImportError as e:
    _record_import_failure("FilePreviewTool", e)

try:
    from .directory_tree_tool import DirectoryTreeTool
    TOOL_CLASSES.append(DirectoryTreeTool)
except ImportError as e:
    _record_import_failure("DirectoryTreeTool", e)

try:
    from .glob_tool import GlobTool
    TOOL_CLASSES.append(GlobTool)
except ImportError as e:
    _record_import_failure("GlobTool", e)

try:
    from .file_search_tool import FileSearchTool
    TOOL_CLASSES.append(FileSearchTool)
except ImportError as e:
    _record_import_failure("FileSearchTool", e)

try:
    from .apply_edits import ApplyEdits
    TOOL_CLASSES.append(ApplyEdits)
except ImportError as e:
    _record_import_failure("ApplyEdits", e)

try:
    from .code_modifier import CodeModifier
    TOOL_CLASSES.append(CodeModifier)
except ImportError as e:
    _record_import_failure("CodeModifier", e)

try:
    from .refactor_tool import RefactorTool
    TOOL_CLASSES.append(RefactorTool)
except ImportError as e:
    _record_import_failure("RefactorTool", e)

try:
    from .search_codebase import SearchCodebaseTool
    TOOL_CLASSES.append(SearchCodebaseTool)
except ImportError as e:
    _record_import_failure("SearchCodebaseTool", e)

try:
    from .datetime_tool import DateTimeTool
    TOOL_CLASSES.append(DateTimeTool)
except ImportError as e:
    _record_import_failure("DateTimeTool", e)

try:
    from .directory_creator import DirectoryCreator
    TOOL_CLASSES.append(DirectoryCreator)
except ImportError as e:
    _record_import_failure("DirectoryCreator", e)

try:
    from .docker_code_runner import DockerCodeRunner
    TOOL_CLASSES.append(DockerCodeRunner)
except ImportError as e:
    _record_import_failure("DockerCodeRunner", e)

try:
    from .container_control import (
        ContainerStartTool,
        ContainerExecTool,
        ContainerStopTool,
        ContainerStatusTool,
        ContainerListTool,
        ContainerBuildTool,
        ContainerLogsTool,
    )
    TOOL_CLASSES.append(ContainerStartTool)
    TOOL_CLASSES.append(ContainerExecTool)
    TOOL_CLASSES.append(ContainerStopTool)
    TOOL_CLASSES.append(ContainerStatusTool)
    TOOL_CLASSES.append(ContainerListTool)
    TOOL_CLASSES.append(ContainerBuildTool)
    TOOL_CLASSES.append(ContainerLogsTool)
except ImportError as e:
    _record_import_failure("container_control", e)

try:
    from .field_viewer import FieldViewer
    TOOL_CLASSES.append(FieldViewer)
except ImportError as e:
    _record_import_failure("FieldViewer", e)

try:
    from .file_mover import FileMover
    TOOL_CLASSES.append(FileMover)
except ImportError as e:
    _record_import_failure("FileMover", e)

try:
    from .file_summary_tool import FileSummaryTool
    TOOL_CLASSES.append(FileSummaryTool)
except ImportError as e:
    _record_import_failure("FileSummaryTool", e)

# Final/FinalReport removed in Phase B — use Respond instead

try:
    from .git_info_tool import GitInfoTool
    TOOL_CLASSES.append(GitInfoTool)
except ImportError as e:
    _record_import_failure("GitInfoTool", e)

try:
    from .knowledge_base import KnowledgeBaseTool
    TOOL_CLASSES.append(KnowledgeBaseTool)
except ImportError as e:
    _record_import_failure("KnowledgeBaseTool", e)

try:
    from .mcp_validator import MCPValidator
    TOOL_CLASSES.append(MCPValidator)
except ImportError as e:
    _record_import_failure("MCPValidator", e)

try:
    from .paginate_tool import PaginateTool
    TOOL_CLASSES.append(PaginateTool)
except ImportError as e:
    _record_import_failure("PaginateTool", e)

try:
    from .progress_report import ProgressReport
    TOOL_CLASSES.append(ProgressReport)
except ImportError as e:
    _record_import_failure("ProgressReport", e)

# RequestUserInteraction removed in Phase B — use Respond instead
try:
    from .respond import Respond
    TOOL_CLASSES.append(Respond)
except ImportError as e:
    _record_import_failure("Respond", e)


try:
    from .summarize_tool import SummarizeTool
    TOOL_CLASSES.append(SummarizeTool)
except ImportError as e:
    _record_import_failure("SummarizeTool", e)

try:
    from .thought import Thought
    TOOL_CLASSES.append(Thought)
except ImportError as e:
    _record_import_failure("Thought", e)

try:
    from .workspace.check_system import CheckSystem
    TOOL_CLASSES.append(CheckSystem)
except ImportError as e:
    _record_import_failure("CheckSystem", e)

try:
    from .workspace.worker import Worker
    TOOL_CLASSES.append(Worker)
except ImportError as e:
    _record_import_failure("Worker", e)

try:
    from .workspace.working_document import WorkingDocument
    TOOL_CLASSES.append(WorkingDocument)
except ImportError as e:
    _record_import_failure("WorkingDocument", e)

# MCP tools are registered lazily via register_mcp_tools() when the agent starts.
# Do NOT call register_mcp_tools() here - it starts MCP server subprocesses
# which can hang if servers are unavailable (see bug #BUG001).

# Initialize SIMPLIFIED_TOOL_CLASSES
_update_simplified_toolset()

__all__ = ['TOOL_CLASSES', 'SIMPLIFIED_TOOL_CLASSES', 'IMPORT_FAILURES', 'get_import_failures', 'register_tool', 'ToolBase']