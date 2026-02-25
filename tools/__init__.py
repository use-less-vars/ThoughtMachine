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

__all__ = ['TOOL_CLASSES']