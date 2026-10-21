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
    container_manager._CONTAINER_REUSED_SEEN.clear()
    yield
    container_manager._EXEC_DRIFT_SEEN.clear()
    container_manager._START_DRIFT_SEEN.clear()
    container_manager._CONTAINER_REUSED_SEEN.clear()


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
    # No DRIFT event; the affirmative ``container_reused`` event fires ONCE.
    assert [e for e in events if e["event_type"] == _EVENT] == []
    assert len(events) == 1
    assert events[0]["event_type"] == "container_reused"
    assert events[0]["record_id"] == "rec-1"
    assert events[0]["payload"]["docker_id"] == ctr.id


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
    # The drift "warn" event PLUS the affirmative reuse event.
    drift_evs = [e for e in events if e["event_type"] == _EVENT]
    reused_evs = [e for e in events if e["event_type"] == "container_reused"]
    assert len(drift_evs) == 1
    assert drift_evs[0]["payload"]["decision"] == "warn"
    assert len(reused_evs) == 1
    assert len(events) == 2


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
    # No DRIFT event.  A ``container_reused`` event can fire ONLY on the
    # record-driven site: the raising-attrs container carries no record label,
    # so the label / workspace-label sites cannot resolve a record id.
    assert [e for e in events if e["event_type"] == _EVENT] == []
    assert all(e["event_type"] == "container_reused" for e in events)
    assert len(events) == (1 if site == "record" else 0)


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
    assert run_kwargs["memswap_limit"] == "2g"
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
    assert run_kwargs["memswap_limit"] == "2g"
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
    assert run_kwargs["memswap_limit"] == "1g"
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
    assert run_kwargs["memswap_limit"] == "1g"
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
    assert run_kwargs["memswap_limit"] == "3g"
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



# ---------------------------------------------------------------------------
# ``cleanup_workspace`` (module-level teardown): force-removes every labelled
# container, REAPS each removed container's bound record, and emits exactly ONE
# ``container_removed`` event (reason="workspace_cleanup").  A container
# force-removed while still RUNNING is reported at WARNING, naming it.
# ---------------------------------------------------------------------------


def _state_attrs(status):
    return {"State": {"Status": status, "Running": status == "running"},
            "HostConfig": {"NetworkMode": "none"}, "Mounts": []}


class _CleanupContainer(_FakeContainer):
    def __init__(self, cid, *, name="agent-x", labels=None, status="exited"):
        super().__init__(cid, name=name, labels=labels, attrs=_state_attrs(status))
        self.status = status
        self.stops = []

    def stop(self, timeout=None):
        self.stops.append(timeout)
        self.status = "exited"


class _CleanupContainers:
    def __init__(self, cs):
        self._cs = list(cs)

    def list(self, all=True, filters=None):
        return list(self._cs)


class _CleanupDocker:
    def __init__(self, cs):
        self.containers = _CleanupContainers(cs)


def test_cleanup_workspace_removes_reaps_and_emits_workspace_cleanup_reason(monkeypatch):
    """A clean cleanup: force-remove + reap the record + ONE workspace_cleanup event."""
    ctr = _CleanupContainer("c" * 16, labels={RECORD_LABEL_KEY: "rec-1"},
                            status="exited")
    _mint_record("w1", "agent-x", ctr.id)
    assert _load_rec() is not None  # minted + bound before cleanup

    log_calls = []
    monkeypatch.setattr(
        container_manager, "log",
        lambda level, logger, msg, *a, **k: log_calls.append((level, logger, msg)))

    captured, unsub = _capture_removal_events()
    try:
        res = container_manager.cleanup_workspace("w1", _CleanupDocker([ctr]))
    finally:
        unsub()

    assert res == {"removed": 1}
    assert ctr.removed == [{"force": True}]
    assert _load_rec() is None  # the bound record was REAPED
    assert len(captured) == 1
    assert captured[0].data["reason"] == "workspace_cleanup"
    assert captured[0].data["workspace_id"] == "w1"
    assert captured[0].data["container_id"] == ctr.id
    # An exited container is NOT a running force-removal: no WARNING.
    assert [c for c in log_calls if c[0] == "WARNING"] == []


