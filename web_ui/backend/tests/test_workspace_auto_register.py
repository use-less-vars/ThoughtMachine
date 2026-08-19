"""
Tests for workspace auto-registration on new_session.

When the frontend opens a folder that has never been registered as a
workspace, server.py's ``new_session`` handler auto-registers it by:

1. Generating a new workspace ID (uuid hex)
2. Creating ``~/.thoughtmachine/workspaces/{id}/config.json`` with ``{"root": ...}``
3. Calling ``ensure_workspace_dirs()`` to bootstrap default files

These tests verify that the auto-registration logic behaves correctly
in isolation (without involving WebSockets).
"""

import json
import uuid
from pathlib import Path

import pytest

from thoughtmachine.workspace_capabilities import (
    ensure_workspace_dirs,
    _workspace_dir,
)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _simulate_auto_register(project_root: Path) -> str:
    """
    Replicate the auto-registration logic from server.py lines 1126-1152.

    Returns the newly created workspace_id.
    """
    new_ws_id = uuid.uuid4().hex
    ws_dir = _workspace_dir(new_ws_id)
    ws_dir.mkdir(parents=True, exist_ok=True)
    config_path = ws_dir / "config.json"
    config_path.write_text(
        json.dumps({"root": str(project_root)}, indent=2),
        encoding="utf-8",
    )
    ensure_workspace_dirs(new_ws_id)
    return new_ws_id


def _bootstrap_file_names() -> set:
    """Return the set of filenames that ensure_workspace_dirs should create."""
    return {
        "capabilities.json",
        "Dockerfile",
        "domain_allowlist.json",
        "workers.json",
        "mcp_servers.json",
    }


def _worker_names_in(workspace_id: str) -> set:
    """Return the set of worker names from a workspace's workers.json."""
    workers_path = _workspace_dir(workspace_id) / "workers.json"
    if not workers_path.exists():
        return set()
    data = json.loads(workers_path.read_text(encoding="utf-8"))
    return {w["name"] for w in data if isinstance(w, dict)}


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_home(monkeypatch, tmp_path: Path) -> Path:
    """Redirect Path.home() to a temporary directory."""
    fake_home_dir = tmp_path / "fake_home"
    fake_home_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home_dir)
    return fake_home_dir


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """Return a temporary project root directory."""
    root = tmp_path / "project"
    root.mkdir(parents=True, exist_ok=True)
    return root


# ── Tests: auto-registration creates config and bootstrap files ────────────────


class TestAutoRegisterCreatesConfigAndDirs:
    """Verify that auto-registration creates all expected files."""

    def test_creates_workspace_directory(self, fake_home, project_root):
        """The workspace directory should exist under ~/.thoughtmachine/workspaces/<id>/."""
        ws_id = _simulate_auto_register(project_root)
        ws_dir = _workspace_dir(ws_id)
        assert ws_dir.is_dir(), f"Workspace directory {ws_dir} does not exist"

    def test_creates_config_json_with_correct_root(self, fake_home, project_root):
        """config.json should contain the project root path."""
        ws_id = _simulate_auto_register(project_root)
        config_path = _workspace_dir(ws_id) / "config.json"
        assert config_path.is_file(), "config.json was not created"
        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert "root" in data, "config.json missing 'root' key"
        assert Path(data["root"]).resolve() == project_root.resolve(), \
            f"Expected root={project_root}, got {data['root']}"

    def test_creates_all_bootstrap_files(self, fake_home, project_root):
        """All 5 default workspace files should be created."""
        ws_id = _simulate_auto_register(project_root)
        ws_dir = _workspace_dir(ws_id)
        expected_files = _bootstrap_file_names()
        actual_files = {f.name for f in ws_dir.iterdir() if f.is_file()}
        # config.json is created by auto-register, rest by ensure_workspace_dirs
        expected_files.add("config.json")
        missing = expected_files - actual_files
        assert not missing, f"Missing bootstrap files: {missing}"

    def test_workers_json_contains_default_worker(self, fake_home, project_root):
        """workers.json should include the default template worker.

        NOTE: the worker template set was consolidated to a single
        general-purpose "default" worker (resources/worker_templates/default.json);
        the older coder/reviewer/researcher template trio no longer exists.
        """
        ws_id = _simulate_auto_register(project_root)
        worker_names = _worker_names_in(ws_id)
        assert "default" in worker_names, \
            f"Expected worker 'default' not found in workers.json; got {worker_names}"

    def test_capabilities_json_is_valid(self, fake_home, project_root):
        """capabilities.json should parse into a valid WorkspaceCapabilities dict."""
        ws_id = _simulate_auto_register(project_root)
        caps_path = _workspace_dir(ws_id) / "capabilities.json"
        data = json.loads(caps_path.read_text(encoding="utf-8"))
        # Check a few representative fields
        assert "allowed_tools" in data
        assert "allow_network" in data
        assert data["allow_network"] is True  # default is permissive

    def test_domain_allowlist_is_empty_array(self, fake_home, project_root):
        """domain_allowlist.json should be an empty JSON array."""
        ws_id = _simulate_auto_register(project_root)
        path = _workspace_dir(ws_id) / "domain_allowlist.json"
        assert json.loads(path.read_text(encoding="utf-8")) == []

    def test_mcp_servers_is_empty_array(self, fake_home, project_root):
        """mcp_servers.json should be an empty JSON array."""
        ws_id = _simulate_auto_register(project_root)
        path = _workspace_dir(ws_id) / "mcp_servers.json"
        assert json.loads(path.read_text(encoding="utf-8")) == []


