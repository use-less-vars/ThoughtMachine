# tools/create_construction_workspace.py
from typing import Literal
from pathlib import Path
import shutil
import os
from datetime import datetime
from pydantic import Field
from .base import ToolBase


class CreateConstructionWorkspaceTool(ToolBase):
    """Creates a construction workspace by copying current code to ./construction/ directory.
    Automatically creates timestamped backup of existing construction workspace before overwriting.
    Default overwrite=False for safety - will not replace existing construction workspace without explicit permission."""
    overwrite: bool = Field(
        default=False,
        description="Overwrite existing construction workspace if it exists. Default is False for safety."
    )

    def execute(self) -> str:
        stable_dir = Path(".")
        construction_dir = Path("./construction")

        # Ensure stable directory exists (it does)
        if not stable_dir.exists():
            return "Error: Stable directory (current directory) does not exist."

        # Initialize backup_message for the scope of the function
        backup_message = ""
        
        # Check if construction directory already exists
        if construction_dir.exists():
            if self.overwrite:
                # Create backup of existing construction workspace before overwriting
                try:
                    # Check if construction directory has any content
                    has_content = any(construction_dir.iterdir())
                    if has_content:
                        # Create timestamp for backup directory name
                        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                        backup_dir = Path(f"./construction_backup_{timestamp}")
                        
                        # Ensure backup directory doesn't already exist (unlikely with timestamp)
                        counter = 1
                        while backup_dir.exists():
                            backup_dir = Path(f"./construction_backup_{timestamp}_{counter}")
                            counter += 1
                        
                        # Copy existing construction directory to backup location
                        shutil.copytree(construction_dir, backup_dir)
                        backup_message = f"\nCreated backup of existing construction workspace at {backup_dir.absolute()}"
                    else:
                        backup_message = "\nConstruction directory exists but is empty, no backup needed."
                except Exception as e:
                    backup_message = f"\nWarning: Could not create backup of existing construction workspace: {e}"
                
                # Remove existing construction directory
                try:
                    shutil.rmtree(construction_dir)
                except Exception as e:
                    return f"Error removing existing construction directory: {e}"
            else:
                # Provide detailed information about existing construction workspace
                try:
                    file_count = 0
                    dir_count = 0
                    for root, dirs, files in os.walk(construction_dir):
                        dir_count += len(dirs)
                        file_count += len(files)
                    
                    # Check if any files are newer than their counterparts in stable, and files unique to construction
                    newer_files = []
                    unique_files = []
                    for root, dirs, files in os.walk(construction_dir):
                        rel_root = Path(root).relative_to(construction_dir)
                        stable_root = stable_dir / rel_root
                        for file in files:
                            const_file = Path(root) / file
                            stable_file = stable_root / file
                            if stable_file.exists():
                                const_mtime = const_file.stat().st_mtime
                                stable_mtime = stable_file.stat().st_mtime
                                if const_mtime > stable_mtime:
                                    newer_files.append(str(rel_root / file) if rel_root != Path('.') else file)
                            else:
                                # File exists only in construction
                                unique_files.append(str(rel_root / file) if rel_root != Path('.') else file)
                    msg = f"Construction directory already exists with {file_count} files and {dir_count} directories."
                    if newer_files:
                        msg += f"\nWarning: {len(newer_files)} files appear newer than stable versions:"
                        for f in newer_files[:5]:  # Show first 5
                            msg += f"\n  - {f}"
                        if len(newer_files) > 5:
                            msg += f"\n  ... and {len(newer_files) - 5} more"
                    if unique_files:
                        msg += f"\nNote: {len(unique_files)} files exist only in construction (not in stable):"
                        for f in unique_files[:5]:  # Show first 5
                            msg += f"\n  - {f}"
                        if len(unique_files) > 5:
                            msg += f"\n  ... and {len(unique_files) - 5} more"
                    msg += "\nSet overwrite=True to replace (a timestamped backup will be created)."
                    return msg
                except Exception as e:
                    return f"Construction directory already exists. Set overwrite=True to replace. (Error gathering details: {e})"

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
            result = f"Successfully created construction workspace at {construction_dir.absolute()}"
            if backup_message:
                result += backup_message
            return result
        except Exception as e:
            return f"Error copying stable workspace to construction: {e}"