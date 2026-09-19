"""Schema-v3 container identity: the RECORD ``name`` field + record-first start.

Pins the ``feat/registry-transition`` behaviour of ``infra/container_manager``
and the record schema:

  1. Schema v3 adds ``name`` (workspace-scoped container identity) to the record
     field set; v4 appends ``retention_days``; ``SCHEMA_VERSION_CURRENT == 4``;
     a native record round-trips the
     name (present -> kept, absent -> ``""``), and the legacy migration
     ``_materialise`` NEVER synthesises one.
  2. The record-first identity ladder: the module-scoped ``(workspace, name) ->
     record id`` index is built ONCE per workspace from ``list_records`` and is
     WORKSPACE-scoped (the same name in two workspaces never collides).
  3. ``start(name=...)`` resolves identity from the RECORD (index -> record ->
     ``docker_id`` -> live container).  The old ``_find_by_labels`` lookup is NOT
     on that identity path.  Ambiguity (>=2 records share a name) REFUSES with a
     WARNING; a record with no ``docker_id`` REFUSES; a cached name with no
     record REFUSES (drift).  A genuinely fresh name still creates.

The manager is built via ``ContainerManager.__new__`` (no Docker) and every
daemon touchpoint (``_compute_config``, ``list_containers``, ``_find_by_labels``,
``_start_drift_decision``) is stubbed, mirroring ``tests/test_container_start_drift.py``.

Non-vacuity anchors (mutate the branch, watch the pinned test FAIL):
  * ``if self._name_collision(name):`` -> ``if False:`` fails the collision test.
  * ``if not docker_id:``             -> ``if False:`` fails the unbound-record test.
"""

from unittest.mock import MagicMock

import pytest

import infra.container_manager as _cm
import thoughtmachine.container_record.migration as migration
from agent.config.defaults import CONTAINER_NAME_LABEL
from infra.container_manager import ContainerManager
from thoughtmachine.container_record import (
    LIFECYCLE_PERSISTENT,
    OWNER_WORKSPACE,
    RECORD_LABEL_KEY,
    SCHEMA_FIELD_NAMES,
    SCHEMA_VERSION_CURRENT,
    SCHEMA_VERSION_LEGACY,
    Record,
    create_record,
    load_record,
    update_record,
)


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class _FakeImageRef:
    def __init__(self, ref="sha256:deadbeef"):
        self.id = ref

    def __str__(self):
        return self.id


class _FakeContainer:
    def __init__(self, container_id, name=None, labels=None, status="running"):
        self.id = container_id
        self.name = name or container_id
        self.image = _FakeImageRef()
        self.status = status
        self.labels = dict(labels or {})
        self.removed = []

    def reload(self):
        pass

    def start(self):
        self.status = "running"

    def remove(self, **kwargs):
        self.removed.append(kwargs)


class _FakeContainers:
    """``client.containers`` stand-in: ``.get(id_or_name) -> container``."""

    def __init__(self, containers):
        self._containers = list(containers)

    def get(self, container_id):
        for c in self._containers:
            if c.id == container_id or c.name == container_id:
                return c
        raise LookupError(container_id)

    def list(self, all=False, filters=None):  # noqa: A002 - docker signature
        label = (filters or {}).get("label")
        if label is None:
            return list(self._containers)
        if isinstance(label, str):
            label = [label]
        out = []
        for c in self._containers:
            labels = dict(getattr(c, "labels", None) or {})
            if all(
                labels.get(spec.partition("=")[0]) == spec.partition("=")[2]
                for spec in label
            ):
                out.append(c)
        return out


def _client(containers=None):
    return type(
        "_C",
        (),
        {"containers": _FakeContainers(containers or []), "ping": lambda self: True},
    )()


def _make_manager(workspace_id, vault_root, client, **overrides):
    """Build a ContainerManager with every daemon touchpoint stubbed."""
    cm = ContainerManager.__new__(ContainerManager)
    cm.workspace_path = "/tmp/tm-reg-transition-ws"
    cm.session_id = "sess-reg-transition"
    cm.workspace_id = workspace_id
    cm.vault_root = str(vault_root)
    cm.session_permissions = {}
    cm.image = "agent-executor"
    cm.mem_limit = "512m"
    cm.cpu_quota = 50000
    cm._containers = {}
    cm.container_notes = {}
    cm.workspace_config = {"disk_quota_mb": 0}
    cm.max_containers = 4
    cm.client = client
    cm._session_config = None
    cm._compute_config = lambda *a, **k: ("none", "ro")
    cm._get_max_containers = lambda: 10
    cm.list_containers = lambda: []
    # The identity ladder must NEVER fall back to label discovery.
    cm._find_by_labels = MagicMock(
        side_effect=AssertionError("_find_by_labels on the identity path"))
    cm._start_drift_decision = lambda *a, **k: ("ok", None)
    cm._remove_container = MagicMock()
    for key, value in overrides.items():
        setattr(cm, key, value)
    return cm


