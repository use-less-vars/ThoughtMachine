"""
Phase 3 item 8 — worker/container stats fields (additive, backend-only).

Covers:
  - GET /api/workspace/{ws_id}/workers: ``time_since_last_query`` (derived
    from status.json ``last_heartbeat``; None when absent/unparseable) and
    ``pruned_since_last_query`` (read from context.json; 0 when missing).
  - GET /api/workspace/{ws_id}/containers: ``containers_in_use`` and
    ``containers_available`` (cap - in_use, clamped >= 0).
  - WorkerThread prune counter: increments on the F1 / WLM stale-abandon
    prune paths and survives a restart via context.json persistence.

Run:  python3 -m pytest web_ui/backend/tests/test_worker_stats_routes.py -q
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import web_ui.backend.server as server_module
import web_ui.backend.workspace_routes as workspace_routes
from agent.core.worker_context import WorkerContext
from tools.workspace.worker import WorkerThread
from web_ui.backend.server import app

client = TestClient(app)


def _iso_ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def fake_ws_dir(monkeypatch, tmp_path):
    """Point the workers endpoint at a throwaway workspace directory."""
    ws_dir = tmp_path / "ws-test"
    ws_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(workspace_routes, "_workspace_dir", lambda ws_id: ws_dir)
    monkeypatch.setattr(workspace_routes, "ensure_workspace_dirs", lambda ws_id: None)
    return ws_dir


# ══════════════════════════════════════════════════════════════════════════
# GET /api/workspace/{ws_id}/workers
# ══════════════════════════════════════════════════════════════════════════


class TestWorkersEndpointStatsFields:
    def test_time_since_last_query_and_pruned_counter(self, fake_ws_dir):
        """Legacy + session-scoped workers: new fields populated from
        status.json/context.json; old fields unchanged."""
        (fake_ws_dir / "workers.json").write_text(
            json.dumps([{"name": "coder"}, {"name": "idle"}]), encoding="utf-8"
        )
        worker_dir = fake_ws_dir / "workers" / "coder"
        worker_dir.mkdir(parents=True)
        (worker_dir / "status.json").write_text(
            json.dumps({
                "runtime_status": "ready",
                "current_task": None,
                "last_heartbeat": _iso_ago(60),
                "error": None,
                "session_id": None,
                "current_context_tokens": 100,
                "max_context_tokens": 8000,
            }),
            encoding="utf-8",
        )
        (worker_dir / "context.json").write_text(
            json.dumps({"pruned_since_last_query": 3, "generation": 1}),
            encoding="utf-8",
        )

        # Session-scoped worker with NO heartbeat and NO context.json
        sess_dir = fake_ws_dir / "workers" / "sess1"
        (sess_dir / "idle").mkdir(parents=True)
        (sess_dir / "idle" / "status.json").write_text(
            json.dumps({
                "runtime_status": "ready",
                "current_task": None,
                "last_heartbeat": None,
                "error": None,
            }),
            encoding="utf-8",
        )

        resp = client.get("/api/workspace/ws-test/workers")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        by_name = {e["name"]: e for e in data}
        assert set(by_name) == {"coder", "idle"}

        coder = by_name["coder"]
        assert 59.0 <= coder["time_since_last_query"] <= 61.0
        assert coder["pruned_since_last_query"] == 3
        # Backwards compatibility: pre-existing fields unchanged
        assert coder["runtime_status"] == "ready"
        assert coder["current_context_tokens"] == 100
        assert coder["has_persisted_context"] is True
        assert coder["last_heartbeat"] is not None

        idle = by_name["idle"]
        assert idle["time_since_last_query"] is None
        assert idle["pruned_since_last_query"] == 0
        assert idle["has_persisted_context"] is False

    def test_name_filter_includes_new_fields(self, fake_ws_dir):
        (fake_ws_dir / "workers.json").write_text(
            json.dumps([{"name": "coder"}]), encoding="utf-8"
        )
        worker_dir = fake_ws_dir / "workers" / "coder"
        worker_dir.mkdir(parents=True)
        (worker_dir / "status.json").write_text(
            json.dumps({
                "runtime_status": "busy",
                "current_task": "working",
                "last_heartbeat": _iso_ago(5),
                "error": None,
            }),
            encoding="utf-8",
        )

        resp = client.get("/api/workspace/ws-test/workers?name=coder")
        assert resp.status_code == 200, resp.text
        entry = resp.json()
        assert entry["name"] == "coder"
        assert 0.0 <= entry["time_since_last_query"] <= 6.0
        assert entry["pruned_since_last_query"] == 0
        assert entry["current_task"] == "working"

    def test_unparseable_heartbeat_yields_none(self, fake_ws_dir):
        (fake_ws_dir / "workers.json").write_text(
            json.dumps([{"name": "w"}]), encoding="utf-8"
        )
        worker_dir = fake_ws_dir / "workers" / "w"
        worker_dir.mkdir(parents=True)
        (worker_dir / "status.json").write_text(
            json.dumps({
                "runtime_status": "ready",
                "last_heartbeat": "not-a-timestamp",
                "error": None,
            }),
            encoding="utf-8",
        )

        resp = client.get("/api/workspace/ws-test/workers")
        assert resp.status_code == 200, resp.text
        entry = resp.json()[0]
        assert entry["time_since_last_query"] is None
        assert entry["pruned_since_last_query"] == 0

    def test_corrupt_context_json_does_not_crash(self, fake_ws_dir):
        (fake_ws_dir / "workers.json").write_text(
            json.dumps([{"name": "w"}]), encoding="utf-8"
        )
        worker_dir = fake_ws_dir / "workers" / "w"
        worker_dir.mkdir(parents=True)
        (worker_dir / "context.json").write_text("{not json", encoding="utf-8")

        resp = client.get("/api/workspace/ws-test/workers")
        assert resp.status_code == 200, resp.text
        entry = resp.json()[0]
        assert entry["pruned_since_last_query"] == 0


# ══════════════════════════════════════════════════════════════════════════
# GET /api/workspace/{workspace_id}/containers
# ══════════════════════════════════════════════════════════════════════════


class _FakeManager:
    """Minimal ContainerManager stand-in with list_containers() + cap."""

    def __init__(self, containers, cap=4):
        self._containers = containers
        self.max_containers = cap

    def list_containers(self):
        return self._containers


class TestContainersEndpointStatsFields:
    def _get(self, monkeypatch, containers, cap=4):
        monkeypatch.setattr(
            server_module, "_make_container_manager",
            lambda ws_id, workspace_path="": _FakeManager(containers, cap),
        )
        return client.get("/api/workspace/ws-test/containers")

    def test_in_use_and_available(self, monkeypatch):
        resp = self._get(monkeypatch, [{"name": "c1", "container_id": "id1"}], cap=4)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["containers_in_use"] == 1
        assert data["containers_available"] == 3
        assert len(data["containers"]) == 1

    def test_full_cap_yields_zero_available(self, monkeypatch):
        containers = [{"name": f"c{i}", "container_id": f"id{i}"} for i in range(4)]
        resp = self._get(monkeypatch, containers, cap=4)
        data = resp.json()
        assert data["containers_in_use"] == 4
        assert data["containers_available"] == 0

    def test_available_clamped_at_zero(self, monkeypatch):
        containers = [{"name": f"c{i}", "container_id": f"id{i}"} for i in range(4)]
        resp = self._get(monkeypatch, containers, cap=2)
        data = resp.json()
        assert data["containers_in_use"] == 4
        assert data["containers_available"] == 0

    def test_no_containers(self, monkeypatch):
        resp = self._get(monkeypatch, [], cap=4)
        data = resp.json()
        assert data["containers_in_use"] == 0
        assert data["containers_available"] == 4
        assert data["containers"] == []

    def test_missing_manager_404_preserved(self, monkeypatch):
        monkeypatch.setattr(
            server_module, "_make_container_manager",
            lambda ws_id, workspace_path="": None,
        )
        resp = client.get("/api/workspace/ws-test/containers")
        assert resp.status_code == 404
        assert "not found" in resp.json()["error"]

    def test_list_failure_503_preserved(self, monkeypatch):
        class _Boom:
            def list_containers(self):
                raise RuntimeError("docker down")

        monkeypatch.setattr(
            server_module, "_make_container_manager",
            lambda ws_id, workspace_path="": _Boom(),
        )
        resp = client.get("/api/workspace/ws-test/containers")
        assert resp.status_code == 503


# ══════════════════════════════════════════════════════════════════════════
# WorkerThread prune counter (increment + persistence)
# ══════════════════════════════════════════════════════════════════════════


class TestPruneCounter:
    def _make_thread(self, tmp_path, name="w1"):
        return WorkerThread(
            name=name,
            definition={},
            agent_config={},
            workspace_dir=Path(tmp_path),
        )

    def _seed_stale_attempt(self, thread):
        thread._worker_ctx = WorkerContext(worker_name=thread.worker_name, user_history=[
            {"role": "system", "content": "Initial context: {\"query\": \"old\"}"},
            {"role": "user", "content": "old query"},
            {"role": "assistant", "content": "partial reply"},
        ])
        thread._last_query = "old"
        thread._last_completed_query = "other"  # incomplete attempt

    def test_f1_prune_increments_and_persists(self, tmp_path):
        t = self._make_thread(tmp_path)
        self._seed_stale_attempt(t)
        assert t._pruned_since_last_query == 0

        t._prune_stale_attempt_before_merge("new query")
        assert t._pruned_since_last_query == 1
        assert t._worker_ctx.user_history == []

        t._save_context()
        # Simulate restart: a fresh thread restores the counter from context.json
        t2 = self._make_thread(tmp_path)
        ctx = t2._load_context()
        assert ctx is not None
        assert t2._pruned_since_last_query == 1

    def test_completed_attempt_not_pruned(self, tmp_path):
        t = self._make_thread(tmp_path)
        self._seed_stale_attempt(t)
        t._last_completed_query = "old"  # completed — preserved

        t._prune_stale_attempt_before_merge("new query")
        assert t._pruned_since_last_query == 0
        assert len(t._worker_ctx.user_history) == 3

    def test_wlm_abandoned_prune_increments(self, tmp_path):
        class _FakeWLM:
            def abandoned_query_ids(self):
                return {"q1"}

            def completed_query_ids(self):
                return set()

        t = self._make_thread(tmp_path)
        self._seed_stale_attempt(t)
        t._last_query_id = "q1"
        t._wlm = _FakeWLM()

        t._prune_abandoned_attempt_via_wlm("new query")
        assert t._pruned_since_last_query == 1
        assert t._worker_ctx.user_history == []

    def test_wlm_completed_attempt_not_pruned(self, tmp_path):
        class _FakeWLM:
            def abandoned_query_ids(self):
                return set()

            def completed_query_ids(self):
                return {"q1"}

        t = self._make_thread(tmp_path)
        self._seed_stale_attempt(t)
        t._last_query_id = "q1"
        t._wlm = _FakeWLM()

        t._prune_abandoned_attempt_via_wlm("new query")
        assert t._pruned_since_last_query == 0
        assert len(t._worker_ctx.user_history) == 3
