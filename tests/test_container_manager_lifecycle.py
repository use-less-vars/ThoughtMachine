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
          lifecycle_class=LIFECYCLE_PERSISTENT, created_at_days=None,
          updated_at_days=None, state=None, intent_snapshot=None):
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
    # Independent overrides so a record can be OLD but freshly edited (or not).
    if created_at_days is not None:
        data["created_at"] = (
            datetime.now(timezone.utc)
            - timedelta(days=created_at_days)).isoformat()
    if updated_at_days is not None:
        data["updated_at"] = (
            datetime.now(timezone.utc)
            - timedelta(days=updated_at_days)).isoformat()
    if state is not None:
        data["state"] = state
    if intent_snapshot is not None:
        data["intent_snapshot"] = intent_snapshot
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


def test_site7b_age_is_measured_from_created_at_not_updated_at(monkeypatch):
    """The age clock is the IMMUTABLE ``created_at``, never ``updated_at``.

    ``updated_at`` is rewritten by ``append_event`` (drift scan and every other
    event write), so ageing from it would make an event-touched record immortal.
    """
    monkeypatch.setattr(container_manager, "_audit", lambda *a, **k: None)
    # BOTH records are 40 days old by ``created_at``; the first was "touched"
    # moments ago (``updated_at`` bumped to now).  The birth stamp is the clock,
    # so the touched record is JUST as reap-eligible as the untouched one.
    _mint(_ORPHAN_WS, "rec-touched", docker_id="c" * 16,
          created_at_days=40, updated_at_days=0)
    _mint(_ORPHAN_WS, "rec-stale", docker_id="d" * 16,
          created_at_days=40, updated_at_days=40)

    result = sweep_orphan_container_records(
        registered_workspace_ids=[_KEPT_WS], docker_client=_FakeDocker([]),
    )

    assert result["removed"] == 2
    assert sorted(result["removed_records"]) == ["rec-stale", "rec-touched"]
    assert "too young" not in result["detail"]
    assert load_record(_ORPHAN_WS, "rec-touched") is None
    assert load_record(_ORPHAN_WS, "rec-stale") is None


def test_site7b_record_mid_creation_is_never_an_orphan(monkeypatch):
    """``state='creating'`` is skipped before the age gate is even consulted."""
    monkeypatch.setattr(container_manager, "_audit", lambda *a, **k: None)
    _mint(_ORPHAN_WS, "rec-creating", docker_id="c" * 16,
          created_at_days=40, updated_at_days=40, state="creating")

    result = sweep_orphan_container_records(
        registered_workspace_ids=[_KEPT_WS], docker_client=_FakeDocker([]),
    )

    assert result["removed"] == 0
    assert "state_creating" in result["detail"]
    assert load_record(_ORPHAN_WS, "rec-creating") is not None


def test_site7b_record_in_error_state_is_never_an_orphan(monkeypatch):
    """``state='error'`` is exempt: reaping it would race the recovery rebuild."""
    monkeypatch.setattr(container_manager, "_audit", lambda *a, **k: None)
    _mint(_ORPHAN_WS, "rec-error", docker_id="c" * 16,
          created_at_days=40, updated_at_days=40, state="error")

    result = sweep_orphan_container_records(
        registered_workspace_ids=[_KEPT_WS], docker_client=_FakeDocker([]),
    )

    assert result["removed"] == 0
    assert "state_error" in result["detail"]
    assert load_record(_ORPHAN_WS, "rec-error") is not None


def test_site7b_reap_audit_carries_the_reconcile_fields(monkeypatch):
    """RECORD_REAP names the record, its state and its last intent."""
    captured = []
    monkeypatch.setattr(container_manager, "_audit",
                        lambda event, data="": captured.append((event, data)))
    _mint(_ORPHAN_WS, "rec-1", docker_id="c" * 16, age_days=10,
          state="running", intent_snapshot={"mem_limit": "256m"})

    sweep_orphan_container_records(
        registered_workspace_ids=[_KEPT_WS], docker_client=_FakeDocker([]),
    )

    reaps = [data for event, data in captured if event == "RECORD_REAP"]
    assert len(reaps) == 1, reaps
    message = reaps[0]
    assert "workspace_id=w-orphan" in message
    assert "record_id=rec-1" in message
    assert "dry_run=False" in message
    assert "name='rec-1'" in message
    assert "state='running'" in message
    assert "created_at=" in message
    assert "updated_at=" in message
    assert "intent_snapshot={'mem_limit': '256m'}" in message


