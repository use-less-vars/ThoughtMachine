"""Bug 2: ContainerManager must derive container isolation from the security
gate SSOT (``security.security_gate.resolve_container_config``) and must never
silently reuse a drifted container on the workspace-label path.

A container whose LIVE isolation is MORE permissive than the resolved policy is
REFUSED (an error carrying the drift detail) and left UNTOUCHED - ``start`` no
longer REMOVES or recreates it on drift.  A non-more-permissive drift is reused
with a ``drift`` detail attached.
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


def _write_disk_grant(vault, workspace_id, session_id, grants):
    """Seed the on-disk permission store so disk-mode resolution is positive.

    ``_compute_config`` threads BOTH the session and workspace ids into the
    security gate, so the vault permission store is the source of truth and a
    grant must be seeded for the gate to resolve ``("bridge", "rw")`` instead
    of fail-closing to ``("none", "ro")``.
    """
    import json as _json

    from thoughtmachine.permission_store import write_session_permissions

    write_session_permissions(vault, workspace_id, session_id, grants)
    cfg_dir = vault / "workspaces" / workspace_id
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(_json.dumps({"permissions": dict(grants)}))


@pytest.fixture
def disk_grant(tmp_path, monkeypatch):
    """Pin ``vault_root`` to a hermetic vault holding the manager's grant.

    ``_mgr`` builds a manager with ``workspace_id="ws-1"`` /
    ``session_id="sess-1"``; both ids present engage disk mode, so the matching
    session grant (sidecar) and workspace ceiling (``config.json``) are seeded.
    """
    import thoughtmachine.vault

    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(thoughtmachine.vault, "vault_root", lambda: vault)
    _write_disk_grant(
        vault, "ws-1", "sess-1", {"network": "outbound", "filesystem": "write"}
    )
    return vault


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
        self.removed = []

    def reload(self):
        pass

    def remove(self, **kwargs):
        self.removed.append(kwargs)

    @property
    def status(self):
        return "running"


def test_desired_config_matches_gate_ssot(permissive_caps, disk_grant):
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
    # High limit: the drift REFUSAL must come from the drift decision itself,
    # NOT from the workspace container-limit guard.
    mgr._get_max_containers = lambda: 100
    mgr._active_containers = lambda cs: []
    return mgr, mgr.start(name="agent-x")


def test_drifted_workspace_label_container_is_not_silently_reused(monkeypatch, permissive_caps):
    # Live "host" network is MORE permissive than the resolved ("bridge")
    # policy -> the workspace-label reuse path must REFUSE, never mutate.
    drifted = _FakeContainer("c" * 16, network="host", workspace_rw=True)
    mgr, result = _drive_start(drifted, monkeypatch)
    assert result.get("status") != "reused"
    assert "error" in result
    assert result["drift"]["decision"] == "deny"
    assert result["drift"]["source"] == "workspace-label"
    # The new policy does NOT remove/recreate on drift: left untouched.
    assert drifted.removed == []
    assert not mgr._remove_container.called


def test_matching_workspace_label_container_is_reused(monkeypatch, permissive_caps, disk_grant):
    import thoughtmachine.container_record as cr

    emitted = []
    monkeypatch.setattr(
        cr, "append_event",
        lambda *a, **k: emitted.append(a[2] if len(a) > 2 else k.get("event_type")),
        raising=True,
    )
    matching = _FakeContainer("d" * 16, network="bridge", workspace_rw=True)
    mgr, result = _drive_start(matching, monkeypatch)
    assert result.get("status") == "reused"
    assert "drift" not in result
    assert not mgr._remove_container.called
    # A NON-drifted container publishes NO start-drift event.
    assert "drift.start_on_drifted_container" not in emitted
