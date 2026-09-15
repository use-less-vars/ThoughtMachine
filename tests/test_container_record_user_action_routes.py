"""Tests for the record-keyed container user-action routes (kill/restart/recreate).

Routes under test (added to ``web_ui/backend/server.py``)::

    POST /api/container-records/{record_id}/kill
    POST /api/container-records/{record_id}/restart
    POST /api/container-records/{record_id}/recreate

All guards run BEFORE any Docker call:

1. ``workspace_id`` required                       -> 400
2. actor attribution (``X-Actor`` / body ``actor``)-> 401 / 400
3. record resolves in-workspace; wrong ws          -> 403; unknown -> 404
4. permission ceiling (fail-closed); audited        -> 403 ``permission_denied``
5. container-manager construction                   -> 403/400/503/404
6. record name + container handle present           -> 409
7. Docker action                                    -> 409 (conflict) / 503 (error)

The permission gate (:func:`_container_write_allowed`) is fail-closed: an
unregistered workspace (no ``config.json``) yields an empty ceiling and DENIES,
so the fail-closed test needs no monkeypatching.

Decision vocabulary (``VALID_DECISIONS`` in ``server.py``) is the closed set
``{applied, denied, conflict, error}``; one test below pins that.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import web_ui.backend.server as server_module
from web_ui.backend.server import app
from infra import container_manager as cm
from thoughtmachine.container_record import api as cr_api
from thoughtmachine.container_record.models import iso_now

client = TestClient(app)

KILL = "/api/container-records/{rid}/kill"
RESTART = "/api/container-records/{rid}/restart"
RECREATE = "/api/container-records/{rid}/recreate"


# ── Fixtures / helpers ──────────────────────────────────────────────────────

@pytest.fixture()
def vault(tmp_path, monkeypatch):
    """Isolate the container-record store in a temp vault root."""
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr("thoughtmachine.vault.vault_root", lambda: root)
    return root


def _seed(vault, *, workspace_id="ws1", record_id="rec-a", name="c-a",
          docker_id="docker-aaa", attach=True):
    cr_api.create_record(workspace_id, "ephemeral", "workspace-owned",
                         id=record_id, name=name, vault_root=vault)
    if attach and docker_id:
        cr_api.attach_container(workspace_id, record_id, docker_id,
                                vault_root=vault)


def _allow(monkeypatch):
    monkeypatch.setattr(server_module, "_container_write_allowed",
                        lambda workspace_id: (True, "allowed"))


def _install_manager(monkeypatch, manager):
    monkeypatch.setattr(
        server_module, "_make_container_manager",
        lambda workspace_id, workspace_path="": manager)
    return manager


def _events(vault, workspace_id, record_id):
    return cr_api.read_event_log(workspace_id, record_id, vault_root=vault)


def _user_actions(vault, workspace_id, record_id):
    return [e for e in _events(vault, workspace_id, record_id)
            if e.get("event_type") == "RECORD_USER_ACTION"]


class FakeManager:
    """Minimal stand-in for ``infra.container_manager.ContainerManager``."""

    def __init__(self, *, start_result=None, remove_result=None,
                 stop_result=None, entries=None):
        self._start_result = start_result
        self._remove_result = remove_result or {
            "status": "removed", "container_id": "docker-aaa"}
        self._stop_result = stop_result or {
            "status": "stopped", "container_id": "docker-aaa"}
        self._entries = ([{"name": "c-a", "container_id": "docker-aaa"}]
                         if entries is None else entries)
        self.calls = []

    def list_containers(self):
        return list(self._entries)

    def stop(self, container_id):
        self.calls.append(("stop", container_id))
        return dict(self._stop_result)

    def remove(self, container_id):
        self.calls.append(("remove", container_id))
        return dict(self._remove_result)

    def start(self, name=None, note=None, allow_fresh=False):
        self.calls.append(("start", name, note, allow_fresh))
        if self._start_result is not None:
            return dict(self._start_result)
        return {"id": "docker-new", "name": name, "status": "created",
                "note": note}


def _post(route, record_id, *, workspace_id="ws1", actor="tester",
          body=None, headers=None):
    params = {} if workspace_id is None else {"workspace_id": workspace_id}
    hdrs = dict(headers or {})
    if actor is not None:
        hdrs.setdefault("X-Actor", actor)
    return client.post(route.format(rid=record_id), params=params,
                       headers=hdrs, json=body)


# ── Real-manager fakes (mirror test_registry_transition_schema_v3) ──────────

class _FakeImage:
    def __init__(self, ref="sha256:cafe"):
        self.id = ref

    def __str__(self):
        return self.id


class _FakeContainer:
    """Minimal container double: id/name/labels/status + reload/lifecycle."""

    def __init__(self, cid, name=None, labels=None, status="running"):
        self.id = cid
        self.name = name or cid
        self.image = _FakeImage()
        self.status = status
        self.labels = dict(labels or {})
        self.attrs = {"State": {"Status": status}}
        self.gone = False

    def reload(self):
        return None

    def start(self):
        self.status = "running"

    def stop(self, timeout=None):
        self.status = "exited"

    def kill(self):
        self.status = "exited"

    def remove(self, force=False):
        self.gone = True


class _FakeContainers:
    """Container collection double (``get``/``list``/``run``)."""

    def __init__(self, containers=None):
        self.items = list(containers or [])
        self.run_calls = []

    def get(self, container_id):
        for c in self.items:
            if not c.gone and (c.id == container_id or c.name == container_id):
                return c
        raise cm.NotFound(container_id)

    def list(self, **kwargs):
        return [c for c in self.items if not c.gone]

    def run(self, *args, **kwargs):
        self.run_calls.append({"args": args, "kwargs": kwargs})
        labels = kwargs.get("labels") or {}
        cid = f"cid-new-{len(self.run_calls)}"
        ctr = _FakeContainer(cid, kwargs.get("name"), labels)
        self.items.append(ctr)
        return ctr


def _client(containers=None):
    class _Client:
        pass

    c = _Client()
    c.containers = _FakeContainers(containers)
    return c


def _real_manager(vault, client, *, workspace_id="ws1"):
    """A REAL ``ContainerManager`` wired to a fake docker client + temp vault.

    Only daemon-touching / gate-touching deps are stubbed so the record-first
    identity ladder (name index, ``_reuse_container``, ``_fresh_start``) runs
    for real.
    """
    cm._NAME_INDEX.clear()
    cm._NAME_INDEX_BUILT.clear()
    cm._NAME_INDEX_COLLISIONS.clear()

    mgr = cm.ContainerManager.__new__(cm.ContainerManager)
    mgr.workspace_id = workspace_id
    mgr.workspace_path = str(vault)
    mgr.session_id = None
    mgr.session_permissions = None
    mgr._session_config = None
    mgr._containers = {}
    mgr.container_notes = {}
    mgr.workspace_config = {"disk_quota_mb": 0}
    mgr.max_containers = 4
    mgr.image = "img"
    mgr.mem_limit = "512m"
    mgr.cpu_quota = 50000
    mgr.vault_root = str(vault)
    mgr.client = client
    mgr._migrate_legacy_notes_once = lambda: None
    mgr._migrate_records_v3_once = lambda: None
    mgr._compute_config = lambda *a, **k: ("none", "ro")
    mgr._get_max_containers = lambda: 10
    mgr.list_containers = lambda: []
    mgr._find_by_labels = MagicMock(return_value=None)
    mgr._start_drift_decision = lambda *a, **k: ("ok", None)
    mgr.class_of = lambda container: "ephemeral"
    return mgr


# ── Success paths ───────────────────────────────────────────────────────────

def test_kill_success(vault, monkeypatch):
    _seed(vault)
    mgr = _install_manager(monkeypatch, FakeManager())
    _allow(monkeypatch)

    resp = _post(KILL, "rec-a", actor="tester")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("id") == "rec-a" or body.get("record_id") == "rec-a"

    assert mgr.calls == [("stop", "docker-aaa")]
    actions = _user_actions(vault, "ws1", "rec-a")
    assert actions and actions[-1]["payload"]["decision"] == "applied"
    assert actions[-1]["actor"] == "tester"


def test_kill_audits_both_channels(vault, monkeypatch):
    _seed(vault)
    _install_manager(monkeypatch, FakeManager())
    _allow(monkeypatch)

    seen = []
    monkeypatch.setattr("infra.container_manager._audit",
                        lambda event, detail: seen.append((event, detail)))

    resp = _post(KILL, "rec-a", actor="tester")
    assert resp.status_code == 200, resp.text
    # container-manager channel
    assert any(e == "RECORD_USER_ACTION" and "decision=applied" in d
               for e, d in seen), seen
    # record event-log channel
    actions = _user_actions(vault, "ws1", "rec-a")
    assert actions and actions[-1]["payload"]["decision"] == "applied"


def test_restart_success_stop_then_start(vault, monkeypatch):
    _seed(vault)
    mgr = _install_manager(monkeypatch, FakeManager())
    _allow(monkeypatch)

    resp = _post(RESTART, "rec-a", actor="tester")
    assert resp.status_code == 200, resp.text
    assert mgr.calls[0] == ("stop", "docker-aaa")
    assert mgr.calls[1][0] == "start"
    assert mgr.calls[1][1] == "c-a"
    assert mgr.calls[1][3] is False  # allow_fresh only on recreate
    actions = _user_actions(vault, "ws1", "rec-a")
    assert actions and actions[-1]["payload"]["decision"] == "applied"


def test_recreate_success_real_ladder(vault, monkeypatch):
    """Recreate happy path through the REAL identity ladder (endpoint-level).

    ``recreate`` = ``remove`` then ``start(name=..., allow_fresh=True)``.  The
    live container for the seeded record is removed, then the route's start
    mints a FRESH container and binds it to the SAME record: the record id
    survives (no new record), while its ``docker_id``/``state`` are rebound.
    """
    labels = {cm.RECORD_LABEL_KEY: "rec-a"}
    fake_client = _client(
        [_FakeContainer("docker-aaa", name="c-a", labels=labels)])

    _seed(vault)  # rec-a / c-a / docker-aaa
    mgr = _install_manager(monkeypatch, _real_manager(vault, fake_client))
    _allow(monkeypatch)

    resp = _post(RECREATE, "rec-a", actor="tester")
    assert resp.status_code == 200, resp.text

    # Exactly one fresh container created, then bound to the SAME record.
    assert len(fake_client.containers.run_calls) == 1
    rec = cr_api.load_record("ws1", "rec-a", vault_root=vault)
    assert rec is not None
    assert rec.name == "c-a"
    assert rec.docker_id == "cid-new-1"
    assert rec.state == "running"
    # Name index still points at the SAME record id (no new record minted).
    assert mgr._record_for_name("c-a") == "rec-a"
    assert mgr._containers["c-a"] == "cid-new-1"
    # The old container was removed.
    assert fake_client.containers.items[0].gone is True

    actions = _user_actions(vault, "ws1", "rec-a")
    assert actions and actions[-1]["payload"]["decision"] == "applied"


# ── Attribution guards ──────────────────────────────────────────────────────

def test_actor_missing_401(vault, monkeypatch):
    _seed(vault)
    _install_manager(monkeypatch, FakeManager())
    _allow(monkeypatch)

    resp = _post(KILL, "rec-a", actor=None)
    assert resp.status_code == 401, resp.text


def test_actor_malformed_400(vault, monkeypatch):
    _seed(vault)
    _install_manager(monkeypatch, FakeManager())
    _allow(monkeypatch)

    resp = _post(KILL, "rec-a", actor="bad actor!")
    assert resp.status_code == 400, resp.text


def test_body_actor_overrides_header(vault, monkeypatch):
    _seed(vault)
    mgr = _install_manager(monkeypatch, FakeManager())
    _allow(monkeypatch)

    resp = _post(KILL, "rec-a", actor="hdr", body={"actor": "body"})
    assert resp.status_code == 200, resp.text
    actions = _user_actions(vault, "ws1", "rec-a")
    assert actions and actions[-1]["actor"] == "body"


# ── Record resolution guards ────────────────────────────────────────────────

def test_wrong_workspace_403(vault, monkeypatch):
    _seed(vault)  # record lives only in ws1
    _install_manager(monkeypatch, FakeManager())
    _allow(monkeypatch)

    resp = _post(KILL, "rec-a", workspace_id="ws2", actor="tester")
    assert resp.status_code == 403, resp.text


def test_unknown_record_404(vault, monkeypatch):
    _seed(vault)
    _install_manager(monkeypatch, FakeManager())
    _allow(monkeypatch)

    resp = _post(KILL, "no-such-record", actor="tester")
    assert resp.status_code == 404, resp.text


def test_missing_workspace_id_400(vault, monkeypatch):
    _seed(vault)
    _install_manager(monkeypatch, FakeManager())
    _allow(monkeypatch)

    resp = _post(KILL, "rec-a", workspace_id=None, actor="tester")
    assert resp.status_code == 400, resp.text


# ── Permission gate ─────────────────────────────────────────────────────────

def test_permission_denied_403_audited(vault, monkeypatch):
    _seed(vault)
    _install_manager(monkeypatch, FakeManager())
    monkeypatch.setattr(server_module, "_container_write_allowed",
                        lambda workspace_id: (False, "no"))

    resp = _post(KILL, "rec-a", actor="tester")
    assert resp.status_code == 403, resp.text
    assert resp.json().get("code") == "permission_denied"

    actions = _user_actions(vault, "ws1", "rec-a")
    assert actions and actions[-1]["payload"]["decision"] == "denied"


def test_fail_closed_unknown_workspace_denied(vault, monkeypatch):
    """No ``_container_write_allowed`` monkeypatch: default must DENY.

    ``ws-unknown-no-config`` has no ``config.json`` -> empty ceiling -> deny.
    The record is seeded under that workspace so guard 3 passes and the
    request reaches (and is refused by) guard 4.
    """
    _seed(vault, workspace_id="ws-unknown-no-config")
    _install_manager(monkeypatch, FakeManager())

    resp = _post(KILL, "rec-a", workspace_id="ws-unknown-no-config",
                 actor="tester")
    assert resp.status_code == 403, resp.text
    assert resp.json().get("code") == "permission_denied"


# ── State / conflict guards ─────────────────────────────────────────────────

def test_record_without_name_409(vault, monkeypatch):
    _seed(vault, name="", attach=False)
    _install_manager(monkeypatch, FakeManager(entries=[]))
    _allow(monkeypatch)

    resp = _post(KILL, "rec-a", actor="tester")
    assert resp.status_code == 409, resp.text


def test_record_without_container_409(vault, monkeypatch):
    _seed(vault, attach=False)  # record has name but no docker_id
    _install_manager(monkeypatch, FakeManager(entries=[]))
    _allow(monkeypatch)

    resp = _post(KILL, "rec-a", actor="tester")
    assert resp.status_code == 409, resp.text


def test_start_error_maps_to_409_conflict(vault, monkeypatch):
    _seed(vault)
    _install_manager(monkeypatch, FakeManager(
        start_result={"error": "boom", "code": "container_record_container_missing"}))
    _allow(monkeypatch)

    resp = _post(RECREATE, "rec-a", actor="tester")
    assert resp.status_code == 409, resp.text
    actions = _user_actions(vault, "ws1", "rec-a")
    assert actions and actions[-1]["payload"]["decision"] == "conflict"


def test_manager_status_error_maps_to_503(vault, monkeypatch):
    _seed(vault)
    _install_manager(monkeypatch, FakeManager(
        stop_result={"status": "error", "container_id": "docker-aaa",
                     "error": "daemon down"}))
    _allow(monkeypatch)

    resp = _post(KILL, "rec-a", actor="tester")
    assert resp.status_code == 503, resp.text


# ── Fail-closed ladder + default-caller preservation (real manager) ─────────

def test_start_refuses_stale_docker_id_without_allow_fresh(vault):
    """Default caller (allow_fresh unset): a stale docker_id still REFUSES."""
    fake_client = _client([])  # record names docker-aaa, but no such container
    _seed(vault)
    mgr = _real_manager(vault, fake_client)

    result = mgr.start(name="c-a")
    assert isinstance(result, dict), result
    assert result.get("code") == "container_record_container_missing", result
    assert fake_client.containers.run_calls == []  # no fresh create


def test_start_refuses_unbound_record_without_allow_fresh(vault):
    """Default caller (allow_fresh unset): an unbound record still REFUSES."""
    fake_client = _client([])
    _seed(vault, attach=False)  # record has name but no docker_id
    mgr = _real_manager(vault, fake_client)

    result = mgr.start(name="c-a")
    assert isinstance(result, dict), result
    assert result.get("code") == "container_record_unbound", result
    assert fake_client.containers.run_calls == []  # no fresh create


def test_start_default_caller_reuses_live_container(vault):
    """Default caller: a record whose docker container is alive REUSES it."""
    labels = {cm.RECORD_LABEL_KEY: "rec-a"}
    fake_client = _client(
        [_FakeContainer("docker-aaa", name="c-a", labels=labels)])
    _seed(vault)
    mgr = _real_manager(vault, fake_client)

    result = mgr.start(name="c-a")
    assert isinstance(result, dict), result
    assert result.get("status") == "reused", result
    assert result.get("id") == "docker-aaa", result
    assert fake_client.containers.run_calls == []  # no fresh create


# ── Decision vocabulary (closed set) ────────────────────────────────────────

def test_decisions_only_canonical_vocabulary(vault, monkeypatch):
    """Every emitted ``decision`` is a member of ``VALID_DECISIONS``.

    Drives one success plus one of each failure class and asserts the observed
    decision set is exactly the canonical four.
    """
    assert server_module.VALID_DECISIONS == frozenset(
        {"applied", "denied", "conflict", "error"})
    seen = set()

    # applied -- kill success.
    _seed(vault, record_id="r-ok", name="c-ok")
    _install_manager(monkeypatch, FakeManager())
    _allow(monkeypatch)
    assert _post(KILL, "r-ok", actor="tester").status_code == 200
    seen |= {a["payload"]["decision"]
             for a in _user_actions(vault, "ws1", "r-ok")}

    # denied -- permission ceiling refuses.
    _seed(vault, record_id="r-deny", name="c-deny")
    monkeypatch.setattr(server_module, "_container_write_allowed",
                        lambda workspace_id: (False, "no"))
    assert _post(KILL, "r-deny", actor="tester").status_code == 403
    seen |= {a["payload"]["decision"]
             for a in _user_actions(vault, "ws1", "r-deny")}

    # conflict -- recreate start returns an error.
    _seed(vault, record_id="r-conf", name="c-conf")
    monkeypatch.setattr(server_module, "_container_write_allowed",
                        lambda workspace_id: (True, "allowed"))
    _install_manager(monkeypatch, FakeManager(
        start_result={"error": "boom",
                      "code": "container_record_container_missing"}))
    assert _post(RECREATE, "r-conf", actor="tester").status_code == 409
    seen |= {a["payload"]["decision"]
             for a in _user_actions(vault, "ws1", "r-conf")}

    # error -- manager reports a daemon error.
    _seed(vault, record_id="r-err", name="c-err")
    _install_manager(monkeypatch, FakeManager(
        stop_result={"status": "error", "container_id": "docker-aaa",
                     "error": "daemon down"}))
    assert _post(KILL, "r-err", actor="tester").status_code == 503
    seen |= {a["payload"]["decision"]
             for a in _user_actions(vault, "ws1", "r-err")}

    assert seen <= server_module.VALID_DECISIONS, seen
    assert seen == {"applied", "denied", "conflict", "error"}, seen
