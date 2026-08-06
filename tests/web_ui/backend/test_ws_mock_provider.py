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
2. continue_session without a usable provider emits an error (mock never used)
3. apply_config replaces session_permissions wholesale
4. saving and loading session preserves config
5. model_override is ignored by apply_config
6. api_key is stripped from the config dump
7. ProviderFactory can instantiate the mock provider directly (unit-level)
"""
from __future__ import annotations

import json
import os
import sys as sys_mod
import tempfile
import pathlib
import time
from concurrent.futures import ThreadPoolExecutor
import importlib


# Module-level single-threaded executor for WebSocket reads.
# NOT wrapped in a ``with`` block — the executor lives for the full
# process lifetime so that a timed-out ``ws.receive_text()`` thread
# doesn't block cleanup.
_receive_pool = ThreadPoolExecutor(max_workers=1)
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
    if "mock" not in ProviderFactory._get_providers():
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
    if "mock" in ProviderFactory._get_providers():
        provider_cls = ProviderFactory._get_providers()["mock"]
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
    """Receive exactly *n* text messages from the WebSocket.
    Uses a thread pool to enforce a real wall-clock timeout."""
    messages = []
    deadline = time.monotonic() + timeout
    for _ in range(n):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        future = _receive_pool.submit(ws.receive_text)
        try:
            raw = future.result(timeout=remaining)
        except TimeoutError:
            future.cancel()
            break
        messages.append(json.loads(raw))
    return messages


def poll_for_type(ws, expected_type: str, timeout: float = 5.0) -> list:
    """Receive messages until one of type ``expected_type`` is found.
    Uses a thread pool to enforce a real wall-clock timeout."""
    messages = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        future = _receive_pool.submit(ws.receive_text)
        try:
            raw = future.result(timeout=remaining)
        except TimeoutError:
            future.cancel()
            break
        msg = json.loads(raw)
        messages.append(msg)
        if msg.get("type") == expected_type:
            break
    return messages


def new_session(ws):
    """Create a new session and drain the initial 5 lifecycle events."""
    ws.send_json({"command": "new_session"})
    return recv_n(ws, 5, timeout=5.0)


# ══════════════════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestWebSocketWithMockProvider:

    def test_new_session_emits_five_events(self, mock_server):
        """new_session emits the standard 5 lifecycle events."""
        app, _tmp_home = mock_server
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                messages = new_session(ws)

        assert len(messages) == 5
        expected_types = [
            "session_loaded", "tokens_updated",
            "context_updated", "config_changed", "status_message",
        ]
        assert [m["type"] for m in messages] == expected_types

    def test_continue_session_mock_provider_not_used(self, mock_server):
        """
        continue_session without a usable provider emits an error status_message.
        apply_config can no longer deliver provider_type to the factory, so the
        mock provider must never be instantiated/called through the WS path.
        """
        app, _tmp_home = mock_server
        instances_before = len(MockProvider._instances)
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                # Create session
                new_session(ws)

                # Attempt to configure the mock provider (will NOT be applied)
                ws.send_json({
                    "command": "apply_config",
                    "config": {
                        "provider_type": "mock",
                        "provider_id": "",
                        "api_key": "mock-key",
                        "model": "mock-model",
                    },
                })
                poll_for_type(ws, "config_changed", timeout=5.0)

                # Continue session — expect an error status_message
                ws.send_json({
                    "command": "continue_session",
                    "query": "Hello, mock provider!",
                })
                messages = poll_for_type(ws, "status_message", timeout=8.0)

        status_msgs = [m for m in messages if m.get("type") == "status_message"]
        assert len(status_msgs) > 0, (
            f"No status_message received. All messages: "
            f"{[m.get('type') for m in messages]}"
        )
        error_related = any(
            "error" in m.get("text", "").lower()
            or "api" in m.get("text", "").lower()
            or "key" in m.get("text", "").lower()
            or "fail" in m.get("text", "").lower()
            or "invalid" in m.get("text", "").lower()
            or "not configured" in m.get("text", "").lower()
            for m in status_msgs
        )
        assert error_related, (
            f"No error-related status_message found. All status messages: "
            f"{[m.get('text') for m in status_msgs]}"
        )

        # The mock provider must NOT have been instantiated by this flow
        assert len(MockProvider._instances) == instances_before, (
            f"MockProvider instances grew from {instances_before} to "
            f"{len(MockProvider._instances)} — the mock must not be used"
        )

    def test_apply_config_permissions_replace(self, mock_server):
        """
        apply_config with partial session_permissions REPLACES the whole
        permissions dict (assignment semantics, not deep-merge).
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

        # session_permissions is replaced wholesale (not deep-merged)
        perms = last_config.get("session_permissions", {})
        assert perms == {"filesystem": "write"}, (
            f"Expected session_permissions to be replaced by "
            f"{{'filesystem': 'write'}}, got {perms}"
        )

    def test_model_override_ignored(self, mock_server):
        """
        model_override is NOT applied by apply_config (only model is mutable);
        the user's model value wins.
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

        # model_override is ignored → the user's model value wins
        assert last_config.get("model") == "gpt-3.5-turbo", (
            f"Expected model=gpt-3.5-turbo (model_override ignored), "
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
        # api_key is never persisted, so api_key_configured is False
        assert last_config.get("api_key_configured") is False

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

def test_provider_factory_instantiates_mock_provider():
    """
    Unit-level: ProviderFactory.create_provider(provider_type='mock') must
    build a MockProvider from keyword config and serve chat_completion.
    This proves the mock mechanism works when provider_type actually reaches
    the factory (which WS apply_config can no longer do).
    """
    _register_mock_provider()
    provider = ProviderFactory.create_provider(
        provider_type="mock",
        api_key="mock-key",
        model="mock-model",
        base_url="https://mock.local/v1",
    )
    assert isinstance(provider, MockProvider)
    response = provider.chat_completion([{"role": "user", "content": "hello"}])
    assert response.provider == "mock"
    assert provider.call_count == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--timeout=30"])
