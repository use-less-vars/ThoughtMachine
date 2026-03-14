from pydantic import Field
from typing import Optional
from datetime import datetime
import os
from pathlib import Path
from .final import Final


class FinalReport(Final):
    """Write a timestamped final report and stop agent execution.
    
    Use this tool to complete work with comprehensive documentation.
    The agent stops after writing the report and returning the final answer.
    This is for task completion with full reporting.
    """
    content: str = Field(default="Report written successfully.", description="The final answer text")
    report_body: Optional[str] = Field(
        default=None, 
        description="The body/content of the report to write to file. If not provided, defaults to the content field."
    )
    report_title: Optional[str] = Field(
        default=None, 
        description="Optional title for the report file name"
    )

    def execute(self) -> str:
        # Ensure reports directory exists
        reports_dir = Path("./reports")
        reports_dir.mkdir(parents=True, exist_ok=True)
        
        # Generate timestamp
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        
        # Determine report body (default to content if not provided)
        report_body = self.report_body if self.report_body is not None else self.content
        
        # Build filename
        if self.report_title:
            # Sanitize title: replace spaces with underscores, remove special characters
            sanitized = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in self.report_title)
            sanitized = sanitized.replace(' ', '_')
            # Limit length
            if len(sanitized) > 50:
                sanitized = sanitized[:50]
            filename = f"{timestamp}_{sanitized}.txt"
        else:
            filename = f"{timestamp}_final_report.txt"
        
        filepath = reports_dir / filename
        
        # Write report content
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(report_body)
            
            # Return final message (content inherited from Final)
            return self._truncate_output(self.content)
        except Exception as e:
            return self._truncate_output(f"Failed to write final report: {e}")