"""
WebSocket integration tests with a MockProvider.

Tests the full lifecycle with a mock LLM provider instead of a real API,
so tests work without any API keys or network access.

The MockProvider:
- Subclasses LLMProvider (from llm_providers.base)
- Returns canned responses
- Tracks call counts for verification
- Gets registered with ProviderFactory

Tests:
1. new_session emits lifecycle events
2. continue_session with mock provider returns a response
3. apply_config merges correctly via the bridge
4. saving and loading session preserves config
5. config merge respects nested session_permissions (deep_merge fix)
"""
from __future__ import annotations

import json
import os
import sys as sys_mod
import tempfile
import pathlib
import time
import importlib
from pathlib import Path
from typing import Optional, List, Dict, Any
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient
from llm_providers.base import LLMProvider, ProviderConfig, LLMResponse
from llm_providers.factory import ProviderFactory


# ══════════════════════════════════════════════════════════════════════════════
# MockProvider — a fake LLM provider for testing
# ══════════════════════════════════════════════════════════════════════════════

class MockProvider(LLMProvider):
    """
    A mock LLM provider that returns canned responses.
    Useful for testing the agent pipeline without network access or API keys.

    Tracks ``call_count`` and stores the last received ``messages`` for
    test assertions.
    """

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self.call_count = 0
        self.last_messages: Optional[List[Dict[str, Any]]] = None
        self.last_tools: Optional[List[Dict[str, Any]]] = None
        self._response_text = "This is a mock response from the test provider."

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        **kwargs,
    ) -> LLMResponse:
        self.call_count += 1
        self.last_messages = messages
        self.last_tools = tools
        return LLMResponse(
            content=self._response_text,
            reasoning="mock reasoning",
            tool_calls=None,
            usage={"prompt_tokens": 10, "completion_tokens": 5},
            provider="mock",
            model="mock-model",
        )

    def count_tokens(self, messages: List[Dict], tools: Optional[List] = None) -> int:
        # Return a fixed token count for testing
        return 42


# ══════════════════════════════════════════════════════════════════════════════
# Fixture: patched environment + MockProvider registration
# ══════════════════════════════════════════════════════════════════════════════

def _register_mock_provider():
    """Register MockProvider if not already registered."""
    if "mock" not in ProviderFactory._providers:
        ProviderFactory.register_provider("mock", MockProvider)


@pytest.fixture(scope="module")
def mock_server():
    """
    Create temp HOME, patch Path.home(), register MockProvider, re-import server.
    The server will start with ``provider_type="mock"`` so it uses the
    MockProvider instead of any real API.
    """
    tmp_home = tempfile.mkdtemp(prefix="test_mock_home_")
    fake_home = Path(tmp_home)

    # Set HOME env var
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = tmp_home

    # Clear real API keys to prevent any accidental use
    saved_env = {}
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_COMPATIBLE_API_KEY"):
        saved_env[key] = os.environ.pop(key, None)

    # Patch Path.home()
    patcher = patch.object(pathlib.Path, "home", return_value=fake_home)
    patcher.start()

    # Register MockProvider BEFORE importing server
    _register_mock_provider()

    # Clear cached modules so re-import picks up the mock
    mod_prefixes = ("web_ui.backend", "agent.config.provider_profile",
                    "thoughtmachine.bootstrap")
    for mod_name in list(sys_mod.modules.keys()):
        if any(mod_name.startswith(p) for p in mod_prefixes):
            del sys_mod.modules[mod_name]

    server_mod = importlib.import_module("web_ui.backend.server")
    app = server_mod.app

    yield app, tmp_home

    # Cleanup
    patcher.stop()
    if old_home is not None:
        os.environ["HOME"] = old_home
    else:
        os.environ.pop("HOME", None)
    for key, val in saved_env.items():
        if val is not None:
            os.environ[key] = val

    import shutil
    shutil.rmtree(tmp_home, ignore_errors=True)


@pytest.fixture(autouse=True)
def reset_mock_provider():
    """Reset MockProvider call tracking between tests."""
    if "mock" in ProviderFactory._providers:
        provider_cls = ProviderFactory._providers["mock"]
        if hasattr(provider_cls, "reset_all"):
            provider_cls.reset_all()


