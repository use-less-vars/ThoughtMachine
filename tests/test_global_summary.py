"""Tests for GET /api/global/summary — global dashboard snapshot.

Hermetic: the route handler is exercised through a lightweight FastAPI app
that includes only ``global_routes.router`` (importing the router module is
side-effect-free).  All runtime singletons (WorkspaceRegistry /
SessionRegistry / worker manager / docker listing) are monkeypatched at the
module level — the handler looks them up at call time — and
``global_routes.vault_root`` is pointed at a throwaway tmp vault so the
per-workspace ``allow_host_resources`` lookup stays hermetic.
"""

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import web_ui.backend.global_routes as global_routes


@pytest.fixture
def app():
    application = FastAPI()
    application.include_router(global_routes.router)
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def _workspace(wid, label="", root_path="/tmp/ws", last_opened="", updated_at=""):
    return SimpleNamespace(
        id=wid,
        label=label or wid,
        root_path=root_path,
        last_opened=last_opened,
        updated_at=updated_at,
    )


def _session(sid, wid, is_open=True, name="s", mode="standard", created_at=""):
    return {
        "session_id": sid,
        "workspace_id": wid,
        "name": name,
        "mode": mode,
        "is_open": is_open,
        "created_at": created_at,
    }


class _FakeRegistry:
    def __init__(self, entries):
        self._entries = entries

    def list_workspaces(self):
        return self._entries


class _FakeSessions:
    def __init__(self, sessions):
        self._sessions = sessions

    def get_all(self):
        return self._sessions


class _FakeWorkerManager:
    def list_workers(self, session_id):
        return ["worker-1"]


def _patch_summary_deps(
    monkeypatch,
    tmp_path,
    workspace_entries,
    sessions,
    worker_manager=None,
    containers=(),
    container_warning=None,
    registry_raises=False,
):
    """Point every summary input at fakes; returns the tmp vault path."""
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)

    if registry_raises:

        class _BrokenRegistry:
            @staticmethod
            def get_default():
                raise RuntimeError("registry down")

        workspace_factory = _BrokenRegistry
    else:
        workspace_factory = type(
            "_WR",
            (),
            {"get_default": staticmethod(lambda: _FakeRegistry(workspace_entries))},
        )
    monkeypatch.setattr(global_routes, "WorkspaceRegistry", workspace_factory)
    monkeypatch.setattr(
        global_routes,
        "SessionRegistry",
        type(
            "_SR",
            (),
            {"get_default": staticmethod(lambda: _FakeSessions(sessions))},
        ),
    )
    if worker_manager is None:
        monkeypatch.setattr(global_routes, "_get_worker_manager", None)
    else:
        monkeypatch.setattr(
            global_routes, "_get_worker_manager", lambda: worker_manager
        )
    monkeypatch.setattr(global_routes, "vault_root", lambda: vault)
    monkeypatch.setattr(
        global_routes,
        "_collect_active_containers",
        lambda entries: (list(containers), container_warning),
    )
    return vault


def test_global_summary_working_and_idle_statuses(
    client, monkeypatch, tmp_path
):
    """Open sessions flip status to 'working'; allow_host_resources reads config.json."""
    vault = _patch_summary_deps(
        monkeypatch,
        tmp_path,
        workspace_entries=[
            _workspace("ws-a", root_path="/tmp/ws-a", last_opened="2026-01-01T10:00:00Z", updated_at="2026-01-01T09:00:00Z"),
            _workspace("ws-b", root_path="/tmp/ws-b", updated_at="2026-01-01T09:00:00Z"),
        ],
        sessions={
            "s1": _session("s1", "ws-a", created_at="2026-01-01T10:00:00Z"),
            "s2": _session("s2", "ws-a", is_open=False),
            "s3": _session("s3", "ws-b", is_open=False, created_at="2026-01-01T11:00:00Z"),
        },
        worker_manager=_FakeWorkerManager(),
        containers=[{"id": "c1", "name": "tm-res-x", "type": "resource"}],
    )
    # ws-a allows host resources; ws-b has no config.json -> default False.
    ws_a_cfg = vault / "workspaces" / "ws-a"
    ws_a_cfg.mkdir(parents=True)
    (ws_a_cfg / "config.json").write_text(
        json.dumps({"allow_host_resources": True}), encoding="utf-8"
    )

    resp = client.get("/api/global/summary")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"workspaces", "active_sessions", "active_containers"}
    assert "warning" not in body

    by_id = {w["id"]: w for w in body["workspaces"]}
    assert len(by_id) == 2
    assert by_id["ws-a"]["status"] == "working"
    assert by_id["ws-a"]["active_sessions_count"] == 1
    assert by_id["ws-a"]["total_workers"] == 1
    assert by_id["ws-a"]["last_active"] == "2026-01-01T10:00:00Z"
    assert by_id["ws-a"]["allow_host_resources"] is True
    assert by_id["ws-b"]["status"] == "idle"
    assert by_id["ws-b"]["active_sessions_count"] == 0
    assert by_id["ws-b"]["allow_host_resources"] is False

    assert [s["session_id"] for s in body["active_sessions"]] == ["s1"]
    assert body["active_sessions"][0]["workspace_id"] == "ws-a"
    assert body["active_sessions"][0]["worker_count"] == 1
    assert len(body["active_containers"]) == 1


def test_global_summary_degrades_with_warning(client, monkeypatch, tmp_path):
    """Unavailable components degrade to [] plus a top-level warning."""
    _patch_summary_deps(
        monkeypatch,
        tmp_path,
        workspace_entries=[],
        sessions={},
        worker_manager=None,
        containers=(),
        container_warning="containers unavailable (docker SDK missing or daemon unreachable)",
        registry_raises=True,
    )

    resp = client.get("/api/global/summary")

    assert resp.status_code == 200
    body = resp.json()
    assert body["workspaces"] == []
    assert body["active_sessions"] == []
    assert body["active_containers"] == []
    assert "warning" in body
    assert "workspaces unavailable" in body["warning"]
    assert "workers unavailable" in body["warning"]
    assert "containers unavailable" in body["warning"]


def test_global_summary_build_summary_never_raises(monkeypatch, tmp_path):
    """Direct _build_summary() call with fakes returns the full shape."""
    _patch_summary_deps(
        monkeypatch,
        tmp_path,
        workspace_entries=[_workspace("ws-a", root_path="/tmp/ws-a")],
        sessions={},
        worker_manager=_FakeWorkerManager(),
    )

    body = global_routes._build_summary()

    assert set(body.keys()) == {"workspaces", "active_sessions", "active_containers"}
    assert body["workspaces"][0]["status"] == "idle"
    assert body["active_sessions"] == []
    assert body["active_containers"] == []
