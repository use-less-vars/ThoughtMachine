# tools/batch_file_editor.py
from typing import List, Optional, Union, Dict, Literal
from pathlib import Path
from pydantic import Field, model_validator
import os
from .base import ToolBase


class BatchFileEditor(ToolBase):
    """Batch file editor supporting read, write, insert, append, replace, and delete operations across multiple files.
    Note: FileEditor now supports batch operations via the `filenames` parameter. This tool is kept for backward compatibility."""
    
    operation: Literal["read", "write", "insert", "append", "replace", "delete"] = Field(
        description="Operation to perform: 'read' (read file), 'write' (write content), 'insert' (insert lines), 'append' (append lines), 'replace' (replace specific lines), 'delete' (delete lines)"
    )
    filenames: List[str] = Field(
        description="List of file paths to operate on"
    )
    content: Optional[Union[str, List[str]]] = Field(
        default=None,
        description="Content for write/insert/append/replace operations. String for write, list of strings for insert/append/replace."
    )
    line_number: Optional[int] = Field(
        default=None,
        description="Line number for insert or write operations (1-indexed). For write operation, specifies which line to overwrite."
    )
    line_numbers: Optional[Union[int, List[int], str]] = Field(
        default=None,
        description="Line number(s) for read or delete operations. Can be: single int, list of ints, 'all', or range string like '1-10'."
    )
    replacements: Optional[Dict[int, str]] = Field(
        default=None,
        description="Dictionary mapping line numbers to new content for replace operation (line numbers are 1-indexed)."
    )
    mode: Literal["replace", "insert", "append"] = Field(
        default="replace",
        description="Mode for write operation: 'replace' (overwrite line), 'insert' (insert before line), 'append' (append after line). Only used when operation='write' and line_number is specified."
    )
    workspace: Literal["stable", "construction"] = Field(
        default="stable",
        description="Workspace to operate in: 'stable' (current directory) or 'construction' (./construction/ directory)"
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
            if self.replacements is None:
                raise ValueError("replacements required for replace")
        elif op == "delete":
            if self.line_numbers is None:
                raise ValueError("line_numbers required for delete")
        return self

    @model_validator(mode='after')
    def validate_filenames(self):
        if not self.filenames:
            raise ValueError("filenames list cannot be empty")
        return self

    def _get_actual_path(self, filename: str) -> str:
        """Convert filename based on workspace setting."""
        if self.workspace == "construction":
            # Ensure construction directory exists
            Path("./construction").mkdir(parents=True, exist_ok=True)
            # If filename is absolute, keep it as is (no workspace mapping)
            if os.path.isabs(filename):
                return filename
            # Prefix with construction directory
            return f"./construction/{filename}"
        return filename
    def execute(self) -> str:
        results = []
        for filename in self.filenames:
            try:
                actual_filename = self._get_actual_path(filename)
                if self.operation == "read":
                    result = self._execute_read(actual_filename)
                elif self.operation == "write":
                    result = self._execute_write(actual_filename)
                elif self.operation == "insert":
                    result = self._execute_insert(actual_filename)
                elif self.operation == "append":
                    result = self._execute_append(actual_filename)
                elif self.operation == "replace":
                    result = self._execute_replace(actual_filename)
                elif self.operation == "delete":
                    result = self._execute_delete(actual_filename)
                else:
                    result = f"Error: Unknown operation {self.operation}"
                results.append(f"{filename}: {result}")
            except Exception as e:
                results.append(f"{filename}: Error: {e}")
        
        # Format output
        success_count = sum(1 for r in results if "Error:" not in r)
        error_count = len(results) - success_count
        
        output = f"Batch operation '{self.operation}' completed.\n"
        output += f"Total files processed: {len(self.filenames)}\n"
        output += f"Successful: {success_count}, Failed: {error_count}\n\n"
        output += "Detailed results:\n" + "\n".join(results)
        return output

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

        # Format the output
        output_lines = []
        for idx, line in zip(line_indices, result_lines):
            output_lines.append(f"Line {idx}: {line.rstrip()}")

        return f"Total lines: {total_lines}\n" + "\n".join(output_lines)

    def _execute_write(self, filename: str) -> str:
        """Write content to file or specific line."""
        if self.line_number is None:
            # Write entire file
            content_str = self.content if isinstance(self.content, str) else "\n".join(self.content)
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(content_str)
            return f"Successfully wrote entire file"
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

            return result_msg

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

        return f"Inserted {len(new_lines)} line(s) before line {line_num}"

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

        return f"Appended {lines_added} line(s)"

    def _execute_replace(self, filename: str) -> str:
        """Replace specific lines."""
        with open(filename, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        total_lines = len(lines)
        replacements = self.replacements

        # Validate line numbers
        for line_num in replacements.keys():
            if line_num < 1 or line_num > total_lines:
                return f"Error: Line number {line_num} is out of range (file has {total_lines} lines)"

        # Apply replacements
        for line_num, new_content in replacements.items():
            lines[line_num - 1] = new_content + "\n"

        with open(filename, 'w', encoding='utf-8') as f:
            f.writelines(lines)

        return f"Replaced {len(replacements)} line(s)"

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

        return f"Deleted {len(delete_indices)} line(s)"