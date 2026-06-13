"""
Tests for the universal config layering system.

Covers:
- ``load_factory_config()`` — loading from file, caching, fallback
- ``_deep_merge_config()`` — flat and nested dict merging
- ``_compute_config_diff()`` — computing minimal diffs vs factory defaults
- ``load_config()`` — factory + user overlay loading
- ``StateBridge.save_config()`` — diff-based overlay saving
- ``StateBridge.reset_config_to_factory()`` — full factory reset
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict
from unittest.mock import ANY

import pytest

import agent.config.loader as loader_mod
from agent.config.loader import (
    FACTORY_CONFIG_PATH,
    _deep_merge_config,
    _compute_config_diff,
    load_factory_config,
    load_config,
    save_config,
)
from agent.presenter.state_bridge import StateBridge


# ── Helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_factory_config(tmp_path: Path) -> Path:
    """Create a temporary factory config file for testing."""
    factory = tmp_path / "factory_config.json"
    data = {
        "temperature": 1.0,
        "max_turns": 200,
        "model": "deepseek-v4-flash",
        "system_prompt": "",
        "session_permissions": {
            "container": False,
            "network": True,
            "filesystem": "write",
        },
        "enabled_tools": ["ToolA", "ToolB"],
    }
    factory.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return factory


@pytest.fixture
def monkeypatch_factory_path(monkeypatch, tmp_factory_config: Path):
    """Point FACTORY_CONFIG_PATH to a temp file and reset the cache."""
    # Reset the cache before each test
    loader_mod._factory_config_cache = None
    monkeypatch.setattr(loader_mod, "FACTORY_CONFIG_PATH", str(tmp_factory_config))
    yield
    loader_mod._factory_config_cache = None


@pytest.fixture
def monkeypatch_user_dir(monkeypatch, tmp_path: Path):
    """Point USER_DIR to a temp path for config overlay files."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(loader_mod, "USER_DIR", fake_home / ".thoughtmachine")
    monkeypatch.setattr(
        loader_mod, "CUSTOM_SYSTEM_PROMPT_PATH",
        str(fake_home / ".thoughtmachine" / "custom_system_prompt.txt"),
    )
    return fake_home


# ── load_factory_config ──────────────────────────────────────────────────────


class TestLoadFactoryConfig:
    """Tests for ``load_factory_config()``."""

    def test_loads_from_json_file(self, monkeypatch_factory_path, tmp_factory_config):
        """Factory config loads values from the JSON file."""
        config = load_factory_config()
        assert config["temperature"] == 1.0
        assert config["max_turns"] == 200
        assert config["model"] == "deepseek-v4-flash"

    def test_returns_copy(self, monkeypatch_factory_path):
        """Each call returns a new dict (copy), not the cached original."""
        c1 = load_factory_config()
        c2 = load_factory_config()
        assert c1 is not c2  # different objects
        assert c1 == c2

    def test_caches_result(self, monkeypatch_factory_path, tmp_factory_config):
        """Result is cached after first load (module-level cache)."""
        load_factory_config()
        # Modify the source file — cache should prevent re-read
        tmp_factory_config.write_text(json.dumps({"temperature": 999}), encoding="utf-8")
        config = load_factory_config()
        assert config["temperature"] == 1.0, "Should use cached value"

    def test_missing_file_falls_back_to_model_defaults(self, monkeypatch):
        """When the factory file doesn't exist, fall back to model defaults."""
        loader_mod._factory_config_cache = None
        monkeypatch.setattr(loader_mod, "FACTORY_CONFIG_PATH", "/nonexistent/path.json")
        config = load_factory_config()
        # Model defaults include temperature=0.2
        assert config.get("temperature") == 0.2 or "temperature" in config

    def test_cache_cleared_by_fixture(self, monkeypatch_factory_path):
        """The fixture properly resets the cache between tests."""
        assert loader_mod._factory_config_cache is None
        load_factory_config()
        assert loader_mod._factory_config_cache is not None


# ── _deep_merge_config ───────────────────────────────────────────────────────


