"""PATCH /api/workspace/{workspace_id}/containers/{record_id}/resources.

Runs with a fully mocked ContainerManager (no Docker daemon required).  Pins the
two P0 fixes on the resize route:

1. the requested limits are VALIDATED before anything is persisted and before
   the container is destroyed -- an invalid ``mem_limit`` (or ``cpu_quota``) is a
   clean 400 that leaves the record's ``intent_snapshot`` and the container
   untouched (no ``remove`` at all);
2. the recreate ladder passes ``keep_record=True`` to ``manager.remove`` so the
   record (and the intent snapshot written just before it) survives the remove,
   instead of being reaped and re-minted on defaults.

Run:  python3 -m pytest tests/test_container_resources_resize_route.py -q
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import web_ui.backend.server as server_module
from web_ui.backend.server import app
from thoughtmachine.container_record import api as cr_api

client = TestClient(app)

WS = "ws1"
RID = "rec-a"
NAME = "c-a"
DOCKER_ID = "docker-aaa"
RESIZE = "/api/workspace/{ws}/containers/{rid}/resources"


# ── Harness ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def vault(tmp_path, monkeypatch):
    """Isolate the container-record store in a temp vault root."""
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr("thoughtmachine.vault.vault_root", lambda: root)
    return root


def _seed(vault, *, intent_snapshot=None, docker_id=DOCKER_ID):
    cr_api.create_record(WS, "ephemeral", "workspace-owned", id=RID, name=NAME,
                         intent_snapshot=intent_snapshot, vault_root=vault)
    if docker_id:
        cr_api.attach_container(WS, RID, docker_id, vault_root=vault)
    return cr_api.load_record(WS, RID, vault_root=vault)


def _allow(monkeypatch):
    monkeypatch.setattr(server_module, "_container_write_allowed",
                        lambda workspace_id: (True, "allowed"))


def _install(monkeypatch, manager):
    monkeypatch.setattr(
        server_module, "_make_container_manager",
        lambda workspace_id, workspace_path="": manager)
    return manager


class _FakeManager:
    """Minimal stand-in for ``ContainerManager`` used by the resize route."""

    def __init__(self):
        self.calls = []
        self._containers = [{"name": NAME, "container_id": DOCKER_ID,
                             "status": "running"}]

    def list_containers(self):
        return list(self._containers)

    def status(self, container_id):
        return {"status": "running", "id": container_id}

    def remove(self, container_id, keep_record=False):
        self.calls.append(("remove", container_id, keep_record))
        return {"status": "removed", "container_id": container_id}

    def start(self, name=None, note=None, allow_fresh=False):
        self.calls.append(("start", name, note, allow_fresh))
        return {"id": "docker-new", "name": name, "status": "created",
                "note": note or ""}

    @property
    def removes(self):
        return [call for call in self.calls if call[0] == "remove"]


def _patch(record_id, body, *, workspace_id=WS, actor="tester"):
    headers = {"X-Actor": actor} if actor else {}
    return client.patch(RESIZE.format(ws=workspace_id, rid=record_id),
                        headers=headers, json=body)


# ── Route: invalid input never persists and never destroys ──────────────────

@pytest.mark.parametrize("bad", ["banana", "", "-1", "1g extra", "1.5", None])
def test_invalid_mem_limit_is_400_and_leaves_everything_untouched(
        vault, monkeypatch, bad):
    record = _seed(vault, intent_snapshot={"mem_limit": "256m"})
    manager = _install(monkeypatch, _FakeManager())
    _allow(monkeypatch)

    resp = _patch(RID, {"mem_limit": bad})

    assert resp.status_code == 400, resp.text
    assert "mem_limit" in resp.json()["error"]
    # Nothing was destroyed or recreated.
    assert manager.calls == []
    after = cr_api.load_record(WS, RID, vault_root=vault)
    assert after.id == record.id
    assert after.docker_id == DOCKER_ID
    assert after.intent_snapshot == {"mem_limit": "256m"}


@pytest.mark.parametrize("bad", [-1, "1000", 1.5, True])
def test_invalid_cpu_quota_is_400_and_leaves_everything_untouched(
        vault, monkeypatch, bad):
    record = _seed(vault, intent_snapshot={"mem_limit": "256m"})
    manager = _install(monkeypatch, _FakeManager())
    _allow(monkeypatch)

    resp = _patch(RID, {"cpu_quota": bad})

    assert resp.status_code == 400, resp.text
    assert "cpu_quota" in resp.json()["error"]
    assert manager.calls == []
    after = cr_api.load_record(WS, RID, vault_root=vault)
    assert after.id == record.id
    assert after.intent_snapshot == {"mem_limit": "256m"}


# ── Route: valid resize keeps the record id and passes keep_record=True ─────

def test_valid_resize_keeps_record_id_and_remove_keeps_record(vault,
                                                             monkeypatch):
    record = _seed(vault, intent_snapshot={"mem_limit": "256m"})
    manager = _install(monkeypatch, _FakeManager())
    _allow(monkeypatch)

    resp = _patch(RID, {"mem_limit": "512m"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == RID
    assert resp.json()["intent_snapshot"]["mem_limit"] == "512m"

    after = cr_api.load_record(WS, RID, vault_root=vault)
    assert after.id == record.id == RID
    assert after.intent_snapshot["mem_limit"] == "512m"
    assert after.docker_id == "docker-new"

    # Exactly ONE remove, and it must carry keep_record=True (a bare remove()
    # reaps a non-resource record, destroying the intent just written).
    assert manager.removes == [("remove", DOCKER_ID, True)]
    # Order: remove -> start(allow_fresh=True), i.e. never a start before remove.
    assert [call[0] for call in manager.calls] == ["remove", "start"]
    assert manager.calls[1][3] is True


# ── ``_parse_mem_limit`` unit coverage (the explicit accept/reject set) ────

@pytest.mark.parametrize("value,expected", [
    ("1073741824", 1073741824),
    ("0", 0),
    ("100b", 100),
    ("1024k", 1024 * 1024),
    ("1024kb", 1024 * 1024),
    ("512m", 512 * 1024 ** 2),
    ("512M", 512 * 1024 ** 2),
    ("512MB", 512 * 1024 ** 2),
    ("512mb", 512 * 1024 ** 2),
    ("1g", 1024 ** 3),
    ("1G", 1024 ** 3),
    ("2gb", 2 * 1024 ** 3),
    ("1.5g", 1024 ** 3 + 512 * 1024 ** 2),
    (" 512m ", 512 * 1024 ** 2),
])
def test_parse_mem_limit_accepts(value, expected):
    assert server_module._parse_mem_limit(value) == expected


@pytest.mark.parametrize("value", [
    "banana", "", "   ", "-1", "-512m", "1g extra", "512 m",
    "1.5", "1.2.3g", "512mbb", "512x", "m", "0x10", None, 512, ["512m"],
])
def test_parse_mem_limit_rejects(value):
    assert server_module._parse_mem_limit(value) is None
