# tools/file_line_appender.py
from typing import List
from .base import ToolBase
from pydantic import Field

class FileLineAppender(ToolBase):
    """Append lines to the end of a file."""
    filename: str = Field(description="Path to the file to modify")
    content: List[str] = Field(description="List of lines to append")
    
    def execute(self) -> str:
        try:
            # Read the entire file to get current line count
            with open(self.filename, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            total_lines = len(lines)
            
            # Prepare the lines to append
            lines_to_append = [line.rstrip('\n') + '\n' for line in self.content]
            
            # Append to file
            with open(self.filename, 'a', encoding='utf-8') as f:
                for line in lines_to_append:
                    f.write(line)
            
            appended_count = len(lines_to_append)
            new_total = total_lines + appended_count
            return f"Successfully appended {appended_count} lines to {self.filename}\n" \
                   f"File size changed from {total_lines} to {new_total} lines"
            
        except FileNotFoundError:
            # If file doesn't exist, create it
            try:
                lines_to_append = [line.rstrip('\n') + '\n' for line in self.content]
                with open(self.filename, 'w', encoding='utf-8') as f:
                    f.writelines(lines_to_append)
                appended_count = len(lines_to_append)
                return f"Created new file {self.filename} with {appended_count} lines"
            except Exception as e:
                return f"Error creating file: {e}"
        except Exception as e:
            return f"Error appending to file: {e}"