"""Server /health endpoint contract (hardening sprint, Step 2).

Proves the REAL FastAPI app (web_ui/backend/server.py) serves ``GET /health``
with the deployment-verification payload: status/service identity plus the git
revision the server was built from (``_SERVER_REVISION``, captured at import
time via ``git rev-parse HEAD``).

Hermetic harness mirrors tests/integration/test_apply_config_coverage.py: temp
HOME + patched ``Path.home()`` + purged/re-imported web_ui.backend modules so
module-level singletons are built against the temp HOME.  No network, no LLM,
no Docker daemon.  The lifespan runs inside ``TestClient(app)`` — safe here
(container scan is wrapped in try/except, and HOME is a throwaway temp dir).

Run (from repo root):
    python -m pytest tests/integration/test_server_health.py -v
"""

from __future__ import annotations

import contextlib
import importlib
import os
import pathlib
import shutil
import subprocess
import sys as sys_mod
import tempfile
from pathlib import Path
from unittest import mock
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

pytestmark = pytest.mark.integration


# ════════════════════════════════════════════════════════════════════════════
# Hermetic full-server harness (EXACT mirror of test_apply_config_coverage.py)
# ════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def contract_server():
    """Temp HOME + purged modules + fresh import of web_ui.backend.server."""
    tmp_home = tempfile.mkdtemp(prefix="test_server_health_")
    fake_home_path = Path(tmp_home)

    old_home_env = os.environ.get("HOME")
    os.environ["HOME"] = tmp_home

    saved_env = {}
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_COMPATIBLE_API_KEY"):
        saved_env[key] = os.environ.pop(key, None)

    patcher = patch.object(pathlib.Path, "home", return_value=fake_home_path)
    patcher.start()

    # Re-import server so module-level singletons (_session_store, registries,
    # _SERVER_REVISION) are built against the temp HOME, not the real one.
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
    """Return the (purged-then-imported) web_ui.backend.server module."""
    return importlib.import_module("web_ui.backend.server")


# ════════════════════════════════════════════════════════════════════════════
# Case 1 — /health identity payload
# ════════════════════════════════════════════════════════════════════════════

def test_health_endpoint_status_ok(contract_server):
    """GET /health → 200 with status 'ok' and service identity."""
    app, _ = contract_server
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "ok"
        assert payload["service"] == "thoughtmachine-web-ui"


@pytest.mark.skipif(shutil.which('git') is None, reason='git binary not available in CI sandbox')
def test_health_endpoint_reports_git_revision(contract_server):
    """GET /health → revision equals the repo's git HEAD at import time.

    The server computes _SERVER_REVISION from _project_root (server.py:165-189);
    this test re-derives the expected value the exact same way and also pins
    the broadcast payload against the module constant.
    """
    app, _ = contract_server
    server_mod = _server_mod()
    assert server_mod._SERVER_REVISION, "server must compute a revision"

    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=server_mod._project_root,
        capture_output=True,
        text=True,
        timeout=5.0,
    ).stdout.strip()
    assert expected, "repo must have a git HEAD for the revision check"

    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["revision"] == expected
        assert payload["revision"] == server_mod._SERVER_REVISION


# -----------------------------------------------------------------------------
# Case 2 - Phase 7: per-workspace container lifecycle API
# (GET /api/workspace/{ws_id}/containers, .../{name}/status,
#  POST .../start, POST .../stop, DELETE .../{name}, GET /api/health/containers)
# All ContainerManager use is mocked; Docker daemon is never touched.
# -----------------------------------------------------------------------------

class _FakeContainerManager:
    """Canned ContainerManager double: records calls, returns fixed results.

    Mirrors the real ContainerManager contract (list_containers/status/start/
    stop/remove, none of which raise).
    """

    def __init__(self, *args, **kwargs):
        self.workspace_path = kwargs.get("workspace_path")
        self.workspace_id = kwargs.get("workspace_id")
        self.list_calls = 0
        self.status_calls = []
        self.start_calls = []
        self.stop_calls = []
        self.remove_calls = []
        self.start_result = None  # optional override (e.g. limit-conflict error)

    def list_containers(self):
        self.list_calls += 1
        return [{
            "container_id": "c1",
            "name": "box-a",
            "image": "agent-executor",
            "status": "running",
            "uptime_seconds": 5,
            "workspace_id": "ws-1",
            "note": "n1",
        }]

    def status(self, container_id):
        self.status_calls.append(container_id)
        return {
            "container_id": container_id,
            "name": "box-a",
            "status": "running",
            "uptime_seconds": 5,
            "memory_usage_bytes": None,
            "note": "",
        }

    def start(self, image=None, name=None, note=None):
        self.start_calls.append({"image": image, "name": name, "note": note})
        if self.start_result is not None:
            return self.start_result
        return {"id": "c1", "name": name, "status": "reused", "note": note or ""}

    def stop(self, container_id):
        self.stop_calls.append(container_id)
        return {"status": "stopped", "container_id": container_id, "name": "box-a"}

    def remove(self, container_id):
        self.remove_calls.append(container_id)
        return {"status": "removed", "container_id": container_id}


