"""
Integration tests for Worker CRUD + Dockerfile PUT endpoints.

Tests cover:
  - POST /api/workspace/{ws_id}/workers          (create)
  - PUT  /api/workspace/{ws_id}/workers/{name}    (update)
  - DELETE /api/workspace/{ws_id}/workers/{name}  (delete)
  - PUT  /api/workspace/{ws_id}/dockerfile        (Dockerfile write)
  - GET  /api/workspace/{ws_id}/workers?name=     (single-worker filter)

Uses the same pattern as test_templates_endpoint.py: mock Path.home() with
tmp_path, create workspace dirs, call endpoints, verify response and disk.
"""

from __future__ import annotations

import json
import pathlib
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Minimal test app (router only — no full server bootstrap needed)
# ---------------------------------------------------------------------------

app = FastAPI()

from web_ui.backend.workspace_routes import router as workspace_router  # noqa: E402
app.include_router(workspace_router)


@pytest.fixture
def client() -> TestClient:
    """Yield a TestClient bound to the minimal app."""
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WS_ID = "test_ws"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_worker() -> Dict[str, Any]:
    """Return a minimal valid WorkerDefinition dict."""
    return {
        "name": "test-worker",
        "description": "A worker created during testing.",
        "system_prompt": "You are a test worker.\n",
        "tools": ["FileEditor", "GlobTool"],
        "permission_footprint": {"filesystem": "read"},
    }


def _workspace_path(root: Path) -> Path:
    """Return the workspace directory under a given tmp_path root."""
    return root / ".thoughtmachine" / "workspaces" / WS_ID


