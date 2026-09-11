"""Bug 2: ContainerManager must derive container isolation from the security
gate SSOT (``get_expected_container_config``) and must never silently reuse a
drifted container on the workspace-label path."""
from unittest.mock import MagicMock

from infra.container_manager import ContainerManager
from security.security_gate import get_expected_container_config

SP = {"container": True, "network": "outbound", "filesystem": "write", "git": "read"}


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


def test_desired_config_equals_ssot():
    mgr = _mgr()
    net, ws = mgr._compute_config(mgr.workspace_path, mgr.workspace_id, SP)
    exp = get_expected_container_config(SP)
    assert net == exp["network_mode"]
    assert ws == exp["workspace_mode"]
    assert net != "none"


def _drive_start(fake_container, monkeypatch):
    import infra.container_manager as cm

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


def test_drifted_workspace_label_container_is_not_silently_reused(monkeypatch):
    drifted = _FakeContainer("c" * 16, network="none", workspace_rw=False)
    mgr, result = _drive_start(drifted, monkeypatch)
    assert result.get("status") != "reused"
    assert "error" in result
    assert mgr._remove_container.called


def test_matching_workspace_label_container_is_reused(monkeypatch):
    matching = _FakeContainer("d" * 16, network="bridge", workspace_rw=True)
    mgr, result = _drive_start(matching, monkeypatch)
    assert result.get("status") == "reused"
    assert not mgr._remove_container.called