def _make_fake_manager():
    """Return a fresh _FakeContainerManager bound to the canned container."""
    return _FakeContainerManager(workspace_path="/tmp/ws", workspace_id="ws-1")


@contextlib.contextmanager
def _registered_ws(tmp_home: str):
    """Temporarily register workspace 'ws-1' at an in-home path.

    The container lifecycle endpoints validate an explicit ``workspace_path``
    against $HOME (excluding the vault) and require the workspace to be
    registered, so the fixture's temp HOME is used instead of a hardcoded
    /tmp path.  The entry is removed afterwards so
    test_list_containers_unknown_workspace keeps its 404 contract (ws-1 is
    unresolvable without an explicit workspace_path).

    Yields the in-home workspace path to pass as the query parameter.
    """
    from thoughtmachine.workspace_registry import WorkspaceRegistry

    ws_path = os.path.join(tmp_home, "ws")
    os.makedirs(ws_path, exist_ok=True)
    registry = WorkspaceRegistry.get_default()
    if registry.get_workspace("ws-1") is None:
        registry.register_workspace("ws-1", ws_path)
    try:
        yield ws_path
    finally:
        registry.unregister_workspace("ws-1")


# -- GET /api/workspace/{workspace_id}/containers ------------------------------

def test_list_containers_endpoint(contract_server):
    """GET .../containers?workspace_path=... -> 200 with the container list."""
    app, tmp_home = contract_server
    fake = _make_fake_manager()
    with _registered_ws(tmp_home) as ws_path:
        with TestClient(app) as client:
            with mock.patch("infra.container_manager.ContainerManager") as cm_cls:
                cm_cls.return_value = fake
                resp = client.get(
                    "/api/workspace/ws-1/containers",
                    params={"workspace_path": ws_path},
                )
    assert resp.status_code == 200
    # NOTE: the endpoint now also reports container capacity alongside the
    # list — containers_in_use (running) and containers_available (capacity
    # minus in-use). The fake manager has no max_containers, so capacity
    # defaults to 4: 1 running container leaves 3 available.
    payload = resp.json()
    assert payload["containers"] == fake.list_containers()
    assert payload["containers_in_use"] == 1
    assert payload["containers_available"] == 3
    assert fake.list_calls >= 1


def test_list_containers_unknown_workspace(contract_server):
    """No workspace_path and unknown ws_id -> 404 (registry lookup fails)."""
    app, _ = contract_server
    with TestClient(app) as client:
        resp = client.get("/api/workspace/ws-1/containers")
    assert resp.status_code == 404
    assert resp.json() == {
        "error": "workspace 'ws-1' not found or path unresolvable"
    }


def test_list_containers_manager_failure(contract_server):
    """ContainerManager construction failure -> 503 with the error text."""
    app, tmp_home = contract_server
    with _registered_ws(tmp_home) as ws_path:
        with TestClient(app) as client:
            with mock.patch(
                "infra.container_manager.ContainerManager",
                side_effect=RuntimeError("boom"),
            ):
                resp = client.get(
                    "/api/workspace/ws-1/containers",
                    params={"workspace_path": ws_path},
                )
    assert resp.status_code == 503
    assert resp.json() == {"error": "boom"}


# -- GET /api/workspace/{workspace_id}/containers/{name}/status ----------------

def test_status_endpoint(contract_server):
    """Status lookup by container NAME -> 200, resolves to the real container_id."""
    app, tmp_home = contract_server
    fake = _make_fake_manager()
    with _registered_ws(tmp_home) as ws_path:
        with TestClient(app) as client:
            with mock.patch("infra.container_manager.ContainerManager") as cm_cls:
                cm_cls.return_value = fake
                resp = client.get(
                    "/api/workspace/ws-1/containers/box-a/status",
                    params={"workspace_path": ws_path},
                )
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"
    assert resp.json()["container_id"] == "c1"
    assert fake.status_calls == ["c1"]


