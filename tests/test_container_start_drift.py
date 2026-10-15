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
    LIFECYCLE_RESOURCE,
    LIFECYCLE_SERVICE,
    OWNER_WORKSPACE,
    RECORD_LABEL_KEY,
    create_record,
    load_record,
    snapshot_from_attrs,
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


def _mint_record(workspace_id, name, docker_id, intent_snapshot=None):
    """Mint a container RECORD binding *name* to the live container's id.

    *intent_snapshot*, when supplied, seeds the record's STORED creation intent
    -- the heal's ONLY rebuild source: an evidence-bearing snapshot is required
    for the heal to proceed (never the current policy).
    """
    vault = _VAULT["root"]
    create_record(
        workspace_id, LIFECYCLE_PERSISTENT, OWNER_WORKSPACE,
        id="rec-1", name=name, vault_root=vault,
    )
    update_record(workspace_id, "rec-1", vault_root=vault, docker_id=docker_id)
    if intent_snapshot is not None:
        update_record(workspace_id, "rec-1", vault_root=vault,
                      intent_snapshot=intent_snapshot)


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
def _clear_heal_memo():
    """Isolate the module-scope auto-heal attempt memo between tests.

    ``_HEAL_ATTEMPTED`` is MODULE scope (it must survive the fresh
    ``ContainerManager`` built per tool call), so it MUST be cleared per test or
    a ``(record_id, stale_docker_id)`` key memoised in one test would suppress
    the heal in the next.  The related drift dedup state (``_EXEC_DRIFT_SEEN`` /
    ``_START_DRIFT_SEEN``) is already reset by ``_clear_memo``.
    """
    container_manager._HEAL_ATTEMPTED.clear()
    yield
    container_manager._HEAL_ATTEMPTED.clear()


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


@pytest.fixture
def audits(monkeypatch):
    """Capture container-manager audits via a patched ``audit_event``.

    ``container_manager._audit`` is ``lambda event, data: audit_event(...)``,
    so patching the module global intercepts every audit call.  This proves the
    stale-docker-id audit actually FIRES (rather than being a swallowed
    ``AttributeError``).
    """
    import infra.container_manager as cm

    captured = []

    def _fake_audit(event, data):
        captured.append({"event": event, "data": data})

    monkeypatch.setattr(cm, "audit_event", _fake_audit, raising=True)
    return captured


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


# ---------------------------------------------------------------------------
# (h) record names a STALE docker_id -> REFUSE + one container_absent drift
# ---------------------------------------------------------------------------


_ABSENT_EVENT = "drift.container_absent"


def test_stale_docker_id_refused_and_emits_absent_drift_once(events, audits):
    stale = "d" * 16
    cm = _make_cm(None, want=("none", "ro"))
    cm.client = _FakeDockerClient([])  # no live containers -> docker_id is stale
    _mint_record("w1", "agent-x", stale)

    r1 = cm.start(name="agent-x")
    assert "error" in r1
    assert r1["code"] == "container_record_unbound"

    absent = [e for e in events if e["event_type"] == _ABSENT_EVENT]
    assert len(absent) == 1
    ev = absent[0]
    assert ev["actor"] == _ACTOR
    assert ev["record_id"] == "rec-1"
    assert ev["workspace_id"] == "w1"
    assert ev["payload"]["source"] == "record"
    assert ev["payload"]["expected"] == {"docker_id": stale}
    assert ev["payload"]["actual"] == {"docker_id": None}
    assert "detected_at" in ev["payload"]

    # Memoised: the SECOND call still refuses but emits NO further event.
    r2 = cm.start(name="agent-x")
    assert "error" in r2
    assert r2["code"] == "container_record_unbound"
    assert [e for e in events if e["event_type"] == _ABSENT_EVENT] == absent

    # A1: the stale path fires the module-level audit ONCE (it is NOT a
    # swallowed ``self._audit`` AttributeError).
    audited = [a for a in audits if a["event"] == "CONTAINER_START_STALE_DOCKER_ID"]
    assert len(audited) == 1
    assert f"expected_docker_id={stale}" in audited[0]["data"]
    assert "actual_docker_id=None" in audited[0]["data"]


def test_stale_docker_id_emits_audit_once(monkeypatch, events):
    """The stale-docker-id refusal raises a CONTAINER_START_STALE_DOCKER_ID audit.

    The audit is the operator-visible trace that a record names a ``docker_id``
    with no live container.  It is emitted from the MODULE-level ``_audit``
    (``ContainerManager`` has no ``self._audit`` attribute), so it is captured by
    patching ``infra.container_manager._audit`` directly, and it is memoised with
    the drift event (same ``_START_DRIFT_SEEN`` signature).
    """
    audits = []
    monkeypatch.setattr(
        container_manager, "_audit",
        lambda event, data: audits.append((event, data)),
    )
    stale = "d" * 16
    cm = _make_cm(None, want=("none", "ro"))
    cm.client = _FakeDockerClient([])  # no live containers -> docker_id is stale
    _mint_record("w1", "agent-x", stale)

    r1 = cm.start(name="agent-x")
    assert r1["code"] == "container_record_unbound"

    fired = [a for a in audits if a[0] == "CONTAINER_START_STALE_DOCKER_ID"]
    assert len(fired) == 1, audits
    _event, data = fired[0]
    assert "name=agent-x" in data
    assert "record_id=rec-1" in data
    assert "source=record" in data
    assert f"expected_docker_id={stale}" in data
    assert "actual_docker_id=None" in data

    # Memoised: a second refused call adds NO further audit.
    cm.start(name="agent-x")
    assert [a for a in audits if a[0] == "CONTAINER_START_STALE_DOCKER_ID"] == fired



