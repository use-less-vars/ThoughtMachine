"""Hermetic tests for the start-path drift admission (ContainerManager.start).

A REUSED container whose LIVE network/workspace isolation no longer matches the
resolved session policy is NEVER silently reused and NEVER mutated in ``start``:

  * a not-more-permissive mismatch is REUSED, with a ``drift`` detail attached;
  * a MORE-permissive mismatch is REFUSED (``{"error", "drift", ...}``) and the
    container is left untouched (no remove / recreate / re-tag).

The drift EVENT + WARNING + audit fire ONCE per distinct signature (its own
module-level memo, ``container_manager._START_DRIFT_SEEN``), while the DECISION
is applied on *every* call.

All three reuse sources are exercised: ``"workspace-label"``, ``"record"`` and
``"label"``.  (The old in-memory ``"registry"`` cache is NO LONGER an identity
source: a cached name with no record now REFUSES, so the record-driven reuse
path takes its place.)  Fakes mirror ``tests/test_container_exec_drift.py``; the
manager is built via ``ContainerManager.__new__`` so the real Docker client is
never touched.
"""

from unittest.mock import MagicMock

import pytest

import infra.container_manager as container_manager
from infra.container_manager import ContainerManager
from thoughtmachine.container_record import (
    LIFECYCLE_CLASSES,
    LIFECYCLE_PERSISTENT,
    OWNER_WORKSPACE,
    RECORD_LABEL_KEY,
    create_record,
    update_record,
)

_SITES = ["workspace-label", "record", "label"]
_EVENT = "drift.start_on_drifted_container"
_ACTOR = "infra.container_manager.start"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _attrs(network, workspace_rw):
    return {
        "State": {"Status": "running"},
        "HostConfig": {"NetworkMode": network},
        "Mounts": [{"Destination": "/workspace", "RW": workspace_rw}],
    }