def _mint(workspace_id, vault_root, rid, name="", docker_id=None):
    rec = create_record(
        workspace_id, LIFECYCLE_PERSISTENT, OWNER_WORKSPACE,
        id=rid, name=name, vault_root=str(vault_root),
    )
    if docker_id is not None:
        update_record(workspace_id, rid, vault_root=str(vault_root), docker_id=docker_id)
    return rec.id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _default_vault_is_tmp(tmp_path, monkeypatch):
    """Point the DEFAULT vault resolution at this test's tmp vault."""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))
    return vault


@pytest.fixture(autouse=True)
def _reset_module_memos():
    """The name-index / notes memos are module-scoped; isolate every test."""
    _cm._NAME_INDEX.clear()
    _cm._NAME_INDEX_COLLISIONS.clear()
    _cm._NAME_INDEX_BUILT.clear()
    _cm._NAME_MIGRATED.clear()
    _cm._NOTES_MIGRATED.clear()
    _cm._NOTES_WARNED.clear()
    yield
    _cm._NAME_INDEX.clear()
    _cm._NAME_INDEX_COLLISIONS.clear()
    _cm._NAME_INDEX_BUILT.clear()
    _cm._NAME_MIGRATED.clear()
    _cm._NOTES_MIGRATED.clear()
    _cm._NOTES_WARNED.clear()


# ---------------------------------------------------------------------------
# 1. Schema v3 -- model + round-trip
# ---------------------------------------------------------------------------


def test_schema_version_current_is_4():
    assert SCHEMA_VERSION_CURRENT == 4


def test_name_is_a_schema_field_and_user_is_last():
    assert "name" in SCHEMA_FIELD_NAMES
    assert "user" in SCHEMA_FIELD_NAMES
    assert SCHEMA_FIELD_NAMES[-1] == "user"


def test_record_round_trip_name_present(tmp_path):
    vault = tmp_path / "vault"
    rec = create_record(
        "ws-rt", LIFECYCLE_PERSISTENT, OWNER_WORKSPACE,
        id="rt-1", name="rt-name", vault_root=str(vault),
    )
    assert rec.schema_version == SCHEMA_VERSION_CURRENT
    assert rec.inferred is False
    assert rec.name == "rt-name"

    data = rec.to_dict()
    assert data["name"] == "rt-name"
    # Dict-level round trip preserves the field.
    assert Record.from_dict(data).name == "rt-name"

    reloaded = load_record("ws-rt", "rt-1", vault_root=str(vault))
    assert reloaded is not None and reloaded.name == "rt-name"


def test_record_round_trip_name_absent_defaults_empty(tmp_path):
    vault = tmp_path / "vault"
    rec = create_record(
        "ws-rt", LIFECYCLE_PERSISTENT, OWNER_WORKSPACE,
        id="rt-2", vault_root=str(vault),
    )
    assert rec.name == ""

    data = rec.to_dict()
    assert data["name"] == ""
    # A legacy payload with NO ``name`` key must read back as "".
    without = {k: v for k, v in data.items() if k != "name"}
    assert Record.from_dict(without).name == ""

    reloaded = load_record("ws-rt", "rt-2", vault_root=str(vault))
    assert reloaded is not None and reloaded.name == ""


def test_materialise_never_synthesises_a_name(tmp_path):
    """The legacy synthesis path leaves ``name`` UNSET (no invention)."""
    vault = tmp_path / "vault"
    migration._materialise(
        "ws-mat", "legacy-1", "docker-legacy", LIFECYCLE_PERSISTENT,
        OWNER_WORKSPACE, {}, "running", None, str(vault),
    )
    rec = load_record("ws-mat", "legacy-1", vault_root=str(vault))
    assert rec is not None
    assert rec.name == ""
    assert rec.schema_version == SCHEMA_VERSION_LEGACY


# ---------------------------------------------------------------------------
# 2. Name index -- build, update, workspace scoping
# ---------------------------------------------------------------------------


def test_name_index_built_from_list_records(tmp_path):
    vault = tmp_path / "vault"
    _mint("ws-idx", vault, "r1", name="alpha")
    _mint("ws-idx", vault, "r2", name="beta")

    cm = _make_manager("ws-idx", vault, _client())
    cm._ensure_name_index()

    assert cm._record_for_name("alpha") == "r1"
    assert cm._record_for_name("beta") == "r2"
    assert _cm._NAME_INDEX[("ws-idx", "alpha")] == "r1"
    assert cm._name_collision("alpha") is False


