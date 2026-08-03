"""Contract tests — WebSocket event payload shapes (frontend Test F3).

Ten PASSING payload-contract tests that drive the real server via REST +
WebSocket (except test 7, which drives ``WebAgentBridge._map_and_emit``
directly) and pin down the exact event payloads the frontend consumes.

The harness mirrors ``tests/integration/test_session_loaded_contract.py``
EXACTLY: same module-scoped ``contract_server`` fixture (temp HOME + patched
``Path.home()`` + purged/re-imported ``web_ui.backend`` modules) and the same
hang-proof ``_receive_until_session_loaded`` drain helper.

DELTAS vs the session-loaded template (all verified against current code):

  * Generic drain helper ``_receive_until`` — drains until an arbitrary
    ``target_type`` instead of ``session_loaded``.  It still fails fast on
    ``error`` events (unless the target IS ``error``) and on failure-looking
    status_message texts (⚠/failed/internal error), but it does NOT fail on
    "loaded"/"not found" — the load_session tail status_message
    "Session ... loaded. Click Run to continue." (server.py:1458) legitimately
    arrives after the bridge broadcasts and must be skipped when the target is
    a later event (e.g. ``config_changed``, ``providers_list``).

  * ``providers_list`` (server.py:1057-1097) — real payload keys per provider:
    id/label/provider_type/base_url/api_key/default_model/models/timeout (NOT
    the table's id/name/models).  The ``session_id`` field IS present (F2
    guard: non-null after a load).  The test seeds
    ``$HOME/.thoughtmachine/providers.json`` BEFORE the TestClient starts
    (ProviderManager is instantiated per command, server.py:1071).

  * ``tools_list`` (server.py:1207-1234) — entries are {name, description}
    only; NO ``session_id`` in the payload and NO ``enabled`` key per tool
    (the enabled-state lives in ``config_changed.config.tools`` instead).

  * ``session_stop`` is NOT a WebSocket event (bridge.py:2083-2106): the
    controller-internal stop maps to a ``state_changed`` IDLE broadcast and a
    final ``conversation_changed`` — never a session_stop payload.  Test 7
    proves this via a direct ``_map_and_emit`` call on a fresh bridge.

  * ``session_cleared`` (bridge.py:1818) carries NO session_id — the
    EventForwarder ``broadcast`` (event_forwarder.py:66-74) never injects a
    routing session_id into the payload, so state_changed /
    conversation_changed / session_cleared all carry NO session_id.

  * ``config_changed`` carries NO session_id and ``permissions`` is a
    TOP-LEVEL key (server.py:1451-1457), alongside ``settings`` and
    ``merged_config`` — the frontend config dict itself uses
    ``session_permissions``.

Run (from repo root):
    python -m pytest tests/integration/test_ws_event_contracts.py -v

Hermetic: temp HOME + patched ``Path.home()`` + purged/re-imported server
modules, exactly like tests/integration/test_session_loaded_contract.py.
No network, no LLM, no Docker daemon involved (test 2 seeds providers.json
with a fake local provider; test 6 force-fails apply_config via a patch).
"""

from __future__ import annotations

import importlib
import json
import os
import pathlib
import queue
import shutil
import sys as sys_mod
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

pytestmark = pytest.mark.integration


# ════════════════════════════════════════════════════════════════════════════
# Hermetic full-server harness (EXACT mirror of test_session_loaded_contract.py)
# ════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def contract_server():
    """Temp HOME + purged modules + fresh import of web_ui.backend.server.

    Module-scoped so the test shares one hermetic vault/store, mirroring the
    `pathed_server` fixture in tests/web_ui/backend/test_websocket_integration.py.
    """
    tmp_home = tempfile.mkdtemp(prefix="test_ws_event_contracts_")
    fake_home_path = Path(tmp_home)

    old_home_env = os.environ.get("HOME")
    os.environ["HOME"] = tmp_home

    saved_env = {}
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_COMPATIBLE_API_KEY"):
        saved_env[key] = os.environ.pop(key, None)

    patcher = patch.object(pathlib.Path, "home", return_value=fake_home_path)
    patcher.start()

    # Re-import server so module-level singletons (_session_store, registries)
    # are built against the temp HOME, not the real one. "session" is purged too:
    # FileSystemSessionStore._instance is a process-global class singleton that an
    # earlier test module may have created under ITS temp HOME (which is deleted at
    # teardown); a stale singleton would make REST create write to the old HOME while
    # the WS load_session path scans the current one, so the lookup would miss.
    mod_prefixes = ("web_ui.backend", "agent.config.provider_profile", "thoughtmachine.bootstrap", "session")
    for mod_name in list(sys_mod.modules.keys()):
        if any(mod_name.startswith(p) for p in mod_prefixes):
            del sys_mod.modules[mod_name]

    server_mod = importlib.import_module("web_ui.backend.server")
    app = server_mod.app

    yield app, tmp_home

    patcher.stop()
    if old_home_env is not None:
        os.environ["HOME"] = old_home_env
    else:
        os.environ.pop("HOME", None)
    for key, val in saved_env.items():
        if val is not None:
            os.environ[key] = val
    shutil.rmtree(tmp_home, ignore_errors=True)


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

