"""Wave 9a STEP 2c -- the destroy sites of ``infra.container_manager`` route
through the lifecycle funnel (``ContainerLifecycle.reconcile``).

Sites exercised:
  5  ``ContainerManager._remove_container`` -- reconcile then destroy only on
     ``decision.destroys``
  6  ``cleanup_workspace`` -- reconcile per container; a refuse leaves the
     container ALIVE
  7  ``sweep_exited_workspace_containers`` / ``sweep_orphan_container_records``
     -- TTL/orphan semantics unchanged, decision routed through the funnel and
     every removal emits a ``CONTAINER_LIFECYCLE`` event

The Docker client is always a fake; the vault is a tmp dir.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

import infra.container_manager as container_manager
from infra.container_lifecycle import (
    ContainerLifecycle,
    ReconcileDecision,
)
from infra.container_manager import (
    ContainerManager,
    cleanup_workspace,
    sweep_exited_workspace_containers,
    sweep_orphan_container_records,
)
from thoughtmachine.container_record import (
    LIFECYCLE_CLASSES,
    LIFECYCLE_PERSISTENT,
    OWNER_WORKSPACE,
    create_record,
    load_record,
)
from thoughtmachine.container_record import storage

WORKSPACE_LABEL = "thoughtmachine.workspace_id"

_ORPHAN_WS = "w-orphan"
_KEPT_WS = "w-kept"


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


def _iso(seconds_ago=0):
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return dt.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeContainer:
    def __init__(self, *, name="tm-c1", cid="c1", status="exited",
                 labels=None, state_running=False, finished_at=None):
        self.name = name
        self.id = cid
        self.status = status
        self.labels = dict(labels or {})
        self.attrs = {
            "State": {
                "Status": status,
                "Running": state_running,
                "FinishedAt": finished_at or _iso(0),
            }
        }
        self.stop_calls = []
        self.remove_calls = []

    def stop(self, timeout=None):
        self.stop_calls.append({"timeout": timeout})

    def remove(self, force=False):
        self.remove_calls.append({"force": force})


class FakeContainers:
    def __init__(self, containers=None):
        self._containers = list(containers or [])

    def list(self, all=False, filters=None):  # noqa: A002 - Docker SDK signature
        return list(self._containers)


class FakeClient:
    def __init__(self, containers=None):
        self.containers = FakeContainers(containers)


class _FakeLiveContainer:
    def __init__(self, cid):
        self.id = cid


class _FakeLiveContainers:
    def __init__(self, ids):
        self._ids = list(ids)

    def list(self, all=True):  # noqa: A002 - Docker SDK signature
        return [_FakeLiveContainer(i) for i in self._ids]


class _FakeDocker:
    def __init__(self, live_ids=()):
        self.containers = _FakeLiveContainers(live_ids)


def _bare_manager(**attrs):
    """A ``ContainerManager`` with only the attributes the removal path reads."""
    mgr = ContainerManager.__new__(ContainerManager)
    mgr.workspace_id = attrs.get("workspace_id", "ws1")
    mgr.session_id = attrs.get("session_id", "s1")
    mgr.session_permissions = attrs.get("session_permissions", None)
    mgr.vault_root = attrs.get("vault_root", "/tmp/tm-nonexistent-vault")
    return mgr


@pytest.fixture(autouse=True)
def _tmp_vault(tmp_path, monkeypatch):
    """Isolated default vault + cleared module-level name index."""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))

    def _reset():
        container_manager._NAME_INDEX.clear()
        container_manager._NAME_INDEX_COLLISIONS.clear()
        container_manager._NAME_INDEX_BUILT.clear()

    _reset()
    yield
    _reset()


# ===========================================================================
# Site 5 -- ContainerManager._remove_container
# ===========================================================================
def test_site5_remove_container_routes_through_reconcile(monkeypatch):
    reap, emit = [], []
    monkeypatch.setattr(container_manager, "_reap_bound_record",
                        lambda ws, c: reap.append((ws, c)))
    monkeypatch.setattr(
        container_manager, "_emit_container_removed",
        lambda ws, sid, c, reason="removed": emit.append((ws, sid, c, reason)),
    )
    spy = _ReconcileSpy(monkeypatch)

    mgr = _bare_manager()
    c = FakeContainer()
    mgr._remove_container(c)

    # Exactly one decision, driven by an explicit reap intent.
    assert len(spy.calls) == 1
    _, session, intent = spy.calls[0]
    assert intent.kind == "reap"
    assert intent.explicit is True
    assert session is None  # session_permissions=None passed through

    # Destroy proceeded (action="reap" -> destroys).
    assert c.stop_calls == [{"timeout": 5}]
    assert len(c.remove_calls) == 1
    assert reap == [("ws1", c)]
    assert emit and emit[0][3] == "removed"


def test_site5_remove_only_called_on_decision_destroys(monkeypatch):
    monkeypatch.setattr(container_manager, "_reap_bound_record",
                        lambda *a, **k: None)
    monkeypatch.setattr(container_manager, "_emit_container_removed",
                        lambda *a, **k: None)
    # Force a non-destroy decision: the container must be left untouched.
    _ReconcileSpy(
        monkeypatch,
        decision=ReconcileDecision("refuse", "nope", "default"),
    )

    mgr = _bare_manager()
    c = FakeContainer()
    mgr._remove_container(c)

    assert c.stop_calls == []
    assert c.remove_calls == []


# ===========================================================================
# Site 6 -- cleanup_workspace
# ===========================================================================
def _silence_side_effects(monkeypatch):
    monkeypatch.setattr(container_manager, "_reap_bound_record",
                        lambda *a, **k: None)
    monkeypatch.setattr(container_manager, "_emit_container_removed",
                        lambda *a, **k: None)
    monkeypatch.setattr(container_manager, "_audit", lambda *a, **k: None)


def test_site6_cleanup_workspace_destroys_via_funnel(monkeypatch):
    _silence_side_effects(monkeypatch)
    spy = _ReconcileSpy(monkeypatch)

    c = FakeContainer(labels={WORKSPACE_LABEL: "ws1"})
    res = cleanup_workspace("ws1", FakeClient([c]))

    assert res == {"removed": 1}
    assert len(spy.calls) == 1
    assert spy.calls[0][2].kind == "reap"
    assert spy.calls[0][2].explicit is True
    assert c.remove_calls == [{"force": True}]


def test_site6_cleanup_workspace_honours_refuse(monkeypatch):
    _silence_side_effects(monkeypatch)
    _ReconcileSpy(
        monkeypatch,
        decision=ReconcileDecision("refuse", "workspace-owned mismatch", "workspace"),
    )

    c = FakeContainer(labels={WORKSPACE_LABEL: "ws1"})
    res = cleanup_workspace("ws1", FakeClient([c]))

    # Nothing destroyed on a refuse -- the container stays alive.
    assert res == {"removed": 0}
    assert c.stop_calls == []
    assert c.remove_calls == []


# ===========================================================================
# Site 7a -- sweep_exited_workspace_containers
# ===========================================================================
def _run_sweep(containers, registered_workspace_ids, max_age_s=3600, dry_run=False):
    mac = {cls: max_age_s for cls in LIFECYCLE_CLASSES}
    client = FakeClient(containers)
    with mock.patch("docker.from_env", return_value=client):
        return sweep_exited_workspace_containers(
            registered_workspace_ids=registered_workspace_ids,
            max_age_s=max_age_s,
            dry_run=dry_run,
            max_age_s_by_class=mac,
        )


def _capture_lifecycle(monkeypatch):
    from agent.events import EventType, global_event_bus

    assert global_event_bus is not None, "expected a live global event bus"
    captured = []
    monkeypatch.setattr(global_event_bus, "publish",
                        lambda ev: captured.append(ev))
    monkeypatch.setattr(container_manager, "_audit", lambda *a, **k: None)
    return EventType, captured


def test_site7a_sweep_exited_removes_and_emits_event(monkeypatch):
    EventType, captured = _capture_lifecycle(monkeypatch)
    c = FakeContainer(name="c1", status="exited",
                      labels={WORKSPACE_LABEL: "ws1"}, finished_at=_iso(7200))

    res = _run_sweep([c], ["ws1"], max_age_s=3600)

    assert res["removed"] == 1
    assert res["removed_registered"] == 1
    assert c.remove_calls == [{"force": True}]

    events = [e for e in captured if e.type == EventType.CONTAINER_LIFECYCLE]
    assert len(events) == 1
    assert events[0].data["action"] == "reap"
    assert events[0].data["reason"] == "ttl_expired"


def test_site7a_too_young_kept(monkeypatch):
    monkeypatch.setattr(container_manager, "_audit", lambda *a, **k: None)
    c = FakeContainer(name="c1", status="exited",
                      labels={WORKSPACE_LABEL: "ws1"}, finished_at=_iso(600))

    res = _run_sweep([c], ["ws1"], max_age_s=3600)

    assert res["removed"] == 0
    assert res["skipped"] == 1
    assert "too young" in res["detail"]
    assert c.remove_calls == []


def test_site7a_unregistered_uses_orphaned_reason(monkeypatch):
    EventType, captured = _capture_lifecycle(monkeypatch)
    c = FakeContainer(name="orphan1", status="exited",
                      labels={WORKSPACE_LABEL: "ws2"}, finished_at=_iso(7200))

    res = _run_sweep([c], ["ws1"], max_age_s=3600)

    assert res["removed"] == 1
    assert res["removed_orphan"] == 1
    events = [e for e in captured if e.type == EventType.CONTAINER_LIFECYCLE]
    assert len(events) == 1
    assert events[0].data["reason"] == "orphaned"


def test_site7a_every_removal_emits_event(monkeypatch):
    EventType, captured = _capture_lifecycle(monkeypatch)
    c1 = FakeContainer(name="c1", labels={WORKSPACE_LABEL: "ws1"},
                       finished_at=_iso(7200))
    c2 = FakeContainer(name="c2", labels={WORKSPACE_LABEL: "ws1"},
                       finished_at=_iso(7200))

    res = _run_sweep([c1, c2], ["ws1"], max_age_s=3600)

    assert res["removed"] == 2
    events = [e for e in captured if e.type == EventType.CONTAINER_LIFECYCLE]
    assert len(events) == 2
    assert all(e.data["action"] == "reap" for e in events)


def test_site7a_dry_run_does_not_reconcile_or_remove(monkeypatch):
    monkeypatch.setattr(container_manager, "_audit", lambda *a, **k: None)
    spy = _ReconcileSpy(monkeypatch)
    c = FakeContainer(name="c1", labels={WORKSPACE_LABEL: "ws1"},
                      finished_at=_iso(7200))

    res = _run_sweep([c], ["ws1"], max_age_s=3600, dry_run=True)

    assert res["removed"] == 1
    assert c.remove_calls == []
    assert spy.calls == []  # no decision emitted for a non-removal


# ===========================================================================
# Site 7b -- sweep_orphan_container_records
# ===========================================================================
def _mint(workspace_id, record_id, *, docker_id="", age_days=None,
          lifecycle_class=LIFECYCLE_PERSISTENT):
    create_record(
        workspace_id, lifecycle_class, OWNER_WORKSPACE,
        id=record_id, name=record_id,
    )
    path = storage.record_path(workspace_id, record_id)
    data = storage.read_record_file(path)
    data["docker_id"] = docker_id
    if age_days is not None:
        ts = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
        data["created_at"] = ts
        data["updated_at"] = ts
    storage.write_record_file(path, data)


def test_site7b_orphan_record_reaped_and_emits_event(monkeypatch):
    EventType, captured = _capture_lifecycle(monkeypatch)
    _mint(_ORPHAN_WS, "rec-1", docker_id="c" * 16, age_days=10)

    result = sweep_orphan_container_records(
        registered_workspace_ids=[_KEPT_WS], docker_client=_FakeDocker([]),
    )

    assert result["removed"] == 1
    assert load_record(_ORPHAN_WS, "rec-1") is None
    events = [e for e in captured if e.type == EventType.CONTAINER_LIFECYCLE]
    assert len(events) == 1
    assert events[0].data["action"] == "reap"
    assert events[0].data["reason"] == "orphaned"


def test_site7b_refuse_keeps_record(monkeypatch):
    _ReconcileSpy(
        monkeypatch,
        decision=ReconcileDecision("refuse", "leave it", "default"),
    )
    monkeypatch.setattr(container_manager, "_audit", lambda *a, **k: None)
    _mint(_ORPHAN_WS, "rec-1", docker_id="c" * 16, age_days=10)

    result = sweep_orphan_container_records(
        registered_workspace_ids=[_KEPT_WS], docker_client=_FakeDocker([]),
    )

    assert result["removed"] == 0
    assert load_record(_ORPHAN_WS, "rec-1") is not None
