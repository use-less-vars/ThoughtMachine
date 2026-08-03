"""State-transition contract tests — session lifecycle (Round J).

Drives the real server via REST + WebSocket (tests H–K) and the
``WebAgentBridge`` / ``AgentController`` directly (tests A–G) to pin down the
session lifecycle state transitions the frontend actually observes, as opposed
to the documented spec.

The harness mirrors ``tests/integration/test_ws_event_contracts.py`` EXACTLY:
same module-scoped ``contract_server`` fixture (temp HOME + patched
``Path.home()`` + purged/re-imported ``web_ui.backend`` modules) and the same
hang-proof thread+queue drain helpers.

DELTA TABLE — spec vs REAL behaviour (all verified against current code):

    spec               | real behaviour
    -------------------|--------------------------------------------------------
    1. start           | NO state_changed from the start_session handler
       IDLE→RUNNING    | (server.py:563-604); RUNNING arrives asynchronously via
                       | the agent's execution_state_change running
                       | (agent/core/agent.py:898-906) mapped by
                       | bridge._map_and_emit (bridge.py:1983-1999) to
                       | state_changed {"state":"RUNNING","is_running":_is_busy}.
    2. pause           | controller.pause() (controller/__init__.py:475-500)
       RUNNING→        | sets PAUSING + emits execution_state_change pausing →
       PAUSING→        | state_changed {"state":"PAUSING","is_running":True}.
       PAUSED          | The legacy raw 'paused' event maps to NOTHING
                       | (_map_and_emit has no branch for it, bridge.py:1952-
                       | 2112) and ExecutionState (agent/core/state.py) has NO
                       | 'paused' member — the 'PAUSED' mapping line
                       | (bridge.py:1987) is DEAD CODE. Completion: controller
                       | emits session_stop stop_reason='paused'
                       | (controller/__init__.py:376-399) → state_changed IDLE.
                       | REAL sequence: RUNNING → PAUSING → IDLE.
    3. resume          | controller.resume() (controller/__init__.py:502-514)
       PAUSED→         | emits NO events → no state_changed. RUNNING only
       RUNNING         | returns when the agent re-enters process_query
                       | (another execution_state_change running).
    4. stop → IDLE     | stop_session handler (server.py:687-690) sends only
                       | status "⏹ Stopped."; state_changed IDLE comes later
                       | from the agent's session_stop event via _map_and_emit
                       | (bridge.py:2083-2106) or the final stop_reason guard
                       | (bridge.py:2108-2112).
    5. wait_for_input  | only reachable via agent_responded with
       RUNNING→        | response_type=='question' → state_changed
       WAITING_FOR_USER| {"state":"WAITING_FOR_USER"} (bridge.py:2050-2067).
                       | No WS command exists for it.
    6. user_input      | no such WS command; continue_session (server.py:
       WAITING→        | 606-675) raises RuntimeError "Bridge is not running.
       RUNNING         | Start it first." (bridge.py:1109-1162) when not
                       | running, else resume() (no event) + queued query;
                       | RUNNING re-emitted by the agent later.

Also pinned: pause/resume/stop with NO running agent → only status_messages,
NO state_changed (server.py:677-690); close_session → the ONLY direct
state_changed, {"state":"IDLE","is_running":False} (server.py:1649-1653).

Run (from repo root):
    python -m pytest tests/integration/test_state_transitions.py -v

Hermetic: temp HOME + patched ``Path.home()`` + purged/re-imported server
modules, exactly like tests/integration/test_ws_event_contracts.py. No network,
no LLM, no Docker daemon; no agent thread is ever started (start_session is
only exercised with an EMPTY query, which short-circuits at server.py:563-566).
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


# ══════════════════════════════════════════════════════════════════════════
# Hermetic full-server harness (EXACT mirror of test_ws_event_contracts.py)
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def contract_server():
    """Temp HOME + purged modules + fresh import of web_ui.backend.server.

    Module-scoped so the test shares one hermetic vault/store, mirroring the
    `pathed_server` fixture in tests/web_ui/backend/test_websocket_integration.py.
    """
    tmp_home = tempfile.mkdtemp(prefix="test_state_transitions_")
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


# ══════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════

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
        legitimately SKIPPED when draining for a later target.

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


def _receive_next(ws, path_label: str, timeout: float = 15.0):
    """Receive exactly ONE WS event — hang-proof, no fail-fast.

    Used where the expected payload itself looks like a failure (e.g. the
    "⚠ Query cannot be empty." status), so the generic drains would bail out.
    """
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
        pytest.fail(f"{path_label}: timed out after {timeout}s waiting for the next event")
    if _kind == "exc":
        raise _val
    return _val


def _receive_until_status_text(ws, text: str, path_label: str, max_events: int = 25, timeout: float = 15.0):
    """Drain WS events until a status_message containing ``text`` arrives.

    Same thread+queue machinery as ``_receive_until``.  Fails fast on error
    events and on failure-looking status texts ("⚠", "failed", "internal
    error").  ALSO fails if any ``state_changed`` arrives before the target
    status — for pause/resume/stop with no running agent there must be none
    (deltas 2–4, server.py:677-690).
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
                f"{path_label}: timed out after {timeout}s waiting for status {text!r}; "
                f"received so far: {[e.get('type') for e in events]}"
            )
        if _kind == "exc":
            raise _val
        evt = _val
        events.append(evt)
        if evt.get("type") == "error":
            pytest.fail(f"{path_label}: received unexpected error event: {evt}")
        if evt.get("type") == "state_changed":
            pytest.fail(
                f"{path_label}: unexpected state_changed {evt!r} before status {text!r} "
                f"(pause/resume/stop must NOT emit state_changed without a running agent)"
            )
        if evt.get("type") == "status_message":
            _t = str(evt.get("text", ""))
            if any(m in _t.lower() for m in ("⚠", "failed", "internal error")):
                pytest.fail(f"{path_label}: server reported failure: {_t!r}")
            if text in _t:
                return evt, events
    pytest.fail(
        f"{path_label}: no status_message containing {text!r} within {max_events} events "
        f"(got types: {[e.get('type') for e in events]})"
    )


