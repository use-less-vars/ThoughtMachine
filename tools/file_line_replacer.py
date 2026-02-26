# tools/file_line_replacer.py
from typing import List, Dict, Union
from .base import ToolBase
from pydantic import Field

class FileLineReplacer(ToolBase):
    """Replace specific lines in a file with new content."""
    filename: str = Field(description="Path to the file to modify")
    replacements: Dict[int, str] = Field(
        description="Dictionary mapping line numbers to new content (line numbers are 1-indexed)"
    )
    
    def execute(self) -> str:
        try:
            # Read the entire file
            with open(self.filename, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            total_lines = len(lines)
            replaced_lines = []
            
            # Sort line numbers to process in order
            line_numbers = sorted(self.replacements.keys())
            
            for line_num in line_numbers:
                if line_num < 1:
                    return f"Error: Line number must be >= 1 (got {line_num})"
                
                if line_num > total_lines:
                    # Pad with empty lines if needed
                    while len(lines) < line_num:
                        lines.append("\n")
                    total_lines = len(lines)
                
                new_content = self.replacements[line_num]
                lines[line_num - 1] = new_content.rstrip('\n') + '\n'
                replaced_lines.append(line_num)
            
            # Write back to file
            with open(self.filename, 'w', encoding='utf-8') as f:
                f.writelines(lines)
            
            new_total = len(lines)
            replaced_count = len(replaced_lines)
            lines_str = ", ".join(str(ln) for ln in replaced_lines)
            return f"Successfully replaced {replaced_count} lines ({lines_str}) in {self.filename}\n" \
                   f"File size changed from {total_lines} to {new_total} lines"
            
        except FileNotFoundError:
            return f"Error: File '{self.filename}' not found"
        except Exception as e:
            return f"Error replacing lines: {e}"