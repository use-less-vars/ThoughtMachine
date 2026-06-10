"""
Integration test for the full first-run WebSocket lifecycle.

Tests:
1. new_session      → 6 events (session_loaded, state_changed, tokens_updated,
                        context_updated, config_changed, status_message)
2. continue_session → status_message about API key error (no key configured)
3. apply_config     → config_changed with api_key_configured: True
"""
from __future__ import annotations

import json
import os
import pathlib
import sys as sys_mod
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


# Module-level single-threaded executor for WebSocket reads.
# NOT wrapped in a ``with`` block so timed-out threads don't block cleanup.
_receive_pool = ThreadPoolExecutor(max_workers=1)
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient


@pytest.fixture(scope="module")
def clean_home():
    """Create temp HOME, patch Path.home() + HOME env, clear API keys."""
    import importlib

    # ── 1. Create temp home ──────────────────────────────────────────────
    tmp_home = tempfile.mkdtemp(prefix="test_webui_home_")
    fake_home_path = Path(tmp_home)

    # ── 2. Set HOME env var ──────────────────────────────────────────────
    old_home_env = os.environ.get("HOME")
    os.environ["HOME"] = tmp_home

    # ── 3. Clear API key env vars ────────────────────────────────────────
    saved_env = {}
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_COMPATIBLE_API_KEY", "ANTHROPIC_API_KEY"):
        saved_env[key] = os.environ.pop(key, None)

    # ── 4. Start persistent Path.home() patch ────────────────────────────
    patcher = patch.object(pathlib.Path, "home", return_value=fake_home_path)
    patcher.start()

    # ── 5. Remove affected modules from cache & re-import ────────────────
    mod_prefixes = ("web_ui.backend", "agent.config.provider_profile",
                    "thoughtmachine.bootstrap")
    for mod_name in list(sys_mod.modules.keys()):
        if any(mod_name.startswith(p) for p in mod_prefixes):
            del sys_mod.modules[mod_name]

    # Ensure the project root is on sys.path so web_ui can be found
    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _project_root not in sys_mod.path:
        sys_mod.path.insert(0, _project_root)

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


@pytest.fixture
def client(clean_home):
    """Yield a TestClient wrapping the patched server app."""
    app, _ = clean_home
    with TestClient(app) as c:
        yield c


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


# ══════════════════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════════════════

def test_full_first_run_lifecycle(client):
    """Verify the complete first-run flow: new session, missing API key handled, config updated."""
    # All steps use a single WebSocket connection because bridge state
    # (session, config) is per-connection.
    with client.websocket_connect("/ws") as ws:
        # ═══ Step 1: new_session emits 6 events ═══
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

        # Extract session_id from session_loaded
        session_id = messages[0]["session_id"]
        assert isinstance(session_id, str) and len(session_id) > 0

        # Verify state_changed content
        assert messages[1]["state"] == "IDLE"
        assert messages[1]["is_running"] is False

        # Verify config_changed content
        assert "config" in messages[4]

        # ═══ Step 2: continue_session without API key → error ═══
        ws.send_json({
            "command": "continue_session",
            "query": "Hello",
        })

        # Poll for error about missing API key — server must not crash
        messages = poll_for_type(ws, "status_message", timeout=6.0)

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

        # ═══ Step 3: new_session to reset bridge state, then apply config with API key ═══
        # (Failed start in step 2 left the controller in a bad state; a fresh
        # session clears it, matching real frontend behavior.)
        ws.send_json({"command": "new_session"})
        recv_n(ws, 6, timeout=5.0)  # drain new_session events

        ws.send_json({
            "command": "apply_config",
            "config": {
                "provider_type": "openai_compatible",
                "api_key": "sk-dummy-test-key",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-chat",
                "temperature": 0.7,
            },
        })

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