def test_name_index_updated_when_a_name_is_recorded(tmp_path):
    """``_name_index_add`` is the update primitive: a record added after the
    (empty) index was built becomes resolvable."""
    vault = tmp_path / "vault"
    cm = _make_manager("ws-idx", vault, _client())
    cm._ensure_name_index()
    assert cm._record_for_name("gamma") is None

    _mint("ws-idx", vault, "r3", name="gamma")
    cm._name_index_add(load_record("ws-idx", "r3"))
    assert cm._record_for_name("gamma") == "r3"


def test_name_index_is_workspace_scoped(tmp_path):
    """Same name in two workspaces -> two distinct identities, NO collision."""
    vault = tmp_path / "vault"
    _mint("ws-A", vault, "a1", name="shared")
    _mint("ws-B", vault, "b1", name="shared")

    cm_a = _make_manager("ws-A", vault, _client())
    cm_b = _make_manager("ws-B", vault, _client())
    cm_a._ensure_name_index()
    cm_b._ensure_name_index()

    assert cm_a._record_for_name("shared") == "a1"
    assert cm_b._record_for_name("shared") == "b1"
    assert cm_a._name_collision("shared") is False
    assert cm_b._name_collision("shared") is False
    assert _cm._NAME_INDEX_COLLISIONS == set()


# ---------------------------------------------------------------------------
# 3. start() -- record-first identity ladder
# ---------------------------------------------------------------------------


def test_start_reuses_via_record_docker_id(tmp_path):
    """(a) record + docker_id -> reuse the named container; cache warmed."""
    vault = tmp_path / "vault"
    _mint("ws-reuse", vault, "rec-1", name="agent-x", docker_id="c" * 16)
    ctr = _FakeContainer("c" * 16, name=CONTAINER_NAME_LABEL,
                         labels={RECORD_LABEL_KEY: "rec-1"})

    cm = _make_manager("ws-reuse", vault, _client([ctr]))
    result = cm.start(name="agent-x")

    assert result["status"] == "reused"
    assert result["id"] == "c" * 16
    assert cm._containers["agent-x"] == "c" * 16
    # The identity path never consulted the label lookup.
    assert cm._find_by_labels.called is False


def test_start_refuses_record_without_docker_id(tmp_path):
    """(b) a record with no bound container -> REFUSE (no daemon touch)."""
    vault = tmp_path / "vault"
    _mint("ws-unbound", vault, "rec-2", name="agent-y")  # docker_id stays ""

    cm = _make_manager("ws-unbound", vault, _client())
    result = cm.start(name="agent-y")

    assert result["code"] == "container_record_unbound"
    assert "error" in result and "status" not in result
    assert ("name.record_without_container", "ws-unbound", "agent-y") in _cm._NOTES_WARNED
    assert cm._find_by_labels.called is False


def test_start_refuses_cached_name_without_record(tmp_path):
    """(c) cached container with NO record -> REFUSE (drift); no create."""
    vault = tmp_path / "vault"
    cm = _make_manager("ws-cache", vault, _client())
    cm._containers = {"agent-z": "cid-stale"}

    result = cm.start(name="agent-z")

    assert result["code"] == "container_no_record"
    assert result["drift"]["reason"] == "cached container without a record"
    assert cm._containers["agent-z"] == "cid-stale"  # untouched, nothing created
    assert ("name.cache_without_record", "ws-cache", "agent-z") in _cm._NOTES_WARNED
    assert cm._find_by_labels.called is False


def test_start_refuses_ambiguous_name_and_warns(tmp_path):
    """(d) >=2 records share (workspace, name) -> REFUSE + WARNING."""
    vault = tmp_path / "vault"
    _mint("ws-dup", vault, "dup-1", name="dup")
    _mint("ws-dup", vault, "dup-2", name="dup")

    cm = _make_manager("ws-dup", vault, _client())
    result = cm.start(name="dup")

    assert result["code"] == "container_name_collision"
    assert "ambiguous" in result["error"]
    assert ("name.collision", "ws-dup", "dup") in _cm._NOTES_WARNED
    assert ("ws-dup", "dup") in _cm._NAME_INDEX_COLLISIONS
    assert cm._find_by_labels.called is False



