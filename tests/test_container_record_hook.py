"""Tests for thoughtmachine/container_record/hook.py (two-phase record CM).

Covers the hook in isolation plus its two integration seams:
  A. record_creation creates the intent stub then injects the record label;
  B. a failure BEFORE attach tears down the container + deletes the stub;
  C. a failure DURING attach tears down the container + deletes the stub;
  D. a clean exit leaves the record in place;
  E. ContainerManager.start fresh-create injects the label + records;
  F. ContainerRegistry.request_container injects the label + records;
  G. attach-time intent-snapshot capture (derive / preserve / no-evidence /
     failure / evidence-rule / end-to-end).
"""

import copy
import logging

import pytest

import infra.container_manager as container_manager
from infra.container_manager import ContainerManager
from infra.container_registry import ContainerRegistry
from thoughtmachine.container_record import (
    LIFECYCLE_EPHEMERAL,
    LIFECYCLE_PERSISTENT,
    LIFECYCLE_RESOURCE,
    RECORD_LABEL_KEY,
    load_record,
)
from thoughtmachine.container_record import hook as hook_mod
from thoughtmachine.container_record.hook import record_creation

WS = "ws-hook"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeCtr:
    """Minimal stand-in for a docker container returned by containers.run."""

    def __init__(self, container_id="docker-1", name="ctr", labels=None, attrs=None):
        self.id = container_id
        self.name = name
        self.status = "created"
        self.labels = dict(labels or {})
        # Thin default inspect payload: carries NO intent evidence, so
        # snapshot_from_attrs yields an all-empty snapshot.
        self.attrs = (
            attrs if attrs is not None else {"State": {"Status": "created"}}
        )
        self.stopped = []
        self.removed = []

    def start(self):
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
        # Inspect payload handed to containers created by ``run``.  ``None``
        # means the thin default (no intent evidence).
        self.run_attrs = None

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
        ctr = _FakeCtr(
            "c-run-1",
            name=name,
            labels=kwargs.get("labels") or {},
            attrs=self.run_attrs,
        )
        self.containers.append(ctr)
        return ctr


class _FakeDockerClient:
    def __init__(self, containers=None):
        self.containers = _FakeContainers(containers or [])


class _RaisingCtr:
    """Container whose inspect ``attrs`` access blows up (best-effort capture)."""

    id = "d-raise"

    @property
    def attrs(self):
        raise RuntimeError("inspect unavailable")


def _realistic_attrs():
    """A Docker inspect payload carrying full intent evidence (design §1.1)."""
    return {
        "Id": "abc123hash",
        "Image": "sha256:deadbeef",
        "Config": {"Image": "agent-executor:latest", "Labels": {}},
        "HostConfig": {
            "NetworkMode": "bridge",
            "Memory": 1073741824,
            "CpuQuota": 50000,
            "OomScoreAdj": 1000,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "ReadonlyRootfs": True,
        },
        "Mounts": [
            {"Destination": "/workspace", "RW": True, "Source": "/tmp/ws"},
        ],
    }


def _make_container_manager(client=None, *, workspace_id="w1", session_id="s1"):
    cm = ContainerManager.__new__(ContainerManager)
    cm.workspace_path = "/tmp/ws-test"
    cm.session_id = session_id
    cm.workspace_id = workspace_id
    cm.session_permissions = {}
    cm._session_config = {"use_container_registry": False}
    cm.image = "agent-executor"
    cm.mem_limit = "1g"
    cm.cpu_quota = 100000
    cm._containers = {}
    cm.client = client if client is not None else _FakeDockerClient()
    cm._compute_config = lambda *a, **k: ("none", "ro")
    cm.vault_root = "/tmp/tm-vault-test"
    cm.container_notes = {}
    cm.max_containers = 4
    cm.workspace_config = {"max_containers": 4}
    cm.dockerfile_path = None
    return cm


# ===========================================================================
# A. stub + attach
# ===========================================================================


