from .base import ToolBase
import os
import pathlib
from pydantic import Field
from typing import Optional, ClassVar, Literal

class FilePreviewTool(ToolBase):
    """Show beginning and end of file with line numbers."""
    tool: Literal["FilePreviewTool"] = "FilePreviewTool"
    
    # Safety limits
    MAX_HEAD_LINES: ClassVar[int] = 500
    MAX_TAIL_LINES: ClassVar[int] = 500
    MAX_TOTAL_LINES: ClassVar[int] = 1000
    MAX_OUTPUT_CHARS: ClassVar[int] = 30_000

    filename: str = Field(description="Path to the file to preview")
    head_lines: int = Field(default=10, description="Number of lines to show from beginning of file")
    tail_lines: int = Field(default=5, description="Number of lines to show from end of file")
    show_line_numbers: bool = Field(default=True, description="Show line numbers in output")
    max_file_size: int = Field(default=10_000_000, description="Maximum file size to read (in bytes)")
    
    def execute(self) -> str:
        try:
            # Resolve file path
            file_path = pathlib.Path(self.filename)
            # Validate file path is within workspace
            try:
                validated_path = self._validate_path(str(file_path))
                file_path = pathlib.Path(validated_path)
            except ValueError as e:
                return self._truncate_output(f"Error: {e}")
            if not file_path.exists():
                return self._truncate_output(f"Error: File '{self.filename}' does not exist.")
            if not file_path.is_file():
                return self._truncate_output(f"Error: '{self.filename}' is not a file.")
            
            # Check file size
            try:
                file_size = file_path.stat().st_size
                if file_size > self.max_file_size:
                    return self._truncate_output(f"Error: File is too large ({file_size:,} bytes > {self.max_file_size:,} bytes limit).")
            except OSError:
                return self._truncate_output(f"Error: Cannot access file '{self.filename}'.")
            
            # Apply safety limits to head_lines and tail_lines
            head_lines = min(self.head_lines, self.MAX_HEAD_LINES)
            tail_lines = min(self.tail_lines, self.MAX_TAIL_LINES)
            # Ensure total lines shown does not exceed MAX_TOTAL_LINES
            if head_lines + tail_lines > self.MAX_TOTAL_LINES:
                # Reduce proportionally, but keep at least 1 each
                if head_lines > 0 and tail_lines > 0:
                    ratio = head_lines / (head_lines + tail_lines)
                    head_lines = max(1, int(self.MAX_TOTAL_LINES * ratio))
                    tail_lines = max(1, self.MAX_TOTAL_LINES - head_lines)
                elif head_lines > 0:
                    head_lines = min(head_lines, self.MAX_TOTAL_LINES)
                else:
                    tail_lines = min(tail_lines, self.MAX_TOTAL_LINES)
            
            # Read file lines
            lines = self._read_file_lines(file_path)
            if not lines:
                return self._truncate_output(f"File '{self.filename}' is empty or unreadable.")
            
            total_lines = len(lines)
            
            # Build output
            output_lines = []
            output_lines.append(f"File: {self.filename} ({total_lines:,} lines, {file_size:,} bytes)")
            output_lines.append("=" * 60)
            
            # Show head lines
            if head_lines > 0:
                head_section = self._extract_section(lines, 0, head_lines, "Beginning")
                output_lines.extend(head_section)
            
            # Show separator if both head and tail will be shown
            if head_lines > 0 and tail_lines > 0 and total_lines > (head_lines + tail_lines):
                omitted = total_lines - head_lines - tail_lines
                if omitted > 0:
                    output_lines.append(f"... {omitted:,} lines omitted ...")
            
            # Show tail lines
            if tail_lines > 0 and total_lines > head_lines:
                tail_start = max(head_lines, total_lines - tail_lines)
                tail_section = self._extract_section(lines, tail_start, tail_lines, "End")
                output_lines.extend(tail_section)
            
            # Add line range info
            output_lines.append("")
            output_lines.append(f"Line range shown: 1-{min(head_lines, total_lines)}",)
            if total_lines > head_lines:
                tail_start = max(head_lines + 1, total_lines - tail_lines + 1)
                output_lines.append(f"               and {tail_start}-{total_lines}")
            
            output = "\n".join(output_lines)
            return self._truncate_output(output)
            
        except Exception as e:
            return self._truncate_output(f"Error previewing file '{self.filename}': {e}")
    
    def _read_file_lines(self, file_path: pathlib.Path) -> list:
        """Read file lines with proper error handling."""
        lines = []
        try:
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                for i, line in enumerate(f):
                    lines.append(line.rstrip('\n'))
                    # Safety limit: don't read more than 100k lines for preview
                    if i >= 100_000:
                        break
        except UnicodeDecodeError:
            # Try different encoding
            try:
                with open(file_path, 'r', encoding='latin-1', errors='replace') as f:
                    for i, line in enumerate(f):
                        lines.append(line.rstrip('\n'))
                        if i >= 100_000:
                            break
            except Exception:
                return []
        except Exception:
            return []
        
        return lines
    
    def _extract_section(self, lines: list, start_idx: int, count: int, section_name: str) -> list:
        """Extract a section of lines and format with line numbers."""
        if start_idx >= len(lines):
            return []
        
        end_idx = min(start_idx + count, len(lines))
        section_lines = lines[start_idx:end_idx]
        
        output = []
        output.append(f"{section_name} (lines {start_idx + 1}-{end_idx}):")
        
        for i, line in enumerate(section_lines):
            line_num = start_idx + i + 1
            if self.show_line_numbers:
                # Format line number with padding
                line_num_str = f"{line_num:>6}: "
                output.append(line_num_str + line)
            else:
                output.append(line)
        
        return output
