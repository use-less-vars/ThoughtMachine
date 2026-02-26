from typing import Literal
from pathlib import Path
from .base import ToolBase
import os
from pydantic import Field

class DirectoryCreator(ToolBase):
    """Create directories"""
    directory_path: str = Field(description="Directory path to create")
    parents: bool = Field(default=True, description="Create parent directories if they don't exist")
    exist_ok: bool = Field(default=True, description="Don't raise error if directory already exists")

    def execute(self) -> str:
        try:
            directory = Path(self.directory_path)
            
            # Check if directory already exists
            if directory.exists():
                if self.exist_ok:
                    return f"Directory '{self.directory_path}' already exists (exist_ok=True)"
                else:
                    return f"Error: Directory '{self.directory_path}' already exists and exist_ok=False"
            
            # Create the directory
            directory.mkdir(parents=self.parents, exist_ok=self.exist_ok)
            
            # Verify it was created
            if directory.exists():
                return f"Successfully created directory '{self.directory_path}'"
            else:
                return f"Error: Failed to create directory '{self.directory_path}'"
                
        except Exception as e:
            return f"Error creating directory: {e}"