# ── Tests: idempotency ─────────────────────────────────────────────────────────


class TestAutoRegisterIdempotent:
    """Running auto-registration logic twice should not cause errors or corruption."""

    def test_double_auto_register_succeeds(self, fake_home, project_root):
        """
        Calling _simulate_auto_register twice with the same project root
        should produce two DIFFERENT workspace IDs, both fully bootstrapped.
        (This is expected — each call creates a new workspace entry.)
        """
        ws_id_1 = _simulate_auto_register(project_root)
        ws_id_2 = _simulate_auto_register(project_root)

        # Different IDs, both valid
        assert ws_id_1 != ws_id_2, \
            "Each auto-register should generate a new unique ID"

        # Both should have config.json with the correct root
        for ws_id in (ws_id_1, ws_id_2):
            config_path = _workspace_dir(ws_id) / "config.json"
            data = json.loads(config_path.read_text(encoding="utf-8"))
            assert Path(data["root"]).resolve() == project_root.resolve()

        # Both should have all bootstrap files
        for ws_id in (ws_id_1, ws_id_2):
            ws_dir = _workspace_dir(ws_id)
            expected_files = _bootstrap_file_names() | {"config.json"}
            actual_files = {f.name for f in ws_dir.iterdir() if f.is_file()}
            assert expected_files == actual_files, \
                f"Workspace {ws_id} has unexpected file set: {actual_files}"

    def test_ensure_workspace_dirs_idempotent(self, fake_home, project_root):
        """
        Calling ensure_workspace_dirs() twice on the same workspace ID
        should return an empty list the second time (nothing new created).
        """
        ws_id = _simulate_auto_register(project_root)

        # Gather checksums before
        ws_dir = _workspace_dir(ws_id)
        before = {
            f.name: f.read_bytes()
            for f in ws_dir.iterdir()
            if f.is_file()
        }

        # Run ensure_workspace_dirs again
        second_result = ensure_workspace_dirs(ws_id)

        # Gather checksums after
        after = {
            f.name: f.read_bytes()
            for f in ws_dir.iterdir()
            if f.is_file()
        }

        assert second_result == [], \
            f"Second ensure_workspace_dirs should return [], got {second_result}"
        assert before == after, \
            "Files changed on second ensure_workspace_dirs call"


# ── Tests: independent workspaces ──────────────────────────────────────────────


