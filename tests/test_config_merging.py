"""
Unit tests for config merging logic across the agent config pipeline.

Tests the key merge/resolve functions in isolation with dummy data
(no real API keys or provider files).  Covers:

1. ``deep_merge`` (agent.utils) — recursive dict merge
2. ``ProviderManager.resolve_config`` (agent.config.provider_profile)
3. ``AgentConfig.resolve_from_profile`` (agent.config.models)
4. ``update_config`` (agent.config.loader)
5. ``validate_config`` (agent.config.loader)
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

# Add project root so imports work
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from agent.utils import deep_merge
from agent.config.models import AgentConfig
from agent.config.provider_profile import ProviderProfile, ProviderManager
from agent.config.loader import update_config, validate_config


# ══════════════════════════════════════════════════════════════════════════════
# 1. deep_merge — unit tests
# ══════════════════════════════════════════════════════════════════════════════

class TestDeepMerge:
    """Tests for agent.utils.deep_merge."""

    def test_flat_merge(self):
        """Simple flat dict overlay."""
        base = {"a": 1, "b": 2}
        overlay = {"b": 3, "c": 4}
        result = deep_merge(base, overlay)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_nested_dict_deep_merge(self):
        """Nested dicts are merged recursively, not replaced."""
        base = {"session_permissions": {"filesystem": "read", "network": "all"}}
        overlay = {"session_permissions": {"filesystem": "write"}}
        result = deep_merge(base, overlay)
        # filesystem should be overwritten by overlay, network preserved from base
        assert result == {"session_permissions": {"filesystem": "write", "network": "all"}}

    def test_nested_dict_overlay_has_extra_key(self):
        """Overlay can add new keys inside nested dicts."""
        base = {"nested": {"a": 1}}
        overlay = {"nested": {"b": 2}}
        result = deep_merge(base, overlay)
        assert result == {"nested": {"a": 1, "b": 2}}

    def test_overlay_none_removes_key(self):
        """Overlay value of None removes the key from the result."""
        base = {"a": 1, "b": 2}
        overlay = {"b": None}
        result = deep_merge(base, overlay)
        assert result == {"a": 1}
        assert "b" not in result

    def test_overlay_none_on_nested_preserves_outer(self):
        """None overlay removes the key but leaves other keys intact."""
        base = {"keep": "me", "remove": {"nested": "value"}}
        overlay = {"remove": None}
        result = deep_merge(base, overlay)
        assert result == {"keep": "me"}

    def test_base_is_not_mutated(self):
        """deep_merge returns a new dict, does not modify the original."""
        base = {"a": {"x": 1}}
        overlay = {"a": {"y": 2}}
        result = deep_merge(base, overlay)
        assert result == {"a": {"x": 1, "y": 2}}
        # Original should be unchanged
        assert base == {"a": {"x": 1}}

    def test_empty_overlay(self):
        """Empty overlay returns a copy of base."""
        base = {"a": 1}
        result = deep_merge(base, {})
        assert result == {"a": 1}
        assert result is not base  # should be a new dict

    def test_scalar_overrides_dict(self):
        """If overlay has a non-dict where base has a dict, overlay wins."""
        base = {"a": {"nested": "value"}}
        overlay = {"a": "scalar"}
        result = deep_merge(base, overlay)
        assert result == {"a": "scalar"}


# ══════════════════════════════════════════════════════════════════════════════
# 2. ProviderManager.resolve_config — unit tests
# ══════════════════════════════════════════════════════════════════════════════

class TestResolveConfig:
    """Tests for ProviderManager.resolve_config() — model priority & field overwrite."""

    @pytest.fixture
    def manager(self, tmp_path):
        """A ProviderManager with a test profile, no real file."""
        mgr = ProviderManager(file_path=tmp_path / "test_providers.json")
        mgr._profiles = {
            "test-openai": ProviderProfile(
                id="test-openai",
                label="Test OpenAI",
                provider_type="openai",
                base_url="https://api.openai.com/v1",
                api_key="sk-test-openai-key",
                default_model="gpt-4",
            ),
            "test-deepseek": ProviderProfile(
                id="test-deepseek",
                label="Test DeepSeek",
                provider_type="deepseek",
                base_url="https://api.deepseek.com/v1",
                api_key="sk-test-deepseek-key",
                default_model="deepseek-chat",
            ),
        }
        mgr._active_profile_id = "test-openai"
        return mgr

    def test_no_profile_id_returns_unchanged(self, manager):
        """If config_dict has no provider_id, return as-is."""
        config = {"model": "gpt-3.5-turbo"}
        result = manager.resolve_config(config)
        assert result == config

    def test_unknown_profile_id_returns_unchanged(self, manager):
        """If provider_id doesn't match any profile, return as-is."""
        config = {"provider_id": "nonexistent", "model": "gpt-3.5-turbo"}
        result = manager.resolve_config(config)
        assert result == config

    def test_provider_fields_overwritten(self, manager):
        """provider_type, base_url, api_key always come from profile."""
        config = {
            "provider_id": "test-openai",
            "provider_type": "anthropic",  # stale value
            "base_url": "https://old.url",  # stale value
            "api_key": "old-key",           # stale value
        }
        result = manager.resolve_config(config)
        assert result["provider_type"] == "openai"
        assert result["base_url"] == "https://api.openai.com/v1"
        assert result["api_key"] == "sk-test-openai-key"

    def test_model_override_takes_precedence(self, manager):
        """model_override is the highest priority for model."""
        config = {
            "provider_id": "test-openai",
            "model": "gpt-3.5-turbo",
            "model_override": "gpt-4-turbo",
        }
        result = manager.resolve_config(config)
        assert result["model"] == "gpt-4-turbo"

    def test_user_model_preserved_when_no_override(self, manager):
        """User's explicit model is preserved if no model_override."""
        config = {
            "provider_id": "test-openai",
            "model": "gpt-3.5-turbo",
        }
        result = manager.resolve_config(config)
        assert result["model"] == "gpt-3.5-turbo"

    def test_default_model_fallback(self, manager):
        """If no model or model_override, use profile.default_model."""
        config = {
            "provider_id": "test-openai",
            # no model key
        }
        result = manager.resolve_config(config)
        assert result["model"] == "gpt-4"

    def test_switching_providers_overwrites_fields(self, manager):
        """Switching provider_id clears stale values from the previous provider."""
        config = {
            "provider_id": "test-deepseek",
            "provider_type": "openai",  # stale
            "base_url": "https://api.openai.com/v1",  # stale
            "api_key": "sk-test-openai-key",  # stale
        }
        result = manager.resolve_config(config)
        assert result["provider_type"] == "deepseek"
        assert result["base_url"] == "https://api.deepseek.com/v1"
        assert result["api_key"] == "sk-test-deepseek-key"

    def test_model_override_with_switched_provider(self, manager):
        """Switching provider while model_override is set uses the override."""
        config = {
            "provider_id": "test-deepseek",
            "model_override": "gpt-4-turbo",
        }
        result = manager.resolve_config(config)
        # Provider fields come from deepseek profile
        assert result["provider_type"] == "deepseek"
        assert result["base_url"] == "https://api.deepseek.com/v1"
        assert result["api_key"] == "sk-test-deepseek-key"
        # Model comes from override
        assert result["model"] == "gpt-4-turbo"

    def test_original_dict_not_mutated(self, manager):
        """resolve_config returns a new dict."""
        config = {"provider_id": "test-openai"}
        original_keys = set(config.keys())
        result = manager.resolve_config(config)
        assert result is not config
        # Original should not have provider fields added
        assert set(config.keys()) == original_keys


