"""Per-class idle-TTL behaviour of ``sweep_exited_workspace_containers``.

``sweep_exited_workspace_containers`` now keys each container's idle TTL off
its LIFECYCLE CLASS (``max_age_s_by_class``); a bare ``max_age_s`` is ONLY a
fallback for classes missing from the map.  When ``max_age_s_by_class`` is
``None`` the per-class defaults apply: ``ephemeral`` 900 s,
``persistent``/``resource``/``service`` 86400 s.

A container's class comes from ``_container_lifecycle_class`` ->
``lifecycle_class_from_labels``: a resource-like container (``RESOURCE_LABEL``)
is ``resource``; any other container is resolved through the container-record
named by its ``RECORD_LABEL_KEY`` label (via ``find_by_docker_label``) and
falls back to ``persistent`` when no record is attached.

These tests drive the REAL sweep against a fake docker client.  Where a class
other than the ``persistent`` fallback is required, the fake carries a
``RECORD_LABEL_KEY`` label and ``find_by_docker_label`` is monkeypatched.

T1  ephemeral finished 1000 s ago  -> REMOVED (default 900 s)
T2  ephemeral finished  500 s ago  -> kept (too young)
T3  persistent finished 1000 s ago -> kept (default 86400 s)
T4  persistent finished 90000 s ago-> REMOVED
T5  RUNNING ephemeral (any age)    -> kept (never touched)
T6  env knob = 100 -> persistent 200 s old REMOVED (server wrapper)
T7  resource & service, old        -> still skipped by ``_gc_should_skip``
"""
from datetime import datetime, timedelta, timezone
from unittest import mock

from infra import container_manager
from thoughtmachine.container_record import (
    LIFECYCLE_CLASSES,
    RECORD_LABEL_KEY,
)

WORKSPACE_LABEL = "thoughtmachine.workspace_id"
RESOURCE_LABEL = "thoughtmachine.resource"


def _iso(seconds_ago=0):
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return dt.isoformat().replace("+00:00", "Z")


class FakeContainer:
    def __init__(self, name, status="exited", labels=None, finished_at=None):
        self.name = name
        self.id = name
        self.status = status
        self.labels = dict(labels or {})
        self.attrs = {"State": {"FinishedAt": finished_at or _iso(0)}}
        self.remove_calls = []

    def remove(self, force=False):
        self.remove_calls.append({"force": force})


class _FakeContainers:
    def __init__(self, containers):
        self.containers = list(containers)

    def list(self, all=False, filters=None):
        return list(self.containers)


class _FakeClient:
    def __init__(self, containers):
        self.containers = _FakeContainers(containers)


class _FakeRecord:
    """Minimal stand-in for ``container_record.api.Record``."""

    def __init__(self, lifecycle_class):
        self.lifecycle_class = lifecycle_class


def _sweep(containers, monkeypatch, *, record_classes=None,
           registered_workspace_ids=None, max_age_s=86400, dry_run=False,
           max_age_s_by_class=None):
    """Run the REAL sweep against a fake docker client.

    ``record_classes`` maps a ``RECORD_LABEL_KEY`` label value -> lifecycle
    class; it is used to patch the record resolver (``find_by_docker_label``)
    so a fake carrying that label resolves to the mapped class.  The resolver
    is ALWAYS patched so no test ever reaches the real (disk-backed) lookup.
    """
    table = dict(record_classes or {})

    def _fake_lookup(record_id, vault_root=None):
        cls = table.get(record_id)
        return _FakeRecord(cls) if cls else None

    monkeypatch.setattr(container_manager, "find_by_docker_label", _fake_lookup)
    client = _FakeClient(containers)
    with mock.patch("docker.from_env", return_value=client):
        return container_manager.sweep_exited_workspace_containers(
            registered_workspace_ids=registered_workspace_ids,
            max_age_s=max_age_s,
            dry_run=dry_run,
            max_age_s_by_class=max_age_s_by_class,
        )


def _ephemeral(name, wid, *, finished_at, status="exited"):
    """A fake container resolving to LIFECYCLE_EPHEMERAL via its record label."""
    rec_id = f"rec-{name}"
    c = FakeContainer(name, status=status,
                      labels={WORKSPACE_LABEL: wid, RECORD_LABEL_KEY: rec_id},
                      finished_at=finished_at)
    return c, {rec_id: "ephemeral"}


# ── T1 ───────────────────────────────────────────────────────────────────────
def test_t1_ephemeral_old_removed(monkeypatch):
    """ephemeral finished 1000 s ago -> removed (default 900 s)."""
    c, table = _ephemeral("c1", "ws1", finished_at=_iso(1000))
    result = _sweep([c], monkeypatch, record_classes=table,
                    registered_workspace_ids=["ws1"])
    assert result["removed"] == 1
    assert result["removed_registered"] == 1
    assert result["removed_containers"] == ["c1"]
    assert c.remove_calls == [{"force": True}]


# ── T2 ───────────────────────────────────────────────────────────────────────
def test_t2_ephemeral_young_kept(monkeypatch):
    """ephemeral finished 500 s ago -> kept (too young)."""
    c, table = _ephemeral("c1", "ws1", finished_at=_iso(500))
    result = _sweep([c], monkeypatch, record_classes=table,
                    registered_workspace_ids=["ws1"])
    assert result["removed"] == 0
    assert result["skipped"] == 1
    assert c.remove_calls == []
    assert "too young" in result["detail"]