class TestMultipleWorkspaces:
    """Multiple auto-registered workspaces should not interfere."""

    def test_distinct_ids_for_distinct_roots(self, fake_home, tmp_path):
        """Two different project roots should produce two different workspace IDs."""
        root_a = tmp_path / "project_alpha"
        root_b = tmp_path / "project_beta"
        root_a.mkdir(parents=True, exist_ok=True)
        root_b.mkdir(parents=True, exist_ok=True)

        ws_id_a = _simulate_auto_register(root_a)
        ws_id_b = _simulate_auto_register(root_b)

        assert ws_id_a != ws_id_b, "Different roots must get different workspace IDs"

    def test_each_has_own_config(self, fake_home, tmp_path):
        """Each workspace's config.json should reference its own root."""
        root_a = tmp_path / "project_alpha"
        root_b = tmp_path / "project_beta"
        root_a.mkdir(parents=True, exist_ok=True)
        root_b.mkdir(parents=True, exist_ok=True)

        ws_id_a = _simulate_auto_register(root_a)
        ws_id_b = _simulate_auto_register(root_b)

        config_a = json.loads(
            (_workspace_dir(ws_id_a) / "config.json").read_text(encoding="utf-8")
        )
        config_b = json.loads(
            (_workspace_dir(ws_id_b) / "config.json").read_text(encoding="utf-8")
        )

        assert Path(config_a["root"]).resolve() == root_a.resolve()
        assert Path(config_b["root"]).resolve() == root_b.resolve()
        assert config_a["root"] != config_b["root"]

    def test_both_have_full_bootstrap(self, fake_home, tmp_path):
        """Both workspaces should each have their own complete set of bootstrap files."""
        root_a = tmp_path / "project_alpha"
        root_b = tmp_path / "project_beta"
        root_a.mkdir(parents=True, exist_ok=True)
        root_b.mkdir(parents=True, exist_ok=True)

        ws_id_a = _simulate_auto_register(root_a)
        ws_id_b = _simulate_auto_register(root_b)

        expected = _bootstrap_file_names() | {"config.json"}

        for ws_id in (ws_id_a, ws_id_b):
            actual = {f.name for f in _workspace_dir(ws_id).iterdir() if f.is_file()}
            assert actual == expected, \
                f"Workspace {ws_id}: expected {expected}, got {actual}"

    def test_worker_templates_in_both(self, fake_home, tmp_path):
        """Both workspaces should have the default worker in their workers.json.

        NOTE: worker templates were consolidated to a single "default" worker;
        the older coder/reviewer/researcher trio no longer exists.
        """
        root_a = tmp_path / "project_alpha"
        root_b = tmp_path / "project_beta"
        root_a.mkdir(parents=True, exist_ok=True)
        root_b.mkdir(parents=True, exist_ok=True)

        ws_id_a = _simulate_auto_register(root_a)
        ws_id_b = _simulate_auto_register(root_b)

        expected_workers = {"default"}
        assert _worker_names_in(ws_id_a) == expected_workers
        assert _worker_names_in(ws_id_b) == expected_workers


# ── Tests: edge cases ──────────────────────────────────────────────────────────


class TestAutoRegisterEdgeCases:
    """Edge cases in the auto-registration logic."""

    def test_empty_project_root_string(self, fake_home, tmp_path):
        """An empty project root should still be stored in config.json."""
        empty_root = tmp_path / ""
        empty_root.mkdir(parents=True, exist_ok=True)
        ws_id = _simulate_auto_register(empty_root)
        config_path = _workspace_dir(ws_id) / "config.json"
        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert "root" in data

    def test_workspace_id_is_valid_hex(self, fake_home, project_root):
        """The generated workspace ID should be a valid 32-character hex string."""
        ws_id = _simulate_auto_register(project_root)
        assert len(ws_id) == 32, f"Expected 32-char hex ID, got {len(ws_id)} chars"
        int(ws_id, 16)  # will raise ValueError if not valid hex

    def test_config_json_has_no_extra_keys(self, fake_home, project_root):
        """config.json should contain exactly the 'root' key."""
        ws_id = _simulate_auto_register(project_root)
        config_path = _workspace_dir(ws_id) / "config.json"
        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert set(data.keys()) == {"root"}, f"Unexpected keys: {data.keys()}"

    def test_ensure_workspace_dirs_on_fresh_id_succeeds(self, fake_home):
        """Calling ensure_workspace_dirs on a non-existent workspace should succeed."""
        fresh_id = uuid.uuid4().hex
        created = ensure_workspace_dirs(fresh_id)
        assert len(created) > 0, "Expected at least one path to be created"
        ws_dir = _workspace_dir(fresh_id)
        assert ws_dir.is_dir()
        assert (ws_dir / "capabilities.json").is_file()

    def test_config_json_does_not_crash_on_special_chars_in_path(self, fake_home, tmp_path):
        """Project root paths with special characters should not cause errors."""
        special_root = tmp_path / "project with spaces and üñîçödé"
        special_root.mkdir(parents=True, exist_ok=True)
        ws_id = _simulate_auto_register(special_root)
        config_path = _workspace_dir(ws_id) / "config.json"
        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert Path(data["root"]).resolve() == special_root.resolve()
