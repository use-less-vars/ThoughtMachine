"""
Tests for API key hygiene — ``api_key`` must never leak into serialised output.

Covers:
- ``AgentConfig.model_dump(exclude={'api_key'})`` excludes the key
- ``StateBridge.get_config()`` and ``save_config()`` exclude the key
- ``ConfigService`` output does not contain the key
- ``loader.save_config()`` never persists the key
- ``_config_to_dict()`` / ``model_dump(exclude={'api_key'})`` in server.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

from agent.config.models import AgentConfig
from agent.config.loader import save_config
from agent.presenter.state_bridge import StateBridge


# ── Helpers ──────────────────────────────────────────────────────────────────


def _check_dict_has_no_api_key(d: Dict[str, Any], label: str) -> None:
    """Assert *d* does not contain an ``api_key`` key at the top level."""
    assert "api_key" not in d, (
        f"api_key LEAK detected in {label}! "
        f"Keys present: {list(d.keys())}"
    )


# ── AgentConfig.model_dump() ────────────────────────────────────────────────


class TestAgentConfigSerialization:
    """``AgentConfig.model_dump()`` must exclude ``api_key`` when asked."""

    def test_model_dump_excludes_api_key(self):
        """model_dump(exclude={'api_key'}) strips api_key."""
        cfg = AgentConfig(api_key="sk-secret-12345")
        dumped = cfg.model_dump(exclude={'api_key'}, exclude_none=True)
        assert "api_key" not in dumped, (
            "api_key leaked through model_dump(exclude={'api_key'})"
        )

    def test_model_dump_default_excludes_api_key(self):
        """api_key has ``exclude=True`` in its Field definition."""
        cfg = AgentConfig(api_key="sk-secret-12345")
        # Without explicit exclude, api_key should still be excluded
        # because the Field has exclude=True for serialization.
        dumped = cfg.model_dump(exclude_none=True)
        assert "api_key" not in dumped, (
            "api_key leaked through model_dump() — Field(exclude=True) "
            "should have excluded it"
        )

    def test_model_dump_field_exclude_always_wins(self):
        """Pydantic V2's `Field(exclude=True)` always excludes api_key from
        ``model_dump()`` — even passing ``exclude=set()`` cannot override it.
        This is by design: the field-level exclude is the last line of defence.
        The api_key remains accessible as a direct attribute for internal use."""
        cfg = AgentConfig(api_key="sk-force-test")
        # model_dump never includes api_key regardless of exclude param
        dumped = cfg.model_dump(exclude_none=True)
        assert "api_key" not in dumped, (
            "Field(exclude=True) should keep api_key out of model_dump()"
        )
        dumped2 = cfg.model_dump(exclude=set(), exclude_none=True)
        assert "api_key" not in dumped2, (
            "Field(exclude=True) takes precedence over model_dump(exclude=set())"
        )
        # But api_key IS accessible as a direct attribute
        assert cfg.api_key == "sk-force-test", "Direct attribute access must work"


# ── StateBridge output ──────────────────────────────────────────────────────


class TestStateBridgeApiKeyHygiene:
    """``StateBridge`` methods must never leak ``api_key`` in their return values."""

    def test_get_config_excludes_api_key(self, monkeypatch, tmp_path):
        """``get_config()`` returns a dict without ``api_key``."""
        from agent.config import loader as loader_mod
        loader_mod._factory_config_cache = None

        overlay_path = tmp_path / "agent_config.json"
        bridge = StateBridge(config_path=str(overlay_path))
        _check_dict_has_no_api_key(bridge.get_config(), "StateBridge.get_config()")

    def test_save_config_overlay_excludes_api_key(self, monkeypatch, tmp_path):
        """The overlay file written by ``save_config()`` must not contain api_key."""
        from agent.config import loader as loader_mod
        loader_mod._factory_config_cache = None

        overlay_path = tmp_path / "agent_config.json"
        bridge = StateBridge(config_path=str(overlay_path))

        # Change a value so there's something to save
        bridge.current_config.temperature = 0.5
        bridge.save_config()

        saved = json.loads(overlay_path.read_text(encoding="utf-8"))
        _check_dict_has_no_api_key(saved, "save_config() overlay file")

    def test_reset_config_to_factory_excludes_api_key(self, monkeypatch, tmp_path):
        """``reset_config_to_factory()`` return value excludes api_key."""
        from agent.config import loader as loader_mod
        loader_mod._factory_config_cache = None

        overlay_path = tmp_path / "agent_config.json"
        bridge = StateBridge(config_path=str(overlay_path))
        result = bridge.reset_config_to_factory()
        _check_dict_has_no_api_key(result, "reset_config_to_factory() return")

    def test_update_config_excludes_api_key(self, monkeypatch, tmp_path):
        """``update_config()`` return value excludes api_key."""
        from agent.config import loader as loader_mod
        loader_mod._factory_config_cache = None

        overlay_path = tmp_path / "agent_config.json"
        bridge = StateBridge(config_path=str(overlay_path))
        result = bridge.update_config({"temperature": 0.3})
        _check_dict_has_no_api_key(result, "update_config() return")

    def test_load_config_excludes_api_key(self, monkeypatch, tmp_path):
        """``load_config()`` return value excludes api_key."""
        from agent.config import loader as loader_mod
        loader_mod._factory_config_cache = None

        overlay_path = tmp_path / "agent_config.json"
        # Write a minimal config first
        overlay_path.write_text(json.dumps({"temperature": 0.7}), encoding="utf-8")

        bridge = StateBridge(config_path=str(overlay_path))
        result = bridge.load_config()
        _check_dict_has_no_api_key(result, "load_config() return")


# ── loader.save_config() ────────────────────────────────────────────────────


class TestLoaderSaveConfigHygiene:
    """``loader.save_config()`` must never persist ``api_key`` to disk."""

    def test_save_config_excludes_api_key(self, tmp_path):
        """Direct call to ``save_config()`` with api_key in the dict must strip it."""
        config_file = tmp_path / "test_config.json"
        data = {
            "temperature": 0.5,
            "api_key": "sk-leaked",
            "model": "test-model",
        }
        save_config(data, str(config_file))

        saved = json.loads(config_file.read_text(encoding="utf-8"))
        _check_dict_has_no_api_key(saved, "loader.save_config()")

    def test_save_config_does_not_touch_api_key_in_source(self, tmp_path):
        """The source dict passed to save_config() should not be mutated."""
        config_file = tmp_path / "test_config.json"
        data = {
            "temperature": 0.5,
            "api_key": "sk-leaked",
        }
        save_config(data, str(config_file))
        assert "api_key" in data, (
            "save_config() mutated the source dict — api_key was removed from it"
        )


# ── ConfigService output ────────────────────────────────────────────────────


class TestConfigServiceHygiene:
    """``ConfigService`` output must never contain ``api_key``."""

    def test_create_agent_config_service_excludes_api_key(self, tmp_path):
        """``create_agent_config_service()`` default config excludes api_key."""
        from agent.config.service import create_agent_config_service

        config_path = str(tmp_path / "service_test.json")
        service = create_agent_config_service(config_path)
        all_config = service.get_all()
        _check_dict_has_no_api_key(all_config, "ConfigService.get_all()")


# ── server.py _config_to_dict ──────────────────────────────────────────────


class TestServerConfigToDictHygiene:
    """The helper used by server.py must exclude api_key."""

    def test_config_to_dict_excludes_api_key(self):
        """``_config_to_dict()`` equivalent excludes api_key."""
        cfg = AgentConfig(api_key="sk-server-secret")
        # Replicate the logic from server.py's _config_to_dict()
        dumped = cfg.model_dump(exclude={'api_key', 'stop_check'}, exclude_none=True)
        assert "api_key" not in dumped, (
            "server.py _config_to_dict() leaked api_key"
        )


# ── End-to-end guard — test the config file on disk ────────────────────────


class TestPersistedFileHygiene:
    """Integration guard: the ``agent_config.json`` file on disk must never
    contain ``api_key`` under any normal save flow."""

    def test_no_api_key_in_saved_overlay(self, monkeypatch, tmp_path):
        """Full save → reload cycle produces no api_key in the file."""
        from agent.config import loader as loader_mod
        loader_mod._factory_config_cache = None

        overlay_path = tmp_path / "agent_config.json"
        bridge = StateBridge(config_path=str(overlay_path))

        # Make several changes and save
        bridge.current_config.temperature = 0.3
        bridge.current_config.max_turns = 50
        bridge.save_config()

        # Read the file directly — it must have no api_key
        raw = overlay_path.read_text(encoding="utf-8")
        assert "api_key" not in raw, (
            "api_key found in raw JSON text of saved overlay! "
            "This is a data leak."
        )

        # Also verify parsing
        data = json.loads(raw)
        _check_dict_has_no_api_key(data, "persisted agent_config.json")
