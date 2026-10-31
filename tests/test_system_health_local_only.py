"""Isolation tests for GET /api/system/health.

The endpoint aggregates vault-global state: the filesystem worker registry and
every open session.  It is an operator endpoint, so this module pins the two
properties that keep it from being a cross-host / cross-workspace read surface:

* the local-caller gate -- a non-loopback peer is refused, and a request with
  no peer address is refused (fail closed);
* the payload carries no event-log surface at all.  The EventLogger writes ONE
  vault-global file shared by every session and workspace, so there is no safe
  tail to render here; the route must not touch ``EventLogger``.

Everything is hermetic: no event-log file, no session store, no HTTP server.
``_WORKSPACES_DIR`` is pointed at a temporary directory and the session store is
stubbed on the module the endpoint imports it from at call time.
"""

from __future__ import annotations

import json
import types
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web_ui.backend import health_routes

# -- Harness -----------------------------------------------------------------

class _StubSessionStore:
    """Directory-free FileSystemSessionStore stand-in."""

    def __init__(self, sessions):
        self._sessions = dict(sessions)

    def get_open_sessions(self):
        return list(self._sessions)

    def load_session(self, session_id, workspace_id=None):
        return self._sessions.get(session_id)


class _ForbiddenEventLogger:
    """EventLogger stand-in that records any touch of the event log.

    ``calls`` stays empty only if the route never resolves EventLogger.
    """

    calls: ClassVar[list] = []

    def __init__(self, file_path, records):
        self.file_path = file_path
        self._records = records

    def get_tail(self, n: int = 20):
        type(self).calls.append(n)
        return list(self._records)[-n:]


def _session(session_id: str, workspace_id: str):
    return types.SimpleNamespace(
        session_id=session_id,
        workspace_id=workspace_id,
        mode="chat",
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T01:00:00",
        metadata={"mode": "chat"},
    )


def _write_worker_status(workers_dir: Path, workspace_path: str) -> None:
    """Seed one legacy-layout worker status.json under *workers_dir*."""
    d = workers_dir / "coder"
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(
        json.dumps(
            {
                "runtime_status": "busy",
                "current_task": "seeding",
                "workspace_path": workspace_path,
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def app():
    application = FastAPI()
    application.include_router(health_routes.router)
    return application


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Alpha and beta each own one session; both are open.  One worker exists."""
    workspaces = tmp_path / "workspaces"
    workers_dir = workspaces / "ws-alpha" / "workers"
    _write_worker_status(workers_dir, "/repo/alpha")
    monkeypatch.setattr(health_routes, "_WORKSPACES_DIR", workspaces)
    monkeypatch.setattr(
        "session.store.FileSystemSessionStore",
        lambda *a, **kw: _StubSessionStore(
            {
                "sess-alpha": _session("sess-alpha", "ws-alpha"),
                "sess-beta": _session("sess-beta", "ws-beta"),
            }
        ),
    )
    return tmp_path


@pytest.fixture
def event_log(monkeypatch, tmp_path):
    """Install a tripwire EventLogger whose records hold an obvious secret."""
    stub = _ForbiddenEventLogger(
        tmp_path / "event_log.jsonl",
        [
            {
                "timestamp": "2026-01-01T00:00:00",
                "event_type": "tool_result",
                "session_id": "sess-beta",
                "data": {"secret": "EVENT-LOG-SECRET-DO-NOT-LEAK"},
            }
        ],
    )
    _ForbiddenEventLogger.calls = []
    monkeypatch.setattr(
        "agent.logging.event_logger.EventLogger.instance", lambda: stub
    )
    return stub


# -- Local-caller gate -------------------------------------------------------

def test_remote_client_is_refused(app, env):
    """A non-loopback peer gets 403 and an error body, never a payload."""
    with TestClient(app, client=("203.0.113.9", 45678)) as client:
        response = client.get("/api/system/health")
    assert response.status_code == 403
    assert response.json() == {"error": "localhost only"}
    assert "running_workers" not in response.text
    assert "active_sessions" not in response.text


def test_remote_client_cannot_reach_workers_or_sessions(app, env):
    """The seeded worker/session inventory is unreachable from a remote peer.

    Both a bare request and a scope-looking one are refused, so no query
    parameter can be used to talk the endpoint into answering.
    """
    with TestClient(app, client=("203.0.113.9", 45678)) as client:
        for url in (
            "/api/system/health",
            "/api/system/health?workspace=ws-alpha",
            "/api/system/health?session_id=sess-alpha",
        ):
            response = client.get(url)
            assert response.status_code == 403, url
            body = response.text
            assert "sess-alpha" not in body
            assert "sess-beta" not in body
            assert "coder" not in body


def test_missing_peer_address_is_refused():
    """Fail closed: no reportable peer address is treated as NOT local."""
    for client in (None, types.SimpleNamespace(host=None)):
        denied = health_routes._localhost_only(
            types.SimpleNamespace(client=client)
        )
        assert denied is not None
        assert denied.status_code == 403
        assert json.loads(denied.body) == {"error": "localhost only"}


def test_loopback_client_is_served(app, env):
    """A loopback peer still gets the full worker/session payload."""
    with TestClient(app, client=("127.0.0.1", 45678)) as client:
        response = client.get("/api/system/health")
    assert response.status_code == 200
    payload = response.json()
    assert [w["worker_name"] for w in payload["running_workers"]] == ["coder"]
    assert {s["session_id"] for s in payload["active_sessions"]} == {
        "sess-alpha",
        "sess-beta",
    }


# -- No event-log surface ----------------------------------------------------

def test_payload_has_no_event_log_content(app, env, event_log):
    """The route neither serves nor even reads the vault-global event log.

    ``event_log`` records any touch of EventLogger and carries a secret; a
    served tail (or any other event-log-shaped key) would fail here.
    """
    with TestClient(app) as client:
        response = client.get("/api/system/health")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"running_workers", "active_sessions"}
    assert "recent_event_log_tail" not in response.text
    assert "EVENT-LOG-SECRET-DO-NOT-LEAK" not in response.text
    # No key anywhere may carry an event-log entry.
    body = json.dumps(payload)
    assert "event_type" not in body
    assert "tool_result" not in body
    # And the route never resolved EventLogger at all.
    assert _ForbiddenEventLogger.calls == []


# -- Retained (pre-existing) parameters --------------------------------------

def test_workspace_filter_still_applies(app, env):
    """``?workspace=`` keeps filtering workers/sessions (UI convenience)."""
    with TestClient(app) as client:
        response = client.get("/api/system/health?workspace=alpha")
    assert response.status_code == 200
    payload = response.json()
    assert [w["worker_name"] for w in payload["running_workers"]] == ["coder"]
    assert {s["session_id"] for s in payload["active_sessions"]} == {"sess-alpha"}


def test_session_id_parameter_no_longer_scopes_anything(app, env):
    """The removed ``session_id`` parameter is inert: it changes no output."""
    with TestClient(app) as client:
        plain = client.get("/api/system/health").json()
        with_param = client.get("/api/system/health?session_id=sess-beta").json()
    assert with_param == plain
