"""RED tests for the workspace container-view backend (C.1, stage 2).

Routes under test (in ``web_ui/backend/server.py``), and the manager contract
they rest on (``infra/container_manager.py``)::

    GET   /api/workspace/{ws}/containers                 -> {"session": [...], "workspace": [...]}
    POST  /api/workspace/{ws}/containers/{id}/action      body {"action": "stop"|"start"|"restart"|"remove"}
    PATCH /api/workspace/{ws}/containers/{id}/resources   body {"mem_limit"?: str, "cpu_quota"?: int}

Pinned contract (from the C.1 brief + recon, dev=bf7fb9f):

* The existing GET (server.py:3207) returns only ``containers`` /
  ``containers_in_use`` / ``containers_available``; it is COMPATIBLY
  EXTENDED with ``session`` (ephemeral records) and ``workspace`` (resource
  records).  Both come from the record store (``api.list_records``) split by
  ``lifecycle_class`` -- ``ContainerManager.list_containers()`` hides resource
  containers, so it cannot enumerate the workspace group.
* Entry keys: ``id, name, kind, state, intent_snapshot, permissions,
  permission_drift, shared``.  ``kind`` is ``ephemeral``|``runtime`` (there is
  NO ``runtime`` lifecycle_class: every non-ephemeral record maps to
  ``runtime``).  ``shared`` is True for runtime.  There is NO
  ``owner_session_id`` field (P6: workspace-wide, no session attribution).
* Raw Docker status -> entry ``state`` mapping: OOMKilled True -> ``oom``
  (override); running|restarting -> ``running``; paused -> ``paused``;
  exited|dead -> ``exited``; created|stopped|removing|missing|error ->
  ``stopped``.
* ``ContainerManager.status()`` gains ``oom_killed`` (read from the SAME
  ``container.attrs["State"]`` dict it already reads -- no new Docker call).
* ``remove`` is refused for runtime (resource) containers -> 403
  ``permission_denied`` with body ``{"error", "code"}`` (the convention already
  used by ``_record_user_action`` at server.py:~3564).
* ``PATCH .../resources`` = stop + recreate, and applies to BOTH kinds (an
  ephemeral edit is NOT refused).

Every test must fail for a genuine reason today: either a route that does not
exist yet (404), a missing response key (KeyError), or a missing assertion.
None may fail as an import/collection/transform error.

The Docker daemon is unavailable in CI, so the manager is injected via
``server_module._make_container_manager`` (the accepted "real HTTP boundary"
pattern from tests/test_container_record_user_action_routes.py).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import web_ui.backend.server as server_module
from web_ui.backend.server import app
from infra import container_manager as cm
from thoughtmachine.container_record import api as cr_api

client = TestClient(app)

LIST = "/api/workspace/{ws}/containers"
ACTION = "/api/workspace/{ws}/containers/{rid}/action"
RESOURCES = "/api/workspace/{ws}/containers/{rid}/resources"

ENTRY_KEYS = {
    "id",
    "name",
    "kind",
    "state",
    "intent_snapshot",
    "permissions",
    "permission_drift",
    "shared",
}


# ── Fixtures / helpers ──────────────────────────────────────────────────────

@pytest.fixture()
def vault(tmp_path, monkeypatch):
    """Isolate the container-record store in a temp vault root."""
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr("thoughtmachine.vault.vault_root", lambda: root)
    return root


def _seed(vault, *, workspace_id="ws1", record_id="rec-e", name="c-e",
          docker_id="docker-e", lifecycle_class="ephemeral",
          intent_snapshot=None, permissions=None, attach=True):
    cr_api.create_record(workspace_id, lifecycle_class, "workspace-owned",
                         intent_snapshot=intent_snapshot,
                         id=record_id, name=name, vault_root=vault,
                         permissions=permissions)
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


def _list(ws="ws1"):
    return client.get(LIST.format(ws=ws))


def _action(rid, action, *, ws="ws1", headers=None):
    hdrs = dict(headers or {})
    hdrs.setdefault("X-Actor", "tester")
    return client.post(ACTION.format(ws=ws, rid=rid),
                       headers=hdrs, json={"action": action})


def _patch(rid, body, *, ws="ws1", headers=None):
    hdrs = dict(headers or {})
    hdrs.setdefault("X-Actor", "tester")
    return client.patch(RESOURCES.format(ws=ws, rid=rid),
                        headers=hdrs, json=body)


def _entry(body):
    """Accept either a nested ``{"entry": {...}}`` or a top-level entry."""
    if isinstance(body, dict) and isinstance(body.get("entry"), dict):
        return body["entry"]
    return body


def _find_entry(entries, entry_id):
    for e in entries:
        if isinstance(e, dict) and e.get("id") == entry_id:
            return e
    raise AssertionError(f"no entry with id={entry_id!r} in {entries!r}")


class FakeManager:
    """Minimal stand-in for ``infra.container_manager.ContainerManager``.

    ``statuses`` maps ``container_id -> {"status": <raw>, "oom_killed": bool}``.
    """

    def __init__(self, *, statuses=None, start_result=None):
        self._statuses = dict(statuses) if statuses is not None else {
            "docker-e": {"status": "running"},
            "docker-r": {"status": "running"},
            "docker-aaa": {"status": "running"},
        }
        self._names = {"docker-e": "c-e", "docker-r": "c-r",
                       "docker-aaa": "c-a"}
        self._start_result = start_result
        self.calls = []

    def _info(self, container_id):
        return self._statuses.setdefault(container_id, {"status": "running"})

    def list_containers(self):
        out = []
        for cid, info in self._statuses.items():
            out.append({
                "name": self._names.get(cid, cid),
                "container_id": cid,
                "status": info.get("status", "running"),
            })
        return out

    def stop(self, container_id):
        self.calls.append(("stop", container_id))
        self._info(container_id)["status"] = "stopped"
        return {"status": "stopped", "container_id": container_id}

    def remove(self, container_id):
        self.calls.append(("remove", container_id))
        self._statuses.pop(container_id, None)
        return {"status": "removed", "container_id": container_id}

    def start(self, name=None, note=None, allow_fresh=False):
        self.calls.append(("start", name, note, allow_fresh))
        if self._start_result is not None:
            result = dict(self._start_result)
        else:
            result = {"id": "docker-new", "name": name,
                      "status": "created", "note": note}
        cid = result.get("id") or "docker-new"
        self._statuses[cid] = {"status": "running"}
        if name:
            self._names[cid] = name
        return result

    def status(self, container_id=None, *args, **kwargs):
        self.calls.append(("status", container_id))
        info = self._statuses.get(container_id, {"status": "running"})
        return {
            "container_id": container_id,
            "name": self._names.get(container_id, container_id),
            "status": info.get("status", "running"),
            "uptime_seconds": 0,
            "memory_usage_bytes": 0,
            "note": "",
            "oom_killed": bool(info.get("oom_killed", False)),
        }


# ── T1: grouping + entry fields ─────────────────────────────────────────────

def test_get_groups_and_entry_fields(vault, monkeypatch):
    _seed(vault, record_id="rec-e", name="c-e", docker_id="docker-e",
          lifecycle_class="ephemeral")
    _seed(vault, record_id="rec-r", name="c-r", docker_id="docker-r",
          lifecycle_class="resource")
    _install_manager(monkeypatch, FakeManager())

    resp = _list()
    assert resp.status_code == 200
    data = resp.json()

    # Compatible extension: the legacy key survives.
    assert "containers" in data

    session = data["session"]
    workspace = data["workspace"]
    assert isinstance(session, list) and isinstance(workspace, list)

    s_entry = _find_entry(session, "rec-e")
    assert s_entry["kind"] == "ephemeral"
    assert s_entry["shared"] is False

    w_entry = _find_entry(workspace, "rec-r")
    assert w_entry["kind"] == "runtime"
    assert w_entry["shared"] is True

    for entry in session + workspace:
        missing = ENTRY_KEYS - set(entry)
        assert not missing, f"entry {entry!r} missing keys {missing}"
        # P6: no session attribution anywhere.
        assert "owner_session_id" not in entry
    assert "owner_session_id" not in data


# ── T2: pre-v5 / unwired record => permissions None, drift None ─────────────

def test_unwired_record_permissions_and_drift_null(vault, monkeypatch):
    _seed(vault, record_id="rec-e", name="c-e", docker_id="docker-e",
          lifecycle_class="ephemeral", permissions=None)
    _install_manager(monkeypatch, FakeManager())

    resp = _list()
    assert resp.status_code == 200
    data = resp.json()

    entry = _find_entry(data["session"], "rec-e")
    assert entry["permissions"] is None
    assert entry["permission_drift"] is None


# ── T3: stop an ephemeral container ─────────────────────────────────────────

def test_action_stop_ephemeral(vault, monkeypatch):
    _seed(vault, record_id="rec-e", name="c-e", docker_id="docker-e",
          lifecycle_class="ephemeral")
    mgr = _install_manager(monkeypatch, FakeManager())
    _allow(monkeypatch)

    resp = _action("rec-e", "stop")
    assert resp.status_code == 200
    assert ("stop", "docker-e") in mgr.calls

    entry = _entry(resp.json())
    assert entry["state"] == "stopped"

    rec = cr_api.load_record("ws1", "rec-e", vault_root=vault)
    assert rec is not None


# ── T4: restart a runtime container => recreate path, new docker_id ─────────

def test_action_restart_runtime_recreates(vault, monkeypatch):
    _seed(vault, record_id="rec-r", name="c-r", docker_id="docker-r",
          lifecycle_class="resource")
    mgr = _install_manager(monkeypatch, FakeManager(
        start_result={"id": "docker-new", "name": "c-r", "status": "created"}))
    _allow(monkeypatch)

    resp = _action("rec-r", "restart")
    assert resp.status_code == 200

    entry = _entry(resp.json())
    assert entry["id"] == "rec-r"
    assert entry["state"] == "running"

    rec = cr_api.load_record("ws1", "rec-r", vault_root=vault)
    assert rec is not None
    assert rec.id == "rec-r"
    assert rec.docker_id != "docker-r"


# ── T5: remove a runtime container => refused ───────────────────────────────

def test_action_remove_runtime_refused(vault, monkeypatch):
    _seed(vault, record_id="rec-r", name="c-r", docker_id="docker-r",
          lifecycle_class="resource")
    mgr = _install_manager(monkeypatch, FakeManager())
    _allow(monkeypatch)

    resp = _action("rec-r", "remove")
    assert resp.status_code == 403

    body = resp.json()
    assert set(body) == {"error", "code"}
    assert body["code"] == "permission_denied"

    # No Docker-mutating verb was reached.
    assert mgr.calls == []


# ── T6: PATCH runtime resources => stop + recreate, new limits ──────────────

def test_patch_runtime_resources_recreates(vault, monkeypatch):
    _seed(vault, record_id="rec-r", name="c-r", docker_id="docker-r",
          lifecycle_class="resource",
          intent_snapshot={"mem_limit": "512m", "cpu_quota": 50000})
    mgr = _install_manager(monkeypatch, FakeManager())
    _allow(monkeypatch)

    resp = _patch("rec-r", {"mem_limit": "2g", "cpu_quota": 200000})
    assert resp.status_code == 200

    rec = cr_api.load_record("ws1", "rec-r", vault_root=vault)
    assert rec is not None
    assert rec.intent_snapshot["mem_limit"] == "2g"
    assert rec.intent_snapshot["cpu_quota"] == 200000
    assert rec.docker_id != "docker-r"

    verbs = {c[0] for c in mgr.calls}
    # Authorised relaxation (C.1 stage 3): a resource edit is a recreate =
    # remove + start; no separate stop verb is issued.
    assert {"remove", "start"} <= verbs


# ── T7: PATCH ephemeral resources => same, NOT refused ──────────────────────

def test_patch_ephemeral_resources_not_refused(vault, monkeypatch):
    _seed(vault, record_id="rec-e", name="c-e", docker_id="docker-e",
          lifecycle_class="ephemeral",
          intent_snapshot={"mem_limit": "512m", "cpu_quota": 50000})
    mgr = _install_manager(monkeypatch, FakeManager())
    _allow(monkeypatch)

    resp = _patch("rec-e", {"mem_limit": "2g", "cpu_quota": 200000})
    assert resp.status_code == 200

    rec = cr_api.load_record("ws1", "rec-e", vault_root=vault)
    assert rec is not None
    assert rec.intent_snapshot["mem_limit"] == "2g"
    assert rec.intent_snapshot["cpu_quota"] == 200000
    assert rec.docker_id != "docker-e"

    verbs = {c[0] for c in mgr.calls}
    # Authorised relaxation (C.1 stage 3): a resource edit is a recreate =
    # remove + start; no separate stop verb is issued.
    assert {"remove", "start"} <= verbs


# ── T12a: ContainerManager.status() reports oom_killed ──────────────────────

class _FakeStatsContainer:
    def __init__(self, cid, *, oom_killed):
        self.id = cid
        self.name = cid
        self.status = "exited"
        self.labels = {}
        self.attrs = {"State": {"Status": "exited", "OOMKilled": oom_killed,
                                "StartedAt": ""}}

    def reload(self):
        return None


class _FakeContainersCollection:
    """Real-SDK-shaped ``client.containers`` collection (only ``.get`` used)."""

    def __init__(self, containers):
        self._containers = {c.id: c for c in containers}

    def get(self, container_id):
        return self._containers[container_id]


class _FakeStats:
    def __init__(self, containers):
        self.containers = _FakeContainersCollection(containers)

    def stats(self, container_id, stream=False):
        return {"memory_stats": {"usage": 0}}


def _real_manager(container):
    cm._NAME_INDEX.clear()
    cm._NAME_INDEX_BUILT.clear()
    cm._NAME_INDEX_COLLISIONS.clear()
    mgr = cm.ContainerManager.__new__(cm.ContainerManager)
    mgr.workspace_id = "ws1"
    mgr.workspace_path = "/tmp"
    mgr.vault_root = "/tmp"
    mgr.session_id = None
    mgr.session_permissions = None
    mgr._session_config = None
    mgr._containers = {}
    mgr.container_notes = {}
    mgr.workspace_config = {}
    mgr.max_containers = 6
    mgr.client = _FakeStats([container])
    mgr.class_of = lambda c: "ephemeral"
    mgr._read_note = lambda c: ""
    return mgr


def test_status_reports_oom_killed():
    oomed = _FakeStatsContainer("docker-oom", oom_killed=True)
    result = _real_manager(oomed).status("docker-oom")
    assert result["oom_killed"] is True

    healthy = _FakeStatsContainer("docker-ok", oom_killed=False)
    result = _real_manager(healthy).status("docker-ok")
    assert result["oom_killed"] is False


# ── T12b: GET surfaces an OOM-killed container as state == "oom" ────────────

def test_get_entry_state_oom(vault, monkeypatch):
    _seed(vault, record_id="rec-e", name="c-e", docker_id="docker-e",
          lifecycle_class="ephemeral")
    _install_manager(monkeypatch, FakeManager(
        statuses={"docker-e": {"status": "running", "oom_killed": True}}))

    resp = _list()
    assert resp.status_code == 200
    data = resp.json()

    entry = _find_entry(data["session"], "rec-e")
    assert entry["state"] == "oom"
    assert entry["state"] != "running"


# ── T14: remove an ephemeral container => direct delete, {"id","removed"} ────

def test_action_remove_ephemeral(vault, monkeypatch):
    _seed(vault, record_id="rec-e", name="c-e", docker_id="docker-e",
          lifecycle_class="ephemeral")
    mgr = _install_manager(monkeypatch, FakeManager())
    _allow(monkeypatch)

    resp = _action("rec-e", "remove")
    assert resp.status_code == 200
    assert resp.json() == {"id": "rec-e", "removed": True}

    # The Docker container was actually removed ...
    assert ("remove", "docker-e") in mgr.calls
    # ... and the record itself is gone from the store.
    assert cr_api.load_record("ws1", "rec-e", vault_root=vault) is None


# ── T15: GET legacy contract survives a record-store failure ────────────────

def test_get_legacy_contract_and_store_failure(vault, monkeypatch):
    _seed(vault, record_id="rec-e", name="c-e", docker_id="docker-e",
          lifecycle_class="ephemeral")
    _install_manager(monkeypatch, FakeManager())

    resp = _list()
    assert resp.status_code == 200
    data = resp.json()

    # The three legacy keys are ALWAYS present, regardless of the extension.
    assert {"containers", "containers_in_use", "containers_available"} <= set(data)
    assert isinstance(data["containers"], list) and data["containers"]
    for item in data["containers"]:
        assert {"name", "status"} <= set(item)

    # Now make the record store explode: the legacy contract must survive.
    def _boom(*args, **kwargs):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(cr_api, "list_records", _boom)

    resp2 = _list()
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert {"containers", "containers_in_use", "containers_available"} <= set(data2)
    assert data2["session"] == []
    assert data2["workspace"] == []


# ── T16: PATCH persists intent BEFORE the container is recreated ────────────

class _CapturingManager(FakeManager):
    """Records the stored intent the recreated container would observe."""

    def __init__(self, vault, **kwargs):
        super().__init__(**kwargs)
        self._vault = vault
        self.observed_intent = None

    def start(self, name=None, note=None, allow_fresh=False):
        rec = cr_api.load_record("ws1", "rec-r", vault_root=self._vault)
        self.observed_intent = dict(rec.intent_snapshot or {})
        return super().start(name=name, note=note, allow_fresh=allow_fresh)


def test_patch_persists_intent_before_recreate(vault, monkeypatch):
    _seed(vault, record_id="rec-r", name="c-r", docker_id="docker-r",
          lifecycle_class="resource",
          intent_snapshot={"mem_limit": "512m", "cpu_quota": 50000})
    mgr = _install_manager(monkeypatch, _CapturingManager(vault))
    _allow(monkeypatch)

    resp = _patch("rec-r", {"mem_limit": "2g", "cpu_quota": 200000})
    assert resp.status_code == 200

    # When the container was (re)started, the record ALREADY carried the new
    # limits -- the snapshot update is persisted before the recreate.
    assert mgr.observed_intent is not None
    assert mgr.observed_intent["mem_limit"] == "2g"
    assert mgr.observed_intent["cpu_quota"] == 200000




# ── Direct unit coverage of the raw-state -> view-state mapper ──────────────

def test_map_container_view_state_raw_mapping():
    """``_map_container_view_state`` maps raw Docker status -> entry ``state``.

    Pins the full raw-state table, the explicit non-OOM exit path, the OOM
    override, and the record-state fallback for unknown/absent raw status.
    """
    map_state = server_module._map_container_view_state

    # (a) full raw-state table with no record state
    assert map_state({"status": "running"}, "") == "running"
    assert map_state({"status": "restarting"}, "") == "running"
    assert map_state({"status": "paused"}, "") == "paused"
    assert map_state({"status": "exited"}, "") == "exited"
    assert map_state({"status": "dead"}, "") == "exited"
    assert map_state({"status": "created"}, "") == "stopped"
    assert map_state({"status": "stopped"}, "") == "stopped"
    assert map_state({"status": "removing"}, "") == "stopped"
    assert map_state({"status": "missing"}, "") == "stopped"
    assert map_state({"status": "error"}, "") == "stopped"

    # (b) explicit non-OOM path: an exited container is 'exited', never 'oom'
    assert map_state({"status": "exited"}, "running") == "exited"
    assert map_state({"status": "exited"}, "running") != "oom"

    # (c) OOM override wins over the raw status
    assert map_state({"status": "running", "oom_killed": True}, "stopped") == "oom"

    # (d) unknown raw string falls back to the record state
    assert map_state({"status": "bogus"}, "running") == "running"
    assert map_state({"status": "bogus"}, "creating") == "stopped"

    # (e) no usable status -> record-state fallback (default 'stopped')
    assert map_state(None, "running") == "running"
    assert map_state(None, "creating") == "stopped"
    assert map_state(None, "") == "stopped"