# ---------------------------------------------------------------------------
# (i) missing-container AUTO-HEAL (agent path, ``heal_missing=True``)
# ---------------------------------------------------------------------------
#
# A record whose ``docker_id`` has NO live container is, when the caller opts in
# with ``heal_missing=True``, rebuilt ONCE for the SAME record (the agent-facing
# counterpart to the operator-only ``allow_fresh`` recreate).  The attempt is
# memoised per ``(record_id, stale_docker_id)`` and is fail-closed: an
# own-lifecycle class, an uncomputable policy, or a refused rebuild all fall
# through to the byte-identical ordinary refusal.  Every refusal assertion
# carries a non-vacuous signal (the recreate/refused event, the heal audit, or
# the ordinary stale-docker-id drift + audit) so a silently-swallowed failure
# can never pass.

_HEAL_EVENT = container_manager.EVENT_CONTAINER_RECORD_AUTO_RECREATED
_HEAL_REFUSED_EVENT = container_manager.EVENT_CONTAINER_RECORD_AUTO_RECREATED_REFUSED
_HEAL_AUDIT_OK = container_manager._HEAL_AUDIT_HEALED
_HEAL_AUDIT_FAIL = container_manager._HEAL_AUDIT_REFUSED
_HEAL_NEW_ID = "e" * 16
_HEAL_STALE = "d" * 16
#: Evidence-bearing intent snapshot the heal rebuilds FROM.  Design §1.1: the
#: heal sources isolation from the record's STORED intent snapshot -- never the
#: current policy -- so the record must carry this before a heal can proceed.
_HEAL_SNAPSHOT = {"network_mode": "none", "workspace_mode": "ro"}


def _missing_cm(workspace_id="w1"):
    """Manager whose record-backed name names a docker_id with NO live container."""
    cm = _make_cm(None, want=("none", "ro"), workspace_id=workspace_id)
    cm.client = _FakeDockerClient([])  # no live containers -> the docker_id is stale
    cm._fresh_start = MagicMock(
        return_value={"id": _HEAL_NEW_ID, "name": "agent-x",
                      "status": "created", "note": ""})
    return cm


def test_missing_heal_recreates_for_existing_record(events, audits):
    """heal_missing=True: a stale docker_id is rebuilt ONCE for the SAME record."""
    cm = _missing_cm()
    _mint_record("w1", "agent-x", _HEAL_STALE, intent_snapshot=_HEAL_SNAPSHOT)

    result = cm.start(name="agent-x", heal_missing=True)

    assert result.get("error") is None
    assert result["id"] == _HEAL_NEW_ID
    assert result["status"] == "created"
    # Exactly ONE rebuild, bound to the EXISTING record, from the STORED snapshot.
    assert cm._fresh_start.call_count == 1
    _args, kwargs = cm._fresh_start.call_args
    assert kwargs["reuse_record_id"] == "rec-1"
    assert kwargs["network_mode"] == "none"
    assert kwargs["workspace_mode"] == "ro"
    # Non-vacuous: the recreate is signal-visible (event + audit).
    recreated = [e for e in events if e["event_type"] == _HEAL_EVENT]
    assert len(recreated) == 1
    ev = recreated[0]
    assert ev["workspace_id"] == "w1"
    assert ev["record_id"] == "rec-1"
    assert ev["actor"] == container_manager._HEAL_ACTOR
    assert ev["payload"]["reason"] == "container_missing"
    assert ev["payload"]["old_docker_id"] == _HEAL_STALE
    assert ev["payload"]["new_docker_id"] == _HEAL_NEW_ID
    heals = [a for a in audits if a["event"] == _HEAL_AUDIT_OK]
    assert len(heals) == 1
    assert f"old_docker_id={_HEAL_STALE}" in heals[0]["data"]
    assert f"new_docker_id={_HEAL_NEW_ID}" in heals[0]["data"]


def test_missing_without_heal_flag_refuses(events, audits):
    """heal_missing defaults False: the ordinary refusal stands, no rebuild."""
    cm = _missing_cm()
    _mint_record("w1", "agent-x", _HEAL_STALE)

    result = cm.start(name="agent-x")

    assert result["code"] == "container_record_unbound"
    assert cm._fresh_start.call_count == 0
    assert [e for e in events if e["event_type"] == _HEAL_EVENT] == []
    assert [a for a in audits if a["event"] == _HEAL_AUDIT_OK] == []
    # Non-vacuous: the ordinary stale-docker-id drift signal DID fire.
    assert len([e for e in events if e["event_type"] == _ABSENT_EVENT]) == 1


def test_missing_heal_is_attempted_once_only_when_it_fails(events, audits):
    """A REFUSED rebuild is terminal: the memo stops a second attempt."""
    cm = _missing_cm()
    cm._fresh_start = MagicMock(return_value={"error": "admission denied"})
    _mint_record("w1", "agent-x", _HEAL_STALE, intent_snapshot=_HEAL_SNAPSHOT)

    r1 = cm.start(name="agent-x", heal_missing=True)
    r2 = cm.start(name="agent-x", heal_missing=True)

    assert r1["code"] == "container_record_container_missing"
    assert r2["code"] == "container_record_unbound"
    # Exactly ONE attempt across BOTH calls: the failed heal is memoised at
    # ATTEMPT time, so a later call never retries.
    assert cm._fresh_start.call_count == 1
    assert [e for e in events if e["event_type"] == _HEAL_EVENT] == []
    # Non-vacuous: the ordinary refusal signal fired (once, memoised).
    assert len([a for a in audits
                if a["event"] == "CONTAINER_START_STALE_DOCKER_ID"]) == 1