def test_creates_stub_then_attaches():
    labels = {"thoughtmachine.container_name": "x"}
    with record_creation(
        workspace_id=WS,
        lifecycle_class=LIFECYCLE_EPHEMERAL,
        labels=labels,
    ) as rec:
        record_id = rec.id
        assert record_id is not None
        # label injected into the SAME dict object (mutated in place)
        assert labels[RECORD_LABEL_KEY] == record_id
        # handle.labels mirrors the injected record label
        assert rec.labels == {RECORD_LABEL_KEY: record_id}
        ctr = _FakeCtr("docker-42")
        rec.attach(ctr)

    assert labels[RECORD_LABEL_KEY] == record_id
    record = load_record(WS, record_id)
    assert record is not None
    assert record.docker_id == "docker-42"
    assert record.lifecycle_class == LIFECYCLE_EPHEMERAL


def test_attach_with_state_sets_state():
    with record_creation(
        workspace_id=WS, lifecycle_class=LIFECYCLE_EPHEMERAL
    ) as rec:
        record_id = rec.id
        rec.attach(_FakeCtr("d-state"), state="running")
    assert load_record(WS, record_id).state == "running"


# ===========================================================================
# B. failure before attach -> rollback
# ===========================================================================


def test_create_failure_deletes_stub():
    labels = {}
    with pytest.raises(RuntimeError):
        with record_creation(
            workspace_id=WS,
            lifecycle_class=LIFECYCLE_EPHEMERAL,
            labels=labels,
        ) as rec:
            record_id = rec.id
            assert load_record(WS, record_id) is not None
            raise RuntimeError("docker run blew up")
    # stub deleted, no orphan record
    assert load_record(WS, record_id) is None


def test_failure_after_container_before_attach_tears_down():
    labels = {}
    ctr = _FakeCtr("d-boom")
    with pytest.raises(RuntimeError):
        with record_creation(
            workspace_id=WS,
            lifecycle_class=LIFECYCLE_EPHEMERAL,
            labels=labels,
        ) as rec:
            record_id = rec.id
            # container created but the body raised before attach: simulate by
            # hand by stashing the container on the handle
            rec._container = ctr
            raise RuntimeError("boom before attach")
    assert ctr.stopped, "rolled-back container should be stopped"
    assert ctr.removed, "rolled-back container should be removed"
    assert load_record(WS, record_id) is None


# ===========================================================================
# C. failure during attach -> rollback
# ===========================================================================


def test_attach_failure_tears_down_and_deletes(monkeypatch):
    ctr = _FakeCtr("d-attach-fail")

    def _boom(*a, **k):
        raise RuntimeError("attach failed")

    monkeypatch.setattr(hook_mod, "attach_container", _boom)

    with pytest.raises(RuntimeError):
        with record_creation(
            workspace_id=WS, lifecycle_class=LIFECYCLE_EPHEMERAL
        ) as rec:
            record_id = rec.id
            rec.attach(ctr)  # raises inside
    assert ctr.stopped
    assert ctr.removed
    assert load_record(WS, record_id) is None


def test_attach_then_body_failure_leaves_record():
    """Once attached, a later body failure must NOT tear down / delete."""
    ctr = _FakeCtr("d-attached-ok")
    with pytest.raises(RuntimeError):
        with record_creation(
            workspace_id=WS, lifecycle_class=LIFECYCLE_EPHEMERAL
        ) as rec:
            record_id = rec.id
            rec.attach(ctr)
            raise RuntimeError("post-attach failure")
    assert not ctr.removed
    record = load_record(WS, record_id)
    assert record is not None
    assert record.docker_id == "d-attached-ok"


# ===========================================================================
# D. clean exit
# ===========================================================================


def test_clean_exit_without_attach_leaves_stub():
    with record_creation(
        workspace_id=WS, lifecycle_class=LIFECYCLE_RESOURCE
    ) as rec:
        record_id = rec.id
    record = load_record(WS, record_id)
    assert record is not None
    assert record.docker_id == ""
    assert record.lifecycle_class == LIFECYCLE_RESOURCE


def test_record_store_failure_propagates_fail_closed(monkeypatch):
    """If begin_record blows up, fail CLOSED: the exception propagates out of
    ``record_creation`` and the ``with`` body (i.e. ``containers.run``) is
    NEVER entered.  A container must never exist without a record."""

    def _boom(*a, **k):
        raise RuntimeError("vault unavailable")

    monkeypatch.setattr(hook_mod, "begin_record", _boom)
    labels = {}
    entered = []

    with pytest.raises(RuntimeError, match="vault unavailable"):
        with record_creation(
            workspace_id=WS,
            lifecycle_class=LIFECYCLE_EPHEMERAL,
            labels=labels,
        ) as rec:
            entered.append(rec)  # must never execute

    assert entered == [], "body must not run when the record cannot be written"
    # labels dict untouched (nothing to inject)
    assert RECORD_LABEL_KEY not in labels