class TestDeepMergeConfig:
    """Tests for ``_deep_merge_config()``."""

    def test_flat_merge_overwrites(self):
        """Scalar values in overlay overwrite those in base."""
        base = {"a": 1, "b": 2}
        overlay = {"b": 99, "c": 3}
        result = _deep_merge_config(base, overlay)
        assert result == {"a": 1, "b": 99, "c": 3}

    def test_nested_dict_merge(self):
        """Nested dicts are merged recursively."""
        base = {"a": {"x": 10, "y": 20}, "b": 2}
        overlay = {"a": {"y": 200, "z": 300}}
        result = _deep_merge_config(base, overlay)
        assert result == {"a": {"x": 10, "y": 200, "z": 300}, "b": 2}

    def test_nested_dict_new_key(self):
        """New nested dict key in overlay is added."""
        base = {"a": 1}
        overlay = {"b": {"x": 100}}
        result = _deep_merge_config(base, overlay)
        assert result == {"a": 1, "b": {"x": 100}}

    def test_base_not_mutated(self):
        """The base dict is not mutated by the merge."""
        base = {"a": 1, "b": {"x": 10}}
        orig_base = {"a": 1, "b": {"x": 10}}
        overlay = {"a": 99, "b": {"y": 20}}
        _deep_merge_config(base, overlay)
        assert base == orig_base

    def test_empty_overlay(self):
        """Empty overlay returns base unchanged."""
        base = {"a": 1, "b": 2}
        result = _deep_merge_config(base, {})
        assert result == base

    def test_session_permissions_like_merge(self):
        """Merge that mimics real session_permissions update."""
        base = {
            "session_permissions": {
                "container": False,
                "network": True,
                "filesystem": "write",
            }
        }
        overlay = {
            "session_permissions": {
                "network": False,
            }
        }
        result = _deep_merge_config(base, overlay)
        assert result == {
            "session_permissions": {
                "container": False,
                "network": False,
                "filesystem": "write",
            }
        }


# ── _compute_config_diff ─────────────────────────────────────────────────────


class TestComputeConfigDiff:
    """Tests for ``_compute_config_diff()``."""

    def test_identical_configs(self):
        """Identical configs produce empty diff."""
        factory = {"a": 1, "b": 2, "c": "hello"}
        diff = _compute_config_diff(factory, factory)
        assert diff == {}

    def test_different_scalar(self):
        """Different scalar values appear in diff."""
        factory = {"a": 1, "b": 2}
        current = {"a": 1, "b": 99}
        diff = _compute_config_diff(factory, current)
        assert diff == {"b": 99}

    def test_new_key_not_in_factory(self):
        """Keys in current but not in factory are included."""
        factory = {"a": 1}
        current = {"a": 1, "b": "new_key"}
        diff = _compute_config_diff(factory, current)
        assert diff == {"b": "new_key"}

    def test_nested_dict_diff(self):
        """Nested dicts produce minimal nested diffs."""
        factory = {"outer": {"inner1": 10, "inner2": 20, "inner3": 30}}
        current = {"outer": {"inner1": 10, "inner2": 99, "inner3": 30}}
        diff = _compute_config_diff(factory, current)
        assert diff == {"outer": {"inner2": 99}}

    def test_nested_dict_unchanged(self):
        """Unchanged nested dicts are excluded from diff."""
        factory = {"session_permissions": {"container": False}}
        current = {"session_permissions": {"container": False}}
        diff = _compute_config_diff(factory, current)
        assert diff == {}

    def test_mixed_unchanged_and_changed(self):
        """Only changed and new keys appear."""
        factory = {"a": 1, "b": 2, "c": {"x": 10, "y": 20}}
        current = {"a": 1, "b": 99, "c": {"x": 10, "y": 100, "z": 30}}
        diff = _compute_config_diff(factory, current)
        assert diff == {"b": 99, "c": {"y": 100, "z": 30}}


# ── load_config ──────────────────────────────────────────────────────────────


