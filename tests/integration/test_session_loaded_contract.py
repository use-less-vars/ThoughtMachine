"""Contract test — the `load_session` WS response shape (frontend Test A).

This test is a SINGLE, PASSING response-shape contract: it drives the real
server via REST + WebSocket and pins down the shape of the events the
frontend receives when a session is loaded — NOT the persisted SessionConfig
internals (those belong to the session-creation contract tests).

Event-order facts this test relies on (verified against current code):

  * ``conversation_changed`` is always sent BEFORE ``session_loaded`` when a
    session is loaded — never assume ``session_loaded`` is the first event:
      - fresh-bridge path: ``bridge.load_session`` broadcasts
        ``conversation_changed`` (web_ui/backend/bridge.py:1653) and only then
        ``session_loaded`` (web_ui/backend/bridge.py:1665);
      - cached-bridge reconnect path: the server sends ``conversation_changed``
        directly to the new websocket (web_ui/backend/server.py:1244) before
        the fallback ``session_loaded`` block (web_ui/backend/server.py:1344).
    Note that ``conversation_changed`` is NOT necessarily the very first event
    in the fresh-bridge path: the server's own post-load ``tokens_updated``
    send (web_ui/backend/server.py:1356-1360) runs directly inside the
    load_session handler and reaches the client ahead of the loop-scheduled
    bridge broadcasts (the bridge's broadcasts go through
    ``asyncio.run_coroutine_threadsafe``, server.py:521-533, so they are
    deferred until the handler yields).  The ordering contract asserted below
    is therefore: ``conversation_changed`` precedes ``session_loaded`` and
    carries the ``messages`` list — not that it is ``events[0]``.
    The helper below therefore DRAINS events until ``session_loaded`` arrives
    and returns everything it saw, so ordering assertions stay explicit.

  * There is NO ``state_changed`` event after ``session_loaded`` (Fix 2a
    removed it): live running-state is embedded in the ``session_loaded``
    payload itself as ``is_running``, which mirrors the old state_changed
    semantics (web_ui/backend/bridge.py:1671-1674).

  * ``session_name`` is never empty: the bridge defaults to
    ``'Untitled Session'`` when the session has no name
    (web_ui/backend/bridge.py:1667) — this guards the S3-2 regression.

Config key names asserted inside ``session_loaded.config`` (the frontend
config dict produced by ``frontend_config_from_bridge`` /
``get_frontend_config``, web_ui/backend/config_manager.py:183-221, 401-409):

  * ``mode``            — session mode ("custom" here; config_manager.py:214-215)
  * ``provider``        — mapped from the backend ``provider_type`` via
                          {"openai": "openai", "anthropic": "anthropic",
                           "openai_compatible": "local"}.get(provider_type, "local")
                          (config_manager.py:233-239) — ALWAYS present
  * ``model``           — model name (may legitimately be "" under a hermetic
                          vault with no global defaults)
  * ``session_permissions`` — the permission matrix (NOT "permissions", which
                          only exists at the config_changed EVENT level,
                          web_ui/backend/server.py:1369-1375)

The contract asserts KEY PRESENCE only — these keys always exist, even when
their values are empty under a hermetic vault.

Run (from repo root):
    python -m pytest tests/integration/test_session_loaded_contract.py -v

Hermetic: temp HOME + patched ``Path.home()`` + purged/re-imported server
modules, exactly like tests/web_ui/backend/test_websocket_integration.py.
No network, no LLM, no Docker daemon involved.
"""

from __future__ import annotations

import importlib
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


# ═════════════════════════════════════════════════════════════════════════════
# Hermetic full-server harness
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def contract_server():
    """Temp HOME + purged modules + fresh import of web_ui.backend.server.

    Module-scoped so the test shares one hermetic vault/store, mirroring the
    `pathed_server` fixture in tests/web_ui/backend/test_websocket_integration.py.
    """
    tmp_home = tempfile.mkdtemp(prefix="test_session_loaded_contract_")
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


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════

