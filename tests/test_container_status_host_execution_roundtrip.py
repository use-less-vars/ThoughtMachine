"""RED (COMMIT 4 / gap iv): route projection round-trip against the REAL writer.

Gap closed: ``tests/test_container_status_host_execution.py`` fabricates the JSONL
with a hand-written helper that mirrors the route's own expectation, so it proves
the route parses *a* file in *its* format. It never proves that the real producer
(``GitInfoTool._record_host_fallback_event``) and the real consumer
(``GET /api/workspace/{ws}/containers/{name}/status``) agree on path and shape.

These tests drive the REAL writer and read the result back through the REAL route.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import web_ui.backend.server as server_module
from web_ui.backend.server import app
from tools.git_info_tool import GitInfoTool

WS = "rt-ws"
STATUS = "/api/workspace/{ws}/containers/{name}/status"

client = TestClient(app)


class _FakeStatusManager:
    def list_containers(self):
        return [{"name": "c1", "container_id": "id1"}]

    def status(self, container_id):
        return {"container_id": container_id, "name": "c1", "status": "running"}


@pytest.fixture
def status_vault(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))
    monkeypatch.setattr(
        server_module,
        "_make_container_manager",
        lambda workspace_id, workspace_path="": _FakeStatusManager(),
    )
    return vault


def _write_with_real_writer(vault, ws, detail=None):
    """Append one event with the REAL production writer; return (log, events)."""
    tool = GitInfoTool(operation="status")
    object.__setattr__(tool, "_resolved_workspace_id", ws)
    tool._record_host_fallback_event(detail)
    log = vault / "workspaces" / ws / "resources" / "git.host_execution.jsonl"
    events = [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return log, events


def test_route_surfaces_event_written_by_real_writer(status_vault):
    log, events = _write_with_real_writer(status_vault, WS)
    assert log.is_file()
    assert len(events) == 1

    resp = client.get(STATUS.format(ws=WS, name="c1"))
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("host_execution") == {
        "fallback": True,
        "reason": "container_unavailable",
        "at": events[0]["timestamp"],
    }
    assert set(body["host_execution"]) == {"fallback", "reason", "at"}


def test_route_surfaces_last_of_multiple_real_events(status_vault):
    _, first = _write_with_real_writer(status_vault, WS)
    _, events = _write_with_real_writer(status_vault, WS)
    assert len(first) == 1
    assert len(events) == 2

    resp = client.get(STATUS.format(ws=WS, name="c1"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["host_execution"]["at"] == events[-1]["timestamp"]
