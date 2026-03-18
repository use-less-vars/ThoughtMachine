from .base import ToolBase
import os
import re
import fnmatch
import bisect
from pathlib import Path
from pydantic import Field
from typing import List, Optional, ClassVar, Literal

class FileSearchTool(ToolBase):
    """Search for patterns across multiple files or directories with regex, multiline, context lines, and line numbers.
    Supports regex (with (?s) flag for dot-matches-newline) and plain text multi-line searches.
    Use file_pattern glob to limit files, or directory/filenames."""
    tool: Literal["FileSearchTool"] = "FileSearchTool"
    
    # Safety limits
    MAX_FILE_SIZE: ClassVar[int] = 10_000_000  # 10MB
    MAX_LINES_PER_FILE: ClassVar[int] = 100_000
    MAX_CONTEXT_LINES: ClassVar[int] = 20
    MAX_OUTPUT_CHARS: ClassVar[int] = 40_000
    MAX_RESULTS: ClassVar[int] = 200
    MAX_FILES_TO_SEARCH: ClassVar[int] = 1000

    pattern: str = Field(description="Search pattern. If use_regex=True, this is a regex pattern; otherwise plain text.")
    file_pattern: Optional[str] = Field(None, description="Glob pattern to limit which files are searched (e.g., '**/*.py'). If None, use filenames or directory.")
    filenames: Optional[List[str]] = Field(default=None, description="List of file paths to search in. If not provided, use directory or file_pattern.")
    directory: Optional[str] = Field(default=None, description="Directory to search recursively (if filenames not provided).")
    context_lines: int = Field(default=5, description="Number of lines of context to show before and after each match.")
    show_line_numbers: bool = Field(default=True, description="Include line numbers in the output.")
    use_regex: bool = Field(default=False, description="If True, treat pattern as a regular expression.")
    case_sensitive: bool = Field(default=False, description="If True, perform case-sensitive search (default False).")
    max_results: int = Field(default=50, description="Maximum number of matches to return.")
    exclude_dirs: List[str] = Field(
        default_factory=lambda: ["__pycache__", ".git", ".svn", ".hg", "node_modules", ".idea", ".vscode", ".pytest_cache", "build", "dist", "*.egg-info", "venv", "env", ".venv"],
        description="Directory names to exclude from search (exact match or glob patterns)"
    )
    
    def _should_exclude_dir(self, dirname: str) -> bool:
        """Check if a directory should be excluded based on exclude_dirs patterns."""
        for pattern in self.exclude_dirs:
            if fnmatch.fnmatch(dirname, pattern):
                return True
        return False

    def _is_path_excluded(self, file_path: str) -> bool:
        """Check if a file path should be excluded based on exclude_dirs patterns."""
        # Check each directory component
        path = Path(file_path)
        for parent in path.parents:
            if self._should_exclude_dir(parent.name):
                return True
        return False

    def execute(self) -> str:
        try:
            # Determine which files to search
            files_to_search = []
            if self.filenames:
                for f in self.filenames:
                    try:
                        validated_f = self._validate_path(f)
                    except ValueError as e:
                        return f"Error: {e}"
                    if os.path.isdir(validated_f):
                        # treat as directory, expand recursively with exclusion
                        for root, dirs, files in os.walk(validated_f):
                            # Modify dirs in-place to exclude unwanted directories
                            dirs[:] = [d for d in dirs if not self._should_exclude_dir(d)]
                            for file in files:
                                full_path = os.path.join(root, file)
                                # Validate each subfile (should be within workspace since root is)
                                try:
                                    validated_sub = self._validate_path(full_path)
                                    files_to_search.append(validated_sub)
                                except ValueError:
                                    # Skip files outside workspace
                                    continue
                    else:
                        files_to_search.append(validated_f)
            elif self.directory:
                # Validate directory path is within workspace
                try:
                    validated_dir = self._validate_path(self.directory)
                except ValueError as e:
                    return f"Error: {e}"
                if not os.path.isdir(validated_dir):
                    return f"Error: '{self.directory}' is not a valid directory."
                for root, dirs, files in os.walk(validated_dir):
                    # Modify dirs in-place to exclude unwanted directories
                    dirs[:] = [d for d in dirs if not self._should_exclude_dir(d)]
                    for file in files:
                        full_path = os.path.join(root, file)
                        # Validate each subfile (should be within workspace since root is)
                        try:
                            validated_sub = self._validate_path(full_path)
                            files_to_search.append(validated_sub)
                        except ValueError:
                            # Skip files outside workspace
                            continue
            elif self.file_pattern:
                # Use glob to find files matching pattern, respecting workspace and exclusions
                files_to_search = []
                base_path = Path(self.workspace_path or '.')
                for p in base_path.glob(self.file_pattern):
                    try:
                        validated_path = self._validate_path(str(p))
                        # Check if file is in excluded directory
                        if self._is_path_excluded(validated_path):
                            continue
                        files_to_search.append(validated_path)
                    except ValueError:
                        # Skip files outside workspace
                        continue
            else:
                return "Error: Provide one of 'filenames', 'directory', or 'file_pattern'."
            
            # Limit number of files to prevent excessive scanning (optional)
            if len(files_to_search) > self.MAX_FILES_TO_SEARCH:
                return f"Error: Too many files to search ({len(files_to_search)}). Please narrow your search."
            
            # Prepare pattern
            if self.use_regex:
                flags = 0 if self.case_sensitive else re.IGNORECASE
                regex_pattern = re.compile(self.pattern, flags)
            else:
                # escape special regex characters, treat as literal
                flags = 0 if self.case_sensitive else re.IGNORECASE
                regex_pattern = re.compile(re.escape(self.pattern), flags)
            
            # Apply safety limits to parameters
            context_lines = min(self.context_lines, self.MAX_CONTEXT_LINES)
            max_results = min(self.max_results, self.MAX_RESULTS)
            
            matches = []
            file_lines_cache = {}
            file_line_offsets_cache = {}
            
            for file_path in files_to_search:
                if not os.path.isfile(file_path):
                    continue
                try:
                    file_size = os.path.getsize(file_path)
                    if file_size > self.MAX_FILE_SIZE:
                        continue
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                    lines = content.splitlines(keepends=True)
                    if len(lines) > self.MAX_LINES_PER_FILE:
                        lines = lines[:self.MAX_LINES_PER_FILE]
                        content = ''.join(lines)
                    file_lines_cache[file_path] = lines
                    line_offsets = [0]
                    for line in lines:
                        line_offsets.append(line_offsets[-1] + len(line))
                    file_line_offsets_cache[file_path] = line_offsets
                except (IOError, PermissionError, UnicodeDecodeError):
                    continue

                # Find all matches
                for match in regex_pattern.finditer(content):
                    start = match.start()
                    end = match.end()
                    # Compute line numbers efficiently using binary search on offsets
                    line_start = bisect.bisect_right(line_offsets, start)
                    # For line_end, use end-1 to get line containing last char
                    line_end = bisect.bisect_right(line_offsets, end - 1) if end > start else line_start
                    matches.append({
                        'file': file_path,
                        'start': start,
                        'end': end,
                        'line_start': line_start,
                        'line_end': line_end,
                        'match_text': match.group()
                    })
                    if len(matches) >= max_results:
                        break
                if len(matches) >= max_results:
                    break            
            if not matches:
                return "No matches found."
            
            # Now build output with context lines and highlighting
            output_lines = []
            for match in matches:
                file_path = match['file']
                # Retrieve cached lines and offsets
                file_lines = file_lines_cache.get(file_path)
                line_offsets = file_line_offsets_cache.get(file_path)
                if file_lines is None or line_offsets is None:
                    continue
                
                line_start = match['line_start']
                line_end = match['line_end']
                start = match['start']
                end = match['end']
                
                context_before = max(1, line_start - context_lines)
                context_after = min(len(file_lines), line_end + context_lines)
                
                # Determine relative path
                try:
                    rel_path = os.path.relpath(file_path)
                except ValueError:
                    rel_path = file_path
                
                output_lines.append(f"{rel_path}:")
                # Context lines before match
                for i in range(context_before, line_start):
                    line_num = i
                    line_content = file_lines[i-1].rstrip('\n')
                    if self.show_line_numbers:
                        output_lines.append(f"  {line_num:4d}: {line_content}")
                    else:
                        output_lines.append(f"  {line_content}")
                # Matched lines with highlighting
                for i in range(line_start, line_end + 1):
                    line_num = i
                    line_idx = i - 1
                    raw_line = file_lines[line_idx]
                    # Compute highlight segment within this line
                    line_start_pos = line_offsets[line_idx]
                    line_end_pos = line_offsets[line_idx + 1]
                    seg_start = max(start, line_start_pos)
                    seg_end = min(end, line_end_pos)
                    if seg_start < seg_end:
                        # Convert to column positions within line (including newline)
                        col_start = seg_start - line_start_pos
                        col_end = seg_end - line_start_pos
                        # Apply highlighting to raw_line
                        highlighted = raw_line[:col_start] + '**' + raw_line[col_start:col_end] + '**' + raw_line[col_end:]
                        line_content = highlighted.rstrip('\n')
                    else:
                        line_content = raw_line.rstrip('\n')
                    if self.show_line_numbers:
                        output_lines.append(f"> {line_num:4d}: {line_content}")
                    else:
                        output_lines.append(f"> {line_content}")
                # Context lines after match
                for i in range(line_end + 1, context_after + 1):
                    line_num = i
                    line_content = file_lines[i-1].rstrip('\n')
                    if self.show_line_numbers:
                        output_lines.append(f"  {line_num:4d}: {line_content}")
                    else:
                        output_lines.append(f"  {line_content}")
                output_lines.append("")
            
            header = f"Found {len(matches)} matches for pattern '{self.pattern}'"
            # Debug: show values
            header += f" [DEBUG: self.max_results={self.max_results}, max_results={max_results}, self.MAX_RESULTS={self.MAX_RESULTS}]"
            if self.max_results != max_results:
                header += f" (clamped from {self.max_results} to {max_results})"
            if self.use_regex:
                header += " (regex)"
            else:
                header += " (plain text)"
            if self.directory:
                header += f" in directory '{self.directory}'"
            header += f" (case_sensitive={self.case_sensitive}, max_results={self.max_results}):"
            output = header + "\n" + "\n".join(output_lines)
            return self._truncate_output(output)
            
        except Exception as e:
            return self._truncate_output(f"Error searching files: {e}")