def _receive_until_session_loaded(ws, path_label: str, max_events: int = 25, timeout: float = 15.0):
    """Drain WS events until ``session_loaded`` arrives — hang-proof.

    ``conversation_changed`` is always broadcast/sent BEFORE ``session_loaded``
    (bridge.py:1653/1665; server.py:1244/1344), but it is not necessarily the
    FIRST event: the fresh-bridge path can emit ``tokens_updated`` first
    (server.py:1356-1360 — the server's direct post-load send races ahead of
    the loop-scheduled bridge broadcasts).  Never assume ``session_loaded`` is
    the first event.  Returns ``(session_loaded, events)`` where ``events`` is
    every event drained (including the session_loaded one).

    Hang-proofing: ``ws.receive_json()`` has NO built-in timeout (it blocks
    forever on a silent server), so each read runs in a short-lived daemon
    thread that posts into a ``queue.Queue``; the main thread waits at most
    ``timeout`` seconds.  On timeout the test FAILS with what it saw so far
    — it can never hang the suite.  Leftover daemon threads are harmless.

    Fail-fast: server-side failure paths never emit the expected
    ``conversation_changed``/``session_loaded`` pair — the generic command
    handler (server.py:1957-1963) replies with a ``status_message`` containing
    "⚠ Internal error", and the silent store-miss path (bridge.py:1582-1584
    + server.py:1270, which sets ``_bridge_loaded_session = True`` regardless)
    emits only the success-looking ``status_message`` "Session ... loaded. Click
    Run to continue." with no ``session_loaded`` at all.  Both are detected
    below and failed immediately instead of waiting out the timeout.
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


# ═════════════════════════════════════════════════════════════════════════════
# Test A: load_session response shape contract (single, PASSING)
# ═════════════════════════════════════════════════════════════════════════════

def test_load_session_response_shape_contract(contract_server):
    """REST-created session, loaded via WS: frontend response-shape contract."""
    app, _ = contract_server

    with TestClient(app) as client:
        # Create the session over REST so the WS path is a pure load.
        resp = client.post("/api/session/create", json={"mode": "custom"})
        assert resp.status_code == 200, f"create failed: {resp.status_code} {resp.text}"
        body = resp.json()
        session_id = body["session_id"]
        assert body["mode"] == "custom"

        # Load it over WS and drain until session_loaded arrives.
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"command": "load_session", "session_id": session_id})
            evt, events = _receive_until_session_loaded(ws, "REST create → WS load_session")

    # --- Event order: conversation_changed precedes session_loaded (never
    #     assume session_loaded is first). The fresh-bridge path may emit
    #     tokens_updated first (server.py:1356-1360 — the server's direct
    #     post-load send races ahead of the loop-scheduled bridge broadcasts),
    #     so we locate conversation_changed among the drained events instead
    #     of asserting it is events[0]. ---
    assert events, "expected at least one event before session_loaded"
    conv_events = [e for e in events if e.get("type") == "conversation_changed"]
    assert conv_events, (
        f"expected a conversation_changed event before session_loaded "
        f"(got types: {[e.get('type') for e in events]})"
    )
    conv = conv_events[0]
    # Fresh-bridge path may emit tokens_updated (server.py:1356-1360 — the
    # server's direct post-load send, which can reach the client before the
    # loop-scheduled bridge broadcasts) ahead of conversation_changed; the
    # contract is conversation_changed precedes session_loaded and carries
    # the messages list.
    assert events.index(conv) < events.index(evt), (
        f"conversation_changed must precede session_loaded "
        f"(got types: {[e.get('type') for e in events]})"
    )
    assert isinstance(conv.get("messages"), list), (
        f"conversation_changed.messages must be a list "
        f"(got {type(conv.get('messages')).__name__})"
    )

    # --- session_loaded payload shape. ---
    assert evt.get("type") == "session_loaded", (
        f"expected session_loaded, got {evt.get('type')!r}"
    )
    assert evt.get("session_id") == session_id, (
        f"session_loaded.session_id {evt.get('session_id')!r} != {session_id!r}"
    )

    # S3-2 guard: session_name is never empty (bridge defaults 'Untitled Session').
    session_name = evt.get("session_name")
    assert isinstance(session_name, str) and session_name, (
        f"session_loaded.session_name must be a non-empty string (got {session_name!r})"
    )

    # Fix 2a: no separate state_changed after load — live running-state is
    # embedded as is_running in the session_loaded payload itself.
    assert isinstance(evt.get("is_running"), bool), (
        f"session_loaded.is_running must be a bool (got {evt.get('is_running')!r})"
    )

    # The frontend config dict must carry the four semantic keys (presence
    # contract only — values may be empty under a hermetic vault).
    fe_config = evt.get("config")
    assert isinstance(fe_config, dict), (
        f"session_loaded.config must be a dict (got {type(fe_config).__name__}: {fe_config!r})"
    )
    required_keys = ("mode", "provider", "model", "session_permissions")
    missing = [key for key in required_keys if key not in fe_config]
    assert not missing, (
        f"session_loaded.config missing required key(s) {missing}; "
        f"present keys: {sorted(fe_config.keys())}"
    )
