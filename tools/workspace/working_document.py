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

    # create / append / read
    doc_id: Optional[str] = Field(
        default=None,
        description=(
            "Document ID (required for append, read actions; optional for create \u2014 "
            "when given, the document is stored under exactly this id)."
        ),
    )
    section: Optional[str] = Field(
        default=None,
        description="Section name (required for append; optional for read — omitting returns TOC).",
    )
    content: Optional[str] = Field(
        default=None,
        description=(
            "Content to append (required for append action). On create it is accepted "
            "only when exactly one section is named, and it becomes that section's text."
        ),
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

    # ── Path containment ───────────────────────────────────────────────────

    def _resolve_doc_path(self, doc_id: str, docs_dir: Path) -> Path:
        """Resolve ``<doc_id>.json`` strictly inside ``docs_dir``.

        ``doc_id`` is caller-supplied and is used as a path component, so it
        must never be able to address a file outside the working-docs
        directory (``../`` traversal, absolute paths, symlink escapes).

        Raises:
            ValueError: if the resolved document path escapes ``docs_dir``.
        """
        docs_root = docs_dir.resolve()
        candidate = (docs_dir / f"{doc_id}.json").resolve()
        if not candidate.is_relative_to(docs_root):
            raise ValueError(
                f"Invalid doc_id '{doc_id}': working documents live in "
                f"'{docs_dir}'; paths outside it are not accessible."
            )
        return candidate

    # ── Create ────────────────────────────────────────────────────────

    def _write_document(self, filepath: Path, document: dict) -> None:
        """Write ``document`` to ``filepath`` and confirm that it landed.

        A ``json.dump`` that raises nothing is not proof of a write, and this
        tool must never report success for a document that did not reach disk:
        the file is re-read and the stored content compared to what was sent.

        Raises:
            OSError: if the write did not persist.
        """
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(document, f, indent=2, ensure_ascii=False)
        with open(filepath, "r", encoding="utf-8") as f:
            stored = json.load(f)
        if stored != document:
            raise OSError(f"write to '{filepath}' did not persist: stored content differs")

    def _handle_create(self, docs_dir: Path) -> str:
        if not self.title:
            return "Error: 'title' is required for create action."

        section_names = list(self.sections or [])

        # ``content`` on create has exactly one home: the single named section.
        # With no section, or with several, there is no defined target, so the
        # text is REFUSED rather than dropped (which is what this path used to
        # do) or placed by a rule the caller was never told about.
        if self.content is not None and len(section_names) != 1:
            named = ", ".join(section_names) if section_names else "none"
            return (
                f"Error: 'content' needs exactly one target section, but "
                f"{len(section_names)} section(s) were named ({named}). Name "
                f"exactly one section to receive the content, or omit 'content' "
                f"and write the text with the append action."
            )

        if not section_names:
            return "Error: 'sections' is required for create action."

        # A caller-supplied id is honoured; it is never silently replaced.
        doc_id = self.doc_id or uuid4().hex[:12]
        filepath = self._resolve_doc_path(doc_id, docs_dir)
        if filepath.exists():
            existing_title = ""
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    existing_title = json.load(f).get("title", "")
            except (OSError, json.JSONDecodeError):
                pass
            named = f" (title '{existing_title}')" if existing_title else ""
            return (
                f"Error: Document '{doc_id}'{named} already exists at '{filepath}'. "
                f"Refusing to overwrite it: pass a different 'doc_id', or 'append' "
                f"to the existing document."
            )

        sections = {s: "" for s in section_names}
        if self.content is not None:
            # Exactly one section was named, guaranteed by the refusal above.
            sections[section_names[0]] = self.content

        now = datetime.now().isoformat()
        document = {
            "doc_id": doc_id,
            "title": self.title,
            "sections": sections,
            "created_at": now,
            "updated_at": now,
        }

        try:
            self._write_document(filepath, document)
        except (OSError, ValueError) as e:
            return f"Error: Document '{doc_id}' was not created: {e}"

        return json.dumps({"doc_id": doc_id, "path": str(filepath)}, ensure_ascii=False)

    # ── Append ────────────────────────────────────────────────────────

    def _handle_append(self, docs_dir: Path) -> str:
        if not self.doc_id:
            return "Error: 'doc_id' is required for append action."
        if not self.section:
            return "Error: 'section' is required for append action."
        if not self.content:
            return "Error: 'content' is required for append action."

        filepath = self._resolve_doc_path(self.doc_id, docs_dir)
        if not filepath.exists():
            return f"Error: Document '{self.doc_id}' not found."

        with open(filepath, "r", encoding="utf-8") as f:
            document = json.load(f)

        # Ensure section exists
        if self.section not in document["sections"]:
            document["sections"][self.section] = ""

        current = document["sections"][self.section]
        if not isinstance(current, str):
            # A legacy value left by the old create bug: a bare line count, not
            # text. Never coerce it (that would launder the damage) and never
            # destroy it by overwriting the section behind the caller's back.
            return (
                f"Error: Section '{self.section}' in document '{self.doc_id}' holds "
                f"an unreadable value ({current!r}), not text — refusing to append "
                f"to it."
            )

        # Build timestamped entry
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}] {self.content}"

        # Append (with newline separator if non-empty)
        document["sections"][self.section] = (
            current + "\n" + entry if current else entry
        )

        document["updated_at"] = datetime.now().isoformat()

        try:
            self._write_document(filepath, document)
        except (OSError, ValueError) as e:
            return (
                f"Error: append to section '{self.section}' in document "
                f"'{self.doc_id}' did not complete: {e}"
            )

        return f"Appended to section '{self.section}' in document '{self.doc_id}'."

    # ── Read ──────────────────────────────────────────────────────────

    def _handle_read(self, docs_dir: Path) -> str:
        if not self.doc_id:
            return "Error: 'doc_id' is required for read action."

        filepath = self._resolve_doc_path(self.doc_id, docs_dir)
        if not filepath.exists():
            return f"Error: Document '{self.doc_id}' not found."

        with open(filepath, "r", encoding="utf-8") as f:
            document = json.load(f)

        if self.section is None:
            # Return TOC
            toc = {
                "title": document["title"],
                "sections": {
                    name: self._section_summary(value)
                    for name, value in document["sections"].items()
                },
            }
            return json.dumps(toc, ensure_ascii=False, indent=2)

        if self.section not in document["sections"]:
            return f"Error: Section '{self.section}' not found in document '{self.doc_id}'."

        text = document["sections"][self.section]
        if not isinstance(text, str):
            # Report what is actually stored, raw value included. Coercing this
            # to text is how a line count got read as a body in the first place.
            return (
                f"Error: Section '{self.section}' in document '{self.doc_id}' is "
                f"unreadable: its stored value is {type(text).__name__} {text!r}, "
                f"not text. A section holding a number was written by an older "
                f"version of this tool, which stored a line count instead of the text."
            )
        if not text:
            return f"(empty section '{self.section}')"
        return text

    # ── Table-of-contents rendering ───────────────────────────────────

    @staticmethod
    def _section_summary(value: object) -> str:
        """Render one table-of-contents entry as TEXT.

        A bare integer here is indistinguishable from content to a reader of
        the JSON (``0`` looked like a body of zero lines, not like an unwritten
        section), so the entry says what it is: the line count plus the opening
        text, or the raw value when the stored value is not text at all.
        """
        if not isinstance(value, str):
            return f"unreadable value {value!r} (not text)"
        if not value:
            return "empty (0 lines)"
        lines = value.splitlines()
        return f"{len(lines)} line(s); starts: {lines[0][:80]}"

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