def _receive_until_session_loaded(ws, path_label: str, max_events: int = 25, timeout: float = 15.0):
    """Drain WS events until ``session_loaded`` arrives — hang-proof.

    EXACT mirror of the helper in tests/integration/test_session_loaded_contract.py:
    the fresh-bridge load path broadcasts conversation_changed + session_loaded
    (bridge.py:1653/1665) which reach the client before the server's direct
    post-load sends; the server never sends its own session_loaded on that path
    (_bridge_loaded_session = True).  Never assume session_loaded is the first
    event.  Returns ``(session_loaded, events)`` with every event drained.

    Hang-proofing: each ``ws.receive_json()`` runs in a short-lived daemon
    thread posting into a ``queue.Queue``; the main thread waits at most
    ``timeout`` seconds and FAILS on timeout — it can never hang the suite.

    Fail-fast: server-side failure paths never emit the expected
    ``session_loaded`` (the generic command handler replies with a
    status_message containing "⚠ Internal error"; the silent store-miss path
    emits only the success-looking status_message "Session ... loaded. Click
    Run to continue." with no session_loaded at all).  Both are detected below.
    """
    events = []
    for _ in range(max_events):
        _box = queue.Queue(maxsize=1)

        def _receive_one(_box=_box):
            try:
                _box.put(("ok", ws.receive_json()))
            except Exception as exc:  # pragma: no cover — defensive
                _box.put(("exc", exc))

        _thread = threading.Thread(target=_receive_one, daemon=True)
        _thread.start()
        try:
            _kind, _val = _box.get(timeout=timeout)
        except queue.Empty:
            pytest.fail(
                f"{path_label}: timed out after {timeout}s waiting for events; "
                f"received so far: {[e.get('type') for e in events]}"
            )
        if _kind == "exc":
            raise _val
        evt = _val
        events.append(evt)
        if evt.get("type") == "error":
            pytest.fail(f"{path_label}: received error event: {evt}")
        if evt.get("type") == "status_message":
            _text = str(evt.get("text", ""))
            if any(m in _text.lower() for m in ("⚠", "failed", "internal error", "not found", "loaded")):
                pytest.fail(
                    f"{path_label}: server reported failure before session_loaded: {_text!r}"
                )
        if evt.get("type") == "session_loaded":
            return evt, events
    pytest.fail(
        f"{path_label}: no session_loaded within {max_events} events "
        f"(got types: {[e.get('type') for e in events]})"
    )