# ===========================================================================
# E. ContainerManager.start integration
# ===========================================================================


def test_container_manager_start_injects_label_and_records(monkeypatch):
    monkeypatch.setattr(
        container_manager, "is_registry_active", lambda cfg: False
    )
    client = _FakeDockerClient()
    cm = _make_container_manager(client, workspace_id="ws-cm")
    result = cm.start(image="agent-executor", name="agent-exec-integration")

    assert result["id"] == "c-run-1"
    run_kwargs = client.containers.run_calls[-1]["kwargs"]
    assert RECORD_LABEL_KEY in run_kwargs["labels"]
    record_id = run_kwargs["labels"][RECORD_LABEL_KEY]
    record = load_record("ws-cm", record_id)
    assert record is not None
    assert record.docker_id == "c-run-1"
    assert record.lifecycle_class == LIFECYCLE_PERSISTENT


# ===========================================================================
# F. ContainerRegistry.request_container integration
# ===========================================================================


def test_registry_request_container_injects_label_and_records():
    client = _FakeDockerClient()
    registry = ContainerRegistry(
        docker_client=client, feature_flag_check=lambda: True
    )
    handle = registry.request_container(
        "worker-1",
        "sess-1",
        {},
        image="agent-executor",
        workspace_id="ws-reg",
    )
    run_kwargs = client.containers.run_calls[-1]["kwargs"]
    assert RECORD_LABEL_KEY in run_kwargs["labels"]
    record_id = run_kwargs["labels"][RECORD_LABEL_KEY]
    record = load_record("ws-reg", record_id)
    assert record is not None
    assert record.docker_id == handle["id"]
    assert record.lifecycle_class == LIFECYCLE_PERSISTENT


def test_container_manager_start_explicit_ephemeral(monkeypatch):
    """An explicit lifecycle_class=ephemeral is honoured on a fresh create.

    The start() default is persistent (non-destructive); only a caller that
    KNOWS the container is ephemeral passes ``lifecycle_class`` explicitly.
    """
    monkeypatch.setattr(
        container_manager, "is_registry_active", lambda cfg: False
    )
    client = _FakeDockerClient()
    cm = _make_container_manager(client, workspace_id="ws-cm-eph")
    result = cm.start(
        image="agent-executor",
        name="agent-exec-ephemeral",
        lifecycle_class=LIFECYCLE_EPHEMERAL,
    )

    assert result["id"] == "c-run-1"
    run_kwargs = client.containers.run_calls[-1]["kwargs"]
    assert RECORD_LABEL_KEY in run_kwargs["labels"]
    record_id = run_kwargs["labels"][RECORD_LABEL_KEY]
    record = load_record("ws-cm-eph", record_id)
    assert record is not None
    assert record.docker_id == "c-run-1"
    assert record.lifecycle_class == LIFECYCLE_EPHEMERAL


# ===========================================================================
# G. attach-time intent-snapshot capture
# ===========================================================================


def test_attach_derives_snapshot_from_attrs():
    """(a) An empty record snapshot is filled from the live inspect payload."""
    with record_creation(
        workspace_id=WS, lifecycle_class=LIFECYCLE_EPHEMERAL
    ) as rec:
        record_id = rec.id
        assert rec.record.intent_snapshot == {}
        rec.attach(_FakeCtr("d-snap", attrs=_realistic_attrs()))

    record = load_record(WS, record_id)
    snap = record.intent_snapshot
    assert snap["network_mode"] == "bridge"
    assert snap["workspace_mode"] == "rw"
    assert snap["mem_limit"] == "1073741824"
    assert snap["cpu_quota"] == 50000
    assert snap["oom_score_adj"] == 1000
    assert snap["image_ref"] == "agent-executor:latest"
    assert snap["image_hash"] == "sha256:deadbeef"
    assert snap["hardening"]["cap_drop"] == ["ALL"]
    assert snap["hardening"]["security_opt"] == ["no-new-privileges:true"]
    assert snap["hardening"]["readonly_rootfs"] is True