def test_status_not_found(contract_server):
    """Unknown container name -> 404."""
    app, tmp_home = contract_server
    fake = _make_fake_manager()
    with _registered_ws(tmp_home) as ws_path:
        with TestClient(app) as client:
            with mock.patch("infra.container_manager.ContainerManager") as cm_cls:
                cm_cls.return_value = fake
                resp = client.get(
                    "/api/workspace/ws-1/containers/nope/status",
                    params={"workspace_path": ws_path},
                )
    assert resp.status_code == 404
    assert resp.json() == {"error": "container 'nope' not found"}


# -- POST /api/workspace/{workspace_id}/containers/{name}/start ----------------

def test_start_with_note(contract_server):
    """POST .../start with a JSON note -> 200 and start(name=..., note=...)."""
    app, tmp_home = contract_server
    fake = _make_fake_manager()
    with _registered_ws(tmp_home) as ws_path:
        with TestClient(app) as client:
            with mock.patch("infra.container_manager.ContainerManager") as cm_cls:
                cm_cls.return_value = fake
                resp = client.post(
                    "/api/workspace/ws-1/containers/box-a/start",
                    params={"workspace_path": ws_path},
                    json={"note": "hi"},
                )
    assert resp.status_code == 200
    assert resp.json() == {"id": "c1", "name": "box-a", "status": "reused", "note": "hi"}
    assert fake.start_calls == [{"image": None, "name": "box-a", "note": "hi"}]


def test_start_limit_conflict(contract_server):
    """Workspace container limit reached -> 409 with the manager's error."""
    app, tmp_home = contract_server
    fake = _make_fake_manager()
    fake.start_result = {
        "error": "Workspace container limit (4) reached. "
                 "Delete a container or raise the limit before starting a new one."
    }
    with _registered_ws(tmp_home) as ws_path:
        with TestClient(app) as client:
            with mock.patch("infra.container_manager.ContainerManager") as cm_cls:
                cm_cls.return_value = fake
                resp = client.post(
                    "/api/workspace/ws-1/containers/box-a/start",
                    params={"workspace_path": ws_path},
                    json={},
                )
    assert resp.status_code == 409
    assert resp.json() == {"error": fake.start_result["error"]}


# -- POST /api/workspace/{workspace_id}/containers/{name}/stop -----------------

def test_stop_endpoint(contract_server):
    """POST .../stop -> 200 and stop() called with the resolved container_id."""
    app, tmp_home = contract_server
    fake = _make_fake_manager()
    with _registered_ws(tmp_home) as ws_path:
        with TestClient(app) as client:
            with mock.patch("infra.container_manager.ContainerManager") as cm_cls:
                cm_cls.return_value = fake
                resp = client.post(
                    "/api/workspace/ws-1/containers/box-a/stop",
                    params={"workspace_path": ws_path},
                )
    assert resp.status_code == 200
    assert resp.json() == {"status": "stopped", "container_id": "c1", "name": "box-a"}
    assert fake.stop_calls == ["c1"]


# -- DELETE /api/workspace/{workspace_id}/containers/{name} --------------------

def test_delete_endpoint(contract_server):
    """DELETE .../containers/{name} -> 200 and remove() called with c1."""
    app, tmp_home = contract_server
    fake = _make_fake_manager()
    with _registered_ws(tmp_home) as ws_path:
        with TestClient(app) as client:
            with mock.patch("infra.container_manager.ContainerManager") as cm_cls:
                cm_cls.return_value = fake
                resp = client.delete(
                    "/api/workspace/ws-1/containers/box-a",
                    params={"workspace_path": ws_path},
                )
    assert resp.status_code == 200
    assert resp.json() == {"status": "removed", "container_id": "c1"}
    assert fake.remove_calls == ["c1"]


# -- GET /api/health/containers ------------------------------------------------

def test_health_containers_ok(contract_server):
    """Docker daemon reachable -> {"status": "ok", "docker": "reachable"}."""
    app, _ = contract_server
    fake_client = mock.MagicMock()
    fake_client.ping.return_value = True
    with TestClient(app) as client:
        with mock.patch("docker.from_env", return_value=fake_client):
            resp = client.get("/api/health/containers")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "docker": "reachable"}
    fake_client.ping.assert_called_once()
    fake_client.close.assert_called_once()


def test_health_containers_degraded(contract_server):
    """Docker daemon unreachable -> {"status": "degraded", "docker": "unreachable"}."""
    app, _ = contract_server
    with TestClient(app) as client:
        with mock.patch("docker.from_env", side_effect=RuntimeError("no docker")):
            resp = client.get("/api/health/containers")
    assert resp.status_code == 200
    assert resp.json() == {"status": "degraded", "docker": "unreachable"}

