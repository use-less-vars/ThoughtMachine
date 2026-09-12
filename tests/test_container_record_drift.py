"""Tests for the read-only container-record drift detector (``drift.py``).

The module under test compares a record's *recorded intent* against a live
Docker container and the policy resolved from the caller's permissions.  It is
**read-only**: it may append drift events to the record's own event log via the
sanctioned ``api.append_event`` helper, but must never mutate Docker.  These
tests exercise the pure classifier in isolation, the duck-typed detector, the
event emitter, the end-to-end scan, and the read-only guarantee itself.
"""

from __future__ import annotations

import dataclasses
import pathlib
from types import SimpleNamespace

import pytest

from security.security_gate import ContainerConfig
from thoughtmachine.container_record import api, drift
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities

WS = "ws-1"

#: Docker-mutating method names the drift module must never touch.
_MUTATING = frozenset(
    {
        "stop",
        "remove",
        "restart",
        "kill",
        "exec_run",
        "run",
        "pause",
        "unpause",
        "rename",
        "update",
        "exec_create",
    }
)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(root))
    return root


@pytest.fixture
def caps():
    return WorkspaceCapabilities()


# ── Fakes / helpers ─────────────────────────────────────────────────────────


class FakeContainer:
    """Duck-typed Docker container (read-only surface only)."""

    def __init__(self, cid, attrs, labels=None):
        self.id = cid
        self.attrs = attrs
        self.labels = labels or {}


class FakeContainers:
    """Duck-typed Docker client exposing ``list(all=...)``."""

    def __init__(self, items=None, list_error=None):
        self._items = list(items or [])
        self._list_error = list_error
        self.list_calls = []

    def list(self, all=False):
        self.list_calls.append(all)
        if self._list_error is not None:
            raise self._list_error
        return list(self._items)


class _ExplodingMapping(dict):
    """A mapping whose ``.get`` raises — forces a live-inspection failure."""

    def get(self, *args, **kwargs):
        raise RuntimeError("boom")


class _GuardedContainer:
    """Read-only fake that *records* every attribute it is asked for."""

    def __init__(self, cid, attrs, labels=None):
        self.id = cid
        self._attrs = attrs
        self.labels = labels or {}
        self.touched = []

    @property
    def attrs(self):
        self.touched.append("attrs")
        return self._attrs

    def __getattr__(self, name):
        touched = self.__dict__.setdefault("touched", [])
        touched.append(name)
        if name in _MUTATING:
            raise AssertionError(f"drift touched mutating container method {name!r}")
        raise AttributeError(name)


class _GuardedContainers:
    """Read-only fake client that *records* every method/attribute access."""

    def __init__(self, items):
        self._items = list(items)
        self.calls = []

    def list(self, all=False):
        self.calls.append(("list", all))
        return list(self._items)

    def __getattr__(self, name):
        self.__dict__.setdefault("calls", []).append(("attr", name))
        if name in _MUTATING:
            raise AssertionError(f"drift touched mutating client method {name!r}")
        raise AttributeError(name)


