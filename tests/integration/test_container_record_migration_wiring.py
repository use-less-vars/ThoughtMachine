"""Wiring tests: the server lifespan runs the container-record migration.

The lifespan (``web_ui.backend.server.lifespan``) must, after the old-style
workspace migration and before the default-workspace auto-registration,
best-effort backfill container records from any pre-existing containers.  The
step is idempotent and must never abort startup.

These tests exercise the REAL lifespan under a hermetic vault.  The Docker
daemon is replaced by a filter-aware fake so:

* the migration pass (``filters={"label": "thoughtmachine.workspace_id"}``)
  sees the injected payloads, and
* the later startup integrity scan (``filters={"name": "agent-exec-"}``) sees
  nothing.

The two periodic/startup sweeps are neutralised so no real Docker work runs.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

import infra.container_manager as container_manager
from infra.container_manager import ContainerManager
from tests.test_container_record_migration import make_payload, ws_labels
from thoughtmachine.container_record import api as cr_api
from thoughtmachine.container_record import migration, storage

# Reuse the production label constant (the migration reads it from labels).
WORKSPACE_ID_LABEL = "thoughtmachine.workspace_id"


# ── Fake Docker (filter-aware) ──────────────────────────────────────────────


class _FilterAwareContainers:
    """Stand-in for ``DockerClient.containers`` that honours ``filters``."""

    def __init__(self, payloads, *, raise_on_list=False):
        self._payloads = list(payloads)
        self._raise_on_list = raise_on_list

    def list(self, all=False, filters=None):  # noqa: A002 - docker signature
        if self._raise_on_list:
            raise RuntimeError("cannot connect to the docker daemon")
        filters = filters or {}
        # The startup integrity scan filters by name; report no matches.
        if "name" in filters:
            return []
        if "label" in filters:
            label = filters["label"]
            return [
                p
                for p in self._payloads
                if label in ((p.get("Config") or {}).get("Labels") or {})
            ]
        return list(self._payloads)


class _FakeClient:
    def __init__(self, payloads, *, raise_on_list=False):
        self.containers = _FilterAwareContainers(
            payloads, raise_on_list=raise_on_list
        )

    def ping(self):
        return True


def _install_fake_docker(monkeypatch, client):
    """Install a stub ``docker`` module whose ``from_env()`` returns *client*."""
    mod = types.ModuleType("docker")
    mod.from_env = lambda *a, **k: client
    monkeypatch.setitem(sys.modules, "docker", mod)


# ── Lifespan / logging harness ──────────────────────────────────────────────


def _load_server():
    from web_ui.backend import server

    return server


def _neutralise_sweeps(monkeypatch, server):
    monkeypatch.setattr(server, "_sweep_exited_workspace_containers", lambda: None)
    monkeypatch.setattr(server, "_sweep_orphan_resource_containers", lambda: None)
    monkeypatch.setattr(server, "_sweep_orphan_container_records", lambda: None)


def _record_log(monkeypatch, server):
    events = []

    def recorder(level, category, message, *args, **kwargs):
        events.append((level, category, message))

    monkeypatch.setattr(server, "log", recorder)
    return events


def _run_lifespan(server):
    """Run the real lifespan start→shutdown under the current event loop."""

    async def scenario():
        async with server.lifespan(server.app):
            pass

    asyncio.run(scenario())


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(root))
    return root


def _records(ws, vault):
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in storage.iter_record_files(storage.containers_dir(ws, vault))
    ]


def _migration_lines(events):
    return [e for e in events if "Container-record migration:" in e[2]]


def _make_container_record(ws, docker_id, vault):
    """Create a natively-authored record, optionally bound to a container id."""
    rec = cr_api.create_record(
        ws, "persistent", "workspace-owned", vault_root=str(vault)
    )
    if docker_id is not None:
        cr_api.attach_container(ws, rec.id, docker_id, vault_root=str(vault))
    return rec


# ── Tests ───────────────────────────────────────────────────────────────────


def test_lifespan_invokes_migration_with_docker_source(monkeypatch, vault):
    """The step calls migrate_records(all workspaces) with the live client."""
    server = _load_server()
    client = _FakeClient([])
    _install_fake_docker(monkeypatch, client)
    _neutralise_sweeps(monkeypatch, server)
    events = _record_log(monkeypatch, server)

    calls = []

    def fake_migrate(workspace_id=None, *, docker_source=None, vault_root=None):
        calls.append(
            {
                "workspace_id": workspace_id,
                "docker_source": docker_source,
                "vault_root": vault_root,
            }
        )
        return {
            "workspace_id": workspace_id,
            "scanned": 0,
            "created": 0,
            "rematerialised": 0,
            "skipped": 0,
            "failed": 0,
            "aborted": False,
            "errors": [],
        }

    import thoughtmachine.container_record as cr

    monkeypatch.setattr(cr, "migrate_records", fake_migrate)

    _run_lifespan(server)

    assert len(calls) == 1
    assert calls[0]["workspace_id"] is None  # all workspaces
    assert calls[0]["docker_source"] is client
    assert calls[0]["vault_root"] is None  # resolved via env at call time

    logged = _migration_lines(events)
    assert logged, events
    assert logged[0][0] == "INFO"
    assert "0 created" in logged[0][2]


def test_aborted_migration_logs_warning(monkeypatch, vault):
    server = _load_server()
    client = _FakeClient([])
    _install_fake_docker(monkeypatch, client)
    _neutralise_sweeps(monkeypatch, server)
    events = _record_log(monkeypatch, server)

    def fake_migrate(workspace_id=None, *, docker_source=None, vault_root=None):
        return {
            "workspace_id": workspace_id,
            "scanned": 0,
            "created": 0,
            "rematerialised": 0,
            "skipped": 0,
            "failed": 0,
            "aborted": True,
            "errors": ["docker access failed"],
        }

    import thoughtmachine.container_record as cr

    monkeypatch.setattr(cr, "migrate_records", fake_migrate)

    _run_lifespan(server)

    warned = [e for e in events if e[0] == "WARNING" and "aborted" in e[2]]
    assert warned, events


def test_failed_migration_logs_warning(monkeypatch, vault):
    server = _load_server()
    client = _FakeClient([])
    _install_fake_docker(monkeypatch, client)
    _neutralise_sweeps(monkeypatch, server)
    events = _record_log(monkeypatch, server)

    def fake_migrate(workspace_id=None, *, docker_source=None, vault_root=None):
        return {
            "workspace_id": workspace_id,
            "scanned": 2,
            "created": 1,
            "rematerialised": 0,
            "skipped": 0,
            "failed": 1,
            "aborted": False,
            "errors": ["boom"],
        }

    import thoughtmachine.container_record as cr

    monkeypatch.setattr(cr, "migrate_records", fake_migrate)

    _run_lifespan(server)

    warned = [e for e in events if e[0] == "WARNING" and "failed" in e[2]]
    assert warned, events


def test_migration_exception_does_not_break_startup(monkeypatch, vault):
    """A raising migration is logged and startup still completes."""
    server = _load_server()
    client = _FakeClient([])
    _install_fake_docker(monkeypatch, client)
    _neutralise_sweeps(monkeypatch, server)
    events = _record_log(monkeypatch, server)

    def boom(workspace_id=None, *, docker_source=None, vault_root=None):
        raise RuntimeError("migration blew up")

    import thoughtmachine.container_record as cr

    monkeypatch.setattr(cr, "migrate_records", boom)

    _run_lifespan(server)  # must not raise

    skipped = [
        e
        for e in events
        if e[0] == "WARNING" and "Container-record migration skipped:" in e[2]
    ]
    assert skipped, events
    # Startup continued past the failure (later lifecycle steps still ran).
    assert any("sweep scheduled" in e[2] for e in events), events


def test_lifespan_migration_is_idempotent(monkeypatch, vault):
    """Two lifespans over the same containers create each record exactly once."""
    server = _load_server()
    payloads = [
        make_payload("d1", ws_labels("ws-1", "free_use")),
        make_payload("d2", ws_labels("ws-1", "resource")),
    ]
    _install_fake_docker(monkeypatch, _FakeClient(payloads))
    _neutralise_sweeps(monkeypatch, server)
    events = _record_log(monkeypatch, server)

    # First run: real migration creates both records.
    _run_lifespan(server)
    first = _records("ws-1", vault)
    assert len(first) == 2
    first_log = _migration_lines(events)
    assert first_log and first_log[0][0] == "INFO"
    assert "2 created" in first_log[0][2]

    # Second run: everything is skipped, records are not duplicated.
    events.clear()
    _run_lifespan(server)
    second = _records("ws-1", vault)
    assert len(second) == 2
    assert {r["id"] for r in first} == {r["id"] for r in second}

    second_log = _migration_lines(events)
    assert second_log
    assert "0 created" in second_log[0][2]
    assert "2 skipped" in second_log[0][2]


def test_daemon_down_aborts_without_writes(monkeypatch, vault):
    """A daemon that refuses to list aborts cleanly and writes nothing."""
    server = _load_server()
    _install_fake_docker(monkeypatch, _FakeClient([], raise_on_list=True))
    _neutralise_sweeps(monkeypatch, server)
    events = _record_log(monkeypatch, server)

    _run_lifespan(server)  # must not raise

    warned = [e for e in events if e[0] == "WARNING" and "aborted" in e[2]]
    assert warned, events
    assert _records("ws-1", vault) == []
    assert not storage.containers_dir("ws-1", vault).exists()


# ── Write-on-create ↔ migration composition ────────────
#
# These two tests exercise the REAL fresh-create path
# (``ContainerManager.start`` → ``record_creation``) and then the REAL
# migration pass (``migration.migrate_records``).  The Docker daemon is
# replaced by a hermetic fake client that both serves ``containers.run`` (the
# create) and ``containers.list`` (the migration scan).  The composition the
# tests pin down is:
#
#   * a container recorded by write-on-create must NOT be re-synthesised
#     (duplicated) by a later migration pass, and
#   * if that record's file is lost (crash between create and write), the
#     migration must re-materialise the SAME record id.


def _woc_labels(ws, name, record_id):
    """The exact label set ``ContainerManager.start`` stamps on a fresh create."""
    return {
        "thoughtmachine.container_name": name,
        "thoughtmachine.workspace_id": ws,
        "thoughtmachine.container_type": "free_use",
        "thoughtmachine.container_id": record_id,
    }


class _WoCContainer:
    """Minimal Docker-container stand-in whose ``attrs`` is a real inspect dict."""

    def __init__(self, container_id, name, labels):
        self.id = container_id
        self.name = name
        self.status = "created"
        self.labels = dict(labels or {})
        self.attrs = {
            "Id": container_id,
            "Config": {"Labels": dict(labels or {}), "Image": "agent-executor"},
            "HostConfig": {
                "NetworkMode": "none",
                "Memory": 1073741824,
                "CpuQuota": 100000,
                "OomScoreAdj": 1000,
            },
            "State": {"Status": "created"},
            "Image": "sha256:deadbeef",
        }
        self.stopped = []
        self.removed = []

    def start(self):
        self.status = "running"

    def stop(self, timeout=None):
        self.stopped.append(timeout)

    def remove(self, **kwargs):
        self.removed.append(kwargs)

    def reload(self):
        pass

    def exec_run(self, **kwargs):
        return (0, (b"", b""))


class _WoCContainers:
    """Serves ``run`` (create) and a filter-aware ``list`` (migration scan)."""

    def __init__(self):
        self._items = []
        self.run_calls = []
        self.list_calls = []

    @staticmethod
    def _labels_of(item):
        if isinstance(item, dict):
            return (item.get("Config") or {}).get("Labels") or {}
        return dict(getattr(item, "labels", None) or {})

    def list(self, all=False, filters=None):  # noqa: A002 - docker signature
        self.list_calls.append({"all": all, "filters": filters})
        filters = filters or {}
        items = list(self._items)
        specs = filters.get("label")
        if specs is not None:
            if isinstance(specs, str):
                specs = [specs]

            def _matches(item):
                labels = self._labels_of(item)
                for spec in specs:
                    key, _, value = spec.partition("=")
                    if key not in labels:
                        return False
                    if value and labels.get(key) != value:
                        return False
                return True

            items = [i for i in items if _matches(i)]
        if "name" in filters:
            return []
        return items

    def get(self, container_id):
        for c in self._items:
            if c.id == container_id or c.name == container_id:
                return c
        raise LookupError(container_id)

    def run(self, *args, **kwargs):
        self.run_calls.append({"args": args, "kwargs": kwargs})
        name = kwargs.get("name") or "run-ctr"
        ctr = _WoCContainer(
            "c-run-1", name=name, labels=kwargs.get("labels") or {}
        )
        self._items.append(ctr)
        return ctr


class _WoCClient:
    def __init__(self):
        self.containers = _WoCContainers()

    def ping(self):
        return True


def _woc_manager(ws, client, vault):
    """Hermetic ContainerManager bound to *client* and the temp *vault*.

    Mirrors ``tests/test_container_record_hook.py::_make_container_manager``.
    """
    cm = ContainerManager.__new__(ContainerManager)
    cm.workspace_path = "/tmp/ws-woc"
    cm.session_id = "sess-woc"
    cm.workspace_id = ws
    cm.session_permissions = {}
    cm._session_config = {"use_container_registry": False}
    cm.image = "agent-executor"
    cm.mem_limit = "1g"
    cm.cpu_quota = 100000
    cm._containers = {}
    cm.client = client
    cm._compute_config = lambda *a, **k: ("none", "ro")
    cm.vault_root = str(vault)
    cm.container_notes = {}
    cm.max_containers = 4
    cm.workspace_config = {"max_containers": 4}
    cm.dockerfile_path = None
    return cm


def test_migration_does_not_duplicate_write_on_create_record(monkeypatch, vault):
    """A write-on-create record survives a later migration pass un-duplicated."""
    ws = "ws-woc"
    client = _WoCClient()
    cm = _woc_manager(ws, client, vault)

    result = cm.start(image="agent-executor", name="agent-exec-woc")
    assert result["status"] == "created"

    before = _records(ws, vault)
    assert len(before) == 1
    assert before[0]["docker_id"] == "c-run-1"

    summary = migration.migrate_records(ws, docker_source=client, vault_root=vault)

    after = _records(ws, vault)
    assert len(after) == 1
    assert summary["created"] == 0
    assert {r["id"] for r in after} == {r["id"] for r in before}


def test_migration_rematerialises_write_on_create_record_after_crash(
    monkeypatch, vault
):
    """If a write-on-create record file is lost, migration rebuilds the SAME id."""
    ws = "ws-woc-crash"
    client = _WoCClient()
    cm = _woc_manager(ws, client, vault)

    cm.start(image="agent-executor", name="agent-exec-woc-crash")
    original = _records(ws, vault)[0]

    # Crash window: the record file never made it to disk.
    storage.record_path(ws, original["id"], vault).unlink()
    assert _records(ws, vault) == []

    summary = migration.migrate_records(ws, docker_source=client, vault_root=vault)

    rebuilt = _records(ws, vault)[0]
    assert summary["rematerialised"] == 1
    assert summary["created"] == 0
    assert rebuilt["id"] == original["id"]


# ── Boot container drift scan ───────────────────────────────────────────────
#
# The lifespan must, immediately after the container-record migration, compare
# every container-bearing record against its live container (read-only) using
# the sanctioned ``drift.detect_record_drift`` detector, then append the
# ``state.creating_drift_finding`` lifecycle axis for stuck-``creating``
# records.  The step is bounded by ``server._BOOT_DRIFT_SCAN_LIMIT`` and must
# never abort startup.


def test_lifespan_boot_drift_scan_inspects_each_container_record(
    monkeypatch, vault
):
    """detect_record_drift runs once per container-bearing record."""
    server = _load_server()
    ws = "ws-drift"
    rec_a = _make_container_record(ws, "cid-a", vault)
    rec_b = _make_container_record(ws, "cid-b", vault)
    _make_container_record(ws, None, vault)  # no docker_id → not scanned

    _install_fake_docker(monkeypatch, _FakeClient([]))
    _neutralise_sweeps(monkeypatch, server)
    events = _record_log(monkeypatch, server)

    from thoughtmachine.container_record import drift as drift_module

    scanned = []

    def fake_detect(record, containers, *, permissions=None, capabilities=None):
        scanned.append(record.id)
        return []

    monkeypatch.setattr(drift_module, "detect_record_drift", fake_detect)

    _run_lifespan(server)

    assert set(scanned) == {rec_a.id, rec_b.id}

    info = [e for e in events if "Startup container drift scan" in e[2]]
    assert info, events
    assert info[0][0] == "INFO"
    assert "inspected 2 record(s)" in info[0][2]


def test_lifespan_boot_drift_scan_is_capped(monkeypatch, vault):
    """The cap bounds records scanned; a WARNING names inspected + skipped."""
    server = _load_server()
    monkeypatch.setattr(server, "_BOOT_DRIFT_SCAN_LIMIT", 2)
    ws = "ws-cap"
    for i in range(3):
        _make_container_record(ws, f"cid-{i}", vault)

    _install_fake_docker(monkeypatch, _FakeClient([]))
    _neutralise_sweeps(monkeypatch, server)
    events = _record_log(monkeypatch, server)

    from thoughtmachine.container_record import drift as drift_module

    scanned = []

    def fake_detect(record, containers, *, permissions=None, capabilities=None):
        scanned.append(record.id)
        return []

    monkeypatch.setattr(drift_module, "detect_record_drift", fake_detect)

    _run_lifespan(server)

    assert len(scanned) == 2

    capped = [e for e in events if e[0] == "WARNING" and "capped at 2" in e[2]]
    assert capped, events
    assert "inspected 2 record(s)" in capped[0][2]
    assert "skipped 1 record(s)" in capped[0][2]


def test_boot_drift_scan_exception_does_not_break_startup(monkeypatch, vault):
    """A raising detector is logged and startup still completes."""
    server = _load_server()
    ws = "ws-drift-boom"
    _make_container_record(ws, "cid-boom", vault)

    _install_fake_docker(monkeypatch, _FakeClient([]))
    _neutralise_sweeps(monkeypatch, server)
    events = _record_log(monkeypatch, server)

    from thoughtmachine.container_record import drift as drift_module

    def boom(record, containers, *, permissions=None, capabilities=None):
        raise RuntimeError("drift detector exploded")

    monkeypatch.setattr(drift_module, "detect_record_drift", boom)

    _run_lifespan(server)  # must not raise

    warned = [
        e
        for e in events
        if e[0] == "WARNING" and "Startup container drift scan skipped:" in e[2]
    ]
    assert warned, events
    # Startup continued past the failure (later lifecycle steps still ran).
    assert any("sweep scheduled" in e[2] for e in events), events


def test_lifespan_boot_drift_scan_threads_capabilities_and_permissions(
    monkeypatch, vault
):
    """Every boot-scan call must carry real capabilities + a permissions map.

    Without them ``resolve_container_config`` fails (``capabilities_required``
    / ``bad_permissions``) and the policy axis of drift detection is silently
    suppressed.  The scan must therefore thread the live workspace
    capabilities (never ``None``) and a ``permissions`` mapping through to
    ``detect_record_drift``.
    """
    server = _load_server()
    ws = "ws-drift-axes"
    _make_container_record(ws, "cid-a", vault)
    _make_container_record(ws, "cid-b", vault)

    _install_fake_docker(monkeypatch, _FakeClient([]))
    _neutralise_sweeps(monkeypatch, server)
    _record_log(monkeypatch, server)

    from thoughtmachine.container_record import drift as drift_module

    calls = []

    def fake_detect(record, containers, *, permissions=None, capabilities=None):
        calls.append(
            {
                "record": record.id,
                "permissions": permissions,
                "capabilities": capabilities,
            }
        )
        return []

    monkeypatch.setattr(drift_module, "detect_record_drift", fake_detect)

    _run_lifespan(server)

    assert calls, "the boot scan never invoked detect_record_drift"
    assert all(c["capabilities"] is not None for c in calls)
    assert all(c["permissions"] is not None for c in calls)


def test_boot_drift_scan_loads_capabilities_once_per_workspace(
    monkeypatch, vault
):
    """Capabilities are looked up at most once per unique workspace per boot."""
    server = _load_server()
    ws_a = "ws-cache-a"
    ws_b = "ws-cache-b"
    _make_container_record(ws_a, "cid-a1", vault)
    _make_container_record(ws_a, "cid-a2", vault)
    _make_container_record(ws_b, "cid-b1", vault)

    _install_fake_docker(monkeypatch, _FakeClient([]))
    _neutralise_sweeps(monkeypatch, server)
    _record_log(monkeypatch, server)

    from thoughtmachine import workspace_capabilities as caps_module
    from thoughtmachine.container_record import drift as drift_module

    load_calls = []

    def counting_load(workspace_id):
        load_calls.append(workspace_id)
        return None  # force the default-capabilities fallback

    # The source imports ``load_workspace_capabilities`` lazily (function-local)
    # so patching the module attribute is what the running code observes.
    monkeypatch.setattr(caps_module, "load_workspace_capabilities", counting_load)

    scanned = []

    def fake_detect(record, containers, *, permissions=None, capabilities=None):
        scanned.append(record.id)
        return []

    monkeypatch.setattr(drift_module, "detect_record_drift", fake_detect)

    _run_lifespan(server)

    assert len(scanned) == 3, scanned
    # Exactly once per unique container-bearing workspace (never once per
    # record): the two records in ``ws_a`` share a single lookup.
    assert set(load_calls) == {ws_a, ws_b}, load_calls
    assert load_calls.count(ws_a) == 1, load_calls
    assert load_calls.count(ws_b) == 1, load_calls


# ── Boot-scan lifecycle (stuck-``creating``) emission ───────────────────────
#
# After the two in-memory container listings (the detector's and the
# lifecycle's, both served by the one startup shim), the boot scan appends the
# ``drift.container_stuck_creating`` finding for any record stuck in
# ``creating``.  These tests drive the REAL lifespan and read the emitted
# event back out of the record's own log.
#
# ``detect_record_drift`` is stubbed to ``[]`` so ONLY the lifecycle axis can
# emit; ``live_state_for_record`` is stubbed to pin the live report the boot
# scan feeds ``creating_drift_finding``.


def _no_findings(record, containers, *, permissions=None, capabilities=None):
    """Detector stub: the boot scan's non-lifecycle axes come back empty."""
    return []


