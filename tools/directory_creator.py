from typing import Literal
from pathlib import Path
from .base import ToolBase
import os
from pydantic import Field

class DirectoryCreator(ToolBase):
    """Create directories"""
    directory_path: str = Field(description="Directory path to create")
    parents: bool = Field(default=True, description="Create parent directories if they don't exist")
    exist_ok: bool = Field(default=True, description="Don't raise error if directory already exists")
    workspace: Literal["stable", "construction"] = Field(
        default="stable",
        description="Workspace to operate in: 'stable' (current directory) or 'construction' (./construction/ directory)"
    )

    def _adjust_path(self, path: str) -> str:
        """Adjust path based on workspace setting."""
        if self.workspace == "construction":
            # Ensure construction directory exists (parent of target)
            Path("./construction").mkdir(parents=True, exist_ok=True)
            # If path is absolute, keep it as is (no workspace mapping)
            if os.path.isabs(path):
                return path
            # Prefix with construction directory
            return f"./construction/{path}"
        return path
    def execute(self) -> str:
        try:
            directory = Path(self._adjust_path(self.directory_path))
            
            # Check if directory already exists
            if directory.exists():
                if self.exist_ok:
                    return f"Directory '{self.directory_path}' already exists (exist_ok=True)"
                else:
                    return f"Error: Directory '{self.directory_path}' already exists and exist_ok=False"
            
            # Create the directory
            directory.mkdir(parents=self.parents, exist_ok=self.exist_ok)
            
            # Verify it was created
            if directory.exists():
                return f"Successfully created directory '{self.directory_path}'"
            else:
                return f"Error: Failed to create directory '{self.directory_path}'"
                
        except Exception as e:
            return f"Error creating directory: {e}"