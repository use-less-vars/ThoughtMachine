"""
Container logs REST route:
  GET /api/workspace/{workspace_id}/containers/{container_name}/logs

Runs with a fully mocked ContainerManager (no Docker daemon required).
Covers the happy path (payload mirrors the ContainerLogsTool shape plus the
``container`` name), ``tail`` clamping / validation, and the error mapping.

Run:  python3 -m pytest tests/test_container_logs_route.py -q
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import web_ui.backend.server as server_module
from web_ui.backend.server import app

client = TestClient(app)

WS = "code_agent"
NAME = "sandbox"
CID = "abc123"


class _FakeManager:
    """Minimal stand-in for ContainerManager used by the logs route."""

    def __init__(self, logs=None, logs_error=None, containers=None):
        self._logs = {"stdout": "hello\n", "stderr": "warn\n"} if logs is None else logs
        self._logs_error = logs_error
        self._containers = (
            [{"name": NAME, "container_id": CID, "status": "running"}]
            if containers is None else containers
        )
        self.calls = []

    def list_containers(self):
        return list(self._containers)

    def get_logs(self, container_id, tail=100, since=None):
        self.calls.append(
            {"container_id": container_id, "tail": tail, "since": since}
        )
        if self._logs_error is not None:
            raise self._logs_error
        return dict(self._logs)


def _install(monkeypatch, manager):
    monkeypatch.setattr(
        server_module, "_make_container_manager",
        lambda workspace_id, workspace_path="": manager,
    )
    return manager


def _get(tail=None):
    params = {} if tail is None else {"tail": tail}
    return client.get(
        f"/api/workspace/{WS}/containers/{NAME}/logs", params=params
    )


class TestContainerLogsRoute:
    def test_happy_path_matches_tool_shape(self, monkeypatch):
        manager = _install(monkeypatch, _FakeManager())
        resp = _get()
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {
            "container", "success", "stdout", "stderr", "duration"}
        assert body["container"] == NAME
        assert body["success"] is True
        assert body["stdout"] == "hello\n"
        assert body["stderr"] == "warn\n"
        assert isinstance(body["duration"], float)
        assert manager.calls == [
            {"container_id": CID, "tail": 200, "since": None}]

    def test_tail_clamped_to_max(self, monkeypatch):
        manager = _install(monkeypatch, _FakeManager())
        resp = _get(tail=999999)
        assert resp.status_code == 200
        assert server_module._CONTAINER_LOGS_MAX_TAIL == 10000
        assert manager.calls[0]["tail"] == 10000

    def test_negative_tail_rejected(self, monkeypatch):
        manager = _install(monkeypatch, _FakeManager())
        resp = _get(tail=-5)
        assert resp.status_code == 400
        assert "tail" in resp.json()["error"]
        assert manager.calls == []

    def test_unknown_container_404(self, monkeypatch):
        _install(monkeypatch, _FakeManager(containers=[]))
        resp = _get()
        assert resp.status_code == 404
        assert NAME in resp.json()["error"]

    def test_not_found_runtime_error_is_404(self, monkeypatch):
        _install(monkeypatch, _FakeManager(
            logs_error=RuntimeError(f"Container {CID} not found")))
        assert _get().status_code == 404

    def test_other_runtime_error_is_503(self, monkeypatch):
        _install(monkeypatch, _FakeManager(
            logs_error=RuntimeError("Docker Python SDK not available")))
        assert _get().status_code == 503

    def test_manager_construction_failure_is_503(self, monkeypatch):
        def _boom(workspace_id, workspace_path=""):
            raise RuntimeError("boom")
        monkeypatch.setattr(server_module, "_make_container_manager", _boom)
        assert _get().status_code == 503

    def test_unresolvable_workspace_is_404(self, monkeypatch):
        monkeypatch.setattr(
            server_module, "_make_container_manager",
            lambda workspace_id, workspace_path="": None)
        assert _get().status_code == 404