def _record(**overrides):
    """Build a minimal record-like object for the pure classifier."""
    base = {
        "id": "rec-1",
        "docker_id": "",
        "intent_snapshot": {},
        "inferred": False,
        "lifecycle_class": "ephemeral",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _config(network_mode, workspace_mode):
    return ContainerConfig(
        network_mode=network_mode,
        workspace_mode=workspace_mode,
        effective={},
        lifecycle_class="ephemeral",
    )


def _finding(drift_class, event_type, expected, actual):
    return drift.DriftFinding(
        drift_class,
        event_type,
        expected,
        actual,
        drift.signature_for(event_type, expected, actual),
    )


def _attrs(
    *,
    image="python:3.12",
    image_hash="sha256:deadbeef",
    memory=536870912,
    network="none",
    rw=True,
    cap_drop=None,
):
    """Build a Docker inspect ``attrs`` payload (shape consumed by snapshot.py)."""
    host = {"NetworkMode": network, "Memory": memory, "OomScoreAdj": 0}
    if cap_drop is not None:
        host["CapDrop"] = cap_drop
    return {
        "HostConfig": host,
        "Config": {"Image": image},
        "Image": image_hash,
        "Mounts": [{"Destination": "/workspace", "RW": rw}],
    }


# ── Constants & signature ───────────────────────────────────────────────────


def test_module_constants():
    assert drift.ACTOR == "thoughtmachine.container_record.drift"
    assert drift.CLASS_IDENTITY == "identity"
    assert drift.CLASS_POLICY == "policy"
    assert drift.CLASS_RUNTIME == "runtime"
    assert drift.CLASS_IMAGE == "image"
    assert drift.CLASS_HARDENING == "hardening"
    assert drift.EVENT_IDENTITY_CHANGED == "drift.identity_changed"
    assert drift.EVENT_CONTAINER_ABSENT == "drift.container_absent"
    assert drift.EVENT_POLICY_CONFIG_CHANGED == "drift.policy_config_changed"
    assert drift.EVENT_RUNTIME_MISMATCH == "drift.runtime_mismatch"
    assert drift.EVENT_IMAGE_CHANGED == "drift.image_changed"
    assert drift.EVENT_HARDENING_LOST == "drift.hardening_lost"


def test_public_api_is_exported():
    for name in (
        "ACTOR",
        "CLASS_IDENTITY",
        "CLASS_POLICY",
        "CLASS_RUNTIME",
        "CLASS_IMAGE",
        "CLASS_HARDENING",
        "EVENT_IDENTITY_CHANGED",
        "EVENT_CONTAINER_ABSENT",
        "EVENT_POLICY_CONFIG_CHANGED",
        "EVENT_RUNTIME_MISMATCH",
        "EVENT_IMAGE_CHANGED",
        "EVENT_HARDENING_LOST",
        "DriftFinding",
        "signature_for",
        "classify_drift",
        "detect_record_drift",
        "emit_drift_findings",
        "scan_record",
    ):
        assert name in drift.__all__


def test_signature_for_is_deterministic_and_short():
    first = drift.signature_for("evt", "a", "b")
    second = drift.signature_for("evt", "a", "b")
    assert first == second
    assert len(first) == 16
    assert all(c in "0123456789abcdef" for c in first)


def test_signature_for_varies_with_inputs():
    base = drift.signature_for("evt", "a", "b")
    assert drift.signature_for("other", "a", "b") != base
    assert drift.signature_for("evt", "c", "b") != base
    assert drift.signature_for("evt", "a", "c") != base


def test_drift_finding_is_frozen():
    finding = _finding(drift.CLASS_IDENTITY, drift.EVENT_IDENTITY_CHANGED, "a", "b")
    with pytest.raises(dataclasses.FrozenInstanceError):
        finding.expected = "changed"  # type: ignore[misc]


# ── classify_drift ──────────────────────────────────────────────────────────


def test_classify_no_drift():
    record = _record(
        docker_id="docker-1",
        intent_snapshot={
            "network_mode": "none",
            "workspace_mode": "ro",
            "mem_limit": "512m",
            "cpu_quota": 50000,
            "oom_score_adj": 0,
            "image_ref": "python:3.12",
            "image_hash": "sha256:deadbeef",
            "hardening": {"cap_drop": ["ALL"]},
        },
    )
    live = {
        "id": "docker-1",
        "network_mode": "none",
        "workspace_mode": "ro",
        "mem_limit": "512m",
        "cpu_quota": 50000,
        "oom_score_adj": 0,
        "image_ref": "python:3.12",
        "image_hash": "sha256:deadbeef",
        "hardening": {"cap_drop": ["ALL"]},
    }
    assert drift.classify_drift(record, live, _config("none", "ro")) == []


def test_classify_identity_changed():
    record = _record(docker_id="docker-1")
    out = drift.classify_drift(record, {"id": "docker-2"}, None)
    assert len(out) == 1
    assert out[0].drift_class == drift.CLASS_IDENTITY
    assert out[0].event_type == drift.EVENT_IDENTITY_CHANGED
    assert out[0].expected == "docker-1"
    assert out[0].actual == "docker-2"


def test_classify_identity_skipped_without_live():
    record = _record(docker_id="docker-1")
    assert drift.classify_drift(record, None, None) == []


def test_classify_identity_skipped_when_docker_id_blank():
    record = _record(docker_id="")
    assert drift.classify_drift(record, {"id": "docker-2"}, None) == []


def test_classify_policy_changed():
    # ``expected`` is the resolved config's desired value; ``actual`` is the
    # recorded snapshot value.  The live mapping carries no ``network_mode``,
    # so the live ``network_mode`` runtime axis ALSO reports the snapshot value
    # against that ``None`` live value.
    record = _record(intent_snapshot={"network_mode": "none"})
    out = drift.classify_drift(record, {"id": "x"}, _config("bridge", "ro"))
    assert [f.drift_class for f in out] == [drift.CLASS_POLICY, drift.CLASS_RUNTIME]
    policy, runtime = out
    assert policy.event_type == drift.EVENT_POLICY_CONFIG_CHANGED
    assert policy.expected == "bridge"
    assert policy.actual == "none"
    assert runtime.event_type == drift.EVENT_RUNTIME_MISMATCH
    assert runtime.expected == "none"
    assert runtime.actual is None


def test_classify_policy_evaluated_without_live():
    record = _record(intent_snapshot={"network_mode": "none"})
    out = drift.classify_drift(record, None, _config("bridge", "ro"))
    assert [f.drift_class for f in out] == [drift.CLASS_POLICY]
    assert out[0].expected == "bridge"
    assert out[0].actual == "none"


def test_classify_policy_skipped_without_config():
    # No config -> the policy class is skipped, but the (live) runtime axis for
    # ``network_mode`` still compares the snapshot value against ``None``.
    record = _record(intent_snapshot={"network_mode": "none"})
    out = drift.classify_drift(record, {"id": "x"}, None)
    assert [f.drift_class for f in out] == [drift.CLASS_RUNTIME]
    assert out[0].expected == "none"
    assert out[0].actual is None


def test_classify_policy_axis_blank_is_skipped():
    # A blank *snapshot* value is not drift (the policy axes skip it, and the
    # runtime axes skip it too because their expected values are blank).
    record = _record(intent_snapshot={"network_mode": "", "workspace_mode": ""})
    assert drift.classify_drift(record, {"id": "x"}, _config("bridge", "rw")) == []
    # A blank *config* value is not policy drift either.  The non-blank snapshot
    # network/workspace values are still compared on the live runtime axes,
    # where the live mapping supplies no value -> two runtime findings.
    record = _record(intent_snapshot={"network_mode": "none", "workspace_mode": "ro"})
    out = drift.classify_drift(record, {"id": "x"}, _config("", ""))
    assert [f.drift_class for f in out] == [drift.CLASS_RUNTIME, drift.CLASS_RUNTIME]
    assert [(f.expected, f.actual) for f in out] == [("none", None), ("ro", None)]


def test_classify_runtime_mismatch():
    record = _record(intent_snapshot={"mem_limit": "512m"})
    out = drift.classify_drift(record, {"id": "x", "mem_limit": "256m"}, None)
    assert len(out) == 1
    assert out[0].drift_class == drift.CLASS_RUNTIME
    assert out[0].event_type == drift.EVENT_RUNTIME_MISMATCH
    assert out[0].expected == "512m"
    assert out[0].actual == "256m"


def test_classify_runtime_skipped_when_snapshot_blank():
    record = _record(intent_snapshot={"mem_limit": ""})
    assert drift.classify_drift(record, {"id": "x", "mem_limit": "256m"}, None) == []


def test_classify_runtime_skipped_without_live():
    record = _record(intent_snapshot={"mem_limit": "512m"})
    assert drift.classify_drift(record, None, None) == []


def test_classify_runtime_network_mode_mismatch():
    # The live ``network_mode`` now participates in the runtime axis: a live
    # value that differs from the snapshot yields exactly one runtime finding.
    record = _record(intent_snapshot={"network_mode": "bridge"})
    out = drift.classify_drift(record, {"id": "x", "network_mode": "host"}, None)
    assert len(out) == 1
    assert out[0].drift_class == drift.CLASS_RUNTIME
    assert out[0].event_type == drift.EVENT_RUNTIME_MISMATCH
    assert out[0].expected == "bridge"
    assert out[0].actual == "host"


def test_classify_runtime_workspace_mode_mismatch():
    # The live ``workspace_mode`` now participates in the runtime axis too.
    record = _record(intent_snapshot={"workspace_mode": "ro"})
    out = drift.classify_drift(record, {"id": "x", "workspace_mode": "rw"}, None)
    assert len(out) == 1
    assert out[0].drift_class == drift.CLASS_RUNTIME
    assert out[0].event_type == drift.EVENT_RUNTIME_MISMATCH
    assert out[0].expected == "ro"
    assert out[0].actual == "rw"


def test_classify_runtime_network_mode_skipped_when_snapshot_blank():
    # An empty/absent snapshot value short-circuits the axis even when the live
    # container reports a value.
    empty = _record(intent_snapshot={"network_mode": ""})
    assert drift.classify_drift(empty, {"id": "x", "network_mode": "host"}, None) == []
    absent = _record(intent_snapshot={})
    assert drift.classify_drift(absent, {"id": "x", "network_mode": "host"}, None) == []


def test_classify_image_changed():
    record = _record(intent_snapshot={"image_ref": "python:3.12"})
    out = drift.classify_drift(record, {"id": "x", "image_ref": "python:3.11"}, None)
    assert len(out) == 1
    assert out[0].drift_class == drift.CLASS_IMAGE
    assert out[0].event_type == drift.EVENT_IMAGE_CHANGED
    assert out[0].expected == "python:3.12"
    assert out[0].actual == "python:3.11"


def test_classify_hardening_lost():
    record = _record(intent_snapshot={"hardening": {"cap_drop": ["ALL"]}})
    out = drift.classify_drift(record, {"id": "x", "hardening": {}}, None)
    assert len(out) == 1
    assert out[0].drift_class == drift.CLASS_HARDENING
    assert out[0].event_type == drift.EVENT_HARDENING_LOST
    assert out[0].expected == {"cap_drop": ["ALL"]}
    assert out[0].actual == {}


def test_classify_hardening_skipped_when_snapshot_empty():
    record = _record(intent_snapshot={"hardening": {}})
    assert drift.classify_drift(record, {"id": "x", "hardening": {"a": 1}}, None) == []


def test_classify_emits_findings_in_fixed_order():
    record = _record(
        docker_id="docker-1",
        intent_snapshot={
            "network_mode": "none",
            "workspace_mode": "ro",
            "mem_limit": "512m",
            "image_ref": "python:3.12",
            "hardening": {"cap_drop": ["ALL"]},
        },
    )
    live = {
        "id": "docker-2",
        "network_mode": "none",
        "workspace_mode": "ro",
        "mem_limit": "256m",
        "image_ref": "python:3.11",
        "hardening": {},
    }
    out = drift.classify_drift(record, live, _config("bridge", "ro"))
    assert [f.drift_class for f in out] == [
        drift.CLASS_IDENTITY,
        drift.CLASS_POLICY,
        drift.CLASS_RUNTIME,
        drift.CLASS_IMAGE,
        drift.CLASS_HARDENING,
    ]


def test_classify_never_raises_on_garbage():
    # Total: odd inputs still return a list and never raise.
    assert isinstance(drift.classify_drift(None, object(), object()), list)
    assert drift.classify_drift(None, None, None) == []
    assert isinstance(
        drift.classify_drift(_record(intent_snapshot="not-a-dict"), {}, None), list
    )
    assert isinstance(
        drift.classify_drift(_record(docker_id=123), {"id": 123}, None), list
    )


# ── detect_record_drift ─────────────────────────────────────────────────────


def test_detect_matches_by_docker_id(caps):
    record = _record(docker_id="d1", intent_snapshot={"mem_limit": "999m"})
    containers = FakeContainers([FakeContainer("d1", _attrs(memory=536870912))])
    out = drift.detect_record_drift(
        record, containers, permissions={}, capabilities=caps
    )
    assert len(out) == 1
    assert out[0].drift_class == drift.CLASS_RUNTIME
    assert out[0].expected == "999m"
    assert out[0].actual == "536870912"


def test_detect_matches_by_record_label(caps):
    record = _record(id="rec-7", docker_id="", intent_snapshot={"image_ref": "python:3.12"})
    container = FakeContainer(
        "c-99", _attrs(image="python:3.11"), labels={api.RECORD_LABEL_KEY: "rec-7"}
    )
    out = drift.detect_record_drift(
        record, FakeContainers([container]), permissions={}, capabilities=caps
    )
    assert len(out) == 1
    assert out[0].drift_class == drift.CLASS_IMAGE
    assert out[0].expected == "python:3.12"
    assert out[0].actual == "python:3.11"


def test_detect_absent_container_reports_container_absent(caps):
    record = _record(docker_id="d1")
    out = drift.detect_record_drift(
        record, FakeContainers([]), permissions={}, capabilities=caps
    )
    assert len(out) == 1
    assert out[0].drift_class == drift.CLASS_IDENTITY
    assert out[0].event_type == drift.EVENT_CONTAINER_ABSENT
    assert out[0].expected == "d1"
    assert out[0].actual is None
    assert out[0].signature == drift.signature_for(
        drift.EVENT_CONTAINER_ABSENT, "d1", None
    )

    # With no docker_id, the record id is the expected identity.
    blank = _record(id="rec-1", docker_id="")
    out = drift.detect_record_drift(
        blank, FakeContainers([]), permissions={}, capabilities=caps
    )
    assert out[0].event_type == drift.EVENT_CONTAINER_ABSENT
    assert out[0].expected == "rec-1"


def test_detect_invalid_lifecycle_suppresses_policy(caps):
    record = _record(
        docker_id="d1", lifecycle_class="bogus", intent_snapshot={"network_mode": "bridge"}
    )
    containers = FakeContainers([FakeContainer("d1", _attrs(network="none"))])
    out = drift.detect_record_drift(
        record, containers, permissions={}, capabilities=caps
    )
    # Config resolution failed -> policy comparison is suppressed entirely (no
    # CLASS_POLICY finding).  The live-dependent runtime axis still runs: the
    # snapshot's ``network_mode`` ("bridge") differs from the live container's
    # ("none").
    assert drift.CLASS_POLICY not in {f.drift_class for f in out}
    assert [f.drift_class for f in out] == [drift.CLASS_RUNTIME]
    assert out[0].event_type == drift.EVENT_RUNTIME_MISMATCH
    assert out[0].expected == "bridge"
    assert out[0].actual == "none"


def test_detect_valid_lifecycle_reports_policy_config_drift(caps):
    record = _record(
        docker_id="d1",
        lifecycle_class="ephemeral",
        intent_snapshot={"network_mode": "bridge"},
    )
    containers = FakeContainers([FakeContainer("d1", _attrs(network="none"))])
    out = drift.detect_record_drift(
        record, containers, permissions={}, capabilities=caps
    )
    # Policy: the resolved config wants "none" while the recorded snapshot says
    # "bridge".  Runtime: the snapshot's ``network_mode`` ("bridge") differs
    # from the live container's ("none") -- both axes are reported.
    assert [f.drift_class for f in out] == [drift.CLASS_POLICY, drift.CLASS_RUNTIME]
    policy, runtime = out
    assert policy.event_type == drift.EVENT_POLICY_CONFIG_CHANGED
    assert policy.expected == "none"
    assert policy.actual == "bridge"
    assert runtime.event_type == drift.EVENT_RUNTIME_MISMATCH
    assert runtime.expected == "bridge"
    assert runtime.actual == "none"


def test_detect_listing_failure_reports_absent(caps):
    record = _record(docker_id="d1")
    containers = FakeContainers(list_error=RuntimeError("daemon down"))
    out = drift.detect_record_drift(
        record, containers, permissions={}, capabilities=caps
    )
    assert len(out) == 1
    assert out[0].event_type == drift.EVENT_CONTAINER_ABSENT


def test_detect_live_inspection_failure_skips_live_axes(caps):
    record = _record(
        docker_id="d1",
        lifecycle_class="ephemeral",
        intent_snapshot={"network_mode": "bridge"},
    )
    # ``attrs`` is a Mapping whose ``.get`` raises: the live build fails and
    # degrades to ``live=None``.
    containers = FakeContainers([FakeContainer("d1", _ExplodingMapping())])
    out = drift.detect_record_drift(
        record, containers, permissions={}, capabilities=caps
    )
    # Live-dependent axes are skipped; the live-independent policy axis remains.
    assert [f.drift_class for f in out] == [drift.CLASS_POLICY]
    assert out[0].event_type == drift.EVENT_POLICY_CONFIG_CHANGED


# ── emit_drift_findings ─────────────────────────────────────────────────────


def test_emit_inferred_record_emits_nothing(vault):
    rec = api.create_record(WS, "ephemeral", "workspace-owned", vault_root=vault)
    api.update_record(WS, rec.id, vault_root=vault, inferred=True)
    rec = api.load_record(WS, rec.id, vault)
    finding = _finding(drift.CLASS_IDENTITY, drift.EVENT_IDENTITY_CHANGED, "a", "b")

    out = drift.emit_drift_findings(WS, rec, [finding], vault_root=vault)

    assert out == []
    assert api.read_event_log(WS, rec.id, vault_root=vault) == []


def test_emit_appends_event_with_exact_payload(vault):
    rec = api.create_record(WS, "ephemeral", "workspace-owned", vault_root=vault)
    finding = _finding(drift.CLASS_IDENTITY, drift.EVENT_IDENTITY_CHANGED, "a", "b")

    out = drift.emit_drift_findings(WS, rec, [finding], vault_root=vault)

    assert out == [finding]
    log = api.read_event_log(WS, rec.id, vault_root=vault)
    assert len(log) == 1
    entry = log[0]
    assert entry["event_type"] == drift.EVENT_IDENTITY_CHANGED
    assert entry["actor"] == drift.ACTOR
    assert set(entry["payload"]) == {
        "class",
        "expected",
        "actual",
        "signature",
        "detected_at",
    }
    assert entry["payload"]["class"] == drift.CLASS_IDENTITY
    assert entry["payload"]["expected"] == "a"
    assert entry["payload"]["actual"] == "b"
    assert entry["payload"]["signature"] == finding.signature
    assert entry["payload"]["detected_at"]


def test_emit_dedupes_identical_finding(vault):
    rec = api.create_record(WS, "ephemeral", "workspace-owned", vault_root=vault)
    finding = _finding(drift.CLASS_IDENTITY, drift.EVENT_IDENTITY_CHANGED, "a", "b")

    assert drift.emit_drift_findings(WS, rec, [finding], vault_root=vault) == [finding]
    # Second scan sees the same signature -> nothing emitted.
    assert drift.emit_drift_findings(WS, rec, [finding], vault_root=vault) == []
    assert len(api.read_event_log(WS, rec.id, vault_root=vault)) == 1


def test_emit_emits_when_signature_changes(vault):
    rec = api.create_record(WS, "ephemeral", "workspace-owned", vault_root=vault)
    first = _finding(drift.CLASS_IDENTITY, drift.EVENT_IDENTITY_CHANGED, "a", "b")
    second = _finding(drift.CLASS_IDENTITY, drift.EVENT_IDENTITY_CHANGED, "c", "b")

    assert drift.emit_drift_findings(WS, rec, [first], vault_root=vault) == [first]
    assert drift.emit_drift_findings(WS, rec, [second], vault_root=vault) == [second]
    assert len(api.read_event_log(WS, rec.id, vault_root=vault)) == 2


def test_emit_distinct_event_types(vault):
    rec = api.create_record(WS, "ephemeral", "workspace-owned", vault_root=vault)
    identity = _finding(drift.CLASS_IDENTITY, drift.EVENT_IDENTITY_CHANGED, "a", "b")
    runtime = _finding(drift.CLASS_RUNTIME, drift.EVENT_RUNTIME_MISMATCH, "512m", "256m")

    out = drift.emit_drift_findings(WS, rec, [identity, runtime], vault_root=vault)

    assert out == [identity, runtime]
    assert len(api.read_event_log(WS, rec.id, vault_root=vault)) == 2


def test_emit_dedupes_against_most_recent_same_type_event(vault):
    # ``read_event_log`` returns entries OLDEST-FIRST (verified empirically for
    # both the embedded log and the ``.events.jsonl`` sidecar), so the LAST
    # entry of a given ``event_type`` is its MOST RECENT one.  The spec rule is:
    # skip only when the LAST event of that type already carries the same
    # signature.  We emit signature A, then B, then A again; the last same-type
    # event is B (not A), so the re-sent A MUST be emitted.  A regression that
    # compared against the OLDEST same-type event (``prior[0]``) would wrongly
    # skip it and leave only [A, B].
    rec = api.create_record(WS, "ephemeral", "workspace-owned", vault_root=vault)
    sig_a = _finding(drift.CLASS_RUNTIME, drift.EVENT_RUNTIME_MISMATCH, "512m", "256m")
    sig_b = _finding(drift.CLASS_RUNTIME, drift.EVENT_RUNTIME_MISMATCH, "512m", "128m")

    assert drift.emit_drift_findings(WS, rec, [sig_a], vault_root=vault) == [sig_a]
    assert drift.emit_drift_findings(WS, rec, [sig_b], vault_root=vault) == [sig_b]
    assert drift.emit_drift_findings(WS, rec, [sig_a], vault_root=vault) == [sig_a]

    log = api.read_event_log(WS, rec.id, vault_root=vault)
    assert [entry["payload"]["signature"] for entry in log] == [
        sig_a.signature,
        sig_b.signature,
        sig_a.signature,
    ]


# ── scan_record end-to-end ──────────────────────────────────────────────────


def test_scan_record_end_to_end_and_dedupes(vault, caps):
    rec = api.create_record(
        WS,
        "ephemeral",
        "workspace-owned",
        intent_snapshot={"image_ref": "python:3.12", "mem_limit": "512m"},
        vault_root=vault,
    )
    api.update_record(WS, rec.id, vault_root=vault, docker_id="d1")
    rec = api.load_record(WS, rec.id, vault)

    container = FakeContainer("d1", _attrs(image="python:3.11", memory=268435456))
    containers = FakeContainers([container])

    out = drift.scan_record(
        rec,
        containers,
        workspace_id=WS,
        permissions={},
        capabilities=caps,
        vault_root=vault,
    )
    assert {f.drift_class for f in out} == {drift.CLASS_IMAGE, drift.CLASS_RUNTIME}
    log = api.read_event_log(WS, rec.id, vault_root=vault)
    assert len(log) == 2
    assert {entry["event_type"] for entry in log} == {
        drift.EVENT_IMAGE_CHANGED,
        drift.EVENT_RUNTIME_MISMATCH,
    }

    # A repeat scan yields identical findings and therefore emits nothing new.
    again = drift.scan_record(
        rec,
        containers,
        workspace_id=WS,
        permissions={},
        capabilities=caps,
        vault_root=vault,
    )
    assert again == []
    assert len(api.read_event_log(WS, rec.id, vault_root=vault)) == 2


# ── Read-only guarantee ─────────────────────────────────────────────────────


def test_drift_source_is_read_only():
    source = pathlib.Path(drift.__file__).read_text(encoding="utf-8")
    # The client is accepted duck-typed; the module imports no docker package.
    assert "import docker" not in source
    assert "docker.from_env" not in source
    # No Docker-mutating call is ever made.
    for name in _MUTATING:
        assert f".{name}(" not in source


def test_scan_record_touches_no_mutating_api(vault, caps):
    rec = api.create_record(
        WS,
        "ephemeral",
        "workspace-owned",
        intent_snapshot={"image_ref": "python:3.12"},
        vault_root=vault,
    )
    api.update_record(WS, rec.id, vault_root=vault, docker_id="d1")
    rec = api.load_record(WS, rec.id, vault)

    container = _GuardedContainer("d1", _attrs(image="python:3.11"))
    client = _GuardedContainers([container])

    out = drift.scan_record(
        rec,
        client,
        workspace_id=WS,
        permissions={},
        capabilities=caps,
        vault_root=vault,
    )

    # The only client interaction is a read-only listing.
    assert client.calls == [("list", True)]
    # Only ``attrs`` was read off the container; no mutating method was touched.
    assert container.touched == ["attrs"]
    assert not any(name in _MUTATING for name in container.touched)
    assert [f.drift_class for f in out] == [drift.CLASS_IMAGE]
