from typing import List, Optional, Literal
from pathlib import Path
from .base import ToolBase
import shutil
import glob
import os
from pydantic import Field, model_validator

class FileMover(ToolBase):
    """Move files and directories. Supports single file, batch moves via list, and glob patterns.
    
    Examples:
    - Single file: source_path='file.txt', destination_path='dest/file.txt'
    - Batch list: source_paths=['a.txt', 'b.txt'], destination_path='dest/'
    - Pattern: pattern='*.tmp', destination_path='temp/', recursive=True
    - Preserve structure: source_paths=['src/a.txt', 'src/b/c.txt'], destination_path='backup/', preserve_structure=True
    """
    source_path: Optional[str] = Field(default=None, description="Source file or directory path (single move).")
    source_paths: Optional[List[str]] = Field(default=None, description="List of source file/directory paths (batch move).")
    pattern: Optional[str] = Field(default=None, description="Glob pattern to match multiple files (e.g., '*.tmp', 'data_*.txt').")
    destination_path: str = Field(description="Destination directory or file path. For batch moves, must be a directory.")
    create_dirs: bool = Field(default=False, description="Create destination directories if they don't exist.")
    recursive: bool = Field(default=False, description="For pattern matching, include subdirectories recursively.")
    preserve_structure: bool = Field(default=False, description="For batch moves, preserve relative directory structure when moving to destination.")
    workspace: Literal["stable", "construction"] = Field(
        default="stable",
        description="Workspace to operate in: 'stable' (current directory) or 'construction' (./construction/ directory)"
    )

    def _adjust_path(self, path: str) -> str:
        """Adjust path based on workspace setting."""
        if self.workspace == "construction":
            # Ensure construction directory exists
            Path("./construction").mkdir(parents=True, exist_ok=True)
            # If path is absolute, keep it as is (no workspace mapping)
            if os.path.isabs(path):
                return path
            # Prefix with construction directory
            return f"./construction/{path}"
        return path
    @model_validator(mode='after')
    def validate_sources(self):
        # Determine which source specification to use
        # Precedence: pattern > source_paths > source_path
        if self.pattern is not None:
            self.source_paths = None
            self.source_path = None
        elif self.source_paths is not None:
            self.source_path = None
        # else source_path is used
        # Ensure at least one source spec is provided
        if self.pattern is None and self.source_paths is None and self.source_path is None:
            raise ValueError("Either source_path, source_paths, or pattern must be provided.")
        return self

    def _expand_sources(self) -> List[Path]:
        """Return a list of Path objects based on the provided source specification."""
        sources = []
        if self.pattern is not None:
            # Use glob to find matching files
            matches = glob.glob(self.pattern, recursive=self.recursive)
            sources = [Path(m) for m in matches]
            # Sort for deterministic behavior
            sources.sort()
        elif self.source_paths is not None:
            sources = [Path(sp) for sp in self.source_paths]
        elif self.source_path is not None:
            sources = [Path(self.source_path)]
        return sources

    def _expand_sources_adjusted(self) -> List[Path]:
        """Return a list of Path objects based on source specification, adjusted for workspace."""
        sources = []
        if self.pattern is not None:
            # Adjust pattern for workspace
            adjusted_pattern = self._adjust_path(self.pattern)
            # Use glob to find matching files
            matches = glob.glob(adjusted_pattern, recursive=self.recursive)
            sources = [Path(m) for m in matches]
            # Sort for deterministic behavior
            sources.sort()
        elif self.source_paths is not None:
            # Adjust each source path
            adjusted_paths = [self._adjust_path(sp) for sp in self.source_paths]
            sources = [Path(sp) for sp in adjusted_paths]
        elif self.source_path is not None:
            adjusted_path = self._adjust_path(self.source_path)
            sources = [Path(adjusted_path)]
        return sources
    def _common_parent(self, paths: List[Path]) -> Path:
        """Return the longest common parent directory for a list of paths."""
        if not paths:
            return Path.cwd()
        # Convert all paths to absolute and resolve
        abs_paths = [p.absolute().resolve() for p in paths]
        # Split each path into parts
        parts_list = [list(p.parts) for p in abs_paths]
        # Find the shortest path (by parts)
        min_len = min(len(parts) for parts in parts_list)
        common_parts = []
        for i in range(min_len):
            part = parts_list[0][i]
            if all(parts[i] == part for parts in parts_list):
                common_parts.append(part)
            else:
                break
        if not common_parts:
            # No common prefix (different drives?) return root
            return Path(abs_paths[0].anchor) if abs_paths else Path.cwd()
        common = Path(*common_parts)
        # Ensure it's a directory (if not, take parent)
        if common.is_file():
            common = common.parent
        return common

    def execute(self) -> str:
        try:
            destination = Path(self._adjust_path(self.destination_path))
            
            # Expand sources
            sources = self._expand_sources_adjusted()
            if not sources:
                return "No files or directories matched the source specification."
            
            # Validate all sources exist before moving anything
            for source in sources:
                if not source.exists():
                    return f"Error: Source path '{source}' does not exist"
            
            # Determine if this is a batch move (multiple sources)
            is_batch = len(sources) > 1
            
            # If batch move, destination must be a directory
            if is_batch:
                # If create_dirs is True, create destination directory
                if self.create_dirs:
                    destination.mkdir(parents=True, exist_ok=True)
                # Ensure destination is a directory (or we will treat it as such)
                if destination.exists() and not destination.is_dir():
                    return f"Error: Destination '{destination}' exists and is not a directory (cannot move multiple items into a file)."
            
            # If preserving structure and batch, compute common parent
            common_parent = None
            if is_batch and self.preserve_structure:
                common_parent = self._common_parent(sources)
            
            moved_items = []
            for source in sources:
                # Determine final destination path for this source
                if is_batch:
                    if self.preserve_structure and common_parent:
                        try:
                            rel_path = source.absolute().relative_to(common_parent.absolute())
                            final_dest = destination / rel_path
                        except ValueError:
                            # fallback to flatten
                            final_dest = destination / source.name
                    else:
                        # Just move item into destination directory with same name
                        final_dest = destination / source.name
                else:
                    # Single source: destination could be a file or directory
                    final_dest = destination
                
                # If create_dirs is True, create parent directories of final destination
                if self.create_dirs:
                    if final_dest.suffix:  # looks like a file
                        final_dest.parent.mkdir(parents=True, exist_ok=True)
                    else:
                        # destination is a directory (ensure it exists)
                        final_dest.mkdir(parents=True, exist_ok=True)
                
                # Move the file or directory
                shutil.move(str(source), str(final_dest))
                
                # Record what was moved
                moved_type = "directory" if source.is_dir() else "file"
                moved_items.append((str(source), str(final_dest), moved_type))
            
            # Generate success message
            if len(moved_items) == 1:
                src, dest, typ = moved_items[0]
                return f"Successfully moved {typ} from '{src}' to '{dest}'"
            else:
                details = "\n".join([f"  - {typ}: '{src}' -> '{dest}'" for src, dest, typ in moved_items])
                return f"Successfully moved {len(moved_items)} items:\n{details}"
            
        except Exception as e:
            return f"Error moving file(s): {e}"