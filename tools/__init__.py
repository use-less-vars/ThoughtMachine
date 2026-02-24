# tools/__init__.py
import importlib
import pkgutil
from pathlib import Path
from typing import Dict, Type, Union

from .base import ToolBase

# Registry: tool name -> Tool class
TOOL_REGISTRY: Dict[str, Type[ToolBase]] = {}

# Automatically discover all .py files in this directory (except __init__.py and base.py)
tools_dir = Path(__file__).parent
for file in tools_dir.glob("*.py"):
    if file.name.startswith("_"):
        continue
    module_name = f"tools.{file.stem}"
    module = importlib.import_module(module_name)
    # Find all classes that inherit from ToolBase
    for attr_name in dir(module):
        cls = getattr(module, attr_name)
        if isinstance(cls, type) and issubclass(cls, ToolBase) and cls != ToolBase:
            # Extract the tool name from the Literal default
            # We assume the class has a field 'tool' with a Literal value
            tool_field = cls.model_fields.get('tool')
            if tool_field and hasattr(tool_field, 'default'):
                tool_name = tool_field.default
            else:
                # fallback: use class name lowercased
                tool_name = cls.__name__.lower()
            TOOL_REGISTRY[tool_name] = cls

# Build the Union type for instructor
AgentResponse = Union[tuple(TOOL_REGISTRY.values())]  # type: ignore