def test_cleanup_workspace_warns_when_force_removing_running_container(monkeypatch):
    """A RUNNING container force-removed by cleanup is reported at WARNING, named."""
    ctr = _CleanupContainer("d" * 16, labels={RECORD_LABEL_KEY: "rec-1"},
                            status="running")
    _mint_record("w1", "agent-x", ctr.id)

    log_calls = []
    monkeypatch.setattr(
        container_manager, "log",
        lambda level, logger, msg, *a, **k: log_calls.append((level, logger, msg)))

    captured, unsub = _capture_removal_events()
    try:
        res = container_manager.cleanup_workspace("w1", _CleanupDocker([ctr]))
    finally:
        unsub()

    assert res == {"removed": 1}
    assert ctr.removed == [{"force": True}]
    assert _load_rec() is None  # still reaped
    assert len(captured) == 1
    assert captured[0].data["reason"] == "workspace_cleanup"
    warnings = [c for c in log_calls if c[0] == "WARNING"]
    assert len(warnings) == 1
    assert "agent-x" in warnings[0][2]
    assert ctr.id in warnings[0][2]


def test_cleanup_workspace_reap_and_emit_are_not_vacuous(monkeypatch):
    """Neutering either step proves the other is not vacuous (RED if absent)."""
    # (a) neuter the REAP step -> the bound record SURVIVES (proves the reap runs).
    ctr = _CleanupContainer("e" * 16, labels={RECORD_LABEL_KEY: "rec-1"},
                            status="exited")
    _mint_record("w1", "agent-x", ctr.id)
    monkeypatch.setattr(container_manager, "_reap_bound_record", lambda *a, **k: None)

    captured, unsub = _capture_removal_events()
    try:
        res = container_manager.cleanup_workspace("w1", _CleanupDocker([ctr]))
    finally:
        unsub()

    assert res == {"removed": 1}
    assert _load_rec() is not None  # reap neutralised -> record still there
    assert len(captured) == 1  # the emit step still fired

    # (b) neuter the EMIT step -> NO event despite a real removal (proves the emit).
    monkeypatch.setattr(container_manager, "_emit_container_removed",
                        lambda *a, **k: None)
    ctr2 = _CleanupContainer("f" * 16, status="exited")
    captured2, unsub2 = _capture_removal_events()
    try:
        res2 = container_manager.cleanup_workspace("w1", _CleanupDocker([ctr2]))
    finally:
        unsub2()

    assert res2 == {"removed": 1}
    assert captured2 == []



# ---------------------------------------------------------------------------
# W3-4: the heal-outcome refusal codes are DECLARED once and produced verbatim.
# ---------------------------------------------------------------------------


def test_heal_outcome_codes_are_a_single_declared_set():
    """The three heal-outcome codes live in ONE frozen set, byte-for-byte."""
    assert container_manager.HEAL_OUTCOME_CODES == frozenset({
        "container_record_unbound",
        "container_record_not_healable",
        "container_record_container_missing",
    })
    assert container_manager.HEAL_OUTCOME_UNBOUND == "container_record_unbound"
    assert container_manager.HEAL_OUTCOME_NOT_HEALABLE == "container_record_not_healable"
    assert (container_manager.HEAL_OUTCOME_CONTAINER_MISSING
            == "container_record_container_missing")


