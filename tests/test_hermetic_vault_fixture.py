"""Tests for the hermetic_vault fixture."""
import json
from pathlib import Path


def test_hermetic_vault_creates_factory_defaults(hermetic_vault):
    """Factory defaults must be present at system/factory_defaults.json."""
    vault_path = Path(hermetic_vault)
    factory_path = vault_path / "system" / "factory_defaults.json"
    assert factory_path.is_file(), f"Missing: {factory_path}"
    data = json.loads(factory_path.read_text())
    assert "version" in data
    assert "description" in data
    assert "config" in data
    assert isinstance(data["config"], dict)


def test_hermetic_vault_creates_user_defaults(hermetic_vault):
    """User defaults must be present at user/defaults.json."""
    vault_path = Path(hermetic_vault)
    user_defaults = vault_path / "user" / "defaults.json"
    assert user_defaults.is_file(), f"Missing: {user_defaults}"
    data = json.loads(user_defaults.read_text())
    assert isinstance(data, dict)


def test_hermetic_vault_creates_vault_compartments(hermetic_vault):
    """All 8 required spec directories must exist."""
    vault_path = Path(hermetic_vault)
    expected = {"credentials", "global", "logs", "sessions", "state", "system", "user", "workspaces"}
    actual = {d.name for d in vault_path.iterdir() if d.is_dir()}
    missing = expected - actual
    assert not missing, f"Missing directories: {missing}"


def test_hermetic_vault_is_isolated_from_real_home(hermetic_vault, tmp_path):
    """Verify the vault lives under tmp_path (not the actual ~)."""
    vault_path = Path(hermetic_vault)
    assert str(vault_path).startswith(str(tmp_path))


def test_hermetic_vault_no_workspace_defaults(hermetic_vault):
    """Workspace-specific defaults must NOT be written until workspace started."""
    vault_path = Path(hermetic_vault)
    workspaces_dir = vault_path / "workspaces"
    assert workspaces_dir.is_dir()
    workspace_ids = [d for d in workspaces_dir.iterdir() if d.is_dir()]
    assert len(workspace_ids) == 0, f"Expected empty workspaces dir, found: {workspace_ids}"
