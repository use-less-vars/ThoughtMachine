# tools/file_line_deleter.py
from typing import List, Union
from .base import ToolBase
from pydantic import Field

class FileLineDeleter(ToolBase):
    """Delete specific lines from a file."""
    filename: str = Field(description="Path to the file to modify")
    line_numbers: Union[int, List[int], str] = Field(
        description="Line number(s) to delete. Can be: single int, list of ints, or range string like '1-10'"
    )
    
    def execute(self) -> str:
        try:
            # Read the entire file
            with open(self.filename, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            total_lines = len(lines)
            
            # Determine which lines to delete
            lines_to_delete = set()
            
            if isinstance(self.line_numbers, int):
                # Single line number
                line_num = self.line_numbers
                if line_num < 1 or line_num > total_lines:
                    return f"Error: Line number {line_num} is out of range (file has {total_lines} lines)"
                lines_to_delete.add(line_num - 1)  # Convert to 0-indexed
                
            elif isinstance(self.line_numbers, list):
                # List of line numbers
                for line_num in self.line_numbers:
                    if line_num < 1 or line_num > total_lines:
                        return f"Error: Line number {line_num} is out of range (file has {total_lines} lines)"
                    lines_to_delete.add(line_num - 1)  # Convert to 0-indexed
                    
            elif isinstance(self.line_numbers, str) and '-' in self.line_numbers:
                # Range string like "1-10"
                try:
                    start_str, end_str = self.line_numbers.split('-')
                    start = int(start_str.strip())
                    end = int(end_str.strip())
                    
                    if start < 1 or end > total_lines or start > end:
                        return f"Error: Invalid range {start}-{end} (file has {total_lines} lines)"
                    
                    for line_num in range(start, end + 1):
                        lines_to_delete.add(line_num - 1)  # Convert to 0-indexed
                except ValueError:
                    return f"Error: Invalid range format '{self.line_numbers}'. Use format like '1-10'"
            else:
                return f"Error: Invalid line_numbers parameter: {self.line_numbers}"
            
            # Delete lines (in reverse order to maintain correct indices)
            deleted_indices = sorted(lines_to_delete, reverse=True)
            deleted_lines_content = []
            
            for idx in deleted_indices:
                deleted_lines_content.insert(0, lines[idx].rstrip())  # Store in original order
                del lines[idx]
            
            # Write back to file
            with open(self.filename, 'w', encoding='utf-8') as f:
                f.writelines(lines)
            
            new_total = len(lines)
            deleted_count = len(deleted_indices)
            
            # Format output
            output = f"Successfully deleted {deleted_count} lines from {self.filename}\n"
            output += f"File size changed from {total_lines} to {new_total} lines\n"
            
            if deleted_count <= 10:  # Show content if not too many lines
                output += "\nDeleted lines:\n"
                for i, (orig_idx, content) in enumerate(zip(sorted(lines_to_delete), deleted_lines_content)):
                    output += f"  Line {orig_idx + 1}: {content}\n"
            
            return output
            
        except FileNotFoundError:
            return f"Error: File '{self.filename}' not found"
        except Exception as e:
            return f"Error deleting lines: {e}"