# ===========================================================================
# T1a -- funnel the two PUBLIC primitives: stop() (U1) + remove() (U2)
# ===========================================================================
# These exercise the two agent-facing ContainerManager primitives that now
# route their explicit-reap decision through the lifecycle funnel, plus the
# three transitive teardown sites (U11 release_container, U12
# cleanup_worker_containers, U13 ExecutionTracker.terminate_all) that reach
# the funnel THROUGH those primitives (their source files are untouched).


class _LiveFakeContainer:
    """A container whose status flips to ``exited`` once stopped."""

    def __init__(self, *, cid="c1", name="tm-c1", status="running", labels=None):
        self.id = cid
        self.name = name
        self.status = status
        self.labels = dict(labels or {})
        self.stop_calls = []
        self.remove_calls = []
        self.kill_calls = 0

    def reload(self):  # noqa: D401 - docker SDK no-op here
        return None

    def stop(self, timeout=None):
        self.stop_calls.append({"timeout": timeout})
        if self.status == "running":
            self.status = "exited"

    def kill(self):
        self.kill_calls += 1
        self.status = "exited"

    def remove(self, force=False):
        self.remove_calls.append({"force": force})


class _GetFakeContainers:
    def __init__(self, mapping):
        self._mapping = dict(mapping)

    def get(self, cid):
        return self._mapping[cid]


class _GetFakeClient:
    def __init__(self, mapping):
        self.containers = _GetFakeContainers(mapping)


def _mgr_with(mapping, **attrs):
    """A bare ``ContainerManager`` wired to a ``containers.get`` fake client."""
    mgr = _bare_manager(**attrs)
    mgr.client = _GetFakeClient(mapping)
    mgr._containers = {}
    return mgr


# -- U1 -- ContainerManager.stop() ------------------------------------------
def test_u1_stop_routes_through_reconcile(monkeypatch):
    EventType, captured = _capture_lifecycle(monkeypatch)
    spy = _ReconcileSpy(monkeypatch)
    c = _LiveFakeContainer(cid="c1", name="tm-c1", status="running")
    mgr = _mgr_with({"c1": c})

    res = mgr.stop("c1")

    assert res == {"status": "stopped", "container_id": "c1", "name": "tm-c1"}
    # Exactly one decision, driven by an explicit reap intent.
    assert len(spy.calls) == 1
    _, session, intent = spy.calls[0]
    assert intent.kind == "reap"
    assert intent.explicit is True
    assert intent.reason == "explicit_stop"
    assert session is None  # session_permissions=None passed through

    # Destroy proceeded (action="reap" -> destroys): stop fired, kill did not
    # (status already exited after the stop).
    assert c.stop_calls == [{"timeout": 5}]
    assert c.kill_calls == 0

    # One CONTAINER_LIFECYCLE event per reconcile.
    events = [e for e in captured if e.type == EventType.CONTAINER_LIFECYCLE]
    assert len(events) == len(spy.calls) == 1
    assert events[0].data["action"] == "reap"


def test_u1_stop_only_fires_on_decision_destroys(monkeypatch):
    monkeypatch.setattr(container_manager, "_audit", lambda *a, **k: None)
    # Force a non-destroy decision: the container must be left running.
    _ReconcileSpy(
        monkeypatch,
        decision=ReconcileDecision("refuse", "nope", "default"),
    )
    c = _LiveFakeContainer(cid="c1", status="running")
    mgr = _mgr_with({"c1": c})

    res = mgr.stop("c1")

    assert res["status"] == "refused"
    assert res["reason"] == "nope"
    assert c.stop_calls == []
    assert c.kill_calls == 0


