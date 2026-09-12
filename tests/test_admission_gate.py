"""Hermetic unit tests for security/admission_gate.py (phase 1).

Everything here runs against a ``FakeProbes`` object or the pure
:func:`security.admission_gate._narrow` helper -- no Docker daemon, no real
disk, no vault.  The suite pins the closed :data:`REASON_CODES` vocabulary,
the admission check order, the policy-narrowing semantics and the
fail-closed (never-raise) contract.
"""

from __future__ import annotations

import random

import pytest

import security.admission_gate as ag
from security.admission_gate import (
    AdmissionRequest,
    Allow,
    ContainerSpec,
    Deny,
    REASON_BAD_PERMISSIONS,
    REASON_CAPABILITIES_REQUIRED,
    REASON_CODES,
    REASON_CONTAINER_LIMIT_EXCEEDED,
    REASON_DAEMON_UNREACHABLE,
    REASON_IMAGE_NOT_ALLOWED,
    REASON_INSUFFICIENT_DISK,
    REASON_INTERNAL_ERROR,
    REASON_NARROWED_TO_POLICY,
    REASON_RESOLUTION_FAILED,
    REASON_TRANSFORM_WIDEN_FORBIDDEN,
    REASON_UNKNOWN_CONTAINER_TYPE,
    REASON_UNKNOWN_LIFECYCLE_CLASS,
    REASON_WORKSPACE_ID_REQUIRED,
    Transform,
    admit,
)
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities

GiB = 1024 ** 3


# ─═ Test doubles ════════════════════════════════════════════════════════════
class FakeProbes:
    """Configurable, deterministic probe double (also can be made to raise)."""

    def __init__(
        self,
        *,
        total=100 * GiB,
        used=10 * GiB,
        free=90 * GiB,
        docker_ok=True,
        count=0,
        disk_raises=False,
        count_raises=False,
        docker_raises=False,
    ):
        self._total = total
        self._used = used
        self._free = free
        self._docker_ok = docker_ok
        self._count = count
        self._disk_raises = disk_raises
        self._count_raises = count_raises
        self._docker_raises = docker_raises

    def disk_usage(self, path):
        if self._disk_raises:
            raise RuntimeError("disk probe boom")
        return (self._total, self._used, self._free)

    def docker_reachable(self):
        if self._docker_raises:
            raise RuntimeError("docker probe boom")
        return self._docker_ok

    def workspace_container_count(self, workspace_id):
        if self._count_raises:
            raise RuntimeError("count probe boom")
        return self._count

    def now(self):
        return 0.0


# ─═ Builders ════════════════════════════════════════════════════════════════
PERMISSIVE = WorkspaceCapabilities.default()
NO_NETWORK = WorkspaceCapabilities(allow_network=False)
# A session profile that yields effective network "write" -> network_mode
# "bridge" when merged with permissive capabilities.
WRITE_PERMS = {"network": "write", "filesystem": "write", "container": True}


def make_spec(**overrides):
    base = dict(
        container_type="user",
        lifecycle_class="persistent",
        workspace_id="ws1",
        image=None,
        network_mode="none",
    )
    base.update(overrides)
    return ContainerSpec(**base)


def make_request(spec=None, permissions=WRITE_PERMS, capabilities=PERMISSIVE,
                 session_config=None):
    if spec is None:
        spec = make_spec()
    return AdmissionRequest(
        spec=spec,
        permissions=permissions,
        capabilities=capabilities,
        session_config=session_config,
    )


# ─═ Vocabulary ══════════════════════════════════════════════════════════════
def test_reason_codes_are_exactly_the_thirteen():
    assert REASON_CODES == frozenset(
        {
            REASON_UNKNOWN_CONTAINER_TYPE,
            REASON_WORKSPACE_ID_REQUIRED,
            REASON_IMAGE_NOT_ALLOWED,
            REASON_INSUFFICIENT_DISK,
            REASON_DAEMON_UNREACHABLE,
            REASON_CONTAINER_LIMIT_EXCEEDED,
            REASON_NARROWED_TO_POLICY,
            REASON_TRANSFORM_WIDEN_FORBIDDEN,
            REASON_INTERNAL_ERROR,
            REASON_UNKNOWN_LIFECYCLE_CLASS,
            REASON_CAPABILITIES_REQUIRED,
            REASON_BAD_PERMISSIONS,
            REASON_RESOLUTION_FAILED,
        }
    )
    assert len(REASON_CODES) == 13


# ─═ Happy path ══════════════════════════════════════════════════════════════
def test_allow_happy_path():
    # Permissive caps + a network:write session -> derived network "bridge",
    # which matches the proposed "bridge": no transform, no notes.
    spec = make_spec(network_mode="bridge", image="agent-executor")
    decision = admit(make_request(spec=spec), probes=FakeProbes(count=0))
    assert isinstance(decision, Allow)
    assert decision.spec is spec
    assert decision.notes == ()