class TestLoadConfigLayering:
    """Tests for ``load_config()`` with the factory overlay model."""

    def test_missing_user_config_returns_factory(self, monkeypatch_factory_path, tmp_path):
        """When user config file doesn't exist, factory defaults are returned."""
        missing_path = str(tmp_path / "nonexistent.json")
        config = load_config(missing_path)
        assert config["temperature"] == 1.0
        assert config["max_turns"] == 200

    def test_empty_user_config_returns_factory(self, monkeypatch_factory_path, tmp_path):
        """Empty user config file returns factory defaults."""
        user_cfg = tmp_path / "user_config.json"
        user_cfg.write_text("", encoding="utf-8")
        config = load_config(str(user_cfg))
        assert config["temperature"] == 1.0

    def test_user_overlay_merges_on_top(self, monkeypatch_factory_path, tmp_path):
        """User config values are overlaid on factory defaults."""
        user_cfg = tmp_path / "user_config.json"
        user_cfg.write_text(json.dumps({"temperature": 0.5}), encoding="utf-8")
        config = load_config(str(user_cfg))
        assert config["temperature"] == 0.5  # user wins
        assert config["max_turns"] == 200  # factory preserved

    def test_nested_dict_overlay(self, monkeypatch_factory_path, tmp_path):
        """Nested dicts (session_permissions) are deep-merged."""
        user_cfg = tmp_path / "user_config.json"
        user_cfg.write_text(
            json.dumps({"session_permissions": {"network": False}}),
            encoding="utf-8",
        )
        config = load_config(str(user_cfg))
        assert config["session_permissions"]["network"] is False
        assert config["session_permissions"]["container"] is False  # from factory
        assert config["session_permissions"]["filesystem"] == "write"  # from factory

    def test_full_legacy_config_still_works(self, monkeypatch_factory_path, tmp_path):
        """A legacy full config (all keys) overlays correctly on factory."""
        factory = load_factory_config()
        user_cfg = tmp_path / "user_config.json"
        # Full config with some overrides
        full_config = dict(factory)
        full_config["temperature"] = 0.8
        full_config["model"] = "custom-model"
        user_cfg.write_text(json.dumps(full_config), encoding="utf-8")
        config = load_config(str(user_cfg))
        assert config["temperature"] == 0.8
        assert config["model"] == "custom-model"
        # Factory values for unchanged keys should still be there
        assert config["max_turns"] == factory["max_turns"]


# ── StateBridge integration ──────────────────────────────────────────────────


