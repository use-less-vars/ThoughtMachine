"""
Integration test for the WebSocket backend.

Tests the full lifecycle:
1. new_session      → 6 events (session_loaded, state_changed, tokens_updated,
                        context_updated, config_changed, status_message)
2. continue_session → status_message about API key error (no key configured)
3. apply_config     → config_changed with api_key_configured: True
"""
from __future__ import annotations

import json
import os
import tempfile
import pathlib
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


# Module-level single-threaded executor for WebSocket reads.
# NOT wrapped in a ``with`` block — the executor lives for the full
# process lifetime so that a timed-out ``ws.receive_text()`` thread
# doesn't block cleanup.
_receive_pool = ThreadPoolExecutor(max_workers=1)
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient


# ══════════════════════════════════════════════════════════════════════════════
# Fixture: patched environment + server import
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def pathed_server():
    """Create temp HOME, patch Path.home() + HOME env, import server."""
    import importlib
    import sys as sys_mod

    # ── 1. Create temp home ──────────────────────────────────────────────
    tmp_home = tempfile.mkdtemp(prefix="test_webui_home_")
    fake_home_path = Path(tmp_home)

    # ── 2. Set HOME env var (for os.path.expanduser in session store) ────
    old_home_env = os.environ.get("HOME")
    os.environ["HOME"] = tmp_home

    # ── 3. Clear API key env vars ────────────────────────────────────────
    saved_env = {}
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_COMPATIBLE_API_KEY"):
        saved_env[key] = os.environ.pop(key, None)

    # ── 4. Start persistent Path.home() patch ────────────────────────────
    patcher = patch.object(pathlib.Path, "home", return_value=fake_home_path)
    patcher.start()

    # ── 5. Remove affected modules from cache & re-import ───────────────
    mod_prefixes = ("web_ui.backend", "agent.config.provider_profile",
                    "thoughtmachine.bootstrap")
    for mod_name in list(sys_mod.modules.keys()):
        if any(mod_name.startswith(p) for p in mod_prefixes):
            del sys_mod.modules[mod_name]

    server_mod = importlib.import_module("web_ui.backend.server")
    app = server_mod.app

    yield app, tmp_home

    # ── 6. Cleanup ───────────────────────────────────────────────────────
    patcher.stop()
    if old_home_env is not None:
        os.environ["HOME"] = old_home_env
    else:
        os.environ.pop("HOME", None)
    for key, val in saved_env.items():
        if val is not None:
            os.environ[key] = val

    import shutil
    shutil.rmtree(tmp_home, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def recv_n(ws, n: int, timeout: float = 5.0) -> list:
    """
    Receive exactly *n* text messages from the WebSocket.
    Uses a thread pool to enforce a real wall-clock timeout.
    """
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
    """
    Receive messages until one of type ``expected_type`` is found.
    Uses a thread pool to enforce a real wall-clock timeout.
    """
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


# ══════════════════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestWebSocketLifecycle:

    def test_new_session_emits_six_events(self, pathed_server):
        """new_session → 6 events in order."""
        app, tmp_home = pathed_server

        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"command": "new_session"})
                messages = recv_n(ws, 6, timeout=5.0)

        assert len(messages) == 6, (
            f"Expected 6 messages from new_session, got {len(messages)}: "
            f"{[m.get('type') for m in messages]}"
        )

        expected_types = [
            "session_loaded",
            "state_changed",
            "tokens_updated",
            "context_updated",
            "config_changed",
            "status_message",
        ]
        actual_types = [m["type"] for m in messages]
        assert actual_types == expected_types, (
            f"Expected types {expected_types}, got {actual_types}"
        )

        # ── session_loaded ───────────────────────────────────────────────
        sl = messages[0]
        assert sl["type"] == "session_loaded"
        assert isinstance(sl["session_id"], str) and len(sl["session_id"]) > 0
        assert isinstance(sl["session_name"], str)

        # ── state_changed ────────────────────────────────────────────────
        sc = messages[1]
        assert sc["type"] == "state_changed"
        assert sc["state"] == "IDLE"
        assert sc["is_running"] is False

        # ── tokens_updated ───────────────────────────────────────────────
        tu = messages[2]
        assert tu["type"] == "tokens_updated"
        assert tu["input"] == 0
        assert tu["output"] == 0

        # ── context_updated ──────────────────────────────────────────────
        cu = messages[3]
        assert cu["type"] == "context_updated"
        assert cu["context_length"] == 0

        # ── config_changed ───────────────────────────────────────────────
        cc = messages[4]
        assert cc["type"] == "config_changed"
        assert isinstance(cc["config"], dict)

        # ── status_message ───────────────────────────────────────────────
        sm = messages[5]
        assert sm["type"] == "status_message"
        assert "Ready" in sm["text"]

    def test_continue_session_without_key_emits_error(self, pathed_server):
        """continue_session without API key → status_message about error."""
        app, tmp_home = pathed_server

        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"command": "new_session"})
                recv_n(ws, 6, timeout=5.0)  # drain new_session responses

                ws.send_json({"command": "continue_session", "query": "Hello"})
                messages = poll_for_type(ws, "status_message", timeout=6.0)

        # Find all status_message entries in the collected messages
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

    def test_apply_config_with_api_key(self, pathed_server):
        """apply_config with an API key → config_changed with api_key_configured: True."""
        app, tmp_home = pathed_server

        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"command": "new_session"})
                recv_n(ws, 6, timeout=5.0)  # drain

                ws.send_json({
                    "command": "apply_config",
                    "config": {
                        "api_key": "sk-test-key-12345",
                        "provider": "openai",
                        "model": "gpt-4",
                    },
                })

                # Receive at most 5 messages looking for config_changed
                messages = poll_for_type(ws, "config_changed", timeout=5.0)

        config_msgs = [m for m in messages if m.get("type") == "config_changed"]
        assert len(config_msgs) > 0, (
            f"No config_changed received. All messages: "
            f"{[m.get('type') for m in messages]}"
        )

        last_config = config_msgs[-1]["config"]
        assert last_config.get("api_key_configured") is True, (
            f"Expected api_key_configured=True, got api_key_configured="
            f"{last_config.get('api_key_configured')!r}. Full config: {last_config}"
        )
