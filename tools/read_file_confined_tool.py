"""ReadFileTool — read-only, root-confined text reader for the MAIN agent.

Design notes
------------
* Read-only by construction: this module only ever opens files in binary
  *read* mode (``"rb"``) and imports nothing that can write, move, delete,
  chmod or execute anything, and nothing that can reach the network.
* Confinement is to ``ToolBase._resolve_workspace_root()`` -- the validated
  ``worktree_root`` when configured, else the registered workspace root (the
  same root the other path-anchored tools use).
* Every rejection carries a distinct machine-checkable ``reason`` code, so a
  caller (or a test) can tell the escape vectors apart instead of matching on
  prose.
* Oversized files reuse the existing ``FilePreviewTool`` guard verbatim: the
  same ``max_file_size`` default (10 MB) and the same "File is too large
  (...)" wording.  No second size policy is introduced here.
* An out-of-bounds line range is REJECTED, never clamped -- matching
  ``FilePreviewTool`` / ``FileEditor`` ("Invalid range a-b (file has N
  lines)").
"""

import json
import os
from typing import ClassVar, Literal, Optional

from pydantic import Field

from tools.base import ToolBase


class ReadFileTool(ToolBase):
    """Read a UTF-8 text file confined to the workspace/worktree root."""

    tool: Literal["ReadFileTool"] = "ReadFileTool"

    # Read-only grain: never ``filesystem:write``.
    required_categories: ClassVar[list[str]] = ["filesystem:read"]

    # ── machine-checkable rejection reasons ────────────────────────────────
    REASON_WORKSPACE_UNRESOLVED: ClassVar[str] = "workspace_unresolved"
    REASON_INVALID_PATH: ClassVar[str] = "invalid_path"
    REASON_ABSOLUTE_PATH: ClassVar[str] = "absolute_path"
    REASON_PARENT_TRAVERSAL: ClassVar[str] = "parent_traversal"
    REASON_SYMLINK_ESCAPE: ClassVar[str] = "symlink_escape"
    REASON_PATH_BLOCKED: ClassVar[str] = "path_blocked"
    REASON_FILE_NOT_FOUND: ClassVar[str] = "file_not_found"
    REASON_NOT_A_FILE: ClassVar[str] = "not_a_file"
    REASON_FILE_TOO_LARGE: ClassVar[str] = "file_too_large"
    REASON_BINARY_FILE: ClassVar[str] = "binary_file"
    REASON_INVALID_LINE_RANGE: ClassVar[str] = "invalid_line_range"
    REASON_LINE_RANGE_OUT_OF_BOUNDS: ClassVar[str] = "line_range_out_of_bounds"
    REASON_READ_FAILED: ClassVar[str] = "read_failed"

    file_path: str = Field(
        description="Path to the file, relative to the workspace root "
                    "(absolute paths and '..' components are rejected).",
    )

    start_line: Optional[int] = Field(
        default=None,
        description="Optional 1-based first line to return (inclusive).",
    )

    end_line: Optional[int] = Field(
        default=None,
        description="Optional 1-based last line to return (inclusive). "
                    "Past EOF is rejected, not clamped.",
    )

    # Same field name / default / wording as FilePreviewTool's size guard.
    max_file_size: int = Field(
        default=10_000_000,
        description="Maximum file size to read (in bytes); 0 disables the limit.",
    )

    # ── helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _error(reason: str, message: str) -> str:
        """Uniform machine-checkable error envelope."""
        return json.dumps({"error": message, "reason": reason})

    @staticmethod
    def _within(path: str, root: str) -> bool:
        """Component-aware containment test (a bare prefix test is not enough)."""
        return path == root or path.startswith(root + os.sep)

    # ── tool entry point ───────────────────────────────────────────────────

    def execute(self) -> str:
        try:
            return self._read()
        except Exception as exc:  # never leak a traceback into a tool result
            return self._error(self.REASON_READ_FAILED, f"ReadFileTool failed: {exc}")

    def _read(self) -> str:
        try:
            root = self._resolve_workspace_root()
        except ValueError as exc:
            return self._error(
                self.REASON_WORKSPACE_UNRESOLVED,
                f"Workspace root could not be resolved: {exc}",
            )
        if not root:
            return self._error(
                self.REASON_WORKSPACE_UNRESOLVED,
                "No workspace root configured — cannot resolve a confined file path.",
            )
        root_real = os.path.realpath(root)
        if not os.path.isdir(root_real):
            return self._error(
                self.REASON_WORKSPACE_UNRESOLVED,
                f"Workspace root {root!r} is not a directory.",
            )

        raw = self.file_path.strip() if isinstance(self.file_path, str) else ""
        if not raw:
            return self._error(
                self.REASON_INVALID_PATH, "file_path must not be empty."
            )

        # (b) absolute paths — refused before any join, so an absolute path can
        # never be smuggled into the workspace namespace.
        if os.path.isabs(raw):
            return self._error(
                self.REASON_ABSOLUTE_PATH,
                f"Absolute paths are rejected: {self.file_path!r} — pass a path "
                f"relative to the workspace root {root!r}.",
            )

        # (a) lexical parent traversal — refused even when it would resolve back
        # inside the root ('sub/../file'), because only lexical inspection can
        # distinguish intent from an accidental escape.
        parts = [p for p in raw.replace("\\", "/").split("/") if p not in ("", ".")]
        if ".." in parts:
            return self._error(
                self.REASON_PARENT_TRAVERSAL,
                f"Path traversal rejected: {self.file_path!r} contains a '..' "
                f"component; paths must stay inside {root!r}.",
            )

        candidate = os.path.join(root_real, *parts) if parts else root_real

        # (c) realpath escape — a symlink inside the root may point outside it.
        resolved = os.path.realpath(candidate)
        if not self._within(resolved, root_real):
            return self._error(
                self.REASON_SYMLINK_ESCAPE,
                f"Path escapes the workspace root via a symlink: {self.file_path!r} "
                f"resolves to {resolved!r}, outside {root_real!r}.",
            )

        # Reuse the centralized tool-layer path policy (vault compartments and
        # the workspace sensitive-path block list), read mode only.
        try:
            self._validate_path(resolved)
        except Exception as exc:
            return self._error(self.REASON_PATH_BLOCKED, str(exc))

        # (d) missing file / directory instead of a file.
        if not os.path.exists(resolved):
            return self._error(
                self.REASON_FILE_NOT_FOUND, f"File not found: {self.file_path}"
            )
        if os.path.isdir(resolved):
            return self._error(
                self.REASON_NOT_A_FILE,
                f"Not a file: {self.file_path!r} is a directory.",
            )
        if not os.path.isfile(resolved):
            return self._error(
                self.REASON_NOT_A_FILE,
                f"Not a file: {self.file_path!r} is not a regular file.",
            )

        limit = self.max_file_size if (self.max_file_size or 0) > 0 else 0
        try:
            size = os.path.getsize(resolved)
        except OSError as exc:
            return self._error(
                self.REASON_READ_FAILED, f"Cannot stat {self.file_path!r}: {exc}"
            )
        if limit and size > limit:
            return self._error(
                self.REASON_FILE_TOO_LARGE,
                f"File is too large ({size:,} bytes > {limit:,} bytes limit).",
            )

        try:
            with open(resolved, "rb") as fh:
                data = fh.read(limit + 1) if limit else fh.read()
        except OSError as exc:
            return self._error(
                self.REASON_READ_FAILED, f"Cannot read {self.file_path!r}: {exc}"
            )
        if limit and len(data) > limit:
            return self._error(
                self.REASON_FILE_TOO_LARGE,
                f"File is too large ({len(data):,} bytes > {limit:,} bytes limit).",
            )

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            return self._error(
                self.REASON_BINARY_FILE,
                f"Not UTF-8 text (binary or undecodable file): {self.file_path!r} ({exc})",
            )

        lines = text.splitlines()
        total = len(lines)

        start, end = self.start_line, self.end_line
        if start is None and end is None:
            first, last = (1, total) if total else (0, 0)
        else:
            if start is None:
                start = 1
            if end is None:
                end = total
            if start < 1:
                return self._error(
                    self.REASON_INVALID_LINE_RANGE,
                    f"Invalid line range: start_line must be >= 1 (got {start}).",
                )
            if end < start:
                return self._error(
                    self.REASON_INVALID_LINE_RANGE,
                    f"Invalid line range {start}-{end}: end_line must be >= start_line.",
                )
            if end > total:
                # Rejected, not clamped (same policy as FilePreviewTool).
                return self._error(
                    self.REASON_LINE_RANGE_OUT_OF_BOUNDS,
                    f"Invalid range {start}-{end} (file has {total} lines)",
                )
            first, last = start, end

        content = "\n".join(lines[first - 1:last]) if total else ""
        return json.dumps({
            "file_path": self.file_path,
            "total_lines": total,
            "start_line": first,
            "end_line": last,
            "content": content,
        })
