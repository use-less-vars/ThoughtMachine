from typing import Literal
from pathlib import Path
from .base import ToolBase
import glob
from pydantic import Field

class FileLister(ToolBase):
    """"Lists files in a directory"""
    directory: str = Field(description="Directory to list files from")

    def execute(self) -> str:
        try:
            files = glob.glob(f"{self.directory}/*")
            return f"Files in {self.directory}: {', '.join(files)}"
        except Exception as e:
            return f"Error listing files: {e}"