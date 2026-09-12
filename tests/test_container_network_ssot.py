"""Bug 2: ContainerManager must derive container isolation from the security
gate SSOT (``security.security_gate.resolve_container_config``) and must never
silently reuse a drifted container on the workspace-label path.
"""
from unittest.mock import MagicMock

import pytest

import infra.container_manager as cm
from infra.container_manager import ContainerManager
from security.security_gate import WorkspaceCapabilities, resolve_container_config
from thoughtmachine.container_record import LIFECYCLE_PERSISTENT

SP = {"container": True, "network": "outbound", "filesystem": "write", "git": "read"}


@pytest.fixture
def permissive_caps(monkeypatch):
    """Seed a fully-permissive capability set so the gate resolves positively.

    ``ContainerManager._compute_config`` loads capabilities with the canonical
    ``security.security_gate.get_workspace_capabilities`` loader (imported inside
    the function body, so patching the module attribute takes effect at call
    time).  The real loader reads the vault from disk and fail-closes to the
    restrictive ceiling in a hermetic test environment.
    """
    monkeypatch.setattr(
        "security.security_gate.get_workspace_capabilities",
        lambda workspace_id=None: WorkspaceCapabilities.default(),
    )


def _mgr():
    mgr = ContainerManager.__new__(ContainerManager)
    mgr.workspace_path = "/tmp/ws"
    mgr.workspace_id = "ws-1"
    mgr.session_permissions = dict(SP)
    mgr.session_id = "sess-1"
    mgr.image = "img"
    mgr._containers = {}
    mgr.container_notes = {}
    mgr._session_config = None
    return mgr


class _FakeContainer:
    def __init__(self, cid, network, workspace_rw):
        self.id = cid
        self.attrs = {
            "HostConfig": {"NetworkMode": network},
            "Mounts": [{"Destination": "/workspace", "RW": workspace_rw}],
        }

    def reload(self):
        pass

    @property
    def status(self):
        return "running"


def test_desired_config_matches_gate_ssot(permissive_caps):
    mgr = _mgr()
    net, ws = mgr._compute_config(mgr.workspace_path, mgr.workspace_id, SP)
    cfg = resolve_container_config(SP, WorkspaceCapabilities.default(), LIFECYCLE_PERSISTENT)
    assert net == cfg.network_mode
    assert ws == cfg.workspace_mode
    assert net == "bridge"
    assert ws == "rw"
    assert net != "none"


def test_desired_config_fails_closed_without_capabilities(monkeypatch):
    # Fail-closed oracle: an absent capability set must resolve to ("none", "ro").
    monkeypatch.setattr(
        "security.security_gate.get_workspace_capabilities",
        lambda workspace_id=None: None,
    )
    mgr = _mgr()
    assert mgr._compute_config(mgr.workspace_path, mgr.workspace_id, SP) == ("none", "ro")


def _drive_start(fake_container, monkeypatch):
    mgr = _mgr()
    mgr.list_containers = lambda: [{"name": "agent-x", "container_id": "c1", "note": ""}]
    client = MagicMock()
    client.containers.get = lambda cid: fake_container
    mgr.client = client
    mgr._remove_container = MagicMock()
    mgr._find_by_labels = lambda n: None
    mgr._get_max_containers = lambda: 0
    mgr._active_containers = lambda cs: []
    monkeypatch.setattr(cm, "is_registry_active", lambda *a, **k: False)
    return mgr, mgr.start(name="agent-x")


def test_drifted_workspace_label_container_is_not_silently_reused(monkeypatch, permissive_caps):
    drifted = _FakeContainer("c" * 16, network="none", workspace_rw=False)
    mgr, result = _drive_start(drifted, monkeypatch)
    assert result.get("status") != "reused"
    assert "error" in result
    assert mgr._remove_container.called


def test_matching_workspace_label_container_is_reused(monkeypatch, permissive_caps):
    matching = _FakeContainer("d" * 16, network="bridge", workspace_rw=True)
    mgr, result = _drive_start(matching, monkeypatch)
    assert result.get("status") == "reused"
    assert not mgr._remove_container.called
