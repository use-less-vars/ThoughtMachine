from typing import Literal
from pathlib import Path
from .base import ToolBase
from pydantic import Field

class FileReader(ToolBase):
    """Reads a single file. Note: For reading specific lines from existing files, consider using FileLineReader."""
    filename: str = Field(description="Path to the file to read")

    def execute(self) -> str:
        try:
            with open(self.filename, 'r', encoding='utf-8') as f:
                content = f.read()
            return f"content of {self.filename}: {content}"
        except Exception as e:
            return f"Error reading file: {e}"