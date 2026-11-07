"""Hermetic tests for the agent-explicit heal opt-in on ``ContainerStartTool``.

``ContainerStartTool.heal`` (``Field(default=False)``) is the ONLY agent-facing
way to request a recreate of an UNBOUND container record.  It threads through to
``ContainerManager.start(heal_missing=True)`` and surfaces the machine-readable
refusal ``code`` in the tool's JSON response.

These tests drive the REAL ``ContainerStartTool.execute`` against a fake manager
(built via ``ContainerManager.__new__``) whose ``_fresh_start`` is stubbed, so
the whole opt-in -> ``start`` -> JSON-response contract is exercised without
Docker.  Fakes mirror ``tests/test_container_start_drift.py``.

T1  heal=True + EPHEMERAL +evidence snapshot -> success, SAME record rebound.
T2  heal=True + PERSISTENT                     -> success, SAME record rebound.
T3  heal=True + RESOURCE/SERVICE (own_lifecycle) -> code 'container_record_not_healable'.
T4  heal=True + EPHEMERAL, NO snapshot -> 'container_record_container_missing',
    then (memoised) 'container_record_unbound'.
T5  heal default(False), unbound -> 'container_record_unbound' surfaced by the tool.
T6  the response carries 'code' ONLY when ``start`` supplied one.
T7  'heal_missing' is forwarded to ``start`` ONLY when ``heal`` is requested.
"""

import json
from unittest import mock
from unittest.mock import MagicMock

import pytest

import infra.container_manager as container_manager
from infra.container_manager import ContainerManager
from thoughtmachine.container_record import (
    LIFECYCLE_EPHEMERAL,
    LIFECYCLE_PERSISTENT,
    LIFECYCLE_RESOURCE,
    LIFECYCLE_SERVICE,
    OWNER_WORKSPACE,
    RECORD_LABEL_KEY,
    create_record,
    load_record,
    update_record,
)
from tools.container_control import ContainerStartTool

_NEW = "e" * 16
_STALE = "d" * 16
#: Evidence-bearing intent snapshot the heal rebuilds FROM (design 1.1).
_SNAPSHOT = {"network_mode": "none", "workspace_mode": "ro"}


# ---------------------------------------------------------------------------
# Fakes (mirror tests/test_container_start_drift.py)
# ---------------------------------------------------------------------------


def _attrs(network, workspace_rw):
    return {
        "State": {"Status": "running"},
        "HostConfig": {"NetworkMode": network},
        "Mounts": [{"Destination": "/workspace", "RW": workspace_rw}],
    }


class _FakeImageRef:
    def __init__(self, image_id="sha256:deadbeef"):
        self.id = image_id

    def __str__(self):
        return self.id


class _FakeContainer:
    def __init__(self, container_id, name=None, labels=None, attrs=None):
        self.id = container_id
        self.name = name or container_id
        self.image = _FakeImageRef()
        self.status = "running"
        self.labels = dict(labels or {})
        self.attrs = attrs if attrs is not None else _attrs("none", False)
        self.removed = []

    def remove(self, **kwargs):
        self.removed.append(kwargs)

    def reload(self):
        pass


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Set by the ``_clean_state`` autouse fixture (below): the record-driven identity
# ladder mints/reads records through the DEFAULT vault.
_VAULT = {}


def _make_cm(container, want, workspace_id="w1"):
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
    cm.client = _FakeDockerClient([container])
    cm._remove_container = MagicMock()
    cm._get_max_containers = lambda: 10
    cm._find_by_labels = lambda n: None
    cm.list_containers = lambda: []
    cm.vault_root = _VAULT.get("root")
    return cm


def _missing_cm(workspace_id="w1"):
    """Manager whose record-backed name names a docker_id with NO live container."""
    cm = _make_cm(None, want=("none", "ro"), workspace_id=workspace_id)
    cm.client = _FakeDockerClient([])  # no live containers -> docker_id is stale
    cm._fresh_start = MagicMock(
        return_value={"id": _NEW, "name": "agent-x", "status": "created",
                      "note": ""})
    return cm