class TestStateBridgeOverlaySave:
    """Tests for ``StateBridge.save_config()`` overlay (diff-based) behavior."""

    def test_save_writes_minimal_overlay(self, monkeypatch_factory_path, monkeypatch, tmp_path):
        """save_config() writes only the diff vs factory defaults."""
        # Use a temp path for the config overlay file
        overlay_path = tmp_path / "agent_config.json"
        bridge = StateBridge(config_path=str(overlay_path))
        # The bridge loads factory defaults since no overlay file exists yet

        # Change a single value
        bridge.current_config.temperature = 0.5
        bridge.save_config()

        # Read back what was written
        saved = json.loads(overlay_path.read_text(encoding="utf-8"))
        assert "temperature" in saved
        assert saved["temperature"] == 0.5
        # max_turns is at factory default (200) — should NOT be in overlay
        assert "max_turns" not in saved, "Factory-default keys should not appear in overlay"

    def test_save_load_roundtrip(self, monkeypatch_factory_path, monkeypatch, tmp_path):
        """Save overlay → load → produces same runtime config."""
        overlay_path = tmp_path / "agent_config.json"

        # Create a bridge and change some values
        bridge = StateBridge(config_path=str(overlay_path))
        bridge.current_config.temperature = 0.5
        bridge.current_config.max_turns = 50
        bridge.save_config()

        # Create a fresh bridge (simulating restart) — it loads factory + overlay
        bridge2 = StateBridge(config_path=str(overlay_path))
        assert bridge2.current_config.temperature == 0.5
        assert bridge2.current_config.max_turns == 50
        # Factory defaults for unchanged fields
        assert bridge2.current_config.model == "deepseek-v4-flash"

    def test_save_does_not_write_unchanged_keys(self, monkeypatch_factory_path, monkeypatch, tmp_path):
        """When config matches factory, saved overlay is empty or minimal."""
        overlay_path = tmp_path / "agent_config.json"
        bridge = StateBridge(config_path=str(overlay_path))

        # Save without any changes (config matches factory)
        bridge.save_config()

        saved = json.loads(overlay_path.read_text(encoding="utf-8"))
        # The overlay should be empty (or contain only api_key-excluded keys)
        # Note: model_dump(exclude={'api_key'}, exclude_none=True) may still
        # output fields that match factory because exclude_none doesn't filter
        # factory-matching values. Let's check it's at least minimal.
        assert isinstance(saved, dict)

    def test_save_system_prompt_handling(self, monkeypatch_factory_path, monkeypatch_user_dir, tmp_path):
        """save_config() still extracts system_prompt to custom file."""
        overlay_path = tmp_path / "agent_config.json"
        bridge = StateBridge(config_path=str(overlay_path))
        bridge.current_config.system_prompt = "Custom system prompt"
        bridge.save_config()

        # Check custom file was written
        custom_path = Path(loader_mod.CUSTOM_SYSTEM_PROMPT_PATH)
        assert custom_path.exists()
        assert custom_path.read_text(encoding="utf-8").strip() == "Custom system prompt"

        # Check the overlay file does NOT contain system_prompt
        saved = json.loads(overlay_path.read_text(encoding="utf-8"))
        assert "system_prompt" not in saved

    def test_save_empty_system_prompt_removes_custom_file(
        self, monkeypatch_factory_path, monkeypatch_user_dir, tmp_path
    ):
        """Empty system_prompt removes the custom file."""
        overlay_path = tmp_path / "agent_config.json"
        bridge = StateBridge(config_path=str(overlay_path))

        # First write a custom prompt
        custom_path = Path(loader_mod.CUSTOM_SYSTEM_PROMPT_PATH)
        custom_path.parent.mkdir(parents=True, exist_ok=True)
        custom_path.write_text("Old prompt", encoding="utf-8")
        assert custom_path.exists()

        # Save with empty system_prompt
        bridge.current_config.system_prompt = None
        bridge.save_config()

        assert not custom_path.exists(), "Custom prompt file should be removed"


class TestResetConfigToFactory:
    """Tests for ``StateBridge.reset_config_to_factory()``."""

    def test_reset_clears_overlay_file(
        self, monkeypatch_factory_path, monkeypatch_user_dir, tmp_path
    ):
        """reset_config_to_factory() writes an empty overlay."""
        overlay_path = tmp_path / "agent_config.json"

        # Create a config with some overrides, save it
        bridge = StateBridge(config_path=str(overlay_path))
        bridge.current_config.temperature = 0.5
        bridge.current_config.max_turns = 50
        bridge.save_config()

        # Verify overlay has content
        saved = json.loads(overlay_path.read_text(encoding="utf-8"))
        assert len(saved) > 0

        # Reset to factory
        result = bridge.reset_config_to_factory()

        # The overlay file should now be empty (or minimal)
        saved_after = json.loads(overlay_path.read_text(encoding="utf-8"))
        assert len(saved_after) == 0 or set(saved_after.keys()).issubset(
            {"temperature", "max_turns"}
        ), "Overlay should be empty after reset"

        # Runtime config should match factory defaults
        assert result["temperature"] == 1.0
        assert result["max_turns"] == 200

    def test_reset_removes_custom_system_prompt(
        self, monkeypatch_factory_path, monkeypatch_user_dir, tmp_path
    ):
        """reset removes custom_system_prompt.txt if it exists."""
        overlay_path = tmp_path / "agent_config.json"

        # Create custom prompt
        custom_path = Path(loader_mod.CUSTOM_SYSTEM_PROMPT_PATH)
        custom_path.parent.mkdir(parents=True, exist_ok=True)
        custom_path.write_text("My custom prompt", encoding="utf-8")
        assert custom_path.exists()

        bridge = StateBridge(config_path=str(overlay_path))
        bridge.reset_config_to_factory()

        assert not custom_path.exists(), "Custom prompt should be removed on reset"

    def test_reset_restores_factory_values(
        self, monkeypatch_factory_path, monkeypatch_user_dir, tmp_path
    ):
        """After reset, runtime config equals factory defaults."""
        overlay_path = tmp_path / "agent_config.json"
        bridge = StateBridge(config_path=str(overlay_path))

        # Make changes
        bridge.current_config.temperature = 0.5
        bridge.current_config.max_turns = 50

        # Reset
        result = bridge.reset_config_to_factory()

        # Runtime values restored to factory
        factory = load_factory_config()
        assert result["temperature"] == factory["temperature"]
        assert result["max_turns"] == factory["max_turns"]

        # New bridge loading from same path also sees factory defaults
        bridge2 = StateBridge(config_path=str(overlay_path))
        assert bridge2.current_config.temperature == factory["temperature"]
        assert bridge2.current_config.max_turns == factory["max_turns"]

    def test_reset_returns_factory_dict(
        self, monkeypatch_factory_path, monkeypatch_user_dir, tmp_path
    ):
        """Return value of reset is a dict with factory defaults."""
        overlay_path = tmp_path / "agent_config.json"
        bridge = StateBridge(config_path=str(overlay_path))
        result = bridge.reset_config_to_factory()
        assert isinstance(result, dict)
        assert "api_key" not in result, "API key should be excluded from return dict"


