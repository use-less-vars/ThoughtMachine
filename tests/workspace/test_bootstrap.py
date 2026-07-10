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

    def test_creates_base_directory(self, temp_user_dir):
        """The workspace base directory is created."""
        ensure_workspace_dirs("test-ws")
        base = _user_dir() / "workspaces" / "test-ws"
        assert base.is_dir(), f"Missing directory: {base}"

    def test_no_stray_subdirectories_created(self, temp_user_dir):
        """No subdirectories (sessions, state, knowledge) are created inside the workspace config dir."""
        ensure_workspace_dirs("test-ws")
        base = _user_dir() / "workspaces" / "test-ws"
        # Only the five expected files should exist — no subdirectories
        expected_files = {
            "capabilities.json",
            "Dockerfile",
            "domain_allowlist.json",
            "workers.json",
            "mcp_servers.json",
        }
        actual = {p.name for p in base.iterdir()}
        # Every item in the directory should be one of the expected files
        for name in actual:
            assert name in expected_files, f"Unexpected item found: {name}"

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
        """workers.json contains the default template worker."""
        ensure_workspace_dirs("test-ws")
        path = _user_dir() / "workspaces" / "test-ws" / "workers.json"
        assert path.exists()
        workers = json.loads(path.read_text(encoding="utf-8"))
        # Must contain template worker, NOT echo
        names = {w["name"] for w in workers}
        assert "echo" not in names
        assert "default" in names
        assert len(workers) == 1

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
        """The return value lists the base dir and all five files."""
        created = ensure_workspace_dirs("test-ws")
        # At minimum: base dir + 5 files (no subdirectories)
        base = _user_dir() / "workspaces" / "test-ws"
        assert str(base) in created
        assert str(base / "capabilities.json") in created
        assert str(base / "Dockerfile") in created
        assert str(base / "domain_allowlist.json") in created
        assert str(base / "workers.json") in created
        assert str(base / "mcp_servers.json") in created
        # Ensure no subdirectories were created (sessions, state, knowledge)
        assert str(base / "sessions") not in created
        assert str(base / "state") not in created
        assert str(base / "knowledge") not in created

    def test_safeguard_warns_on_unexpected_item(self, temp_user_dir, caplog):
        """The safeguard logs warnings for unexpected items in the workspace dir."""
        import logging
        caplog.set_level(logging.WARNING)

        ensure_workspace_dirs("test-ws")
        base = _user_dir() / "workspaces" / "test-ws"

        # Create an unexpected subdirectory and file
        (base / "sessions").mkdir(exist_ok=True)
        (base / "container_state.json").write_text("{}", encoding="utf-8")

        # Call ensure_workspace_dirs again — safeguard should warn
        ensure_workspace_dirs("test-ws")

        # Check that both unexpected items were logged
        assert any("sessions" in record.message for record in caplog.records)
        assert any("container_state.json" in record.message for record in caplog.records)

    def test_safeguard_does_not_delete_unexpected_items(self, temp_user_dir, caplog):
        """The safeguard warns but does not delete unexpected items."""
        import logging
        caplog.set_level(logging.WARNING)

        ensure_workspace_dirs("test-ws")
        base = _user_dir() / "workspaces" / "test-ws"

        # Create an unexpected subdirectory
        (base / "sessions").mkdir(exist_ok=True)
        (base / "sessions" / "test.txt").write_text("data", encoding="utf-8")

        ensure_workspace_dirs("test-ws")

        # The unexpected item should still exist
        assert (base / "sessions").is_dir()
        assert (base / "sessions" / "test.txt").exists()
        # And we should have warned about it
        assert any("sessions" in record.message for record in caplog.records)