def test_missing_heal_refuses_when_snapshot_absent(events, audits):
    """A record with NO evidence-bearing snapshot invents nothing: refuse.

    The heal's ONLY rebuild source is the record's STORED intent snapshot, so an
    absent (or all-empty) snapshot is a hard refusal -- it never falls back to
    the current policy to fabricate isolation.
    """
    cm = _missing_cm()
    # No intent_snapshot seeded -> snapshot_has_evidence(None) is False.
    _mint_record("w1", "agent-x", _HEAL_STALE)

    result = cm.start(name="agent-x", heal_missing=True)

    assert result["code"] == "container_record_container_missing"
    assert cm._fresh_start.call_count == 0  # never built from an absent snapshot
    refused = [e for e in events if e["event_type"] == _HEAL_REFUSED_EVENT]
    assert len(refused) == 1
    assert refused[0]["payload"]["reason"] == "container_missing"
    assert refused[0]["payload"]["detail"] == "snapshot_missing"
    assert refused[0]["payload"]["old_docker_id"] == _HEAL_STALE
    failed = [a for a in audits if a["event"] == _HEAL_AUDIT_FAIL]
    assert len(failed) == 1
    assert "detail=snapshot_missing" in failed[0]["data"]


def test_missing_heal_refused_for_own_lifecycle_class(events, audits):
    """Classes that OWN their lifecycle (RESOURCE/SERVICE) are never auto-healed."""
    cm = _missing_cm()
    create_record("w1", LIFECYCLE_RESOURCE, OWNER_WORKSPACE, id="rec-1",
                  name="agent-x", vault_root=_VAULT["root"])
    update_record("w1", "rec-1", vault_root=_VAULT["root"], docker_id=_HEAL_STALE)

    result = cm.start(name="agent-x", heal_missing=True)

    # CLASS-ONLY refusal: an own-lifecycle class reports the distinct
    # 'container_record_not_healable' code (NOT the generic missing code).
    assert result["code"] == "container_record_not_healable"
    assert cm._fresh_start.call_count == 0  # the class gate blocks the rebuild
    assert [e for e in events if e["event_type"] == _HEAL_EVENT] == []
    # Non-vacuous: the ordinary stale-docker-id drift signal still fires.
    assert len([e for e in events if e["event_type"] == _ABSENT_EVENT]) == 1


def test_drift_still_refuses_even_with_heal_flag(events, audits):
    """Regression pin: auto-heal is reachable ONLY on container_missing, never drift."""
    ctr = _FakeContainer("c" * 16, labels={RECORD_LABEL_KEY: "rec-1"},
                         attrs=_attrs("bridge", True))  # MORE permissive than policy
    cm = _make_cm(ctr, want=("none", "ro"))
    cm._fresh_start = MagicMock()
    _arrange(cm, ctr, "agent-x", "record")

    result = cm.start(name="agent-x", heal_missing=True)

    assert "error" in result
    assert result["drift"]["decision"] == "deny"
    assert cm._fresh_start.called is False
    assert [e for e in events if e["event_type"] == _HEAL_EVENT] == []


def test_missing_heal_rebuilds_from_stored_snapshot_then_reuses_clean(events):
    """The rebuild uses the STORED snapshot; the rebound container then reuses clean."""
    cm = _missing_cm()
    _mint_record("w1", "agent-x", _HEAL_STALE, intent_snapshot=_HEAL_SNAPSHOT)

    def _fake_fresh(**kwargs):
        # Mirror _fresh_start's real side effect: rebind the EXISTING record to the
        # fresh container, and publish it live with the snapshot's isolation.
        update_record("w1", "rec-1", vault_root=_VAULT["root"],
                      docker_id=_HEAL_NEW_ID)
        cm.client.containers.containers = [
            _FakeContainer(_HEAL_NEW_ID, name="agent-x",
                           labels={RECORD_LABEL_KEY: "rec-1"},
                           attrs=_attrs("none", False))]
        return {"id": _HEAL_NEW_ID, "name": "agent-x", "status": "created",
                "note": ""}

    cm._fresh_start = MagicMock(side_effect=_fake_fresh)

    r1 = cm.start(name="agent-x", heal_missing=True)
    assert r1["id"] == _HEAL_NEW_ID
    _args, kwargs = cm._fresh_start.call_args
    assert (kwargs["network_mode"], kwargs["workspace_mode"]) == ("none", "ro")

    # A SECOND start resolves the (now live) rebuilt container -> CLEAN reuse.
    r2 = cm.start(name="agent-x", heal_missing=True)
    assert r2["status"] == "reused"
    assert "drift" not in r2
    assert [e for e in events if e["event_type"] == _ABSENT_EVENT] == []


def test_missing_heal_memo_prevents_second_heal(events, audits):
    """After ONE heal the ``(record_id, stale_docker_id)`` memo is spent."""
    cm = _missing_cm()
    _mint_record("w1", "agent-x", _HEAL_STALE, intent_snapshot=_HEAL_SNAPSHOT)

    r1 = cm.start(name="agent-x", heal_missing=True)
    assert r1["id"] == _HEAL_NEW_ID
    assert cm._fresh_start.call_count == 1

    # The record STILL names the original stale docker_id (no rebind here): a
    # second start must NOT heal again.
    r2 = cm.start(name="agent-x", heal_missing=True)
    assert r2["code"] == "container_record_unbound"
    assert cm._fresh_start.call_count == 1