def test_u1_stop_running_container_kills_when_still_running(monkeypatch):
    """stop()+kill() both fire when the container stays running after stop()."""
    EventType, captured = _capture_lifecycle(monkeypatch)
    _ReconcileSpy(monkeypatch)

    class _Stubborn(_LiveFakeContainer):
        def stop(self, timeout=None):  # noqa: D401 - stays running
            self.stop_calls.append({"timeout": timeout})

    c = _Stubborn(cid="c1", status="running")
    mgr = _mgr_with({"c1": c})

    res = mgr.stop("c1")

    assert res["status"] == "stopped"
    assert c.stop_calls == [{"timeout": 5}]
    assert c.kill_calls == 1


# -- U2 -- ContainerManager.remove() ----------------------------------------
def test_u2_remove_routes_through_reconcile_once(monkeypatch):
    EventType, captured = _capture_lifecycle(monkeypatch)
    monkeypatch.setattr(container_manager, "_reap_bound_record",
                        lambda *a, **k: None)
    monkeypatch.setattr(container_manager, "_emit_container_removed",
                        lambda *a, **k: None)
    spy = _ReconcileSpy(monkeypatch)
    c = _LiveFakeContainer(cid="c1", name="tm-c1", status="running")
    mgr = _mgr_with({"c1": c})

    res = mgr.remove("c1")

    assert res == {"status": "removed", "container_id": "c1"}
    # A SINGLE removal decides ONCE: the internal stop must not re-reconcile.
    assert len(spy.calls) == 1
    _, session, intent = spy.calls[0]
    assert intent.kind == "reap"
    assert intent.explicit is True
    assert intent.reason == "explicit_remove"

    # Destroy proceeded: stop (via the reconcile-free core) + remove.
    assert c.stop_calls == [{"timeout": container_manager._REMOVE_GRACE_SECONDS}]
    assert c.remove_calls == [{"force": True}]

    # ONE CONTAINER_LIFECYCLE event per reconcile.
    events = [e for e in captured if e.type == EventType.CONTAINER_LIFECYCLE]
    assert len(events) == 1
    assert len(events) == len(spy.calls)
    assert events[0].data["action"] == "reap"


def test_u2_remove_truncates_the_stop_grace(monkeypatch):
    """remove() force-removes on the next line -- it must not burn the full 5s
    graceful-stop ceiling; a plain ``stop()`` still gets the full grace."""
    _capture_lifecycle(monkeypatch)
    monkeypatch.setattr(container_manager, "_reap_bound_record",
                        lambda *a, **k: None)
    monkeypatch.setattr(container_manager, "_emit_container_removed",
                        lambda *a, **k: None)
    _ReconcileSpy(monkeypatch)

    assert container_manager._REMOVE_GRACE_SECONDS < 5

    c = _LiveFakeContainer(cid="c1", name="tm-c1", status="running")
    mgr = _mgr_with({"c1": c})

    assert mgr.remove("c1") == {"status": "removed", "container_id": "c1"}

    # The pre-force-remove stop is truncated ...
    assert c.stop_calls == [{"timeout": container_manager._REMOVE_GRACE_SECONDS}]
    assert c.remove_calls == [{"force": True}]

    # ... while an agent-facing stop keeps the full grace.
    c2 = _LiveFakeContainer(cid="c2", name="tm-c2", status="running")
    mgr2 = _mgr_with({"c2": c2})

    assert mgr2.stop("c2")["status"] == "stopped"
    assert c2.stop_calls == [{"timeout": 5}]


def test_u2_remove_only_fires_on_decision_destroys(monkeypatch):
    monkeypatch.setattr(container_manager, "_reap_bound_record",
                        lambda *a, **k: None)
    monkeypatch.setattr(container_manager, "_emit_container_removed",
                        lambda *a, **k: None)
    _ReconcileSpy(
        monkeypatch,
        decision=ReconcileDecision("refuse", "nope", "default"),
    )
    c = _LiveFakeContainer(cid="c1", status="running")
    mgr = _mgr_with({"c1": c})

    res = mgr.remove("c1")

    assert res["status"] == "refused"
    assert c.stop_calls == []
    assert c.remove_calls == []