def _mint(workspace_id, name, docker_id, lifecycle_class=LIFECYCLE_PERSISTENT,
          snapshot=None):
    """Mint a container RECORD binding *name* to the (stale) *docker_id*."""
    vault = _VAULT["root"]
    create_record(
        workspace_id, lifecycle_class, OWNER_WORKSPACE,
        id="rec-1", name=name, vault_root=vault,
    )
    update_record(workspace_id, "rec-1", vault_root=vault, docker_id=docker_id)
    if snapshot is not None:
        update_record(workspace_id, "rec-1", vault_root=vault,
                      intent_snapshot=snapshot)


def _fake_fresh_for(cm):
    """A ``_fresh_start`` stand-in: rebind the SAME record + publish the container."""
    new_id = _NEW

    def _fake_fresh(**kwargs):
        update_record(cm.workspace_id, "rec-1", vault_root=_VAULT["root"],
                      docker_id=new_id)
        cm.client.containers.containers = [
            _FakeContainer(new_id, name="agent-x",
                           labels={RECORD_LABEL_KEY: "rec-1"},
                           attrs=_attrs("none", False))]
        return {"id": new_id, "name": "agent-x", "status": "created", "note": ""}

    return _fake_fresh


def _drive(cm, **tool_kwargs):
    """Run the REAL ``ContainerStartTool.execute`` against *cm*; parse the JSON."""
    tool = ContainerStartTool(**tool_kwargs)
    with mock.patch.object(ContainerStartTool, "_make_manager",
                           return_value=cm):
        raw = tool.execute()
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state(tmp_path, monkeypatch):
    """Fresh tmp vault + clear every module-scope memo the start path consults."""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))
    _VAULT["root"] = str(vault)

    def _reset():
        container_manager._EXEC_DRIFT_SEEN.clear()
        container_manager._START_DRIFT_SEEN.clear()
        container_manager._HEAL_ATTEMPTED.clear()
        container_manager._NAME_INDEX.clear()
        container_manager._NAME_INDEX_COLLISIONS.clear()
        container_manager._NAME_INDEX_BUILT.clear()
        container_manager._NAME_MIGRATED.clear()
        container_manager._NOTES_WARNED.clear()
        container_manager._NOTES_MIGRATED.clear()

    _reset()
    yield
    _reset()


# ---------------------------------------------------------------------------
# T1/T2: heal=True rebuilds the SAME record for a non-owning class
# ---------------------------------------------------------------------------


def test_heal_true_recreates_ephemeral_record():
    """T1: EPHEMERAL (non-owning) + evidence snapshot -> heal succeeds."""
    cm = _missing_cm()
    cm._fresh_start = MagicMock(side_effect=_fake_fresh_for(cm))
    _mint("w1", "agent-x", _STALE, lifecycle_class=LIFECYCLE_EPHEMERAL,
          snapshot=_SNAPSHOT)

    result = _drive(cm, name="agent-x", heal=True)

    assert result["success"] is True
    assert result["container_id"] == _NEW
    assert result["status"] == "created"
    assert cm._fresh_start.call_count == 1
    # The SAME record id survives; only its docker_id is rebound.
    rec = load_record("w1", "rec-1", vault_root=_VAULT["root"])
    assert rec.id == "rec-1"
    assert rec.docker_id == _NEW


def test_heal_true_recreates_persistent_record():
    """T2: PERSISTENT (non-owning) + evidence snapshot -> heal succeeds."""
    cm = _missing_cm()
    cm._fresh_start = MagicMock(side_effect=_fake_fresh_for(cm))
    _mint("w1", "agent-x", _STALE, lifecycle_class=LIFECYCLE_PERSISTENT,
          snapshot=_SNAPSHOT)

    result = _drive(cm, name="agent-x", heal=True)

    assert result["success"] is True
    assert result["container_id"] == _NEW
    assert result["status"] == "created"
    assert cm._fresh_start.call_count == 1
    rec = load_record("w1", "rec-1", vault_root=_VAULT["root"])
    assert rec.id == "rec-1"
    assert rec.docker_id == _NEW


