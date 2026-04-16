# tools/file_editor.py
from typing import List, Optional, Union, Dict, Literal
import os
from pathlib import Path
from pydantic import Field, model_validator
from .base import ToolBase


class FileEditor(ToolBase):
    """Unified file editor supporting read, write, insert, append, replace, and delete operations.
    Supports single file operations or batch operations across multiple files."""
    tool: Literal["FileEditor"] = "FileEditor"
    operation: Literal["read", "write", "insert", "append", "replace", "delete", "grep"] = Field(
        description="Operation: read, write, insert, append, replace, delete, or grep."
    )
    filename: Optional[str] = Field(
        default=None,
        description="Path to a single file."
    )
    filenames: Optional[List[str]] = Field(
        default=None,
        description="List of file paths to operate on."
    )
    content: Optional[Union[str, List[str]]] = Field(
        default=None,
        description="Content for write/insert/append/replace operations."
    )
    line_number: Optional[int] = Field(
        default=None,
        description="Line number for insert/write (1-indexed)."
    )
    line_numbers: Optional[Union[int, List[int], str]] = Field(
        default=None,
        description="Line number(s) for read/delete: int, list, 'all', or range."
    )
    replacements: Optional[Dict[int, str]] = Field(
        default=None,
        description="Dictionary mapping line numbers to new content."
    )
    mode: Literal["replace", "insert", "append"] = Field(
        default="replace",
        description="Mode for write operation: replace, insert, or append."
    )
    context_lines: int = Field(
        default=0,
        description="Number of surrounding lines to show when reading specific lines (only for read operation)"
    )
    pattern: Optional[str] = Field(
        default=None,
        description="Pattern for grep operation (only for grep operation)"
    )

    @model_validator(mode='after')
    def validate_operation(self):
        op = self.operation
        if op == "read":
            pass
        elif op == "write":
            if self.content is None:
                raise ValueError("content required for write")
        elif op == "insert":
            if self.content is None or self.line_number is None:
                raise ValueError("content and line_number required for insert")
        elif op == "append":
            if self.content is None:
                raise ValueError("content required for append")
        elif op == "replace":
            # For replace operation, we accept either:
            # 1. replacements dict (original behavior)
            # 2. content + line_numbers (for replacing range/list with same content)
            # 3. content + line_number (backward compatibility for single line)
            if self.replacements is None:
                if self.content is None:
                    raise ValueError("For replace operation, either replacements dict or content is required")
                if self.line_numbers is None and self.line_number is None:
                    raise ValueError("For replace operation with content, line_numbers or line_number is required")
        elif op == "delete":
            if self.line_numbers is None:
                raise ValueError("line_numbers required for delete")
        elif op == "grep":
            if self.pattern is None:
                raise ValueError("pattern required for grep")
        return self


    @model_validator(mode='after')
    def validate_filenames(self):
        if self.filename is None and self.filenames is None:
            raise ValueError("Either filename or filenames must be provided")
        if self.filename is not None and self.filenames is not None:
            raise ValueError("Cannot provide both filename and filenames")
        if self.filenames is not None and not self.filenames:
            raise ValueError("filenames list cannot be empty")
        return self

    def execute(self) -> str:
        # Determine target files
        if self.filenames is not None:
            target_files = self.filenames
            batch_mode = True
        else:
            target_files = [self.filename]
            batch_mode = False
        
        results = []
        for filename in target_files:
            try:
                actual_filename = filename
                # Validate path is within workspace if workspace_path is set
                validated_filename = self._validate_path(actual_filename)
                if self.operation == "read":
                    result = self._execute_read(validated_filename)
                elif self.operation == "write":
                    result = self._execute_write(validated_filename)
                elif self.operation == "insert":
                    result = self._execute_insert(validated_filename)
                elif self.operation == "append":
                    result = self._execute_append(validated_filename)
                elif self.operation == "replace":
                    result = self._execute_replace(validated_filename)
                elif self.operation == "delete":
                    result = self._execute_delete(validated_filename)
                elif self.operation == "grep":
                    result = self._execute_grep(validated_filename)
                else:
                    result = f"Error: Unknown operation {self.operation}"
                    result = self._execute_delete(validated_filename)
                results.append(f"{filename}: {result}")
            except Exception as e:
                results.append(f"{filename}: Error: {e}")
        # Format output
        if batch_mode:
            success_count = sum(1 for r in results if "Error:" not in r)
            error_count = len(results) - success_count
            output = f"Batch operation '{self.operation}' completed.\n"
            output += f"Total files processed: {len(target_files)}\n"
            output += f"Successful: {success_count}, Failed: {error_count}\n\n"
            output += "Detailed results:\n" + "\n".join(results)
            return self._truncate_output(output)
        else:
            # Single file: return the result directly (without filename prefix)
            # The result already includes filename in its message
            result = results[0].split(": ", 1)[1] if ": " in results[0] else results[0]
            return self._truncate_output(result)

    # Helper methods that accept filename parameter
    def _execute_read(self, filename: str) -> str:
        """Read file or specific lines."""
        with open(filename, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        total_lines = len(lines)

        if self.line_numbers is None or self.line_numbers == 'all':
            # Read all lines
            result_lines = lines
            line_indices = list(range(1, total_lines + 1))
        elif isinstance(self.line_numbers, int):
            # Single line number
            line_num = self.line_numbers
            if line_num < 1 or line_num > total_lines:
                return f"Error: Line number {line_num} is out of range (file has {total_lines} lines)"
            result_lines = [lines[line_num - 1]]
            line_indices = [line_num]
        elif isinstance(self.line_numbers, list):
            # List of line numbers
            line_indices = []
            result_lines = []
            
            # Special case: 2-element list treated as range [start, end]
            if len(self.line_numbers) == 2:
                start, end = self.line_numbers[0], self.line_numbers[1]
                if start < 1 or end > total_lines or start > end:
                    return f"Error: Invalid range {start}-{end} (file has {total_lines} lines)"
                line_indices = list(range(start, end + 1))
                result_lines = [lines[i - 1] for i in line_indices]
            else:
                # Regular list of discrete line numbers
                for line_num in self.line_numbers:
                    if line_num < 1 or line_num > total_lines:
                        return f"Error: Line number {line_num} is out of range (file has {total_lines} lines)"
                    line_indices.append(line_num)
                    result_lines.append(lines[line_num - 1])
        elif isinstance(self.line_numbers, str) and '-' in self.line_numbers:
            # Range string like "1-10"
            try:
                start_str, end_str = self.line_numbers.split('-')
                start = int(start_str.strip())
                end = int(end_str.strip())

                if start < 1 or end > total_lines or start > end:
                    return f"Error: Invalid range {start}-{end} (file has {total_lines} lines)"

                line_indices = list(range(start, end + 1))
                result_lines = [lines[i - 1] for i in line_indices]
            except ValueError:
                return f"Error: Invalid range format '{self.line_numbers}'. Use format like '1-10'"
        else:
            return f"Error: Invalid line_numbers parameter: {self.line_numbers}"

        # Apply context lines if specified
        if self.context_lines > 0:
            expanded_indices = set()
            for idx in line_indices:
                start = max(1, idx - self.context_lines)
                end = min(total_lines, idx + self.context_lines)
                expanded_indices.update(range(start, end + 1))
            line_indices = sorted(expanded_indices)
            result_lines = [lines[i-1] for i in line_indices]

        # Format the output
        output_lines = []
        for idx, line in zip(line_indices, result_lines):
            output_lines.append(f"Line {idx}: {line.rstrip()}")

        return f"File: {filename}\nTotal lines: {total_lines}\n" + "\n".join(output_lines)
        output_lines = []
        for idx, line in zip(line_indices, result_lines):
            output_lines.append(f"Line {idx}: {line.rstrip()}")

        return f"File: {filename}\nTotal lines: {total_lines}\n" + "\n".join(output_lines)

    def _execute_write(self, filename: str) -> str:
        """Write content to file or specific line."""
        if self.line_number is None:
            # Write entire file
            content_str = self.content if isinstance(self.content, str) else "\n".join(self.content)
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(content_str)
            return f"Successfully wrote to {filename}"
        else:
            # Write to specific line
            with open(filename, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            total_lines = len(lines)
            line_num = self.line_number

            if line_num < 1 or line_num > total_lines + 1:
                return f"Error: Line number {line_num} is out of range (file has {total_lines} lines)"

            # Convert content to list of lines
            if isinstance(self.content, str):
                new_lines = [self.content + "\n"]
            else:
                new_lines = [line + "\n" for line in self.content]

            if self.mode == "replace":
                # Overwrite existing line
                if line_num <= total_lines:
                    lines[line_num - 1] = new_lines[0]
                else:
                    # Append if beyond end
                    lines.extend(new_lines)
                result_msg = f"Replaced line {line_num}"
            elif self.mode == "insert":
                # Insert before line
                lines = lines[:line_num - 1] + new_lines + lines[line_num - 1:]
                result_msg = f"Inserted {len(new_lines)} line(s) before line {line_num}"
            elif self.mode == "append":
                # Insert after line
                lines = lines[:line_num] + new_lines + lines[line_num:]
                result_msg = f"Appended {len(new_lines)} line(s) after line {line_num}"
            else:
                return f"Error: Invalid mode '{self.mode}'"

            with open(filename, 'w', encoding='utf-8') as f:
                f.writelines(lines)

            return f"Successfully modified {filename}: {result_msg}"

    def _execute_insert(self, filename: str) -> str:
        """Insert lines before specified line."""
        with open(filename, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        total_lines = len(lines)
        line_num = self.line_number

        if line_num < 1 or line_num > total_lines + 1:
            return f"Error: Line number {line_num} is out of range (file has {total_lines} lines)"

        # Convert content to list of lines with newline
        if isinstance(self.content, str):
            new_lines = [self.content + "\n"]
        else:
            new_lines = [line + "\n" for line in self.content]

        lines = lines[:line_num - 1] + new_lines + lines[line_num - 1:]

        with open(filename, 'w', encoding='utf-8') as f:
            f.writelines(lines)

        return f"Successfully inserted {len(new_lines)} line(s) before line {line_num} in {filename}"

    def _execute_append(self, filename: str) -> str:
        """Append lines to end of file."""
        with open(filename, 'a', encoding='utf-8') as f:
            if isinstance(self.content, str):
                f.write(self.content + "\n")
                lines_added = 1
            else:
                for line in self.content:
                    f.write(line + "\n")
                lines_added = len(self.content)

        return f"Successfully appended {lines_added} line(s) to {filename}"

    def _execute_replace(self, filename: str) -> str:
        """Replace specific lines."""
        with open(filename, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        total_lines = len(lines)
        
        # Build replacements dict from either self.replacements or content + line_numbers/line_number
        replacements = {}
        
        if self.replacements is not None:
            # Original behavior: use replacements dict
            replacements = self.replacements
        else:
            # New behavior: use content + line_numbers/line_number
            # Normalize line_numbers
            line_numbers = self.line_numbers
            if line_numbers is None and self.line_number is not None:
                line_numbers = self.line_number
            
            if line_numbers is None:
                return "Error: No line numbers specified for replace operation"
            
            # Parse line_numbers into list of line numbers
            line_nums = []
            
            if line_numbers == 'all':
                line_nums = list(range(1, total_lines + 1))
            elif isinstance(line_numbers, int):
                line_nums = [line_numbers]
            elif isinstance(line_numbers, list):
                # Special case: 2-element list treated as range [start, end]
                if len(line_numbers) == 2:
                    start, end = line_numbers[0], line_numbers[1]
                    if start < 1 or end > total_lines or start > end:
                        return f"Error: Invalid range {start}-{end} (file has {total_lines} lines)"
                    line_nums = list(range(start, end + 1))
                else:
                    line_nums = line_numbers
            elif isinstance(line_numbers, str) and '-' in line_numbers:
                try:
                    start_str, end_str = line_numbers.split('-')
                    start = int(start_str.strip())
                    end = int(end_str.strip())
                    if start < 1 or end > total_lines or start > end:
                        return f"Error: Invalid range {start}-{end} (file has {total_lines} lines)"
                    line_nums = list(range(start, end + 1))
                except ValueError:
                    return f"Error: Invalid range format '{line_numbers}'. Use format like '1-10'"
            else:
                return f"Error: Invalid line_numbers parameter: {line_numbers}"
            
            # Validate all line numbers are in range
            for line_num in line_nums:
                if line_num < 1 or line_num > total_lines:
                    return f"Error: Line number {line_num} is out of range (file has {total_lines} lines)"
            
            # Create replacements dict from content
            content = self.content
            if isinstance(content, str):
                # Single content for all lines
                for line_num in line_nums:
                    replacements[line_num] = content
            elif isinstance(content, list):
                # List of content - should match number of lines
                if len(content) != len(line_nums):
                    return f"Error: Content list length ({len(content)}) doesn't match number of lines to replace ({len(line_nums)})"
                for line_num, content_item in zip(line_nums, content):
                    replacements[line_num] = content_item
            else:
                return f"Error: Invalid content type: {type(content)}"
        
        # Apply replacements
        for line_num, new_content in replacements.items():
            lines[line_num - 1] = new_content + "\n"

        with open(filename, 'w', encoding='utf-8') as f:
            f.writelines(lines)

        return f"Successfully replaced {len(replacements)} line(s) in {filename}"

    def _execute_delete(self, filename: str) -> str:
        """Delete specific lines."""
        with open(filename, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        total_lines = len(lines)

        # Determine which line indices to delete
        delete_indices = set()

        if isinstance(self.line_numbers, int):
            line_num = self.line_numbers
            if line_num < 1 or line_num > total_lines:
                return f"Error: Line number {line_num} is out of range (file has {total_lines} lines)"
            delete_indices.add(line_num - 1)
        elif isinstance(self.line_numbers, list):
            # Special case: 2-element list treated as range [start, end]
            if len(self.line_numbers) == 2:
                start, end = self.line_numbers[0], self.line_numbers[1]
                if start < 1 or end > total_lines or start > end:
                    return f"Error: Invalid range {start}-{end} (file has {total_lines} lines)"
                for line_num in range(start, end + 1):
                    delete_indices.add(line_num - 1)
            else:
                # Regular list of discrete line numbers
                for line_num in self.line_numbers:
                    if line_num < 1 or line_num > total_lines:
                        return f"Error: Line number {line_num} is out of range (file has {total_lines} lines)"
                    delete_indices.add(line_num - 1)
        elif isinstance(self.line_numbers, str) and '-' in self.line_numbers:
            try:
                start_str, end_str = self.line_numbers.split('-')
                start = int(start_str.strip())
                end = int(end_str.strip())

                if start < 1 or end > total_lines or start > end:
                    return f"Error: Invalid range {start}-{end} (file has {total_lines} lines)"

                for line_num in range(start, end + 1):
                    delete_indices.add(line_num - 1)
            except ValueError:
                return f"Error: Invalid range format '{self.line_numbers}'. Use format like '1-10'"
        else:
            return f"Error: Invalid line_numbers parameter: {self.line_numbers}"

        # Delete lines (in reverse order to preserve indices)
        for idx in sorted(delete_indices, reverse=True):
            del lines[idx]

        with open(filename, 'w', encoding='utf-8') as f:
            f.writelines(lines)

        return f"Successfully deleted {len(delete_indices)} line(s) from {filename}"

    def _execute_grep(self, filename: str) -> str:
        """Search for pattern in file."""
        try:
            with open(filename, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
        except Exception as e:
            return f"Error reading file: {e}"
        
        matches = []
        pattern = self.pattern
        for i, line in enumerate(lines, start=1):
            if pattern in line:
                matches.append((i, line.rstrip()))
        
        if not matches:
            return f"No matches for pattern '{pattern}' in {filename}"
        
        # Limit matches for readability
        max_matches = 20
        displayed = matches[:max_matches]
        output_lines = [f"File: {filename}", f"Pattern: '{pattern}'", f"Total matches: {len(matches)}"]
        for line_num, line in displayed:
            output_lines.append(f"Line {line_num}: {line}")
        if len(matches) > max_matches:
            output_lines.append(f"... and {len(matches) - max_matches} more matches")
        
        return "\n".join(output_lines)
