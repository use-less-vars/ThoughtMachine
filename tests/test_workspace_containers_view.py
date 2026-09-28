"""RED tests for the workspace container-view backend (C.1, stage 2).

Routes under test (in ``web_ui/backend/server.py``), and the manager contract
they rest on (``infra/container_manager.py``)::

    GET   /api/workspace/{ws}/containers                 -> {"containers": [...], "containers_in_use": n, "containers_available": m}
    POST  /api/workspace/{ws}/containers/{id}/action      body {"action": "stop"|"start"|"restart"|"remove"}
    PATCH /api/workspace/{ws}/containers/{id}/resources   body {"mem_limit"?: str, "cpu_quota"?: int}

Pinned contract (from the C.1 brief + recon, dev=bf7fb9f):

* The GET returns ``containers`` / ``containers_in_use`` /
  ``containers_available`` (PC3).  ``containers`` is a FLAT list of
  record-derived entries (``api.list_records``); the legacy ``session`` /
  ``workspace`` keys are GONE.  ``containers_in_use`` / ``containers_available``
  stay derived from ``ContainerManager.list_containers()`` /
  ``max_containers``.
* Entry keys: ``id, name, kind, state, intent_snapshot, permissions,
  permission_drift, shared, live, unrecorded``.  ``kind`` is ``ephemeral``|
  ``persistent``|``resource`` (PC5).  ``ephemeral`` lifecycle -> ``ephemeral``;
  ``persistent`` and ``service`` -> ``persistent`` (PC4: ``service`` is a
  persistent-class container in intent; it has no producers today, so it
  renders in the persistent section rather than inventing a UI section with no
  writers); ``resource`` -> ``resource``; anything else -> ``persistent``.
  ``shared`` = ``kind in ('persistent', 'resource')``.  There is NO
  ``owner_session_id`` field (P6: workspace-wide, no session attribution).
* The old ``runtime`` kind is RETIRED (PC5): it appears in neither the backend
  payload nor the UI.
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
    # Liveness markers (EXTENSION 2): ``live`` = matched to a live container (or
    # ``None`` when the live set is unverifiable); ``unrecorded`` = a live
    # container with no backing record.  Both are on EVERY entry, so pinning
    # them here makes the ``ENTRY_KEYS <= set(...)`` assertions bind them.
    "live",
    "unrecorded",
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


_WS_LABEL = "thoughtmachine.workspace_id"
_RESOURCE_LABEL = "thoughtmachine.resource"


class _FakeClientContainer:
    """Docker-SDK-shaped live container (only the attrs ``server.py`` reads)."""

    def __init__(self, container_id, name, *, status="running", labels=None,
                 oom_killed=False):
        self.id = container_id
        self.name = name
        self.labels = dict(labels or {})
        self.status = status
        self.attrs = {
            "State": {"Status": status, "OOMKilled": bool(oom_killed),
                      "StartedAt": ""},
        }

    def reload(self):
        return None


class _FakeClientContainers:
    """``client.containers`` collection (``.list`` + ``.get``)."""

    def __init__(self, manager):
        self._manager = manager

    def _iter(self):
        for cid, info in self._manager._statuses.items():
            yield _FakeClientContainer(
                cid,
                self._manager._names.get(cid, cid),
                status=info.get("status", "running"),
                labels=self._manager._labels_for(cid),
                oom_killed=bool(info.get("oom_killed", False)),
            )

    def list(self, all=True, filters=None):  # noqa: A002 - docker signature
        label_filters = {}
        if isinstance(filters, dict) and isinstance(filters.get("label"), str):
            key, _, value = filters["label"].partition("=")
            label_filters[key] = value
        out = []
        for container in self._iter():
            if label_filters and any(
                    container.labels.get(k) != v
                    for k, v in label_filters.items()):
                continue
            out.append(container)
        return out

    def get(self, container_id):
        for container in self._iter():
            if container.id == container_id:
                return container
        raise KeyError(container_id)


class _FakeDockerClient:
    """``manager.client`` stand-in exposing ``.containers``."""

    def __init__(self, manager):
        self.containers = _FakeClientContainers(manager)


class FakeManager:
    """Minimal stand-in for ``infra.container_manager.ContainerManager``.

    ``statuses`` maps ``container_id -> {"status": <raw>, "oom_killed": bool}``.
    ``names`` overrides the container-name index.  ``client`` mirrors the
    Docker client handle the backend reads for workspace live enumeration, and
    ``class_of`` mirrors the canonical live classifier.
    """

    def __init__(self, *, statuses=None, names=None, start_result=None,
                 workspace_id="ws1"):
        self._statuses = dict(statuses) if statuses is not None else {
            "docker-e": {"status": "running"},
            "docker-r": {"status": "running"},
            "docker-aaa": {"status": "running"},
        }
        self._names = {"docker-e": "c-e", "docker-r": "c-r",
                       "docker-aaa": "c-a"}
        if names is not None:
            self._names.update(dict(names))
        self.workspace_id = workspace_id
        self._start_result = start_result
        self.calls = []
        self.client = _FakeDockerClient(self)

    def _labels_for(self, container_id):
        labels = {_WS_LABEL: self.workspace_id}
        name = self._names.get(container_id, "") or ""
        if name.lstrip("/").startswith("tm-res-"):
            labels[_RESOURCE_LABEL] = "1"
        return labels

    def class_of(self, container):
        labels = getattr(container, "labels", None) or {}
        name = getattr(container, "name", "") or ""
        if labels.get(_RESOURCE_LABEL) or \
                name.lstrip("/").startswith("tm-res-"):
            return "resource"
        return "persistent"

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
    _seed(vault, record_id="rec-p", name="c-p", docker_id="docker-p",
          lifecycle_class="persistent")
    _seed(vault, record_id="rec-r", name="c-r", docker_id="docker-r",
          lifecycle_class="resource")
    _seed(vault, record_id="rec-s", name="c-s", docker_id="docker-s",
          lifecycle_class="service")
    _install_manager(monkeypatch, FakeManager())

    resp = _list()
    assert resp.status_code == 200
    data = resp.json()

    # PC3: the three legacy keys survive; ``session``/``workspace`` are gone.
    assert {"containers", "containers_in_use", "containers_available"} <= set(data)
    assert "session" not in data
    assert "workspace" not in data

    containers = data["containers"]
    assert isinstance(containers, list)

    # PC3/PC4/PC5: kind mapping -- ephemeral / persistent / resource; the
    # ``service`` class is a persistent-class container in intent (PC4).
    assert _find_entry(containers, "rec-e")["kind"] == "ephemeral"
    assert _find_entry(containers, "rec-p")["kind"] == "persistent"
    assert _find_entry(containers, "rec-r")["kind"] == "resource"
    assert _find_entry(containers, "rec-s")["kind"] == "persistent"

    # shared = kind in ('persistent', 'resource'); ephemeral is never shared.
    assert _find_entry(containers, "rec-e")["shared"] is False
    assert _find_entry(containers, "rec-p")["shared"] is True
    assert _find_entry(containers, "rec-r")["shared"] is True
    assert _find_entry(containers, "rec-s")["shared"] is True

    for entry in containers:
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

    entry = _find_entry(data["containers"], "rec-e")
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


# ── T4: restart a resource container => recreate path, new docker_id ─────────

def test_action_restart_resource_recreates(vault, monkeypatch):
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


# ── T5: remove a resource container => refused ───────────────────────────────

def test_action_remove_resource_refused(vault, monkeypatch):
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


# ── T6: PATCH resource resources => stop + recreate, new limits ──────────────

def test_patch_resource_resources_recreates(vault, monkeypatch):
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

    entry = _find_entry(data["containers"], "rec-e")
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


# ── Empty workspace => empty flat entry list (PC3) ────────

def test_empty_workspace_yields_empty_container_list(vault, monkeypatch):
    _install_manager(monkeypatch, FakeManager(statuses={}))

    resp = _list("ws-empty")
    assert resp.status_code == 200
    data = resp.json()
    assert data["containers"] == []
    assert data["containers_in_use"] == 0
    assert "session" not in data and "workspace" not in data


# ── T15: GET legacy contract survives a record-store failure ────────────────

def test_get_legacy_contract_and_store_failure(vault, monkeypatch):
    _seed(vault, record_id="rec-e", name="c-e", docker_id="docker-e",
          lifecycle_class="ephemeral")
    _install_manager(monkeypatch, FakeManager())

    resp = _list()
    assert resp.status_code == 200
    data = resp.json()

    # The three legacy keys are ALWAYS present (PC3).
    assert {"containers", "containers_in_use", "containers_available"} <= set(data)
    assert isinstance(data["containers"], list) and data["containers"]
    for item in data["containers"]:
        assert ENTRY_KEYS <= set(item)

    # Now make the record store explode: the legacy keys must survive and the
    # flat entry list degrades to [].
    def _boom(*args, **kwargs):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(cr_api, "list_records", _boom)

    resp2 = _list()
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert {"containers", "containers_in_use", "containers_available"} <= set(data2)
    assert data2["containers"] == []


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
    assert map_state({"status": "bogus"}, "creating") == "creating"

    # (e) no usable status -> record-state fallback (default 'stopped')
    assert map_state(None, "running") == "running"
    assert map_state(None, "creating") == "creating"
    assert map_state(None, "") == "stopped"


# ── Liveness reconciliation: records vs the LIVE workspace container set ──
# Strand: bug/panel-omits-live-resource-container.


def _find_entry_by_name(entries, name):
    for e in entries:
        if isinstance(e, dict) and e.get("name") == name:
            return e
    raise AssertionError(f"no entry with name={name!r} in {entries!r}")


def test_record_without_live_container_marked_not_live(vault, monkeypatch):
    """Test A: a record with no live container is visibly distinct (not live).

    Currently FAILS: the builder is record-only and never asserts liveness,
    so there is no distinction to read (``live`` key absent).
    """
    _seed(vault, record_id="rec-dead", name="c-dead", docker_id=None,
          lifecycle_class="persistent")
    _seed(vault, record_id="rec-live", name="c-live", docker_id="docker-live",
          lifecycle_class="persistent")
    _install_manager(monkeypatch, FakeManager(
        statuses={"docker-live": {"status": "running"}}))

    data = _list().json()
    dead = _find_entry(data["containers"], "rec-dead")
    live = _find_entry(data["containers"], "rec-live")

    # The dead record is still emitted (never hidden), but clearly marked
    # non-live; the live one is marked live.
    assert dead["live"] is False
    assert live["live"] is True
    # Both rows keep the record-derived shape.
    assert ENTRY_KEYS <= set(dead)
    assert ENTRY_KEYS <= set(live)
    assert dead["unrecorded"] is False
    # And they are distinguishable on liveness.
    assert dead["live"] != live["live"]


def test_live_container_with_record_shown_live(vault, monkeypatch):
    """Test B: a live container with a record renders with its live status.

    CONFIRM: passes on the current tree and must keep passing (asserts only the
    pre-existing kind/state contract, never the new liveness marker).
    """
    _seed(vault, record_id="rec-e", name="c-e", docker_id="docker-e",
          lifecycle_class="ephemeral")
    _install_manager(monkeypatch, FakeManager(
        statuses={"docker-e": {"status": "running"}}))

    data = _list().json()
    entry = _find_entry(data["containers"], "rec-e")
    assert entry["kind"] == "ephemeral"
    assert entry["state"] == "running"
    assert ENTRY_KEYS <= set(entry)


def test_live_container_without_record_appears(vault, monkeypatch):
    """Test C: a live container with NO record still appears.

    Currently FAILS: the builder is record-only, so the live ``tm-res-*git``
    resource container (the reported bug) is missing from the payload.
    """
    _install_manager(monkeypatch, FakeManager(
        statuses={"tm-res-abc": {"status": "running"}},
        names={"tm-res-abc": "tm-res-abc-git"}))

    data = _list().json()
    entry = _find_entry_by_name(data["containers"], "tm-res-abc-git")
    assert entry["kind"] == "resource"
    assert entry["state"] == "running"
    assert entry["live"] is True
    assert entry["unrecorded"] is True
    assert ENTRY_KEYS <= set(entry)



# ── EXTENSION 2: entries-derived count + key pinning ────────────────────────

class _ResourceHidingManager(FakeManager):
    """``FakeManager`` whose ``list_containers()`` hides resource containers.

    Faithfully mirrors ``ContainerManager.list_containers()``: it "deliberately
    hides resource containers" (server.py:3274-3276 / 3431-3433), so the
    manager-derived count covers the session/agent-visible set only, while the
    Docker-client enumeration (``_list_workspace_live_containers``) sees the
    live resource container too.  This is exactly the split the panel's counts
    must be honest about.
    """

    def list_containers(self):
        return [c for c in super().list_containers()
                if not (c.get("name") or "").lstrip("/").startswith("tm-res-")]


def test_listed_count_includes_live_resource_container(vault, monkeypatch):
    """EXTENSION 2: the entries-derived count covers the reconciled list the
    panel renders (including the live RESOURCE container), while
    ``containers_in_use`` / ``containers_available`` stay manager-derived (the
    session/agent-visible set) and are UNCHANGED.

    The two counts cover DIFFERENT sets and so DISAGREE here: the live resource
    container is rendered in the panel (counted by the entries-derived count)
    but is hidden by ``ContainerManager.list_containers()`` (NOT in the
    manager-derived pair).  That disagreement is the intended behavior, not a
    bug.

    Currently FAILS: the response carries no entries-derived count key.
    """
    _seed(vault, record_id="rec-e", name="c-e", docker_id="docker-e",
          lifecycle_class="ephemeral")
    mgr = _install_manager(monkeypatch, _ResourceHidingManager(
        statuses={"docker-e": {"status": "running"},
                  "tm-res-abc": {"status": "running"}},
        names={"tm-res-abc": "tm-res-abc-git"}))

    data = _list().json()

    # Entries-derived count: the reconciled list the panel renders -- the live
    # record container AND the live resource container.
    assert data["containers_listed"] == len(data["containers"])
    assert data["containers_listed"] == 2
    assert {e["name"] for e in data["containers"]} == {"c-e", "tm-res-abc-git"}

    # Manager-derived pair: unchanged semantics -- the session/agent-visible set.
    assert data["containers_in_use"] == len(mgr.list_containers())
    assert data["containers_in_use"] == 1
    cap = int(getattr(mgr, "max_containers", 6))
    assert data["containers_available"] == max(
        0, cap - data["containers_in_use"])

    # Different sets -> the counts disagree here (intended, not a bug).
    assert data["containers_listed"] != data["containers_in_use"]


def test_entries_pin_live_and_unrecorded_keys(vault, monkeypatch):
    """EXTENSION 2: the two new entry keys (``live``, ``unrecorded``) are pinned
    in ``ENTRY_KEYS`` AND present in EVERY returned entry.

    Currently FAILS: ``ENTRY_KEYS`` does not yet include ``live`` /
    ``unrecorded``, so the pinning assertion below catches their absence.
    """
    # A record (with its live container) + a recordless live resource container.
    _seed(vault, record_id="rec-e", name="c-e", docker_id="docker-e",
          lifecycle_class="ephemeral")
    _install_manager(monkeypatch, FakeManager(
        statuses={"docker-e": {"status": "running"},
                  "tm-res-abc": {"status": "running"}},
        names={"tm-res-abc": "tm-res-abc-git"}))

    # The pin set itself must bind the new keys.
    assert {"live", "unrecorded"} <= ENTRY_KEYS

    data = _list().json()
    assert data["containers"], "expected at least one entry"
    for entry in data["containers"]:
        assert ENTRY_KEYS <= set(entry)
        assert {"live", "unrecorded"} <= set(entry)

    # The pinned assertion has teeth: an entry missing a pinned key fails it.
    sample = dict(data["containers"][0])
    sample.pop("live")
    assert not (ENTRY_KEYS <= set(sample))
    sample.pop("unrecorded")
    assert not (ENTRY_KEYS <= set(sample))

