from typing import Literal
from pathlib import Path
from .base import ToolBase
import glob
from pydantic import Field

class FileLister(ToolBase):
    tool: Literal['file_lister'] = Field(default = 'file_lister', description="Lists files in a directory")
    directory: str

    def execute(self) -> str:
        try:
            files = glob.glob(f"{self.directory}/*")
            return f"Files in {self.directory}: {', '.join(files)}"
        except Exception as e:
            return f"Error listing files: {e}"