# Add a reset mechanism to MockProvider
MockProvider._instances = []

_orig_mock_init = MockProvider.__init__
def _tracking_init(self, config):
    _orig_mock_init(self, config)
    MockProvider._instances.append(self)
MockProvider.__init__ = _tracking_init

@classmethod
def reset_all(cls):
    for inst in cls._instances:
        inst.call_count = 0
        inst.last_messages = None
        inst.last_tools = None
MockProvider.reset_all = reset_all


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def recv_n(ws, n: int, timeout: float = 5.0) -> list:
    """Receive exactly *n* text messages from the WebSocket."""
    messages = []
    deadline = time.monotonic() + timeout
    for _ in range(n):
        if time.monotonic() > deadline:
            break
        raw = ws.receive_text()
        messages.append(json.loads(raw))
    return messages


def poll_for_type(ws, expected_type: str, timeout: float = 5.0) -> list:
    """Receive messages until one of type ``expected_type`` is found."""
    messages = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = ws.receive_text()
        msg = json.loads(raw)
        messages.append(msg)
        if msg.get("type") == expected_type:
            break
    return messages


def new_session(ws):
    """Create a new session and drain the initial 6 lifecycle events."""
    ws.send_json({"command": "new_session"})
    return recv_n(ws, 6, timeout=5.0)


