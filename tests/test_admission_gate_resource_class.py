"""Focused tests: the per-workspace container cap must be CLASS-AWARE.

The cap counts only *persistent*-class, non-terminal containers
(:func:`security.admission_gate._counts_toward_ceiling`), because only those
consume a workspace slot.  The step-6 refusal, however, was evaluated on that
persistent count alone -- regardless of the class being CREATED -- so a
workspace with a full persistent budget could never create a RESOURCE-class
container (e.g. the git resource container
``tm-res-<hash>-git``), even though creating one does not increase the count
being checked against.

These tests pin the asymmetry in both directions: a RESOURCE create is admitted
on a full persistent budget, a PERSISTENT create is still refused on it.

The doubles (``FakeDockerClient`` / ``_container`` / ``_pin_disk_seam`` /
``make_spec`` / ``make_request`` / ``PERMISSIVE``) are the
ones already used by ``tests/test_admission_gate.py`` -- reused, not
re-invented, so the two files mock the same seam the same way.
"""

from __future__ import annotations

import security.admission_gate as ag
from security.admission_gate import (
    AdmissionRequest,
    Allow,
    ContainerSpec,
    Deny,
    REASON_CONTAINER_LIMIT_EXCEEDED,
    admit,
)
from tests.test_admission_gate import (
    PERMISSIVE,
    FakeDockerClient,
    _container,
    _pin_disk_seam,
    make_request,
    make_spec,
)

# The live git resource container: image and deterministic name prefix come
# straight from infra/resource_container_manager.py (RESOURCE_IMAGE_TAG /
# ``tm-res-<sha12(workspace_path)>-git``).
RESOURCE_IMAGE = "tm-resource-git"
RESOURCE_NAME = "tm-res-abc123def456-git"
RESOURCE_LABEL = "thoughtmachine.resource"

# Default per-workspace ceiling (agent/config/defaults.py DEFAULT_MAX_CONTAINERS).
FULL_CAP = {"container_limits": {"max_containers": 6}}


def _resource_spec(**overrides):
    """A RESOURCE-class spec shaped like the git resource container's."""
    base = dict(
        container_type="resource",
        lifecycle_class="resource",
        workspace_id="ws1",
        image=RESOURCE_IMAGE,
        name=RESOURCE_NAME,
        network_mode="none",
        read_only=True,
    )
    base.update(overrides)
    return ContainerSpec(**base)


def _pin_resource_disk_seam(monkeypatch):
    """Pin the vault read to the EMPTY grants the resource create site uses.

    The resource create site submits ``permissions={}`` while ``admit``
    resolves the envelope from disk; pinning the disk seam to the same empty
    grants keeps the derived policy identical to the requested one (otherwise
    the permissive session grants of ``_pin_disk_seam``'s default would derive
    ``bridge`` and the request's ``none`` would look like a widening).
    """
    _pin_disk_seam(monkeypatch, grants={})


def _resource_request(session_config=None):
    """The admission request the resource create site actually builds.

    Mirrors ``infra/resource_container_manager.py:1124-1146``: ``permissions={}``
    (an empty Mapping, not ``None``) because the per-resource grant was already
    enforced upstream.
    """
    return AdmissionRequest(
        spec=_resource_spec(),
        permissions={},
        capabilities=PERMISSIVE,
        session_config=session_config,
    )


def _persistent_containers(count, status="running"):
    """``count`` live, persistent-class container doubles."""
    return [_container(f"live-{i}", status=status) for i in range(count)]


def _resource_container():
    """A live RESOURCE-class container double (git resource container)."""
    return _container(
        RESOURCE_NAME, labels={RESOURCE_LABEL: "git"}, status="running",
        image=RESOURCE_IMAGE,
    )


# ── 1. The bug: a full persistent budget must not block a RESOURCE create ────
def test_resource_create_allowed_when_persistent_cap_is_full(monkeypatch):
    """6 live persistent containers + RESOURCE create -> ALLOWED.

    Before the fix this returned ``Deny(container_limit_exceeded)`` with
    ``"container limit reached (6/6 consuming container(s))"``: the refusal ran
    on the persistent count even though a resource container consumes no slot,
    so the workspace could never provision its git resource container again.
    """
    _pin_resource_disk_seam(monkeypatch)
    probes = ag.ClientProbes(
        FakeDockerClient(items=_persistent_containers(6), ping=lambda: True)
    )
    # The count really is at the ceiling -- so the ONLY thing that can permit
    # the resource create is the class-awareness of the check itself.
    assert probes.workspace_container_count("ws1") == 6

    decision = admit(_resource_request(session_config=FULL_CAP), probes=probes)
    assert isinstance(decision, Allow), decision


# ── 2. The cap still applies to the class it counts ──────────────────────────
def test_persistent_create_still_refused_when_persistent_cap_is_full(monkeypatch):
    """6 live persistent containers + PERSISTENT create -> still refused."""
    _pin_disk_seam(monkeypatch)
    probes = ag.ClientProbes(
        FakeDockerClient(items=_persistent_containers(6), ping=lambda: True)
    )
    decision = admit(
        make_request(
            spec=make_spec(network_mode="bridge", image="agent-executor"),
            session_config=FULL_CAP,
        ),
        probes=probes,
    )
    assert isinstance(decision, Deny)
    assert decision.code == REASON_CONTAINER_LIMIT_EXCEEDED
    assert "(6/6" in decision.message


# ── 3. No regression below the ceiling ───────────────────────────────────────
def test_resource_create_allowed_below_the_cap(monkeypatch):
    """5 live persistent containers + RESOURCE create -> ALLOWED."""
    _pin_resource_disk_seam(monkeypatch)
    probes = ag.ClientProbes(
        FakeDockerClient(items=_persistent_containers(5), ping=lambda: True)
    )
    assert probes.workspace_container_count("ws1") == 5

    decision = admit(_resource_request(session_config=FULL_CAP), probes=probes)
    assert isinstance(decision, Allow), decision


# ── 4. A resource container never changes the consuming count ────────────────
def test_resource_container_does_not_change_the_consuming_count(monkeypatch):
    """Presence of a RESOURCE container leaves the persistent count untouched.

    Six persistent containers plus an existing git resource container still
    count 6 -- so the resource container neither blocks a PERSISTENT create
    (the cap is already full from the persistent ones) nor inflates the count
    the RESOURCE create is measured against.
    """
    _pin_resource_disk_seam(monkeypatch)
    resource = _resource_container()
    probes = ag.ClientProbes(
        FakeDockerClient(
            items=_persistent_containers(6) + [resource], ping=lambda: True
        )
    )

    # The resource container itself occupies no slot...
    assert ag._counts_toward_ceiling(resource) is False
    # ...so 7 containers are present but only 6 consume the ceiling.
    assert probes.workspace_container_count("ws1") == 6

    # RESOURCE create: admitted (resource containers do not consume slots).
    assert isinstance(
        admit(_resource_request(session_config=FULL_CAP), probes=probes), Allow
    )

    # PERSISTENT create: still refused by the 6 persistent consumers.
    decision = admit(
        make_request(
            spec=make_spec(network_mode="bridge", image="agent-executor"),
            session_config=FULL_CAP,
        ),
        probes=probes,
    )
    assert isinstance(decision, Deny)
    assert decision.code == REASON_CONTAINER_LIMIT_EXCEEDED
    assert "(6/6" in decision.message
