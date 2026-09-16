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
    assert r1["code"] == "container_record_container_missing"

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
    assert r2["code"] == "container_record_container_missing"
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
    assert r1["code"] == "container_record_container_missing"

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
_HEAL_AUDIT_OK = container_manager._HEAL_AUDIT_RECREATED
_HEAL_AUDIT_FAIL = container_manager._HEAL_AUDIT_REFUSED
_HEAL_NEW_ID = "e" * 16
_HEAL_STALE = "d" * 16


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
    _mint_record("w1", "agent-x", _HEAL_STALE)

    result = cm.start(name="agent-x", heal_missing=True)

    assert result.get("error") is None
    assert result["id"] == _HEAL_NEW_ID
    assert result["status"] == "created"
    # Exactly ONE rebuild, bound to the EXISTING record, from the CURRENT policy.
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

    assert result["code"] == "container_record_container_missing"
    assert cm._fresh_start.call_count == 0
    assert [e for e in events if e["event_type"] == _HEAL_EVENT] == []
    assert [a for a in audits if a["event"] == _HEAL_AUDIT_OK] == []
    # Non-vacuous: the ordinary stale-docker-id drift signal DID fire.
    assert len([e for e in events if e["event_type"] == _ABSENT_EVENT]) == 1


def test_missing_heal_is_attempted_once_only_when_it_fails(events, audits):
    """A REFUSED rebuild is terminal: the memo stops a second attempt."""
    cm = _missing_cm()
    cm._fresh_start = MagicMock(return_value={"error": "admission denied"})
    _mint_record("w1", "agent-x", _HEAL_STALE)

    r1 = cm.start(name="agent-x", heal_missing=True)
    r2 = cm.start(name="agent-x", heal_missing=True)

    assert r1["code"] == "container_record_container_missing"
    assert r2["code"] == "container_record_container_missing"
    # Exactly ONE attempt across BOTH calls: the failed heal is memoised at
    # ATTEMPT time, so a later call never retries.
    assert cm._fresh_start.call_count == 1
    assert [e for e in events if e["event_type"] == _HEAL_EVENT] == []
    # Non-vacuous: the ordinary refusal signal fired (once, memoised).
    assert len([a for a in audits
                if a["event"] == "CONTAINER_START_STALE_DOCKER_ID"]) == 1


def test_missing_heal_refuses_when_policy_uncomputable(events, audits):
    """An uncomputable policy invents nothing: refuse via the heal signal."""
    cm = _missing_cm()
    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        if calls["n"] > 1:  # start()'s own compute succeeds; the HEAL recompute fails
            raise RuntimeError("no policy")
        return ("none", "ro")

    cm._compute_config = _boom
    _mint_record("w1", "agent-x", _HEAL_STALE)

    result = cm.start(name="agent-x", heal_missing=True)

    assert result["code"] == "container_record_container_missing"
    assert cm._fresh_start.call_count == 0  # never built an un-policied container
    refused = [e for e in events if e["event_type"] == _HEAL_REFUSED_EVENT]
    assert len(refused) == 1
    assert refused[0]["payload"]["reason"] == "container_missing"
    assert refused[0]["payload"]["detail"] == "config_uncomputable"
    assert refused[0]["payload"]["old_docker_id"] == _HEAL_STALE
    failed = [a for a in audits if a["event"] == _HEAL_AUDIT_FAIL]
    assert len(failed) == 1
    assert "detail=config_uncomputable" in failed[0]["data"]


def test_missing_heal_refused_for_own_lifecycle_class(events, audits):
    """Classes that OWN their lifecycle (RESOURCE/SERVICE) are never auto-healed."""
    cm = _missing_cm()
    create_record("w1", LIFECYCLE_RESOURCE, OWNER_WORKSPACE, id="rec-1",
                  name="agent-x", vault_root=_VAULT["root"])
    update_record("w1", "rec-1", vault_root=_VAULT["root"], docker_id=_HEAL_STALE)

    result = cm.start(name="agent-x", heal_missing=True)

    assert result["code"] == "container_record_container_missing"
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


def test_missing_heal_rebuilds_from_current_policy_then_reuses_clean(events):
    """The rebuild uses the CURRENT policy; the rebound container then reuses clean."""
    cm = _missing_cm()
    _mint_record("w1", "agent-x", _HEAL_STALE)

    def _fake_fresh(**kwargs):
        # Mirror _fresh_start's real side effect: rebind the EXISTING record to the
        # fresh container, and publish it live with CURRENT-policy isolation.
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
    _mint_record("w1", "agent-x", _HEAL_STALE)

    r1 = cm.start(name="agent-x", heal_missing=True)
    assert r1["id"] == _HEAL_NEW_ID
    assert cm._fresh_start.call_count == 1

    # The record STILL names the original stale docker_id (no rebind here): a
    # second start must NOT heal again.
    r2 = cm.start(name="agent-x", heal_missing=True)
    assert r2["code"] == "container_record_container_missing"
    assert cm._fresh_start.call_count == 1


def test_missing_heal_fires_even_when_record_state_is_creating(events):
    """Negative pin: the heal gate does NOT consult the record's ``state`` field."""
    cm = _missing_cm()
    create_record("w1", LIFECYCLE_PERSISTENT, OWNER_WORKSPACE, id="rec-1",
                  name="agent-x", vault_root=_VAULT["root"])
    update_record("w1", "rec-1", vault_root=_VAULT["root"],
                  docker_id=_HEAL_STALE, state="creating")

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

    def get(self, container_id):
        for c in self._items:
            if c.id == container_id or c.name == container_id:
                return c
        raise LookupError(container_id)

    def list(self, all=False, filters=None):
        return list(self._items)

    def run(self, **kwargs):
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


def test_missing_heal_refresh_snapshot_on_rebuild(monkeypatch):
    """Guardrail 7: a healed rebuild attaches the FRESH snapshot derived from the
    live container's attrs (evidence-bearing) and it round-trips into the record.

    Drives the REAL ``_fresh_start`` reuse branch -- ``_fresh_start`` is NOT
    stubbed; only the daemon create seam is faked.
    """
    attrs = _attrs("none", False)  # network_mode/workspace_mode -> real evidence
    cm = _real_heal_cm(attrs)
    _mint_record("w1", "agent-x", _HEAL_STALE)

    spy = MagicMock(wraps=container_manager.attach_container)
    monkeypatch.setattr(container_manager, "attach_container", spy)

    result = cm.start(name="agent-x", heal_missing=True)

    assert result.get("error") is None
    assert result["id"] == _HEAL_NEW_ID
    assert spy.call_count == 1
    expected = snapshot_from_attrs(attrs)
    _args, kwargs = spy.call_args
    assert kwargs.get("intent_snapshot") == expected
    # Round-trip: the same snapshot is persisted on the record (default vault).
    rec = load_record("w1", "rec-1", vault_root=_VAULT["root"])
    assert rec.intent_snapshot == expected


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



def test_start_refusal_message_unchanged_for_stale_docker_id():
    """The missing-container refusal MESSAGE is pinned byte-for-byte (F4).

    The refusal text is asserted as a fully LITERAL string built from this
    test's own known inputs, so an incidental edit to the source f-string
    (e.g. ``names`` -> ``references``) turns this assertion RED.
    """
    cm = _missing_cm()
    _mint_record("w1", "agent-x", _HEAL_STALE)

    result = cm.start(name="agent-x")

    assert result["error"] == (
        "Container 'agent-x' record rec-1 names docker_id "
        "'dddddddddddddddd' but no such container exists."
    )