def test_missing_heal_fires_even_when_record_state_is_creating(events):
    """Negative pin: the heal gate does NOT consult the record's ``state`` field."""
    cm = _missing_cm()
    create_record("w1", LIFECYCLE_PERSISTENT, OWNER_WORKSPACE, id="rec-1",
                  name="agent-x", vault_root=_VAULT["root"])
    update_record("w1", "rec-1", vault_root=_VAULT["root"],
                  docker_id=_HEAL_STALE, state="creating",
                  intent_snapshot=_HEAL_SNAPSHOT)

    result = cm.start(name="agent-x", heal_missing=True)

    assert result["id"] == _HEAL_NEW_ID
    assert cm._fresh_start.call_count == 1
    assert len([e for e in events if e["event_type"] == _HEAL_EVENT]) == 1



# ---------------------------------------------------------------------------
# Guardrail 7 (chunk 4.1): the attach-time intent-snapshot refresh on rebuild.
#
# n7 above STUBS ``_fresh_start`` (deliberately, for ITS assertion), so the REAL
# guardrail-7 block inside ``_fresh_start``'s ``reuse_record_id`` branch is never
# entered by any earlier test.  n10/n11 close that gap: they drive the REAL
# ``_fresh_start`` through the heal path and fake ONLY the daemon create seam
# (``client.containers.run``) -- the SUBJECT (``_fresh_start``) is NEVER stubbed.
# ---------------------------------------------------------------------------


class _RunContainers:
    """Fake ``client.containers``: the read-through ``get``/``list`` the admission
    probes need, plus ``run`` (the single daemon create seam) returning a
    pre-baked container whose ``.attrs`` carry the inspect payload under test."""

    def __init__(self, items, run_result):
        self._items = list(items)
        self.run_result = run_result
        #: Every kwargs dict the daemon create seam (``run``) was called with,
        #: in order -- lets a test assert the resource intent that reached it.
        self.run_calls = []

    def get(self, container_id):
        for c in self._items:
            if c.id == container_id or c.name == container_id:
                return c
        raise LookupError(container_id)

    def list(self, all=False, filters=None):
        return list(self._items)

    def run(self, *args, **kwargs):
        self.run_calls.append(kwargs)
        return self.run_result


class _RunFakeDockerClient:
    """Fake docker client rich enough for the admission gate to ALLOW: ``ping``
    succeeds and ``containers.list`` resolves the workspace count to a small,
    under-limit number.  Distinct from ``_FakeDockerClient`` (which is the
    intentionally-minimal read-only double the reuse tests use)."""

    def __init__(self, created, live=()):
        self.containers = _RunContainers(live, created)

    def ping(self):  # admission: a client without ping is treated unreachable
        return True


def _real_heal_cm(created_attrs, *, workspace_id="w1"):
    """A manager that drives the REAL ``_fresh_start`` via ``heal_missing``.

    Only the daemon create seam (``client.containers.run``) is faked; the guard
    block under test runs for real.  ``_fresh_start`` is DELIBERATELY left as the
    real bound method (n7 stubs it; n10/n11 must NOT)."""
    created = _FakeContainer(_HEAL_NEW_ID, name="agent-x", attrs=created_attrs)
    cm = _make_cm(None, want=("none", "ro"), workspace_id=workspace_id)
    cm.client = _RunFakeDockerClient(created)
    cm.mem_limit = "1g"
    cm.cpu_quota = 100000
    return cm


_EMPTY_EVIDENCE_ATTRS = {
    "State": {"Status": "running"},
    "HostConfig": {},          # no NetworkMode -> snapshot network_mode ""
    "Mounts": [],              # no /workspace mount -> snapshot workspace_mode ""
}


def test_missing_heal_preserves_existing_snapshot_on_rebuild(monkeypatch):
    """Guardrail 7 FILL-ONCE: a record that ALREADY carries an evidence-bearing
    intent snapshot (an OPERATOR write, e.g. the resources PATCH endpoint) WINS --
    the healed rebuild must NOT overwrite it from the freshly created container.

    Drives the REAL ``_fresh_start`` reuse branch -- ``_fresh_start`` is NOT
    stubbed; only the daemon create seam is faked.
    """
    attrs = _attrs("none", False)  # network_mode/workspace_mode -> real evidence
    cm = _real_heal_cm(attrs)
    # The record's STORED snapshot is BOTH the heal's rebuild source AND the
    # operator intent that must survive the rebuild byte-identically.
    seed = dict(_HEAL_SNAPSHOT)
    _mint_record("w1", "agent-x", _HEAL_STALE, intent_snapshot=seed)

    spy = MagicMock(wraps=container_manager.attach_container)
    monkeypatch.setattr(container_manager, "attach_container", spy)

    result = cm.start(name="agent-x", heal_missing=True)

    assert result.get("error") is None
    assert result["id"] == _HEAL_NEW_ID
    assert spy.call_count == 1
    _args, kwargs = spy.call_args
    # FILL-ONCE: the record already carries an evidence-bearing snapshot, so the
    # derived fresh snapshot is NOT attached (the kwarg is omitted entirely).
    assert "intent_snapshot" not in kwargs
    # Round-trip: the operator's snapshot survives UNCHANGED.
    rec = load_record("w1", "rec-1", vault_root=_VAULT["root"])
    assert rec.intent_snapshot == seed