def _receive_until(ws, target_type: str, path_label: str, max_events: int = 25, timeout: float = 15.0):
    """Drain WS events until an event of ``target_type`` arrives — hang-proof.

    Same thread+queue machinery as ``_receive_until_session_loaded`` but for an
    arbitrary target.  DELTAS vs the session-loaded helper:

      * On an ``error`` event: if the target IS ``error``, return it; otherwise
        fail fast (an unexpected error means the trigger command failed).
      * On ``status_message``: fail fast ONLY on failure-looking texts
        ("⚠", "failed", "internal error").  The load_session tail status
        "Session ... loaded. Click Run to continue." (server.py:1458) is
        legitimately SKIPPED when draining for a later target, and the
        apply_config success status "✅ Switched to project: ..."
        (server.py:948-951) is skipped too.

    Returns ``(target_event, events)`` where ``events`` is everything drained
    including the target event.
    """
    events = []
    for _ in range(max_events):
        _box = queue.Queue(maxsize=1)

        def _receive_one(_box=_box):
            try:
                _box.put(("ok", ws.receive_json()))
            except Exception as exc:  # pragma: no cover — defensive
                _box.put(("exc", exc))

        _thread = threading.Thread(target=_receive_one, daemon=True)
        _thread.start()
        try:
            _kind, _val = _box.get(timeout=timeout)
        except queue.Empty:
            pytest.fail(
                f"{path_label}: timed out after {timeout}s waiting for {target_type!r}; "
                f"received so far: {[e.get('type') for e in events]}"
            )
        if _kind == "exc":
            raise _val
        evt = _val
        events.append(evt)
        if evt.get("type") == "error":
            if target_type == "error":
                return evt, events
            pytest.fail(f"{path_label}: received unexpected error event: {evt}")
        if evt.get("type") == "status_message":
            _text = str(evt.get("text", ""))
            if any(m in _text.lower() for m in ("⚠", "failed", "internal error")):
                pytest.fail(
                    f"{path_label}: server reported failure while waiting for "
                    f"{target_type!r}: {_text!r}"
                )
        if evt.get("type") == target_type:
            return evt, events
    pytest.fail(
        f"{path_label}: no {target_type} within {max_events} events "
        f"(got types: {[e.get('type') for e in events]})"
    )


# ════════════════════════════════════════════════════════════════════════════
# F3: WS event payload contracts (10 tests)
# ════════════════════════════════════════════════════════════════════════════

def test_config_changed_payload_contract(contract_server):
    """config_changed after load_session: config + top-level settings/permissions."""
    app, _ = contract_server

    with TestClient(app) as client:
        resp = client.post("/api/session/create", json={"mode": "custom"})
        assert resp.status_code == 200, f"create failed: {resp.status_code} {resp.text}"
        session_id = resp.json()["session_id"]

        with client.websocket_connect("/ws") as ws:
            ws.send_json({"command": "load_session", "session_id": session_id})
            _receive_until_session_loaded(ws, "REST create → WS load_session")
            # config_changed is the server's direct post-load send (server.py:1451-1457);
            # the "loaded" status_message follows it and is skipped by the generic drain.
            evt, _ = _receive_until(ws, "config_changed", "load_session → config_changed")

    assert evt.get("type") == "config_changed"
    # No routing session_id in the payload (server.py:1451-1457 sends none).
    assert "session_id" not in evt, (
        f"config_changed must not carry a session_id (got keys: {sorted(evt.keys())})"
    )

    fe_config = evt.get("config")
    assert isinstance(fe_config, dict), (
        f"config_changed.config must be a dict (got {type(fe_config).__name__}: {fe_config!r})"
    )
    # Frontend config keys: mode/provider/model/session_permissions (the
    # permission matrix is NESTED inside config as session_permissions).
    required_keys = ("mode", "provider", "model", "session_permissions")
    missing = [key for key in required_keys if key not in fe_config]
    assert not missing, (
        f"config_changed.config missing required key(s) {missing}; "
        f"present keys: {sorted(fe_config.keys())}"
    )
    # permissions is TOP-LEVEL alongside settings/merged_config (server.py:1451-1457).
    for top_key in ("settings", "permissions", "merged_config"):
        assert top_key in evt, (
            f"config_changed missing top-level key {top_key!r} (got keys: {sorted(evt.keys())})"
        )
    if "tools" in fe_config:
        assert isinstance(fe_config["tools"], list), (
            f"config_changed.config.tools must be a list (got {type(fe_config['tools']).__name__})"
        )