def _create_workspace(root: Path) -> Path:
    """Create the workspace directory (tests can call this or rely on
    ``ensure_workspace_dirs`` in the endpoint).  Returns the path."""
    ws_dir = _workspace_path(root)
    ws_dir.mkdir(parents=True, exist_ok=True)
    return ws_dir


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCreateWorker:
    """POST /api/workspace/{ws_id}/workers"""

    def test_create_worker(self, client, tmp_path):
        """POST a valid worker → 201, verify body, assert workers.json on disk."""
        with patch.object(pathlib.Path, "home") as mock_home:
            mock_home.return_value = tmp_path
            _create_workspace(tmp_path)
            body = _valid_worker()
            resp = client.post(f"/api/workspace/{WS_ID}/workers", json=body)

        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}"
        data = resp.json()
        assert data["name"] == body["name"]
        assert data["description"] == body["description"]
        assert data["system_prompt"] == body["system_prompt"]
        assert data["tools"] == body["tools"]
        assert data["permission_footprint"] == body["permission_footprint"]

        # Verify on disk
        ws_dir = _workspace_path(tmp_path)
        workers_path = ws_dir / "workers.json"
        assert workers_path.exists(), "workers.json should exist on disk"
        disk_data = json.loads(workers_path.read_text())
        assert len(disk_data) == 1
        assert disk_data[0]["name"] == body["name"]

    def test_create_duplicate(self, client, tmp_path):
        """POST same worker twice → first 201, second 409."""
        with patch.object(pathlib.Path, "home") as mock_home:
            mock_home.return_value = tmp_path
            _create_workspace(tmp_path)
            body = _valid_worker()

            # First POST
            resp1 = client.post(f"/api/workspace/{WS_ID}/workers", json=body)
            assert resp1.status_code == 201

            # Second POST (duplicate)
            resp2 = client.post(f"/api/workspace/{WS_ID}/workers", json=body)
            assert resp2.status_code == 409

            # Verify only one entry on disk
            ws_dir = _workspace_path(tmp_path)
            disk_data = json.loads((ws_dir / "workers.json").read_text())
            assert len(disk_data) == 1

    def test_create_invalid_schema(self, client, tmp_path):
        """POST with missing required field → 422."""
        with patch.object(pathlib.Path, "home") as mock_home:
            mock_home.return_value = tmp_path
            _create_workspace(tmp_path)
            # Missing 'description', 'tools', 'permission_footprint'
            invalid = {"name": "no-fields"}
            resp = client.post(f"/api/workspace/{WS_ID}/workers", json=invalid)

        assert resp.status_code == 422, f"Expected 422, got {resp.status_code}"
        detail = resp.json().get("detail", "")
        assert "description" in str(detail) or "Field required" in str(detail)

    def test_create_invalid_json(self, client, tmp_path):
        """POST with malformed body (not valid JSON) → 422 or 400."""
        with patch.object(pathlib.Path, "home") as mock_home:
            mock_home.return_value = tmp_path
            _create_workspace(tmp_path)
            # Send raw text instead of JSON
            resp = client.post(
                f"/api/workspace/{WS_ID}/workers",
                content="this is not json",
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code in (400, 422), (
            f"Expected 400 or 422, got {resp.status_code}"
        )


class TestUpdateWorker:
    """PUT /api/workspace/{ws_id}/workers/{name}"""

    def test_update_worker(self, client, tmp_path):
        """Create then PUT with new system_prompt → 200, verify file updated."""
        with patch.object(pathlib.Path, "home") as mock_home:
            mock_home.return_value = tmp_path
            _create_workspace(tmp_path)
            body = _valid_worker()
            client.post(f"/api/workspace/{WS_ID}/workers", json=body)

            # Update
            updated = {**body, "system_prompt": "Updated system prompt."}
            resp = client.put(
                f"/api/workspace/{WS_ID}/workers/{body['name']}", json=updated
            )

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        data = resp.json()
        assert data["system_prompt"] == "Updated system prompt."

        # Verify on disk
        ws_dir = _workspace_path(tmp_path)
        disk_data = json.loads((ws_dir / "workers.json").read_text())
        assert len(disk_data) == 1
        assert disk_data[0]["system_prompt"] == "Updated system prompt."

    def test_update_nonexistent(self, client, tmp_path):
        """PUT to a worker that doesn't exist → 404."""
        with patch.object(pathlib.Path, "home") as mock_home:
            mock_home.return_value = tmp_path
            _create_workspace(tmp_path)
            body = _valid_worker()
            resp = client.put(
                f"/api/workspace/{WS_ID}/workers/nonexistent", json=body
            )

        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"

    def test_update_invalid_schema(self, client, tmp_path):
        """PUT with missing required fields → 422."""
        with patch.object(pathlib.Path, "home") as mock_home:
            mock_home.return_value = tmp_path
            _create_workspace(tmp_path)
            # First create a valid worker
            body = _valid_worker()
            client.post(f"/api/workspace/{WS_ID}/workers", json=body)

            # Then PUT with invalid data
            invalid = {"name": "test-worker"}  # Missing required fields
            resp = client.put(
                f"/api/workspace/{WS_ID}/workers/{body['name']}", json=invalid
            )

        assert resp.status_code == 422, f"Expected 422, got {resp.status_code}"


class TestDeleteWorker:
    """DELETE /api/workspace/{ws_id}/workers/{name}"""

    def test_delete_worker(self, client, tmp_path):
        """Create then DELETE → 204, verify file no longer contains it."""
        with patch.object(pathlib.Path, "home") as mock_home:
            mock_home.return_value = tmp_path
            _create_workspace(tmp_path)
            body = _valid_worker()
            client.post(f"/api/workspace/{WS_ID}/workers", json=body)

            # Delete
            resp = client.delete(
                f"/api/workspace/{WS_ID}/workers/{body['name']}"
            )

        assert resp.status_code == 204, f"Expected 204, got {resp.status_code}"

        # Verify on disk
        ws_dir = _workspace_path(tmp_path)
        disk_data = json.loads((ws_dir / "workers.json").read_text())
        assert len(disk_data) == 0, "workers.json should be empty after delete"

    def test_delete_nonexistent(self, client, tmp_path):
        """DELETE a non-existent worker → 404."""
        with patch.object(pathlib.Path, "home") as mock_home:
            mock_home.return_value = tmp_path
            _create_workspace(tmp_path)

            resp = client.delete(f"/api/workspace/{WS_ID}/workers/nobody")

        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"

    def test_delete_from_empty_file(self, client, tmp_path):
        """DELETE when workers.json doesn't exist → 404."""
        with patch.object(pathlib.Path, "home") as mock_home:
            mock_home.return_value = tmp_path
            _create_workspace(tmp_path)

            # Don't create workers.json at all
            resp = client.delete(f"/api/workspace/{WS_ID}/workers/nobody")

        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"


class TestDockerfile:
    """PUT /api/workspace/{ws_id}/dockerfile"""

    def test_put_dockerfile(self, client, tmp_path):
        """PUT plain text → 200, verify Dockerfile on disk matches."""
        with patch.object(pathlib.Path, "home") as mock_home:
            mock_home.return_value = tmp_path
            content = "FROM python:3.12\nWORKDIR /app\n"
            resp = client.put(
                f"/api/workspace/{WS_ID}/dockerfile", content=content
            )

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        data = resp.json()
        assert data["status"] == "ok"
        assert data["workspace_id"] == WS_ID

        # Verify on disk
        ws_dir = _workspace_path(tmp_path)
        dockerfile_path = ws_dir / "Dockerfile"
        assert dockerfile_path.exists(), "Dockerfile should exist on disk"
        assert dockerfile_path.read_text() == content

    def test_put_dockerfile_overwrites(self, client, tmp_path):
        """PUT dockerfile twice → second write replaces first."""
        with patch.object(pathlib.Path, "home") as mock_home:
            mock_home.return_value = tmp_path
            _create_workspace(tmp_path)

            # First write
            first = "FROM python:3.11\n"
            client.put(f"/api/workspace/{WS_ID}/dockerfile", content=first)

            # Second write (overwrite)
            second = "FROM python:3.12\n"
            resp = client.put(
                f"/api/workspace/{WS_ID}/dockerfile", content=second
            )

        assert resp.status_code == 200
        ws_dir = _workspace_path(tmp_path)
        assert (ws_dir / "Dockerfile").read_text() == second, (
            "Dockerfile should contain the second write"
        )


class TestGetWorkerByName:
    """GET /api/workspace/{ws_id}/workers?name="""

    def test_get_worker_by_name(self, client, tmp_path):
        """Create worker, GET ?name= → 200 with the worker data."""
        with patch.object(pathlib.Path, "home") as mock_home:
            mock_home.return_value = tmp_path
            _create_workspace(tmp_path)
            body = _valid_worker()
            client.post(f"/api/workspace/{WS_ID}/workers", json=body)

            resp = client.get(
                f"/api/workspace/{WS_ID}/workers?name={body['name']}"
            )

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        data = resp.json()
        assert data["name"] == body["name"]
        # Verify definition fields are present
        assert data["description"] == body["description"]
        assert data["system_prompt"] == body["system_prompt"]
        assert data["tools"] == body["tools"]
        # Runtime fields may be None if the worker was never started
        assert "runtime_status" in data
        assert "has_persisted_context" in data

    def test_get_worker_by_name_missing(self, client, tmp_path):
        """GET ?name=nonexistent → 404."""
        with patch.object(pathlib.Path, "home") as mock_home:
            mock_home.return_value = tmp_path
            _create_workspace(tmp_path)
            body = _valid_worker()
            client.post(f"/api/workspace/{WS_ID}/workers", json=body)

            resp = client.get(
                f"/api/workspace/{WS_ID}/workers?name=nonexistent"
            )

        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"

    def test_get_worker_by_name_from_empty(self, client, tmp_path):
        """GET ?name= from workspace with no workers.json → 404."""
        with patch.object(pathlib.Path, "home") as mock_home:
            mock_home.return_value = tmp_path
            _create_workspace(tmp_path)

            resp = client.get(
                f"/api/workspace/{WS_ID}/workers?name=anyone"
            )

        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"


class TestListWorkers:
    """GET /api/workspace/{ws_id}/workers (list)"""

    def test_list_workers_after_crud(self, client, tmp_path):
        """Sequence: create 2, GET list → 2, delete 1, GET list → 1."""
        with patch.object(pathlib.Path, "home") as mock_home:
            mock_home.return_value = tmp_path
            _create_workspace(tmp_path)

            # Create worker A
            worker_a = {**_valid_worker(), "name": "worker-a"}
            client.post(f"/api/workspace/{WS_ID}/workers", json=worker_a)

            # Create worker B
            worker_b = {**_valid_worker(), "name": "worker-b"}
            client.post(f"/api/workspace/{WS_ID}/workers", json=worker_b)

            # List → should have 2
            resp = client.get(f"/api/workspace/{WS_ID}/workers")
            assert resp.status_code == 200
            assert len(resp.json()) == 2

            # Delete worker A
            client.delete(f"/api/workspace/{WS_ID}/workers/worker-a")

            # List → should have 1
            resp = client.get(f"/api/workspace/{WS_ID}/workers")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 1
            assert data[0]["name"] == "worker-b"

    def test_list_workers_empty(self, client, tmp_path):
        """GET workers on workspace with no workers.json → empty list."""
        with patch.object(pathlib.Path, "home") as mock_home:
            mock_home.return_value = tmp_path
            _create_workspace(tmp_path)

            resp = client.get(f"/api/workspace/{WS_ID}/workers")

        assert resp.status_code == 200
        assert resp.json() == []