def _assert_quiet(ws, path_label: str, timeout: float = 1.5):
    """Assert NO further WS event arrives within ``timeout`` seconds."""
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
        return
    pytest.fail(f"{path_label}: expected NO further WS events, but got {_val!r}")


def _make_bridge(events, exec_state=None):
    """Fresh WebAgentBridge with an optional fake controller (no threads)."""
    from types import SimpleNamespace

    from web_ui.backend.bridge import WebAgentBridge
    from session.store import FileSystemSessionStore
    from agent.controller import AgentController

    bridge = WebAgentBridge(session_store=FileSystemSessionStore())
    if exec_state is not None:
        ctrl = AgentController()
        # Stub the agent so controller.is_busy reads our fake execution_state.
        ctrl.agent = SimpleNamespace(state=SimpleNamespace(execution_state=exec_state))
        bridge.set_controller(ctrl)
    bridge.set_event_callback(events.append)
    return bridge


# ══════════════════════════════════════════════════════════════════════════
# A. execution_state_change → state_changed mapping (direct bridge tests)
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "new_state,expected",
    [
        ("running", "RUNNING"),
        ("pausing", "PAUSING"),
        ("ready", "IDLE"),
        ("idle", "IDLE"),
        ("stopped", "IDLE"),
        ("completed", "IDLE"),
        ("error", "IDLE"),
        ("waiting", "WAITING_FOR_USER"),
        ("paused", "PAUSED"),
    ],
)
def test_execution_state_change_mapping(contract_server, new_state, expected):
    """execution_state_change new_state → EXACTLY one state_changed broadcast."""
    events = []
    bridge = _make_bridge(events)
    bridge._map_and_emit({"type": "execution_state_change", "new_state": new_state})

    state_events = [e for e in events if e.get("type") == "state_changed"]
    assert len(state_events) == 1, (
        f"expected exactly one state_changed for execution_state_change {new_state!r} "
        f"(got {events!r})"
    )
    assert state_events[0].get("state") == expected, (
        f"state_changed.state for {new_state!r} must be {expected!r} "
        f"(got {state_events[0].get('state')!r})"
    )
    assert isinstance(state_events[0].get("is_running"), bool), (
        f"state_changed.is_running must be a bool (got {state_events[0].get('is_running')!r})"
    )


# ══════════════════════════════════════════════════════════════════════════
# B. is_busy semantics: is_running mirrors controller.is_busy
# ══════════════════════════════════════════════════════════════════════════

def test_is_busy_drives_is_running(contract_server):
    """is_running True for RUNNING/PAUSING stub, False for READY stub."""
    from agent.core.state import ExecutionState

    cases = [
        (ExecutionState.RUNNING, "running", "RUNNING", True),
        (ExecutionState.PAUSING, "pausing", "PAUSING", True),
        (ExecutionState.READY, "ready", "IDLE", False),
    ]
    for exec_state, new_state, expected_state, expected_running in cases:
        events = []
        bridge = _make_bridge(events, exec_state=exec_state)
        bridge._map_and_emit({"type": "execution_state_change", "new_state": new_state})

        state_events = [e for e in events if e.get("type") == "state_changed"]
        assert len(state_events) == 1, (
            f"{exec_state.name}: expected one state_changed (got {events!r})"
        )
        assert state_events[0]["state"] == expected_state, (
            f"{exec_state.name}: expected state {expected_state!r} "
            f"(got {state_events[0]['state']!r})"
        )
        assert state_events[0]["is_running"] is expected_running, (
            f"{exec_state.name}: expected is_running={expected_running} "
            f"(got {state_events[0]['is_running']!r})"
        )


