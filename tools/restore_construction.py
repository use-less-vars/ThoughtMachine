# tools/restore_construction.py
from typing import Literal, Optional
from pathlib import Path
import shutil
import subprocess
import os
from pydantic import Field
from .base import ToolBase


class RestoreConstructionTool(ToolBase):
    """Restores construction workspace from stable or git backup."""
    
    source: Literal["stable", "git"] = Field(
        default="stable",
        description="Source to restore from: 'stable' (copy from current directory) or 'git' (reset to last commit)."
    )
    
    git_ref: Optional[str] = Field(
        default=None,
        description="Git reference to restore from (e.g., 'HEAD', 'main'). Only used when source='git'."
    )
    
    def execute(self) -> str:
        construction_dir = Path("./construction")
        
        if self.source == "stable":
            # Delete construction directory if exists
            if construction_dir.exists():
                try:
                    shutil.rmtree(construction_dir)
                except Exception as e:
                    return f"Error removing existing construction directory: {e}"
            
            # Recreate using same logic as CreateConstructionWorkspaceTool
            stable_dir = Path(".")
            try:
                def ignore_func(src, names):
                    ignored = []
                    for name in names:
                        if name == 'construction':
                            ignored.append(name)
                    return ignored
                shutil.copytree(stable_dir, construction_dir, ignore=ignore_func, dirs_exist_ok=False)
                return f"Successfully restored construction workspace from stable directory."
            except Exception as e:
                return f"Error restoring construction workspace: {e}"
        
        elif self.source == "git":
            # Check if construction directory is a git repository
            # If not, initialize git? For now, assume it's a git repo.
            if not construction_dir.exists():
                return "Construction directory does not exist. Cannot restore from git."
            
            # Determine git reference
            ref = self.git_ref if self.git_ref is not None else "HEAD"
            
            try:
                # Run git reset --hard to specified ref
                subprocess.run(
                    ["git", "reset", "--hard", ref],
                    cwd=construction_dir,
                    check=True,
                    capture_output=True,
                    text=True
                )
                return f"Successfully restored construction workspace to git ref '{ref}'."
            except subprocess.CalledProcessError as e:
                return f"Git restore failed: {e.stderr}"
            except Exception as e:
                return f"Error during git restore: {e}"
        
        else:
            return f"Unknown source: {self.source}"