# tools/file_line_writer.py
from typing import List, Optional
from .base import ToolBase
from pydantic import Field

class FileLineWriter(ToolBase):
    """Write content to specific lines in a file."""
    filename: str = Field(description="Path to the file to modify")
    line_number: int = Field(description="Line number to write to (1-indexed)")
    content: str = Field(description="Content to write to the specified line")
    mode: str = Field(
        default="replace",
        description="Write mode: 'replace' (overwrite line), 'insert' (insert before), 'append' (append to line)"
    )
    
    def execute(self) -> str:
        try:
            # Read the entire file
            with open(self.filename, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            total_lines = len(lines)
            line_num = self.line_number
            
            if line_num < 1:
                return f"Error: Line number must be >= 1 (got {line_num})"
            
            if self.mode == "replace":
                # Replace existing line
                if line_num > total_lines:
                    # Pad with empty lines if needed
                    while len(lines) < line_num:
                        lines.append("\n")
                    lines[line_num - 1] = self.content.rstrip('\n') + '\n'
                else:
                    lines[line_num - 1] = self.content.rstrip('\n') + '\n'
                    
            elif self.mode == "insert":
                # Insert before the specified line
                if line_num > total_lines + 1:
                    return f"Error: Cannot insert at line {line_num} (file has {total_lines} lines)"
                lines.insert(line_num - 1, self.content.rstrip('\n') + '\n')
                
            elif self.mode == "append":
                # Append to existing line
                if line_num > total_lines:
                    return f"Error: Cannot append to line {line_num} (file has {total_lines} lines)"
                lines[line_num - 1] = lines[line_num - 1].rstrip('\n') + self.content + '\n'
                
            else:
                return f"Error: Invalid mode '{self.mode}'. Use 'replace', 'insert', or 'append'"
            
            # Write back to file
            with open(self.filename, 'w', encoding='utf-8') as f:
                f.writelines(lines)
            
            new_total = len(lines)
            action = self.mode
            return f"Successfully {action}ed content at line {line_num} in {self.filename}\n" \
                   f"File size changed from {total_lines} to {new_total} lines"
            
        except FileNotFoundError:
            return f"Error: File '{self.filename}' not found"
        except Exception as e:
            return f"Error writing to file: {e}"