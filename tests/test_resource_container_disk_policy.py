"""RED-first regression test for the resource-container policy-widening bug.

Strand: bug/git-resource-container-create-policy-widening

THE BUG
-------
``GitReadTool._resolve_resource_execution`` -- the REAL resource-path call site
that builds the ``ResourceContainerManager`` for git -- derived the container
``network_mode`` from the IN-MEMORY session mirror
(``resolve_container_config(..., use_disk=False)``), while the admission gate's
own resolver call (``security.admission_gate._admit``) uses the
DISK-AUTHORITATIVE session sidecar (``resolve_container_config(...,
use_disk=True)``).

When the session sidecar grants ``network: write`` but the in-memory mirror
does not, the resource path proposes ``network_mode="none"`` while the gate
derives ``"bridge"``.  The gate then runs ``_narrow("none", "bridge")`` and
returns ``Deny(transform_widen_forbidden, "derived policy would widen:
network_mode")`` -- so the git resource container create fails and the
workspace's git tooling is dead.

WHAT THIS TEST DOES
-------------------
It exercises the REAL call site (``GitReadTool._resolve_resource_execution``)
and asserts that the network_mode it feeds to ``ResourceContainerManager``
equals the mode the admission gate derives from the same sidecar, and that the
value survives the gate's own ``_narrow`` transform.

It stubs ONLY the security vault boundaries (the permission-store read and the
workspace-capabilities read) and the docker boundary
(``ResourceContainerManager`` construction).  It NEVER stubs
``resolve_container_config`` itself -- doing so would test nothing.

Run:  pytest tests/test_resource_container_disk_policy.py -q
"""

import security.security_gate as gate
from security.admission_gate import Allow, ContainerSpec, _narrow
from security.security_gate import WorkspaceCapabilities
from thoughtmachine.container_record import LIFECYCLE_RESOURCE

_WORKSPACE_ID = "ws-diskpolicy"
_SESSION_ID = "sess-diskpolicy"

# In-memory session mirror: NO network grant ("banned" is a valid level and
# maps to network_mode "none").  The disk sidecar below grants network, so the
# two sources DISAGREE -- which is exactly the divergence under test.
_MIRROR = {"network": "banned", "filesystem": "write"}
# Disk session sidecar grants.
_DISK_GRANTS = {"network": "write", "filesystem": "write"}


def _stub_security_boundaries(monkeypatch):
    """Stub ONLY the vault/disk reads: permission store + workspace caps."""
    monkeypatch.setattr(
        gate,
        "_read_disk_permission_sources",
        lambda workspace_id, session_id: (_DISK_GRANTS, None),
    )
    monkeypatch.setattr(
        gate,
        "get_workspace_capabilities",
        lambda workspace_id: WorkspaceCapabilities.default(),
    )


def _stub_docker_boundary(monkeypatch, captured):
    """Stub ONLY the docker boundary: capture the manager's init kwargs."""
    import infra.resource_container_manager as rcm

    class _RecordingManager:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def ensure_resource(self, name):
            return {
                "mode": "containerized",
                "container_id": "fake-container",
                "status": "running",
                "image": "fake-image",
                "detail": "",
                "failure_reason": None,
                "fallback_used": False,
            }

    monkeypatch.setattr(rcm, "ResourceContainerManager", _RecordingManager)


def _make_tool():
    from tools.git_info_tool import GitReadTool

    tool = GitReadTool(
        operation="status",
        session_permissions=dict(_MIRROR),
        session_id=_SESSION_ID,
        workspace_id=_WORKSPACE_ID,
    )
    # Preconditions normally set by the registry-resolution step so that
    # ``_use_container_mode()`` is True and the resource path is entered.
    tool._resolved_workspace_path = "/tmp/ws-diskpolicy"
    tool._resolved_workspace_id = _WORKSPACE_ID
    return tool


def _gate_derived_config():
    """The gate's OWN derivation: disk-authoritative (use_disk=True)."""
    return gate.resolve_container_config(
        {},
        WorkspaceCapabilities.default(),
        LIFECYCLE_RESOURCE,
        session_id=_SESSION_ID,
        workspace_id=_WORKSPACE_ID,
        use_disk=True,
    )


def test_resource_path_derives_network_from_disk_sidecar(monkeypatch):
    """R1: the RESOURCE path derives the SAME network_mode as the gate."""
    _stub_security_boundaries(monkeypatch)
    captured = {}
    _stub_docker_boundary(monkeypatch, captured)

    tool = _make_tool()
    mode, _manager = tool._resolve_resource_execution()

    gate_cfg = _gate_derived_config()

    assert mode.get("mode") == "containerized", mode
    assert captured.get("network_mode") == gate_cfg.network_mode == "bridge", (
        "resource-path network_mode diverged from the admission gate's "
        "disk-authoritative derivation: "
        f"resource={captured.get('network_mode')!r} "
        f"gate={gate_cfg.network_mode!r}"
    )
    # The ids must have been threaded through so the derivation could engage
    # the disk store at all.
    assert captured.get("workspace_id") == _WORKSPACE_ID
    assert captured.get("session_id") == _SESSION_ID


def test_resource_path_network_survives_gate_narrowing(monkeypatch):
    """R2: the network the resource path proposes passes the gate's narrowing.

    Reproduces the gate's real post-create transform (``_narrow``) using the
    derived policy the gate itself computes from the same sidecar.  Pre-fix the
    resource path proposes "none" while the gate derives "bridge", so the guard
    returns ``Deny(transform_widen_forbidden, "derived policy would widen:
    network_mode")``.
    """
    _stub_security_boundaries(monkeypatch)
    captured = {}
    _stub_docker_boundary(monkeypatch, captured)

    tool = _make_tool()
    tool._resolve_resource_execution()
    resource_network = captured.get("network_mode")

    gate_cfg = _gate_derived_config()
    derived = {
        "network_mode": gate_cfg.network_mode,
        "mem_limit": None,
        "cpu_quota": None,
        "oom_score_adj": None,
        "read_only": True if gate_cfg.workspace_mode == "ro" else None,
    }
    spec = ContainerSpec(
        container_type="resource",
        lifecycle_class=LIFECYCLE_RESOURCE,
        workspace_id=_WORKSPACE_ID,
        session_id=_SESSION_ID,
        network_mode=resource_network,
        read_only=True,
    )
    decision = _narrow(spec, derived)
    assert isinstance(decision, Allow), (
        "gate denied the git resource container: "
        f"code={getattr(decision, 'code', None)!r} "
        f"message={getattr(decision, 'message', None)!r}"
    )