def test_producer_unbound_returns_the_declared_constant(events):
    """The (b) unbound producer yields HEAL_OUTCOME_UNBOUND."""
    cm = _make_cm(None, want=("none", "ro"))
    cm.client = _FakeDockerClient([])
    # A record present but naming NO container -> the unbound refusal.
    create_record("w1", LIFECYCLE_PERSISTENT, OWNER_WORKSPACE, id="rec-1",
                  name="agent-x", vault_root=_VAULT["root"])

    result = cm.start(name="agent-x")

    assert result["code"] == container_manager.HEAL_OUTCOME_UNBOUND
    assert result["code"] in container_manager.HEAL_OUTCOME_CODES


def test_producer_not_healable_returns_the_declared_constant(events):
    """The own-lifecycle class refusal yields HEAL_OUTCOME_NOT_HEALABLE."""
    cm = _missing_cm()
    create_record("w1", LIFECYCLE_RESOURCE, OWNER_WORKSPACE, id="rec-1",
                  name="agent-x", vault_root=_VAULT["root"])
    update_record("w1", "rec-1", vault_root=_VAULT["root"], docker_id=_HEAL_STALE)

    result = cm.start(name="agent-x", heal_missing=True)

    assert result["code"] == container_manager.HEAL_OUTCOME_NOT_HEALABLE
    assert result["code"] in container_manager.HEAL_OUTCOME_CODES


def test_producer_container_missing_returns_the_declared_constant(events):
    """The stale-docker-id fallback yields HEAL_OUTCOME_CONTAINER_MISSING."""
    cm = _missing_cm()
    _mint_record("w1", "agent-x", _HEAL_STALE)  # no evidence snapshot -> refuse

    result = cm.start(name="agent-x", heal_missing=True)

    assert result["code"] == container_manager.HEAL_OUTCOME_CONTAINER_MISSING
    assert result["code"] in container_manager.HEAL_OUTCOME_CODES


# ---------------------------------------------------------------------------
# W3-5: the affirmative ``container_reused`` event fires ONCE per identity pair.
# ---------------------------------------------------------------------------


def test_reuse_emits_container_reused_event_once_per_identity(events):
    """A reuse records ONE ``container_reused`` event (kind + payload pinned)."""
    ctr = _FakeContainer("c" * 16, labels={RECORD_LABEL_KEY: "rec-1"},
                         attrs=_attrs("none", False))
    cm, result = _start(ctr, want=("none", "ro"), site="record", name="agent-x")

    assert result["status"] == "reused"
    reused = [e for e in events if e["event_type"] == "container_reused"]
    assert len(reused) == 1
    ev = reused[0]
    assert ev["record_id"] == "rec-1"
    assert ev["workspace_id"] == "w1"
    assert ev["actor"] == container_manager._CONTAINER_REUSED_ACTOR
    assert ev["payload"]["docker_id"] == ctr.id


def test_container_reused_event_is_deduped_across_calls(events):
    """A SECOND identical reuse emits NO further ``container_reused`` event."""
    ctr = _FakeContainer("c" * 16, labels={RECORD_LABEL_KEY: "rec-1"},
                         attrs=_attrs("none", False))
    cm = _make_cm(ctr, want=("none", "ro"))
    _arrange(cm, ctr, "agent-x", "record")

    cm.start(name="agent-x")
    cm.start(name="agent-x")

    reused = [e for e in events if e["event_type"] == "container_reused"]
    assert len(reused) == 1


def test_container_reused_event_is_not_emitted_for_unresolvable_record(events):
    """A label-site reuse whose record id cannot be resolved emits NOTHING."""
    ctr = _FakeContainer("c" * 16, labels={}, attrs=_attrs("none", False))
    cm = _make_cm(ctr, want=("none", "ro"))
    _arrange(cm, ctr, "agent-x", "label")

    result = cm.start(name="agent-x")

    assert result["status"] == "reused"
    assert [e for e in events if e["event_type"] == "container_reused"] == []


