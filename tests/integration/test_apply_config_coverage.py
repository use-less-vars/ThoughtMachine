"""apply_config branch coverage (frontend Test Round K).

Drives the REAL server (FastAPI app + /ws) via starlette TestClient and proves
``apply_config`` works in every branch of ``web_ui/backend/server.py``'s
WebSocket handler.  Hermetic harness uses the same hermetic setup as
``tests/integration/test_ws_event_contracts.py`` (temp HOME + patched
``Path.home()`` + purged/re-imported web_ui.backend modules), with
hang-proof drain helpers built on a single-reader pump (see below).

═╦═ REAL apply_config contract (verified against current code) ═══════════════

Command / payload (server.py:716-724):
    { "command": "apply_config", "config": {...} }       # payload key: "config"
    empty config → status_message "⚠ apply_config: empty config received"

Reply events — normal branch, no workspace change (server.py:971-1007):
    bridge is None         → status_message "⚠ No active session to configure"
    apply succeeds         → config_changed {config, settings, permissions,
                             merged_config} ONLY (no status_message)
    apply returns error    → status_message "⚠ Failed to apply config: {err}"
    apply raises           → outer handler → status "⚠ Internal error: ..."
                             (server.py:2039-2045; NOT an error event)

Reply events — workspace-change branch (server.py:726-968):
    Trigger: config.workspace_path is non-empty AND != connection project
             (server.py:729-733).
    Old bridge saved + stopped, fresh bridge + controller created (750-764).
    Session strategy (794-894):
      * existing session WITH conversation → NEW session created; server sends
        session_loaded {session_id, session_name, message_count: 0,
        workspace_id, workspace_path, config} (841-852).  Old session is NOT
        closed — no session_closed, no state_changed; it is saved and left in
        the store (orphaned from this connection).
      * existing session, NO conversation → same session reused; NO
        session_loaded (853-862).
      * no prior session → new session + session_loaded (863-894).
    Then: config_changed (920-925) + status "✅ Switched to project: {path}"
    (948-951).  Exception → error event {"type": "error", "session_id": ...,
    "message": "Failed to apply config"} (952-968).

═╦═ DELTAS vs the engineer spec ══════════════════════════════════════════════

  * Case 3 (no session, no workspace_path): spec expects session_loaded +
    config_changed.  REALITY: bridge is None → ONLY status_message
    "⚠ No active session to configure" (server.py:973-978).  No session is
    created.  (With workspace_path in the payload, the workspace branch DOES
    create a session + send session_loaded + config_changed — pinned by the
    custom variant of case 3.)

  * Case 4 (workspace A→B): spec expects "new session + old session closed
    cleanly".  REALITY: a new session is created ONLY if the existing session
    has a real conversation (_session_has_conversation, server.py:2264 — a
    user/assistant message in user_history).  An empty/fresh session is REUSED
    in place (same session_id, no session_loaded).  The old session is never
    "closed" — no session_closed / state_changed events are emitted; it is
    saved and left intact in the store (bridge stopped, connection moves to a
    fresh bridge).

  * Case 5 (invalid provider_id): spec expects an error event and previous
    config preserved.  REALITY: there is NO provider validation in the apply
    path.  ConfigManager.apply_config sets provider_id via plain setattr
    (config_manager.py:545-547) BEFORE provider resolution; a resolution
    failure is only logged as a WARNING (config_manager.py:556-566) and does
    not raise.  The invalid provider_id is silently accepted and reflected in
    config_changed; no error event, session remains usable.  (api_key is not
    resolved for the unknown provider.)

  * new_session command (server.py:1664-1796) is REAL and sends: session_loaded
    → tokens_updated {0,0} → context_updated {0} → config_changed → status
    "Ready. Type a query to start."  Used here to create sessions (the spec's
    "new_session command → receive session_loaded").

  * bridge.apply_config always returns {config, settings, permissions,
    merged_config} (bridge.py:1357-1362) — failure is expressed by raising, so
    the "result without config" status branch (server.py:1002-1007) is only
    reachable in theory.

Run (from repo root):
    python -m pytest tests/integration/test_apply_config_coverage.py -v

Hermetic: temp HOME + patched Path.home() + purged/re-imported server modules.
No network, no LLM, no Docker daemon.  Agent threads are NEVER started: the
"conversation" needed by case 4 is injected straight into the session object's
user_history (server internals) instead of running start_session.
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
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

pytestmark = pytest.mark.integration


# ════════════════════════════════════════════════════════════════════════════
# Hermetic full-server harness (EXACT mirror of test_ws_event_contracts.py)
# ════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def contract_server():
    """Temp HOME + purged modules + fresh import of web_ui.backend.server."""
    tmp_home = tempfile.mkdtemp(prefix="test_apply_config_coverage_")
    fake_home_path = Path(tmp_home)

    old_home_env = os.environ.get("HOME")
    os.environ["HOME"] = tmp_home

    saved_env = {}
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_COMPATIBLE_API_KEY"):
        saved_env[key] = os.environ.pop(key, None)

    patcher = patch.object(pathlib.Path, "home", return_value=fake_home_path)
    patcher.start()

    # Re-import server so module-level singletons (_session_store, registries)
    # are built against the temp HOME, not the real one. "session" is purged
    # too (FileSystemSessionStore class-level singleton, see the F3 harness).
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


def _server_mod():
    """Return the (purged-then-imported) web_ui.backend.server module.

    The fixture re-imports it into sys.modules, so a plain importlib call
    returns the SAME module object the running server uses (enables reading
    server internals like ``_session_bridges`` in tests).
    """
    return importlib.import_module("web_ui.backend.server")


# ════════════════════════════════════════════════════════════════════════════
# Hang-proof drain helpers (single-reader pump)
# ════════════════════════════════════════════════════════════════════════════
# One daemon thread per WebSocket connection drains receive_json() into a
# queue; helpers just block on that queue.  The original per-call-thread
# design (mirrored from test_ws_event_contracts.py) had a DETERMINISTIC race:
# a helper whose receive timed out (_assert_quiet) left its thread blocked
# inside ws.receive_json(), and because stream/queue waiters are FIFO that
# leftover thread consumed the NEXT helper's event — so the get_config probe
# sent right after a successful apply_config received ZERO events (the probe
# helper's fresh receive thread was second in line and timed out).  A
# single reader makes every helper a pure queue consumer: no two threads
# ever wait on the same socket, so a timed-out quiet check can never steal
# a later event.

_READER_REGISTRY: "dict[int, '_WSReader']" = {}


class _WSReader:
    """Drain a TestClient websocket from one daemon thread into a queue."""

    def __init__(self, ws):
        self._ws = ws
        self._q = queue.Queue(maxsize=64)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            try:
                self._q.put(("ok", self._ws.receive_json()))
            except Exception as exc:  # ws closed at test teardown
                try:
                    self._q.put(("exc", exc))
                except Exception:
                    pass
                return

    def get(self, timeout: float):
        """Block for the next (kind, value); raises queue.Empty on timeout."""
        return self._q.get(timeout=timeout)


def _reader_for(ws) -> "_WSReader":
    """Get (or lazily create) the single reader for this WebSocket connection."""
    reader = _READER_REGISTRY.get(id(ws))
    if reader is None or reader._ws is not ws:
        reader = _WSReader(ws)
        _READER_REGISTRY[id(ws)] = reader
    return reader


def _receive_until_session_loaded(ws, path_label: str, max_events: int = 25, timeout: float = 15.0):
    """Drain WS events until ``session_loaded`` arrives — hang-proof, fail-fast."""
    events = []
    for _ in range(max_events):
        try:
            _kind, _val = _reader_for(ws).get(timeout=timeout)
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
    """Drain WS events until ``target_type`` arrives — hang-proof, fail-fast.

    Fails on ``error`` events (unless the target IS error) and on failure-ish
    status_message texts (⚠/failed/internal error).  The apply_config success
    status "✅ Switched to project: ..." (server.py:948-951) is legitimately
    skipped.  Returns ``(target_event, events)``.
    """
    events = []
    for _ in range(max_events):
        try:
            _kind, _val = _reader_for(ws).get(timeout=timeout)
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
    """Receive exactly ONE event — hang-proof, NO fail-fast on its content."""
    try:
        _kind, _val = _reader_for(ws).get(timeout=timeout)
    except queue.Empty:
        pytest.fail(f"{path_label}: timed out after {timeout}s waiting for next event")
    if _kind == "exc":
        raise _val
    return _val


def _expect(ws, expected_type: str, path_label: str, timeout: float = 15.0):
    """Receive one event and assert its type; fail fast on error events."""
    evt = _receive_next(ws, path_label, timeout=timeout)
    if evt.get("type") == "error":
        pytest.fail(f"{path_label}: unexpected error event: {evt}")
    assert evt.get("type") == expected_type, (
        f"{path_label}: expected {expected_type!r}, got {evt.get('type')!r}: {evt}"
    )
    return evt


def _assert_quiet(ws, path_label: str, timeout: float = 0.75):
    """Assert NO event arrives within ``timeout`` seconds."""
    try:
        _kind, _val = _reader_for(ws).get(timeout=timeout)
    except queue.Empty:
        return  # quiet — as expected
    if _kind == "exc":
        pytest.fail(f"{path_label}: ws error during quiet check: {_val}")
    pytest.fail(f"{path_label}: expected quiet, got event: {_val}")


# ════════════════════════════════════════════════════════════════════════════
# Flow helpers
# ════════════════════════════════════════════════════════════════════════════

def _new_session(ws, path_label: str, mode: str = "custom"):
    """Send ``new_session`` and drain the full handshake.

    Real sequence (server.py:1664-1796): session_loaded → tokens_updated {0,0}
    → context_updated {0} → config_changed → status "Ready. Type a query to
    start."  Returns ``(session_loaded, events)``.
    """
    ws.send_json({"command": "new_session", "mode": mode})
    evt, events = _receive_until_session_loaded(ws, path_label)
    events.append(_expect(ws, "tokens_updated", path_label))
    events.append(_expect(ws, "context_updated", path_label))
    events.append(_expect(ws, "config_changed", path_label))
    final = _expect(ws, "status_message", path_label)
    events.append(final)
    assert "Ready" in str(final.get("text", "")), (
        f"{path_label}: unexpected final status after new_session: {final}"
    )
    return evt, events


def _apply_config(ws, config: dict, path_label: str):
    """Send apply_config and return the events until the reply sequence ends.

    Default variant (no workspace_path): normal branch → config_changed only.
    Custom variant (workspace_path != project): workspace branch → config_changed
    + status "✅ Switched to project: ...".  Returns (config_changed_evt, events).
    """
    ws.send_json({"command": "apply_config", "config": config})
    evt, events = _receive_until(ws, "config_changed", path_label)
    return evt, events


def _get_config(ws, path_label: str):
    """Send ``get_config`` and drain until config_changed (bridge usability probe)."""
    ws.send_json({"command": "get_config"})
    evt, _ = _receive_until(ws, "config_changed", f"{path_label} → get_config")
    return evt


# ════════════════════════════════════════════════════════════════════════════
# Parametrisation
# ════════════════════════════════════════════════════════════════════════════

_WORKSPACE_TYPES = ["default", "custom"]

# Valid SessionPermissions profile (thoughtmachine/security.py:83-95):
#   container: bool; network: banned|ask|write|outbound;
#   filesystem/system/git/execution: banned|read|write|full|ask
_NEW_PERMISSIONS = {
    "container": True,
    "network": "outbound",
    "filesystem": "write",
    "system": "read",
    "git": "write",
    "execution": "write",
}


_WS_PATH_CACHE: dict = {}


def _ws_path(case_name: str, workspace_type: str) -> str:
    """Unique, STABLE workspace path per case×variant (memoized).

    A fresh uuid on every call made assert-side references disagree with the
    path embedded in the config payload (send-side) — e.g. the custom-variant
    status assert compared against a path generated AFTER the apply.  The
    cache makes every call for the same key return the same path.  case6
    passes distinct case names ("case6a"/"case6b") so its two rapid
    consecutive applies keep two DISTINCT workspace paths (B2 != B1).
    """
    key = (case_name, workspace_type)
    if key not in _WS_PATH_CACHE:
        _WS_PATH_CACHE[key] = f"/tmp/round-k-{case_name}-{uuid.uuid4().hex[:8]}"
    return _WS_PATH_CACHE[key]


def _variant_config(case_name: str, workspace_type: str, **fields) -> dict:
    """Build an apply_config payload for the given variant.

    ``default`` → no workspace_path (normal branch).  ``custom`` → unique
    workspace_path != connection project (workspace-change branch).
    """
    cfg = dict(fields)
    if workspace_type == "custom":
        cfg["workspace_path"] = _ws_path(case_name, workspace_type)
    return cfg


# ════════════════════════════════════════════════════════════════════════════
# Case 1 — session exists, change session_permissions
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("workspace_type", _WORKSPACE_TYPES,
                         ids=["default", "custom"])
def test_case1_permissions_change(contract_server, workspace_type):
    """apply_config with modified session_permissions → config_changed reflects
    the new permissions, no error, session still alive."""
    app, _ = contract_server
    label = f"case1-{workspace_type}"
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            loaded, _ = _new_session(ws, label)
            sid = loaded["session_id"]
            assert sid

            cfg = _variant_config("case1", workspace_type,
                                  session_permissions=_NEW_PERMISSIONS)
            evt, events = _apply_config(ws, cfg, label)
            assert evt["permissions"]["filesystem"] == "write"
            assert evt["permissions"]["git"] == "write"
            assert evt["permissions"]["network"] == "outbound"
            assert evt["config"]["session_permissions"]["filesystem"] == "write"

            if workspace_type == "custom":
                # workspace branch: empty session is reused (no session_loaded);
                # the final status confirms the project switch.
                final = _expect(ws, "status_message", label)
                assert "✅ Switched to project" in final.get("text", "")
                assert _ws_path("case1", workspace_type) in final.get("text", "")
            else:
                # normal branch: config_changed is the ONLY reply.
                _assert_quiet(ws, label)

            # Session still alive and reflects the applied permissions.
            probe = _get_config(ws, label)
            assert probe["config"]["session_permissions"]["filesystem"] == "write"


# ════════════════════════════════════════════════════════════════════════════
# Case 2 — session exists, change provider/model
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("workspace_type", _WORKSPACE_TYPES,
                         ids=["default", "custom"])
def test_case2_provider_model_change(contract_server, workspace_type):
    """apply_config with provider_id/model → config_changed reflects the change."""
    app, _ = contract_server
    label = f"case2-{workspace_type}"
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            loaded, _ = _new_session(ws, label)
            sid = loaded["session_id"]
            assert sid

            cfg = _variant_config("case2", workspace_type,
                                  provider_id="roundk-provider",
                                  model="roundk-model-v1")
            evt, _ = _apply_config(ws, cfg, label)
            # provider_id/model are SessionConfig fields (session_config.py:72-79);
            # the invalid provider is silently accepted (no validation).
            assert evt["config"]["provider_id"] == "roundk-provider"
            assert evt["config"]["model"] == "roundk-model-v1"

            if workspace_type == "custom":
                final = _expect(ws, "status_message", label)
                assert "✅ Switched to project" in final.get("text", "")
            else:
                _assert_quiet(ws, label)

            probe = _get_config(ws, label)
            assert probe["config"]["model"] == "roundk-model-v1"


# ════════════════════════════════════════════════════════════════════════════
# Case 3 — no session
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("workspace_type", _WORKSPACE_TYPES,
                         ids=["default", "custom"])
def test_case3_no_session(contract_server, workspace_type):
    """apply_config with no session at all (fresh connection, bridge is None)."""
    app, _ = contract_server
    label = f"case3-{workspace_type}"
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            if workspace_type == "default":
                # DELTA vs spec: no session → ONLY a warning status; no
                # session_loaded, no config_changed (server.py:973-978).
                ws.send_json({"command": "apply_config",
                              "config": {"model": "roundk-no-session"}})
                evt = _receive_next(ws, label)
                assert evt.get("type") == "status_message", f"unexpected: {evt}"
                assert "No active session to configure" in evt.get("text", "")
                _assert_quiet(ws, label)
            else:
                # workspace branch, no prior session → session is CREATED and
                # session_loaded + config_changed arrive (server.py:863-894).
                cfg = _variant_config("case3", workspace_type,
                                      model="roundk-no-session")
                # (the send happens via _apply_config; drain session_loaded first)
                ws.send_json({"command": "apply_config", "config": cfg})
                loaded_evt, _ = _receive_until_session_loaded(ws, label)
                assert loaded_evt["session_id"]
                assert loaded_evt["workspace_path"] == _ws_path("case3", workspace_type)
                evt, _ = _receive_until(ws, "config_changed", label)
                assert evt["config"]["model"] == "roundk-no-session"
                final = _expect(ws, "status_message", label)
                assert "✅ Switched to project" in final.get("text", "")

                # Session is usable.
                probe = _get_config(ws, label)
                assert probe["config"]["model"] == "roundk-no-session"


# ════════════════════════════════════════════════════════════════════════════
# Case 4 — workspace change A → B with a real conversation
# ════════════════════════════════════════════════════════════════════════════

def test_case4_workspace_change_new_session(contract_server):
    """Workspace switch with a conversation-bearing session → NEW session for B,
    old session preserved (NOT closed), no session_closed/state_changed events."""
    app, _ = contract_server
    label = "case4"
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            loaded, _ = _new_session(ws, label)
            old_sid = loaded["session_id"]
            assert old_sid

            # Inject a real conversation WITHOUT starting an agent: append a
            # user message straight into the session object the bridge holds
            # (server._session_bridges[sid]._loaded_session).  This is what
            # _session_has_conversation (server.py:2264) inspects.
            bridge = _server_mod()._session_bridges[old_sid]
            session = bridge._loaded_session or bridge._session
            assert session is not None, "new_session must leave a loaded session"
            session.user_history.append({"role": "user", "content": "hello"})

            ws_b = _ws_path("case4", "custom")
            ws.send_json({"command": "apply_config",
                          "config": {"workspace_path": ws_b, "model": "roundk-c4"}})

            # strategy: existing session HAS conversation → new session created;
            # session_loaded carries the NEW session_id (server.py:812-852).
            new_loaded, events = _receive_until_session_loaded(ws, label)
            new_sid = new_loaded["session_id"]
            assert new_sid != old_sid, f"expected new session, got same {new_sid}"
            assert new_loaded["workspace_path"] == ws_b
            assert new_loaded["message_count"] == 0

            evt, _ = _receive_until(ws, "config_changed", label)
            assert evt["config"]["model"] == "roundk-c4"
            final = _expect(ws, "status_message", label)
            assert "✅ Switched to project" in final.get("text", "")

            # Old session is NOT closed: no session_closed, no state_changed for
            # it — the old bridge was stopped (server.py:750-753) silently.
            for e in events:
                assert e.get("type") not in ("session_closed", "state_changed"), (
                    f"old session should not emit close/state events, got: {e}"
                )
            _assert_quiet(ws, label)

            # Old session is preserved intact in the store (saved, orphaned).
            store = _server_mod()._get_session_store()
            old = store.load_session(old_sid)
            assert old is not None, "old session must remain in the store"
            roles = [m.get("role") for m in old.user_history if isinstance(m, dict)]
            assert "user" in roles, "old session must keep its conversation"

            # New session usable.
            probe = _get_config(ws, label)
            assert probe["config"]["model"] == "roundk-c4"


# ════════════════════════════════════════════════════════════════════════════
# Case 5 — invalid provider_id
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("workspace_type", _WORKSPACE_TYPES,
                         ids=["default", "custom"])
def test_case5_invalid_provider(contract_server, workspace_type):
    """Invalid provider_id → NO error event; silently accepted (no validation);
    session remains usable and later applies still work."""
    app, _ = contract_server
    label = f"case5-{workspace_type}"
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            loaded, _ = _new_session(ws, label)
            sid = loaded["session_id"]
            assert sid

            cfg = _variant_config("case5", workspace_type,
                                  provider_id="no-such-provider-xyz",
                                  model="roundk-c5a")
            evt, _ = _apply_config(ws, cfg, label)
            # DELTA vs spec: no error event; the invalid provider_id is set
            # (config_manager.py:545-547), resolution failure only logged as a
            # WARNING (config_manager.py:556-566).
            assert evt["config"]["provider_id"] == "no-such-provider-xyz"

            if workspace_type == "custom":
                final = _expect(ws, "status_message", label)
                assert "✅ Switched to project" in final.get("text", "")
            else:
                _assert_quiet(ws, label)

            # Session remains usable — a follow-up apply succeeds.
            evt2, _ = _apply_config(ws, {"model": "roundk-c5b"}, label)
            assert evt2["config"]["model"] == "roundk-c5b"
            probe = _get_config(ws, label)
            assert probe["config"]["model"] == "roundk-c5b"


# ════════════════════════════════════════════════════════════════════════════
# Case 6 — two rapid consecutive apply_config
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("workspace_type", _WORKSPACE_TYPES,
                         ids=["default", "custom"])
def test_case6_rapid_consecutive_applies(contract_server, workspace_type):
    """Two rapid apply_config calls → both complete without crash, final config
    matches the second call."""
    app, _ = contract_server
    label = f"case6-{workspace_type}"
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            loaded, _ = _new_session(ws, label)
            sid = loaded["session_id"]
            assert sid

            if workspace_type == "default":
                cfg1 = {"model": "roundk-m6a"}
                cfg2 = {"model": "roundk-m6b"}
            else:
                cfg1 = _variant_config("case6a", workspace_type, model="roundk-m6a")
                cfg2 = _variant_config("case6b", workspace_type, model="roundk-m6b")
                # NOTE: distinct workspace paths → the second apply is ANOTHER
                # workspace change (B2 != B1), still completing without crash.

            evt1, _ = _apply_config(ws, cfg1, label)
            assert evt1["config"]["model"] == "roundk-m6a"
            if workspace_type == "custom":
                _expect(ws, "status_message", label)

            evt2, _ = _apply_config(ws, cfg2, label)
            assert evt2["config"]["model"] == "roundk-m6b"
            if workspace_type == "custom":
                final = _expect(ws, "status_message", label)
                assert "✅ Switched to project" in final.get("text", "")
            else:
                _assert_quiet(ws, label)

            probe = _get_config(ws, label)
            assert probe["config"]["model"] == "roundk-m6b"


# ════════════════════════════════════════════════════════════════════════════
# Case 7 — permission-only apply in an established workspace keeps the session
# (locks cbe5f72 "include workspace_path in apply_config config_changed event")
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("workspace_type", _WORKSPACE_TYPES,
                         ids=["default", "custom"])
def test_case7_permission_only_apply_keeps_session(contract_server, workspace_type):
    """Permission-only apply_config on an established session → SAME session_id,
    NO session_loaded, no status_message; config_changed carries the updated
    permissions and (custom workspace) the workspace_path key."""
    app, _ = contract_server
    label = f"case7-{workspace_type}"
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            loaded, _ = _new_session(ws, label)
            sid = loaded["session_id"]
            assert sid

            # Establish the workspace: custom → bind to a unique workspace path
            # (empty session reused in place, no session_loaded, server.py:853-862);
            # default → plain model apply (normal branch).
            cfg_setup = _variant_config("case7", workspace_type, model="roundk-c7-setup")
            if workspace_type == "custom":
                ws_path = _ws_path("case7", workspace_type)
                evt, events = _apply_config(ws, cfg_setup, label)
                assert evt["config"]["model"] == "roundk-c7-setup"
                assert "session_loaded" not in [e.get("type") for e in events], (
                    f"empty-session setup must not emit session_loaded: "
                    f"{[e.get('type') for e in events]}"
                )
                final = _expect(ws, "status_message", label)
                assert "✅ Switched to project" in final.get("text", "")
            else:
                ws_path = None
                _apply_config(ws, cfg_setup, label)
                _assert_quiet(ws, label)

            bridge_before = _server_mod()._session_bridges.get(sid)
            assert bridge_before is not None, "session must have a live bridge"
            session = bridge_before._loaded_session or bridge_before._session
            assert session is not None and session.session_id == sid

            # The user's exact scenario: permission-only apply, no workspace_path.
            evt, events = _apply_config(
                ws, {"session_permissions": _NEW_PERMISSIONS}, label
            )
            assert "session_loaded" not in [e.get("type") for e in events], (
                f"permission-only apply must NOT replace the session; got: "
                f"{[e.get('type') for e in events]}"
            )
            assert evt["permissions"]["filesystem"] == "write"
            assert evt["permissions"]["git"] == "write"
            assert evt["config"]["session_permissions"]["filesystem"] == "write"
            if workspace_type == "custom":
                # cbe5f72: config_changed carries the workspace_path key.
                assert evt["config"]["workspace_path"] == ws_path, (
                    f"config_changed must carry workspace_path, got "
                    f"{evt['config'].get('workspace_path')!r}"
                )
            # Normal branch sends config_changed ONLY — no status_message.
            _assert_quiet(ws, label)

            # Same bridge object and same session id afterwards.
            bridge_after = _server_mod()._session_bridges.get(sid)
            assert bridge_after is not None
            assert bridge_after is bridge_before, (
                "permission-only apply must not rebuild the bridge/session"
            )
            session = bridge_after._loaded_session or bridge_after._session
            assert session is not None and session.session_id == sid

            # Session is usable and persisted with the applied permissions.
            probe = _get_config(ws, label)
            assert probe["config"]["session_permissions"]["filesystem"] == "write"


# ════════════════════════════════════════════════════════════════════════════
# Case 8 — same directory, trailing-slash form → NOT a workspace change
# (locks e72a61a "normalize paths in apply_config workspace_changed detection")
# ════════════════════════════════════════════════════════════════════════════

def test_case8_same_workspace_trailing_slash_no_replacement(contract_server):
    """apply_config whose workspace_path is the SAME directory with a trailing
    slash → os.path.normpath (server.py:730-732) makes it a no-op workspace
    change: same session_id, no session_loaded, no 'Switched to project'."""
    app, _ = contract_server
    label = "case8"
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            loaded, _ = _new_session(ws, label)
            sid = loaded["session_id"]
            assert sid

            ws_path = _ws_path("case8", "custom")
            ws.send_json({"command": "apply_config",
                          "config": {"workspace_path": ws_path, "model": "roundk-c8-setup"}})
            evt, _ = _receive_until(ws, "config_changed", label)
            assert evt["config"]["model"] == "roundk-c8-setup"
            final = _expect(ws, "status_message", label)
            assert "✅ Switched to project" in final.get("text", "")

            bridge_before = _server_mod()._session_bridges.get(sid)
            assert bridge_before is not None
            session = bridge_before._loaded_session or bridge_before._session
            assert session is not None and session.session_id == sid

            # Same directory, different FORM.  Pre-e72a61a this compared raw
            # strings → treated as a workspace change → session replaced (the
            # user-visible stale-session banner bug).
            evt, events = _apply_config(
                ws,
                {"workspace_path": ws_path + "/", "session_permissions": _NEW_PERMISSIONS},
                label,
            )
            assert "session_loaded" not in [e.get("type") for e in events], (
                f"same-dir trailing-slash workspace_path must NOT replace the "
                f"session; got: {[e.get('type') for e in events]}"
            )
            assert evt["permissions"]["filesystem"] == "write"
            # config_changed reflects the NORMALIZED path, not the raw form.
            assert evt["config"]["workspace_path"] == ws_path, (
                f"expected normalized workspace_path {ws_path!r}, got "
                f"{evt['config'].get('workspace_path')!r}"
            )
            # Normal branch: config_changed ONLY — no 'Switched to project'.
            _assert_quiet(ws, label)

            bridge_after = _server_mod()._session_bridges.get(sid)
            assert bridge_after is not None
            assert bridge_after is bridge_before, (
                "same-dir trailing-slash workspace_path must not rebuild the bridge"
            )
            session = bridge_after._loaded_session or bridge_after._session
            assert session is not None and session.session_id == sid

            probe = _get_config(ws, label)
            assert probe["config"]["session_permissions"]["filesystem"] == "write"



# ════════════════════════════════════════════════════════════════════════════
# Case 9 — idle (non-workspace) apply of model + temperature + tools together
# (Config Change Queue: immediate path — controller idle → config_changed only)
# ════════════════════════════════════════════════════════════════════════════

def test_case9_idle_apply_model_temp_tools(contract_server):
    """apply_config while the controller is IDLE (fresh session, agent not
    started) → immediate config_changed on the SAME session: model +
    temperature + tools applied together, no session_loaded, no status_message;
    tool entries carry a non-empty description."""
    app, _ = contract_server
    label = "case9"
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            loaded, _ = _new_session(ws, label)
            sid = loaded["session_id"]
            assert sid

            bridge_before = _server_mod()._session_bridges.get(sid)
            assert bridge_before is not None
            # Fresh session: controller exists (server.py:1675-1680) but no
            # agent started → is_busy False → the apply must go through the
            # IMMEDIATE path (config_changed, no config_queued).
            assert bridge_before._controller is not None
            assert not bridge_before._controller.is_busy

            cfg = {
                "model": "roundk-c9",
                "temperature": 0.42,
                "tools": [
                    {"name": "FileEditor", "enabled": True},
                    {"name": "FileReader", "enabled": False},
                ],
            }
            evt, events = _apply_config(ws, cfg, label)
            assert "session_loaded" not in [e.get("type") for e in events], (
                f"idle apply must not replace the session; got: "
                f"{[e.get('type') for e in events]}"
            )
            assert evt["config"]["model"] == "roundk-c9"
            assert evt["config"]["temperature"] == 0.42

            tools = evt["config"].get("tools", [])
            names = {t.get("name") for t in tools}
            assert "FileEditor" in names, f"tools list missing FileEditor: {tools}"
            fe = next(t for t in tools if t.get("name") == "FileEditor")
            assert fe.get("enabled") is True
            desc = fe.get("description", "")
            assert isinstance(desc, str) and desc.strip(), (
                f"tool entries must carry a non-empty description, got {desc!r}"
            )

            # Immediate (idle) path: config_changed ONLY — no status_message.
            _assert_quiet(ws, label)

            bridge_after = _server_mod()._session_bridges.get(sid)
            assert bridge_after is not None
            assert bridge_after is bridge_before, (
                "idle apply must not rebuild the bridge/session"
            )
            session = bridge_after._loaded_session or bridge_after._session
            assert session is not None and session.session_id == sid

            probe = _get_config(ws, label)
            assert probe["config"]["model"] == "roundk-c9"
            assert probe["config"]["temperature"] == 0.42


# ════════════════════════════════════════════════════════════════════════════
# Case 10 — busy controller → config_queued ACK, then deferred config_changed
# (Config Change Queue: queued path — busy → idle transition)
# ════════════════════════════════════════════════════════════════════════════

def test_case10_busy_config_queued_then_deferred_apply(contract_server):
    """apply_config while the controller is BUSY (agent RUNNING) → the server
    ACKs with config_queued and applies NOTHING yet; once the controller becomes
    idle the queued config is applied on the SAME bridge/session and the
    deferred config_changed is broadcast with the applied permissions and the
    normalized workspace_path. No session_loaded anywhere."""
    app, _ = contract_server
    label = "case10"
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            loaded, _ = _new_session(ws, label)
            sid = loaded["session_id"]
            assert sid

            # Establish a workspace (mirrors case7/8 setup) so the deferred
            # apply carries a meaningful workspace_path.
            ws_path = _ws_path("case10", "custom")
            cfg_setup = _variant_config("case10", "custom", model="roundk-c10-setup")
            evt, _ = _apply_config(ws, cfg_setup, label)
            assert evt["config"]["model"] == "roundk-c10-setup"
            final = _expect(ws, "status_message", label)
            assert "✅ Switched to project" in final.get("text", "")

            bridge = _server_mod()._session_bridges.get(sid)
            assert bridge is not None
            controller = bridge._controller
            assert controller is not None

            # Simulate a busy controller WITHOUT starting a real agent thread:
            # stub agent with a RUNNING execution state + a no-op config update;
            # stub thread with the (alive) main thread so apply_config's
            # controller_alive check (bridge.py) doesn't trigger _restart_controller.
            from agent.core.state import ExecutionState

            class _FakeAgentState:
                execution_state = ExecutionState.RUNNING

            class _FakeAgent:
                state = _FakeAgentState()
                session = None  # keep _on_controller_event session-capture quiet

                def request_config_update(self, agent_config):  # noqa: D401
                    return None

            orig_agent = controller.agent
            orig_thread = controller.thread
            try:
                controller.agent = _FakeAgent()
                controller.thread = threading.main_thread()
                assert controller.is_busy, "stub must make the controller busy"

                # ── Busy: config must be QUEUED, not applied ────────────────
                ws.send_json({"command": "apply_config",
                              "config": {"session_permissions": _NEW_PERMISSIONS}})
                evt_q, events_q = _receive_until(
                    ws, "config_queued", f"{label} → busy apply"
                )
                assert evt_q.get("status") == "queued"
                assert "config_changed" not in [e.get("type") for e in events_q], (
                    f"busy apply must NOT emit config_changed early; got: "
                    f"{[e.get('type') for e in events_q]}"
                )
                assert "session_loaded" not in [e.get("type") for e in events_q]
                # Nothing else may arrive while the controller is still busy.
                _assert_quiet(ws, f"{label} → still busy")

                # ── Idle: queued config is applied and broadcast ────────────
                controller.agent.state.execution_state = ExecutionState.READY
                bridge._on_controller_event({"type": "thread_finished"})
                evt_d, events_d = _receive_until(
                    ws, "config_changed", f"{label} → deferred apply"
                )
                assert evt_d["permissions"]["filesystem"] == "write"
                assert evt_d["permissions"]["git"] == "write"
                assert evt_d["config"]["workspace_path"] == ws_path, (
                    f"deferred config_changed must carry the normalized "
                    f"workspace_path, got {evt_d['config'].get('workspace_path')!r}"
                )
                assert "session_loaded" not in [e.get("type") for e in events_d], (
                    f"deferred apply must not replace the session; got: "
                    f"{[e.get('type') for e in events_d]}"
                )
                # Deferred apply runs on the SAME bridge and SAME session.
                bridge_after = _server_mod()._session_bridges.get(sid)
                assert bridge_after is bridge, (
                    "deferred apply must not rebuild the bridge/session"
                )
                session = bridge_after._loaded_session or bridge_after._session
                assert session is not None and session.session_id == sid
                _assert_quiet(ws, label)
            finally:
                controller.agent = orig_agent
                controller.thread = orig_thread

