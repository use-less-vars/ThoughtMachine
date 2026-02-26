# tools/file_line_inserter.py
from typing import List
from .base import ToolBase
from pydantic import Field

class FileLineInserter(ToolBase):
    """Insert multiple lines at a specific position in a file."""
    filename: str = Field(description="Path to the file to modify")
    line_number: int = Field(description="Line number to insert before (1-indexed)")
    content: List[str] = Field(description="List of lines to insert")
    
    def execute(self) -> str:
        try:
            # Read the entire file
            with open(self.filename, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            total_lines = len(lines)
            line_num = self.line_number
            
            if line_num < 1:
                return f"Error: Line number must be >= 1 (got {line_num})"
            
            if line_num > total_lines + 1:
                return f"Error: Cannot insert at line {line_num} (file has {total_lines} lines)"
            
            # Prepare the lines to insert
            lines_to_insert = [line.rstrip('\n') + '\n' for line in self.content]
            
            # Insert the lines
            insertion_point = line_num - 1
            for i, line in enumerate(lines_to_insert):
                lines.insert(insertion_point + i, line)
            
            # Write back to file
            with open(self.filename, 'w', encoding='utf-8') as f:
                f.writelines(lines)
            
            new_total = len(lines)
            inserted_count = len(lines_to_insert)
            return f"Successfully inserted {inserted_count} lines at position {line_num} in {self.filename}\n" \
                   f"File size changed from {total_lines} to {new_total} lines"
            
        except FileNotFoundError:
            return f"Error: File '{self.filename}' not found"
        except Exception as e:
            return f"Error inserting lines: {e}"