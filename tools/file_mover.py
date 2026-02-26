from typing import Literal
from pathlib import Path
from .base import ToolBase
import shutil
import os
from pydantic import Field

class FileMover(ToolBase):
    """Move files from one directory to another"""
    source_path: str = Field(description="Source file or directory path")
    destination_path: str = Field(description="Destination directory or file path")
    create_dirs: bool = Field(default=False, description="Create destination directories if they don't exist")

    def execute(self) -> str:
        try:
            source = Path(self.source_path)
            destination = Path(self.destination_path)
            
            # Check if source exists
            if not source.exists():
                return f"Error: Source path '{self.source_path}' does not exist"
            
            # If create_dirs is True, create destination directory if needed
            if self.create_dirs:
                if destination.is_dir() or destination.suffix == "":
                    # Destination appears to be a directory
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    # Destination is a file path, create parent directory
                    destination.parent.mkdir(parents=True, exist_ok=True)
            
            # Move the file or directory
            shutil.move(str(source), str(destination))
            
            # Determine what was moved
            if source.is_file():
                moved_type = "file"
            else:
                moved_type = "directory"
            
            return f"Successfully moved {moved_type} from '{self.source_path}' to '{self.destination_path}'"
            
        except Exception as e:
            return f"Error moving file: {e}"