def test_container_reused_emitter_is_not_vacuous(monkeypatch, events):
    """Neutering the emitter drops the event -> the call sites are load-bearing."""
    monkeypatch.setattr(
        container_manager.ContainerManager, "_emit_container_reused_once",
        lambda self, record_id, docker_id: None)
    ctr = _FakeContainer("c" * 16, labels={RECORD_LABEL_KEY: "rec-1"},
                         attrs=_attrs("none", False))
    cm, result = _start(ctr, want=("none", "ro"), site="record", name="agent-x")

    assert result["status"] == "reused"
    assert [e for e in events if e["event_type"] == "container_reused"] == []



# ===========================================================================
# W4 (wave9a) REFUSAL-ADMISSION INVARIANTS.
#
# CLAIM pinned here: a REFUSED / DENIED ``start`` NEVER starts the existing
# container and NEVER creates a new one.
#   * (A) on every refusal path the daemon start seam reached ONLY through
#     ``_ensure_running`` (L3975; ``container.start()`` L3983) is never invoked
#     on the discovered container, no fresh container is minted
#     (``_fresh_start`` L1860 not called) and the fake daemon's container set is
#     unchanged;
#   * (B) a drift-DENIED exited orphan discovered by label is not started, holds
#     no new slot and registers no new container;
#   * (C) NON-VACUOUS: a REUSE of the same fixture DOES reach the seam, and
#     forcing the SAME drifted fixture down the reuse branch DOES start it;
#   * (D) ADMISSION FAIL-CLOSED: an unknown/None status counts as slot-consuming
#     (the positive pin already exists elsewhere; only the load-bearing
#     sensitivity is proven here);
#   * (E) FINDING: a reuse response may hand back a container that is NOT
#     running (``_ensure_running`` is best-effort and never verified).
# ===========================================================================


class _StartSpyContainer(_FakeContainer):
    """Fake container that COUNTS every ``.start()`` daemon call.

    ``_FakeContainer`` has no ``start`` method at all, so the ONLY way to observe
    ``_ensure_running`` actually ordering the daemon to start an existing
    container is to spy on the container itself.
    """

    def __init__(self, container_id, name=None, labels=None, attrs=None):
        super().__init__(container_id, name=name, labels=labels, attrs=attrs)
        self.start_calls = 0

    def start(self):
        self.start_calls += 1
        self.status = "running"  # a real start transitions the container


def _fresh_spy(cm):
    """Replace ``_fresh_start`` with a spy: any fresh container shows up here."""
    cm._fresh_start = MagicMock(
        return_value={"id": "f" * 16, "name": "fresh",
                      "status": "created", "note": ""})
    return cm._fresh_start


def _ensure_spy(cm):
    """Wrap ``cm._ensure_running`` in a recorder (returns the call list)."""
    calls = []
    real = cm._ensure_running

    def _rec(container):
        calls.append(container)
        return real(container)

    cm._ensure_running = _rec
    return calls


def _image_attrs(network="none", workspace_rw=False):
    """Isolation attrs carrying a DIFFERENT image ref (forces image mismatch)."""
    attrs = _attrs(network, workspace_rw)
    attrs["Config"] = {"Image": "sha256:deadbeef"}
    return attrs


# --- (A) NO-REFUSAL-STARTS: the live-container refusal / deny paths ---------
_LIVE_REFUSALS = [
    ("record", "image"),
    ("workspace-label", "image"),
    ("label", "image"),
    ("record", "deny"),
    ("workspace-label", "deny"),
    ("label", "deny"),
]


