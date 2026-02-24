from typing import Literal
from pathlib import Path
from .base import ToolBase
from pydantic import Field

class FileWriter(ToolBase):
    tool: Literal['file_writer'] = Field(default = 'file_writer', description="Writes a single file")
    filename: str
    content: str

    def execute(self) -> str:
        try:
            Path(self.filename).parent.mkdir(parents=True, exist_ok=True)
            with open(self.filename, 'w', encoding='utf-8') as f:
                f.write(self.content)
            return f"Successfully wrote to {self.filename}"
        except Exception as e:
            return f"Error writing file: {e}"