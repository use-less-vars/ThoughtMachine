"""Unified agent-to-user response tool. Replaces Final, FinalReport, and RequestUserInteraction."""

from typing import ClassVar, List, Literal, Optional
from pydantic import Field
from .base import ToolBase


class Respond(ToolBase):
    required_categories: ClassVar[List[str]] = ["filesystem:write"]
    """Unified agent-to-user response tool. Replaces Final, FinalReport, and RequestUserInteraction.

    The `content` field MUST contain the complete message to display to the user — this is
    the agent's full output for this turn (both answer and question variants).
    The optional `report_body` field writes a downloadable report file in addition to the
    `content` shown in chat — use this for detailed reports, analysis, or summaries that
    the user can download while `content` provides the concise or conversational version.
    """
    tool: Literal["Respond"] = "Respond"
    skip_output_truncation: ClassVar[bool] = True

    content: str = Field(
        ...,
        description="The complete message to display to the user (answer or question)"
    )
    response_type: Literal["answer", "question"] = Field(
        "answer",
        description="'answer' = final response, no reply needed; 'question' = wait for user input"
    )
    report_body: Optional[str] = Field(
        None,
        description="Optional full report to write as a file (user can download)"
    )
    report_title: Optional[str] = Field(
        None,
        description="Title for the report file (only used if report_body is provided)"
    )

    def execute(self) -> str:
        """Execute the Respond tool."""
        if self.report_body and self.report_title:
            try:
                from datetime import datetime
                from pathlib import Path

                base = Path(self.workspace_path) if self.workspace_path else Path(".")
                reports_dir = base / "reports"
                reports_dir.mkdir(parents=True, exist_ok=True)

                timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

                # Sanitize title for filename
                sanitized = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in self.report_title)
                sanitized = sanitized.replace(' ', '_')
                if len(sanitized) > 50:
                    sanitized = sanitized[:50]
                filename = f"{timestamp}_{sanitized}.md"

                filepath = reports_dir / filename
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(self.report_body)
            except Exception as e:
                return f"{self.content}\n\n[Note: Failed to write report file: {e}]"

        return self.content