# ══════════════════════════════════════════════════════════════════════════
# C. agent_responded → WAITING_FOR_USER / IDLE
# ══════════════════════════════════════════════════════════════════════════

def test_agent_responded_question_waits_for_user(contract_server):
    """agent_responded response_type='question' → state_changed WAITING_FOR_USER."""
    events = []
    bridge = _make_bridge(events)
    bridge._map_and_emit({"type": "agent_responded", "response_type": "question"})

    state_events = [e for e in events if e.get("type") == "state_changed"]
    assert len(state_events) == 1, f"expected one state_changed (got {events!r})"
    assert state_events[0]["state"] == "WAITING_FOR_USER", (
        f"agent_responded question must map to WAITING_FOR_USER "
        f"(got {state_events[0]['state']!r})"
    )
    assert isinstance(state_events[0]["is_running"], bool)


def test_agent_responded_final_idle(contract_server):
    """agent_responded (non-question) → state_changed IDLE."""
    events = []
    bridge = _make_bridge(events)
    bridge._map_and_emit({"type": "agent_responded", "response_type": "final"})

    state_events = [e for e in events if e.get("type") == "state_changed"]
    assert len(state_events) == 1, f"expected one state_changed (got {events!r})"
    assert state_events[0]["state"] == "IDLE", (
        f"agent_responded final must map to IDLE (got {state_events[0]['state']!r})"
    )
    assert isinstance(state_events[0]["is_running"], bool)


# ══════════════════════════════════════════════════════════════════════════
# D. Legacy raw 'paused' event — dead code, emits NOTHING
# ══════════════════════════════════════════════════════════════════════════

def test_legacy_paused_event_emits_nothing(contract_server):
    """Delta 2: no _map_and_emit branch for 'paused' → ZERO events.

    The 'PAUSED' mapping line (bridge.py:1987) is unreachable: ExecutionState
    has no 'paused' member (agent/core/state.py) and the agent never emits a
    raw event of type 'paused' that _map_and_emit would translate.
    """
    events = []
    bridge = _make_bridge(events)
    bridge._map_and_emit({"type": "paused"})

    assert events == [], f"raw 'paused' event must emit NOTHING (got {events!r})"


# ══════════════════════════════════════════════════════════════════════════
# E. session_stop → state_changed IDLE (never a session_stop WS event)
# ══════════════════════════════════════════════════════════════════════════

def test_session_stop_maps_to_idle_state_changed(contract_server):
    """session_stop stop_reason='completed' → IDLE state_changed only."""
    events = []
    bridge = _make_bridge(events)
    bridge._map_and_emit({"type": "session_stop", "stop_reason": "completed"})

    stop_events = [e for e in events if e.get("type") == "session_stop"]
    assert not stop_events, (
        f"session_stop must never surface as a WS event (got {stop_events!r})"
    )
    state_events = [e for e in events if e.get("type") == "state_changed"]
    assert state_events, f"expected a state_changed broadcast (got {events!r})"
    first = state_events[0]
    assert first.get("state") == "IDLE", (
        f"state_changed.state must be IDLE after session_stop (got {first.get('state')!r})"
    )
    assert isinstance(first.get("is_running"), bool)


# ══════════════════════════════════════════════════════════════════════════
# F. resume emits NO events (delta 3)
# ══════════════════════════════════════════════════════════════════════════

def test_resume_emits_no_events(contract_server):
    """controller.resume() emits NOTHING → no state_changed (delta 3)."""
    from agent.core.state import ExecutionState

    events = []
    bridge = _make_bridge(events, exec_state=ExecutionState.PAUSING)
    bridge.resume()

    assert events == [], f"resume must not emit any events (got {events!r})"


# ══════════════════════════════════════════════════════════════════════════
# G. continue_session requires a running bridge (delta 6)
# ══════════════════════════════════════════════════════════════════════════

def test_continue_session_requires_running(contract_server):
    """continue_session on a fresh (not running) bridge raises RuntimeError."""
    events = []
    bridge = _make_bridge(events)

    with pytest.raises(RuntimeError) as excinfo:
        bridge.continue_session("hello")
    assert "Bridge is not running. Start it first." in str(excinfo.value), (
        f"unexpected RuntimeError message: {excinfo.value}"
    )
    assert events == [], f"failed continue_session must not emit events (got {events!r})"


# ══════════════════════════════════════════════════════════════════════════
# H. WS: start_session with empty query short-circuits (no agent thread)
# ══════════════════════════════════════════════════════════════════════════

