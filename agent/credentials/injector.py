"""
Credential injection system for secure tool dispatch.

Provides ``{{credential:<key>}}`` placeholder resolution in tool arguments
using files stored in ``<vault_root>/credentials/<workspace_id>/`` and, failing
that, ``<vault_root>/credentials/global/``.

One file per credential key — plain text, no JSON wrapper.
"""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Optional

from thoughtmachine.vault import vault_root


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
            ``<vault_root>/credentials/`` that holds credential files.
    """

    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id
        # One function owns "where is the vault" (design Decision 2, Inv.6):
        # both the writer and this reader call ``vault_root()``.
        self.credentials_dir = vault_root() / "credentials" / workspace_id
        self.global_credentials_dir = vault_root() / "credentials" / "global"

    # ── Public API ──────────────────────────────────────────────────────────

    def resolve(self, key: str) -> Secret:
        """Read a single credential file and return its contents as a ``Secret``.

        Resolution order (design Decision 2): ``credentials/<workspace_id>/<key>``
        first, then ``credentials/global/<key>``.  The workspace scope is the
        binding; the global scope is the vault-wide library.

        Args:
            key: A bare filename (no path separators, no traversal).

        Returns:
            ``Secret`` containing the file contents (trailing newline stripped).

        Raises:
            CredentialError: If the key is invalid, the file is missing from both
                scopes, or a symlink-traversal attack is detected.
        """
        # 1. Validate the key — must be a single filename component
        self._validate_key(key)

        # 2. Workspace scope first, then the global scope
        for directory in (self.credentials_dir, self.global_credentials_dir):
            value = self._read_scoped_credential(directory, key)
            if value is not None:
                return Secret(value)

        raise CredentialError(f"Credential '{key}' not found")

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

    def _read_scoped_credential(self, directory: Path, key: str) -> Optional[str]:
        """Read *key* from *directory*; ``None`` when it is absent there.

        Symlink-traversal protection is per-scope: a resolved path that escapes
        *directory* is refused outright, never silently skipped in favour of the
        next scope.

        Raises:
            CredentialError: If a symlink-traversal attack is detected, or the
                path exists but is not a regular file.
        """
        try:
            resolved = os.path.realpath(str(directory / key))
        except OSError:
            return None

        resolved_dir = os.path.realpath(str(directory))
        if not resolved.startswith(resolved_dir + os.sep) and resolved != resolved_dir:
            raise CredentialError(f"Invalid credential key: '{key}' — path traversal detected")

        path_obj = Path(resolved)
        if not path_obj.exists():
            return None
        if not path_obj.is_file():
            raise CredentialError(f"Credential file for '{key}' is not a regular file")

        try:
            return path_obj.read_text(encoding="utf-8").rstrip("\n")
        except OSError as exc:
            raise CredentialError(f"Failed to read credential '{key}': {exc}")

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