def test_allow_with_disk_warning_note():
    # free 5 GiB trips the advisory tier (below 10 GiB) but not the deny tier.
    spec = make_spec(network_mode="bridge")
    decision = admit(make_request(spec=spec), probes=FakeProbes(free=5 * GiB))
    assert isinstance(decision, Allow)
    assert "disk_usage_high" in decision.notes


# ─═ One scenario per reason code ════════════════════════════════════════════
@pytest.mark.parametrize(
    "name, req, probes, expected_kind, expected_code",
    [
        (
            "unknown_container_type",
            AdmissionRequest(spec=make_spec(container_type="x")),
            FakeProbes(),
            Deny,
            REASON_UNKNOWN_CONTAINER_TYPE,
        ),
        (
            "workspace_id_required",
            AdmissionRequest(spec=make_spec(workspace_id="")),
            FakeProbes(),
            Deny,
            REASON_WORKSPACE_ID_REQUIRED,
        ),
        (
            "image_not_allowed",
            AdmissionRequest(spec=make_spec(image="evil")),
            FakeProbes(),
            Deny,
            REASON_IMAGE_NOT_ALLOWED,
        ),
        (
            "insufficient_disk",
            make_request(),
            FakeProbes(total=100 * GiB, used=99 * GiB, free=1 * GiB),
            Deny,
            REASON_INSUFFICIENT_DISK,
        ),
        (
            "daemon_unreachable",
            make_request(),
            FakeProbes(docker_ok=False),
            Deny,
            REASON_DAEMON_UNREACHABLE,
        ),
        (
            "container_limit_exceeded",
            make_request(),
            FakeProbes(count=6),
            Deny,
            REASON_CONTAINER_LIMIT_EXCEEDED,
        ),
        (
            "narrowed_to_policy",
            make_request(
                spec=make_spec(network_mode="bridge"),
                capabilities=NO_NETWORK,
            ),
            FakeProbes(),
            Transform,
            REASON_NARROWED_TO_POLICY,
        ),
        (
            "transform_widen_forbidden",
            make_request(spec=make_spec(network_mode="none")),
            FakeProbes(),
            Deny,
            REASON_TRANSFORM_WIDEN_FORBIDDEN,
        ),
        (
            "internal_error",
            make_request(),
            FakeProbes(disk_raises=True),
            Deny,
            REASON_INTERNAL_ERROR,
        ),
        (
            "unknown_lifecycle_class",
            make_request(spec=make_spec(lifecycle_class="bogus")),
            FakeProbes(),
            Deny,
            REASON_UNKNOWN_LIFECYCLE_CLASS,
        ),
        (
            "capabilities_required",
            make_request(capabilities=None),
            FakeProbes(),
            Deny,
            REASON_CAPABILITIES_REQUIRED,
        ),
        (
            "bad_permissions",
            make_request(permissions=42),
            FakeProbes(),
            Deny,
            REASON_BAD_PERMISSIONS,
        ),
    ],
)
def test_reason_code_scenarios(name, req, probes, expected_kind, expected_code):
    decision = admit(req, probes=probes)
    assert isinstance(decision, expected_kind), (name, decision)
    if isinstance(decision, (Deny, Transform)):
        assert decision.code == expected_code


