"""
ReadFile — simple tool for reading files from the workspace.

This tool is designed for agent workers that need to read files
as part of a tool-use loop. It wraps basic file-reading operations
and requires ``filesystem:read`` permission.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, ClassVar, Literal, Optional

from pydantic import Field

from tools.base import ToolBase

logger = logging.getLogger(__name__)


class ReadFile(ToolBase):
    """Read the contents of a file from the workspace directory."""

    tool: Literal["ReadFile"] = "ReadFile"
    required_categories: ClassVar[list[str]] = ["filesystem:read"]

    file_path: str = Field(
        description="Path to the file, relative to the workspace root.",
    )

    max_chars: Optional[int] = Field(
        default=100_000,
        description="Maximum number of characters to return (default 100_000). "
                    "Set to 0 for no limit.",
    )

    skip_output_truncation: ClassVar[bool] = False

    def execute(self) -> str:
        try:
            # === Resolve workspace path from registries (primary) ===
            root = None
            if self.session_id:
                try:
                    from session.session_registry import SessionRegistry
                    from thoughtmachine.workspace_registry import WorkspaceRegistry
                    session_info = SessionRegistry.get_default().get(self.session_id)
                    ws_id = session_info.get("workspace_id") if session_info else None
                    if ws_id:
                        entry = WorkspaceRegistry.get_default().get_workspace(ws_id)
                        root = entry.root_path if entry else None
                except Exception:
                    pass

            # Fallback to deprecated AgentConfig.workspace_path
            if not root:
                root = getattr(self, 'workspace_path', None)
                if root:
                    logging.warning(
                        "ReadFile falling back to deprecated AgentConfig.workspace_path")

            if not root:
                return json.dumps({
                    "error": "No workspace path configured — cannot resolve file path.",
                })

            # Resolve the path and prevent directory traversal
            root_path = Path(root).resolve()
            target = (root_path / self.file_path).resolve()

            if not str(target).startswith(str(root_path) + os.sep) \
                    and str(target) != str(root_path):
                return json.dumps({
                    "error": f"Path '{self.file_path}' escapes the workspace directory.",
                })

            if not target.exists():
                return json.dumps({
                    "error": f"File not found: {self.file_path}",
                })

            if not target.is_file():
                return json.dumps({
                    "error": f"Not a file: {self.file_path}",
                })

            content = target.read_text(encoding="utf-8")

            if self.max_chars and self.max_chars > 0 and len(content) > self.max_chars:
                content = content[: self.max_chars] + "\n\n[... truncated at {} characters]".format(
                    self.max_chars
                )

            return json.dumps({
                "file_path": self.file_path,
                "size": len(content),
                "content": content,
            })

        except Exception as exc:
            logger.exception("ReadFile failed")
            return json.dumps({"error": str(exc)})
