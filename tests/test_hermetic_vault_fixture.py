"""Tests for the hermetic_vault fixture."""
from pathlib import Path


def test_hermetic_vault_creates_config(hermetic_vault):
    vault_path = Path(hermetic_vault)
    # From MANIFEST.json: default_config.json -> agent_config.json (manifest)
    # Also system/factory_defaults.json -> created by vault defaults
    config_file = vault_path / "agent_config.json"
    assert config_file.exists(), f"Expected {config_file} to exist"
    factory_defaults = vault_path / "system" / "factory_defaults.json"
    assert factory_defaults.exists(), f"Expected {factory_defaults} to exist"


def test_hermetic_vault_creates_global_kb_dirs(hermetic_vault):
    vault_path = Path(hermetic_vault)
    assert (vault_path / "global" / "system").is_dir()
    assert (vault_path / "global" / "user").is_dir()


def test_hermetic_vault_creates_vault_compartments(hermetic_vault):
    vault_path = Path(hermetic_vault)
    assert (vault_path / "system").is_dir()
    assert (vault_path / "user").is_dir()
    assert (vault_path / "credentials").is_dir()
    assert (vault_path / "workspaces").is_dir()
    assert (vault_path / "global").is_dir()
    assert (vault_path / "state").is_dir()
    assert (vault_path / "logs").is_dir()


def test_hermetic_vault_is_isolated_from_real_home(hermetic_vault, tmp_path):
    # Path.home() is patched, so "real" vault == fixture vault during test.
    # Instead, verify the vault lives under tmp_path (not the actual ~).
    vault_path = Path(hermetic_vault)
    assert str(vault_path).startswith(str(tmp_path))