@pytest.mark.parametrize("site,kind", _LIVE_REFUSALS)
def test_refusal_never_starts_existing_container(site, kind, events):
    """A refused reuse of a LIVE container never starts it and creates nothing."""
    if kind == "image":
        ctr = _StartSpyContainer("c" * 16, labels={RECORD_LABEL_KEY: "rec-1"},
                                 attrs=_image_attrs("none", False))
        kwargs = {"name": "agent-x", "image": "otherimg"}
    else:
        # Live is MORE permissive than the ("none","ro") policy -> drift DENY.
        ctr = _StartSpyContainer("c" * 16, labels={RECORD_LABEL_KEY: "rec-1"},
                                 attrs=_attrs("bridge", True))
        kwargs = {"name": "agent-x"}

    cm = _make_cm(ctr, want=("none", "ro"))
    _arrange(cm, ctr, "agent-x", site)
    fresh = _fresh_spy(cm)
    _ensure_spy(cm)
    containers_before = list(cm.client.containers.containers)
    cache_before = dict(cm._containers)

    result = cm.start(**kwargs)

    assert "error" in result
    assert "status" not in result            # NOT handed back as "reused"
    if kind == "deny":
        assert result["drift"]["decision"] == "deny"
    # (A1) the daemon start seam was NEVER reached for the existing container.
    assert ctr.start_calls == 0
    # (A2) no fresh container was minted.
    assert fresh.call_count == 0
    assert cm.client.containers.containers == containers_before
    # (A3) the cache gained no NEW container id (the record path only WARMS the
    #      cache to the pre-existing container).
    assert set(cm._containers.values()) <= (set(cache_before.values()) | {ctr.id})
    assert ctr.removed == []


_NO_CONTAINER_REFUSALS = {
    "unbound": {},
    "not_healable": {"heal_missing": True},
    "container_missing": {"heal_missing": True},
}


@pytest.mark.parametrize("case", list(_NO_CONTAINER_REFUSALS))
def test_refusal_without_live_container_creates_nothing(case, events):
    """A record whose docker_id has NO live container refuses and creates nothing.

    Covers the three record-path refusals that never reach a live container:
    ``container_record_unbound`` / ``container_record_not_healable`` /
    ``container_record_container_missing``.
    """
    cm = _make_cm(None, want=("none", "ro"))
    cm.client = _FakeDockerClient([])  # no live containers
    if case == "not_healable":
        create_record("w1", LIFECYCLE_RESOURCE, OWNER_WORKSPACE, id="rec-1",
                      name="agent-x", vault_root=_VAULT["root"])
        update_record("w1", "rec-1", vault_root=_VAULT["root"], docker_id="d" * 16)
    else:
        _mint_record("w1", "agent-x", "d" * 16)
    kwargs = _NO_CONTAINER_REFUSALS[case]

    fresh = _fresh_spy(cm)
    ensure = _ensure_spy(cm)

    result = cm.start(name="agent-x", **kwargs)

    assert "error" in result
    assert result["code"] in container_manager.HEAL_OUTCOME_CODES
    assert "status" not in result
    assert fresh.call_count == 0                  # nothing created
    assert ensure == []                           # no live container -> seam untouched
    assert cm._containers == {}                   # cache unchanged
    assert cm.client.containers.containers == []  # no new daemon container


# --- (B) DRIFT-DENIED ORPHAN HOLDS NO SLOT ----------------------------------


@pytest.mark.parametrize("site", ["workspace-label", "label"])
def test_drift_denied_exited_orphan_holds_no_slot(site, events):
    """A STOPPED container whose config DENIES reuse is left stopped, no slot."""
    ctr = _StartSpyContainer("c" * 16, labels={RECORD_LABEL_KEY: "rec-1"},
                             attrs=_attrs("bridge", True))  # MORE permissive
    ctr.status = "exited"                                  # an orphaned container
    cm = _make_cm(ctr, want=("none", "ro"))
    _arrange(cm, ctr, "agent-x", site)
    fresh = _fresh_spy(cm)
    cache_before = dict(cm._containers)
    n_before = len(cm.client.containers.containers)

    result = cm.start(name="agent-x")

    assert "error" in result
    assert result["drift"]["decision"] == "deny"
    assert ctr.status == "exited"               # STILL not running
    assert ctr.start_calls == 0                 # never started
    assert cm._containers == cache_before       # no new container registered
    assert fresh.call_count == 0                # no fresh container -> no new slot
    assert len(cm.client.containers.containers) == n_before