def test_start_create_indexes_record_then_reuses(tmp_path):
    """A hermetic create -> index -> reuse cycle.

    The registry-inactive fresh create mints a record (``record_creation``) and
    the manager INDEXES it under the requested name; a second
    ``start(name=...)`` therefore resolves identity from that record and REUSES
    the container instead of creating a second one.
    """
    vault = tmp_path / "vault"

    class _RunContainer:
        def __init__(self, cid, labels):
            self.id = cid
            self.name = labels.get(CONTAINER_NAME_LABEL, cid)
            self.labels = dict(labels)
            self.image = _FakeImageRef()
            self.status = "running"
            self.attrs = {"State": {"Status": "running"}}
            self.removed = []

        def reload(self):
            pass

        def start(self):
            self.status = "running"

        def remove(self, **kwargs):
            self.removed.append(kwargs)

    class _Containers:
        def __init__(self):
            self.items = []
            self.run_calls = []

        def get(self, container_id):
            for c in self.items:
                if c.id == container_id or c.name == container_id:
                    return c
            raise LookupError(container_id)

        def list(self, all=False, filters=None):
            return list(self.items)

        def run(self, *args, **kwargs):
            self.run_calls.append({"args": args, "kwargs": kwargs})
            ctr = _RunContainer("cid-created-1", kwargs.get("labels") or {})
            self.items.append(ctr)
            return ctr

    client = type("_C", (), {"containers": _Containers(), "ping": lambda self: True})()
    cm = _make_manager(
        "ws-create", vault, client,
        _find_by_labels=MagicMock(return_value=None))

    first = cm.start(name="made")
    assert first["status"] == "created"
    assert first["id"] == "cid-created-1"
    assert len(client.containers.run_calls) == 1
    label_lookups_after_create = cm._find_by_labels.call_count

    # The fresh create INDEXED the freshly minted record under the name ...
    rec_id = cm._record_for_name("made")
    assert rec_id == _cm._NAME_INDEX[("ws-create", "made")]
    assert rec_id
    rec = load_record("ws-create", rec_id, vault_root=str(vault))
    assert rec is not None
    assert rec.name == "made"
    assert rec.docker_id == "cid-created-1"

    # ... so a second start() reuses it (NO second create).
    second = cm.start(name="made")
    assert second["status"] == "reused"
    assert second["id"] == "cid-created-1"
    assert len(client.containers.run_calls) == 1
    # The identity path resolved the record; it never fell back to label scan.
    assert cm._find_by_labels.call_count == label_lookups_after_create


# ---------------------------------------------------------------------------
# 4. Lazy v1/v2 -> v3 name backfill
# ---------------------------------------------------------------------------


def test_lazy_migration_backfills_name_from_label(tmp_path):
    vault = tmp_path / "vault"
    _mint("ws-mig", vault, "m1", docker_id="cid-1")  # name unset (legacy shape)

    cm = _make_manager("ws-mig", vault, _client())
    cm.list_containers = lambda: [
        {"container_id": "cid-1", "labels": {CONTAINER_NAME_LABEL: "from-label"}},
    ]
    cm._migrate_records_v3_once()

    rec = load_record("ws-mig", "m1", vault_root=str(vault))
    assert rec is not None and rec.name == "from-label"
    # The backfill also updates the index.
    assert cm._record_for_name("from-label") == "m1"


def test_lazy_migration_leaves_name_unset_when_unlabelled(tmp_path):
    vault = tmp_path / "vault"
    _mint("ws-mig", vault, "m2", docker_id="cid-2")

    cm = _make_manager("ws-mig", vault, _client())
    # Container present but carries NO name label -> leave UNSET (no synthesis).
    cm.list_containers = lambda: [{"container_id": "cid-2", "labels": {}}]
    cm._migrate_records_v3_once()

    rec = load_record("ws-mig", "m2", vault_root=str(vault))
    assert rec is not None and rec.name == ""
    assert cm._record_for_name("cid-2") is None


def test_lazy_migration_is_one_shot(tmp_path):
    """A second run is a no-op: records created after the first run stay UNSET."""
    vault = tmp_path / "vault"
    _mint("ws-mig", vault, "m3", docker_id="cid-3")

    cm = _make_manager("ws-mig", vault, _client())
    cm.list_containers = lambda: [
        {"container_id": "cid-3", "labels": {CONTAINER_NAME_LABEL: "first-name"}},
    ]
    cm._migrate_records_v3_once()
    assert load_record("ws-mig", "m3", vault_root=str(vault)).name == "first-name"

    # A record introduced AFTER the one-shot migration is not picked up.
    _mint("ws-mig", vault, "m4", docker_id="cid-4")
    cm.list_containers = lambda: [
        {"container_id": "cid-4", "labels": {CONTAINER_NAME_LABEL: "late-name"}},
    ]
    cm._migrate_records_v3_once()
    assert load_record("ws-mig", "m4", vault_root=str(vault)).name == ""
