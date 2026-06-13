"""
Tests for the custom system prompt mechanism.

Covers:
- Migration from legacy ``system_prompt.txt`` to ``custom_system_prompt.txt``
- ``load_custom_system_prompt()`` behaviour
- Field-validator precedence (custom file > explicit > factory default)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

import agent.config.loader as loader_mod
from agent.config.models import AgentConfig


@pytest.fixture(autouse=True)
def _patch_loader_paths(monkeypatch, tmp_path: Path):
    """Point the loader's path constants to *tmp_path* instead of the real home."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(
        loader_mod, "CUSTOM_SYSTEM_PROMPT_PATH",
        str(fake_home / ".thoughtmachine" / "custom_system_prompt.txt"),
    )
    monkeypatch.setattr(
        loader_mod, "LEGACY_SYSTEM_PROMPT_PATH",
        str(fake_home / ".thoughtmachine" / "system_prompt.txt"),
    )
    return fake_home


# ── _migrate_legacy_system_prompt ──────────────────────────────────────────────


class TestMigrateLegacySystemPrompt:
    """Tests for ``_migrate_legacy_system_prompt()``."""

    def test_no_legacy_file_does_nothing(self, _patch_loader_paths):
        """When legacy ``system_prompt.txt`` does not exist, nothing happens."""
        leg = Path(loader_mod.LEGACY_SYSTEM_PROMPT_PATH)
        cust = Path(loader_mod.CUSTOM_SYSTEM_PROMPT_PATH)
        assert not leg.exists()
        assert not cust.exists()

        loader_mod._migrate_legacy_system_prompt()

        assert not cust.exists()

    def test_legacy_migrates_to_custom(self, _patch_loader_paths):
        """When legacy exists but custom does not, content is migrated."""
        leg = Path(loader_mod.LEGACY_SYSTEM_PROMPT_PATH)
        cust = Path(loader_mod.CUSTOM_SYSTEM_PROMPT_PATH)
        leg.parent.mkdir(parents=True, exist_ok=True)
        leg.write_text("You are a helpful assistant.\n", encoding="utf-8")

        loader_mod._migrate_legacy_system_prompt()

        assert cust.exists(), "custom_system_prompt.txt should have been created"
        content = cust.read_text(encoding="utf-8").strip()
        assert content == "You are a helpful assistant."
        assert not leg.exists(), "legacy file should have been removed"

    def test_both_exist_keeps_custom(self, _patch_loader_paths):
        """When both files exist, custom is kept, legacy is removed."""
        leg = Path(loader_mod.LEGACY_SYSTEM_PROMPT_PATH)
        cust = Path(loader_mod.CUSTOM_SYSTEM_PROMPT_PATH)
        leg.parent.mkdir(parents=True, exist_ok=True)
        leg.write_text("Old prompt", encoding="utf-8")
        cust.write_text("New prompt", encoding="utf-8")

        loader_mod._migrate_legacy_system_prompt()

        assert cust.exists()
        assert cust.read_text(encoding="utf-8").strip() == "New prompt"
        assert not leg.exists()

    def test_empty_legacy_does_not_create_custom(self, _patch_loader_paths):
        """Empty legacy file is removed but does not create a custom file."""
        leg = Path(loader_mod.LEGACY_SYSTEM_PROMPT_PATH)
        cust = Path(loader_mod.CUSTOM_SYSTEM_PROMPT_PATH)
        leg.parent.mkdir(parents=True, exist_ok=True)
        leg.write_text("   \n", encoding="utf-8")  # whitespace-only

        loader_mod._migrate_legacy_system_prompt()

        assert not leg.exists()
        assert not cust.exists()


# ── load_custom_system_prompt ──────────────────────────────────────────────────


