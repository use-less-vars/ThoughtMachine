from typing import Literal
from pathlib import Path
from .base import ToolBase
import glob
import os
from pydantic import Field

class FileLister(ToolBase):
    """Lists files in a directory"""
    directory: str = Field(description="Directory to list files from")

    def execute(self) -> str:
        try:
            actual_directory = self.directory
            files = glob.glob(f"{actual_directory}/*")
            return f"Files in {self.directory}: {', '.join(files)}"
        except Exception as e:
            return f"Error listing files: {e}"
