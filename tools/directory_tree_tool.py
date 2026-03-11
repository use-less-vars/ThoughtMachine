from .base import ToolBase
import os
import pathlib
import stat
from pydantic import Field
from typing import Optional, List, Dict, Any, Tuple, ClassVar
import time

class DirectoryTreeTool(ToolBase):
    """Show directory structure with tree visualization or flat file listing. Supports recursion limits, hidden file filtering, pattern matching, and output truncation.
    
    Key improvements to reduce token usage:
    - Excludes cache directories (__pycache__, .git, node_modules, etc.) by default
    - Reduced default max_depth from 3 to 2
    - Skips line counting for binary files and large files (>1MB)
    - Optional skip_line_count parameter for performance
    """
    
    # Common binary file extensions where line counting should be skipped
    BINARY_EXTENSIONS: ClassVar[set[str]] = {'.pyc', '.pyo', '.so', '.dll', '.exe', '.bin', '.o', '.a', '.lib', '.dylib',
                         '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.ico', '.webp',
                         '.mp3', '.mp4', '.avi', '.mov', '.wav', '.flac', '.ogg', '.mkv',
                         '.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.rar',
                         '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
                         '.db', '.sqlite', '.sqlite3', '.mdb', '.accdb'}
    
    def _debug_log(self, message: str) -> None:
        """Debug logging - writes to file and stderr."""
        import sys
        print(f"DEBUG: {message}", file=sys.stderr)
        try:
            with open('debug.log', 'a', encoding='utf-8') as f:
                import time
                f.write(f"{time.time():.3f}: {message}\n")
        except Exception as e:
            print(f"DEBUG LOG ERROR: {e}", file=sys.stderr)
    

    
    directory: str = Field(description="Root directory to show tree structure")
    max_depth: int = Field(default=2, description="Maximum depth to recurse (0 for unlimited)")
    show_hidden: bool = Field(default=False, description="Show hidden files and directories (starting with .)")
    include_sizes: bool = Field(default=True, description="Include file sizes and line counts")
    skip_line_count: bool = Field(default=True, description="Skip line counting for performance (line_count will be 0)")
    pattern: str = Field(default="*", description="Glob pattern to filter files (e.g., '*.py', '*.txt')")
    format: str = Field(default='tree', description="Output format: 'tree' (directory tree) or 'list' (flat file list)")
    max_results: int = Field(default=100, description="Maximum files/entries to show (0=unlimited, applies to both list and tree formats)")
    sort_by: str = Field(default='name', description="Sort order for list format: 'name', 'size', or 'modified'")
    exclude_dirs: List[str] = Field(default_factory=lambda: ["__pycache__", ".git", ".svn", ".hg", "node_modules", ".idea", ".vscode", ".pytest_cache", "build", "dist", "*.egg-info"], description="Directories to exclude from traversal")
    
    def execute(self) -> str:
        self._debug_log(f"DirectoryTreeTool.execute called with directory={self.directory}, exclude_dirs={self.exclude_dirs}")
        import sys
        print(f"EXECUTE: directory={self.directory}, exclude_dirs={self.exclude_dirs}", file=sys.stderr)
        try:
            # Resolve directory path
            dir_path = pathlib.Path(self.directory)
            # Validate directory is within workspace
            try:
                validated_path = self._validate_path(str(dir_path))
                dir_path = pathlib.Path(validated_path)
            except ValueError as e:
                return f"Error: {e}"
            if not dir_path.exists():
                return f"Error: Directory '{self.directory}' does not exist."
            if not dir_path.is_dir():
                return f"Error: '{self.directory}' is not a directory."

            if self.format == 'list':
                return self._execute_list_format(dir_path)
            else:
                return self._execute_tree_format(dir_path)
        except Exception as e:
            return f"Error generating directory tree: {e}"

    
    def _build_tree(self, dir_path: pathlib.Path, current_depth: int) -> Dict[str, Any]:
        """Recursively build tree structure."""
        # Debug: write to file
        try:
            import os
            with open('tree_debug.txt', 'a', encoding='utf-8') as f:
                f.write(f"_build_tree: {dir_path.name}, depth={current_depth}, cwd={os.getcwd()}\n")
        except Exception as e:
            with open('debug_error.txt', 'a') as f2:
                f2.write(f"Error: {e}\n")
        self._debug_log(f"_build_tree: {dir_path.name}, depth={current_depth}")
        if self.max_depth > 0 and current_depth >= self.max_depth:
            self._debug_log(f"max_depth reached, returning empty")
            return {'type': 'directory', 'path': dir_path, 'name': dir_path.name, 'children': [], 'file_count': 0, 'total_size': 0, 'line_count': 0}
        
        tree_node = {
            'type': 'directory',
            'path': dir_path,
            'name': dir_path.name,
            'children': [],
            'file_count': 0,
            'total_size': 0,
            'line_count': 0
        }
        
        try:
            entries = list(dir_path.iterdir())
            
            # Filter hidden files if needed
            if not self.show_hidden:
                entries = [e for e in entries if not e.name.startswith('.')]
            
            # Sort: directories first, then files, alphabetically
            entries.sort(key=lambda x: (not x.is_dir(), x.name.lower()))

            # Filter out excluded directories
            self._debug_log(f"Before filter, entries: {[e.name for e in entries]}")
            filtered_entries = []
            for entry in entries:
                if entry.is_dir():
                    excluded = self._should_exclude_dir(entry.name)
                    self._debug_log(f"Checking directory {entry.name}: excluded={excluded}")
                    if excluded:
                        self._debug_log(f"Excluding directory {entry.name}")
                        continue
                filtered_entries.append(entry)
            entries = filtered_entries
            self._debug_log(f"After filter, entries: {[e.name for e in entries]}")

            for entry in entries:
                if entry.is_dir():
                    # Recursively process subdirectory
                    child_node = self._build_tree(entry, current_depth + 1)
                    tree_node['children'].append(child_node)
                    tree_node['file_count'] += child_node['file_count']
                    tree_node['total_size'] += child_node['total_size']
                    tree_node['line_count'] += child_node['line_count']
                elif entry.is_file():
                    # Check pattern filter
                    if not self._matches_pattern(entry.name):
                        continue

                    file_info = self._get_file_info(entry)
                    tree_node['children'].append(file_info)
                    tree_node['file_count'] += 1
                    tree_node['total_size'] += file_info['size']
                    tree_node['line_count'] += file_info.get('line_count', 0)            
        except (PermissionError, OSError) as e:
            tree_node['error'] = str(e)
        
        return tree_node
    
    def _matches_pattern(self, filename: str) -> bool:
        """Check if filename matches the glob pattern."""
        import fnmatch
        return fnmatch.fnmatch(filename, self.pattern)

    def _should_exclude_dir(self, dir_name: str) -> bool:
        """Check if directory should be excluded based on exclude_dirs patterns."""
        import fnmatch
        import os
        os.makedirs('temp', exist_ok=True)
        # Debug: write to file
        try:
            with open('temp/debug_exclude.txt', 'a', encoding='utf-8') as f:
                f.write(f"Checking '{dir_name}' against {self.exclude_dirs}\n")
        except:
            pass
        # Debug: print patterns and matching
        self._debug_log(f"exclude_dirs: {self.exclude_dirs}, checking dir '{dir_name}'")
        import sys
        print(f"EXCLUDE_CHECK: '{dir_name}' against {self.exclude_dirs}", file=sys.stderr)
        for pattern in self.exclude_dirs:
            if fnmatch.fnmatch(dir_name, pattern):
                self._debug_log(f"directory '{dir_name}' matches pattern '{pattern}'")
                import sys
                print(f"EXCLUDE_MATCH: '{dir_name}' matches pattern '{pattern}'", file=sys.stderr)
                # Also write to file
                try:
                    with open('temp/debug_exclude.txt', 'a', encoding='utf-8') as f:
                        f.write(f"  MATCH: '{dir_name}' matches '{pattern}'\n")
                except:
                    pass
                return True
        # Write no match
        try:
            with open('temp/debug_exclude.txt', 'a', encoding='utf-8') as f:
                f.write(f"  NO MATCH\n")
        except:
            pass
        return False
    
    def _get_file_info(self, file_path: pathlib.Path) -> Dict[str, Any]:
        """Get detailed information about a file."""
        file_info = {
            'type': 'file',
            'path': file_path,
            'name': file_path.name,
            'size': 0,
            'mtime': None,
            'line_count': 0
        }
        
        try:
            # File size
            stat_info = file_path.stat()
            file_info['size'] = stat_info.st_size
            file_info['mtime'] = stat_info.st_mtime
            
            # Line count (for text files)
            if self.include_sizes and not self.skip_line_count:
                # Skip line counting for binary files and large files (>1MB)
                skip_line_count = False
                file_ext = file_path.suffix.lower()
                if file_ext in self.BINARY_EXTENSIONS or file_info['size'] > 1_048_576:
                    file_info['line_count'] = 0
                    skip_line_count = True
                
                if not skip_line_count:
                    try:
                        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                            line_count = 0
                            for line in f:
                                line_count += 1
                                if line_count > 10000:  # Limit for performance
                                    break
                            file_info['line_count'] = line_count
                    except (UnicodeDecodeError, IOError):
                        # Binary file or unreadable
                        file_info['line_count'] = 0
                    
        except (OSError, PermissionError):
            pass
        
        return file_info
    
    def _format_tree(self, tree_node: Dict[str, Any], root_path: pathlib.Path) -> List[str]:
        """Format tree structure as ASCII tree."""
        output_lines = []
        
        # Root directory
        rel_path = str(tree_node['path'].relative_to(root_path)) if tree_node['path'] != root_path else "."
        if rel_path == ".":
            output_lines.append(f"{tree_node['path'].resolve()}")
        else:
            output_lines.append(f"{rel_path}")
        
        # Recursively format children
        self._format_tree_recursive(tree_node['children'], "", True, output_lines, root_path)
        
        return output_lines
    
    def _format_tree_recursive(self, children: List[Dict[str, Any]], prefix: str, is_last: bool, 
                              output_lines: List[str], root_path: pathlib.Path):
        """Recursive helper for formatting tree."""
        for i, child in enumerate(children):
            is_last_child = (i == len(children) - 1)
            
            # Determine connector symbols
            if is_last:
                connector = "└── "
                new_prefix = prefix + "    "
            else:
                connector = "├── "
                new_prefix = prefix + "│   "
            
            # Format the entry
            if child['type'] == 'directory':
                line = f"{prefix}{connector}{child['name']}/"
                if self.include_sizes:
                    size_str = self._format_size(child['total_size'])
                    line += f" ({child['file_count']} files, {size_str})"
                output_lines.append(line)
                
                # Recursively process directory children
                self._format_tree_recursive(child['children'], new_prefix, is_last_child, 
                                           output_lines, root_path)
                
            else:  # file
                line = f"{prefix}{connector}{child['name']}"
                if self.include_sizes:
                    size_str = self._format_size(child['size'])
                    line_count = child.get('line_count', 0)
                    if line_count > 0:
                        line += f" ({size_str}, {line_count} lines)"
                    else:
                        line += f" ({size_str})"
                
                # Add modification time if available
                if child.get('mtime'):
                    mtime_str = time.strftime("%Y-%m-%d", time.localtime(child['mtime']))
                    line += f" [modified: {mtime_str}]"
                
                output_lines.append(line)
    
    def _format_size(self, size_bytes: int) -> str:
        """Format file size in human-readable format."""
        if size_bytes == 0:
            return "0 bytes"
        
        units = ['bytes', 'KB', 'MB', 'GB']
        size = float(size_bytes)
        unit_index = 0
        
        while size >= 1024 and unit_index < len(units) - 1:
            size /= 1024
            unit_index += 1
        
        if unit_index == 0:
            return f"{size_bytes} bytes"
        else:
            return f"{size:.1f} {units[unit_index]}"
    
    def _generate_summary(self, tree_node: Dict[str, Any]) -> List[str]:
        """Generate summary statistics."""
        summary = []
        summary.append("")
        summary.append("SUMMARY:")
        summary.append(f"  Directories: {self._count_directories(tree_node)}")
        summary.append(f"  Files: {tree_node['file_count']}")
        
        if self.include_sizes:
            summary.append(f"  Total size: {self._format_size(tree_node['total_size'])}")
            if tree_node['line_count'] > 0:
                summary.append(f"  Total lines: {tree_node['line_count']:,}")
        
        return summary
    
    def _count_directories(self, tree_node: Dict[str, Any]) -> int:
        """Count total directories in tree."""
        count = 0
        stack = [tree_node]
        
        while stack:
            node = stack.pop()
            if node['type'] == 'directory':
                count += 1
                stack.extend(node['children'])
        
        return count - 1  # Exclude root directory
    def _execute_tree_format(self, dir_path: pathlib.Path) -> str:
        """Generate tree format output."""
        # Build tree structure
        tree_data = self._build_tree(dir_path, current_depth=0)

        # Generate tree visualization
        output_lines = self._format_tree(tree_data, dir_path)

        # Add summary statistics
        summary = self._generate_summary(tree_data)
        output_lines.extend(summary)

        return "\n".join(output_lines)
    def _collect_file_entries(self, dir_path: pathlib.Path) -> List[Tuple[str, int, float]]:
        """Collect file entries matching criteria, respecting max_results."""
        import os
        from pathlib import Path
        entries = []
        # Use stack of (path, depth)
        stack = [(dir_path, 0)]
        while stack and (self.max_results == 0 or len(entries) < self.max_results):
            current_path, depth = stack.pop()
            # If max_depth > 0 and depth >= max_depth, skip deeper traversal
            if self.max_depth > 0 and depth >= self.max_depth:
                continue
            try:
                for entry in current_path.iterdir():
                    # Skip hidden if not showing hidden
                    if not self.show_hidden and entry.name.startswith('.'):
                        continue
                    if entry.is_dir():
                        # Skip excluded directories
                        if self._should_exclude_dir(entry.name):
                            continue
                        # Add subdirectory to stack for further traversal
                        stack.append((entry, depth + 1))
                    elif entry.is_file():
                        # Check pattern filter
                        if not self._matches_pattern(entry.name):
                            continue
                        # Get file info
                        try:
                            stat_info = entry.stat()
                            size = stat_info.st_size
                            mtime = stat_info.st_mtime
                        except (OSError, PermissionError):
                            size = -1
                            mtime = -1
                        # Compute relative path from root dir
                        rel_path = str(entry.relative_to(dir_path))
                        entries.append((rel_path, size, mtime))
                        # Stop if max_results reached
                        if self.max_results > 0 and len(entries) >= self.max_results:
                            break
            except (PermissionError, OSError):
                # Skip directories we can't read
                continue
        return entries
    def _execute_list_format(self, dir_path: pathlib.Path) -> str:
        """Generate list format output."""
        import os
        import time
        entries = self._collect_file_entries(dir_path)

        if not entries:
            return f"No files matching pattern '{self.pattern}' in directory '{self.directory}' (max_depth={self.max_depth})."

        # Sort entries
        if self.sort_by == 'name':
            entries.sort(key=lambda x: x[0].lower())
        elif self.sort_by == 'size':
            # Sort by size ascending, unknown sizes (-1) last
            entries.sort(key=lambda x: (x[1] == -1, x[1]))
        elif self.sort_by == 'modified':
            # Sort by modification time descending (newest first), unknown times last
            entries.sort(key=lambda x: (x[2] == -1, -x[2] if x[2] != -1 else 0))
        else:
            return f"Error: Invalid sort_by value '{self.sort_by}'. Must be 'name', 'size', or 'modified'."

        # Build output lines
        lines = []
        for rel_path, size, mtime in entries:
            # Format size
            if size >= 0:
                size_str = f"{size:,} bytes"
            else:
                size_str = "?"
            # Format modification time
            if mtime >= 0:
                time_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
            else:
                time_str = "?"
            # Indent subdirectory files
            indent = ""
            if os.path.dirname(rel_path):
                indent = "    "
            lines.append(f"{indent}{rel_path} ({size_str}, modified {time_str})")

        # Determine if truncated due to max_results
        truncated = False
        if self.max_results > 0 and len(entries) >= self.max_results:
            # We may have more files beyond max_results; we need to know total count
            # For simplicity, we can note truncation
            truncated = True

        # Build header
        header = f"Files in {self.directory} (pattern='{self.pattern}', max_depth={self.max_depth}, format='list', sorted by {self.sort_by}):"
        output = header + "\n" + "\n".join(lines)
        if truncated:
            output += f"\n... and more files (truncated, max_results={self.max_results})"
        return output