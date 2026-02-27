# tools/create_construction_workspace.py
from typing import Literal
from pathlib import Path
import shutil
import os
from pydantic import Field
from .base import ToolBase


class CreateConstructionWorkspaceTool(ToolBase):
    """Creates a construction workspace by copying current code to ./construction/ directory."""
    
    overwrite: bool = Field(
        default=True,
        description="Overwrite existing construction workspace if it exists."
    )
    
    def execute(self) -> str:
        stable_dir = Path(".")
        construction_dir = Path("./construction")
        
        # Ensure stable directory exists (it does)
        if not stable_dir.exists():
            return "Error: Stable directory (current directory) does not exist."
        
        # Check if construction directory already exists
        if construction_dir.exists():
            if self.overwrite:
                # Remove existing construction directory
                try:
                    shutil.rmtree(construction_dir)
                except Exception as e:
                    return f"Error removing existing construction directory: {e}"
            else:
                return "Construction directory already exists. Set overwrite=True to replace."
        
        # Copy stable to construction
        try:
            # Exclude the construction directory itself from copy
            # We'll use shutil.copytree with ignore function to skip construction dir
            def ignore_func(src, names):
                ignored = []
                # Skip any directory named 'construction' within the root
                for name in names:
                    if name == 'construction':
                        ignored.append(name)
                return ignored
            
            shutil.copytree(stable_dir, construction_dir, ignore=ignore_func, dirs_exist_ok=False)
            
            # Ensure the construction directory has proper permissions (copied already)
            return f"Successfully created construction workspace at {construction_dir.absolute()}"
        except Exception as e:
            return f"Error copying stable workspace to construction: {e}"