class TestLoadCustomSystemPrompt:
    """Tests for ``load_custom_system_prompt()``."""

    def test_no_file_returns_none(self, _patch_loader_paths):
        """When ``custom_system_prompt.txt`` does not exist, return ``None``."""
        result = loader_mod.load_custom_system_prompt()
        assert result is None

    def test_existing_file_returns_content(self, _patch_loader_paths):
        """When the file exists, return its trimmed content."""
        cust = Path(loader_mod.CUSTOM_SYSTEM_PROMPT_PATH)
        cust.parent.mkdir(parents=True, exist_ok=True)
        cust.write_text("  Hello world!  \n", encoding="utf-8")

        result = loader_mod.load_custom_system_prompt()
        assert result == "Hello world!"

    def test_empty_file_returns_none(self, _patch_loader_paths):
        """Empty or whitespace-only file returns ``None``."""
        cust = Path(loader_mod.CUSTOM_SYSTEM_PROMPT_PATH)
        cust.parent.mkdir(parents=True, exist_ok=True)
        cust.write_text("   \n\n", encoding="utf-8")

        result = loader_mod.load_custom_system_prompt()
        assert result is None


# ── _migrate_system_prompt_in_config ───────────────────────────────────────────


class TestMigrateSystemPromptInConfig:
    """Tests for ``_migrate_system_prompt_in_config()``."""

    def test_strips_system_prompt_when_custom_file_exists(self, _patch_loader_paths):
        """When custom_system_prompt.txt exists, ``system_prompt`` is removed
        from the config dict."""
        # Create custom file
        cust = Path(loader_mod.CUSTOM_SYSTEM_PROMPT_PATH)
        cust.parent.mkdir(parents=True, exist_ok=True)
        cust.write_text("Custom prompt", encoding="utf-8")

        config: Dict[str, Any] = {
            "system_prompt": "This should be stripped",
            "model": "test-model",
        }
        result = loader_mod._migrate_system_prompt_in_config(config)
        assert "system_prompt" not in result
        assert result["model"] == "test-model"

    def test_keeps_system_prompt_when_no_custom_file(self, _patch_loader_paths):
        """When no custom file exists, ``system_prompt`` is preserved."""
        config: Dict[str, Any] = {
            "system_prompt": "Keep me",
            "model": "test-model",
        }
        result = loader_mod._migrate_system_prompt_in_config(config)
        assert result["system_prompt"] == "Keep me"

    def test_handles_config_without_system_prompt(self, _patch_loader_paths):
        """Config without ``system_prompt`` key is unchanged."""
        config: Dict[str, Any] = {"model": "test-model"}
        result = loader_mod._migrate_system_prompt_in_config(config)
        assert result == {"model": "test-model"}


# ── AgentConfig field_validator ───────────────────────────────────────────────


class TestAgentConfigSystemPromptValidator:
    """Tests that ``AgentConfig.system_prompt`` field validator uses the correct
    precedence: custom file > explicit value > factory default.
    """

    def test_custom_file_takes_precedence(self, _patch_loader_paths, monkeypatch):
        """When custom_system_prompt.txt exists and a non-empty value is passed,
        the custom file wins."""
        fake_home = _patch_loader_paths
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        # Write custom prompt
        custom_path = fake_home / ".thoughtmachine" / "custom_system_prompt.txt"
        custom_path.parent.mkdir(parents=True, exist_ok=True)
        custom_path.write_text("Custom file prompt", encoding="utf-8")

        cfg = AgentConfig(system_prompt="Explicit prompt")
        # Custom file should win over explicit value
        assert cfg.system_prompt == "Custom file prompt"

    def test_explicit_value_when_no_custom_file(self, _patch_loader_paths, monkeypatch):
        """When no custom file, the explicit value passed to the constructor is used."""
        fake_home = _patch_loader_paths
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        cfg = AgentConfig(system_prompt="My explicit prompt")
        assert cfg.system_prompt == "My explicit prompt"

    def test_empty_string_falls_to_factory(self, _patch_loader_paths, monkeypatch):
        """Empty string or None falls through to factory default when no custom file."""
        fake_home = _patch_loader_paths
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        cfg = AgentConfig(system_prompt="")
        prompt_path = Path(__file__).resolve().parent.parent / "resources" / "default_system_prompt.txt"
        expected = prompt_path.read_text(encoding="utf-8")
        # The validator returns the raw file content (with trailing newline)
        assert cfg.system_prompt == expected
