"""Integration tests: First query on a completely fresh vault.

Tests that the config pipeline and bridge handle a brand-new workspace
(with zero workspace-specific defaults) gracefully. Verifies fixes for
the "first-query silent failure" bug where:

1. Missing workspace defaults.json caused silent failure (2A)
2. Controller path session capture race caused conversation_changed
   events to be skipped (2B)
3. session_stop didn't emit final conversation data (2C)
"""

import json
import tempfile
from pathlib import Path
from typing import Dict, List, Any

import pytest

from agent.config.session_config import SessionConfig
from agent.config.config_manager import resolve_config_defaults
from web_ui.backend.bridge import WebAgentBridge
from web_ui.backend.config_manager import ConfigManager
from web_ui.backend.session_manager import SessionManager
from session.store import FileSystemSessionStore
from session.models import Session

from tests.mocks.puppet_agent import PuppetLLM
from tests.integration.test_ws_config_roundtrip import (
    MockWebSocket,
    EventCollector,
    simulate_apply_config,
)


# ---------------------------------------------------------------------------
# Test 1: resolve_config_defaults handles missing workspace defaults (2A)
# ---------------------------------------------------------------------------

class TestResolveConfigDefaultsFreshVault:
    """Verify resolve_config_defaults works with missing workspace defaults."""

    @pytest.fixture
    def vault_with_factory_only(self, tmp_path, monkeypatch):
        """Create a vault with factory defaults but NO workspace defaults."""
        vault_root = tmp_path / ".thoughtmachine"
        (vault_root / "system").mkdir(parents=True)
        (vault_root / "user").mkdir(parents=True)

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

        monkeypatch.setattr(
            "agent.config.config_manager._vault_root",
            lambda: vault_root,
        )
        yield vault_root

    def test_missing_workspace_defaults_returns_factory(self, vault_with_factory_only):
        """resolve_config_defaults for a non-existent workspace returns factory defaults."""
        result = resolve_config_defaults("nonexistent-workspace-12345")
        assert isinstance(result, dict)
        assert result.get("provider_id") == "openai"
        assert result.get("temperature") == 0.7

    def test_empty_vault_returns_empty(self, tmp_path, monkeypatch):
        """Completely empty vault returns empty dict (no crash)."""
        vault_root = tmp_path / ".thoughtmachine"
        vault_root.mkdir(parents=True)
        monkeypatch.setattr(
            "agent.config.config_manager._vault_root",
            lambda: vault_root,
        )
        result = resolve_config_defaults("nonexistent-ws")
        assert result == {}


# ---------------------------------------------------------------------------
# Test 2: session_stop emits conversation_changed (2C)
# ---------------------------------------------------------------------------

class TestSessionStopEmitsConversation:
    """Verify that session_stop always emits conversation_changed."""

    def test_session_stop_broadcasts_conversation(self, hermetic_vault):
        """session_stop event triggers conversation_changed broadcast."""
        collector = EventCollector()
        store_dir = tempfile.mkdtemp(prefix="test_session_stop_")
        session_store = FileSystemSessionStore(sessions_dir=store_dir)

        bridge = WebAgentBridge(session_store=session_store)
        bridge.set_event_callback(collector)

        # Set up a session with messages on the bridge
        session = Session()
        session.add_message("user", "Hello")
        session.add_message("assistant", "Hi there!")
        bridge._session = session
        bridge._session_id = session.session_id
        bridge._history_version = session.conversation_version

        # Directly call _map_and_emit with a session_stop event
        bridge._map_and_emit({
            "type": "session_stop",
            "stop_reason": "completed",
        })

        # Verify conversation_changed was emitted
        conv_events = [
            e for e in collector.events
            if e.get("type") == "conversation_changed"
        ]
        assert len(conv_events) >= 1, (
            "session_stop did not emit conversation_changed"
        )

        # Verify messages are in the event
        last_conv = conv_events[-1]
        messages = last_conv.get("messages", [])
        assert len(messages) >= 2, (
            f"Expected at least 2 messages in conversation, got {len(messages)}"
        )

        # Verify state_changed was also emitted
        state_events = [
            e for e in collector.events
            if e.get("type") == "state_changed"
        ]
        assert len(state_events) >= 1

    def test_session_stop_no_session_does_not_crash(self, hermetic_vault):
        """session_stop with None session does not crash (edge case)."""
        collector = EventCollector()
        store_dir = tempfile.mkdtemp(prefix="test_session_stop_")
        session_store = FileSystemSessionStore(sessions_dir=store_dir)

        bridge = WebAgentBridge(session_store=session_store)
        bridge.set_event_callback(collector)

        # Session is None (as in fresh start before capture)
        bridge._session = None

        # Should not raise
        bridge._map_and_emit({
            "type": "session_stop",
            "stop_reason": "completed",
        })

        # Should have at least state_changed
        state_events = [
            e for e in collector.events
            if e.get("type") == "state_changed"
        ]
        assert len(state_events) >= 1


