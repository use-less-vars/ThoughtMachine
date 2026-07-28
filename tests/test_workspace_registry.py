"""
test_workspace_registry.py — Tests for WorkspaceRegistry path resolution.

The main bug this test guards against: on Windows, paths with different casing
(e.g. ``C:\\Users\\Foo`` vs ``c:\\users\\foo``) should be treated as equal.
``os.path.normcase()`` lowercases on Windows and is a no-op on POSIX, so we
mock it to simulate Windows behaviour and verify the fix.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from thoughtmachine.workspace_registry import (
    WorkspaceRegistry,
    _normalize_path,
)


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def registry() -> WorkspaceRegistry:
    """Return a WorkspaceRegistry backed by a temporary file."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        reg_path = Path(f.name)
    # Write empty registry
    reg_path.write_text("{}", encoding="utf-8")
    reg = WorkspaceRegistry(reg_path)
    yield reg
    # Cleanup
    if reg_path.exists():
        reg_path.unlink()


# ── _normalize_path ───────────────────────────────────────────────────────


class TestNormalizePath:
    def test_resolves_relative_to_absolute(self):
        """Parent-dir segments are resolved away."""
        result = _normalize_path("/tmp/../home")
        assert result == "/home"

    def test_strips_trailing_slash(self):
        result = _normalize_path("/home/user/")
        assert result == "/home/user"
        assert not result.endswith("/")

    def test_converts_backslashes_to_forward(self):
        """Even on POSIX, backslashes in input get converted."""
        result = _normalize_path("C:\\Users\\Foo")
        assert "\\" not in result

    def test_case_sensitive_on_posix(self):
        """On POSIX, os.path.normcase is a no-op, so casing is preserved."""
        upper = _normalize_path("/HOME/USER")
        lower = _normalize_path("/home/user")
        # On Linux these are different paths
        assert upper != lower

    def test_windows_case_insensitive(self):
        """When os.path.normcase lowercases (Windows), paths with differing
        casing should compare equal."""
        with patch.object(os.path, "normcase", side_effect=lambda p: p.lower()):
            upper = _normalize_path("/HOME/USER")
            lower = _normalize_path("/home/user")
            assert upper == lower


# ── resolve_by_root ───────────────────────────────────────────────────────


class TestResolveByRoot:
    def test_resolve_exact_match(self, registry: WorkspaceRegistry):
        registry.register_workspace("ws-1", "/tmp/project")
        entry = registry.resolve_by_root("/tmp/project")
        assert entry is not None
        assert entry.id == "ws-1"

    def test_resolve_with_trailing_slash(self, registry: WorkspaceRegistry):
        registry.register_workspace("ws-1", "/tmp/project")
        entry = registry.resolve_by_root("/tmp/project/")
        assert entry is not None
        assert entry.id == "ws-1"

    def test_resolve_with_parent_refs(self, registry: WorkspaceRegistry):
        registry.register_workspace("ws-1", "/tmp/project")
        entry = registry.resolve_by_root("/tmp/./project/../project")
        assert entry is not None
        assert entry.id == "ws-1"

    def test_resolve_nonexistent(self, registry: WorkspaceRegistry):
        registry.register_workspace("ws-1", "/tmp/project")
        entry = registry.resolve_by_root("/tmp/other")
        assert entry is None

    @patch.object(os.path, "normcase", side_effect=lambda p: p.lower())
    def test_resolve_windows_case_insensitive(self, registry: WorkspaceRegistry, _mock_normcase):
        """Simulate Windows: register with one casing, look up with different
        casing — must still match."""
        registry.register_workspace("ws-win", "/TMP/MYPROJECT")
        entry = registry.resolve_by_root("/tmp/myproject")
        assert entry is not None
        assert entry.id == "ws-win"

    @patch.object(os.path, "normcase", side_effect=lambda p: p.lower())
    def test_resolve_windows_case_insensitive_reverse(
        self, registry: WorkspaceRegistry, _mock_normcase
    ):
        """Register in lowercase, look up in uppercase."""
        registry.register_workspace("ws-win", "/tmp/myproject")
        entry = registry.resolve_by_root("/TMP/MYPROJECT")
        assert entry is not None
        assert entry.id == "ws-win"


