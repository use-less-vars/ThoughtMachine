from pydantic import Field
from typing import Optional
from datetime import datetime
import os
from pathlib import Path
from .final import Final


class FinalizeAndReport(Final):
    """
    Write a timestamped report into the ./reports folder and finalize.
    The report is saved with a timestamp and optional title.
    This tool also acts as a finalization tool (the agent will stop after execution).
    
    Use this when you want to both:
    1. Provide a final answer to the user
    2. Save detailed work to a timestamped report file
    
    Example: After analyzing a system, provide a summary in `content` 
    and save the full analysis in `report_body`.
    """
    content: str = Field(default="Report written successfully.", description="The final answer text")
    report_body: Optional[str] = Field(default=None, description="The body/content of the report to write to file. If not provided, defaults to the content field.")
    report_title: Optional[str] = Field(default=None, description="Optional title for the report file name")

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
            filename = f"{timestamp}_report.txt"
        
        filepath = reports_dir / filename
        
        # Write report content
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(report_body)
        except Exception as e:
            return f"Failed to write report: {e}"
        
        # Return final message (content inherited from Final)
        return self.content