"""Tests for the system health endpoint (filesystem‑based implementation)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# ── Build a minimal FastAPI app with just the health router ─────────────


@pytest.fixture
def app():
    """Create a fresh FastAPI app with the health router."""
    from fastapi import FastAPI

    app = FastAPI()
    from web_ui.backend.health_routes import router

    app.include_router(router)
    return app


@pytest.fixture
def client(app):
    """Return a TestClient bound to the app."""
    return TestClient(app)


# ── Helpers to seed filesystem worker status ────────────────────────────


def _make_legacy_status(workers_dir: Path, name: str, status: dict):
    """Write status.json in legacy layout: workers/<name>/status.json"""
    d = workers_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(json.dumps(status), encoding="utf-8")


def _make_session_status(
    workers_dir: Path, session_id: str, name: str, status: dict
):
    """Write status.json in session-scoped layout:
    workers/<session_id>/<name>/status.json
    """
    d = workers_dir / session_id / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(json.dumps(status), encoding="utf-8")


# ── Tests ────────────────────────────────────────────────────────────────


class TestHealthEndpoint:
    """Tests for GET /api/system/health"""

    def test_empty_response(self, client):
        """When no workspaces or sessions exist, return empty arrays."""
        with patch(
            "web_ui.backend.health_routes._WORKSPACES_DIR",
            Path(tempfile.mkdtemp(prefix="test_empty_")),
        ):
            resp = client.get("/api/system/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["running_workers"] == []
            assert data["active_sessions"] == []
            assert "entries" in data["recent_event_log_tail"]

    def test_legacy_worker_status(self, client, tmp_path: Path):
        """status.json in workers/<name>/ is picked up."""
        workspaces = tmp_path / "workspaces"
        ws_dir = workspaces / "ws_001"
        workers_dir = ws_dir / "workers"
        _make_legacy_status(
            workers_dir,
            "coder",
            {
                "runtime_status": "busy",
                "current_task": "writing tests",
                "last_heartbeat": "2025-01-01T00:00:00",
            },
        )
        with patch(
            "web_ui.backend.health_routes._WORKSPACES_DIR", workspaces
        ):
            resp = client.get("/api/system/health")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["running_workers"]) == 1
            w = data["running_workers"][0]
            assert w["worker_name"] == "coder"
            assert w["status"] == "busy"
            assert w["current_task"] == "writing tests"

    def test_session_scoped_worker_status(self, client, tmp_path: Path):
        """status.json in workers/<session_id>/<name>/ is picked up."""
        workspaces = tmp_path / "workspaces"
        ws_dir = workspaces / "ws_002"
        workers_dir = ws_dir / "workers"
        _make_session_status(
            workers_dir,
            "session-abc",
            "helper",
            {
                "runtime_status": "ready",
                "current_task": None,
                "session_id": "session-abc",
            },
        )
        with patch(
            "web_ui.backend.health_routes._WORKSPACES_DIR", workspaces
        ):
            resp = client.get("/api/system/health")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["running_workers"]) == 1
            w = data["running_workers"][0]
            assert w["worker_name"] == "helper"
            assert w["session_id"] == "session-abc"

    def test_session_scoped_fallback_session_id(self, client, tmp_path: Path):
        """When status.json lacks session_id, use dir name as fallback."""
        workspaces = tmp_path / "workspaces"
        ws_dir = workspaces / "ws_003"
        workers_dir = ws_dir / "workers"
        # status.json has NO session_id
        _make_session_status(
            workers_dir,
            "session-xyz",
            "worker_a",
            {"runtime_status": "completed", "current_task": None},
        )
        with patch(
            "web_ui.backend.health_routes._WORKSPACES_DIR", workspaces
        ):
            resp = client.get("/api/system/health")
            assert resp.status_code == 200
            data = resp.json()
            w = data["running_workers"][0]
            # session_id should fall back to the parent dir name
            assert w["session_id"] == "session-xyz"

    def test_workspace_filter(self, client, tmp_path: Path):
        """?workspace= filters results to matching workspace paths."""
        workspaces = tmp_path / "workspaces"
        ws_a = workspaces / "proj_alpha"
        ws_b = workspaces / "proj_beta"
        _make_legacy_status(
            ws_a / "workers", "worker_1", {"runtime_status": "busy"}
        )
        _make_legacy_status(
            ws_b / "workers", "worker_2", {"runtime_status": "idle"}
        )
        # Write config.json to resolve workspace paths
        for ws_dir, label in [(ws_a, "alpha"), (ws_b, "beta")]:
            (ws_dir / "config.json").write_text(
                json.dumps({"root": f"/projects/{label}"}), encoding="utf-8"
            )
        with patch(
            "web_ui.backend.health_routes._WORKSPACES_DIR", workspaces
        ):
            resp = client.get("/api/system/health?workspace=alpha")
            assert resp.status_code == 200
            data = resp.json()
            # Only the alpha worker should be returned
            names = [w["worker_name"] for w in data["running_workers"]]
            assert names == ["worker_1"]

    def test_event_log_tail_empty(self, client):
        """When event log does not exist, return empty entries with a note."""
        with patch(
            "web_ui.backend.health_routes._WORKSPACES_DIR",
            Path(tempfile.mkdtemp(prefix="test_evt_")),
        ):
            with patch(
                "agent.logging.event_logger.EventLogger.get_tail"
            ) as mock_get_tail:
                mock_get_tail.return_value = []
                resp = client.get("/api/system/health")
                assert resp.status_code == 200
                data = resp.json()
                tail = data["recent_event_log_tail"]
                assert tail["entries"] == []
                assert tail["note"] is not None

    def test_event_log_tail_with_entries(self, client):
        """When event log has entries, they are returned."""
        fake_entries = [
            {"timestamp": "2025-01-01T00:00:00", "event_type": "test"},
            {"timestamp": "2025-01-01T00:01:00", "event_type": "test", "data": {"msg": "hello"}},
        ]
        with patch(
            "web_ui.backend.health_routes._WORKSPACES_DIR",
            Path(tempfile.mkdtemp(prefix="test_evt2_")),
        ):
            with patch(
                "agent.logging.event_logger.EventLogger.get_tail"
            ) as mock_get_tail:
                mock_get_tail.return_value = fake_entries
                resp = client.get("/api/system/health")
                assert resp.status_code == 200
                data = resp.json()
                tail = data["recent_event_log_tail"]
                assert tail["entries"] == fake_entries
                assert tail["note"] is None

    def test_both_layouts_simultaneously(self, client, tmp_path: Path):
        """Legacy and session-scoped workers both appear."""
        workspaces = tmp_path / "workspaces"
        ws_dir = workspaces / "ws_mixed"
        workers_dir = ws_dir / "workers"
        # Legacy
        _make_legacy_status(
            workers_dir, "legacy_worker", {"runtime_status": "ready"}
        )
        # Session-scoped
        _make_session_status(
            workers_dir,
            "session-1",
            "session_worker",
            {"runtime_status": "busy", "session_id": "session-1"},
        )
        with patch(
            "web_ui.backend.health_routes._WORKSPACES_DIR", workspaces
        ):
            resp = client.get("/api/system/health")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["running_workers"]) == 2
            names = {w["worker_name"] for w in data["running_workers"]}
            assert names == {"legacy_worker", "session_worker"}

    def test_active_sessions_via_store(self, client):
        """active_sessions come from FileSystemSessionStore.

        We mock the store to return predictable data.
        """
        from unittest.mock import MagicMock

        mock_session = MagicMock()
        mock_session.workspace_id = "ws_001"
        mock_session.mode = "chat"
        mock_session.created_at = "2025-01-01T00:00:00"
        mock_session.updated_at = "2025-01-01T01:00:00"
        mock_session.metadata = {"mode": "chat"}

        with patch(
            "web_ui.backend.health_routes._WORKSPACES_DIR",
            Path(tempfile.mkdtemp(prefix="test_sess_")),
        ):
            with patch(
                "session.store.FileSystemSessionStore"
            ) as MockStore:
                instance = MockStore.return_value
                instance.get_open_sessions.return_value = ["sess-001"]
                instance.load_session.return_value = mock_session

                with patch(
                    "thoughtmachine.workspace_capabilities._workspace_dir"
                ) as mock_ws_dir:
                    mock_ws_dir.return_value = Path("/tmp/fake_ws")
                    resp = client.get("/api/system/health")
                    assert resp.status_code == 200
                    data = resp.json()
                    sessions = data["active_sessions"]
                    assert len(sessions) >= 1
                    # Find our mocked session
                    sess = next(
                        (s for s in sessions if s["session_id"] == "sess-001"),
                        None,
                    )
                    if sess:
                        assert sess["mode"] == "chat"
                        assert sess["workspace"] is not None