def test_missing_heal_omits_empty_snapshot_on_rebuild(monkeypatch):
    """ITEM-0 mitigation: when the live attrs carry NO evidence the heal OMITS the
    ``intent_snapshot`` kwarg, so the record's PRE-EXISTING good snapshot survives.

    Real ``_fresh_start`` path; only the daemon create seam is faked.
    """
    cm = _real_heal_cm(_EMPTY_EVIDENCE_ATTRS)
    _mint_record("w1", "agent-x", _HEAL_STALE)
    seed = {"network_mode": "bridge", "workspace_mode": "rw",
            "hardening": {"cap_drop": ["ALL"]}}
    update_record("w1", "rec-1", vault_root=_VAULT["root"],
                  intent_snapshot=dict(seed))

    spy = MagicMock(wraps=container_manager.attach_container)
    monkeypatch.setattr(container_manager, "attach_container", spy)

    result = cm.start(name="agent-x", heal_missing=True)

    assert result.get("error") is None
    assert result["id"] == _HEAL_NEW_ID
    assert spy.call_count == 1
    _args, kwargs = spy.call_args
    assert "intent_snapshot" not in kwargs  # explicit kwarg-absence (not truthiness)
    rec = load_record("w1", "rec-1", vault_root=_VAULT["root"])
    assert rec.intent_snapshot == seed      # the good snapshot is byte-identical


# ---------------------------------------------------------------------------
# Resources-endpoint Intent ownership.
#
# An OPERATOR write to the record's ``intent_snapshot`` (e.g. the resources
# PATCH endpoint, which may target a RESOURCE-class record) must SURVIVE the
# recreate and be HONOURED at rebuild.  These tests drive the REAL
# ``_fresh_start`` -- only the daemon create seam is faked -- so the guardrail-7
# FILL-ONCE rule and the reuse-site resource wiring are exercised end to end.
# ---------------------------------------------------------------------------

#: Evidence-bearing operator intent carrying the resource limits that must be
#: honoured at rebuild (mem_limit/cpu_quota also count as evidence).
_OPERATOR_INTENT = {
    "network_mode": "none",
    "workspace_mode": "ro",
    "mem_limit": "2g",
    "cpu_quota": 50000,
}


def _weak_hardening_attrs():
    """Attrs that MATCH the isolation policy but FAIL the hardening recipe.

    ``HostConfig.CapDrop`` is PRESENT but omits ``"ALL"``, so
    ``_hardening_conformance`` reports a real failing axis and
    ``_start_drift_decision`` returns the ``"recreate"`` action.
    """
    attrs = _attrs("none", False)
    attrs["HostConfig"]["CapDrop"] = []
    return attrs


def _mint_resource_record(workspace_id, name, docker_id, intent_snapshot=None):
    """Mint a RESOURCE-class record (own_lifecycle True) binding *name*."""
    vault = _VAULT["root"]
    create_record(
        workspace_id, LIFECYCLE_RESOURCE, OWNER_WORKSPACE,
        id="rec-1", name=name, vault_root=vault,
    )
    update_record(workspace_id, "rec-1", vault_root=vault, docker_id=docker_id)
    if intent_snapshot is not None:
        update_record(workspace_id, "rec-1", vault_root=vault,
                      intent_snapshot=intent_snapshot)


def test_operator_intent_mem_cpu_honoured_by_heal_rebuild():
    """THE correctness argument: an OPERATOR write to the record's intent is
    honoured at rebuild (mem_limit/cpu_quota reach the daemon create call) AND
    the SAME snapshot survives the rebuild untouched (FILL-ONCE).
    """
    attrs = _attrs("none", False)
    cm = _real_heal_cm(attrs)  # instance defaults mem_limit="1g", cpu_quota=100000
    seed = dict(_OPERATOR_INTENT)
    _mint_record("w1", "agent-x", _HEAL_STALE, intent_snapshot=seed)

    result = cm.start(name="agent-x", heal_missing=True)

    assert result.get("error") is None
    assert result["id"] == _HEAL_NEW_ID
    # HONOURED: the operator's resource intent reaches the daemon create call.
    run_kwargs = cm.client.containers.run_calls[-1]
    assert run_kwargs["mem_limit"] == "2g"
    assert run_kwargs["cpu_quota"] == 50000
    # SURVIVES: the operator's snapshot is never overwritten.
    rec = load_record("w1", "rec-1", vault_root=_VAULT["root"])
    assert rec.intent_snapshot == seed


def test_operator_recreate_of_resource_record_honours_and_preserves_intent(monkeypatch):
    """Endpoint-ish path: a RESOURCE record (own_lifecycle True) whose container
    is gone, recreated by the operator (allow_fresh).  The operator intent is
    honoured AND preserved.
    """
    attrs = _attrs("none", False)
    cm = _real_heal_cm(attrs)
    seed = dict(_OPERATOR_INTENT)
    _mint_resource_record("w1", "agent-x", _HEAL_STALE, intent_snapshot=seed)

    spy = MagicMock(wraps=container_manager.attach_container)
    monkeypatch.setattr(container_manager, "attach_container", spy)

    result = cm.start(name="agent-x", allow_fresh=True)

    assert result.get("error") is None
    assert result["id"] == _HEAL_NEW_ID
    run_kwargs = cm.client.containers.run_calls[-1]
    assert run_kwargs["mem_limit"] == "2g"
    assert run_kwargs["cpu_quota"] == 50000
    # FILL-ONCE: the record ALREADY carries an evidence-bearing operator intent,
    # so the rebuild does NOT overwrite it (the kwarg is omitted entirely).
    _args, kwargs = spy.call_args
    assert "intent_snapshot" not in kwargs
    rec = load_record("w1", "rec-1", vault_root=_VAULT["root"])
    assert rec.intent_snapshot == seed