# --- (C) NON-VACUITY --------------------------------------------------------


@pytest.mark.parametrize("site", _SITES)
def test_reuse_reaches_the_ensure_running_seam(site, events):
    """(C-i) The SAME seam instrumented in (A) IS reached on a clean reuse."""
    ctr = _StartSpyContainer("c" * 16, labels={RECORD_LABEL_KEY: "rec-1"},
                             attrs=_attrs("none", False))  # matches ("none","ro")
    cm = _make_cm(ctr, want=("none", "ro"))
    _arrange(cm, ctr, "agent-x", site)
    ensure = _ensure_spy(cm)

    result = cm.start(name="agent-x")

    assert result["status"] == "reused"
    # The start-ensuring seam IS reached (the record site reaches it through
    # ``_reuse_container``; the others through the explicit call).
    assert ensure and all(c is ctr for c in ensure)


def test_drift_deny_suppresses_the_start_reuse_would_do(monkeypatch, events):
    """(C-ii) DIFFERENTIAL: the DENY, not the fixture, suppresses the start.

    The identical drifted+exited fixture goes down the REUSE branch once the
    drift decision is forced to "reuse" -- and THEN the exited container really
    IS started, proving ``start_calls`` observes the daemon start seam (so the
    zero-starts assertions in (A)/(B) are non-vacuous).
    """
    ctr = _StartSpyContainer("c" * 16, labels={RECORD_LABEL_KEY: "rec-1"},
                             attrs=_attrs("bridge", True))  # drift DENY
    ctr.status = "exited"
    cm = _make_cm(ctr, want=("none", "ro"))
    _arrange(cm, ctr, "agent-x", "workspace-label")

    denied = cm.start(name="agent-x")
    assert "error" in denied
    assert denied["drift"]["decision"] == "deny"
    assert ctr.start_calls == 0            # DENY -> no start
    assert ctr.status == "exited"

    forced = {"drifted": True, "decision": "warn",
              "reason": "forced_reuse_for_non_vacuity",
              "network_mode": "bridge", "workspace_mode": "ro",
              "source": "workspace-label"}
    monkeypatch.setattr(
        container_manager.ContainerManager, "_start_drift_decision",
        lambda self, *a, **k: ("reuse", forced))

    reused = cm.start(name="agent-x")
    assert reused["status"] == "reused"
    assert ctr.start_calls == 1            # REUSE -> the exited container starts
    assert ctr.status == "running"


# --- (D) ADMISSION FAIL-CLOSED (non-vacuity only; positive pin exists) -------


def test_unknown_status_admission_is_fail_closed_and_load_bearing(monkeypatch):
    """(D) An unknown/None container status counts as slot-CONSUMING.

    The POSITIVE pin already lives elsewhere and is deliberately NOT duplicated
    here:
      * tests/test_worker_stale_container_cleanup.py:282 (missing/None status)
      * tests/test_worker_stale_container_cleanup.py:345 (unknown status)
      * tests/test_lifecycle_policy.py:483/:489 (bogus / absent status)
      * tests/test_admission_gate.py:661 (explicit None status)
    This test only proves the classifier is LOAD-BEARING: neuter the shared
    ``consumes_workspace_slot`` predicate so an unknown status is treated as FREE
    and the SAME entry drops out of the consuming set.
    """
    cm = _make_cm(None, want=("none", "ro"))
    unknown = {"name": "agent-x", "status": None, "labels": {}}
    novel = {"name": "agent-y", "status": "weird-new-state", "labels": {}}
    terminal = {"name": "agent-z", "status": "exited", "labels": {}}

    # Fail-closed: unknown / novel statuses consume; the terminal one does not.
    assert cm._active_containers([unknown, novel, terminal]) == [unknown, novel]

    # Non-vacuity: neuter the SSOT predicate -> the unknowns are now "free".
    monkeypatch.setattr(container_manager, "consumes_workspace_slot",
                        lambda **kwargs: False)
    assert cm._active_containers([unknown, novel]) == []




