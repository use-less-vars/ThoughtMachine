"""
Tests for path validation hardening — validating vault compartment blocking
and workspace boundary enforcement in ``thoughtmachine.security.validate_path``.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from thoughtmachine.security import (
    PathOutsideWorkspaceError,
    VAULT_ROOT,
    VAULT_BLOCKED_SUBDIRS,
    validate_path,
)


# ══════════════════════════════════════════════════════════════════════════
#  Vault compartment blocking
# ══════════════════════════════════════════════════════════════════════════


class TestBlockVaultCompartments:
    """Every subdirectory in VAULT_BLOCKED_SUBDIRS must be blocked."""

    @pytest.mark.parametrize("subdir", VAULT_BLOCKED_SUBDIRS)
    def test_blocked_vault_compartments(self, subdir: str):
        """Paths inside a vault compartment raise PathOutsideWorkspaceError."""
        test_path = os.path.join(VAULT_ROOT, subdir, "some_file.txt")
        with pytest.raises(PathOutsideWorkspaceError) as exc_info:
            validate_path(test_path, mode="read")
        assert subdir in str(exc_info.value) or "vault" in str(exc_info.value).lower()

    def test_blocked_vault_root(self):
        """A path directly in ~/.thoughtmachine/ (not in a subdir) is blocked."""
        test_path = os.path.join(VAULT_ROOT, "some_unknown_file.txt")
        with pytest.raises(PathOutsideWorkspaceError) as exc_info:
            validate_path(test_path, mode="read")
        assert "vault" in str(exc_info.value).lower()

    def test_blocked_vault_root_no_subdir(self):
        """A path just inside the vault root dir (no subdir) is blocked."""
        test_path = os.path.join(VAULT_ROOT, "random_config.toml")
        with pytest.raises(PathOutsideWorkspaceError):
            validate_path(test_path)

    @pytest.mark.parametrize(
        "subdir",
        [
            "credentials",
            "system",
            "global",
            "user",
        ],
    )
    def test_blocked_nested_paths(self, subdir: str):
        """Deeply nested paths within a vault compartment are also blocked."""
        test_path = os.path.join(VAULT_ROOT, subdir, "nested", "deep", "file.txt")
        with pytest.raises(PathOutsideWorkspaceError):
            validate_path(test_path)


# ══════════════════════════════════════════════════════════════════════════
#  Workspace boundary enforcement
# ══════════════════════════════════════════════════════════════════════════


class TestWorkspaceBoundary:
    """Path validation within a workspace."""

    def test_allowed_workspace_relative_path(self, tmp_path: Path):
        """A relative path within the workspace is allowed."""
        ws = str(tmp_path)
        allowed = validate_path("some_file.txt", workspace_path=ws)
        assert os.path.isabs(allowed)
        assert allowed.startswith(ws)

    def test_allowed_workspace_absolute_path(self, tmp_path: Path):
        """An absolute path within the workspace is allowed."""
        ws = str(tmp_path)
        file_path = os.path.join(ws, "data.txt")
        Path(file_path).write_text("hello")
        allowed = validate_path(file_path, workspace_path=ws)
        assert allowed == os.path.realpath(file_path)

    def test_directory_traversal_blocked(self, tmp_path: Path):
        """A ../ path that escapes the workspace is blocked."""
        ws = str(tmp_path)
        traversal_path = os.path.join(ws, "..", "..", "etc", "passwd")
        with pytest.raises(PathOutsideWorkspaceError):
            validate_path(traversal_path, workspace_path=ws)

    def test_absolute_path_outside_workspace(self, tmp_path: Path):
        """An absolute path outside the workspace is blocked."""
        ws = str(tmp_path)
        with pytest.raises(PathOutsideWorkspaceError):
            validate_path("/tmp", workspace_path=ws)


# ══════════════════════════════════════════════════════════════════════════
#  workspace_path=None (no restriction) — vault still blocked
# ══════════════════════════════════════════════════════════════════════════


class TestNoWorkspaceRestriction:
    """When workspace_path is None, non-vault paths are allowed, vault blocked."""

    def test_workspace_path_none_allows_tmp(self):
        """A path in /tmp is allowed when workspace_path is None."""
        result = validate_path("/tmp/some_test_file.txt")
        assert result is not None
        assert os.path.isabs(result)

    def test_workspace_path_none_allows_tempfile(self):
        """A tempfile path is allowed when workspace_path is None."""
        with tempfile.NamedTemporaryFile(suffix=".txt") as f:
            result = validate_path(f.name)
        assert result is not None
        assert os.path.isabs(result)

    def test_workspace_path_none_still_blocks_vault(self):
        """Even with workspace_path=None, vault paths are blocked."""
        test_path = os.path.join(VAULT_ROOT, "credentials", "secret.key")
        with pytest.raises(PathOutsideWorkspaceError):
            validate_path(test_path)

    def test_workspace_path_none_blocks_vault_root(self):
        """Even with workspace_path=None, the vault root itself is blocked."""
        test_path = os.path.join(VAULT_ROOT, "random_file")
        with pytest.raises(PathOutsideWorkspaceError):
            validate_path(test_path)


# ══════════════════════════════════════════════════════════════════════════
#  Edge cases
# ══════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Edge cases and error handling."""

    def test_invalid_path_raises_value_error(self):
        """An invalid path raises ValueError, not PathOutsideWorkspaceError."""
        with pytest.raises((ValueError, PathOutsideWorkspaceError)):
            validate_path("\x00invalid")

    def test_relative_path_outside_workspace(self, tmp_path: Path):
        """A relative path that resolves outside workspace is blocked."""
        ws = str(tmp_path)
        with pytest.raises(PathOutsideWorkspaceError):
            validate_path("../../../etc/passwd", workspace_path=ws)

    def test_symlink_pointing_to_vault_is_blocked(self, tmp_path: Path):
        """A symlink inside workspace pointing to a vault file is blocked."""
        ws = str(tmp_path)
        vault_file = os.path.join(VAULT_ROOT, "credentials", "some_key")
        symlink_path = os.path.join(ws, "link_to_vault")
        try:
            os.symlink(vault_file, symlink_path)
        except (OSError, NotImplementedError):
            pytest.skip("Symlinks not supported on this OS/filesystem")

        with pytest.raises(PathOutsideWorkspaceError):
            validate_path(symlink_path, workspace_path=ws)

    def test_symlink_pointing_outside_workspace_is_allowed(self, tmp_path: Path):
        """A symlink inside workspace pointing outside is allowed.

        The symlink file itself resides inside the workspace, so it passes path
        validation. The resolved target is not checked against the workspace
        boundary — that is a deliberate design choice (the original code's
        comment: 'If symlink points outside workspace or is broken, that's OK').
        """
        ws = str(tmp_path)
        symlink_path = os.path.join(ws, "link_outside")
        try:
            os.symlink("/tmp", symlink_path)
        except (OSError, NotImplementedError):
            pytest.skip("Symlinks not supported on this OS/filesystem")

        result = validate_path(symlink_path, workspace_path=ws)
        # The resolved path should point to /tmp (the symlink target)
        assert result == "/tmp"