def test_ws_start_session_empty_query(contract_server):
    """start_session empty query → status '⚠ Query cannot be empty.' (no bridge)."""
    app, _ = contract_server

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"command": "start_session", "query": ""})
            # Skip any connect-time events until the status arrives; the
            # expected payload itself contains ⚠, so use the no-fail-fast drain.
            evt = _receive_next(ws, "start_session empty query")
            while evt.get("type") != "status_message":
                evt = _receive_next(ws, "start_session empty query")

    assert evt.get("text") == "⚠ Query cannot be empty.", (
        f"expected the empty-query status (got {evt!r})"
    )


# ══════════════════════════════════════════════════════════════════════════
# I/J. WS: pause/resume/stop with NO running agent → status only, no state_changed
# ══════════════════════════════════════════════════════════════════════════

def _load_session(client, session_id, ws):
    """Shared REST-create → WS load_session prologue (mirrors F3 flow)."""
    ws.send_json({"command": "load_session", "session_id": session_id})
    _receive_until_session_loaded(ws, "REST create → WS load_session")


def test_ws_pause_session_status_only(contract_server):
    """pause_session with no running agent → '⏸ Pausing…', NO state_changed."""
    app, _ = contract_server

    with TestClient(app) as client:
        resp = client.post("/api/session/create", json={"mode": "custom"})
        assert resp.status_code == 200, f"create failed: {resp.status_code} {resp.text}"
        session_id = resp.json()["session_id"]

        with client.websocket_connect("/ws") as ws:
            _load_session(client, session_id, ws)
            ws.send_json({"command": "pause_session"})
            evt, _ = _receive_until_status_text(ws, "⏸ Pausing…", "pause_session → status")
            assert evt.get("text") == "⏸ Pausing…"
            _assert_quiet(ws, "pause_session must not emit state_changed", timeout=1.5)


def test_ws_resume_session_status_only(contract_server):
    """resume_session with no running agent → '▶ Resumed.', NO state_changed."""
    app, _ = contract_server

    with TestClient(app) as client:
        resp = client.post("/api/session/create", json={"mode": "custom"})
        assert resp.status_code == 200, f"create failed: {resp.status_code} {resp.text}"
        session_id = resp.json()["session_id"]

        with client.websocket_connect("/ws") as ws:
            _load_session(client, session_id, ws)
            ws.send_json({"command": "resume_session"})
            evt, _ = _receive_until_status_text(ws, "▶ Resumed.", "resume_session → status")
            assert evt.get("text") == "▶ Resumed."
            _assert_quiet(ws, "resume_session must not emit state_changed", timeout=1.5)


def test_ws_stop_session_status_only(contract_server):
    """stop_session with no running agent → '⏹ Stopped.', NO state_changed."""
    app, _ = contract_server

    with TestClient(app) as client:
        resp = client.post("/api/session/create", json={"mode": "custom"})
        assert resp.status_code == 200, f"create failed: {resp.status_code} {resp.text}"
        session_id = resp.json()["session_id"]

        with client.websocket_connect("/ws") as ws:
            _load_session(client, session_id, ws)
            ws.send_json({"command": "stop_session"})
            evt, _ = _receive_until_status_text(ws, "⏹ Stopped.", "stop_session → status")
            assert evt.get("text") == "⏹ Stopped."
            _assert_quiet(ws, "stop_session must not emit state_changed", timeout=1.5)


# ══════════════════════════════════════════════════════════════════════════
# K. WS: close_session → the ONLY direct state_changed: IDLE / False
# ══════════════════════════════════════════════════════════════════════════

def test_ws_close_session_idle_state_changed(contract_server):
    """close_session → first state_changed is EXACTLY {IDLE, is_running: False}."""
    app, _ = contract_server

    with TestClient(app) as client:
        resp = client.post("/api/session/create", json={"mode": "custom"})
        assert resp.status_code == 200, f"create failed: {resp.status_code} {resp.text}"
        session_id = resp.json()["session_id"]

        with client.websocket_connect("/ws") as ws:
            _load_session(client, session_id, ws)
            ws.send_json({"command": "close_session", "session_id": session_id})
            # Server's direct post-close state_changed (server.py:1649-1653) and/or
            # the bridge broadcast (bridge.py:1819-1822) — both are IDLE/False.
            evt, _ = _receive_until(ws, "state_changed", "close_session → state_changed")

    assert evt.get("type") == "state_changed"
    assert evt.get("state") == "IDLE", (
        f"close_session state_changed.state must be EXACTLY 'IDLE' "
        f"(got {evt.get('state')!r})"
    )
    assert evt.get("is_running") is False, (
        f"close_session state_changed.is_running must be EXACTLY False "
        f"(got {evt.get('is_running')!r})"
    )
