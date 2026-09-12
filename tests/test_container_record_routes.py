"""Tests for the read-only container-record REST routes (chunk 4).

Exercises ``web_ui.backend.container_record_routes`` in isolation: a bare
FastAPI app with only that router mounted, and ``thoughtmachine.vault.vault_root``
monkeypatched onto a temporary vault.  The vault is seeded through the real
``thoughtmachine.container_record.api`` so the on-disk layout matches production.

Run:  python -m pytest tests/test_container_record_routes.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from thoughtmachine.container_record import api
from web_ui.backend import container_record_routes


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A temporary vault whose root is what ``vault_root()`` returns."""
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr("thoughtmachine.vault.vault_root", lambda: root)
    return root


@pytest.fixture
def client(vault):
    app = FastAPI()
    app.include_router(container_record_routes.router)
    return TestClient(app)


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Map every regular file under *root* to its (mtime_ns, size)."""
    snap: dict[str, tuple[int, int]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            st = path.stat()
            snap[str(path.relative_to(root))] = (st.st_mtime_ns, st.st_size)
    return snap


def test_list_empty_vault_returns_zero(vault, client):
    resp = client.get("/api/container-records")
    assert resp.status_code == 200
    assert resp.json() == {"records": [], "count": 0}


def test_list_single_workspace_returns_its_records(vault, client):
    api.create_record("ws1", "ephemeral", "workspace-owned", id="rec-a", vault_root=vault)
    api.create_record("ws1", "persistent", "system-owned", id="rec-b", vault_root=vault)
    api.attach_container("ws1", "rec-a", "docker-aaa", vault_root=vault)

    resp = client.get("/api/container-records", params={"workspace_id": "ws1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    by_id = {r["id"]: r for r in body["records"]}
    assert set(by_id) == {"rec-a", "rec-b"}
    assert by_id["rec-a"]["docker_id"] == "docker-aaa"
    assert by_id["rec-b"]["docker_id"] == ""
    assert by_id["rec-a"]["workspace_id"] == "ws1"
    assert by_id["rec-a"]["container_id"] == "rec-a"


def test_list_unknown_workspace_is_empty_200(vault, client):
    api.create_record("ws1", "ephemeral", "workspace-owned", id="rec-a", vault_root=vault)
    resp = client.get("/api/container-records", params={"workspace_id": "does-not-exist"})
    assert resp.status_code == 200
    assert resp.json() == {"records": [], "count": 0}


def test_list_without_workspace_aggregates_all(vault, client):
    api.create_record("ws1", "ephemeral", "workspace-owned", id="rec-a", vault_root=vault)
    api.create_record("ws2", "persistent", "system-owned", id="rec-b", vault_root=vault)

    resp = client.get("/api/container-records")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    ws_seen = {r["workspace_id"] for r in body["records"]}
    assert ws_seen == {"ws1", "ws2"}
    # workspace_id + id round-trip
    tagged = {(r["workspace_id"], r["id"]) for r in body["records"]}
    assert tagged == {("ws1", "rec-a"), ("ws2", "rec-b")}


def test_get_single_record(vault, client):
    api.create_record(
        "ws1", "ephemeral", "workspace-owned", purpose="hello", id="rec-a",
        vault_root=vault,
    )
    api.attach_container("ws1", "rec-a", "docker-aaa", vault_root=vault)

    resp = client.get("/api/container-records/rec-a", params={"workspace_id": "ws1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "rec-a"
    assert body["docker_id"] == "docker-aaa"
    assert body["purpose"] == "hello"
    assert body["workspace_id"] == "ws1"
    assert body["container_id"] == "rec-a"


def test_get_unknown_record_returns_404(vault, client):
    resp = client.get(
        "/api/container-records/nope", params={"workspace_id": "ws1"}
    )
    assert resp.status_code == 404
    assert "error" in resp.json()


def test_get_record_without_workspace_returns_400(vault, client):
    api.create_record("ws1", "ephemeral", "workspace-owned", id="rec-a", vault_root=vault)
    resp = client.get("/api/container-records/rec-a")
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_get_record_blank_workspace_returns_400(vault, client):
    resp = client.get("/api/container-records/rec-a", params={"workspace_id": "   "})
    assert resp.status_code == 400


def test_events_endpoint_returns_log(vault, client):
    api.create_record("ws1", "ephemeral", "workspace-owned", id="rec-a", vault_root=vault)
    api.append_event("ws1", "rec-a", "attached", "tester", vault_root=vault)

    resp = client.get(
        "/api/container-records/rec-a/events", params={"workspace_id": "ws1"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["record_id"] == "rec-a"
    assert body["count"] == len(body["events"]) == 1
    assert body["events"][0]["event_type"] == "attached"


def test_events_endpoint_requires_workspace(vault, client):
    resp = client.get("/api/container-records/rec-a/events")
    assert resp.status_code == 400


def test_get_is_read_only(vault, client):
    """A series of GETs must leave the on-disk store byte-for-byte untouched."""
    api.create_record("ws1", "ephemeral", "workspace-owned", id="rec-a", vault_root=vault)
    api.create_record("ws1", "persistent", "system-owned", id="rec-b", vault_root=vault)
    api.append_event("ws1", "rec-a", "attached", "tester", vault_root=vault)

    before = _snapshot(vault)

    assert client.get("/api/container-records").status_code == 200
    assert client.get(
        "/api/container-records", params={"workspace_id": "ws1"}
    ).status_code == 200
    assert client.get(
        "/api/container-records/rec-a", params={"workspace_id": "ws1"}
    ).status_code == 200
    assert client.get(
        "/api/container-records/rec-a/events", params={"workspace_id": "ws1"}
    ).status_code == 200
    assert client.get("/api/container-records/unknown").status_code == 400

    assert _snapshot(vault) == before