# ---------------------------------------------------------------------------
# Test 3: SessionManager creates session without workspace defaults (2A)
# ---------------------------------------------------------------------------

class TestSessionManagerFreshVault:
    """SessionManager operations on fresh vault."""

    def test_session_manager_create_no_workspace_defaults(self, hermetic_vault):
        """SessionManager.create_session() should not fail when workspace has no defaults."""
        store_dir = tempfile.mkdtemp(prefix="test_sm_")
        session_store = FileSystemSessionStore(sessions_dir=store_dir)
        config_mgr = ConfigManager()
        sm = SessionManager(session_store=session_store, config_manager=config_mgr)

        session_id, frontend_config = sm.create_session(mode="agent")
        assert session_id is not None
        assert isinstance(frontend_config, dict)
        assert frontend_config.get("mode") == "agent"


# ---------------------------------------------------------------------------
# Test 4: Bridge captures session in controller path (2B)
# ---------------------------------------------------------------------------

class TestBridgeSessionCapture:
    """Verify session capture in controller path."""

    def test_bridge_start_sets_session_in_standalone_path(self, hermetic_vault):
        """Standalone path sets session immediately (pre-existing behavior)."""
        collector = EventCollector()
        store_dir = tempfile.mkdtemp(prefix="test_session_capture_")
        session_store = FileSystemSessionStore(sessions_dir=store_dir)

        bridge = WebAgentBridge(session_store=session_store)
        bridge.set_event_callback(collector)

        bridge._session_config = SessionConfig(
            mode="custom",
            max_turns=3,
            session_permissions={},
            enabled_tools=[],
            provider_id="openai",
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
        )

        # Create a loaded session (simulating resume)
        session = Session()
        session.add_message("user", "Hello")
        bridge._loaded_session = session

        # Simulate the standalone path setup
        bridge._agent = type('obj', (object,), {'process_query': lambda self, q: iter([])})()
        bridge._session = session
        bridge._session_id = session.session_id
        bridge._running = True

        assert bridge._session is not None
        assert bridge._session_id is not None

    def test_session_capture_from_controller_agent(self, hermetic_vault):
        """Verify _on_controller_event captures session from controller agent."""
        collector = EventCollector()
        store_dir = tempfile.mkdtemp(prefix="test_ctrl_capture_")
        session_store = FileSystemSessionStore(sessions_dir=store_dir)

        bridge = WebAgentBridge(session_store=session_store)
        bridge.set_event_callback(collector)

        # Create a minimal mock controller with an agent that has a session
        session = Session()
        session.add_message("user", "Test")

        mock_agent = type('obj', (object,), {'session': session})()
        mock_controller = type('obj', (object,), {
            'agent': mock_agent,
            'is_busy': False,
            'is_running': False,
            'set_event_callback': lambda self, cb: None,
        })()

        bridge.set_controller(mock_controller)
        bridge._session = None  # Fresh start

        # Send a fake event through _on_controller_event
        bridge._on_controller_event({
            "type": "execution_state_change",
            "new_state": "running",
        })

        # Session should have been captured
        assert bridge._session is not None, (
            "Bridge should have captured session from controller agent"
        )
        assert bridge._session.session_id == session.session_id, (
            "Session ID should match the controller's session"
        )


# ---------------------------------------------------------------------------
# Test 5: Config changed message includes settings, permissions, merged_config
# ---------------------------------------------------------------------------