# ===========================================================================
# W5 (wave9a) VERIFIED-RUNNING INVARIANT  ("finding E").
#
# CLAIM pinned here: a REUSE never claims a running container it did not
# observe.  ``_ensure_running`` now VERIFIES the post-start status and returns a
# bool (never raising); every reuse site gates BOTH the response (``"running":
# False`` on a container it could not verify) AND the warm-cache write on that
# verified result.
#
#   * ``_ensure_running`` returns True for a running container; False when
#     ``start()`` raises / the container never reaches ``running`` / the status
#     read itself raises; it WARNs and never raises out to its caller;
#   * for EACH reuse site: a container that will not run is surfaced (``"running":
#     False``) and its dead id is NOT warmed into the read-through cache, while a
#     genuinely running container reuses with NO ``running`` key and DOES warm the
#     cache (non-vacuity);
#   * DIFFERENTIAL: neutering the verified result (constant True == the pre-fix
#     "assume it worked") re-warms the dead id and drops the marker, proving the
#     gate is load-bearing.
# ===========================================================================


class _StuckContainer(_FakeContainer):
    """A container that is NOT running and whose ``start()`` does not help.

    Models a best-effort start that silently failed: ``start`` is COUNTED (so the
    daemon seam is observable) but never transitions the status.
    """

    def __init__(self, container_id, name=None, labels=None, attrs=None,
                 status="exited"):
        super().__init__(container_id, name=name, labels=labels, attrs=attrs)
        self.status = status
        self.start_calls = 0

    def start(self):
        self.start_calls += 1  # observed ... but the daemon start did not take


class _RaisingStartContainer(_StuckContainer):
    """A container whose ``start()`` raises (the start-failure path)."""

    def start(self):
        self.start_calls += 1
        raise RuntimeError("start failed")


class _RaisingStatusContainer(_FakeContainer):
    """A container whose ``status`` read raises (defensive-read path)."""

    def __init__(self, container_id, name=None, labels=None, attrs=None):
        # Deliberately avoid ``self.status = ...`` (the property has no setter).
        self.id = container_id
        self.name = name or container_id
        self.image = _FakeImageRef()
        self.labels = dict(labels or {})
        self.attrs = attrs if attrs is not None else _attrs("none", False)
        self.removed = []

    @property
    def status(self):
        raise RuntimeError("status unavailable")


def _log_spy(monkeypatch):
    """Capture ``container_manager.log`` calls as a list of arg-tuples."""
    seen = []
    monkeypatch.setattr(container_manager, "log",
                        lambda *a, **k: seen.append(a))
    return seen


# --- ``_ensure_running`` verification contract ------------------------------


def test_ensure_running_true_for_running_container():
    ctr = _FakeContainer("c" * 16, attrs=_attrs("none", False))  # status running
    cm = _make_cm(ctr, want=("none", "ro"))
    assert cm._ensure_running(ctr) is True


def test_ensure_running_false_when_start_raises(monkeypatch):
    seen = _log_spy(monkeypatch)
    ctr = _RaisingStartContainer("c" * 16, attrs=_attrs("none", False))
    cm = _make_cm(ctr, want=("none", "ro"))
    assert cm._ensure_running(ctr) is False  # never raises out to the caller
    assert ctr.start_calls == 1
    assert any(a and a[0] == "WARNING" for a in seen)


def test_ensure_running_false_when_still_not_running(monkeypatch):
    seen = _log_spy(monkeypatch)
    ctr = _StuckContainer("c" * 16, attrs=_attrs("none", False))
    cm = _make_cm(ctr, want=("none", "ro"))
    assert cm._ensure_running(ctr) is False
    assert ctr.start_calls == 1
    msgs = " ".join(str(a[-1]) for a in seen if a and a[0] == "WARNING")
    assert "still not running" in msgs
    assert ctr.id in msgs  # the WARNING names the offending container


