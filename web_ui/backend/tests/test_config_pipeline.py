"""
Tests for the web_ui backend config pipeline.

Covers:
- bridge.apply_config() stores and persists config correctly
- _translate_frontend_config() correctly maps frontend ↔ backend fields
"""
import json
import pytest
from pathlib import Path
from agent.config.models import AgentConfig
from web_ui.backend.bridge import WebAgentBridge
from web_ui.backend.server import _translate_frontend_config, _backend_to_frontend_config


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _find_session_files(sessions_dir: Path) -> list:
    """Return all .json session files (excluding _meta files) in the given directory sorted by name."""
    return sorted(f for f in sessions_dir.glob("*.json") if not f.name.startswith("_meta"))


# ══════════════════════════════════════════════════════════════════════════════
# Tests: bridge.apply_config
# ══════════════════════════════════════════════════════════════════════════════

class TestApplyConfig:
    """Tests for WebAgentBridge.apply_config()."""

    def test_apply_config_updates_bridge_config(self, session_store, temp_session_dir):
        """
        apply_config should:
        1. Update bridge._config with the supplied values
        2. Persist the config to a session file on disk
        """
        # ── Setup ──────────────────────────────────────────────────────
        bridge = WebAgentBridge()
        bridge._session_store = session_store

        # ── Exercise ───────────────────────────────────────────────────
        result = bridge.apply_config({
            "temperature": 1.5,
            "model": "test-model",
            "max_tokens": 8192,
        })

        # ── Assert: bridge config updated ──────────────────────────────
        assert result == {"success": True}, f"apply_config failed: {result}"
        assert bridge._session_config is not None, "bridge._session_config should not be None after apply_config"
        assert bridge._session_config.temperature == 1.5
        assert bridge._session_config.model == "test-model"
        assert bridge._session_config.max_tokens == 8192

        # ── Assert: session file written to disk ───────────────────────
        session_files = _find_session_files(temp_session_dir)
        assert len(session_files) > 0, "No session files were written to disk"

        # Load the most recent session file and check config values
        latest = max(session_files, key=lambda f: f.stat().st_mtime)
        with open(latest, "r") as f:
            session_data = json.load(f)

        session_config = session_data.get("metadata", {}).get("session_config", {})
        assert session_config.get("temperature") == 1.5, \
            f"Expected temperature=1.5 in session file, got {session_config.get('temperature')}"
        assert session_config.get("model") == "test-model", \
            f"Expected model='test-model' in session file, got {session_config.get('model')}"
        assert session_config.get("max_tokens") == 8192, \
            f"Expected max_tokens=8192 in session file, got {session_config.get('max_tokens')}"

    def test_apply_config_merges_with_existing_config(self, session_store, temp_session_dir):
        """
        apply_config should merge with the existing config rather than
        replacing it entirely.
        """
        bridge = WebAgentBridge()
        bridge._session_store = session_store

        # Set initial config
        bridge.apply_config({
            "temperature": 0.5,
            "model": "initial-model",
            "max_tokens": 8192,
        })

        # Apply a partial update — only temperature changes
        bridge.apply_config({
            "temperature": 1.0,
        })

        # model and max_tokens should be preserved from the initial call
        assert bridge._session_config.temperature == 1.0
        assert bridge._session_config.model == "initial-model"
        assert bridge._session_config.max_tokens == 8192

    def test_apply_config_invalid_values_return_error(self, session_store):
        """apply_config should return {'success': False} for invalid input."""
        bridge = WebAgentBridge()
        bridge._session_store = session_store

        result = bridge.apply_config({
            "temperature": "not-a-number",  # invalid type
        })

        assert result.get("success") is False
        assert "error" in result


# ══════════════════════════════════════════════════════════════════════════════
# Tests: _translate_frontend_config
# ══════════════════════════════════════════════════════════════════════════════

