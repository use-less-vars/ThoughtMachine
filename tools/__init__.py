# tools/__init__.py
import importlib
from pathlib import Path
from typing import List, Type
from .base import ToolBase

TOOL_CLASSES: List[Type[ToolBase]] = []

tools_dir = Path(__file__).parent
for file in tools_dir.glob("*.py"):
    if file.name.startswith("_"):
        continue
    module_name = f"tools.{file.stem}"
    module = importlib.import_module(module_name)
    for attr_name in dir(module):
        cls = getattr(module, attr_name)
        if isinstance(cls, type) and issubclass(cls, ToolBase) and cls != ToolBase:
            TOOL_CLASSES.append(cls)

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

SIMPLIFIED_TOOL_CLASSES = [
    cls for cls in TOOL_CLASSES 
    if cls.__name__ not in FILE_TOOL_BLACKLIST
]

# Ensure FileEditor is included (in case it wasn't discovered yet)
try:
    from .file_editor import FileEditor
    if FileEditor not in SIMPLIFIED_TOOL_CLASSES:
        SIMPLIFIED_TOOL_CLASSES.append(FileEditor)
except ImportError:
    pass

__all__ = ['TOOL_CLASSES', 'SIMPLIFIED_TOOL_CLASSES']