"""Compliance tests: verify vault structure matches design spec."""

from pathlib import Path
import json
import pytest


def test_factory_defaults_present(hermetic_vault):
    """Factory defaults must be present at system/factory_defaults.json."""
    vault_path = Path(hermetic_vault)
    factory_path = vault_path / "system" / "factory_defaults.json"
    assert factory_path.is_file(), f"Missing: {factory_path}"
    data = json.loads(factory_path.read_text())
    # Must have the expected structure
    assert "version" in data
    assert "description" in data
    assert "config" in data
    assert isinstance(data["config"], dict)


def test_user_defaults_present(hermetic_vault):
    """User defaults must be present at user/defaults.json."""
    vault_path = Path(hermetic_vault)
    user_defaults = vault_path / "user" / "defaults.json"
    assert user_defaults.is_file(), f"Missing: {user_defaults}"
    data = json.loads(user_defaults.read_text())
    assert isinstance(data, dict)


def test_all_spec_directories_exist(hermetic_vault):
    """All 8 required spec directories must exist."""
    vault_path = Path(hermetic_vault)
    expected = {"credentials", "global", "logs", "sessions", "state", "system", "user", "workspaces"}
    actual = {d.name for d in vault_path.iterdir() if d.is_dir()}
    missing = expected - actual
    assert not missing, f"Missing directories: {missing}"


def test_no_workspace_defaults_written(hermetic_vault):
    """Workspace-specific defaults must NOT be written until workspace started."""
    vault_path = Path(hermetic_vault)
    workspaces_dir = vault_path / "workspaces"
    # The workspaces dir should exist but be empty
    assert workspaces_dir.is_dir()
    workspace_ids = [d for d in workspaces_dir.iterdir() if d.is_dir()]
    for ws_dir in workspace_ids:
        defaults = ws_dir / "defaults.json"
        assert not defaults.exists(), f"Unexpected defaults file: {defaults}"
