# tools/file_line_reader.py
from typing import List, Optional, Union
from .base import ToolBase
from pydantic import Field

class FileLineReader(ToolBase):
    """Read specific lines from a file using line numbers."""
    filename: str = Field(description="Path to the file to read")
    line_numbers: Optional[Union[int, List[int], str]] = Field(
        default=None,
        description="Line number(s) to read. Can be: single int, list of ints, 'all', or range string like '1-10'"
    )
    
    def execute(self) -> str:
        try:
            with open(self.filename, 'r', encoding='utf-8') as f:
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
            
            return f"File: {self.filename}\nTotal lines: {total_lines}\n" + "\n".join(output_lines)
            
        except FileNotFoundError:
            return f"Error: File '{self.filename}' not found"
        except Exception as e:
            return f"Error reading file: {e}"