def test_providers_list_payload_contract(contract_server):
    """providers_list: real provider keys + non-null session_id (F2 guard)."""
    app, tmp_home = contract_server

    # Seed providers.json BEFORE the TestClient starts (lifespan's
    # ensure_user_defaults does not touch providers.json).
    tm_dir = Path(tmp_home) / ".thoughtmachine"
    tm_dir.mkdir(parents=True, exist_ok=True)
    (tm_dir / "providers.json").write_text(
        json.dumps({
            "profiles": [{
                "id": "f3-provider",
                "label": "F3 Provider",
                "provider_type": "openai_compatible",
                "base_url": "http://localhost:9999",
                "api_key": "sk-test",
                "default_model": "f3-model",
                "models": ["f3-model"],
                "timeout": 30,
            }],
            "active_profile_id": "f3-provider",
        }),
        encoding="utf-8",
    )

    with TestClient(app) as client:
        resp = client.post("/api/session/create", json={"mode": "custom"})
        assert resp.status_code == 200, f"create failed: {resp.status_code} {resp.text}"
        session_id = resp.json()["session_id"]

        with client.websocket_connect("/ws") as ws:
            ws.send_json({"command": "load_session", "session_id": session_id})
            _receive_until_session_loaded(ws, "REST create → WS load_session")
            ws.send_json({"command": "get_providers"})
            evt, _ = _receive_until(ws, "providers_list", "get_providers → providers_list")

    assert evt.get("type") == "providers_list"
    # F2 guard: the reply routes to the right tab — session_id must be non-null
    # after a load (bridge._loaded_session is set; server.py:1064-1068).
    assert evt.get("session_id") is not None, (
        f"providers_list.session_id must not be None (got {evt.get('session_id')!r})"
    )
    providers = evt.get("providers")
    assert isinstance(providers, list), (
        f"providers_list.providers must be a list (got {type(providers).__name__})"
    )
    real_keys = {"id", "label", "provider_type", "base_url", "api_key",
                 "default_model", "models", "timeout"}
    if providers:
        for p in providers:
            assert isinstance(p, dict), f"each provider must be a dict (got {p!r})"
            missing = [k for k in real_keys if k not in p]
            assert not missing, f"provider {p.get('id')!r} missing key(s) {missing}"
    seeded = [p for p in providers if p.get("id") == "f3-provider"]
    assert seeded, f"expected seeded provider f3-provider in {providers!r}"
    assert seeded[0]["label"] == "F3 Provider"
    assert seeded[0]["default_model"] == "f3-model"
    assert seeded[0]["models"] == ["f3-model"]


def test_tools_list_payload_contract(contract_server):
    """tools_list: {name, description} entries; no session_id, no enabled key."""
    app, _ = contract_server

    with TestClient(app) as client:
        resp = client.post("/api/session/create", json={"mode": "custom"})
        assert resp.status_code == 200, f"create failed: {resp.status_code} {resp.text}"
        session_id = resp.json()["session_id"]

        with client.websocket_connect("/ws") as ws:
            ws.send_json({"command": "load_session", "session_id": session_id})
            _receive_until_session_loaded(ws, "REST create → WS load_session")
            ws.send_json({"command": "get_available_tools"})
            evt, _ = _receive_until(ws, "tools_list", "get_available_tools → tools_list")

    assert evt.get("type") == "tools_list"
    # No routing session_id (server.py:1224-1227 sends none).
    assert "session_id" not in evt, (
        f"tools_list must not carry a session_id (got keys: {sorted(evt.keys())})"
    )
    tools = evt.get("tools")
    assert isinstance(tools, list), (
        f"tools_list.tools must be a list (got {type(tools).__name__})"
    )
    assert tools, "expected at least one tool definition from SIMPLIFIED_TOOL_CLASSES"
    for tool in tools:
        assert isinstance(tool, dict), f"each tool must be a dict (got {tool!r})"
        name = tool.get("name")
        assert isinstance(name, str) and name, f"tool.name must be a non-empty string (got {name!r})"
        assert isinstance(tool.get("description"), str), (
            f"tool {name!r}: description must be a str (got {tool.get('description')!r})"
        )
        # enabled-state lives in config_changed.config.tools — never here.
        assert "enabled" not in tool, f"tool {name!r} must not carry an enabled key"


def test_state_changed_payload_contract(contract_server):
    """state_changed after close_session: {state, is_running}, no session_id."""
    app, _ = contract_server

    with TestClient(app) as client:
        resp = client.post("/api/session/create", json={"mode": "custom"})
        assert resp.status_code == 200, f"create failed: {resp.status_code} {resp.text}"
        session_id = resp.json()["session_id"]

        with client.websocket_connect("/ws") as ws:
            ws.send_json({"command": "load_session", "session_id": session_id})
            _receive_until_session_loaded(ws, "REST create → WS load_session")
            ws.send_json({"command": "close_session", "session_id": session_id})
            # The server's direct post-close state_changed (server.py:1649-1653)
            # and/or the bridge broadcast (bridge.py:1819-1822) — both are IDLE.
            evt, _ = _receive_until(ws, "state_changed", "close_session → state_changed")

    assert evt.get("type") == "state_changed"
    assert evt.get("state") in {"IDLE", "RUNNING", "PAUSED", "WAITING_FOR_USER"}, (
        f"state_changed.state {evt.get('state')!r} outside the documented enum"
    )
    assert isinstance(evt.get("is_running"), bool), (
        f"state_changed.is_running must be a bool (got {evt.get('is_running')!r})"
    )
    # EventForwarder.broadcast never injects a routing session_id (event_forwarder.py:66-74).
    assert "session_id" not in evt, (
        f"state_changed must not carry a session_id (got keys: {sorted(evt.keys())})"
    )


