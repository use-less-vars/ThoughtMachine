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


def _removed(container_id=DOCKER_ID):
    """Queued ``remove`` result."""
    return {"status": "removed", "container_id": container_id}


def _started(docker_id="docker-new"):
    """Queued ``start`` result."""
    return {"id": docker_id, "name": NAME, "status": "created", "note": ""}


class _FakeManager:
    """Minimal stand-in for ``ContainerManager`` used by the resize route.

    ``start_results`` / ``remove_results`` are FIFO queues: an entry is either a
    return value (a dict) or an exception instance which is raised instead, so a
    single test can make the first ``start`` fail and the recovery ``start``
    succeed (or fail too).  The queues are EXHAUSTIVE -- an empty queue is an
    ``AssertionError``, never a silent default, so every test must enqueue every
    call the route makes.

    ``record_probe`` (optional) is called at ``start`` CALL TIME and its
    ``intent_snapshot`` recorded in ``start_snapshots``: it models exactly what a
    real ``ContainerManager.start`` reads when it builds a container, so a test
    can prove WHICH limits a rebuild ran with.
    """

    def __init__(self, *, start_results=None, remove_results=None,
                 record_probe=None):
        self.calls = []
        self.remove_kwargs = []
        self.start_results = list(start_results or [])
        self.remove_results = list(remove_results or [])
        self.record_probe = record_probe
        self.start_snapshots = []
        self._containers = [{"name": NAME, "container_id": DOCKER_ID,
                             "status": "running"}]

    def list_containers(self):
        return list(self._containers)

    def status(self, container_id):
        return {"status": "running", "id": container_id}

    @staticmethod
    def _next(queue, what):
        """Pop the next queued result -- an EMPTY queue is a test bug."""
        assert queue, (
            f"_FakeManager: no queued {what} result; enqueue one explicitly "
            "instead of relying on a silent default")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def remove(self, container_id, keep_record=False, **kwargs):
        self.calls.append(("remove", container_id, keep_record))
        self.remove_kwargs.append(dict(kwargs))
        return self._next(self.remove_results, "remove")

    def start(self, name=None, note=None, allow_fresh=False, created_by=None):
        self.calls.append(("start", name, note, allow_fresh, created_by))
        if self.record_probe is not None:
            record = self.record_probe()
            self.start_snapshots.append(
                dict(getattr(record, "intent_snapshot", None) or {})
                if record is not None else {})
        return self._next(self.start_results, "start")

    @property
    def removes(self):
        return [call for call in self.calls if call[0] == "remove"]

    @property
    def starts(self):
        return [call for call in self.calls if call[0] == "start"]


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
    manager = _install(monkeypatch, _FakeManager(
        start_results=[_started()], remove_results=[_removed()]))
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


# ── Route: below the Docker 6MB floor never destroys anything ─────────────

@pytest.mark.parametrize("bad", ["1m", "5m", "0", "6291455"])
def test_mem_limit_below_docker_floor_is_400_and_destroys_nothing(
        vault, monkeypatch, bad):
    record = _seed(vault, intent_snapshot={"mem_limit": "256m"})
    manager = _install(monkeypatch, _FakeManager())
    _allow(monkeypatch)

    resp = _patch(RID, {"mem_limit": bad})

    assert resp.status_code == 400, resp.text
    assert "6MB" in resp.json()["error"]
    # Nothing was destroyed, recreated or even persisted.
    assert manager.calls == []
    after = cr_api.load_record(WS, RID, vault_root=vault)
    assert after.id == record.id
    assert after.docker_id == DOCKER_ID
    assert after.intent_snapshot == {"mem_limit": "256m"}


@pytest.mark.parametrize("good", ["6m", "6291456"])
def test_mem_limit_at_the_docker_floor_still_resizes(vault, monkeypatch, good):
    _seed(vault, intent_snapshot={"mem_limit": "256m"})
    manager = _install(monkeypatch, _FakeManager(
        start_results=[_started()], remove_results=[_removed()]))
    _allow(monkeypatch)

    resp = _patch(RID, {"mem_limit": good})

    assert resp.status_code == 200, resp.text
    assert manager.removes == [("remove", DOCKER_ID, True)]
    after = cr_api.load_record(WS, RID, vault_root=vault)
    assert after.docker_id == "docker-new"
    assert after.intent_snapshot == {"mem_limit": good}


# ── Route: a failed recreate restores the previous intent ──────────────────