_ABSENT_WORKSPACE_ATTRS = {
    "State": {"Status": "running"},
    "HostConfig": {"NetworkMode": "none"},
    "Mounts": [],  # /workspace bind gone -> "absent" (more permissive)
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


class _RaisingAttrsContainer(_FakeContainer):
    def __init__(self, container_id, name=None):
        # Deliberately avoid ``self.attrs = ...`` (the property has no setter).
        self.id = container_id
        self.name = name or container_id
        self.image = _FakeImageRef()
        self.status = "running"
        self.labels = {}
        self.removed = []

    @property
    def attrs(self):
        raise RuntimeError("attrs unavailable")


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


def _make_cm(container, want, name="agent-x", workspace_id="w1"):
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
    # Spy: the OLD start() mutated on drift by removing the container; the NEW
    # policy must NOT.  ``MagicMock`` lets a test assert ``.called is False``.
    cm._remove_container = MagicMock()
    cm._get_max_containers = lambda: 10
    cm._find_by_labels = lambda n: None
    cm.list_containers = lambda: []
    cm.vault_root = _VAULT.get("root")
    return cm


# Set by the ``_tmp_vault`` autouse fixture (below); the record-driven reuse
# site mints a REAL record into this vault so ``start`` resolves identity from
# the record (name -> docker_id -> live container), not from a label lookup.
_VAULT = {}


def _mint_record(workspace_id, name, docker_id):
    """Mint a container RECORD binding *name* to the live container's id."""
    vault = _VAULT["root"]
    create_record(
        workspace_id, LIFECYCLE_PERSISTENT, OWNER_WORKSPACE,
        id="rec-1", name=name, vault_root=vault,
    )
    update_record(workspace_id, "rec-1", vault_root=vault, docker_id=docker_id)


def _arrange(cm, container, name, site):
    """Route ``start`` through the requested reuse source."""
    if site == "workspace-label":
        cm.list_containers = (
            lambda: [{"name": name, "container_id": container.id, "note": ""}]
        )
    elif site == "record":
        _mint_record(cm.workspace_id, name, container.id)
    elif site == "label":
        cm._find_by_labels = lambda n: container
    else:  # pragma: no cover - guards against a typo in the parametrisation
        raise ValueError(f"unknown site {site!r}")
    return cm


def _start(container, want, site, **kwargs):
    cm = _make_cm(container, want=want)
    _arrange(cm, container, kwargs.get("name", "agent-x"), site)
    return cm, cm.start(**kwargs)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_memo():
    container_manager._EXEC_DRIFT_SEEN.clear()
    container_manager._START_DRIFT_SEEN.clear()
    yield
    container_manager._EXEC_DRIFT_SEEN.clear()
    container_manager._START_DRIFT_SEEN.clear()


@pytest.fixture(autouse=True)
def _tmp_vault(tmp_path, monkeypatch):
    """A tmp DEFAULT vault + isolated name-index/notes memos.

    The record-driven reuse site mints/reads records through the DEFAULT vault
    (``THOUGHTMACHINE_VAULT_ROOT``); the ``(workspace, name) -> record id`` index
    and the notes memos are module-scoped, so they must be cleared per test or a
    freshly minted record in the same workspace would never be indexed.
    """
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
def _no_registry(monkeypatch):
    """Never let the registry facade intercept the (stubbed) reuse paths."""
    monkeypatch.setattr(
        container_manager, "is_registry_active", lambda *a, **k: False
    )


@pytest.fixture
def events(monkeypatch):
    """Capture record events via a patched thoughtmachine.container_record."""
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


# ---------------------------------------------------------------------------
# (a) clean match -> reuse exactly as today, NO drift key, NO event
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("site", _SITES)
def test_clean_match_reuses_without_drift(site, events):
    ctr = _FakeContainer("c" * 16, labels={RECORD_LABEL_KEY: "rec-1"},
                         attrs=_attrs("none", False))  # policy ("none","ro")
    cm, result = _start(ctr, want=("none", "ro"), site=site, name="agent-x")

    assert result["status"] == "reused"
    assert "drift" not in result
    assert ctr.removed == []
    assert cm._remove_container.called is False
    assert events == []


# ---------------------------------------------------------------------------
# (b) live MORE permissive -> REFUSE, container NOT mutated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("site", _SITES)
def test_more_permissive_network_refused(site, events):
    ctr = _FakeContainer("c" * 16, labels={RECORD_LABEL_KEY: "rec-1"},
                         attrs=_attrs("bridge", True))  # policy is ("none","ro")
    cm, result = _start(ctr, want=("none", "ro"), site=site, name="agent-x")

    assert "error" in result
    assert "status" not in result
    assert result["drift"]["decision"] == "deny"
    assert result["drift"]["reason"] == "network_more_permissive"
    assert result["drift"]["source"] == site
    assert result["drift"]["network_mode"] == "bridge"
    assert result["drift"]["workspace_mode"] == "rw"
    # The refused container is NEVER removed / mutated.
    assert ctr.removed == []
    assert cm._remove_container.called is False
    assert len(events) == 1
    ev = events[0]
    assert ev["event_type"] == _EVENT
    assert ev["actor"] == _ACTOR
    assert ev["record_id"] == "rec-1"
    assert ev["payload"]["decision"] == "deny"
    assert ev["payload"]["reason"] == "network_more_permissive"
    assert ev["payload"]["source"] == site
    assert ev["payload"]["expected"] == {"network_mode": "none", "workspace_mode": "ro"}
    assert ev["payload"]["actual"] == {"network_mode": "bridge", "workspace_mode": "rw"}
    assert "detected_at" in ev["payload"]


@pytest.mark.parametrize("site", _SITES)
def test_more_permissive_workspace_refused(site, events):
    ctr = _FakeContainer("c" * 16, labels={RECORD_LABEL_KEY: "rec-1"},
                         attrs=_ABSENT_WORKSPACE_ATTRS)
    cm, result = _start(ctr, want=("none", "ro"), site=site, name="agent-x")

    assert "error" in result
    assert result["drift"]["decision"] == "deny"
    assert result["drift"]["reason"] == "workspace_more_permissive"
    assert result["drift"]["workspace_mode"] == "absent"
    assert ctr.removed == []
    assert cm._remove_container.called is False
    assert len(events) == 1


# ---------------------------------------------------------------------------
# (c) differs but NOT more permissive -> REUSE with a drift "warn" detail
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("site", _SITES)
def test_less_permissive_is_reused_with_warn(site, events):
    # policy rw, live network equal, workspace is ro (stricter) -> warn + reuse.
    ctr = _FakeContainer("c" * 16, labels={RECORD_LABEL_KEY: "rec-1"},
                         attrs=_attrs("bridge", False))
    cm, result = _start(ctr, want=("bridge", "rw"), site=site, name="agent-x")

    assert result["status"] == "reused"
    assert result["drift"]["decision"] == "warn"
    assert result["drift"]["reason"] == "config_differs_not_more_permissive"
    assert result["drift"]["source"] == site
    assert ctr.removed == []
    assert cm._remove_container.called is False
    assert len(events) == 1
    assert events[0]["payload"]["decision"] == "warn"


# ---------------------------------------------------------------------------
# (d) memoisation: one emission per distinct signature; decision still applies
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("site", _SITES)
def test_event_emitted_once_per_detection(site, events):
    ctr = _FakeContainer("c" * 16, labels={RECORD_LABEL_KEY: "rec-1"},
                         attrs=_attrs("bridge", True))
    cm = _make_cm(ctr, want=("none", "ro"))
    _arrange(cm, ctr, "agent-x", site)

    r1 = cm.start(name="agent-x")
    r2 = cm.start(name="agent-x")

    # Same signature -> still refused on BOTH calls, but only ONE event emitted.
    assert "error" in r1 and "error" in r2
    assert len(events) == 1


# ---------------------------------------------------------------------------
# (e) policy applies regardless of the container's lifecycle class
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lifecycle_class", list(LIFECYCLE_CLASSES))
def test_all_lifecycle_classes_refused_on_drift(lifecycle_class, events):
    ctr = _FakeContainer("c" * 16, labels={RECORD_LABEL_KEY: "rec-1"},
                         attrs=_attrs("bridge", True))
    cm, result = _start(ctr, want=("none", "ro"), site="workspace-label",
                        name="agent-x", lifecycle_class=lifecycle_class)

    assert "error" in result
    assert result["drift"]["decision"] == "deny"
    assert len(events) == 1


# ---------------------------------------------------------------------------
# (f) negative: a NON-drifted container publishes NO drift event
# ---------------------------------------------------------------------------


def test_non_drifted_reuse_emits_no_drift_event(events):
    ctr = _FakeContainer("c" * 16, labels={RECORD_LABEL_KEY: "rec-1"},
                         attrs=_attrs("none", False))
    cm, result = _start(ctr, want=("none", "ro"), site="workspace-label",
                        name="agent-x")

    assert result["status"] == "reused"
    assert "drift" not in result
    assert [e for e in events if e["event_type"] == _EVENT] == []


# ---------------------------------------------------------------------------
# (g) fail-safe: unreadable attrs -> reuse as today, never crash
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("site", _SITES)
def test_unreadable_attrs_reuse_without_drift(site, events):
    ctr = _RaisingAttrsContainer("c" * 16, name="agent-x")
    cm, result = _start(ctr, want=("none", "ro"), site=site, name="agent-x")

    assert result["status"] == "reused"
    assert "drift" not in result
    assert cm._remove_container.called is False
    assert events == []