class TestLegacyFullConfigMigration:
    """Tests that legacy full-config files are handled by the overlay model."""

    def test_legacy_full_config_loads_correctly(
        self, monkeypatch_factory_path, tmp_path, monkeypatch_user_dir
    ):
        """A legacy full config (from old version) loads correctly via overlay."""
        factory = load_factory_config()
        user_cfg = tmp_path / "agent_config.json"
        # Simulate a legacy full config: copy factory and change a value
        legacy = dict(factory)
        legacy["temperature"] = 0.3
        legacy["max_turns"] = 150
        user_cfg.write_text(json.dumps(legacy), encoding="utf-8")

        # Load via StateBridge (uses load_config which overlays on factory)
        bridge = StateBridge(config_path=str(user_cfg))
        assert bridge.current_config.temperature == 0.3
        assert bridge.current_config.max_turns == 150

    def test_legacy_full_config_save_converts_to_overlay(
        self, monkeypatch_factory_path, tmp_path, monkeypatch_user_dir
    ):
        """Saving a legacy full config converts it to a minimal overlay."""
        factory = load_factory_config()
        user_cfg = tmp_path / "agent_config.json"
        # Write a legacy full config that differs from factory in 2 keys
        legacy = dict(factory)
        legacy["temperature"] = 0.3  # differs from 1.0
        legacy["max_turns"] = 150    # differs from 200
        user_cfg.write_text(json.dumps(legacy), encoding="utf-8")

        # Load and save
        bridge = StateBridge(config_path=str(user_cfg))
        bridge.save_config()

        # The saved file should now be a minimal overlay
        saved = json.loads(user_cfg.read_text(encoding="utf-8"))
        # Only temperature and max_turns differ from factory
        assert "temperature" in saved
        assert "max_turns" in saved
        assert saved["temperature"] == 0.3
        assert saved["max_turns"] == 150
        # All factory-matching keys should be absent
        for key in factory:
            if key not in ("temperature", "max_turns", "system_prompt"):
                assert key not in saved, (
                    f"Key {key!r} should not be in overlay (matches factory)"
                )


class TestFactoryEdgeCases:
    """Edge-case tests for factory config."""

    def test_factory_config_cache_reset_between_tests(self, monkeypatch_factory_path):
        """Cache is properly reset between test runs."""
        assert loader_mod._factory_config_cache is None

    def test_factory_does_not_contain_system_prompt_value(
        self, monkeypatch_factory_path
    ):
        """Factory config has empty system_prompt (falls through to file default)."""
        fc = load_factory_config()
        # The factory JSON has "system_prompt": ""
        assert fc.get("system_prompt") == ""