def test_recreate_derives_snapshot_when_record_has_none(monkeypatch):
    """Derive branch: a record with NO stored snapshot rebuilds and the fresh
    container's (evidence-bearing) attrs are DERIVED into the record.
    """
    attrs = _attrs("none", False)
    cm = _real_heal_cm(attrs)
    # Record with NO snapshot and NO bound container -> allow_fresh recreate.
    create_record("w1", LIFECYCLE_PERSISTENT, OWNER_WORKSPACE, id="rec-1",
                  name="agent-x", vault_root=_VAULT["root"])

    spy = MagicMock(wraps=container_manager.attach_container)
    monkeypatch.setattr(container_manager, "attach_container", spy)

    result = cm.start(name="agent-x", allow_fresh=True)

    assert result.get("error") is None
    assert result["id"] == _HEAL_NEW_ID
    assert spy.call_count == 1
    expected = snapshot_from_attrs(attrs)
    _args, kwargs = spy.call_args
    assert kwargs.get("intent_snapshot") == expected
    rec = load_record("w1", "rec-1", vault_root=_VAULT["root"])
    assert rec.intent_snapshot == expected


def test_rebuild_without_resource_intent_falls_back_to_instance_defaults():
    """No operator resource intent -> no fabricated resources: the rebuild falls
    back to the INSTANCE defaults (the tool-supplied values).
    """
    attrs = _attrs("none", False)
    cm = _real_heal_cm(attrs)  # mem_limit="1g", cpu_quota=100000
    # Evidence-bearing snapshot for isolation only -- no mem/cpu keys.
    _mint_record("w1", "agent-x", _HEAL_STALE, intent_snapshot=dict(_HEAL_SNAPSHOT))

    result = cm.start(name="agent-x", heal_missing=True)

    assert result.get("error") is None
    run_kwargs = cm.client.containers.run_calls[-1]
    assert run_kwargs["mem_limit"] == "1g"
    assert run_kwargs["cpu_quota"] == 100000


@pytest.mark.parametrize(
    "site", ["allow-fresh-no-docker-id", "allow-fresh-container-missing",
             "drift-recreate"])
def test_recreate_sites_honour_operator_intent_mem_cpu(site):
    """EVERY reuse site that spawns a fresh container on the record's behalf
    passes the operator's stored resource intent through to ``_fresh_start``.
    """
    fresh = MagicMock(return_value={"id": _HEAL_NEW_ID, "name": "agent-x",
                                    "status": "created", "note": ""})
    if site == "allow-fresh-no-docker-id":
        cm = _make_cm(None, want=("none", "ro"))
        cm.client = _FakeDockerClient([])
        # Record exists but names no container (no docker_id) -> allow_fresh.
        create_record("w1", LIFECYCLE_PERSISTENT, OWNER_WORKSPACE, id="rec-1",
                      name="agent-x", vault_root=_VAULT["root"])
        update_record("w1", "rec-1", vault_root=_VAULT["root"],
                      intent_snapshot=dict(_OPERATOR_INTENT))
    elif site == "allow-fresh-container-missing":
        cm = _make_cm(None, want=("none", "ro"))
        cm.client = _FakeDockerClient([])
        # RESOURCE class keeps the docker_id (no unbound reconcile) so the
        # container-missing allowance site is the one that recreates.
        _mint_resource_record("w1", "agent-x", _HEAL_STALE,
                              intent_snapshot=dict(_OPERATOR_INTENT))
    else:  # drift-recreate
        ctr = _FakeContainer("c" * 16, labels={RECORD_LABEL_KEY: "rec-1"},
                             attrs=_weak_hardening_attrs())
        cm = _make_cm(ctr, want=("none", "ro"))
        _mint_record("w1", "agent-x", ctr.id,
                     intent_snapshot=dict(_OPERATOR_INTENT))
    cm.mem_limit = "1g"
    cm.cpu_quota = 100000
    cm._fresh_start = fresh

    result = cm.start(name="agent-x", allow_fresh=True)

    assert result.get("error") is None
    _args, kwargs = cm._fresh_start.call_args
    assert kwargs["mem_limit"] == "2g"
    assert kwargs["cpu_quota"] == 50000


def test_fresh_create_uses_instance_resource_values():
    """Agent-tool resource overrides are unchanged: with NO operator intent the
    fresh create uses the INSTANCE values the tool passed to the constructor.
    """
    cm = _real_heal_cm(_attrs("none", False))  # defaults 1g / 100000
    result = cm._fresh_start(
        image="img", name="fresh-x", note=None, worker_name=None,
        lifecycle_class=LIFECYCLE_PERSISTENT,
        network_mode="none", workspace_mode="ro")

    assert result.get("error") is None
    run_kwargs = cm.client.containers.run_calls[-1]
    assert run_kwargs["mem_limit"] == "1g"
    assert run_kwargs["cpu_quota"] == 100000


def test_fresh_create_uses_constructor_resource_overrides():
    """Tool-supplied constructor values win on a fresh create (no operator
    intent) -- the agent-tool override path is unchanged.
    """
    cm = _real_heal_cm(_attrs("none", False))
    cm.mem_limit = "3g"        # as a tool would pass to the constructor
    cm.cpu_quota = 200000
    result = cm._fresh_start(
        image="img", name="fresh-x", note=None, worker_name=None,
        lifecycle_class=LIFECYCLE_PERSISTENT,
        network_mode="none", workspace_mode="ro")

    assert result.get("error") is None
    run_kwargs = cm.client.containers.run_calls[-1]
    assert run_kwargs["mem_limit"] == "3g"
    assert run_kwargs["cpu_quota"] == 200000