def test_conversation_changed_payload_contract(contract_server):
    """conversation_changed: messages list + total_count/has_more; no session_id."""
    app, _ = contract_server

    with TestClient(app) as client:
        resp = client.post("/api/session/create", json={"mode": "custom"})
        assert resp.status_code == 200, f"create failed: {resp.status_code} {resp.text}"
        session_id = resp.json()["session_id"]

        with client.websocket_connect("/ws") as ws:
            ws.send_json({"command": "load_session", "session_id": session_id})
            # The fresh-bridge load broadcasts conversation_changed FIRST
            # (bridge.py:1653-1657) with the full pagination envelope.
            evt, _ = _receive_until(ws, "conversation_changed", "load_session → conversation_changed")

    assert evt.get("type") == "conversation_changed"
    # No routing session_id (forwarder broadcast never injects one).
    assert "session_id" not in evt, (
        f"conversation_changed must not carry a session_id (got keys: {sorted(evt.keys())})"
    )
    messages = evt.get("messages")
    assert isinstance(messages, list), (
        f"conversation_changed.messages must be a list (got {type(messages).__name__})"
    )
    assert isinstance(evt.get("total_count"), int), (
        f"conversation_changed.total_count must be an int (got {evt.get('total_count')!r})"
    )
    assert isinstance(evt.get("has_more"), bool), (
        f"conversation_changed.has_more must be a bool (got {evt.get('has_more')!r})"
    )
    for msg in messages:
        assert isinstance(msg, dict), f"each message must be a dict (got {msg!r})"
        assert isinstance(msg.get("role"), str), (
            f"message.role must be a str (got {msg.get('role')!r})"
        )
        assert "content" in msg, (
            f"message missing content key (got keys: {sorted(msg.keys())})"
        )


def test_error_payload_contract(contract_server):
    """error after failed apply_config: session_id present + canonical message."""
    from web_ui.backend.bridge import WebAgentBridge

    app, _ = contract_server

    with TestClient(app) as client:
        resp = client.post("/api/session/create", json={"mode": "custom"})
        assert resp.status_code == 200, f"create failed: {resp.status_code} {resp.text}"
        session_id = resp.json()["session_id"]

        # Force bridge.apply_config to raise inside the Round C handler
        # (server.py:913 → except at server.py:952-968 sends the error event).
        with patch.object(WebAgentBridge, "apply_config",
                          side_effect=RuntimeError("F3 forced failure")):
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"command": "load_session", "session_id": session_id})
                _receive_until_session_loaded(ws, "REST create → WS load_session")
                ws.send_json({"command": "apply_config",
                              "config": {"workspace_path": "/tmp/f3-nonexistent-xyz"}})
                evt, _ = _receive_until(ws, "error", "apply_config → error")

    assert evt.get("type") == "error"
    # Round C failure path (server.py:958-966) always attaches the resolved
    # session id (bridge._loaded_session is set after load_session).
    assert "session_id" in evt, (
        f"error event must carry a session_id (got keys: {sorted(evt.keys())})"
    )
    assert evt.get("session_id") is not None, (
        f"error.session_id must not be None (got {evt.get('session_id')!r})"
    )
    assert evt.get("message") == "Failed to apply config", (
        f"error.message must be the canonical 'Failed to apply config' (got {evt.get('message')!r})"
    )