# ── register_by_root (deduplication) ──────────────────────────────────────


class TestRegisterByRoot:
    def test_register_new(self, registry: WorkspaceRegistry):
        entry = registry.register_by_root("/tmp/new-project")
        assert entry.root_path == "/tmp/new-project"
        # Should have generated a human-readable ID
        assert entry.id and len(entry.id) > 0

    def test_register_existing_returns_same(self, registry: WorkspaceRegistry):
        entry1 = registry.register_by_root("/tmp/project")
        entry2 = registry.register_by_root("/tmp/project")
        assert entry2.id == entry1.id
        assert entry2.root_path == entry1.root_path

    @patch.object(os.path, "normcase", side_effect=lambda p: p.lower())
    def test_register_existing_different_casing(
        self, registry: WorkspaceRegistry
    ):
        """Simulate Windows: first register with uppercase, then try with
        lowercase — should return the existing entry, not create a new one."""
        entry1 = registry.register_by_root("/TMP/PROJECT")
        entry2 = registry.register_by_root("/tmp/project")
        assert entry2.id == entry1.id
        # The stored path should be case-normalized (lowercased on Windows)
        assert entry2.root_path == entry1.root_path

    def test_register_multiple_distinct(self, registry: WorkspaceRegistry):
        a = registry.register_by_root("/tmp/a")
        b = registry.register_by_root("/tmp/b")
        assert a.id != b.id


# ── register_workspace (storage normalisation) ────────────────────────────


class TestRegisterWorkspace:
    def test_stores_normalized_path(self, registry: WorkspaceRegistry):
        entry = registry.register_workspace("ws-1", "/tmp/./foo/../bar")
        # The stored path should be absolute, no ../ or ./
        assert "/./" not in entry.root_path
        assert "/../" not in entry.root_path

    @patch.object(os.path, "normcase", side_effect=lambda p: p.lower())
    def test_stores_lowercased_on_windows(self, registry: WorkspaceRegistry, _mock_normcase):
        entry = registry.register_workspace("ws-win", "/TMP/PROJECT")
        assert entry.root_path == "/tmp/project"


# ── resolve_workspace_id (from workspace_capabilities) ────────────────────


class TestResolveWorkspaceId:
    def test_legacy_config_json_match(self):
        """Test the config.json scanning path of resolve_workspace_id()."""
        from thoughtmachine.workspace_capabilities import resolve_workspace_id

        with tempfile.TemporaryDirectory() as tmpdir:
            # Pretend ~/.thoughtmachine by patching _user_dir
            ws_dir = Path(tmpdir) / ".thoughtmachine" / "workspaces" / "my-ws"
            ws_dir.mkdir(parents=True)
            config = ws_dir / "config.json"
            config.write_text(
                json.dumps({"root": "/tmp/project"}), encoding="utf-8"
            )

            with patch.object(
                Path, "home", return_value=Path(tmpdir)
            ):
                result = resolve_workspace_id("/tmp/project")
                assert result == "my-ws"

    def test_legacy_config_json_different_casing(self):
        """Simulate Windows: config.json has uppercase path, look up with
        lowercase — must still match."""
        from thoughtmachine.workspace_capabilities import resolve_workspace_id

        with tempfile.TemporaryDirectory() as tmpdir:
            ws_dir = Path(tmpdir) / ".thoughtmachine" / "workspaces" / "my-ws"
            ws_dir.mkdir(parents=True)
            config = ws_dir / "config.json"
            config.write_text(
                json.dumps({"root": "/TMP/PROJECT"}), encoding="utf-8"
            )

            with patch.object(
                Path, "home", return_value=Path(tmpdir)
            ):
                with patch.object(
                    os.path, "normcase", side_effect=lambda p: p.lower()
                ):
                    result = resolve_workspace_id("/tmp/project")
                    assert result == "my-ws"
