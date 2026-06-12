# tools/workspace/edit_dockerfile.py
"""
EditDockerfile — append instructions to the workspace Dockerfile, or create one
from the default template.

Use cases
---------
- Add a system package to the Docker image (e.g., ``redis-tools``).
- Pin a Python dependency version.
- Set an environment variable that should be baked into the image.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional
from pydantic import Field

from tools.base import ToolBase

# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------
try:
    from thoughtmachine.workspace_capabilities import (
        resolve_workspace_id,
        _workspace_dir,
    )
    CAPABILITIES_AVAILABLE = True
except ImportError:
    CAPABILITIES_AVAILABLE = False
    resolve_workspace_id = None
    _workspace_dir = None


# ---------------------------------------------------------------------------
# Default template path (relative to project root)
# ---------------------------------------------------------------------------
_DEFAULT_TEMPLATE = "resources/default_dockerfile.txt"


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

class EditDockerfile(ToolBase):
    """Append instructions or create a Dockerfile from the default template."""

    tool: str = "EditDockerfile"
    required_categories: ClassVar[List[str]] = ["container:write"]

    instructions: str = Field(
        description="Dockerfile lines to append",
    )

    skip_output_truncation: ClassVar[bool] = True

    # ------------------------------------------------------------------
    def execute(self) -> str:
        try:
            ws_id = None
            if resolve_workspace_id and self.workspace_path:
                ws_id = resolve_workspace_id(self.workspace_path)

            if ws_id is None:
                return json.dumps({"error": "No active workspace"})

            # Validate instructions
            if not self.instructions or not self.instructions.strip():
                return json.dumps({"error": "instructions must not be empty"})

            # Determine Dockerfile path: _workspace_dir(ws_id) / "Dockerfile"
            dockerfile_path = _workspace_dir(ws_id) / "Dockerfile"

            # If file doesn't exist, create from template
            if not dockerfile_path.exists():
                self._create_from_template(dockerfile_path)

            # Read current content
            current_content = dockerfile_path.read_text(encoding="utf-8")

            # Append with timestamp comment
            timestamp = datetime.now().isoformat()
            append_text = f"\n# Added by agent via edit_dockerfile on {timestamp}\n{self.instructions}\n"

            new_content = current_content + append_text

            # Write back
            dockerfile_path.write_text(new_content, encoding="utf-8")

            # Return the full new content as plain string (not JSON)
            return new_content
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    # -- helpers ----------------------------------------------------

    def _create_from_template(self, dockerfile_path: Path) -> None:
        """Create a Dockerfile from the default template."""
        # Find template relative to this file's location
        template_path = (
            Path(__file__).resolve().parent.parent.parent / _DEFAULT_TEMPLATE
        )
        if not template_path.exists():
            # Fallback: try cwd
            template_path = Path.cwd() / _DEFAULT_TEMPLATE

        if template_path.exists():
            content = template_path.read_text(encoding="utf-8")
        else:
            # Ultimate fallback: write a minimal Dockerfile
            content = "FROM python:3.11-slim\n"

        dockerfile_path.parent.mkdir(parents=True, exist_ok=True)
        dockerfile_path.write_text(content, encoding="utf-8")
