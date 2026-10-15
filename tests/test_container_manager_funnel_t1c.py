"""Wave 9a T1c -- the ``ContainerManager`` recreate ladder and the stale-worker
destroy site route through the lifecycle funnel (``ContainerLifecycle.reconcile``).

Sites exercised:
  U3  ``ContainerManager.start()`` recreate branches (record / workspace-label /
      label) -- reconcile then destroy+recreate ONLY on ``decision.destroys``;
      a (defensive) non-destroy decision leaves the weak container ALIVE and
      falls through to reuse.
  U4  ``cleanup_stale_worker_containers`` -- reconcile per stale worker; a refuse
      leaves the container ALIVE.

The Docker client is always a fake; no vault is touched (every pre/post concern
on the ``start`` path is stubbed so only the funnel branch runs).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import infra.container_manager as container_manager
from infra.container_lifecycle import ContainerLifecycle, ReconcileDecision
from infra.container_manager import ContainerManager
from thoughtmachine.container_record import LIFECYCLE_PERSISTENT

_WORKER_LABEL = "thoughtmachine.worker"


# ---------------------------------------------------------------------------
# reconcile spy -- records every call, optionally forces a canned decision
# ---------------------------------------------------------------------------
class _ReconcileSpy:
    def __init__(self, monkeypatch, decision=None):
        self.calls = []
        self._real = ContainerLifecycle.reconcile
        self._decision = decision

        def spy(container, session, intent=None):
            self.calls.append((container, session, intent))
            if self._decision is not None:
                return self._decision
            return self._real(container, session, intent)

        monkeypatch.setattr(ContainerLifecycle, "reconcile", spy)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeContainer:
    def __init__(self, *, name="tm-c1", cid="c1", status="exited", labels=None):
        self.name = name
        self.id = cid
        self.status = status
        self.labels = dict(labels or {})
        self.attrs = {
            "State": {
                "Status": status,
                "Running": status == "running",
                "FinishedAt": "2020-01-01T00:00:00Z",
            }
        }
        self.stop_calls = []
        self.remove_calls = []

    def stop(self, timeout=None):
        self.stop_calls.append({"timeout": timeout})

    def remove(self, force=False):
        self.remove_calls.append({"force": force})


class FakeContainers:
    def __init__(self, containers=None, get_map=None):
        self._containers = list(containers or [])
        self._get_map = dict(get_map or {})

    def list(self, all=False, filters=None):  # noqa: A002 - Docker SDK signature
        return list(self._containers)

    def get(self, cid):
        return self._get_map[cid]


class FakeClient:
    def __init__(self, containers=None, get_map=None):
        self.containers = FakeContainers(containers, get_map)


def _bare_manager(**attrs):
    """A ``ContainerManager`` with only the attributes the start path reads."""
    mgr = ContainerManager.__new__(ContainerManager)
    mgr.workspace_id = attrs.get("workspace_id", "ws1")
    mgr.session_id = attrs.get("session_id", "s1")
    mgr.session_permissions = attrs.get("session_permissions", None)
    mgr.vault_root = attrs.get("vault_root", "/tmp/tm-nonexistent-vault")
    mgr.image = attrs.get("image", "img")
    mgr.workspace_path = attrs.get("workspace_path", "/tmp/ws")
    mgr._containers = {}
    return mgr


@pytest.fixture(autouse=True)
def _reset_name_index():
    def _clear():
        container_manager._NAME_INDEX.clear()
        container_manager._NAME_INDEX_COLLISIONS.clear()
        container_manager._NAME_INDEX_BUILT.clear()

    _clear()
    yield
    _clear()


def _install_start_scaffolding(monkeypatch, *, drift=("recreate", None)):
    """No-op every pre/post-recreate concern so only the funnel branch runs.

    Returns a list that captures ``_fresh_start`` kwargs on each call.
    """
    fresh = []
    monkeypatch.setattr(container_manager, "_host_ids", lambda: None)
    monkeypatch.setattr(
        ContainerManager, "_migrate_legacy_notes_once", lambda self: None
    )
    monkeypatch.setattr(
        ContainerManager, "_compute_config", lambda self, *a, **k: ("none", "ro")
    )
    monkeypatch.setattr(ContainerManager, "_ensure_name_index", lambda self: None)
    monkeypatch.setattr(
        ContainerManager, "_migrate_records_v3_once", lambda self: None
    )
    monkeypatch.setattr(
        ContainerManager, "_name_collision", lambda self, name: False
    )
    monkeypatch.setattr(
        ContainerManager, "_start_drift_decision", lambda self, *a, **k: drift
    )
    monkeypatch.setattr(ContainerManager, "_read_note", lambda self, c: "")
    monkeypatch.setattr(ContainerManager, "_image_ref", lambda self, c: "img")
    monkeypatch.setattr(ContainerManager, "_record_id_for", lambda self, c: None)
    monkeypatch.setattr(
        ContainerManager, "_emit_container_reused_once", lambda self, *a, **k: None
    )
    monkeypatch.setattr(container_manager, "snapshot_has_evidence", lambda x: False)
    monkeypatch.setattr(container_manager, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(container_manager, "log_container_event", lambda *a, **k: None)
    monkeypatch.setattr(
        ContainerManager,
        "_fresh_start",
        lambda self, **k: (fresh.append(k), {"status": "fresh", "id": "new"})[1],
    )
    return fresh


# ===========================================================================
# U3 -- site 1: record path recreate
# ===========================================================================
def _site1_common(monkeypatch):
    mgr = _bare_manager()
    fresh = _install_start_scaffolding(monkeypatch)
    monkeypatch.setattr(
        ContainerManager, "_record_for_name", lambda self, name: "rec-1"
    )
    rec = SimpleNamespace(
        docker_id="c1", intent_snapshot=None, lifecycle_class=LIFECYCLE_PERSISTENT
    )
    monkeypatch.setattr(container_manager, "load_record", lambda ws, rid: rec)
    c = FakeContainer(name="tm-c1", cid="c1")
    monkeypatch.setattr(
        ContainerManager, "_reuse_container", lambda self, cid: (c, True)
    )
    return mgr, fresh, c


def test_u3_site1_record_recreate_routed_through_funnel(monkeypatch):
    mgr, fresh, c = _site1_common(monkeypatch)
    spy = _ReconcileSpy(monkeypatch)

    resp = mgr.start(name="tm-c1")

    assert len(spy.calls) == 1
    _, session, intent = spy.calls[0]
    assert intent.kind == "reap"
    assert intent.explicit is True
    assert intent.reason == "start_recreate"
    assert session is None  # session_permissions=None passed through
    # Destroy proceeded (action="reap" -> destroys).
    assert c.remove_calls == [{"force": True}]
    assert fresh  # _fresh_start called
    assert resp["status"] == "fresh"


def test_u3_site1_recreate_refused_keeps_container(monkeypatch):
    mgr, fresh, c = _site1_common(monkeypatch)
    _ReconcileSpy(
        monkeypatch, decision=ReconcileDecision("refuse", "nope", "default")
    )

    resp = mgr.start(name="tm-c1")

    # Non-destroy decision is HONOURED: no removal, fall through to reuse.
    assert c.remove_calls == []
    assert fresh == []
    assert resp["status"] == "reused"
    assert resp["id"] == "c1"


# ===========================================================================
# U3 -- site 2: workspace-label path recreate
# ===========================================================================
def _site2_common(monkeypatch):
    mgr = _bare_manager()
    fresh = _install_start_scaffolding(monkeypatch)
    monkeypatch.setattr(
        ContainerManager, "_record_for_name", lambda self, name: None
    )
    monkeypatch.setattr(ContainerManager, "_ensure_running", lambda self, c: True)
    c = FakeContainer(name="tm-c1", cid="c1")
    entry = {"name": "tm-c1", "container_id": "c1", "note": ""}
    monkeypatch.setattr(mgr, "list_containers", lambda: [entry])
    mgr.client = FakeClient(get_map={"c1": c})
    return mgr, fresh, c


def test_u3_site2_workspace_label_recreate_routed_through_funnel(monkeypatch):
    mgr, fresh, c = _site2_common(monkeypatch)
    spy = _ReconcileSpy(monkeypatch)

    resp = mgr.start(name="tm-c1")

    assert len(spy.calls) == 1
    _, session, intent = spy.calls[0]
    assert intent.reason == "start_recreate"
    assert intent.explicit is True
    assert c.remove_calls == [{"force": True}]
    assert fresh
    assert resp["status"] == "fresh"


def test_u3_site2_recreate_refused_keeps_container(monkeypatch):
    mgr, fresh, c = _site2_common(monkeypatch)
    _ReconcileSpy(
        monkeypatch, decision=ReconcileDecision("refuse", "nope", "default")
    )

    resp = mgr.start(name="tm-c1")

    assert c.remove_calls == []
    assert fresh == []
    assert resp["status"] == "reused"


# ===========================================================================
# U3 -- site 3: label path recreate
# ===========================================================================
def _site3_common(monkeypatch):
    mgr = _bare_manager()
    fresh = _install_start_scaffolding(monkeypatch)
    monkeypatch.setattr(
        ContainerManager, "_record_for_name", lambda self, name: None
    )
    monkeypatch.setattr(mgr, "list_containers", lambda: [])
    monkeypatch.setattr(ContainerManager, "_get_max_containers", lambda self: 100)
    monkeypatch.setattr(
        ContainerManager, "_active_containers", lambda self, cs: []
    )
    c = FakeContainer(name="tm-c1", cid="c1")
    monkeypatch.setattr(ContainerManager, "_find_by_labels", lambda self, name: c)
    monkeypatch.setattr(ContainerManager, "_ensure_running", lambda self, c: True)
    return mgr, fresh, c


def test_u3_site3_label_recreate_routed_through_funnel(monkeypatch):
    mgr, fresh, c = _site3_common(monkeypatch)
    spy = _ReconcileSpy(monkeypatch)

    resp = mgr.start(name="tm-c1")

    assert len(spy.calls) == 1
    _, session, intent = spy.calls[0]
    assert intent.reason == "start_recreate"
    assert c.remove_calls == [{"force": True}]
    assert fresh
    assert resp["status"] == "fresh"


def test_u3_site3_recreate_refused_keeps_container(monkeypatch):
    mgr, fresh, c = _site3_common(monkeypatch)
    _ReconcileSpy(
        monkeypatch, decision=ReconcileDecision("refuse", "nope", "default")
    )

    resp = mgr.start(name="tm-c1")

    assert c.remove_calls == []
    assert fresh == []
    assert resp["status"] == "reused"


# ===========================================================================
# U4 -- cleanup_stale_worker_containers
# ===========================================================================
def _u4_container():
    return FakeContainer(
        name="c1", cid="c1", status="exited", labels={_WORKER_LABEL: "owner-1"}
    )


def test_u4_stale_worker_cleanup_removes_on_destroy(monkeypatch):
    monkeypatch.setattr(container_manager, "_gc_should_skip", lambda c: False)
    monkeypatch.setattr(container_manager, "log", lambda *a, **k: None)
    spy = _ReconcileSpy(monkeypatch)
    c = _u4_container()

    removed = container_manager.cleanup_stale_worker_containers(
        FakeClient([c]), "owner-1"
    )

    assert removed == ["c1"]
    assert c.remove_calls == [{"force": True}]
    assert len(spy.calls) == 1
    _, session, intent = spy.calls[0]
    assert intent.kind == "reap"
    assert intent.explicit is True
    assert intent.reason == "stale_worker_cleanup"
    assert session is None


def test_u4_stale_worker_cleanup_refused_keeps_container(monkeypatch):
    monkeypatch.setattr(container_manager, "_gc_should_skip", lambda c: False)
    monkeypatch.setattr(container_manager, "log", lambda *a, **k: None)
    _ReconcileSpy(
        monkeypatch, decision=ReconcileDecision("refuse", "nope", "default")
    )
    c = _u4_container()

    removed = container_manager.cleanup_stale_worker_containers(
        FakeClient([c]), "owner-1"
    )

    assert removed == []
    assert c.remove_calls == []