def test_attach_does_not_overwrite_existing_snapshot():
    """(b) A natively-authored snapshot wins over one derived at attach time."""
    authored = {"network_mode": "host", "hardening": {"cap_add": ["NET_ADMIN"]}}
    with record_creation(
        workspace_id=WS,
        lifecycle_class=LIFECYCLE_EPHEMERAL,
        intent_snapshot=authored,
    ) as rec:
        record_id = rec.id
        rec.attach(_FakeCtr("d-preserve", attrs=_realistic_attrs()))

    record = load_record(WS, record_id)
    assert record.intent_snapshot == authored


def test_attach_without_evidence_leaves_snapshot_empty():
    """(c) A thin inspect payload yields no snapshot; the attach still binds."""
    with record_creation(
        workspace_id=WS, lifecycle_class=LIFECYCLE_EPHEMERAL
    ) as rec:
        record_id = rec.id
        rec.attach(_FakeCtr("d-thin"))  # default thin attrs -> no evidence

    record = load_record(WS, record_id)
    assert record.intent_snapshot == {}
    assert record.docker_id == "d-thin"


def test_attach_snapshot_failure_logs_and_still_binds(caplog):
    """(d) A raising inspect access is logged; attach must not be blocked."""
    with record_creation(
        workspace_id=WS, lifecycle_class=LIFECYCLE_EPHEMERAL
    ) as rec:
        record_id = rec.id
        with caplog.at_level(logging.WARNING, logger="infra.container_record.hook"):
            rec.attach(_RaisingCtr())

    record = load_record(WS, record_id)
    assert record.docker_id == "d-raise"
    assert record.intent_snapshot == {}
    assert any(
        "failed to derive intent snapshot" in m for m in caplog.messages
    )


def test_snapshot_has_evidence_truth_table():
    """(e) The evidence rule: any truthy scalar / non-empty hardening counts."""
    has_evidence = hook_mod._snapshot_has_evidence
    assert has_evidence(None) is False
    assert has_evidence("nope") is False
    assert has_evidence({}) is False
    assert has_evidence({"network_mode": ""}) is False
    assert has_evidence({"hardening": {}}) is False
    assert has_evidence({"network_mode": "bridge"}) is True
    assert has_evidence({"image_hash": "sha256:x"}) is True
    assert has_evidence({"oom_score_adj": 0}) is False
    assert has_evidence({"oom_score_adj": 1000}) is True
    assert has_evidence({"hardening": {"cap_drop": ["ALL"]}}) is True


def test_container_manager_start_persists_snapshot(monkeypatch):
    """(f) End-to-end: ContainerManager.start records the derived snapshot."""
    monkeypatch.setattr(
        container_manager, "is_registry_active", lambda cfg: False
    )
    client = _FakeDockerClient()
    client.containers.run_attrs = _realistic_attrs()
    cm = _make_container_manager(client, workspace_id="ws-cm-snap")
    result = cm.start(image="agent-executor", name="agent-exec-snap")

    assert result["id"] == "c-run-1"
    run_kwargs = client.containers.run_calls[-1]["kwargs"]
    record_id = run_kwargs["labels"][RECORD_LABEL_KEY]
    record = load_record("ws-cm-snap", record_id)
    assert record is not None
    assert record.intent_snapshot["image_ref"] == "agent-executor:latest"
    assert record.intent_snapshot["network_mode"] == "bridge"
    assert record.intent_snapshot["hardening"]["readonly_rootfs"] is True


def test_registry_request_container_persists_snapshot():
    """(g) End-to-end: ContainerRegistry.request_container records the snapshot."""
    client = _FakeDockerClient()
    client.containers.run_attrs = _realistic_attrs()
    registry = ContainerRegistry(
        docker_client=client, feature_flag_check=lambda: True
    )
    handle = registry.request_container(
        "worker-1",
        "sess-1",
        {},
        image="agent-executor",
        workspace_id="ws-reg-snap",
    )
    run_kwargs = client.containers.run_calls[-1]["kwargs"]
    record_id = run_kwargs["labels"][RECORD_LABEL_KEY]
    record = load_record("ws-reg-snap", record_id)
    assert record is not None
    assert record.docker_id == handle["id"]
    assert record.intent_snapshot["network_mode"] == "bridge"
    assert record.intent_snapshot["workspace_mode"] == "rw"
    assert record.intent_snapshot["hardening"]["cap_drop"] == ["ALL"]