def test_start_refusal_message_is_unbound_for_stale_docker_id():
    """The missing-container refusal maps to the UNBOUND message + code (F4).

    A record whose ``docker_id`` has no live container is reconciled to an
    unbound record: the refusal carries the shipped "record is unbound"
    message and the ``container_record_unbound`` code. The message is asserted
    as a fully LITERAL string so an incidental edit to the source turns this
    assertion RED.
    """
    cm = _missing_cm()
    _mint_record("w1", "agent-x", _HEAL_STALE)

    result = cm.start(name="agent-x")

    assert result["code"] == "container_record_unbound"
    assert result["error"] == (
        "Container record is unbound — the underlying container no longer "
        "exists. Recovery: an agent cannot heal it with `heal=True` once the "
        "record is unbound; an operator can recreate it via the record's "
        "recreate action."
    )


# ---------------------------------------------------------------------------
# (j) IMMEDIATE record reap on container REMOVAL (W3 item 3).
#
# ``ContainerManager.remove`` (success branch) deletes the durable record bound
# to the removed container AT ONCE -- best-effort, never raising -- mirroring the
# orphan sweeper's class gate.  ``stop`` alone NEVER reaps: the stop-only caller
# keeps owning the record (only removal ends the record's life).  The reap is
# fail-closed on record-vs-container ownership and never resurrects a
# lifecycle-owning (SERVICE) record.
#
# These tests drive the REAL ``remove``/``stop`` against the vault floor; only
# the Docker daemon is faked (``_FakeContainer`` / ``_FakeDockerClient``).  The
# status is set to ``"exited"`` so ``stop`` skips ``container.stop()`` /
# ``container.kill()`` (the minimal fake has neither); ``_agent_access_denial``
# is overridden to ``None`` because the ACCESS gate would otherwise reject the
# own-lifecycle classes (the reap gate under test is ``policy_for``, not the
# access gate).
# ---------------------------------------------------------------------------


def _remove_cm(container, workspace_id="w1"):
    """Manager wired for a real ``remove``/``stop`` against the vault floor."""
    cm = _make_cm(container, want=("none", "ro"), workspace_id=workspace_id)
    # The ACCESS gate (stop) is not the subject here; the reap gate is
    # ``policy_for`` directly.  Resolve it to "allowed" so stop() proceeds.
    cm._agent_access_denial = lambda *a, **k: None
    return cm


def _exited_record_container(container_id="c" * 16, name="agent-x"):
    """A removable fake whose ``status`` short-circuits stop()'s kill path."""
    ctr = _FakeContainer(container_id, name=name,
                         labels={RECORD_LABEL_KEY: "rec-1"})
    ctr.status = "exited"  # stop() skips container.stop()/kill()
    return ctr


def _load_rec():
    return load_record("w1", "rec-1", vault_root=_VAULT["root"])


def test_remove_deletes_the_bound_durable_record():
    """The success branch of remove() reaps the record once the container is gone."""
    ctr = _exited_record_container()
    cm = _remove_cm(ctr)
    _mint_record("w1", "agent-x", ctr.id)
    assert _load_rec() is not None  # minted and bound before removal

    result = cm.remove(ctr.id)

    assert result["status"] == "removed"
    assert ctr.removed == [{"force": True}]  # the container really was removed
    assert _load_rec() is None  # ...and the durable record went with it


def test_second_remove_is_a_noop_and_never_raises():
    """A repeat remove() finds no record (already reaped) and does NOT raise."""
    ctr = _exited_record_container()
    cm = _remove_cm(ctr)
    _mint_record("w1", "agent-x", ctr.id)

    assert cm.remove(ctr.id)["status"] == "removed"
    assert _load_rec() is None

    # delete_record() raises RecordNotFound for an absent record; the reap
    # swallows it, so the second removal still reports the idempotent status.
    second = cm.remove(ctr.id)
    assert second["status"] == "removed"
    assert _load_rec() is None


def test_remove_leaves_a_record_bound_to_a_different_container():
    """Ownership guard: a record whose docker_id != this container is untouched."""
    ctr = _exited_record_container()
    cm = _remove_cm(ctr)
    # The container's record LABEL resolves "rec-1", but the record names a
    # DIFFERENT docker id -- so it is not this container's record to reap.
    _mint_record("w1", "agent-x", "d" * 16)

    result = cm.remove(ctr.id)

    assert result["status"] == "removed"
    rec = _load_rec()
    assert rec is not None
    assert rec.docker_id == "d" * 16  # left intact (bound to another container)


def test_stop_alone_does_not_reap_the_record():
    """The stop-only path keeps the record: only removal ends its life."""
    ctr = _exited_record_container()
    cm = _remove_cm(ctr)
    _mint_record("w1", "agent-x", ctr.id)

    result = cm.stop(ctr.id)

    assert result["status"] == "stopped"
    assert _load_rec() is not None  # stop() never reaps


def test_remove_does_not_reap_a_service_class_record():
    """Own-lifecycle SERVICE records are skipped by the reap (class gate)."""
    ctr = _exited_record_container()
    cm = _remove_cm(ctr)
    create_record("w1", LIFECYCLE_SERVICE, OWNER_WORKSPACE, id="rec-1",
                  name="agent-x", vault_root=_VAULT["root"])
    update_record("w1", "rec-1", vault_root=_VAULT["root"], docker_id=ctr.id)

    result = cm.remove(ctr.id)

    assert result["status"] == "removed"
    assert _load_rec() is not None  # own-lifecycle -> NOT reaped




# ---------------------------------------------------------------------------
# Container-removal event emission (tenant event log)
# ---------------------------------------------------------------------------