def test_ensure_running_never_raises_when_status_read_raises(monkeypatch):
    seen = _log_spy(monkeypatch)
    ctr = _RaisingStatusContainer("c" * 16)
    cm = _make_cm(ctr, want=("none", "ro"))
    assert cm._ensure_running(ctr) is False  # defensive read -> False, no raise
    assert any(a and a[0] == "WARNING" for a in seen)


# --- every reuse site surfaces only VERIFIED state --------------------------

# The workspace-label path never warms the read-through cache; the record and
# label paths do.  Encoding that here keeps the non-vacuity control honest.
_REUSE_SITE_WARMS_CACHE = {
    "record": True,
    "workspace-label": False,
    "label": True,
}


@pytest.mark.parametrize("site", list(_REUSE_SITE_WARMS_CACHE))
def test_non_running_reuse_surfaces_unverified_state(site, events):
    """A reuse of a container that will not run reports ``"running": False``.

    The container is STOPPED and its ``start()`` does not take, so the reuse must
    NOT claim a running container -- and must NOT warm the cache with the dead id
    it could not verify.
    """
    ctr = _StuckContainer("c" * 16, labels={RECORD_LABEL_KEY: "rec-1"},
                          attrs=_attrs("none", False))  # matches ("none", "ro")
    cm = _make_cm(ctr, want=("none", "ro"))
    _arrange(cm, ctr, "agent-x", site)

    result = cm.start(name="agent-x")

    assert result["status"] == "reused"
    assert result.get("running") is False          # unverified state surfaced
    assert ctr.id not in cm._containers.values()   # dead id never warmed
    assert cm._containers.get("agent-x") != ctr.id
    assert ctr.start_calls >= 1                    # the start WAS attempted


@pytest.mark.parametrize("site", list(_REUSE_SITE_WARMS_CACHE))
def test_running_reuse_claims_no_unverified_state(site, events):
    """NON-VACUITY: a genuinely running container reuses with NO ``running`` key."""
    ctr = _FakeContainer("c" * 16, labels={RECORD_LABEL_KEY: "rec-1"},
                         attrs=_attrs("none", False))  # already running
    cm = _make_cm(ctr, want=("none", "ro"))
    _arrange(cm, ctr, "agent-x", site)

    result = cm.start(name="agent-x")

    assert result["status"] == "reused"
    assert "running" not in result                 # verified -> byte-identical
    if _REUSE_SITE_WARMS_CACHE[site]:
        assert cm._containers.get("agent-x") == ctr.id


@pytest.mark.parametrize("site", ["record", "label"])
def test_verified_running_gate_is_load_bearing(site, monkeypatch, events):
    """DIFFERENTIAL: neuter the verification and the assertions above go FALSE.

    Forcing the verified result to a constant ``True`` (the pre-W5 "assume the
    start worked" behaviour) re-warms the dead id and drops the ``running``
    marker -- proving BOTH the verified return (change 1) and the call-site gate
    (change 3) are load-bearing.
    """
    ctr = _StuckContainer("c" * 16, labels={RECORD_LABEL_KEY: "rec-1"},
                          attrs=_attrs("none", False))
    cm = _make_cm(ctr, want=("none", "ro"))
    _arrange(cm, ctr, "agent-x", site)

    # Honest: the stopped container is surfaced and the cache stays clean.
    resp = cm.start(name="agent-x")
    assert resp.get("running") is False
    assert ctr.id not in cm._containers.values()

    # Mutation: the verified result is replaced by "we assume it worked".
    monkeypatch.setattr(cm, "_ensure_running", lambda c: True)
    cm._containers.clear()
    resp2 = cm.start(name="agent-x")
    assert "running" not in resp2                  # the lie is now hidden
    assert cm._containers.get("agent-x") == ctr.id  # ...and the dead id warmed

