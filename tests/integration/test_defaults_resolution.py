"""Integration tests for resolve_config_defaults."""

import json

import pytest

from agent.config.config_manager import resolve_config_defaults, save_config_defaults


class TestDefaultsResolution:
    """Verify layered defaults resolution with hermetic vault."""

    @pytest.fixture
    def hermetic_vault(self, tmp_path, monkeypatch):
        """Set up a temporary vault with layered defaults."""
        vault_root = tmp_path / ".thoughtmachine"
        (vault_root / "system").mkdir(parents=True)
        (vault_root / "user").mkdir(parents=True)
        (vault_root / "workspaces" / "ws-1").mkdir(parents=True)

        # Layer 1: factory defaults
        factory = {
            "version": "1",
            "description": "Test factory defaults",
            "config": {
                "provider_id": "openai",
                "temperature": 0.7,
                "max_turns": 50,
                "enabled_tools": [],
            },
        }
        (vault_root / "system" / "factory_defaults.json").write_text(
            json.dumps(factory, indent=2)
        )

        # Layer 2: user defaults (override temperature only)
        user = {
            "temperature": 0.5,
            "system_prompt": "You are a helpful assistant.",
        }
        (vault_root / "user" / "defaults.json").write_text(
            json.dumps(user, indent=2)
        )

        # Layer 3: workspace defaults (override provider)
        ws = {
            "provider_id": "anthropic",
        }
        (vault_root / "workspaces" / "ws-1" / "defaults.json").write_text(
            json.dumps(ws, indent=2)
        )

        monkeypatch.setattr(
            "agent.config.config_manager._vault_root",
            lambda: vault_root,
        )
        yield vault_root

    def test_layered_merge(self, hermetic_vault):
        result = resolve_config_defaults("ws-1")
        assert result["provider_id"] == "anthropic"  # workspace overrides
        assert result["temperature"] == 0.5  # user overrides
        assert result["max_turns"] == 50  # from factory
        assert result["system_prompt"] == "You are a helpful assistant."  # from user

    def test_missing_workspace_defaults(self, hermetic_vault):
        result = resolve_config_defaults("nonexistent-ws")
        # Should get factory + user, no workspace layer
        assert result["provider_id"] == "openai"  # from factory
        assert result["temperature"] == 0.5  # from user

    def test_empty_vault(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "agent.config.config_manager._vault_root",
            lambda: tmp_path / ".thoughtmachine",
        )
        result = resolve_config_defaults("ws-1")
        assert result == {}  # no files at all

    def test_deep_merge_preserves_factory_keys(self, hermetic_vault):
        result = resolve_config_defaults("ws-1")
        assert "enabled_tools" in result  # from factory
        assert result["enabled_tools"] == []  # list replaced per spec

    def test_save_and_resolve_roundtrip(self, hermetic_vault):
        # Save workspace defaults (full dict)
        saved_path = save_config_defaults(
            {"temperature": 0.3, "model": "gpt-4"},
            "ws-1",
        )
        assert saved_path.exists()
        result = resolve_config_defaults("ws-1")
        # Factory + user + workspace (newly saved)
        assert result["temperature"] == 0.3
        assert result["model"] == "gpt-4"
        assert result["provider_id"] == "openai"  # from factory (ws file no longer has this)
        assert result["system_prompt"] == "You are a helpful assistant."  # from user
