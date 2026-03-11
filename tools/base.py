# tools/base.py
from pydantic import BaseModel, Field
from typing import Literal, Any, Optional
import os
from pathlib import Path

class ToolBase(BaseModel):
    """
    All tools must inherit from this class.
    They must define a 'tool' field with a Literal of their unique name.
    They must implement execute() returning a string.
    """
    workspace_path: Optional[str] = Field(default=None, description="Root directory for file operations (None = unrestricted)")

    def execute(self) -> str:
        raise NotImplementedError

    def model_dump_tool(self) -> dict:
        """Dump all fields except 'execute' method."""
        return self.model_dump(exclude={'execute'})

    def _validate_path(self, path: str) -> str:
        """
        Validate that a given path is within the workspace.
        Returns absolute normalized path if valid.
        Raises ValueError if path is outside workspace.
        """
        if self.workspace_path is None:
            # No restrictions
            return os.path.abspath(path)
        
        # Convert to absolute paths
        workspace_abs = os.path.abspath(self.workspace_path)
        target_abs = os.path.abspath(path)
        
        # Ensure target is within workspace
        try:
            target_rel = os.path.relpath(target_abs, workspace_abs)
        except ValueError:
            # Paths are on different drives (Windows)
            raise ValueError(f"Path {path} is outside workspace {self.workspace_path}")
        
        # Check for directory traversal attempts
        if target_rel.startswith("..") or os.path.isabs(target_rel):
            raise ValueError(f"Path {path} is outside workspace {self.workspace_path}")
        
        return target_abs
