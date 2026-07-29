"""Tests for the hermetic_vault fixture."""
from pathlib import Path


def test_hermetic_vault_creates_config(hermetic_vault):
    vault_path = Path(hermetic_vault)
    # From MANIFEST.json: default_config.json -> agent_config.json
    config_file = vault_path / "agent_config.json"
    assert config_file.exists(), f"Expected {config_file} to exist"


def test_hermetic_vault_creates_knowledge_dirs(hermetic_vault):
    vault_path = Path(hermetic_vault)
    assert (vault_path / "knowledge" / "system").is_dir()
    assert (vault_path / "knowledge" / "user").is_dir()


def test_hermetic_vault_creates_sessions_dir(hermetic_vault):
    vault_path = Path(hermetic_vault)
    assert (vault_path / "sessions").is_dir()


def test_hermetic_vault_is_isolated_from_real_home(hermetic_vault, tmp_path):
    # Path.home() is patched, so "real" vault == fixture vault during test.
    # Instead, verify the vault lives under tmp_path (not the actual ~).
    vault_path = Path(hermetic_vault)
    assert str(vault_path).startswith(str(tmp_path))
