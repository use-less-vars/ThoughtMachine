# tools/file_writer.py
from tools.base import ToolBase
from pydantic import Field

class FileWriter(ToolBase):
    """Write content to a file."""
    filename: str = Field(description="Name of the file to write (can include path).")
    content: str = Field(description="Content to write to the file.")

    def execute(self) -> str:
        try:
            with open(self.filename, 'w', encoding='utf-8') as f:
                f.write(self.content)
            return f"Successfully wrote to {self.filename}"
        except Exception as e:
            return f"Error writing file: {e}"