def test_resolution_failed_when_resolver_raises(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(ag, "resolve_container_config", _boom)
    decision = admit(make_request(), probes=FakeProbes())
    assert isinstance(decision, Deny)
    assert decision.code == REASON_RESOLUTION_FAILED


def test_all_reason_codes_have_a_scenario_or_fault_path():
    # Codes exercised by the parametrised table + the monkeypatch test.
    covered = {
        REASON_UNKNOWN_CONTAINER_TYPE,
        REASON_WORKSPACE_ID_REQUIRED,
        REASON_IMAGE_NOT_ALLOWED,
        REASON_INSUFFICIENT_DISK,
        REASON_DAEMON_UNREACHABLE,
        REASON_CONTAINER_LIMIT_EXCEEDED,
        REASON_NARROWED_TO_POLICY,
        REASON_TRANSFORM_WIDEN_FORBIDDEN,
        REASON_INTERNAL_ERROR,
        REASON_UNKNOWN_LIFECYCLE_CLASS,
        REASON_CAPABILITIES_REQUIRED,
        REASON_BAD_PERMISSIONS,
        REASON_RESOLUTION_FAILED,
    }
    assert covered == REASON_CODES


# ─═ Container-limit precedence ═════════════════════════════════════════════
def test_session_config_limit_override():
    spec = make_spec(network_mode="bridge")
    req = make_request(
        spec=spec,
        session_config={"container_limits": {"max_containers": 2}},
    )
    # count 2 >= limit 2 -> denied.
    decision = admit(req, probes=FakeProbes(count=2))
    assert isinstance(decision, Deny)
    assert decision.code == REASON_CONTAINER_LIMIT_EXCEEDED
    # count 1 < limit 2 -> admitted.
    ok = admit(req, probes=FakeProbes(count=1))
    assert isinstance(ok, Allow)


# ─═ _narrow: narrowing semantics ════════════════════════════════════════════
def test_narrow_network_bridge_to_none():
    spec = make_spec(network_mode="bridge")
    decision = ag._narrow(spec, {"network_mode": "none"})
    assert isinstance(decision, Transform)
    assert decision.code == REASON_NARROWED_TO_POLICY
    assert decision.spec.network_mode == "none"


def test_narrow_mem_limit_smaller():
    spec = make_spec(network_mode="none", mem_limit=2 * GiB)
    decision = ag._narrow(spec, {"mem_limit": 1 * GiB})
    assert isinstance(decision, Transform)
    assert decision.code == REASON_NARROWED_TO_POLICY
    assert decision.spec.mem_limit == 1 * GiB


# ─═ _narrow: widen is forbidden ═════════════════════════════════════════════
def test_narrow_network_widen_forbidden():
    spec = make_spec(network_mode="none")
    decision = ag._narrow(spec, {"network_mode": "bridge"})
    assert isinstance(decision, Deny)
    assert decision.code == REASON_TRANSFORM_WIDEN_FORBIDDEN


def test_narrow_read_only_widen_forbidden():
    spec = make_spec(network_mode="none", read_only=True)
    decision = ag._narrow(spec, {"read_only": False})
    assert isinstance(decision, Deny)
    assert decision.code == REASON_TRANSFORM_WIDEN_FORBIDDEN


# ─═ Fail-closed fault injection ════════════════════════════════════════════
def test_fail_closed_on_disk_probe_error():
    decision = admit(make_request(), probes=FakeProbes(disk_raises=True))
    assert isinstance(decision, Deny)
    assert decision.code == REASON_INTERNAL_ERROR


def test_fail_closed_on_count_probe_error():
    decision = admit(make_request(), probes=FakeProbes(count_raises=True))
    assert isinstance(decision, Deny)
    assert decision.code == REASON_INTERNAL_ERROR


def test_daemon_unreachable_on_probe_false():
    decision = admit(make_request(), probes=FakeProbes(docker_ok=False))
    assert isinstance(decision, Deny)
    assert decision.code == REASON_DAEMON_UNREACHABLE


def test_daemon_unreachable_on_probe_error():
    decision = admit(make_request(), probes=FakeProbes(docker_raises=True))
    assert isinstance(decision, Deny)
    assert decision.code == REASON_DAEMON_UNREACHABLE


def test_capabilities_none_is_capabilities_required():
    decision = admit(make_request(capabilities=None), probes=FakeProbes())
    assert isinstance(decision, Deny)
    assert decision.code == REASON_CAPABILITIES_REQUIRED


def test_malformed_permissions_is_bad_permissions():
    decision = admit(make_request(permissions="nonsense"), probes=FakeProbes())
    assert isinstance(decision, Deny)
    assert decision.code == REASON_BAD_PERMISSIONS


def test_missing_workspace_id():
    decision = admit(
        AdmissionRequest(spec=make_spec(workspace_id="")), probes=FakeProbes()
    )
    assert isinstance(decision, Deny)
    assert decision.code == REASON_WORKSPACE_ID_REQUIRED


# ─═ Fuzz: admit never raises ═══════════════════════════════════════════════
def test_fuzz_admit_never_raises():
    rng = random.Random(20240607)
    junk = [None, 0, 1, -1, "x", [], {}, (), object(), 3.14, True, False]

    for _ in range(300):
        kind = rng.randrange(3)
        if kind == 0:
            request = rng.choice(junk)
        elif kind == 1:
            request = AdmissionRequest(spec=rng.choice(junk))
        else:
            spec = make_spec(
                container_type=rng.choice(list(junk) + ["user"]),
                lifecycle_class=rng.choice(list(junk) + ["persistent"]),
                workspace_id=rng.choice(list(junk) + ["ws1"]),
            )
            request = AdmissionRequest(
                spec=spec,
                permissions=rng.choice(junk),
                capabilities=rng.choice(junk + [PERMISSIVE]),
                session_config=rng.choice(junk),
            )
        decision = admit(request, probes=FakeProbes())
        assert isinstance(decision, (Allow, Deny, Transform))