def _make_stated_record(ws, docker_id, vault, state):
    """A record whose recorded ``state`` is set explicitly (creating/running)."""
    rec = cr_api.begin_record(
        ws, "persistent", "workspace-owned", vault_root=str(vault)
    )
    return cr_api.update_record(
        ws, rec.id, vault_root=vault, docker_id=docker_id, state=state
    )


def _backdate_created_at(ws, record_id, vault, seconds_ago):
    """Rewrite ``created_at`` on disk (``update_record`` forbids changing it)."""
    path = storage.record_path(ws, record_id, vault)
    data = storage.read_record_file(path)
    data["created_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    ).isoformat()
    storage.write_record_file(path, data)


def _stuck_creating_events(ws, record_id, vault, drift_module):
    return [
        e
        for e in cr_api.read_event_log(ws, record_id, vault_root=vault)
        if e.get("event_type") == drift_module.EVENT_CONTAINER_STUCK_CREATING
    ]


def test_boot_scan_emits_stuck_creating_when_live_contradicts(monkeypatch, vault):
    """A ``creating`` record with a live non-creating container is stuck.

    The boot scan must append exactly one ``drift.container_stuck_creating``
    event whose payload records the lifecycle class, ``expected="creating"``,
    the normalised live state as ``actual`` and ``fresh=False``.
    """
    server = _load_server()
    ws = "ws-lifecycle-running"
    rec = _make_stated_record(ws, "cid-live", vault, "creating")

    _install_fake_docker(monkeypatch, _FakeClient([]))
    _neutralise_sweeps(monkeypatch, server)
    _record_log(monkeypatch, server)

    from thoughtmachine.container_record import drift as drift_module

    monkeypatch.setattr(drift_module, "detect_record_drift", _no_findings)
    monkeypatch.setattr(
        drift_module,
        "live_state_for_record",
        lambda record, containers, **kw: "running",
    )

    _run_lifespan(server)

    stuck = _stuck_creating_events(ws, rec.id, vault, drift_module)
    assert len(stuck) == 1, stuck
    payload = stuck[0]["payload"]
    assert payload["class"] == drift_module.CLASS_LIFECYCLE
    assert payload["expected"] == "creating"
    assert payload["actual"] == "running"
    assert payload["fresh"] is False
    assert payload["signature"] == drift_module.signature_for(
        drift_module.EVENT_CONTAINER_STUCK_CREATING, "creating", "running"
    )
    # The contradiction cause must NOT collide with the absent cause's digest,
    # or the two would silently suppress one another in the dedup gate.
    assert payload["signature"] != drift_module.signature_for(
        drift_module.EVENT_CONTAINER_STUCK_CREATING, "creating", ""
    )


def test_boot_scan_emits_stuck_creating_when_absent_past_expiry(monkeypatch, vault):
    """A ``creating`` record past its window with no container is stuck.

    ``actual`` is the empty string for the absent cause (distinct from the
    contradiction cause) and ``fresh`` is ``False``.
    """
    server = _load_server()
    ws = "ws-lifecycle-absent"
    rec = _make_stated_record(ws, "cid-gone", vault, "creating")
    _backdate_created_at(ws, rec.id, vault, 960)  # > CREATING_EXPIRY_SECONDS

    _install_fake_docker(monkeypatch, _FakeClient([]))
    _neutralise_sweeps(monkeypatch, server)
    _record_log(monkeypatch, server)

    from thoughtmachine.container_record import drift as drift_module

    monkeypatch.setattr(drift_module, "detect_record_drift", _no_findings)
    monkeypatch.setattr(
        drift_module,
        "live_state_for_record",
        lambda record, containers, **kw: None,
    )

    _run_lifespan(server)

    stuck = _stuck_creating_events(ws, rec.id, vault, drift_module)
    assert len(stuck) == 1, stuck
    payload = stuck[0]["payload"]
    assert payload["class"] == drift_module.CLASS_LIFECYCLE
    assert payload["expected"] == "creating"
    assert payload["actual"] == ""
    assert payload["fresh"] is False
    assert payload["signature"] == drift_module.signature_for(
        drift_module.EVENT_CONTAINER_STUCK_CREATING, "creating", ""
    )


@pytest.mark.parametrize(
    "recorded_state, live_state, backdate_seconds",
    [
        ("creating", "created", 0),  # live container still creating → fresh
        ("creating", None, 0),  # absent but inside the window → fresh
        ("running", None, 960),  # resolves to running, not a lifecycle case
    ],
)
def test_boot_scan_emits_no_lifecycle_finding_when_not_stale(
    monkeypatch, vault, recorded_state, live_state, backdate_seconds
):
    """No false positives: a fresh (or non-``creating``) record emits nothing."""
    server = _load_server()
    ws = "ws-lifecycle-fresh"
    rec = _make_stated_record(ws, "cid-x", vault, recorded_state)
    if backdate_seconds:
        _backdate_created_at(ws, rec.id, vault, backdate_seconds)

    _install_fake_docker(monkeypatch, _FakeClient([]))
    _neutralise_sweeps(monkeypatch, server)
    _record_log(monkeypatch, server)

    from thoughtmachine.container_record import drift as drift_module

    monkeypatch.setattr(drift_module, "detect_record_drift", _no_findings)
    monkeypatch.setattr(
        drift_module,
        "live_state_for_record",
        lambda record, containers, **kw: live_state,
    )

    _run_lifespan(server)

    assert _stuck_creating_events(ws, rec.id, vault, drift_module) == []