class _Boom(Exception):
    """Daemon-style error carrying the HTTP-ish ``status_code`` attribute."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


def _collect_resize_audits(monkeypatch):
    """Capture ``infra.container_manager._audit(event, data)`` calls."""
    from infra import container_manager as cm

    seen = []
    monkeypatch.setattr(cm, "_audit",
                        lambda event, data="": seen.append((event, data)))
    return seen


def test_start_error_dict_is_400_and_restores_previous_limits(vault,
                                                             monkeypatch):
    _seed(vault, intent_snapshot={"mem_limit": "256m", "cpu_quota": 50000})
    manager = _install(monkeypatch, _FakeManager(
        start_results=[
            {"error": "Minimum memory limit allowed is 6MB"},
            _started("docker-restored"),
        ],
        remove_results=[_removed()]))
    _allow(monkeypatch)
    audits = _collect_resize_audits(monkeypatch)

    resp = _patch(RID, {"mem_limit": "8m"})

    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert "resource edit failed" in error
    assert "previous limits restored" in error

    # The new intent was written, then rolled back to the ORIGINAL snapshot.
    after = cr_api.load_record(WS, RID, vault_root=vault)
    assert after.intent_snapshot == {"mem_limit": "256m", "cpu_quota": 50000}
    assert after.docker_id == "docker-restored"
    assert after.state == "running"

    # One remove, then TWO starts: the failed one and the recovery one.
    assert len(manager.removes) == 1
    assert len(manager.starts) == 2
    assert [tag for tag, _ in audits] == ["CONTAINER_RESIZE_FAILED"]
    assert "requested={'mem_limit': '8m'}" in audits[0][1]


def test_recovery_rebuild_runs_with_the_previous_limits(vault, monkeypatch):
    """The recovery ``start`` is handed the PREVIOUS limits, not the edit.

    ``_FakeManager.start`` is given ``record_probe`` -- a callback evaluated AT
    START CALL TIME that reads the record exactly as a real
    ``ContainerManager.start`` does when it builds a container -- and records the
    ``intent_snapshot`` it saw.  So this pins, from the caller's point of view,
    WHICH limits each rebuild ran with: the failed start ran with the REQUESTED
    limits (already persisted), the recovery start ran with the PREVIOUS ones
    (the rollback wrote them back before starting).
    """
    _seed(vault, intent_snapshot={"mem_limit": "256m", "cpu_quota": 50000})
    manager = _install(monkeypatch, _FakeManager(
        start_results=[
            {"error": "Minimum memory limit allowed is 6MB"},
            _started("docker-restored"),
        ],
        remove_results=[_removed()],
        record_probe=lambda: cr_api.load_record(WS, RID, vault_root=vault),
    ))
    _allow(monkeypatch)
    _collect_resize_audits(monkeypatch)

    resp = _patch(RID, {"mem_limit": "8m"})

    assert resp.status_code == 400, resp.text
    assert len(manager.starts) == 2
    assert len(manager.start_snapshots) == 2
    # First start: the NEW intent was persisted before the rebuild.
    assert manager.start_snapshots[0]["mem_limit"] == "8m"
    # Recovery start: the rollback restored the PRE-EDIT snapshot first.
    assert manager.start_snapshots[1] == {"mem_limit": "256m",
                                          "cpu_quota": 50000}


@pytest.mark.parametrize("status_code,expected", [(400, 400), (500, 503)])
def test_start_exception_status_code_maps_to_http_status(
        vault, monkeypatch, status_code, expected):
    _seed(vault, intent_snapshot={"mem_limit": "256m"})
    manager = _install(monkeypatch, _FakeManager(
        start_results=[
            _Boom("daemon said no", status_code=status_code),
            _started("docker-restored"),
        ],
        remove_results=[_removed()]))
    _allow(monkeypatch)
    audits = _collect_resize_audits(monkeypatch)

    resp = _patch(RID, {"mem_limit": "512m"})

    assert resp.status_code == expected, resp.text
    after = cr_api.load_record(WS, RID, vault_root=vault)
    assert after.intent_snapshot == {"mem_limit": "256m"}
    assert after.docker_id == "docker-restored"
    assert len(manager.starts) == 2
    assert [tag for tag, _ in audits] == ["CONTAINER_RESIZE_FAILED"]
    assert "daemon said no" in audits[0][1]


def test_rebuild_failure_unbinds_the_record_docker_id(vault, monkeypatch):
    _seed(vault, intent_snapshot={"mem_limit": "256m"})
    manager = _install(monkeypatch, _FakeManager(
        start_results=[
            {"error": "no space left on device"},
            {"error": "rebuild also failed"},
        ],
        remove_results=[_removed()]))
    _allow(monkeypatch)
    audits = _collect_resize_audits(monkeypatch)

    resp = _patch(RID, {"mem_limit": "512m"})

    assert resp.status_code == 400, resp.text
    assert "container NOT restored" in resp.json()["error"]
    # Unbound (not a dangling docker id) and flagged, after a disk round-trip.
    after = cr_api.load_record(WS, RID, vault_root=vault)
    assert not after.docker_id
    assert after.state == "error"
    assert after.intent_snapshot == {"mem_limit": "256m"}
    assert len(manager.starts) == 2
    assert "container NOT restored" in audits[0][1]


# ── Route: the happy path labels the destroy as a resize ────────────────

def test_success_labels_the_destroy_as_a_resize_and_rebinds(vault, monkeypatch):
    _seed(vault, intent_snapshot={"mem_limit": "256m"})
    manager = _install(monkeypatch, _FakeManager(
        start_results=[_started()], remove_results=[_removed()]))
    _allow(monkeypatch)
    audits = _collect_resize_audits(monkeypatch)

    resp = _patch(RID, {"mem_limit": "512m"})

    assert resp.status_code == 200, resp.text
    assert manager.remove_kwargs == [{
        "audit_reason": "resize_recreate",
        "audit_source": "server.workspace_container_resources",
    }]
    after = cr_api.load_record(WS, RID, vault_root=vault)
    assert after.docker_id == "docker-new"
    assert after.state == "running"
    assert audits == []          # no CONTAINER_RESIZE_FAILED on the happy path


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
