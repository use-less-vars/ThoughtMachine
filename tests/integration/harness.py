"""Hermetic test harness for ``web_ui.backend`` integration tests.

Restart simulation
------------------
``start_backend()`` redirects ``HOME`` to a throwaway directory, patches
``pathlib.Path.home`` to match, purges every app module from ``sys.modules``
and then imports ``web_ui.backend.server`` fresh.  The returned ``stop_fn``
restores the environment; calling ``stop_fn(rmtree=False, restore_env=False)``
keeps the fake HOME alive so a *second* import (``restart()``) simulates a
process restart against the same persisted ``~/.thoughtmachine`` state.

Isolation rules
---------------
* No ``web_ui.backend`` / ``session`` / app imports at module level - the
  backend is imported lazily inside ``start_backend`` / ``restart``.
* API-key environment variables are popped before import; only mock providers
  are registered (``register_mock_provider``) - never real credentials.
* WebSocket receives always go through a daemon thread + bounded queue so a
  dead socket cannot hang a test (see ``_receive_json``).
"""
from __future__ import annotations

import importlib
import os
import pathlib
import queue
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

_PURGE_PREFIXES = (
    "web_ui.backend",
    "agent.config.provider_profile",
    "thoughtmachine.bootstrap",
    "session",
)


def _purge_modules() -> None:
    """Drop every app module so the next import is a cold start."""
    for name in [m for m in sys.modules if m.startswith(_PURGE_PREFIXES)]:
        del sys.modules[name]


def _snapshot_provider_registry():
    """Shallow-copy the process-global ProviderFactory registry.

    ``llm_providers.factory`` is intentionally NOT purged by
    ``_purge_modules()`` (it is not under any purge prefix), so its
    ``ProviderFactory._providers`` dict survives module purges and is shared
    by every test file in the process.  Snapshot the clean baseline here so
    ``stop_fn`` can restore it after this test's mock registrations.
    """
    from llm_providers.factory import ProviderFactory

    return dict(ProviderFactory._get_providers())


def _restore_provider_registry(snapshot) -> None:
    """Reset the process-global ProviderFactory registry to ``snapshot``."""
    from llm_providers.factory import ProviderFactory

    providers = ProviderFactory._get_providers()
    providers.clear()
    providers.update(snapshot)


def start_backend(tmp_home=None):
    """Start an isolated backend.

    Returns ``(app, tmp_home, stop_fn)``.  ``stop_fn(rmtree=True,
    restore_env=True)`` tears everything down; pass ``rmtree=False`` and
    ``restore_env=False`` to keep the fake HOME alive for a restart.
    """
    if tmp_home is None:
        tmp_home = tempfile.mkdtemp(prefix="tm_home_")
    else:
        Path(tmp_home).mkdir(parents=True, exist_ok=True)
    old_home_env = os.environ.get("HOME")
    os.environ["HOME"] = tmp_home
    saved_env = {
        k: os.environ.pop(k, None)
        for k in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_COMPATIBLE_API_KEY")
    }
    patcher = patch.object(pathlib.Path, "home", return_value=Path(tmp_home))
    patcher.start()
    _purge_modules()
    server_mod = importlib.import_module("web_ui.backend.server")
    app = server_mod.app
    provider_snapshot = _snapshot_provider_registry()

    state = {"env_restored": False}

    def stop_fn(rmtree=True, restore_env=True):
        if restore_env and not state["env_restored"]:
            try:
                patcher.stop()
            except Exception:  # pragma: no cover - defensive
                pass
            if old_home_env is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home_env
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            _restore_provider_registry(provider_snapshot)
            state["env_restored"] = True
        if rmtree:
            shutil.rmtree(tmp_home, ignore_errors=True)

    return app, tmp_home, stop_fn


def restart(tmp_home):
    """Simulate a process restart against the same ``tmp_home``.

    Assumes the fake HOME is still active (i.e. ``stop_fn(rmtree=False,
    restore_env=False)`` was used); purges app modules and imports a fresh
    backend, which reloads the persisted ``~/.thoughtmachine`` state.
    """
    _purge_modules()
    server_mod = importlib.import_module("web_ui.backend.server")
    return server_mod.app


def make_client(app):
    return TestClient(app)


# ---------------------------------------------------------------------------
# REST helpers
# ---------------------------------------------------------------------------

def create_workspace(client, tmp_home, name="ws"):
    root = os.path.join(tmp_home, name)
    os.makedirs(root, exist_ok=True)
    resp = client.post(
        "/api/workspace", json={"path": root, "purpose": "general"}
    )
    assert resp.status_code in (200, 201), (
        f"create workspace failed: {resp.status_code} {resp.text}"
    )
    return resp.json()


def create_session(client, workspace_id=None, workspace_path=None):
    payload = {"mode": "custom"}
    if workspace_id is not None:
        payload["workspace_id"] = workspace_id
    if workspace_path is not None:
        payload["workspace_path"] = workspace_path
    resp = client.post("/api/session/create", json=payload)
    assert resp.status_code == 200, (
        f"create session failed: {resp.status_code} {resp.text}"
    )
    return resp.json()["session_id"]