# ══════════════════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestWebSocketWithMockProvider:

    def test_new_session_emits_six_events(self, mock_server):
        """new_session emits the standard 6 lifecycle events."""
        app, _tmp_home = mock_server
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                messages = new_session(ws)

        assert len(messages) == 6
        expected_types = [
            "session_loaded", "state_changed", "tokens_updated",
            "context_updated", "config_changed", "status_message",
        ]
        assert [m["type"] for m in messages] == expected_types

    def test_continue_session_calls_mock_provider(self, mock_server):
        """
        continue_session triggers the mock provider and returns a response.
        The MockProvider should be called exactly once.
        """
        app, _tmp_home = mock_server
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                # Create session
                new_session(ws)

                # Send a query (no API key needed — mock provider handles it)
                ws.send_json({
                    "command": "apply_config",
                    "config": {
                        "provider_type": "mock",
                        "provider_id": "",
                        "api_key": "mock-key",
                        "model": "mock-model",
                    },
                })
                # Wait for config_changed confirmation
                poll_for_type(ws, "config_changed", timeout=5.0)

                # Continue session
                ws.send_json({
                    "command": "continue_session",
                    "query": "Hello, mock provider!",
                })

                # Collect messages — we expect at least state_changed + conversation_changed
                responses = []
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    try:
                        raw = ws.receive_text()
                        msg = json.loads(raw)
                        responses.append(msg)
                        # Stop when we see a response_type that indicates finish
                        if msg.get("type") == "conversation_changed":
                            break
                    except Exception:
                        break

        # The conversation_changed should contain the mock response
        conv_msgs = [m for m in responses if m.get("type") == "conversation_changed"]
        assert len(conv_msgs) > 0, (
            f"No conversation_changed received. Got types: "
            f"{[m.get('type') for m in responses]}"
        )

        # Find the mock provider instance and verify it was called
        mock_instances = MockProvider._instances
        assert len(mock_instances) > 0, "No MockProvider was ever created"
        called = any(inst.call_count > 0 for inst in mock_instances)
        assert called, "MockProvider was never called"

    def test_apply_config_deep_merge_permissions(self, mock_server):
        """
        apply_config with partial session_permissions should deep-merge,
        not overwrite the other permission fields (the shallow merge bug).
        """
        app, _tmp_home = mock_server
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                # Create session
                new_session(ws)

                # First, apply a config with full session_permissions
                ws.send_json({
                    "command": "apply_config",
                    "config": {
                        "provider_type": "mock",
                        "api_key": "mock-key",
                        "model": "mock-model",
                        "session_permissions": {
                            "filesystem": "read",
                            "network": False,
                            "browser": "deny",
                        },
                    },
                })
                poll_for_type(ws, "config_changed", timeout=5.0)

                # Now apply a partial update — only change one field
                ws.send_json({
                    "command": "apply_config",
                    "config": {
                        "session_permissions": {
                            "filesystem": "write",
                        },
                    },
                })
                config_msgs = poll_for_type(ws, "config_changed", timeout=5.0)
                last_config = config_msgs[-1]["config"]

        # The deep-merge fix should preserve network and browser
        perms = last_config.get("session_permissions", {})
        assert perms.get("filesystem") == "write", (
            f"Expected filesystem=write, got {perms.get('filesystem')}"
        )
        assert perms.get("network") is False, (
            f"Expected network=False (preserved by deep_merge), got {perms.get('network')}"
        )
        assert perms.get("browser") == "deny", (
            f"Expected browser=deny (preserved by deep_merge), got {perms.get('browser')}"
        )

    def test_model_override_priority(self, mock_server):
        """
        model_override should take precedence over user's model and
        profile's default_model.
        """
        app, _tmp_home = mock_server
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                new_session(ws)

                # Apply config with both model and model_override
                ws.send_json({
                    "command": "apply_config",
                    "config": {
                        "provider_type": "mock",
                        "api_key": "mock-key",
                        "model": "gpt-3.5-turbo",
                        "model_override": "gpt-4-turbo",
                    },
                })
                config_msgs = poll_for_type(ws, "config_changed", timeout=5.0)
                last_config = config_msgs[-1]["config"]

        # model_override should win → model = gpt-4-turbo
        assert last_config.get("model") == "gpt-4-turbo", (
            f"Expected model=gpt-4-turbo (model_override wins), "
            f"got model={last_config.get('model')!r}"
        )

    def test_api_key_stripped_from_config_dump(self, mock_server):
        """
        The config_changed event should NOT contain the actual api_key value.
        It should be stripped before sending to the frontend.
        """
        app, _tmp_home = mock_server
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                new_session(ws)

                ws.send_json({
                    "command": "apply_config",
                    "config": {
                        "provider_type": "mock",
                        "api_key": "sk-test-secret-key-12345",
                        "model": "mock-model",
                    },
                })
                config_msgs = poll_for_type(ws, "config_changed", timeout=5.0)
                last_config = config_msgs[-1]["config"]

        # api_key should not be in the config dict sent to frontend
        config_api_key = last_config.get("api_key")
        assert config_api_key is None or config_api_key == "", (
            f"api_key should be stripped from config dump, "
            f"got {config_api_key!r}"
        )
        # But api_key_configured should be True
        assert last_config.get("api_key_configured") is True

    def test_save_and_load_session_roundtrip(self, mock_server):
        """
        Saving a session and loading it back should preserve config fields
        (including session_permissions).
        """
        app, tmp_home = mock_server
        session_id = None

        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                msgs = new_session(ws)
                session_id = msgs[0].get("session_id")

                # Apply config with custom settings
                ws.send_json({
                    "command": "apply_config",
                    "config": {
                        "provider_type": "mock",
                        "api_key": "mock-key",
                        "model": "mock-model",
                        "session_permissions": {
                            "filesystem": "read",
                            "network": "all",
                        },
                        "system_prompt": "You are a test agent.",
                    },
                })
                poll_for_type(ws, "config_changed", timeout=5.0)

                # Save the session
                ws.send_json({"command": "save_session"})
                poll_for_type(ws, "status_message", timeout=5.0)

        # Now load the session in a new connection
        assert session_id is not None
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.send_json({
                    "command": "load_session",
                    "session_id": session_id,
                })
                msgs = poll_for_type(ws, "session_loaded", timeout=5.0)
                load_msg = msgs[-1]

        # Check the loaded session has the right id and config is present
        assert load_msg.get("session_id") == session_id
        loaded_config = load_msg.get("config", {})
        assert loaded_config.get("model") == "mock-model"
        assert loaded_config.get("system_prompt") == "You are a test agent."
        perms = loaded_config.get("session_permissions", {})
        assert perms.get("filesystem") == "read"
        assert perms.get("network") == "all"


# ══════════════════════════════════════════════════════════════════════════════
# Run directly
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--timeout=30"])
