from pydantic import Field
from typing import Optional
from datetime import datetime
import os
from pathlib import Path
from .base import ToolBase


class ProgressReport(ToolBase):
    """Write a timestamped progress report without stopping agent execution.
    
    Use this tool to document intermediate progress, milestones, or status updates
    during long-running batch jobs. The agent continues executing after writing the report.
    """
    report_body: str = Field(description="The body/content of the progress report")
    report_title: Optional[str] = Field(
        default=None, 
        description="Optional title for the report file name"
    )
    append: bool = Field(
        default=False, 
        description="Append to existing report file instead of creating new file"
    )

    def execute(self) -> str:
        # Ensure reports directory exists
        reports_dir = Path("./reports")
        reports_dir.mkdir(parents=True, exist_ok=True)
        
        # Generate timestamp for this update
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        
        # Build filename
        if self.append:
            # For append mode, use a fixed filename based on title or generic name
            if self.report_title:
                sanitized = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in self.report_title)
                sanitized = sanitized.replace(' ', '_')
                if len(sanitized) > 50:
                    sanitized = sanitized[:50]
                filename = f"progress_log_{sanitized}.txt"
            else:
                filename = "progress_log.txt"
        else:
            # For new files, include timestamp for uniqueness
            if self.report_title:
                sanitized = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in self.report_title)
                sanitized = sanitized.replace(' ', '_')
                if len(sanitized) > 50:
                    sanitized = sanitized[:50]
                filename = f"{timestamp}_{sanitized}.txt"
            else:
                filename = f"{timestamp}_progress_report.txt"
        
        filepath = reports_dir / filename
        
        # Write report content
        try:
            mode = "a" if self.append else "w"
            with open(filepath, mode, encoding="utf-8") as f:
                if self.append:
                    f.write(f"\n\n--- Progress Update at {timestamp} ---\n\n")
                else:
                    # For new files, optionally add a header
                    f.write(f"--- Progress Report started at {timestamp} ---\n\n")
                f.write(self.report_body)
            
            action = "Appended to" if self.append else "Wrote"
            return self._truncate_output(f"{action} progress report: {filepath}")
        except Exception as e:
            return self._truncate_output(f"Failed to write progress report: {e}")