# ---------------------------------------------------------------------------
# T3: heal=True is REFUSED (distinct code) for an own-lifecycle class
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lifecycle_class", [LIFECYCLE_RESOURCE, LIFECYCLE_SERVICE])
def test_heal_true_refused_for_own_lifecycle_class(lifecycle_class):
    """T3: RESOURCE/SERVICE own their lifecycle -> 'container_record_not_healable'."""
    cm = _missing_cm()
    cm._fresh_start = MagicMock(side_effect=_fake_fresh_for(cm))
    _mint("w1", "agent-x", _STALE, lifecycle_class=lifecycle_class,
          snapshot=_SNAPSHOT)

    result = _drive(cm, name="agent-x", heal=True)

    assert result["success"] is False
    assert result["code"] == "container_record_not_healable"
    assert result["code"] != "container_record_container_missing"
    assert isinstance(result["error"], str)
    assert cm._fresh_start.call_count == 0  # the class gate blocks the rebuild


# ---------------------------------------------------------------------------
# T4: heal=True with NO snapshot evidence falls through to the ordinary codes
# ---------------------------------------------------------------------------


def test_heal_true_ephemeral_without_snapshot_refuses_then_unbound():
    """T4: no evidence snapshot -> container_missing, then memoised -> unbound."""
    cm = _missing_cm()
    cm._fresh_start = MagicMock(side_effect=_fake_fresh_for(cm))
    _mint("w1", "agent-x", _STALE, lifecycle_class=LIFECYCLE_EPHEMERAL)  # no snapshot

    r1 = _drive(cm, name="agent-x", heal=True)
    assert r1["success"] is False
    assert r1["code"] == "container_record_container_missing"
    assert cm._fresh_start.call_count == 0  # never built from an absent snapshot

    # The attempt is memoised at ATTEMPT time -> the second call reconciles the
    # unbound record and refuses with the ordinary 'unbound' code.
    r2 = _drive(cm, name="agent-x", heal=True)
    assert r2["success"] is False
    assert r2["code"] == "container_record_unbound"


# ---------------------------------------------------------------------------
# T5: the default (heal omitted/False) surfaces the unbound code
# ---------------------------------------------------------------------------


def test_heal_default_false_surfaces_unbound_code():
    """T5: no heal opt-in -> the ordinary 'container_record_unbound' refusal."""
    cm = _missing_cm()
    _mint("w1", "agent-x", _STALE, lifecycle_class=LIFECYCLE_EPHEMERAL,
          snapshot=_SNAPSHOT)

    result = _drive(cm, name="agent-x")  # heal defaults False

    assert result["success"] is False
    assert result["code"] == "container_record_unbound"
    assert isinstance(result["error"], str)
    assert cm._fresh_start.call_count == 0


# ---------------------------------------------------------------------------
# T6: 'code' is surfaced additively, ONLY when start() supplied one
# ---------------------------------------------------------------------------


def test_error_result_surfaces_code_only_when_present():
    """T6: an error WITHOUT a code gains no 'code' key; WITH one it is surfaced."""
    no_code = MagicMock()
    no_code.start = MagicMock(return_value={"error": "boom"})
    r1 = _drive(no_code, name="agent-x")

    assert r1["success"] is False
    assert r1["error"] == "boom"
    assert "code" not in r1

    with_code = MagicMock()
    with_code.start = MagicMock(return_value={"error": "nope", "code": "some_code"})
    r2 = _drive(with_code, name="agent-x")

    assert r2["success"] is False
    assert r2["error"] == "nope"
    assert r2["code"] == "some_code"


# ---------------------------------------------------------------------------
# T7: 'heal_missing' is forwarded to start() ONLY when heal is requested
# ---------------------------------------------------------------------------


def test_heal_forwards_heal_missing_only_when_requested():
    """T7: default call carries no 'heal_missing'; heal=True sets it True."""
    ok = {"id": _NEW, "name": "agent-x", "status": "created", "note": ""}

    cm_default = MagicMock()
    cm_default.start = MagicMock(return_value=dict(ok))
    r_default = _drive(cm_default, name="agent-x")  # heal not passed -> False
    assert r_default["success"] is True
    assert "heal_missing" not in cm_default.start.call_args.kwargs

    cm_heal = MagicMock()
    cm_heal.start = MagicMock(return_value=dict(ok))
    r_heal = _drive(cm_heal, name="agent-x", heal=True)
    assert r_heal["success"] is True
    assert cm_heal.start.call_args.kwargs.get("heal_missing") is True
