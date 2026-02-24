from typing import Literal
from pathlib import Path
from .base import ToolBase
from pydantic import Field

class FileReader(ToolBase):
    tool: Literal['file_reader'] = Field(default = 'file_reader', description="Reads a single file")
    filename: str

    def execute(self) -> str:
        try:
            with open(self.filename, 'r', encoding='utf-8') as f:
                content = f.read()
            return f"content of {self.filename}: {content}"
        except Exception as e:
            return f"Error reading file: {e}"