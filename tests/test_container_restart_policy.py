"""Hermetic tests for the container restart-policy feature.

The feature threads a per-lifecycle-class Docker restart policy from the policy
table (``thoughtmachine.container_record.lifecycle_policy``) into every
container CREATE site, records the intended policy on the container record
(v2, ``restart_policy``), and refuses to REUSE a drifted container whose LIVE
restart policy is MORE permissive than the class policy.

Everything here is hermetic: a fake docker client records the
``containers.run(...)`` kwargs at every create site and never touches a daemon.

Covered:
  (a) the REPRODUCER -- a freshly created container is actually ASKED for the
      class restart policy (the pre-feature code passed no ``restart_policy``);
  (b) per-class create-site kwargs at ALL FOUR create sites
      (ContainerManager.start, create_hardened_container,
      ResourceContainerManager's legacy create, DockerExecutor._ensure_container);
  (c) start-path restart-policy drift via ``_start_drift_decision`` in BOTH
      directions (more-permissive -> DENY without touching the container;
      not-more-permissive -> REUSE with a drift detail);
  (d) all four lifecycle classes for the policy VALUE and the drift axis;
  (e) v1 -> v2 migration: a record dict with no ``restart_policy`` loads as
      ``None`` and the drift axis falls back to the CLASS policy; an unknown
      class skips the axis.

The real-daemon end-to-end proof lives in
``tests/docker/test_restart_policy_live.py`` and skips without a daemon.
"""

from __future__ import annotations

import copy
from unittest.mock import MagicMock, patch

import pytest

import infra.container_manager as container_manager
import infra.resource_container_manager as rcm
from infra.container_manager import ContainerManager
from infra.container_registry import ContainerProfile, create_hardened_container
from security import admission_gate
from security.admission_gate import Allow
import thoughtmachine.container_record as container_record
from thoughtmachine.container_record import (
    LIFECYCLE_CLASSES,
    LIFECYCLE_EPHEMERAL,
    LIFECYCLE_PERSISTENT,
    LIFECYCLE_RESOURCE,
    LIFECYCLE_SERVICE,
    RECORD_LABEL_KEY,
    SCHEMA_VERSION_CURRENT,
    docker_restart_policy,
    policy_for,
)
from thoughtmachine.container_record.drift import (
    CLASS_RESTART_POLICY,
    EVENT_RESTART_POLICY_MISMATCH,
    classify_drift,
)
from thoughtmachine.container_record.models import OWNER_WORKSPACE, Record

#: The expected restart-policy NAME per lifecycle class (feature SSOT).
_RESTART_POLICY_BY_CLASS = {
    LIFECYCLE_PERSISTENT: "unless-stopped",
    LIFECYCLE_EPHEMERAL: "no",
    LIFECYCLE_RESOURCE: "unless-stopped",
    LIFECYCLE_SERVICE: "unless-stopped",
}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeImageRef:
    def __init__(self, image_id="sha256:deadbeef"):
        self.id = image_id

    def __str__(self):
        return self.id


class _FakeCtr:
    """Records the mutating calls a create site might (wrongly) make."""

    def __init__(self, container_id="c-run-1", name=None, labels=None, attrs=None):
        self.id = container_id
        self.name = name or container_id
        self.image = _FakeImageRef()
        self.status = "created"
        self.labels = dict(labels or {})
        self.attrs = attrs if attrs is not None else {"State": {"Status": "created"}}
        self.started = []
        self.stopped = []
        self.removed = []

    def start(self):
        self.started.append(True)
        self.status = "running"

    def stop(self, timeout=None):
        self.stopped.append(timeout)

    def remove(self, **kwargs):
        self.removed.append(kwargs)

    def reload(self):
        pass

    def exec_run(self, **kwargs):
        return (0, (b"", b""))


class _FakeContainers:
    def __init__(self, containers=None):
        self.containers = list(containers or [])
        self.run_calls = []
        self.list_calls = []

    def list(self, all=False, filters=None):
        self.list_calls.append({"all": all, "filters": filters})
        return copy.copy(self.containers)

    def get(self, container_id):
        for c in self.containers:
            if c.id == container_id or c.name == container_id:
                return c
        raise LookupError(container_id)

    def run(self, *args, **kwargs):
        self.run_calls.append({"args": args, "kwargs": kwargs})
        name = kwargs.get("name") or "run-ctr"
        ctr = _FakeCtr("c-run-1", name=name, labels=kwargs.get("labels") or {})
        self.containers.append(ctr)
        return ctr


class _FakeDockerClient:
    def __init__(self, containers=None):
        self.containers = _FakeContainers(containers or [])


