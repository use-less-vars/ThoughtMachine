"""Hermetic tests: the auto-heal rebuild is sourced from the STORED snapshot.

``ContainerManager._heal_missing`` rebuilds a record whose ``docker_id`` names
no live container FROM THE RECORD'S STORED ``intent_snapshot`` -- never from the
CURRENT policy:

  * an evidence-bearing snapshot IS the rebuild source (its ``network_mode`` /
    ``workspace_mode`` are exactly what ``_fresh_start`` is called with);
  * a missing/absent/empty snapshot is NOT a rebuild source: the heal invents
    nothing -- it emits an ``intent_snapshot_missing`` drift finding and refuses
    (no fresh container), falling through to the ordinary refusal.

The manager is built via ``ContainerManager.__new__`` (real Docker never
touched) and the daemon create seam (``_fresh_start``) is stubbed so only the
source-selection under test runs.
"""

from unittest.mock import MagicMock

import pytest

import infra.container_manager as container_manager
from infra.container_manager import ContainerManager
from thoughtmachine.container_record import (
    LIFECYCLE_PERSISTENT,
    OWNER_WORKSPACE,
    create_record,
    update_record,
)
from thoughtmachine.container_record import drift

_STALE = "d" * 16
_NEW_ID = "e" * 16
_VAULT = {}


class _FakeContainers:
    def __init__(self, containers):
        self.containers = list(containers)

    def get(self, container_id):
        for c in self.containers:
            if c.id == container_id or c.name == container_id:
                return c
        raise LookupError(container_id)


class _FakeDockerClient:
    def __init__(self, containers):
        self.containers = _FakeContainers(containers)


def _make_cm(workspace_id="w1", want=("none", "ro")):
    cm = ContainerManager.__new__(ContainerManager)
    cm.workspace_path = "/tmp/ws-test"
    cm.session_id = "s1"
    cm.workspace_id = workspace_id
    cm.image = "img"
    cm.session_permissions = {}
    cm.workspace_config = {"disk_quota_mb": 0}
    cm._containers = {}
    cm.container_notes = {}
    cm._session_config = None
    cm._compute_config = lambda *a, **k: want
    cm.client = _FakeDockerClient([])  # no live containers -> docker_id is stale
    cm._remove_container = MagicMock()
    cm._get_max_containers = lambda: 10
    cm._find_by_labels = lambda n: None
    cm.list_containers = lambda: []
    cm.vault_root = _VAULT.get("root")
    cm._fresh_start = MagicMock(
        return_value={"id": _NEW_ID, "name": "agent-x",
                      "status": "created", "note": ""})
    return cm


def _mint_record(workspace_id, name, docker_id, intent_snapshot=None):
    """Mint the record binding *name* to a stale docker id, optional snapshot."""
    vault = _VAULT["root"]
    create_record(workspace_id, LIFECYCLE_PERSISTENT, OWNER_WORKSPACE,
                  id="rec-1", name=name, vault_root=vault)
    if intent_snapshot is None:
        update_record(workspace_id, "rec-1", vault_root=vault, docker_id=docker_id)
    else:
        update_record(workspace_id, "rec-1", vault_root=vault,
                      docker_id=docker_id, intent_snapshot=dict(intent_snapshot))


@pytest.fixture(autouse=True)
def _tmp_vault(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))
    _VAULT["root"] = str(vault)

    def _reset():
        container_manager._NAME_INDEX.clear()
        container_manager._NAME_INDEX_COLLISIONS.clear()
        container_manager._NAME_INDEX_BUILT.clear()
        container_manager._NAME_MIGRATED.clear()
        container_manager._NOTES_WARNED.clear()
        container_manager._NOTES_MIGRATED.clear()

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _clear_memos():
    container_manager._HEAL_ATTEMPTED.clear()
    container_manager._START_DRIFT_SEEN.clear()
    yield
    container_manager._HEAL_ATTEMPTED.clear()
    container_manager._START_DRIFT_SEEN.clear()


@pytest.fixture
def events(monkeypatch):
    import thoughtmachine.container_record as cr

    recorded = []

    def _fake_append(workspace_id, record_id, event_type, actor,
                     vault_root=None, **payload):
        recorded.append({
            "workspace_id": workspace_id,
            "record_id": record_id,
            "event_type": event_type,
            "actor": actor,
            "payload": payload,
        })

    monkeypatch.setattr(cr, "append_event", _fake_append, raising=True)
    return recorded


@pytest.fixture
def audits(monkeypatch):
    captured = []

    def _fake_audit(event, data):
        captured.append({"event": event, "data": data})

    monkeypatch.setattr(container_manager, "audit_event", _fake_audit, raising=True)
    return captured


def test_heal_rebuilds_from_stored_snapshot(events):
    """An evidence-bearing snapshot IS the rebuild source (not the policy)."""
    cm = _make_cm(want=("none", "ro"))  # current policy differs from the snapshot
    _mint_record("w1", "agent-x", _STALE,
                 intent_snapshot={"network_mode": "bridge", "workspace_mode": "rw"})

    result = cm.start(name="agent-x", heal_missing=True)

    assert result.get("error") is None
    assert result["id"] == _NEW_ID
    assert cm._fresh_start.call_count == 1
    _args, kwargs = cm._fresh_start.call_args
    assert kwargs["reuse_record_id"] == "rec-1"
    # Sourced from the STORED snapshot, NOT the ("none", "ro") current policy.
    assert kwargs["network_mode"] == "bridge"
    assert kwargs["workspace_mode"] == "rw"
    assert "permissions" not in kwargs
    recreated = [e for e in events if e["event_type"]
                 == container_manager.EVENT_CONTAINER_RECORD_AUTO_RECREATED]
    assert len(recreated) == 1


def test_heal_refuses_when_snapshot_absent(events, audits):
    """A record with no evidence-bearing snapshot is NOT healed: drift + refuse."""
    cm = _make_cm()
    _mint_record("w1", "agent-x", _STALE)  # default intent_snapshot has no evidence

    result = cm.start(name="agent-x", heal_missing=True)

    assert result["code"] == "container_record_container_missing"
    assert cm._fresh_start.call_count == 0  # never invented a container
    missing = [e for e in events
               if e["event_type"] == drift.EVENT_INTENT_SNAPSHOT_MISSING]
    assert len(missing) == 1
    assert missing[0]["actor"] == drift.ACTOR
    assert missing[0]["payload"]["class"] == drift.CLASS_INTENT
    refused = [e for e in events if e["event_type"]
               == container_manager.EVENT_CONTAINER_RECORD_AUTO_RECREATED_REFUSED]
    assert len(refused) == 1
    assert refused[0]["payload"]["detail"] == "snapshot_missing"
    assert [a for a in audits
            if a["event"] == container_manager._HEAL_AUDIT_REFUSED]
