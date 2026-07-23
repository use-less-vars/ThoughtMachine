"""WorkingDocument — persistent multi-section document tool.

Supports create, append, read, and list operations on JSON documents
stored in ``.thoughtmachine/working_docs/`` within the resolved workspace.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Optional
from uuid import uuid4

from pydantic import Field, field_validator
from typing_extensions import Literal

from tools.base import ToolBase

logger = logging.getLogger(__name__)


class WorkingDocument(ToolBase):
    """Create, append to, read, and list persistent working documents.

    Documents are JSON files stored in ``.thoughtmachine/working_docs/``
    within the resolved workspace.  Each document contains a flat dict of
    named sections (string values) to which content can be appended with
    timestamped entries.
    """

    # ── Class-level metadata ──────────────────────────────────────────
    skip_output_truncation: ClassVar[bool] = False

    @classmethod
    def get_required_categories(cls, params: dict | None = None) -> list[str]:
        """Return filesystem:write — this tool always reads and writes files."""
        return ["filesystem:write"]

    # ── Instance fields ───────────────────────────────────────────────
    tool: Literal["WorkingDocument"] = "WorkingDocument"

    action: str = Field(
        description="Operation to perform: 'create', 'append', 'read', or 'list'.",
    )

    # create
    title: Optional[str] = Field(
        default=None,
        description="Document title (required for create action).",
    )
    sections: Optional[list[str]] = Field(
        default=None,
        description="List of section names (required for create action).",
    )

    # append / read
    doc_id: Optional[str] = Field(
        default=None,
        description="Document ID (required for append, read actions).",
    )
    section: Optional[str] = Field(
        default=None,
        description="Section name (required for append; optional for read — omitting returns TOC).",
    )
    content: Optional[str] = Field(
        default=None,
        description="Content to append (required for append action).",
    )

    # ── Validators ────────────────────────────────────────────────────

    @field_validator("action")
    @classmethod
    def _validate_action(cls, v: str) -> str:
        allowed = {"create", "append", "read", "list"}
        if v not in allowed:
            raise ValueError(f"Invalid action '{v}'. Must be one of: {', '.join(sorted(allowed))}")
        return v

    # ── Execute ───────────────────────────────────────────────────────

    def execute(self) -> str:
        # Resolve workspace root
        ws_path = self._resolve_registry_workspace()
        if not ws_path:
            ws_path = "."
        base = Path(ws_path)
        docs_dir = base / ".thoughtmachine" / "working_docs"
        docs_dir.mkdir(parents=True, exist_ok=True)

        # Dispatch
        handler = {
            "create": self._handle_create,
            "append": self._handle_append,
            "read": self._handle_read,
            "list": self._handle_list,
        }.get(self.action)

        if handler is None:
            return f"Unknown action: {self.action}"

        try:
            result = handler(docs_dir)
            return self._truncate_output(result)
        except Exception as e:
            return self._truncate_output(f"WorkingDocument error: {e}")

    # ── Create ────────────────────────────────────────────────────────

    def _handle_create(self, docs_dir: Path) -> str:
        if not self.title:
            return "Error: 'title' is required for create action."
        if not self.sections:
            return "Error: 'sections' is required for create action."

        doc_id = uuid4().hex[:12]
        now = datetime.now().isoformat()

        document = {
            "doc_id": doc_id,
            "title": self.title,
            "sections": {s: "" for s in self.sections},
            "created_at": now,
            "updated_at": now,
        }

        filepath = docs_dir / f"{doc_id}.json"
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(document, f, indent=2, ensure_ascii=False)

        return json.dumps({"doc_id": doc_id, "path": str(filepath)}, ensure_ascii=False)

    # ── Append ────────────────────────────────────────────────────────

    def _handle_append(self, docs_dir: Path) -> str:
        if not self.doc_id:
            return "Error: 'doc_id' is required for append action."
        if not self.section:
            return "Error: 'section' is required for append action."
        if not self.content:
            return "Error: 'content' is required for append action."

        filepath = docs_dir / f"{self.doc_id}.json"
        if not filepath.exists():
            return f"Error: Document '{self.doc_id}' not found."

        with open(filepath, "r", encoding="utf-8") as f:
            document = json.load(f)

        # Ensure section exists
        if self.section not in document["sections"]:
            document["sections"][self.section] = ""

        # Build timestamped entry
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}] {self.content}"

        # Append (with newline separator if non-empty)
        current = document["sections"][self.section]
        if current:
            document["sections"][self.section] = current + "\n" + entry
        else:
            document["sections"][self.section] = entry

        document["updated_at"] = datetime.now().isoformat()

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(document, f, indent=2, ensure_ascii=False)

        return f"Appended to section '{self.section}' in document '{self.doc_id}'."

    # ── Read ──────────────────────────────────────────────────────────

    def _handle_read(self, docs_dir: Path) -> str:
        if not self.doc_id:
            return "Error: 'doc_id' is required for read action."

        filepath = docs_dir / f"{self.doc_id}.json"
        if not filepath.exists():
            return f"Error: Document '{self.doc_id}' not found."

        with open(filepath, "r", encoding="utf-8") as f:
            document = json.load(f)

        if self.section is None:
            # Return TOC
            toc = {
                "title": document["title"],
                "sections": {
                    name: len(value.splitlines()) if value else 0
                    for name, value in document["sections"].items()
                },
            }
            return json.dumps(toc, ensure_ascii=False, indent=2)
        else:
            if self.section not in document["sections"]:
                return f"Error: Section '{self.section}' not found in document '{self.doc_id}'."
            text = document["sections"][self.section]
            if not text:
                return f"(empty section '{self.section}')"
            return text

    # ── List ──────────────────────────────────────────────────────────

    def _handle_list(self, docs_dir: Path) -> str:
        files = sorted(docs_dir.glob("*.json"))
        if not files:
            return "[]"

        results = []
        for fp in files:
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    doc = json.load(f)
                results.append({
                    "doc_id": doc.get("doc_id", fp.stem),
                    "title": doc.get("title", fp.stem),
                    "updated_at": doc.get("updated_at", ""),
                })
            except (json.JSONDecodeError, OSError):
                # Skip corrupted files
                continue

        return json.dumps(results, ensure_ascii=False, indent=2)
