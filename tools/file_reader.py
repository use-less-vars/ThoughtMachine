from typing import Literal
from pathlib import Path
from .base import ToolBase
from pydantic import Field

class FileReader(ToolBase):
    """Reads a single file"""""
    filename: str = Field(description="Path to the file to read")

    def execute(self) -> str:
        try:
            with open(self.filename, 'r', encoding='utf-8') as f:
                content = f.read()
            return f"content of {self.filename}: {content}"
        except Exception as e:
            return f"Error reading file: {e}"