class TestTranslateFrontendConfig:
    """Tests for _translate_frontend_config()."""

    def test_translate_frontend_config_passes_all_fields(self):
        """
        _translate_frontend_config should:
        - Map 'provider' → 'provider_type'
        - Pass through model, temperature unchanged
        - Convert tools [{name, enabled}] → enabled_tools list of enabled names
        """
        result = _translate_frontend_config({
            "temperature": 1.0,
            "model": "gpt-4",
            "provider": "openai",
            "tools": [
                {"name": "bash", "enabled": True},
                {"name": "file_read", "enabled": False},
            ],
        })

        # Provider mapping
        assert result.get("provider_type") == "openai"
        assert "provider" not in result, "provider should be removed, not kept alongside provider_type"

        # Passthrough fields
        assert result.get("model") == "gpt-4"
        assert result.get("temperature") == 1.0

        # Tools conversion
        assert "tools" not in result, "frontend 'tools' list should be removed after translation"
        assert "bash" in result.get("enabled_tools", []), \
            "'bash' should be in enabled_tools (was enabled=true)"
        assert "file_read" not in result.get("enabled_tools", []), \
            "'file_read' should NOT be in enabled_tools (was enabled=false)"

    def test_translate_frontend_config_anthropic_provider(self):
        """Verify 'anthropic' provider maps to provider_type='anthropic'."""
        result = _translate_frontend_config({"provider": "anthropic"})
        assert result.get("provider_type") == "anthropic"

    def test_translate_frontend_config_local_provider(self):
        """Verify 'local' provider maps to provider_type='openai_compatible'."""
        result = _translate_frontend_config({"provider": "local"})
        assert result.get("provider_type") == "openai_compatible"

    def test_translate_frontend_config_no_tools_list(self):
        """When no tools field is provided, enabled_tools should not be set."""
        result = _translate_frontend_config({"temperature": 0.5})
        assert "enabled_tools" not in result

    def test_translate_frontend_config_empty_tools_list(self):
        """When tools is an empty list, enabled_tools should be an empty list
        (all tools explicitly disabled)."""
        result = _translate_frontend_config({"tools": []})
        assert result.get("enabled_tools") == [], \
            f"Expected enabled_tools=[], got {result.get('enabled_tools')}"

    def test_translate_frontend_config_all_disabled_tools(self):
        """When all tools are disabled, enabled_tools should be an empty list
        (all tools explicitly disabled)."""
        result = _translate_frontend_config({
            "tools": [
                {"name": "bash", "enabled": False},
                {"name": "file_read", "enabled": False},
            ],
        })
        assert result.get("enabled_tools") == [], \
            f"Expected enabled_tools=[], got {result.get('enabled_tools')}"

    def test_translate_frontend_config_strips_private_keys(self):
        """Keys starting with '_' should be stripped from the result."""
        result = _translate_frontend_config({
            "temperature": 0.7,
            "_internal_id": "abc123",
            "_debug": True,
        })
        assert result.get("temperature") == 0.7
        assert "_internal_id" not in result
        assert "_debug" not in result

    def test_translate_frontend_config_no_provider(self):
        """When no provider is given, provider_type should not be set."""
        result = _translate_frontend_config({"temperature": 0.5})
        assert "provider_type" not in result

# ══════════════════════════════════════════════════════════════════════════════
# Tests: _backend_to_frontend_config  (round-trip completeness)
# ══════════════════════════════════════════════════════════════════════════════

class TestBackendToFrontendConfig:
    """Tests for _backend_to_frontend_config()."""

    def test_backend_to_frontend_emits_all_tools(self):
        """
        _backend_to_frontend_config should emit ALL known tools from
        SIMPLIFIED_TOOL_CLASSES, each with enabled: true/false.
        """
        result = _backend_to_frontend_config({
            "enabled_tools": ["FileEditor", "ReadFile"],
        })
        tools = result.get("tools", [])
        # Should be a list; every entry has name + enabled
        assert isinstance(tools, list)
        assert len(tools) > 5, (
            f"Expected many tools from SIMPLIFIED_TOOL_CLASSES, "
            f"got {len(tools)}: {[t['name'] for t in tools]}"
        )
        # FileEditor should be enabled
        fe = next((t for t in tools if t["name"] == "FileEditor"), None)
        assert fe is not None, "FileEditor should be in tools list"
        assert fe["enabled"] is True
        # Something not in enabled_tools should be disabled
        disabled = [t for t in tools if not t["enabled"]]
        assert len(disabled) > 0, (
            "At least some tools should be disabled (only enabled_tools "
            "containing FileEditor and ReadFile were passed)"
        )

    def test_backend_to_frontend_no_enabled_tools_key(self):
        """
        When enabled_tools key is absent, tools should remain as-is
        (or absent), not be populated.
        """
        result = _backend_to_frontend_config({"temperature": 0.5})
        # tools should not be populated from nowhere
        assert "tools" not in result or result["tools"] is None

    def test_backend_to_frontend_empty_enabled_tools(self):
        """
        When enabled_tools is an empty list, all tools should be
        present with enabled=False.
        """
        result = _backend_to_frontend_config({"enabled_tools": []})
        tools = result.get("tools", [])
        assert len(tools) > 0, "Should still emit all tools"
        assert all(t["enabled"] is False for t in tools), (
            "All tools should be disabled when enabled_tools is empty"
        )

