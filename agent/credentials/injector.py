"""
Credential injection system for secure tool dispatch.

Provides ``{{credential:<key>}}`` placeholder resolution in tool arguments
using files stored in ``~/.thoughtmachine/credentials/<workspace_id>/``.

One file per credential key — plain text, no JSON wrapper.
"""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path


# ── Exceptions ──────────────────────────────────────────────────────────────


class CredentialError(Exception):
    """Raised when a credential operation fails (missing file, invalid key, etc.)."""
    pass


# ── Redacted string type ───────────────────────────────────────────────────


class Secret(str):
    """A ``str`` subclass that redacts its value from accidental exposure.

    The actual string value is preserved for programmatic use (``==``,
    ``len()``, etc.), but ``repr()``, ``str()``, and ``format()`` all
    return ``"***"`` to prevent secrets from appearing in logs or output.
    """

    def __repr__(self) -> str:
        return "***"

    def __str__(self) -> str:
        return "***"

    def __format__(self, format_spec: str) -> str:
        return "***"


# ── Injector ────────────────────────────────────────────────────────────────

_PLACEHOLDER_RE = re.compile(r"\{\{credential:(.+?)\}\}")


class CredentialInjector:
    """Resolves ``{{credential:<key>}}`` placeholders from vault credential files.

    Args:
        workspace_id: Identifies the subdirectory under
            ``~/.thoughtmachine/credentials/`` that holds credential files.
    """

    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id
        self.credentials_dir = Path.home() / ".thoughtmachine" / "credentials" / workspace_id

    # ── Public API ──────────────────────────────────────────────────────────

    def resolve(self, key: str) -> Secret:
        """Read a single credential file and return its contents as a ``Secret``.

        Args:
            key: A bare filename (no path separators, no traversal).

        Returns:
            ``Secret`` containing the file contents (trailing newline stripped).

        Raises:
            CredentialError: If the key is invalid, the file is missing,
                or a symlink-traversal attack is detected.
        """
        # 1. Validate the key — must be a single filename component
        self._validate_key(key)

        # 2. Construct the path
        cred_path = self.credentials_dir / key

        # 3. Symlink-traversal protection: resolve and verify prefix
        try:
            resolved = os.path.realpath(str(cred_path))
        except OSError:
            raise CredentialError(f"Credential key '{key}' not found")

        resolved_cred_dir = os.path.realpath(str(self.credentials_dir))
        if not resolved.startswith(resolved_cred_dir + os.sep) and resolved != resolved_cred_dir:
            raise CredentialError(f"Invalid credential key: '{key}' — path traversal detected")

        # 4. Check it exists and is a regular file
        path_obj = Path(resolved)
        if not path_obj.exists():
            raise CredentialError(f"Credential '{key}' not found at '{resolved}'")
        if not path_obj.is_file():
            raise CredentialError(f"Credential path '{resolved}' is not a regular file")

        # 5. Read the file, strip final trailing newline
        try:
            value = path_obj.read_text(encoding="utf-8").rstrip("\n")
        except OSError as exc:
            raise CredentialError(f"Failed to read credential '{key}': {exc}")

        return Secret(value)

    def inject(self, tool_args: dict) -> dict:
        """Replace ``{{credential:<key>}}`` placeholders in string values.

        Only top-level string values are processed (tool args are flat).

        Args:
            tool_args: Original tool arguments dict (not mutated).

        Returns:
            A new dict with placeholders resolved to ``Secret`` values.

        Raises:
            CredentialError: If any placeholder references a missing or
                invalid credential (fail-fast).
        """
        result = copy.deepcopy(tool_args)

        for key, value in result.items():
            if not isinstance(value, str):
                continue
            match = _PLACEHOLDER_RE.fullmatch(value)
            if match:
                cred_key = match.group(1)
                result[key] = self.resolve(cred_key)

        return result

    # ── Internal helpers ────────────────────────────────────────────────────

    def _validate_key(self, key: str) -> None:
        """Validate that *key* is a safe filename (no path traversal, no separators).

        Raises ``CredentialError`` for invalid keys.
        """
        if not key:
            raise CredentialError("Invalid credential key: empty key")

        # No null bytes
        if "\0" in key:
            raise CredentialError("Invalid credential key: contains null byte")

        # No path separators
        if "/" in key or "\\" in key:
            raise CredentialError("Invalid credential key: contains path separator")

        # No parent-directory traversal
        if ".." in key:
            raise CredentialError("Invalid credential key: contains '..'")

        # No home-directory reference
        if "~" in key:
            raise CredentialError("Invalid credential key: contains '~'")

        # Must be a relative single component
        if os.path.isabs(key):
            raise CredentialError("Invalid credential key: absolute path")