def put_permissions(client, ws_id, permissions, allow_host_resources=None):
    payload = {"permissions": permissions}
    if allow_host_resources is not None:
        payload["allow_host_resources"] = allow_host_resources
    resp = client.put(f"/api/workspace/{ws_id}/permissions", json=payload)
    assert resp.status_code == 200, (
        f"put permissions failed: {resp.status_code} {resp.text}"
    )
    return resp.json()


def get_summary(client, ws_id):
    resp = client.get(f"/api/workspace/{ws_id}/summary")
    assert resp.status_code == 200, (
        f"workspace summary failed: {resp.status_code} {resp.text}"
    )
    return resp.json()


def get_effective_permissions(client, ws_id, session_id=None):
    url = f"/api/workspace/{ws_id}/effective_permissions"
    if session_id is not None:
        url += f"?session_id={session_id}"
    resp = client.get(url)
    assert resp.status_code == 200, (
        f"effective_permissions failed: {resp.status_code} {resp.text}"
    )
    return resp.json()


def get_providers(client):
    resp = client.get("/api/providers")
    assert resp.status_code == 200, (
        f"get providers failed: {resp.status_code} {resp.text}"
    )
    return resp.json()


def post_provider(client, **fields):
    resp = client.post("/api/providers", json={"provider": fields})
    assert resp.status_code in (200, 201), (
        f"post provider failed: {resp.status_code} {resp.text}"
    )
    return resp.json()


# ---------------------------------------------------------------------------
# WebSocket helpers (thread + queue; never a bare receive_json)
# ---------------------------------------------------------------------------

def _receive_json(ws, timeout=15.0):
    box = queue.Queue(maxsize=1)

    def _receive_one(_box=box):
        try:
            _box.put(("ok", ws.receive_json()))
        except Exception as exc:  # noqa: BLE001
            _box.put(("exc", exc))

    threading.Thread(target=_receive_one, daemon=True).start()
    kind, value = box.get(timeout=timeout)
    if kind == "exc":
        raise value
    return value


def ws_connect(client):
    return client.websocket_connect("/ws")


def receive_until_type(ws, target_type, timeout=15.0, max_events=25):
    """Receive events until ``target_type``; fails on error events."""
    events = []
    for _ in range(max_events):
        evt = _receive_json(ws, timeout=timeout)
        events.append(evt)
        if evt.get("type") == "error":
            pytest.fail(f"[{target_type}] received error event: {evt}")
        if evt.get("type") == target_type:
            return evt, events
    pytest.fail(
        f"no '{target_type}' event received; got types: "
        f"{[e.get('type') for e in events]}"
    )


_BAD_STATUS_MARKERS = ("⚠", "failed", "internal error", "not found", "loaded")


def receive_until_session_loaded(ws, timeout=15.0, max_events=25):
    """Receive until a ``session_loaded`` event (with status fail-fast)."""
    events = []
    for _ in range(max_events):
        evt = _receive_json(ws, timeout=timeout)
        events.append(evt)
        if evt.get("type") == "error":
            pytest.fail(f"received error event: {evt}")
        status = (evt.get("status_message") or "").lower()
        if any(marker in status for marker in _BAD_STATUS_MARKERS):
            pytest.fail(f"unexpected status_message in event: {evt}")
        if evt.get("type") == "session_loaded":
            return evt, events
    pytest.fail(
        "no session_loaded event received; got types: "
        f"{[e.get('type') for e in events]}"
    )


def drain_more(ws, events, n=3, timeout=5.0):
    """Best-effort drain of any trailing events (tolerates empty queue)."""
    for _ in range(n):
        try:
            events.append(_receive_json(ws, timeout=timeout))
        except queue.Empty:
            return events
    return events


# ---------------------------------------------------------------------------
# Mock provider
# ---------------------------------------------------------------------------

def register_mock_provider(server_mod=None, name="mock"):
    """Register a canned-response mock provider with ProviderFactory.

    Must be called again after every ``start_backend``/``restart`` because
    ``ProviderFactory`` is re-created on each import.  Returns the previously
    registered class (or None).
    """
    from llm_providers.base import LLMProvider, LLMResponse
    from llm_providers.factory import ProviderFactory

    class MockProvider(LLMProvider):
        def __init__(self, config):
            super().__init__(config)
            self.call_count = 0
            self.last_messages = None
            self.last_tools = None

        def chat_completion(self, messages, tools=None, **kwargs):
            self.call_count += 1
            self.last_messages = messages
            self.last_tools = tools
            return LLMResponse(
                content="This is a mock response from the test provider.",
                reasoning="",
                tool_calls=None,
                usage={"prompt_tokens": 10, "completion_tokens": 5},
                provider=name,
                model="mock-model",
            )

        def count_tokens(self, messages, tools=None):
            return 42

    providers = ProviderFactory._get_providers()
    prev = providers.get(name)
    ProviderFactory.register_provider(name, MockProvider)
    return prev
