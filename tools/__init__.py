# tools/__init__.py - Explicit tool registration to avoid circular imports
import logging
from typing import List, Type, Set
from .base import ToolBase

logger = logging.getLogger(__name__)

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
    logger.warning(f"Failed to import FileEditor: {e}")

try:
    from .file_preview_tool import FilePreviewTool
    TOOL_CLASSES.append(FilePreviewTool)
except ImportError as e:
    logger.warning(f"Failed to import FilePreviewTool: {e}")

try:
    from .directory_tree_tool import DirectoryTreeTool
    TOOL_CLASSES.append(DirectoryTreeTool)
except ImportError as e:
    logger.warning(f"Failed to import DirectoryTreeTool: {e}")

try:
    from .glob_tool import GlobTool
    TOOL_CLASSES.append(GlobTool)
except ImportError as e:
    logger.warning(f"Failed to import GlobTool: {e}")

try:
    from .file_search_tool import FileSearchTool
    TOOL_CLASSES.append(FileSearchTool)
except ImportError as e:
    logger.warning(f"Failed to import FileSearchTool: {e}")

try:
    from .apply_edits import ApplyEdits
    TOOL_CLASSES.append(ApplyEdits)
except ImportError as e:
    logger.warning(f"Failed to import ApplyEdits: {e}")

try:
    from .code_modifier import CodeModifier
    TOOL_CLASSES.append(CodeModifier)
except ImportError as e:
    logger.warning(f"Failed to import CodeModifier: {e}")

try:
    from .refactor_tool import RefactorTool
    TOOL_CLASSES.append(RefactorTool)
except ImportError as e:
    logger.warning(f"Failed to import RefactorTool: {e}")

try:
    from .search_codebase import SearchCodebaseTool
    TOOL_CLASSES.append(SearchCodebaseTool)
except ImportError as e:
    logger.warning(f"Failed to import SearchCodebaseTool: {e}")

try:
    from .datetime_tool import DateTimeTool
    TOOL_CLASSES.append(DateTimeTool)
except ImportError as e:
    logger.warning(f"Failed to import DateTimeTool: {e}")

try:
    from .directory_creator import DirectoryCreator
    TOOL_CLASSES.append(DirectoryCreator)
except ImportError as e:
    logger.warning(f"Failed to import DirectoryCreator: {e}")

try:
    from .docker_code_runner import DockerCodeRunner
    TOOL_CLASSES.append(DockerCodeRunner)
except ImportError as e:
    logger.warning(f"Failed to import DockerCodeRunner: {e}")

try:
    from .field_viewer import FieldViewer
    TOOL_CLASSES.append(FieldViewer)
except ImportError as e:
    logger.warning(f"Failed to import FieldViewer: {e}")

try:
    from .file_mover import FileMover
    TOOL_CLASSES.append(FileMover)
except ImportError as e:
    logger.warning(f"Failed to import FileMover: {e}")

try:
    from .file_summary_tool import FileSummaryTool
    TOOL_CLASSES.append(FileSummaryTool)
except ImportError as e:
    logger.warning(f"Failed to import FileSummaryTool: {e}")

try:
    from .final import Final
    TOOL_CLASSES.append(Final)
except ImportError as e:
    logger.warning(f"Failed to import Final: {e}")

try:
    from .final_report import FinalReport
    TOOL_CLASSES.append(FinalReport)
except ImportError as e:
    logger.warning(f"Failed to import FinalReport: {e}")

try:
    from .git_info_tool import GitInfoTool
    TOOL_CLASSES.append(GitInfoTool)
except ImportError as e:
    logger.warning(f"Failed to import GitInfoTool: {e}")

try:
    from .mcp_validator import MCPValidator
    TOOL_CLASSES.append(MCPValidator)
except ImportError as e:
    logger.warning(f"Failed to import MCPValidator: {e}")

try:
    from .paginate_tool import PaginateTool
    TOOL_CLASSES.append(PaginateTool)
except ImportError as e:
    logger.warning(f"Failed to import PaginateTool: {e}")

try:
    from .progress_report import ProgressReport
    TOOL_CLASSES.append(ProgressReport)
except ImportError as e:
    logger.warning(f"Failed to import ProgressReport: {e}")

try:
    from .request_user_interaction import RequestUserInteraction
    TOOL_CLASSES.append(RequestUserInteraction)
except ImportError as e:
    logger.warning(f"Failed to import RequestUserInteraction: {e}")

try:
    from .summarize_tool import SummarizeTool
    TOOL_CLASSES.append(SummarizeTool)
except ImportError as e:
    logger.warning(f"Failed to import SummarizeTool: {e}")

try:
    from .thought import Thought
    TOOL_CLASSES.append(Thought)
except ImportError as e:
    logger.warning(f"Failed to import Thought: {e}")

try:
    from .mcp_manager import register_mcp_tools
    register_mcp_tools()
    logger.info("MCP tools registered successfully")
except ImportError as e:
    logger.debug(f"MCP tools not available: {e}")
except Exception as e:
    logger.warning(f"Failed to register MCP tools: {e}")

# Initialize SIMPLIFIED_TOOL_CLASSES
_update_simplified_toolset()

__all__ = ['TOOL_CLASSES', 'SIMPLIFIED_TOOL_CLASSES', 'register_tool', 'ToolBase']