class _FakeDockerModule:
    def __init__(self, client):
        self._client = client

    def from_env(self, **kwargs):
        return self._client


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

    The record store resolves its vault lazily from
    ``THOUGHTMACHINE_VAULT_ROOT`` (else ``~/.thoughtmachine``) when a call does
    not pass an explicit ``vault_root``; the shared default would leak
    ``(workspace, name) -> record`` identity across tests, making a fresh create
    spuriously REUSE an earlier test's record.  The ``(workspace, name) ->
    record id`` index and the notes memos are module-scoped, so clear them too.
    """
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))

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
    """Never let the registry facade intercept the stubbed start() paths."""
    monkeypatch.setattr(
        container_manager, "is_registry_active", lambda *a, **k: False
    )


@pytest.fixture(autouse=True)
def events(monkeypatch):
    """Capture record events so drift emission never writes to a real vault."""
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

    monkeypatch.setattr(
        container_record, "append_event", _fake_append, raising=True
    )
    return recorded


# ---------------------------------------------------------------------------
# ContainerManager.start scaffolding
# ---------------------------------------------------------------------------


def _make_start_cm(client, workspace_id="ws-rp"):
    cm = ContainerManager.__new__(ContainerManager)
    cm.workspace_path = "/tmp/ws-test"
    cm.session_id = "s1"
    cm.workspace_id = workspace_id
    cm.session_permissions = {}
    cm._session_config = {"use_container_registry": False}
    cm.image = "agent-executor"
    cm.mem_limit = "1g"
    cm.cpu_quota = 100000
    cm._containers = {}
    cm.client = client
    cm._compute_config = lambda *a, **k: ("none", "ro")
    cm.vault_root = "/tmp/tm-vault-test"
    cm.container_notes = {}
    cm.max_containers = 4
    cm.workspace_config = {"max_containers": 4}
    cm.dockerfile_path = None
    return cm


# ---------------------------------------------------------------------------
# (a) REPRODUCER: create sites ASK docker for the class restart policy.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lifecycle_class", list(LIFECYCLE_CLASSES))
def test_reproducer_container_manager_start_passes_restart_policy(lifecycle_class):
    """A fresh create MUST carry ``restart_policy`` (the old code passed none)."""
    client = _FakeDockerClient()
    cm = _make_start_cm(client)

    cm.start(
        image="agent-executor",
        name="agent-restart-repro",
        lifecycle_class=lifecycle_class,
    )

    kwargs = client.containers.run_calls[-1]["kwargs"]
    assert "restart_policy" in kwargs, (
        "container create did not ask for a restart policy (pre-feature bug)"
    )
    assert kwargs["restart_policy"] == docker_restart_policy(lifecycle_class)
    assert kwargs["restart_policy"]["Name"] == _RESTART_POLICY_BY_CLASS[lifecycle_class]


# ---------------------------------------------------------------------------
# (b) PER-CLASS CREATE-SITE KWARGS -- all FOUR create sites.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lifecycle_class", list(LIFECYCLE_CLASSES))
def test_site1_container_manager_start(lifecycle_class):
    client = _FakeDockerClient()
    cm = _make_start_cm(client)
    cm.start(image="agent-executor", name="n", lifecycle_class=lifecycle_class)
    kwargs = client.containers.run_calls[-1]["kwargs"]
    assert kwargs["restart_policy"] == docker_restart_policy(lifecycle_class)


@pytest.mark.parametrize(
    "container_type, lifecycle_arg, expected_class",
    [
        ("user", None, LIFECYCLE_PERSISTENT),
        ("resource", None, LIFECYCLE_RESOURCE),
        ("user", LIFECYCLE_EPHEMERAL, LIFECYCLE_EPHEMERAL),
        ("resource", LIFECYCLE_SERVICE, LIFECYCLE_SERVICE),
    ],
)
def test_site2_create_hardened_container(container_type, lifecycle_arg, expected_class):
    client = _FakeDockerClient()
    profile = ContainerProfile(
        image="agent-executor",
        command=["tail", "-f", "/dev/null"],
        container_type=container_type,
        network_mode="none",
    )
    # workspace_id=None -> PURE create (no admission / no daemon probes).
    create_hardened_container(
        client, profile, "c2", workspace_id=None, lifecycle_class=lifecycle_arg
    )
    kwargs = client.containers.run_calls[-1]["kwargs"]
    assert kwargs["restart_policy"] == docker_restart_policy(expected_class)
    assert kwargs["restart_policy"]["Name"] == _RESTART_POLICY_BY_CLASS[expected_class]


def test_site3_resource_container_manager_legacy_create(monkeypatch):
    client = _FakeDockerClient()
    monkeypatch.setattr(rcm, "docker", _FakeDockerModule(client))
    monkeypatch.setattr(rcm, "_ensure_resource_image", lambda: True)
    monkeypatch.setattr(rcm, "is_registry_active", lambda cfg: False)

    mgr = rcm.ResourceContainerManager(
        workspace_id="ws-rp",
        workspace_path="/tmp/tm-rp-ws",
        network_mode="none",
        vault_root="/tmp/tm-rp-vault",
        session_config=None,
        session_id="s-rp",
    )
    mgr.ensure_container()

    assert len(client.containers.run_calls) == 1
    kwargs = client.containers.run_calls[0]["kwargs"]
    assert kwargs["restart_policy"] == docker_restart_policy(LIFECYCLE_RESOURCE)


def test_site4_docker_executor_ensure_container(monkeypatch):
    from docker_executor import DockerExecutor

    try:
        from docker.errors import NotFound
    except Exception:  # pragma: no cover - docker SDK absent
        NotFound = LookupError

    client = MagicMock()
    client.containers.run.return_value = _FakeCtr("c-exec-1")
    client.containers.get.side_effect = NotFound("nope")
    client.volumes.get_or_create.return_value = MagicMock()

    monkeypatch.setattr(DockerExecutor, "_ensure_image", lambda self, *a, **k: None)
    monkeypatch.setattr(
        DockerExecutor, "_compute_container_config", lambda self: ("none", "ro")
    )
    monkeypatch.setattr(
        admission_gate, "admit", lambda request, *, probes=None: Allow(spec=request.spec)
    )

    with patch("docker_executor.docker.from_env", return_value=client):
        executor = DockerExecutor(
            workspace_path="/tmp/tm-rp-ws",
            image="agent-executor-test",
            network="none",
            mem_limit="128m",
            cpu_quota=50000,
            session_permissions={"container": True},
            workspace_id="ws-rp",
        )

    executor._ensure_container()

    kwargs = client.containers.run.call_args.kwargs
    assert "restart_policy" in kwargs
    assert kwargs["restart_policy"] == docker_restart_policy(LIFECYCLE_PERSISTENT)


# ---------------------------------------------------------------------------
# (d) policy VALUE per class (policy table + docker dict form).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lifecycle_class", list(LIFECYCLE_CLASSES))
def test_policy_value_per_class(lifecycle_class):
    expected = _RESTART_POLICY_BY_CLASS[lifecycle_class]
    assert policy_for(lifecycle_class).restart_policy == expected
    assert docker_restart_policy(lifecycle_class) == {
        "Name": expected,
        "MaximumRetryCount": 0,
    }


# ---------------------------------------------------------------------------
# (c)/(d) start-path restart-policy drift via _start_drift_decision.
# ---------------------------------------------------------------------------


def _drift_attrs(restart_name):
    """Attrs whose isolation MATCHES the ("none", "ro") policy; only the
    restart policy varies so the restart axis is the sole drift driver."""
    return {
        "State": {"Status": "running"},
        "HostConfig": {
            "NetworkMode": "none",
            "RestartPolicy": {"Name": restart_name, "MaximumRetryCount": 0},
        },
        "Mounts": [{"Destination": "/workspace", "RW": False}],
    }


def _make_drift_cm(container, workspace_id="w1"):
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
    cm.client = _FakeDockerClient([container])
    cm._remove_container = MagicMock()
    cm._audit = MagicMock()
    cm._get_max_containers = lambda: 10
    cm._find_by_labels = lambda n: None
    cm.list_containers = lambda: []
    return cm


def test_drift_more_permissive_denied_without_mutation():
    """expected unless-stopped, live always (more permissive) -> DENY."""
    ctr = _FakeCtr("c" * 16, attrs=_drift_attrs("always"))
    cm = _make_drift_cm(ctr)

    decision, payload = cm._start_drift_decision(
        ctr, "none", "ro", "label", lifecycle_class=LIFECYCLE_PERSISTENT
    )

    assert decision == "deny"
    assert payload["error"]
    drift = payload["drift"]
    assert drift["decision"] == "deny"
    assert drift["reason"] == "restart_policy_more_permissive"
    assert drift["restart_policy"] == {"expected": "unless-stopped", "actual": "always"}
    # The drifted container is NEVER mutated and never handed a create call.
    assert ctr.removed == [] and ctr.stopped == [] and ctr.started == []
    assert cm.client.containers.run_calls == []
    assert cm._remove_container.called is False


def test_drift_not_more_permissive_reused_with_detail():
    """expected unless-stopped, live no (less permissive) -> REUSE + detail."""
    ctr = _FakeCtr("c" * 16, attrs=_drift_attrs("no"))
    cm = _make_drift_cm(ctr)

    decision, payload = cm._start_drift_decision(
        ctr, "none", "ro", "label", lifecycle_class=LIFECYCLE_PERSISTENT
    )

    assert decision == "reuse"
    assert payload["drifted"] is True
    assert payload["decision"] == "warn"
    assert payload["reason"] == "restart_policy_differs_not_more_permissive"
    assert payload["restart_policy"] == {"expected": "unless-stopped", "actual": "no"}
    assert ctr.removed == [] and cm._remove_container.called is False


def test_drift_matches_policy_returns_ok():
    ctr = _FakeCtr("c" * 16, attrs=_drift_attrs("unless-stopped"))
    cm = _make_drift_cm(ctr)
    decision, payload = cm._start_drift_decision(
        ctr, "none", "ro", "label", lifecycle_class=LIFECYCLE_PERSISTENT
    )
    assert decision == "ok"
    assert payload is None


@pytest.mark.parametrize("lifecycle_class", list(LIFECYCLE_CLASSES))
def test_drift_more_permissive_denied_for_every_class(lifecycle_class):
    """``always`` outranks every class default -> DENY for all four classes."""
    ctr = _FakeCtr("c" * 16, attrs=_drift_attrs("always"))
    cm = _make_drift_cm(ctr)
    decision, payload = cm._start_drift_decision(
        ctr, "none", "ro", "label", lifecycle_class=lifecycle_class
    )
    assert decision == "deny"
    assert payload["drift"]["restart_policy"] == {
        "expected": _RESTART_POLICY_BY_CLASS[lifecycle_class],
        "actual": "always",
    }


@pytest.mark.parametrize("lifecycle_class", list(LIFECYCLE_CLASSES))
def test_drift_exact_match_is_ok_for_every_class(lifecycle_class):
    """A container carrying its class policy is never a false positive."""
    ctr = _FakeCtr("c" * 16, attrs=_drift_attrs(_RESTART_POLICY_BY_CLASS[lifecycle_class]))
    cm = _make_drift_cm(ctr)
    decision, payload = cm._start_drift_decision(
        ctr, "none", "ro", "label", lifecycle_class=lifecycle_class
    )
    assert decision == "ok"
    assert payload is None


@pytest.mark.parametrize(
    "lifecycle_class",
    [LIFECYCLE_PERSISTENT, LIFECYCLE_RESOURCE, LIFECYCLE_SERVICE],
)
def test_drift_less_permissive_reused_with_warn_for_every_class(lifecycle_class):
    """A stricter live policy (``no``) -> warn + reuse, never deny."""
    ctr = _FakeCtr("c" * 16, attrs=_drift_attrs("no"))
    cm = _make_drift_cm(ctr)
    decision, payload = cm._start_drift_decision(
        ctr, "none", "ro", "label", lifecycle_class=lifecycle_class
    )
    assert decision == "reuse"
    assert payload["decision"] == "warn"
    assert payload["restart_policy"] == {
        "expected": _RESTART_POLICY_BY_CLASS[lifecycle_class],
        "actual": "no",
    }


def test_drift_unknown_class_skips_restart_axis():
    """An unknown class has no expected policy -> the axis is skipped."""
    ctr = _FakeCtr("c" * 16, attrs=_drift_attrs("always"))
    cm = _make_drift_cm(ctr)
    decision, payload = cm._start_drift_decision(
        ctr, "none", "ro", "label", lifecycle_class="bogus-class"
    )
    assert decision == "ok"
    assert payload is None


def test_drift_axis_skipped_when_lifecycle_class_is_none():
    """No class supplied -> the restart axis is not evaluated at all."""
    ctr = _FakeCtr("c" * 16, attrs=_drift_attrs("always"))
    cm = _make_drift_cm(ctr)
    decision, payload = cm._start_drift_decision(ctr, "none", "ro", "label")
    assert decision == "ok"
    assert payload is None


# ---------------------------------------------------------------------------
# (e) migration: v1 record (no restart_policy) + record-policy authority.
# ---------------------------------------------------------------------------


def _v1_record_dict(**overrides):
    """A v1 record dict -- deliberately MISSING the ``restart_policy`` key."""
    data = {
        "id": "rec-v1",
        "docker_id": "docker-9",
        "lifecycle_class": LIFECYCLE_PERSISTENT,
        "owner": OWNER_WORKSPACE,
        "purpose": "",
        "intent_snapshot": {},
        "notes": "",
        "event_log": [],
        "schema_version": 1,
        "inferred": False,
        "state": "running",
        "created_at": "",
        "updated_at": "",
    }
    data.update(overrides)
    return data


def test_v1_dict_without_restart_policy_loads_none():
    data = _v1_record_dict()
    assert "restart_policy" not in data

    record = Record.from_dict(data)
    assert record.restart_policy is None
    # to_dict always emits the field, even when unset.
    assert record.to_dict()["restart_policy"] is None


def test_v2_record_restart_policy_round_trips():
    data = _v1_record_dict(schema_version=SCHEMA_VERSION_CURRENT, restart_policy="always")
    record = Record.from_dict(data)
    assert record.restart_policy == "always"
    assert record.to_dict()["restart_policy"] == "always"


def test_classify_drift_restart_falls_back_to_class_policy():
    record = Record.from_dict(_v1_record_dict())
    findings = classify_drift(record, {"id": "docker-9", "restart_policy": "always"}, None)

    restart = [f for f in findings if f.drift_class == CLASS_RESTART_POLICY]
    assert len(restart) == 1
    assert restart[0].event_type == EVENT_RESTART_POLICY_MISMATCH
    assert restart[0].expected == "unless-stopped"  # class default, not None
    assert restart[0].actual == "always"


def test_classify_drift_uses_record_policy_when_set():
    record = Record.from_dict(
        _v1_record_dict(schema_version=SCHEMA_VERSION_CURRENT, restart_policy="always")
    )
    findings = classify_drift(
        record, {"id": "docker-9", "restart_policy": "unless-stopped"}, None
    )
    restart = [f for f in findings if f.drift_class == CLASS_RESTART_POLICY]
    assert len(restart) == 1
    # The record's own field is authoritative over the class default.
    assert restart[0].expected == "always"
    assert restart[0].actual == "unless-stopped"


def test_classify_drift_unknown_class_skips_restart_axis():
    record = Record.from_dict(_v1_record_dict(lifecycle_class="bogus-class"))
    findings = classify_drift(record, {"id": "docker-9", "restart_policy": "always"}, None)
    assert [f for f in findings if f.drift_class == CLASS_RESTART_POLICY] == []


def test_classify_drift_skips_axis_without_live_key():
    """A live mapping lacking the axis is not evidence of drift."""
    record = Record.from_dict(_v1_record_dict())
    findings = classify_drift(record, {"id": "docker-9"}, None)
    assert [f for f in findings if f.drift_class == CLASS_RESTART_POLICY] == []


def test_start_drift_uses_record_policy_when_present(monkeypatch):
    """The persisted record's restart_policy wins over the class default."""
    record = Record(
        id="rec-1",
        lifecycle_class=LIFECYCLE_PERSISTENT,
        owner=OWNER_WORKSPACE,
        restart_policy="always",
    )
    monkeypatch.setattr(container_manager, "find_by_docker_label", lambda rid: record)
    ctr = _FakeCtr(
        "c" * 16, labels={RECORD_LABEL_KEY: "rec-1"}, attrs=_drift_attrs("unless-stopped")
    )
    cm = _make_drift_cm(ctr)

    decision, payload = cm._start_drift_decision(
        ctr, "none", "ro", "label", lifecycle_class=LIFECYCLE_PERSISTENT
    )
    assert decision == "reuse"  # expected always, live unless-stopped -> not more
    assert payload["restart_policy"] == {"expected": "always", "actual": "unless-stopped"}


def test_start_drift_v1_record_falls_back_to_class_policy(monkeypatch):
    """A v1 record (restart_policy None) uses the class policy on the axis."""
    record = Record(
        id="rec-1",
        lifecycle_class=LIFECYCLE_PERSISTENT,
        owner=OWNER_WORKSPACE,
        restart_policy=None,
    )
    monkeypatch.setattr(container_manager, "find_by_docker_label", lambda rid: record)
    ctr = _FakeCtr(
        "c" * 16, labels={RECORD_LABEL_KEY: "rec-1"}, attrs=_drift_attrs("always")
    )
    cm = _make_drift_cm(ctr)

    decision, payload = cm._start_drift_decision(
        ctr, "none", "ro", "label", lifecycle_class=LIFECYCLE_PERSISTENT
    )
    assert decision == "deny"
    assert payload["drift"]["restart_policy"] == {
        "expected": "unless-stopped",
        "actual": "always",
    }