def test_session_stop_not_a_ws_event(contract_server):
    """session_stop is controller-internal — NO session_stop WS event ever."""
    # Direct bridge test (no WebSocket): a fresh bridge (no session) mapped with
    # a session_stop raw event must emit ONLY the IDLE state_changed broadcast —
    # the server/bridge never forward a session_stop payload to the frontend.
    from web_ui.backend.bridge import WebAgentBridge
    from session.store import FileSystemSessionStore

    bridge = WebAgentBridge(session_store=FileSystemSessionStore())
    events = []
    bridge.set_event_callback(events.append)

    bridge._map_and_emit({"type": "session_stop", "stop_reason": "completed"})

    stop_events = [e for e in events if e.get("type") == "session_stop"]
    assert not stop_events, (
        f"session_stop must never surface as a WS event (got {stop_events!r})"
    )
    state_events = [e for e in events if e.get("type") == "state_changed"]
    assert state_events, (
        f"expected a state_changed broadcast from session_stop (got {events!r})"
    )
    first = state_events[0]
    assert first.get("state") == "IDLE", (
        f"state_changed.state must be IDLE after session_stop (got {first.get('state')!r})"
    )
    assert isinstance(first.get("is_running"), bool), (
        f"state_changed.is_running must be a bool (got {first.get('is_running')!r})"
    )


def test_session_cleared_payload_contract(contract_server):
    """session_cleared after close_session: type-only payload, NO session_id."""
    app, _ = contract_server

    with TestClient(app) as client:
        resp = client.post("/api/session/create", json={"mode": "custom"})
        assert resp.status_code == 200, f"create failed: {resp.status_code} {resp.text}"
        session_id = resp.json()["session_id"]

        with client.websocket_connect("/ws") as ws:
            ws.send_json({"command": "load_session", "session_id": session_id})
            _receive_until_session_loaded(ws, "REST create → WS load_session")
            ws.send_json({"command": "close_session", "session_id": session_id})
            # bridge.close_session broadcasts session_cleared (bridge.py:1818);
            # the server's direct session_closed/state_changed arrive first and
            # are skipped by the generic drain.
            evt, _ = _receive_until(ws, "session_cleared", "close_session → session_cleared")

    assert evt.get("type") == "session_cleared"
    # Delta vs the protocol table: EventForwarder.broadcast never injects a
    # routing session_id into the payload (event_forwarder.py:66-74).
    assert "session_id" not in evt, (
        f"session_cleared must not carry a session_id (got keys: {sorted(evt.keys())})"
    )


def test_session_renamed_payload_contract(contract_server):
    """session_renamed: {session_id, new_name} broadcast to the loaded session."""
    app, _ = contract_server

    with TestClient(app) as client:
        resp = client.post("/api/session/create", json={"mode": "custom"})
        assert resp.status_code == 200, f"create failed: {resp.status_code} {resp.text}"
        session_id = resp.json()["session_id"]

        with client.websocket_connect("/ws") as ws:
            ws.send_json({"command": "load_session", "session_id": session_id})
            _receive_until_session_loaded(ws, "REST create → WS load_session")
            ws.send_json({"command": "rename_session",
                          "session_id": session_id, "new_name": "F3 Renamed"})
            # bridge._forwarder.broadcast (server.py:1543-1546) delivers the
            # session_renamed payload to every registered tab, including ours.
            evt, _ = _receive_until(ws, "session_renamed", "rename_session → session_renamed")

    assert evt.get("type") == "session_renamed"
    assert evt.get("session_id") == session_id, (
        f"session_renamed.session_id {evt.get('session_id')!r} != {session_id!r}"
    )
    assert evt.get("new_name") == "F3 Renamed", (
        f"session_renamed.new_name {evt.get('new_name')!r} != 'F3 Renamed'"
    )


def test_session_deleted_payload_contract(contract_server):
    """session_deleted: {session_id} direct send after delete_session."""
    app, _ = contract_server

    with TestClient(app) as client:
        resp = client.post("/api/session/create", json={"mode": "custom"})
        assert resp.status_code == 200, f"create failed: {resp.status_code} {resp.text}"
        session_id = resp.json()["session_id"]

        with client.websocket_connect("/ws") as ws:
            ws.send_json({"command": "load_session", "session_id": session_id})
            _receive_until_session_loaded(ws, "REST create → WS load_session")
            ws.send_json({"command": "delete_session", "session_id": session_id})
            # Direct server send (server.py:1509-1512) — arrives immediately.
            evt, _ = _receive_until(ws, "session_deleted", "delete_session → session_deleted")

    assert evt.get("type") == "session_deleted"
    assert evt.get("session_id") == session_id, (
        f"session_deleted.session_id {evt.get('session_id')!r} != {session_id!r}"
    )