# ══════════════════════════════════════════════════════════════════════════════
# 3. AgentConfig.resolve_from_profile — integration
# ══════════════════════════════════════════════════════════════════════════════

class TestResolveFromProfile:
    """Tests for AgentConfig.resolve_from_profile() — model field priority fix."""

    @pytest.fixture
    def manager(self, tmp_path):
        mgr = ProviderManager(file_path=tmp_path / "test_providers.json")
        mgr._profiles = {
            "test-openai": ProviderProfile(
                id="test-openai", label="Test OpenAI",
                provider_type="openai",
                base_url="https://api.openai.com/v1",
                api_key="sk-test-key",
                default_model="gpt-4",
            ),
        }
        mgr._active_profile_id = "test-openai"
        return mgr

    def _make_config(self, **overrides) -> AgentConfig:
        """Helper to build an AgentConfig with minimal required fields."""
        defaults = dict(
            provider_id="test-openai",
            provider_type="openai",
            base_url="https://api.openai.com/v1",
            api_key="sk-test-key",
            model="gpt-3.5-turbo",
            enabled_tools=["FilePreviewTool"],
        )
        defaults.update(overrides)
        return AgentConfig(**defaults)

    def test_resolve_preserves_user_model(self, manager):
        """resolve_from_profile() preserves user's explicit model."""
        config = self._make_config(model="gpt-3.5-turbo", model_override=None)
        config.resolve_from_profile(manager)
        assert config.model == "gpt-3.5-turbo"

    def test_resolve_applies_model_override(self, manager):
        """resolve_from_profile() applies model_override."""
        config = self._make_config(model="gpt-3.5-turbo", model_override="gpt-4-turbo")
        resolved = config.resolve_from_profile(manager)
        assert resolved.model == "gpt-4-turbo"

    def test_resolve_falls_back_to_default_model(self, manager):
        """resolve_from_profile() uses default_model when no model set."""
        config = self._make_config(model="", model_override=None)
        resolved = config.resolve_from_profile(manager)
        assert resolved.model == "gpt-4"