def test_u2_remove_keep_record_preserves_record(monkeypatch):
    """keep_record=True skips the record reap but still funnels the removal."""
    EventType, _ = _capture_lifecycle(monkeypatch)
    reap, emit = [], []
    monkeypatch.setattr(container_manager, "_reap_bound_record",
                        lambda ws, c: reap.append((ws, c)))
    monkeypatch.setattr(container_manager, "_emit_container_removed",
                        lambda ws, sid, c, reason="removed": emit.append(reason))
    spy = _ReconcileSpy(monkeypatch)
    c = _LiveFakeContainer(cid="c1", status="exited")
    mgr = _mgr_with({"c1": c})

    res = mgr.remove("c1", keep_record=True)

    assert res == {"status": "removed", "container_id": "c1"}
    assert len(spy.calls) == 1
    assert spy.calls[0][2].reason == "explicit_remove"
    assert reap == []                       # record preserved
    assert emit == ["recreate"]             # tagged for the recreate ladder
    assert c.remove_calls == [{"force": True}]


# -- U11 / U12 / U13 -- transitive sites reach the funnel for free ----------
def test_u11_release_container_routes_through_reconcile(monkeypatch):
    _capture_lifecycle(monkeypatch)
    spy = _ReconcileSpy(monkeypatch)
    c = _LiveFakeContainer(cid="c1", status="running")
    mgr = _mgr_with({"c1": c})

    from infra.workspace_lifecycle_manager import WorkerSupervisor

    sup = WorkerSupervisor("w1", mgr, None, feature_flag_check=lambda: True)
    res = sup.release_container("c1")

    assert res["status"] == "stopped"
    assert len(spy.calls) == 1
    assert spy.calls[0][2].reason == "explicit_stop"


def test_u12_cleanup_worker_containers_routes_through_reconcile(monkeypatch):
    _capture_lifecycle(monkeypatch)
    import tools.workspace.worker_container as worker_container

    # The lifecycle-class exclusion is orthogonal here: treat the fixture as
    # reclaimable so the ownership sweep reaches the two primitives.
    monkeypatch.setattr(worker_container, "is_worker_teardown_excluded_container",
                        lambda c: False)
    monkeypatch.setattr(container_manager, "_reap_bound_record",
                        lambda *a, **k: None)
    monkeypatch.setattr(container_manager, "_emit_container_removed",
                        lambda *a, **k: None)
    spy = _ReconcileSpy(monkeypatch)

    owner = "s1:w1"
    c = _LiveFakeContainer(cid="c1", name="tm-c1", status="running",
                           labels={"thoughtmachine.worker": owner})
    mgr = _mgr_with({"c1": c})
    mgr.list_containers = lambda: [
        {"container_id": "c1", "name": "tm-c1",
         "labels": {"thoughtmachine.worker": owner}}
    ]

    worker_container.cleanup_worker_containers(mgr, owner)

    # cleanup_worker_containers calls stop() THEN remove(); both route through
    # the funnel (each emits exactly one decision).
    reasons = [call[2].reason for call in spy.calls]
    assert reasons == ["explicit_stop", "explicit_remove"]
    assert c.remove_calls == [{"force": True}]


def test_u13_terminate_all_routes_through_reconcile(monkeypatch):
    _capture_lifecycle(monkeypatch)
    spy = _ReconcileSpy(monkeypatch)
    c = _LiveFakeContainer(cid="c1", status="running")
    mgr = _mgr_with({"c1": c})

    from tools.workspace.worker_execution import ExecutionTracker

    tracker = ExecutionTracker()
    tracker.register("w1", query_id="q1", container_id="c1",
                     type="scoped_container")

    tracker.terminate_all("w1", mgr, None, session_id="s1")

    assert len(spy.calls) == 1
    assert spy.calls[0][2].reason == "explicit_stop"

