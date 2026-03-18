# tools/__init__.py
import importlib
import logging
from pathlib import Path
from typing import List, Type
from .base import ToolBase

logger = logging.getLogger(__name__)

TOOL_CLASSES: List[Type[ToolBase]] = []

tools_dir = Path(__file__).parent
for file in tools_dir.glob("*.py"):
    if file.name.startswith("_"):
        continue
    module_name = f"tools.{file.stem}"
    try:
        module = importlib.import_module(module_name)
        for attr_name in dir(module):
            cls = getattr(module, attr_name)
            if isinstance(cls, type) and issubclass(cls, ToolBase) and cls != ToolBase:
                if cls not in TOOL_CLASSES:  # Prevent duplicates
                    TOOL_CLASSES.append(cls)
    except Exception as e:
        logger.warning(f"Failed to load module {module_name}: {e}")

# Try to load MCP tools (if available and configured)
try:
    from .mcp_manager import register_mcp_tools
    register_mcp_tools()
    logger.info("MCP tools registered successfully")
except ImportError as e:
    logger.debug(f"MCP tools not available: {e}")
except Exception as e:
    logger.warning(f"Failed to register MCP tools: {e}")

# Define a simplified toolset that excludes redundant file operation tools
# Keep only unified FileEditor and essential file management tools
FILE_TOOL_BLACKLIST = {
    'FileLineReader',
    'FileLineWriter', 
    'FileLineInserter',
    'FileLineAppender',
    'FileLineReplacer',
    'FileLineDeleter',
    'FileReader',
    'FileWriter',
}

# Create simplified toolset, removing duplicates by class object
SIMPLIFIED_TOOL_CLASSES = []
seen_classes = set()
for cls in TOOL_CLASSES:
    if cls.__name__ not in FILE_TOOL_BLACKLIST and cls not in seen_classes:
        seen_classes.add(cls)
        SIMPLIFIED_TOOL_CLASSES.append(cls)

# Ensure FileEditor is included (in case it wasn't discovered yet)
try:
    from .file_editor import FileEditor
    if FileEditor not in SIMPLIFIED_TOOL_CLASSES:
        SIMPLIFIED_TOOL_CLASSES.append(FileEditor)
except ImportError:
    pass

__all__ = ['TOOL_CLASSES', 'SIMPLIFIED_TOOL_CLASSES']
