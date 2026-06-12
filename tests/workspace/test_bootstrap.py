"""
Tests for workspace bootstrap — ensure_workspace_dirs() creates all required
files idempotently.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from thoughtmachine.workspace_capabilities import (
    WorkspaceCapabilities,
    ensure_workspace_dirs,
    _user_dir,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_user_dir():
    """Temporarily redirect ``~/.thoughtmachine`` to a temp directory."""
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(Path, "home", return_value=Path(tmp)):
            yield Path(tmp)


# ── Bootstrap tests ──────────────────────────────────────────────────────────


class TestEnsureWorkspaceDirs:
    """Tests for ensure_workspace_dirs()."""

    def test_creates_all_directories(self, temp_user_dir):
        """The standard subdirectories are created."""
        ensure_workspace_dirs("test-ws")
        base = _user_dir() / "workspaces" / "test-ws"
        for sub in ("", "sessions", "state", "knowledge"):
            assert (base / sub).is_dir(), f"Missing directory: {base / sub}"

    def test_creates_capabilities_file(self, temp_user_dir):
        """A default capabilities.json is written."""
        ensure_workspace_dirs("test-ws")
        caps_path = _user_dir() / "workspaces" / "test-ws" / "capabilities.json"
        assert caps_path.exists()
        raw = json.loads(caps_path.read_text(encoding="utf-8"))
        assert raw["allow_network"] is True
        assert raw["allow_docker"] is True

    def test_creates_dockerfile(self, temp_user_dir):
        """Dockerfile is copied from resources/default_dockerfile.txt."""
        ensure_workspace_dirs("test-ws")
        dockerfile = _user_dir() / "workspaces" / "test-ws" / "Dockerfile"
        assert dockerfile.exists()
        content = dockerfile.read_text(encoding="utf-8")
        assert "FROM python" in content

    def test_creates_domain_allowlist(self, temp_user_dir):
        """domain_allowlist.json is an empty JSON array."""
        ensure_workspace_dirs("test-ws")
        path = _user_dir() / "workspaces" / "test-ws" / "domain_allowlist.json"
        assert path.exists()
        assert json.loads(path.read_text(encoding="utf-8")) == []

    def test_creates_workers_json(self, temp_user_dir):
        """workers.json is an empty JSON array."""
        ensure_workspace_dirs("test-ws")
        path = _user_dir() / "workspaces" / "test-ws" / "workers.json"
        assert path.exists()
        assert json.loads(path.read_text(encoding="utf-8")) == []

    def test_creates_mcp_servers_json(self, temp_user_dir):
        """mcp_servers.json is an empty JSON array."""
        ensure_workspace_dirs("test-ws")
        path = _user_dir() / "workspaces" / "test-ws" / "mcp_servers.json"
        assert path.exists()
        assert json.loads(path.read_text(encoding="utf-8")) == []

    def test_idempotent_does_not_overwrite_existing(self, temp_user_dir):
        """Calling ensure_workspace_dirs again does not overwrite files."""
        ensure_workspace_dirs("test-ws")

        # Modify the Dockerfile
        dockerfile = _user_dir() / "workspaces" / "test-ws" / "Dockerfile"
        dockerfile.write_text("# modified", encoding="utf-8")

        # Modify domain_allowlist
        dal = _user_dir() / "workspaces" / "test-ws" / "domain_allowlist.json"
        dal.write_text('["example.com"]', encoding="utf-8")

        # Call again
        ensure_workspace_dirs("test-ws")

        # Verify files are untouched
        assert dockerfile.read_text(encoding="utf-8") == "# modified"
        assert json.loads(dal.read_text(encoding="utf-8")) == ["example.com"]

    def test_returns_list_of_created_paths(self, temp_user_dir):
        """The return value lists all created directories and files."""
        created = ensure_workspace_dirs("test-ws")
        # At minimum: base dir, sessions, state, knowledge + 5 files
        base = _user_dir() / "workspaces" / "test-ws"
        assert str(base) in created
        assert str(base / "capabilities.json") in created
        assert str(base / "Dockerfile") in created
        assert str(base / "domain_allowlist.json") in created
        assert str(base / "workers.json") in created
        assert str(base / "mcp_servers.json") in created