# ══════════════════════════════════════════════════════════════════════════════
# 4. update_config — unit tests
# ══════════════════════════════════════════════════════════════════════════════

class TestUpdateConfig:
    """Tests for loader.update_config()."""

    def test_simple_update(self):
        """Shallow update replaces top-level keys."""
        current = {"a": 1, "b": 2}
        updates = {"b": 3}
        result = update_config(current, updates)
        assert result == {"a": 1, "b": 3}

    def test_update_adds_new_key(self):
        """Update can add keys not in the original."""
        current = {"a": 1}
        updates = {"b": 2}
        result = update_config(current, updates)
        assert result == {"a": 1, "b": 2}

    def test_original_not_mutated(self):
        """Returns a new dict."""
        current = {"a": 1}
        updates = {"b": 2}
        result = update_config(current, updates)
        assert result is not current
        assert "b" not in current

    def test_empty_updates(self):
        """Empty updates returns a copy."""
        current = {"a": 1}
        result = update_config(current, updates={})
        assert result == {"a": 1}
        assert result is not current

    def test_nested_not_deep_merged(self):
        """update_config does NOT deep merge (it's a known limitation)."""
        current = {"nested": {"a": 1, "b": 2}}
        updates = {"nested": {"a": 99}}  # only provides 'a'
        result = update_config(current, updates)
        # The outer 'nested' key is replaced wholesale
        assert result == {"nested": {"a": 99}}
        assert "b" not in result["nested"]


# ══════════════════════════════════════════════════════════════════════════════
# 5. validate_config — unit tests
# ══════════════════════════════════════════════════════════════════════════════

class TestValidateConfig:
    """Tests for loader.validate_config()."""

    def test_valid_minimal_config(self):
        """Minimal valid config with required fields."""
        config = {
            "enabled_tools": ["FilePreviewTool"],
        }
        result = validate_config(config)
        assert result is not None
        assert isinstance(result, AgentConfig)

    def test_valid_full_config(self):
        """Valid config with all optional fields."""
        config = {
            "provider_id": "test",
            "provider_type": "openai",
            "model": "gpt-4",
            "enabled_tools": ["FilePreviewTool", "GlobTool"],
            "system_prompt": "You are a test agent.",
            "workspace_path": "/tmp/test_ws",
            "tool_output_token_limit": 500,
            "session_permissions": {
                "filesystem": "read",
                "network": False,
                "container": False,
            },
        }
        result = validate_config(config)
        assert result is not None
        assert result.model == "gpt-4"
        assert result.tool_output_token_limit == 500

    def test_invalid_config_returns_none(self):
        """Truly invalid config returns None (no crash)."""
        config = {"enabled_tools": "not_a_list"}  # should be list
        result = validate_config(config)
        assert result is None

    def test_stop_check_string_is_stripped(self):
        """If stop_check is a string (legacy), it is stripped."""
        config = {
            "enabled_tools": ["FilePreviewTool"],
            "stop_check": "some string that should be removed",
        }
        result = validate_config(config)
        assert result is not None
        # stop_check should not be on the resulting model (it's a callable field)
        assert not hasattr(result, "stop_check") or result.stop_check is None


# ══════════════════════════════════════════════════════════════════════════════
# Run directly
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
