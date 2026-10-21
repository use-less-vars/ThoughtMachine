"""Boundary-layer ingress guards for the web UI server.

Pins the four ingress defects fixed on ``fix/boundary-layer-ingress``:

* Item 4 -- CORS: loopback dev-server origins only, never ``*``, and no
  credentialed wildcard (``allow_credentials`` must be False).
* Item 5 -- client-supplied workspace paths (``?project=``, ``set_project``,
  ``apply_config.workspace_path``) are confined like the REST browser
  endpoints: an existing directory OUTSIDE the vault root.
* Item 6 -- a ``security_response`` may only resolve a prompt raised on the
  SAME WebSocket connection.
* Item 7 -- the WebSocket handshake rejects a present non-loopback Origin.

No network, no LLM, no Docker daemon: every case either inspects app
configuration or drives a rejection path (no agent is ever started).
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import web_ui.backend.server as server_mod
from web_ui.backend import origin_policy
from web_ui.backend.server import app


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────

def _collect(ws, target, max_events=40):
    """Receive frames until ``target`` arrives; return every frame seen."""
    events = []
    for _ in range(max_events):
        evt = ws.receive_json()
        events.append(evt)
        if evt.get("type") == target:
            return events
    raise AssertionError(
        f"never received {target!r}; saw {[e.get('type') for e in events]}"
    )


def _vault_subdir(name="sessions"):
    """A REAL directory inside the vault root (the confined-away prefix)."""
    from thoughtmachine.vault import vault_root
    path = vault_root() / name
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


# ───────────────────────────────────────────────────────────────────────────
# Item 4 / Item 7 -- shared origin policy
# ───────────────────────────────────────────────────────────────────────────

def test_allowed_origins_are_loopback_dev_port_only(monkeypatch):
    monkeypatch.delenv("TM_FRONTEND_PORT", raising=False)
    assert origin_policy.allowed_origins() == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


def test_allowed_origins_follow_tm_frontend_port(monkeypatch):
    monkeypatch.setenv("TM_FRONTEND_PORT", "4321")
    assert origin_policy.allowed_origins() == [
        "http://localhost:4321",
        "http://127.0.0.1:4321",
    ]


def test_allowed_origins_never_contain_wildcard(monkeypatch):
    monkeypatch.delenv("TM_FRONTEND_PORT", raising=False)
    assert "*" not in origin_policy.allowed_origins()


@pytest.mark.parametrize(
    "origin",
    [None, "", "http://localhost:5173", "http://127.0.0.1:5173",
     "https://localhost:8000"],
)
def test_ws_origin_policy_allows_absent_and_loopback(origin):
    assert origin_policy.websocket_origin_allowed(origin) is True


@pytest.mark.parametrize(
    "origin",
    ["http://evil.example", "https://evil.example:5173", "null",
     "http://localhost.evil.example:5173"],
)
def test_ws_origin_policy_rejects_non_loopback(origin):
    assert origin_policy.websocket_origin_allowed(origin) is False


def test_vault_repair_routes_reexports_shared_policy():
    """The REST origin guard must be the SAME code the WS handshake uses."""
    from web_ui.backend import vault_repair_routes as vrr

    # Compare values, not object identity: some peer tests reload/duplicate the
    # module, so `is` is too strong for a full-suite run.
    assert vrr._ORIGIN_RE.pattern == origin_policy._ORIGIN_RE.pattern
    assert vrr._origin_allowed.__module__ == "web_ui.backend.origin_policy"


# ───────────────────────────────────────────────────────────────────────────
# Item 4 -- CORS middleware configuration
# ───────────────────────────────────────────────────────────────────────────

def _cors_options():
    from fastapi.middleware.cors import CORSMiddleware

    for entry in app.user_middleware:
        if entry.cls is CORSMiddleware:
            return entry.kwargs
    raise AssertionError("CORSMiddleware is not installed on the app")


def test_cors_middleware_has_no_wildcard_and_no_credentials(monkeypatch):
    monkeypatch.delenv("TM_FRONTEND_PORT", raising=False)
    opts = _cors_options()
    assert opts["allow_origins"] == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    assert "*" not in opts["allow_origins"]
    assert opts["allow_credentials"] is False


def test_cors_preflight_allows_loopback_origin_only():
    client = TestClient(app)
    ok = client.options(
        "/api/tools",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert ok.status_code == 200
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "access-control-allow-credentials" not in ok.headers

    bad = client.options(
        "/api/tools",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in bad.headers


# ───────────────────────────────────────────────────────────────────────────
# Item 7 -- WebSocket handshake Origin check
# ───────────────────────────────────────────────────────────────────────────

def test_ws_handshake_rejects_non_loopback_origin():
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/ws", headers={"origin": "http://evil.example"}
        ) as ws:
            ws.receive_json()


def test_ws_handshake_accepts_loopback_origin():
    client = TestClient(app)
    with client.websocket_connect(
        "/ws", headers={"origin": "http://localhost:5173"}
    ) as ws:
        ws.send_json({"command": "get_open_sessions"})
        _collect(ws, "open_sessions")


def test_ws_handshake_accepts_absent_origin():
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"command": "get_open_sessions"})
        _collect(ws, "open_sessions")


# ───────────────────────────────────────────────────────────────────────────
# Item 5 -- workspace-path confinement on every WS ingress
# ───────────────────────────────────────────────────────────────────────────

def test_ws_handshake_rejects_project_inside_vault():
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws?project={_vault_subdir()}") as ws:
            ws.receive_json()


def test_set_project_rejects_path_inside_vault():
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"command": "set_project", "project": _vault_subdir()})
        ws.send_json({"command": "get_open_sessions"})
        events = _collect(ws, "open_sessions")
    types = [e.get("type") for e in events]
    assert "session_loaded" not in types, events
    rejection = [e for e in events if e.get("type") == "status_message"]
    assert rejection, events
    assert "Project path rejected" in rejection[0]["text"]


def test_apply_config_rejects_workspace_path_inside_vault():
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_json({
            "command": "apply_config",
            "config": {"mode": "custom", "workspace_path": _vault_subdir()},
        })
        ws.send_json({"command": "get_open_sessions"})
        events = _collect(ws, "open_sessions")
    types = [e.get("type") for e in events]
    assert "config_changed" not in types, events
    rejection = [e for e in events if e.get("type") == "status_message"]
    assert rejection, events
    assert "Workspace path rejected" in rejection[0]["text"]


# ───────────────────────────────────────────────────────────────────────────
# Item 6 -- security_response is bound to the asking connection
# ───────────────────────────────────────────────────────────────────────────

def test_security_response_from_other_connection_is_refused():
    request_id = "rid-bound-to-another-socket"
    server_mod._record_escalation_asker(request_id, -12345)
    try:
        client = TestClient(app)
        with client.websocket_connect("/ws") as ws:
            ws.send_json({
                "command": "security_response",
                "request_id": request_id,
                "approved": True,
            })
            ws.send_json({"command": "get_open_sessions"})
            events = _collect(ws, "open_sessions")
        rejection = [e for e in events if e.get("type") == "status_message"]
        assert rejection, events
        assert "different connection" in rejection[0]["text"]
        # A refused response must NOT consume the pending escalation.
        assert server_mod._escalation_asker(request_id) == -12345
    finally:
        server_mod._forget_escalation(request_id)


def test_security_response_with_unknown_request_id_falls_through():
    """Unknown ids keep the historical behaviour (no asker recorded)."""
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_json({
            "command": "security_response",
            "request_id": "never-raised",
            "approved": True,
        })
        ws.send_json({"command": "bogus-command"})
        events = _collect(ws, "status_message")
    assert any("Unknown command" in e.get("text", "") for e in events), events
    assert not any("different connection" in e.get("text", "") for e in events)


def test_escalation_registry_scopes_and_forgets():
    server_mod._record_escalation_asker("rid-a", 1)
    server_mod._record_escalation_asker("rid-b", 1)
    server_mod._record_escalation_asker("rid-c", 2)
    assert server_mod._escalation_asker("rid-a") == 1
    server_mod._forget_escalations_for(1)
    assert server_mod._escalation_asker("rid-a") is None
    assert server_mod._escalation_asker("rid-b") is None
    assert server_mod._escalation_asker("rid-c") == 2
    server_mod._forget_escalation("rid-c")
    assert server_mod._escalation_asker("rid-c") is None