# ── T3 ───────────────────────────────────────────────────────────────────────
def test_t3_persistent_old_kept(monkeypatch):
    """persistent finished 1000 s ago -> kept (default 86400 s)."""
    # No RECORD_LABEL_KEY -> resolves to the persistent fallback.
    c = FakeContainer("c1", labels={WORKSPACE_LABEL: "ws1"},
                      finished_at=_iso(1000))
    result = _sweep([c], monkeypatch, registered_workspace_ids=["ws1"])
    assert result["removed"] == 0
    assert result["skipped"] == 1
    assert c.remove_calls == []
    assert "too young" in result["detail"]


# ── T4 ───────────────────────────────────────────────────────────────────────
def test_t4_persistent_very_old_removed(monkeypatch):
    """persistent finished 90000 s ago -> removed (past 86400 s)."""
    c = FakeContainer("c1", labels={WORKSPACE_LABEL: "ws1"},
                      finished_at=_iso(90000))
    result = _sweep([c], monkeypatch, registered_workspace_ids=["ws1"])
    assert result["removed"] == 1
    assert result["removed_containers"] == ["c1"]
    assert c.remove_calls == [{"force": True}]


# ── T5 ───────────────────────────────────────────────────────────────────────
def test_t5_running_ephemeral_kept(monkeypatch):
    """A RUNNING ephemeral container is never touched, whatever its age."""
    c, table = _ephemeral("c1", "ws1", finished_at=_iso(100000),
                          status="running")
    result = _sweep([c], monkeypatch, record_classes=table,
                    registered_workspace_ids=["ws1"])
    assert result["removed"] == 0
    assert result["skipped"] == 1
    assert c.remove_calls == []


# ── T6 ───────────────────────────────────────────────────────────────────────
class _FakeEntry:
    def __init__(self, workspace_id):
        self.id = workspace_id


def test_t6_env_knob_forces_single_ttl(monkeypatch):
    """``THOUGHTMACHINE_EXITED_CONTAINER_MAX_AGE_S``=100 forces EVERY class.

    The knob is parsed ONCE at server import time into the module global
    ``server._EXITED_CONTAINER_SWEEP_MAX_AGE_S``; exporting the env var after
    import therefore has NO effect on its own.  We set BOTH the env var
    (documenting the knob / any re-parse) AND the module global the wrapper
    actually reads.  A persistent container 200 s old -- normally kept under
    the 86400 s default -- must then be removed.
    """
    from web_ui.backend import server

    monkeypatch.setenv("THOUGHTMACHINE_EXITED_CONTAINER_MAX_AGE_S", "100")
    monkeypatch.setattr(server, "_EXITED_CONTAINER_SWEEP_MAX_AGE_S", 100)

    registry = server.WorkspaceRegistry.__new__(server.WorkspaceRegistry)
    entry = _FakeEntry("ws1")
    monkeypatch.setattr(
        server.WorkspaceRegistry, "get_default", staticmethod(lambda: registry))
    monkeypatch.setattr(
        server.WorkspaceRegistry, "list_workspaces", staticmethod(lambda: [entry]))

    c = FakeContainer("c1", labels={WORKSPACE_LABEL: "ws1"},
                      finished_at=_iso(200))
    client = _FakeClient([c])
    with mock.patch("docker.from_env", return_value=client):
        server._sweep_exited_workspace_containers()

    assert c.remove_calls == [{"force": True}]


# ── T7 ───────────────────────────────────────────────────────────────────────
def test_t7_resource_and_service_skipped(monkeypatch):
    """resource & service containers are skipped by ``_gc_should_skip``.

    Both lifecycle classes own their lifecycle (``own_lifecycle=True``), so the
    sweep must skip them regardless of age -- the resource one via its
    ``RESOURCE_LABEL``, the service one via its record's lifecycle class.
    """
    res = FakeContainer("res1", labels={
        WORKSPACE_LABEL: "ws1", RESOURCE_LABEL: "git"}, finished_at=_iso(100000))
    svc = FakeContainer("svc1", labels={
        WORKSPACE_LABEL: "ws1", RECORD_LABEL_KEY: "rec-svc1"},
        finished_at=_iso(100000))

    result = _sweep([res, svc], monkeypatch,
                    record_classes={"rec-svc1": "service"},
                    registered_workspace_ids=["ws1"])
    assert result["removed"] == 0
    assert result["skipped"] == 2
    assert res.remove_calls == []
    assert svc.remove_calls == []
    assert "resource" in result["detail"]


# Sanity: the lifespan / server knob path names every declared class.
def test_sweep_accepts_empty_map_and_class_list_is_stable(monkeypatch):
    """``max_age_s_by_class={}`` falls back to the flat ``max_age_s`` per class.

    Also pins that the class list the server knob iterates is the canonical
    ``LIFECYCLE_CLASSES`` tuple, so the knob names every class (a class missing
    from the map would otherwise silently keep its fallback).
    """
    assert set(LIFECYCLE_CLASSES) == {
        "ephemeral", "persistent", "resource", "service"}

    # Empty map -> EVERY class falls back to the flat ``max_age_s``. A
    # PERSISTENT-class container (no RECORD_LABEL_KEY -> persistent fallback)
    # 7200 s old must be REMOVED under a 3600 s flat TTL: had the empty map
    # instead kept the 86400 s per-class default, it would have been skipped.
    flat_max_age_s = 3600
    c = FakeContainer("c1", labels={WORKSPACE_LABEL: "ws1"},
                      finished_at=_iso(7200))
    result = _sweep([c], monkeypatch, max_age_s=flat_max_age_s,
                    max_age_s_by_class={},
                    registered_workspace_ids=["ws1"])
    assert result["removed"] == 1
    assert result["removed_registered"] == 1
    assert result["removed_containers"] == ["c1"]
    assert c.remove_calls == [{"force": True}]
