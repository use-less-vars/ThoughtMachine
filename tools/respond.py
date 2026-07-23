"""Unified agent-to-user response tool. Replaces Final, FinalReport, and RequestUserInteraction."""

import logging
from typing import ClassVar, List, Literal, Optional
from pydantic import Field
from .base import ToolBase


class Respond(ToolBase):
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
    status: Optional[Literal["final", "progress", "timeout"]] = Field(
        None,
        description="'final' = task complete; 'progress' = partial update, more to come; 'timeout' = system-generated on force-stop (not set by worker)"
    )
    confidence: Optional[Literal["high", "medium", "low"]] = Field(
        None,
        description="Worker's confidence in the response"
    )
    meta: Optional[dict] = Field(
        None,
        description="Additional metadata: struggles, needs_direction, blocked_by, remaining_work"
    )

    @classmethod
    def get_required_categories(cls, params: dict | None = None) -> list[str]:
        """Return filesystem:write only when report_body is provided (requires file writing)."""
        if params and params.get("report_body"):
            return ["filesystem:write"]
        return []

    def execute(self) -> str:
        """Execute the Respond tool."""
        if self.report_body and self.report_title:
            try:
                from datetime import datetime
                from pathlib import Path

                # === Resolve workspace path from registries (primary) ===
                workspace_path = None
                if self.session_id:
                    try:
                        from session.session_registry import SessionRegistry
                        from thoughtmachine.workspace_registry import WorkspaceRegistry
                        session_info = SessionRegistry.get_default().get(self.session_id)
                        ws_id = session_info.get("workspace_id") if session_info else None
                        if ws_id:
                            entry = WorkspaceRegistry.get_default().get_workspace(ws_id)
                            workspace_path = entry.root_path if entry else None
                    except Exception:
                        pass

                # Fallback to deprecated AgentConfig.workspace_path
                if not workspace_path:
                    workspace_path = getattr(self, 'workspace_path', None)
                    if workspace_path:
                        logging.warning(
                            "Respond falling back to deprecated AgentConfig.workspace_path")

                base = Path(workspace_path) if workspace_path else Path(".")
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
