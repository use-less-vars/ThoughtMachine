"""Wave 1: record/docker reconciliation on the ``ContainerManager.start`` path.

Divergences closed
------------------
1. A record whose ``docker_id`` names a container that no longer exists is now
   marked UNBOUND (``docker_id=None``) on the ORDINARY start path and a
   ``drift.container_absent`` event is appended to the record log -- instead of
   silently refusing with the ``container_record_container_missing`` code.
2. The refusal for a newly-unbound record carries the dedicated
   ``container_record_unbound`` code and the verbatim Wave-1 message.

Reconciliation is NON-healing: the underlying container is NOT recreated, so
nothing is created when the record is marked unbound. Recovery is explicit and
separate: an operator can recreate it via the record's recreate action, and an
agent heals from the record's ``intent_snapshot`` -- automatically via
``DockerCodeRunner`` (which passes ``heal_missing=True``) or explicitly via
``ContainerStartTool`` (``heal=True``).

The manager is built via ``ContainerManager.__new__`` (no Docker daemon) and
every daemon touchpoint is stubbed, mirroring
``tests/test_registry_transition_schema_v3.py`` and
``tests/tools/test_container_control.py``.
"""

from unittest.mock import MagicMock

import pytest

import infra.container_manager as _cm
from infra.container_manager import ContainerManager
from thoughtmachine.container_record import (
    LIFECYCLE_PERSISTENT,
    OWNER_WORKSPACE,
    create_record,
    load_record,
    read_event_log,
    update_record,
)


CONTAINER_ABSENT_EVENT = "drift.container_absent"


# ═══════════════════════════════════════════════════════════════════════════════
#  Fakes / helpers
# ═══════════════════════════════════════════════════════════════════════════════


class _FakeImageRef:
    def __init__(self, ref="sha256:deadbeef"):
        self.id = ref

    def __str__(self):
        return self.id


class _FakeContainer:
    def __init__(self, container_id, name=None, status="running"):
        self.id = container_id
        self.name = name or container_id
        self.image = _FakeImageRef()
        self.status = status
        self.labels = {}
        self.attrs = {"State": {"Status": status}}

    def reload(self):
        pass

    def start(self):
        self.status = "running"


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
        return list(self._containers)


def _client(containers=None):
    return type(
        "_C",
        (),
        {"containers": _FakeContainers(containers or []), "ping": lambda self: True},
    )()


def _manager(workspace_id, vault_root, client):
    """Build a ContainerManager with every daemon touchpoint stubbed."""
    cm = ContainerManager.__new__(ContainerManager)
    cm.workspace_path = "/tmp/tm-record-reconciliation-ws"
    cm.session_id = "sess-record-reconciliation"
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
    return cm


def _mint(workspace_id, vault_root, rid, name="", docker_id=None):
    """Seed a record (optionally bound to a docker_id) in the test vault."""
    create_record(
        workspace_id, LIFECYCLE_PERSISTENT, OWNER_WORKSPACE,
        id=rid, name=name, vault_root=str(vault_root),
    )
    if docker_id is not None:
        update_record(workspace_id, rid, vault_root=str(vault_root),
                      docker_id=docker_id)
    return rid


# ═══════════════════════════════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


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
    memos = (_cm._NAME_INDEX, _cm._NAME_INDEX_COLLISIONS, _cm._NAME_INDEX_BUILT,
             _cm._NAME_MIGRATED, _cm._NOTES_MIGRATED, _cm._NOTES_WARNED)
    for memo in memos:
        memo.clear()
    yield
    for memo in memos:
        memo.clear()


# ═══════════════════════════════════════════════════════════════════════════════
#  start(): a record naming an ABSENT container -> mark UNBOUND (no heal)
# ═══════════════════════════════════════════════════════════════════════════════


def test_start_marks_record_unbound_when_named_container_is_absent(tmp_path):
    """Divergences 1+2: absent container -> mark unbound + refuse as unbound."""
    vault = tmp_path / "vault"
    stale_docker_id = "c" * 16
    # (1) a record whose docker_id names a container that does NOT exist.
    _mint("ws-recon", vault, "rec-1", name="agent-x",
          docker_id=stale_docker_id)

    cm = _manager("ws-recon", vault, _client())  # no live container

    # (2) drive the ordinary start path.
    result = cm.start(name="agent-x")

    # (3) the record now carries no docker_id (marked unbound).
    rec = load_record("ws-recon", "rec-1", vault_root=str(vault))
    assert rec is not None
    assert not rec.docker_id

    # (4) a drift.container_absent event was appended to the record log.
    events = read_event_log("ws-recon", "rec-1", vault_root=str(vault))
    assert CONTAINER_ABSENT_EVENT in [e.get("event_type") for e in events]

    # (5) the refusal uses the DEDICATED unbound code, not container_missing.
    assert result["code"] == "container_record_unbound"
    assert result["code"] != "container_record_container_missing"

    # (6) the shipped Wave-1 message is surfaced.
    assert "an operator can recreate it via the record's recreate action" in result["error"]

    # The identity ladder never consulted label discovery.
    assert cm._find_by_labels.called is False


def test_mark_unbound_does_not_create_a_container(tmp_path):
    """Reconciliation is NON-healing: nothing is created on the refusal."""
    vault = tmp_path / "vault"
    _mint("ws-recon", vault, "rec-2", name="agent-y", docker_id="d" * 16)

    run_calls = []

    class _Containers:
        def get(self, container_id):
            raise LookupError(container_id)

        def list(self, all=False, filters=None):  # noqa: A002
            return []

        def run(self, *args, **kwargs):
            run_calls.append(kwargs)
            raise AssertionError("reconciliation must not create a container")

    client = type("_C", (), {"containers": _Containers(),
                            "ping": lambda self: True})()
    cm = _manager("ws-recon", vault, client)

    result = cm.start(name="agent-y")

    assert result["code"] == "container_record_unbound"
    assert run_calls == []
