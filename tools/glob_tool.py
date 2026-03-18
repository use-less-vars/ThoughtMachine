"""
GlobTool - Efficient file pattern matching with pagination and exclusion support.

Provides fast file discovery using Python's glob module with:
- Recursive pattern matching ( supports ** )
- Directory exclusion (like __pycache__, .git, node_modules)
- Pagination for large result sets
- Both tree and list output formats
"""
from .base import ToolBase
import os
import sys
import glob
import fnmatch
from pathlib import Path
from typing import List, Optional, Literal
from pydantic import Field

class GlobTool(ToolBase):
    """Find files and directories using glob patterns.

    Features:
    - Pattern matching: '*.py', '**/*.txt', 'src/**/test_*.py'
    - Recursive search with max_depth control
    - Exclude directories to skip (build artifacts, VCS, etc.)
    - Paginate results with page and per_page parameters
    - Optional file/directory filtering by type
    - Sort results for consistent output

    Examples:
        pattern='**/*.py' will find all Python files recursively
        pattern='*.txt' finds only .txt files in the specified directory
        exclude_dirs=['__pycache__', '.git', 'node_modules'] skips common artifacts
        page=2, per_page=50 gets the second page of 50 results each
    """
    tool: Literal["GlobTool"] = "GlobTool"


    directory: str = Field(
        description="Root directory to start the glob search (default: current directory)"
    )
    pattern: str = Field(
        default="*",
        description="Glob pattern (e.g., '*.py', '**/*.txt', 'src/**/test_*)"
    )
    recursive: bool = Field(
        default=False,
        description="If True, '**' pattern matches any subdirectories (including all levels)"
    )
    exclude_dirs: List[str] = Field(
        default_factory=lambda: ["__pycache__", ".git", ".svn", ".hg", "node_modules", ".idea", ".vscode", ".pytest_cache", "build", "dist", "*.egg-info", "venv", "env", ".venv"],
        description="Directory names to exclude (exact match or glob patterns)"
    )
    page: int = Field(
        default=1,
        description="Page number (1-indexed). Set to 1 for first page."
    )
    per_page: int = Field(
        default=100,
        description="Number of results per page (0 for unlimited)"
    )
    include_dirs: bool = Field(
        default=False,
        description="If True, include directories in results (default: files only)"
    )
    sort_by: str = Field(
        default="name",
        description="Sort order: 'name', 'size', 'modified'"
    )
    reverse: bool = Field(
        default=False,
        description="If True, reverse the sort order"
    )

    def _should_exclude(self, path: Path, exclude_patterns: List[str]) -> bool:
        """Check if a path should be excluded based on exclude patterns."""
        # Check if any parent directory in the path matches exclude patterns
        for part in path.parts:
            for pattern in exclude_patterns:
                if fnmatch.fnmatch(part, pattern):
                    return True
        return False

    def _get_files(self) -> List[Path]:
        """Perform the glob search with exclusions."""
        base_path = Path(self.directory).expanduser().resolve()
        if not base_path.exists():
            raise ValueError(f"Directory does not exist: {self.directory}")

        # Prepare exclude patterns
        exclude_patterns = self.exclude_dirs

        # Perform glob
        if self.recursive:
            # Avoid double '**' if pattern already contains it
            if "**" in self.pattern:
                pattern = str(base_path / self.pattern)
            else:
                pattern = str(base_path / "**" / self.pattern)
            all_paths = glob.glob(pattern, recursive=True)
        else:
            pattern = str(base_path / self.pattern)
            all_paths = glob.glob(pattern, recursive=False)

        # Convert to Path objects and filter
        paths = [Path(p) for p in all_paths if Path(p).is_file() or self.include_dirs]

        # Apply exclusions
        if exclude_patterns:
            paths = [p for p in paths if not self._should_exclude(p, exclude_patterns)]

        # Sort
        if self.sort_by == "name":
            paths.sort(key=lambda p: str(p), reverse=self.reverse)
        elif self.sort_by == "size":
            def safe_size(p: Path) -> int:
                try:
                    return p.stat().st_size if p.exists() else 0
                except (OSError, PermissionError):
                    return 0
            paths.sort(key=safe_size, reverse=self.reverse)
        elif self.sort_by == "modified":
            def safe_mtime(p: Path) -> float:
                try:
                    return p.stat().st_mtime if p.exists() else 0.0
                except (OSError, PermissionError):
                    return 0.0
            paths.sort(key=safe_mtime, reverse=self.reverse)
        else:
            raise ValueError(f"Invalid sort_by: {self.sort_by}. Use 'name', 'size', or 'modified'")

        return paths

    def execute(self) -> str:
        """Execute the glob search and return formatted results."""
        try:
            paths = self._get_files()

            # Pagination
            total = len(paths)
            if self.per_page > 0:
                start_idx = (self.page - 1) * self.per_page
                end_idx = start_idx + self.per_page
                page_paths = paths[start_idx:end_idx]
                total_pages = (total + self.per_page - 1) // self.per_page
            else:
                page_paths = paths
                total_pages = 1

            # Build output
            lines = []
            for p in page_paths:
                suffix = " (dir)" if p.is_dir() else ""
                # Compute relative path with compatibility for Python < 3.9
                try:
                    rel_path = p.relative_to(Path(self.directory).resolve())
                except ValueError:
                    rel_path = p
                lines.append(f"{rel_path}{suffix}")

            output = "\n".join(lines)

            # Add pagination info
            if self.per_page > 0:
                output += f"\n\n--- Page {self.page} of {total_pages} ({total} total results) ---"
                if self.page < total_pages:
                    output += f"\nUse page={self.page + 1} to see more results."

            return self._truncate_output(output)

        except Exception as e:
            return f"Error in GlobTool: {e}"