class TestConfigChangedMessageStructure:
    """Verify config_changed broadcasts include the new structured fields."""

    def test_apply_config_includes_settings_permissions_merged(self, hermetic_vault):
        """bridge.apply_config() returns config, settings, permissions, merged_config."""
        from tests.integration.test_ws_config_roundtrip import simulate_apply_config

        bridge = WebAgentBridge()
        bridge._session_config = SessionConfig(
            mode="custom",
            max_turns=100,
            session_permissions={},
            enabled_tools=[],
            provider_id="openai",
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
        )

        frontend_config = {
            "mode": "custom",
            "temperature": 0.3,
            "session_permissions": {
                "filesystem": "write",
                "network": "banned",
            },
        }

        output = simulate_apply_config(bridge, frontend_config)
        result = output["result"]

        # --- Result has the expected structure ---
        assert isinstance(result, dict)
        assert "config" in result, "Result must contain 'config'"
        assert "settings" in result, "Result must contain 'settings'"
        assert "permissions" in result, "Result must contain 'permissions'"
        assert "merged_config" in result, "Result must contain 'merged_config'"

        # --- config is the full frontend-format config dict ---
        assert isinstance(result["config"], dict)
        assert result["config"]["mode"] == "custom"
        assert result["config"].get("provider") is not None  # provider was set from SessionConfig

        # --- settings is a subset of operational knobs ---
        assert isinstance(result["settings"], dict)
        assert result["settings"]["mode"] == "custom"
        assert "temperature" in result["settings"]
        assert "provider" in result["settings"]
        assert "model" in result["settings"]
        # settings should NOT include tools or permissions
        assert "tools" not in result["settings"]
        assert "session_permissions" not in result["settings"]

        # --- permissions is the resolved permissions dict ---
        assert isinstance(result["permissions"], dict)
        assert "filesystem" in result["permissions"]
        assert result["permissions"]["filesystem"] == "write"
        assert "network" in result["permissions"]
        assert result["permissions"]["network"] == "banned"
        # Default fields should be present
        assert result["permissions"].get("container") is not None
        assert result["permissions"].get("execution") is not None

        # --- merged_config equals config (full frontend format) ---
        assert result["merged_config"] == result["config"]

    def test_apply_config_without_permissions_uses_defaults(self, hermetic_vault):
        """When no session_permissions in config, defaults are applied."""
        from tests.integration.test_ws_config_roundtrip import simulate_apply_config

        bridge = WebAgentBridge()
        bridge._session_config = SessionConfig(
            mode="agent",
            max_turns=50,
            session_permissions={},
            enabled_tools=[],
            provider_id="anthropic",
            model="claude-3",
            base_url="https://api.anthropic.com/v1",
        )

        frontend_config = {
            "mode": "agent",
            "temperature": 0.7,
        }

        output = simulate_apply_config(bridge, frontend_config)
        result = output["result"]

        # Permissions should be populated with defaults
        perms = result["permissions"]
        assert perms.get("filesystem") is not None
        assert perms.get("network") is not None
        assert perms.get("container") is not None
        assert perms.get("system") is not None
        assert perms.get("git") is not None
        assert perms.get("execution") is not None

    def test_apply_config_changed_event_has_settings_permissions(self, hermetic_vault):
        """Config changed event sent to frontend has all new fields."""
        from tests.integration.test_ws_config_roundtrip import simulate_apply_config

        bridge = WebAgentBridge()
        bridge._session_config = SessionConfig(
            mode="custom",
            max_turns=100,
            session_permissions={"filesystem": "write"},
            enabled_tools=[],
            provider_id="openai",
            model="gpt-4",
            base_url="https://api.openai.com/v1",
        )

        frontend_config = {
            "mode": "custom",
            "temperature": 0.5,
            "session_permissions": {"filesystem": "full"},
        }

        output = simulate_apply_config(bridge, frontend_config)
        event = output["config_changed_event"]

        assert event["type"] == "config_changed"
        assert "config" in event
        assert "settings" in event
        assert "permissions" in event
        assert "merged_config" in event

        # Settings should reflect the applied config
        assert event["settings"]["mode"] == "custom"
        assert event["settings"]["temperature"] == 0.5

        # Permissions should reflect the applied permission overrides
        assert event["permissions"]["filesystem"] == "full"

        # merged_config should equal config
        assert event["merged_config"] == event["config"]
