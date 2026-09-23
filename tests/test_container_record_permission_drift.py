"""Tests for container-record *permission* drift + schema/permission migration.

These tests cover the (proposed) v4 -> v5 record-schema evolution that records
the container's create-time permission grants on the record itself and reports
drift when those recorded grants differ from the current effective grants:

* ``models.SCHEMA_VERSION_CURRENT`` is bumped to ``5``.
* ``Record`` carries a ``permissions`` snapshot (raw create-time grants dict,
  default ``None`` == UNKNOWN: "this record predates the field").  An empty
  dict is NOT the unknown marker.  It round-trips through ``to_dict`` /
  ``from_dict`` and is a member of ``SCHEMA_FIELD_NAMES`` (so ``update_record``
  accepts it).  ``from_dict`` is lenient: absent / blank / empty-dict on disk
  all normalise to ``None`` (UNKNOWN).
* ``migration.migrate_records`` upgrades existing on-disk v4 records to v5
  (schema_version 4 -> 5, ``permissions`` left UNKNOWN == ``None``), idempotently.
* ``drift`` gains a ``CLASS_PERMISSION`` axis / ``EVENT_PERMISSION_CHANGED``
  event: the *expected* value is the record's stored ``permissions`` snapshot,
  the *actual* value is the CURRENT effective six-category grants resolved by
  the SSOT (``security.security_gate.resolve_container_config`` provides
  ``ContainerConfig.effective``).  The axis is quiet when the two are equal, and
  is compared ONLY when BOTH sides are known: the recorded snapshot is not
  ``None`` AND the current resolved profile is not the framework-default
  ("unknown") profile.

NOTE: the charter for this feature was unavailable when these tests were
authored; the API names above (``permissions`` field, ``migration.migrate_records``,
``drift.CLASS_PERMISSION`` / ``drift.EVENT_PERMISSION_CHANGED``) are the assumed
contract.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent.config.defaults import CONTAINER_NAME_LABEL, CONTAINER_TYPE_LABEL
from infra.container_manager import ContainerManager
import security.security_gate as security_gate
from security.security_gate import resolve_container_config
from thoughtmachine.container_record import (
    LIFECYCLE_EPHEMERAL,
    LIFECYCLE_PERSISTENT,
    RECORD_LABEL_KEY,
    api,
    drift,
    migration,
    models,
    storage,
)
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities

WS = "ws-1"

#: Six-key effective-permission grants (``security.get_effective_permissions``).
_PERMISSION_KEYS = (
    "filesystem",
    "network",
    "container",
    "git",
    "mcp",
    "host_bash",
)

#: Framework-default profile == "current side UNKNOWN" (what ``permissions={}``
#: resolves to).  See thoughtmachine.security.SessionPermissions defaults:
#: container=False, network='banned', filesystem='read', git='read',
#: mcp='banned', host_bash='banned'.
UNKNOWN_PERMS = {
    "filesystem": "read",
    "network": "banned",
    "container": False,
    "git": "read",
    "mcp": "banned",
    "host_bash": "banned",
}

#: Valid SessionPermissions kwargs (values MUST be legal levels/bool so the SSOT
#: resolver ``resolve_container_config`` can build a ContainerConfig).  Recorded
#: on the record at create time -- the WIDER profile.
STORED_PERMS = {
    "filesystem": "write",
    "network": "write",
    "container": True,
    "git": "write",
    "mcp": "connect",
    # The resolver force-collapses host_bash to "banned", so a stored snapshot
    # must be in canonical (already-resolved) form to compare byte-for-byte
    # against ``config.effective``.
    "host_bash": "banned",
}

#: The same six keys, later NARROWED (still not the all-default unknown profile).
CURRENT_PERMS = {
    "filesystem": "read",
    "network": "write",
    "container": True,
    "git": "read",
    "mcp": "banned",
    "host_bash": "banned",
}


# -- Fixtures -----------------------------------------------------------------


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(root))
    return root


@pytest.fixture
def caps():
    return WorkspaceCapabilities()


@pytest.fixture
def permissive_caps():
    """Fully-permissive caps so the resolved effective dict == the input grants."""
    return WorkspaceCapabilities(
        filesystem_write=True,
        allow_docker=True,
        allow_network=True,
        git_available=True,
    )


# -- Fakes / helpers (mirrors tests/test_container_record_drift.py) ------------


class FakeContainer:
    """Duck-typed Docker container (read-only surface only)."""

    def __init__(self, cid, attrs, labels=None):
        self.id = cid
        self.attrs = attrs
        self.labels = labels or {}


class FakeContainers:
    """Duck-typed Docker client exposing ``list(all=...)``."""

    def __init__(self, items=None, list_error=None):
        self._items = list(items or [])
        self._list_error = list_error

    def list(self, all=False):
        if self._list_error is not None:
            raise self._list_error
        return list(self._items)


def _record(**overrides):
    """Build a minimal record-like object for the pure classifier."""
    base = {
        "id": "rec-1",
        "docker_id": "",
        "intent_snapshot": {},
        "inferred": False,
        "lifecycle_class": "ephemeral",
        "permissions": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _attrs(*, image="python:3.12", image_hash="sha256:deadbeef", memory=536870912, network="none"):
    """Build a Docker inspect ``attrs`` payload (shape consumed by snapshot.py)."""
    return {
        "HostConfig": {"NetworkMode": network, "Memory": memory, "OomScoreAdj": 0},
        "Config": {"Image": image},
        "Image": image_hash,
        "Mounts": [{"Destination": "/workspace", "RW": True}],
    }


def _live(container_id="d1"):
    return FakeContainers([FakeContainer(container_id, _attrs(network="none"))])


def _perm_findings(findings):
    return [f for f in findings if f.drift_class == drift.CLASS_PERMISSION]


def _assert_axis_engaged(caps):
    """Positive control: a genuinely-different pair MUST fire the axis.

    Guards every 'silent' test against passing vacuously (feature absent).
    """
    findings = drift.detect_record_drift(
        _record(docker_id="d1", permissions=dict(STORED_PERMS)),
        _live(),
        permissions=dict(CURRENT_PERMS),
        capabilities=caps,
    )
    assert [f.drift_class for f in findings] == [drift.CLASS_PERMISSION]
    assert findings[0].event_type == drift.EVENT_PERMISSION_CHANGED


# -- 0. Category set ----------------------------------------------------------


def test_permission_categories_match_resolver_output(caps):
    config = resolve_container_config({}, caps, "ephemeral")
    assert set(config.effective) == set(_PERMISSION_KEYS)


# -- 1. Schema version --------------------------------------------------------


def test_schema_version_current_is_five():
    assert models.SCHEMA_VERSION_CURRENT == 5


# -- 2. Record carries the permissions snapshot -------------------------------


def test_record_carries_permissions_snapshot_roundtrip():
    rec = models.Record(
        id="r1",
        lifecycle_class="ephemeral",
        owner="workspace-owned",
        permissions=dict(STORED_PERMS),
    )
    assert hasattr(rec, "permissions")
    assert rec.permissions == STORED_PERMS

    data = rec.to_dict()
    assert "permissions" in data
    assert data["permissions"] == STORED_PERMS
    # New key appended LAST in the field-order list (stable key order).
    assert list(data.keys())[-1] == "permissions"

    back = models.Record.from_dict(data)
    assert back.permissions == STORED_PERMS
    assert back.to_dict()["permissions"] == STORED_PERMS

    # Default (a record that predates the field) is UNKNOWN == None, NOT {}.
    plain = models.Record(id="r2", lifecycle_class="ephemeral", owner="workspace-owned")
    assert plain.permissions is None
    assert plain.to_dict()["permissions"] is None
    assert models.Record.from_dict(
        {"id": "r3", "lifecycle_class": "ephemeral", "owner": "workspace-owned"}
    ).permissions is None
    # from_dict leniency: absent / blank / empty-dict on disk all normalise to
    # None (UNKNOWN), never to an empty grants dict.
    assert models.Record.from_dict(
        {"id": "r4", "lifecycle_class": "ephemeral", "owner": "workspace-owned",
         "permissions": {}}
    ).permissions is None
    assert models.Record.from_dict(
        {"id": "r5", "lifecycle_class": "ephemeral", "owner": "workspace-owned",
         "permissions": ""}
    ).permissions is None


# -- 3. v4 -> v5 migration ----------------------------------------------------


def _v4_record_dict(record_id):
    """Return a v4 on-disk record dict (17 keys, no ``permissions``)."""
    return {
        "id": record_id,
        "docker_id": "d-legacy",
        "lifecycle_class": "ephemeral",
        "owner": "workspace-owned",
        "purpose": "",
        "intent_snapshot": {},
        "notes": "",
        "event_log": [],
        "schema_version": 4,
        "inferred": False,
        "state": "",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "restart_policy": None,
        "name": "",
        "retention_days": None,
        "user": "",
    }


def test_v4_record_migrates_to_v5_with_unknown_permissions_and_is_idempotent(vault):
    record_id = "legacy-rec"
    storage.write_record_file(
        storage.record_path(WS, record_id, vault), _v4_record_dict(record_id)
    )
    assert "permissions" not in storage.read_record_file(
        storage.record_path(WS, record_id, vault)
    )

    result = migration.upgrade_record_schema(WS, vault_root=vault)
    assert isinstance(result, dict)

    loaded = api.load_record(WS, record_id, vault)
    assert loaded.schema_version == 5
    # A migrated legacy record is UNKNOWN (None), never empty, never defaults.
    assert loaded.permissions is None
    # Migration is additive: the pre-existing fields survive untouched.
    assert loaded.docker_id == "d-legacy"
    assert loaded.lifecycle_class == "ephemeral"

    # Idempotent: a second pass leaves the (now v5) record unchanged.
    second = migration.upgrade_record_schema(WS, vault_root=vault)
    assert isinstance(second, dict)
    reloaded = api.load_record(WS, record_id, vault)
    assert reloaded.schema_version == 5
    assert reloaded.permissions is None


# -- 4/5. Permission drift axis ----------------------------------------------


def test_detect_record_drift_narrowing_fires(permissive_caps):
    """``finding.actual == CURRENT_PERMS`` is byte-exact only under the
    fully-permissive-cap fixture with no forced values (``host_bash`` is
    force-``banned``)."""
    record = _record(docker_id="d1", permissions=dict(STORED_PERMS))
    findings = drift.detect_record_drift(
        record, _live(), permissions=dict(CURRENT_PERMS), capabilities=permissive_caps
    )
    assert [f.drift_class for f in findings] == [drift.CLASS_PERMISSION]
    finding = findings[0]
    assert finding.event_type == drift.EVENT_PERMISSION_CHANGED
    assert finding.expected == STORED_PERMS
    assert finding.actual == CURRENT_PERMS
    assert finding.signature == drift.signature_for(
        drift.EVENT_PERMISSION_CHANGED, STORED_PERMS, CURRENT_PERMS
    )


def test_detect_record_drift_widening_fires(permissive_caps):
    """``finding.actual == STORED_PERMS`` is byte-exact only under the
    fully-permissive-cap fixture with no forced values (``host_bash`` is
    force-``banned``)."""
    record = _record(docker_id="d1", permissions=dict(CURRENT_PERMS))
    findings = drift.detect_record_drift(
        record, _live(), permissions=dict(STORED_PERMS), capabilities=permissive_caps
    )
    assert [f.drift_class for f in findings] == [drift.CLASS_PERMISSION]
    assert findings[0].event_type == drift.EVENT_PERMISSION_CHANGED
    assert findings[0].expected == CURRENT_PERMS
    assert findings[0].actual == STORED_PERMS


def test_detect_record_drift_quiet_when_match(permissive_caps):
    _assert_axis_engaged(permissive_caps)  # non-vacuous: axis exists and CAN fire
    record = _record(docker_id="d1", permissions=dict(STORED_PERMS))
    findings = drift.detect_record_drift(
        record, _live(), permissions=dict(STORED_PERMS), capabilities=permissive_caps
    )
    assert findings == []


def test_detect_record_drift_quiet_when_current_unknown(caps, permissive_caps):
    _assert_axis_engaged(permissive_caps)
    # CURRENT side == framework-default ("unknown", e.g. permissions={} at boot).
    record = _record(docker_id="d1", permissions=dict(STORED_PERMS))
    findings = drift.detect_record_drift(
        record, _live(), permissions={}, capabilities=caps
    )
    assert findings == []


def test_detect_record_drift_quiet_when_recorded_unknown(permissive_caps):
    _assert_axis_engaged(permissive_caps)
    # RECORDED side UNKNOWN (None): a record that predates the field.
    record = _record(docker_id="d1", permissions=None)
    findings = drift.detect_record_drift(
        record, _live(), permissions=dict(CURRENT_PERMS), capabilities=permissive_caps
    )
    assert findings == []


# -- 6. End-to-end scan emits the permission-drift event ----------------------


def test_scan_record_emits_permission_drift_event(vault, permissive_caps):
    rec = api.create_record(WS, "ephemeral", "workspace-owned", vault_root=vault)
    api.update_record(
        WS, rec.id, vault_root=vault, docker_id="d1", permissions=dict(STORED_PERMS)
    )
    rec = api.load_record(WS, rec.id, vault)
    assert rec.permissions == STORED_PERMS

    out = drift.scan_record(
        rec,
        _live(),
        workspace_id=WS,
        permissions=dict(CURRENT_PERMS),
        capabilities=permissive_caps,
        vault_root=vault,
    )

    assert any(f.drift_class == drift.CLASS_PERMISSION for f in out)
    assert any(f.event_type == drift.EVENT_PERMISSION_CHANGED for f in out)

    log = api.read_event_log(WS, rec.id, vault_root=vault)
    permission_events = [
        e for e in log if e.get("event_type") == drift.EVENT_PERMISSION_CHANGED
    ]
    assert len(permission_events) == 1
    assert permission_events[0]["payload"]["expected"] == STORED_PERMS
    assert permission_events[0]["payload"]["actual"] == CURRENT_PERMS


# ============================================================================
# 7. ContainerManager.start records the EFFECTIVE (merged) grants
# ============================================================================
#
# These tests pin the value written to ``record.permissions`` by the live
# create path: it must be the SSOT-resolved EFFECTIVE six-key profile
# (``resolve_container_config(...).effective``), NOT the raw caller grants.
# The discriminating pair below differs ONLY on ``host_bash``: the caller grants
# ``"allow"`` but the resolver force-collapses it to ``"banned"``, so a test
# that merely echoed the raw grants would still look "wired" while a test that
# checks the effective profile can tell them apart.

#: Raw caller grants that DISCRIMINATE from their effective profile: the
#: resolver force-collapses ``host_bash`` from ``"allow"`` -> ``"banned"``.
_GRANTS = {
    "filesystem": "write",
    "network": "write",
    "container": True,
    "git": "write",
    "mcp": "connect",
    "host_bash": "allow",
}


class _RunCtr:
    """Minimal live container returned by ``containers.run``."""

    def __init__(self, container_id, *, name="ctr", labels=None, attrs=None):
        self.id = container_id
        self.name = name
        self.status = "created"
        self.labels = dict(labels or {})
        self.attrs = {"State": {"Status": "created"}} if attrs is None else attrs

    def start(self):
        self.status = "running"

    def stop(self, timeout=None):
        self.status = "exited"

    def remove(self, **kwargs):
        self.status = "removed"

    def reload(self):
        pass

    def exec_run(self, *args, **kwargs):
        return (0, (b"", b""))


class _RunContainers:
    """Duck-typed ``client.containers`` capturing ``run`` kwargs."""

    def __init__(self):
        self.containers = []
        self.run_calls = []

    def list(self, all=False, filters=None):
        return list(self.containers)

    def get(self, container_id):
        for ctr in self.containers:
            if ctr.id == container_id or ctr.name == container_id:
                return ctr
        raise KeyError(container_id)

    def run(self, *args, **kwargs):
        self.run_calls.append({"args": args, "kwargs": kwargs})
        ctr = _RunCtr(
            "c-run-1",
            name=kwargs.get("name") or "run-ctr",
            labels=kwargs.get("labels") or {},
        )
        self.containers.append(ctr)
        return ctr


class _RunDockerClient:
    """Duck-typed docker client whose ``containers`` captures ``run``."""

    def __init__(self):
        self.containers = _RunContainers()

    def ping(self):
        return True


def _make_start_manager(
    monkeypatch, permissive_caps, *, workspace_id=WS, session_id="s1", grants=None
):
    """Build a ``ContainerManager`` wired to a capture-only docker client.

    The REAL ``_compute_config`` path runs (nothing is stubbed); only the SSOT
    capability lookup is pinned to *permissive_caps* so the resolved profile is
    deterministic regardless of on-disk workspace state.
    """
    monkeypatch.setattr(
        security_gate, "get_workspace_capabilities", lambda ws: permissive_caps
    )
    client = _RunDockerClient()
    cm = ContainerManager.__new__(ContainerManager)
    cm.workspace_path = "/tmp/ws-test"
    cm.session_id = session_id
    cm.workspace_id = workspace_id
    cm.session_permissions = dict(grants or {})
    cm._session_config = {"use_container_registry": False}
    cm.image = "agent-executor"
    cm.mem_limit = "1g"
    cm.cpu_quota = 100000
    cm._containers = {}
    cm.client = client
    cm.vault_root = None
    cm.container_notes = {}
    cm.max_containers = 4
    cm.workspace_config = {"max_containers": 4}
    cm.dockerfile_path = None
    return cm, client


def test_start_records_effective_merge_not_raw_grants(
    vault, monkeypatch, permissive_caps
):
    """``record.permissions`` must be the resolved EFFECTIVE profile.

    RED against the pre-change tree: ``record.permissions`` is ``None``
    (nothing is recorded yet), so the canonical-key assertions below fail with
    an AssertionError -- NOT a TypeError / collection error.
    """
    expected = resolve_container_config(
        _GRANTS, permissive_caps, LIFECYCLE_PERSISTENT
    ).effective

    # GUARD: the fixture pair MUST discriminate, else this test is vacuous.
    assert expected != _GRANTS, (
        "discriminating grants do not discriminate: "
        f"effective == raw ({_GRANTS!r})"
    )

    cm, client = _make_start_manager(monkeypatch, permissive_caps, grants=_GRANTS)
    result = cm.start(image="agent-executor", name="agent-perm-t1")
    assert "error" not in result, result

    run_kwargs = client.containers.run_calls[-1]["kwargs"]
    record_id = run_kwargs["labels"][api.RECORD_LABEL_KEY]
    record = api.load_record(WS, record_id, vault)

    assert record.permissions is not None, (
        "record.permissions was not populated by the live create path; "
        f"expected the effective profile {expected!r}"
    )
    assert set(record.permissions) == set(_PERMISSION_KEYS)
    for key in _PERMISSION_KEYS:
        assert record.permissions[key] == expected[key], (
            key,
            record.permissions[key],
            expected[key],
        )


def test_record_created_without_permissions_has_none(vault):
    """PIN (green before + after): an omitted / explicit-None ``permissions``
    kwarg leaves the record field UNKNOWN (``None``) -- an empty dict is NOT
    the unknown marker, so ``None`` must survive the round-trip."""
    rec = api.create_record(WS, "ephemeral", "workspace-owned", vault_root=vault)
    rec2 = api.create_record(
        WS, "ephemeral", "workspace-owned", vault_root=vault, permissions=None
    )

    assert api.load_record(WS, rec.id, vault).permissions is None
    assert api.load_record(WS, rec2.id, vault).permissions is None


def test_boot_shape_empty_permissions_is_silent_on_the_axis(vault, permissive_caps):
    """PIN (green before + after): the boot call shape (``permissions={}`` +
    loaded caps) resolves to the framework-default profile, which reads as
    "current side UNKNOWN" -> the permission axis is silent.  A positive
    control confirms the axis really fires for a genuine difference."""
    from thoughtmachine.workspace_capabilities import load_workspace_capabilities

    rec = api.create_record(WS, "ephemeral", "workspace-owned", vault_root=vault)
    api.update_record(
        WS, rec.id, vault_root=vault, docker_id="d1", permissions=dict(STORED_PERMS)
    )
    record = api.load_record(WS, rec.id, vault)

    boot_caps = load_workspace_capabilities(WS) or WorkspaceCapabilities.default()
    silent = drift.scan_record(
        record,
        _live(),
        workspace_id=WS,
        permissions={},
        capabilities=boot_caps,
        vault_root=vault,
    )
    assert [f for f in silent if f.drift_class == drift.CLASS_PERMISSION] == []

    control = drift.scan_record(
        record,
        _live(),
        workspace_id=WS,
        permissions=dict(CURRENT_PERMS),
        capabilities=permissive_caps,
        vault_root=vault,
    )
    assert [
        f.drift_class for f in control if f.drift_class == drift.CLASS_PERMISSION
    ] == [drift.CLASS_PERMISSION]


def test_start_create_arguments_unchanged(vault, monkeypatch, permissive_caps):
    """PIN: recording the effective profile must not perturb any OTHER create
    argument.  Every value except ``labels`` is byte-identical to the observed
    call; the label delta is exactly the record-id label."""
    name = "agent-perm-t4"
    expected = {
        "cap_drop": ["ALL"],
        "cpu_quota": 100000,
        "detach": True,
        "environment": {
            "PYTHONUSERBASE": "/home/agent/.local",
            "THOUGHTMACHINE_SESSION_ID": "s1",
            "THOUGHTMACHINE_WORKSPACE_ID": WS,
        },
        "extra_hosts": {},
        "mem_limit": "1g",
        "mounts": [
            {
                "Target": "/workspace",
                "Source": "/tmp/ws-test",
                "Type": "bind",
                "ReadOnly": False,
            },
            {
                "Target": "/home/agent/.local",
                "Source": f"tm-packages-{WS}",
                "Type": "volume",
                "ReadOnly": False,
            },
        ],
        "name": name,
        "network_mode": "bridge",
        "oom_score_adj": 1000,
        "read_only": True,
        "restart_policy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
        "security_opt": ["no-new-privileges:true"],
        "stdin_open": True,
        "tmpfs": {
            "/tmp": "rw,noexec,nosuid,size=64m",
            "/home/agent": "rw,exec,size=256M,uid=1000,gid=1000",
        },
        "tty": True,
        "user": "1000:1000",
    }

    cm, client = _make_start_manager(monkeypatch, permissive_caps, grants=_GRANTS)
    cm.start(image="agent-executor", name=name)

    run_kwargs = client.containers.run_calls[-1]["kwargs"]
    assert set(run_kwargs) == set(expected) | {"labels"}

    labels = run_kwargs["labels"]
    assert set(labels) == {
        CONTAINER_NAME_LABEL,
        "thoughtmachine.workspace_id",
        CONTAINER_TYPE_LABEL,
        api.RECORD_LABEL_KEY,
    }
    pre_record = {
        CONTAINER_NAME_LABEL: name,
        "thoughtmachine.workspace_id": WS,
        CONTAINER_TYPE_LABEL: "free_use",
    }
    for key, value in pre_record.items():
        assert labels[key] == value
    assert set(labels) - set(pre_record) == {api.RECORD_LABEL_KEY}

    for key, value in expected.items():
        assert run_kwargs[key] == value, (key, run_kwargs[key], value)
