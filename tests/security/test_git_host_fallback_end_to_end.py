"""RED (COMMIT 4 / gap i): full host-fallback path, container -> unavailable ->
fallback -> WARNING -> event written -> route projection.

Invariant under test: a single agent git operation that finds the git resource
container unavailable must, in one pass,
  (a) emit the structured ``kill_switch_state=`` fallback WARNING,
  (b) append a ``host_execution`` event to the workspace vault log using the
      REAL writer (``tools/git_info_tool.py``), and
  (c) surface as ``host_execution`` on
      ``GET /api/workspace/{ws}/containers/{name}/status``.

Gap closed: the per-commit tests prove each link in isolation and never exercise
the real producer (``tools/git_info_tool.py``) together with the real consumer
(``web_ui/backend/server.py``). A producer/consumer path mismatch therefore goes
undetected. This test wires the REAL writer to the REAL route.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import web_ui.backend.server as server_module
from web_ui.backend.server import app
from tools.git_info_tool import GitInfoTool

WS = "e2e-ws"
STATUS = "/api/workspace/{ws}/containers/{name}/status"

client = TestClient(app)


class _FakeSandbox:
    instances = []

    def __init__(self, **kwargs):
        _FakeSandbox.instances.append(self)

    def run(self, *args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")


class _DegradedManager:
    """Resource manager whose git resource degraded to host fallback."""

    def ensure_resource(self, name):
        return {
            "mode": "host_fallback",
            "container_id": None,
            "status": None,
            "image": None,
            "detail": "resource image unavailable",
            "failure_reason": "build_failed",
            "fallback_used": True,
        }

    def exec(self, *args, **kwargs):
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}


class _FakeStatusManager:
    def list_containers(self):
        return [{"name": "c1", "container_id": "id1"}]

    def status(self, container_id):
        return {"container_id": container_id, "name": "c1", "status": "running"}


@pytest.fixture(autouse=True)
def _host_sandbox(monkeypatch):
    _FakeSandbox.instances = []
    monkeypatch.setattr("tools.git_info_tool.SandboxedExecution", _FakeSandbox)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    cfg = vault / "workspaces" / WS / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"allow_host_resources": True}), encoding="utf-8")
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))
    return tmp_path


def _event_log(tmp_path):
    return (
        tmp_path
        / "vault"
        / "workspaces"
        / WS
        / "resources"
        / "git.host_execution.jsonl"
    )


def test_degraded_fallback_end_to_end_surfaces_on_route(tmp_path, monkeypatch, caplog):
    tool = GitInfoTool(
        operation="status",
        session_permissions={"git": "read"},
        effective_permissions={"git": "read"},
    )
    object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
    object.__setattr__(tool, "_resolved_workspace_id", WS)
    object.__setattr__(tool, "_resource_manager", _DegradedManager())

    with caplog.at_level(logging.WARNING, logger="tools.git_info_tool"):
        result = tool._run_git_raw(tmp_path, ["status"])

    # (a) exactly one structured fallback WARNING carrying the full token set.
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "kill_switch_state=" in r.getMessage()
    ]
    assert len(warnings) == 1, warnings
    msg = warnings[0]
    assert "reason=container_unavailable" in msg
    assert f"workspace_id={WS}" in msg
    assert "kill_switch_state=on" in msg
    assert result is not None
    assert tool._last_execution_mode == "host_fallback"

    # (b) the REAL writer appended exactly one host_execution event.
    log = _event_log(tmp_path)
    assert log.is_file(), f"no event log written at {log}"
    events = [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(events) == 1
    assert events[0]["event_type"] == "host_execution"
    assert events[0]["payload"]["reason"] == "container_unavailable"
    assert events[0]["payload"]["detail"] == "resource image unavailable"

    # (c) the route projects that event as host_execution.
    manager = _FakeStatusManager()
    monkeypatch.setattr(
        server_module,
        "_make_container_manager",
        lambda workspace_id, workspace_path="": manager,
    )
    resp = client.get(STATUS.format(ws=WS, name="c1"))
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("host_execution") == {
        "fallback": True,
        "reason": "container_unavailable",
        "at": events[0]["timestamp"],
    }