def _capture_removal_events():
    """Subscribe a wildcard capture for CONTAINER_REMOVED; return (list, unsub)."""
    from agent.events import EventType, global_event_bus

    captured = []

    def capture(ev):
        if getattr(ev, "type", None) == EventType.CONTAINER_REMOVED:
            captured.append(ev)

    global_event_bus.subscribe(None, capture)
    return captured, (lambda: global_event_bus.unsubscribe(None, capture))


def test_remove_publishes_exactly_one_container_removed_event():
    """The success branch of remove() emits EXACTLY ONE container_removed event."""
    from agent.events import EventType

    ctr = _exited_record_container()
    cm = _remove_cm(ctr)
    _mint_record("w1", "agent-x", ctr.id)

    captured, unsub = _capture_removal_events()
    try:
        result = cm.remove(ctr.id)
    finally:
        unsub()

    assert result["status"] == "removed"
    assert len(captured) == 1  # EXACTLY one event -- not zero, not two
    ev = captured[0]
    assert ev.type == EventType.CONTAINER_REMOVED
    assert ev.type.value == "container_removed"
    assert ev.data["container_id"] == ctr.id
    assert ev.data["name"] == "agent-x"
    assert ev.data["workspace_id"] == "w1"
    assert ev.data["session_id"] == "s1"
    assert ev.data["reason"] == "removed"


def test_monitored_remove_is_noop_return_shape_unchanged():
    """remove() still returns the same shape; the publish adds no behaviour."""
    ctr = _exited_record_container()
    cm = _remove_cm(ctr)
    _mint_record("w1", "agent-x", ctr.id)

    captured, unsub = _capture_removal_events()
    try:
        result = cm.remove(ctr.id)
    finally:
        unsub()

    assert set(result.keys()) == {"status", "container_id"}
    assert result == {"status": "removed", "container_id": ctr.id}
    assert ctr.removed == [{"force": True}]  # log/return/flow unchanged
    assert len(captured) == 1


def test_removal_refused_by_access_gate_publishes_nothing():
    """A stop() access refusal short-circuits remove() -> NO event published."""
    ctr = _exited_record_container()
    cm = _remove_cm(ctr)
    # Override the allow stub: stop() now refuses -> remove() returns early.
    cm._agent_access_denial = lambda *a, **k: "service-class access denied"

    captured, unsub = _capture_removal_events()
    try:
        result = cm.remove(ctr.id)
    finally:
        unsub()

    assert result["status"] == "error"
    assert captured == []  # refused removal emits nothing
    assert ctr.removed == []  # the container was never touched


def test_removal_failure_publishes_nothing():
    """A container.remove() failure emits NO event (success branch not reached)."""
    ctr = _exited_record_container()
    cm = _remove_cm(ctr)
    ctr.remove = MagicMock(side_effect=RuntimeError("boom"))

    captured, unsub = _capture_removal_events()
    try:
        result = cm.remove(ctr.id)
    finally:
        unsub()

    assert result["status"] == "error"
    assert captured == []  # failed removal emits nothing



# ---------------------------------------------------------------------------
# ``keep_record`` flag: the recreate/rebind ladder PRESERVES the bound record
# ---------------------------------------------------------------------------
# The record-user-action *recreate* ladder is remove + start: it removes the old
# container then (re)starts a fresh one and rebinds the SAME record.  It passes
# ``keep_record=True`` so ``remove()`` does NOT reap the record it still needs.
# Every other caller keeps the default (terminal removal reaps).

def test_remove_keep_record_preserves_bound_record_and_emits_recreate_reason():
    """``remove(keep_record=True)`` keeps the record; event reason='recreate'."""
    from agent.events import EventType

    ctr = _exited_record_container()
    cm = _remove_cm(ctr)
    _mint_record("w1", "agent-x", ctr.id)
    assert _load_rec() is not None  # minted + bound before removal

    captured, unsub = _capture_removal_events()
    try:
        result = cm.remove(ctr.id, keep_record=True)
    finally:
        unsub()

    assert result == {"status": "removed", "container_id": ctr.id}
    assert ctr.removed == [{"force": True}]
    # The record SURVIVES the removal -- the ladder rebinds it to the new one.
    rec = _load_rec()
    assert rec is not None
    assert rec.docker_id == ctr.id  # unchanged until the ladder rebinds it
    # Exactly ONE event, tagged as a recreate (distinct from a terminal removal).
    assert len(captured) == 1
    assert captured[0].type == EventType.CONTAINER_REMOVED
    assert captured[0].data["reason"] == "recreate"


def test_remove_default_still_reaps_and_emits_removed_reason():
    """Contrast: a DEFAULT (terminal) removal reaps the record; reason='removed'."""
    ctr = _exited_record_container()
    cm = _remove_cm(ctr)
    _mint_record("w1", "agent-x", ctr.id)

    captured, unsub = _capture_removal_events()
    try:
        result = cm.remove(ctr.id)  # no flag -> terminal removal
    finally:
        unsub()

    assert result["status"] == "removed"
    assert _load_rec() is None  # reaped
    assert len(captured) == 1
    assert captured[0].data["reason"] == "removed"


def test_remove_keep_record_idempotent_second_call_never_raises():
    """A second ``keep_record`` removal is a no-op that never raises."""
    ctr = _exited_record_container()
    cm = _remove_cm(ctr)
    _mint_record("w1", "agent-x", ctr.id)

    first = cm.remove(ctr.id, keep_record=True)
    second = cm.remove(ctr.id, keep_record=True)

    assert first["status"] == "removed"
    assert second["status"] == "removed"  # idempotent; never raises
    assert _load_rec() is not None  # preserved across BOTH calls

