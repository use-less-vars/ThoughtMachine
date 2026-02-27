from typing import Literal
from pathlib import Path
from .base import ToolBase
import glob
import os
from pydantic import Field

class FileLister(ToolBase):
    """Lists files in a directory"""
    directory: str = Field(description="Directory to list files from")
    workspace: Literal["stable", "construction"] = Field(
        default="stable",
        description="Workspace to operate in: 'stable' (current directory) or 'construction' (./construction/ directory)"
    )

    def _get_actual_directory(self) -> str:
        """Convert directory path based on workspace setting."""
        if self.workspace == "construction":
            # Ensure construction directory exists
            Path("./construction").mkdir(parents=True, exist_ok=True)
            # If directory is absolute, keep it as is (no workspace mapping)
            if os.path.isabs(self.directory):
                return self.directory
            # Prefix with construction directory
            return f"./construction/{self.directory}"
        return self.directory
    def execute(self) -> str:
        try:
            actual_directory = self._get_actual_directory()
            files = glob.glob(f"{actual_directory}/*")
            return f"Files in {self.directory} (workspace: {self.workspace}): {', '.join(files)}"
        except Exception as e:
            return f"Error listing files: {e}"
