"""Unit tests for the workspace-lifecycle wiring (provision + startup sweep).

Covers:
- ``infra.resource_container_manager.provision_workspace_resource`` (best-effort
  provisioning wrapper) and ``prune_unreferenced_resource_images``.
- The resolve-path endpoint (``web_ui.backend.workspace_routes``) provisioning
  the resource after registration without breaking the response.
- ``setup_workspace.main()`` provisioning after registration.
- ``web_ui.backend.server._sweep_orphan_resource_containers`` (startup sweep)
  delegating to the rcm sweep/prune helpers.

No Docker daemon required — the ``docker`` module attribute of
``infra.resource_container_manager`` is replaced with the same fakes used by
``test_resource_lifecycle``.
"""

import asyncio
import os

import pytest

import infra.resource_container_manager as rcm
import setup_workspace as setup_workspace
import web_ui.backend.server as server
import web_ui.backend.workspace_routes as workspace_routes

try:
    from docker.errors import ImageNotFound
except Exception:  # pragma: no cover - docker SDK absent
    ImageNotFound = Exception


# ------------------------------------------------------------------ fakes


class _FakeImage:
    def __init__(self, image_id="sha256:img-current"):
        self.id = image_id


class _FakeImages:
    """``client.images`` fake with ``get`` and ``remove`` recording."""

    def __init__(self, present=True):
        self.present = present
        self.get_calls = []
        self.remove_calls = []

    def get(self, tag):
        self.get_calls.append(tag)
        if not self.present:
            raise ImageNotFound(tag)
        return _FakeImage()

    def remove(self, tag, force=False):
        self.remove_calls.append({"tag": tag, "force": force})
        self.present = False


class _FakeContainer:
    def __init__(self, container_id, labels=None, owner=None):
        self.id = container_id
        self.labels = labels or {}
        self.owner = owner

    def remove(self, force=False):
        if self.owner is not None and self in self.owner.containers:
            self.owner.containers.remove(self)


def _matches_filters(container, filters):
    """Emulate docker-py's label-filter semantics for the fakes."""
    if not filters:
        return True
    label_filters = filters.get("label")
    if label_filters:
        if isinstance(label_filters, str):
            label_filters = [label_filters]
        for lf in label_filters:
            labels = container.labels or {}
            if "=" in lf:
                key, value = lf.split("=", 1)
                if labels.get(key) != value:
                    return False
            elif labels.get(lf) is None:
                return False
    return True


class _FakeContainers:
    """``client.containers`` fake with label-filtered ``list``."""

    def __init__(self, containers=None):
        self.containers = list(containers or [])
        for container in self.containers:
            container.owner = self

    def list(self, all=True, filters=None):
        return [c for c in self.containers if _matches_filters(c, filters)]


class _FakeClient:
    def __init__(self, images=None, containers=None):
        self.images = images
        self.containers = containers


class _FakeDockerModule:
    """Stands in for the ``docker`` module: ``from_env()`` -> client."""

    def __init__(self, images=None, containers=None):
        self.images = images
        self.containers = containers

    def from_env(self):
        return _FakeClient(self.images, self.containers)


class _FakeManager:
    """ResourceContainerManager stand-in recording ctor args + ensure calls."""

    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        _FakeManager.instances.append(self)

    def ensure_resource(self, name):
        self.ensured = name
        return {"mode": "containerized", "resource": name}


class _FakeEntry:
    def __init__(self, workspace_id, root_path):
        self.id = workspace_id
        self.root_path = root_path


class _FakeRegistry:
    """WorkspaceRegistry stand-in: resolve_by_root -> None, register -> entry."""

    def __init__(self, entry):
        self._entry = entry
        self.registered_roots = []

    def resolve_by_root(self, path):
        return None

    def register_by_root(self, confined, label="", metadata=None):
        self.registered_roots.append(confined)
        return self._entry


# --------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _reset_image_ready():
    """Isolate the module-level image-readiness cache per test."""
    rcm._RESOURCE_IMAGE_READY = False
    yield
    rcm._RESOURCE_IMAGE_READY = False


def _resource_labels(workspace_id, kind="git"):
    return {
        rcm.ResourceContainerManager.WORKSPACE_LABEL: workspace_id,
        rcm.ResourceContainerManager.RESOURCE_LABEL: kind,
        rcm.ResourceContainerManager.CONTAINER_NAME_LABEL: f"tm-res-{workspace_id}-git",
    }


# ------------------------------------------------- provision tests


def test_provision_workspace_resource_calls_ensure_resource(monkeypatch):
    _FakeManager.instances = []
    monkeypatch.setattr(rcm, "ResourceContainerManager", _FakeManager)

    perms = {"network": True}
    result = rcm.provision_workspace_resource("ws-1", "/tmp/ws", perms)

    assert result == {"mode": "containerized", "resource": "git"}
    inst = _FakeManager.instances[-1]
    assert inst.ensured == "git"
    assert inst.args == ("ws-1", "/tmp/ws")
    assert inst.kwargs["network_mode"] in ("bridge", "none")
    assert inst.kwargs["image"] == rcm.RESOURCE_IMAGE_TAG
    assert inst.kwargs["vault_root"] == os.path.join(
        os.path.expanduser("~"), ".thoughtmachine"
    )
    assert inst.kwargs["session_config"] == {}
    assert inst.kwargs["session_id"] is None
    assert inst.kwargs["session_permissions"] is perms


def test_provision_workspace_resource_never_raises(monkeypatch):
    class _BoomManager:
        def __init__(self, *args, **kwargs):
            pass

        def ensure_resource(self, name):
            raise RuntimeError("boom")

    monkeypatch.setattr(rcm, "ResourceContainerManager", _BoomManager)
    result = rcm.provision_workspace_resource("ws-1", "/tmp/ws", None)
    assert result["mode"] == "unavailable"
    assert "boom" in result["detail"]


def test_module_imports_no_circular():
    """All wired modules import in one process without circular-import errors."""
    assert callable(rcm.provision_workspace_resource)
    assert callable(rcm.prune_unreferenced_resource_images)
    assert callable(workspace_routes.resolve_workspace_path)
    assert callable(server._sweep_orphan_resource_containers)
    assert callable(setup_workspace.main)


def test_register_by_root_provisions(monkeypatch, tmp_path):
    entry = _FakeEntry("ws-1", str(tmp_path))
    registry = _FakeRegistry(entry)
    monkeypatch.setattr(
        workspace_routes.WorkspaceRegistry,
        "get_default",
        staticmethod(lambda: registry),
    )
    monkeypatch.setattr(
        workspace_routes, "_confine_to_home", lambda path: str(tmp_path)
    )

    provision_calls = []

    def _fake_provision(workspace_id, workspace_path, session_permissions=None):
        provision_calls.append((workspace_id, workspace_path, session_permissions))
        return {"mode": "containerized"}

    monkeypatch.setattr(rcm, "provision_workspace_resource", _fake_provision)

    result = asyncio.run(
        workspace_routes.resolve_workspace_path(
            workspace_routes.ResolvePathBody(path=str(tmp_path))
        )
    )
    assert result == {"workspace_id": "ws-1", "root": str(tmp_path)}
    assert provision_calls == [("ws-1", str(tmp_path), None)]

    # Provisioning raising must NOT break the endpoint response.
    def _boom_provision(workspace_id, workspace_path, session_permissions=None):
        raise RuntimeError("prov boom")

    monkeypatch.setattr(rcm, "provision_workspace_resource", _boom_provision)
    result2 = asyncio.run(
        workspace_routes.resolve_workspace_path(
            workspace_routes.ResolvePathBody(path=str(tmp_path))
        )
    )
    assert result2 == {"workspace_id": "ws-1", "root": str(tmp_path)}


def test_setup_workspace_provisions(monkeypatch):
    """setup_workspace.main() provisions after register_by_root (best-effort)."""
    import thoughtmachine.workspace_registry as wreg

    expected_root = os.path.abspath(setup_workspace._PROJECT_ROOT)
    entry = _FakeEntry("ws-setup", expected_root)
    registered = []

    class _SetupRegistry:
        def register_by_root(self, root_path, label="", metadata=None):
            registered.append(root_path)
            return entry

    monkeypatch.setattr(
        wreg.WorkspaceRegistry, "get_default", staticmethod(lambda: _SetupRegistry())
    )
    monkeypatch.setattr(setup_workspace, "_write_identity_file", lambda *a, **k: None)
    monkeypatch.setattr(setup_workspace, "_write_config_json", lambda *a, **k: None)
    monkeypatch.setattr(
        "thoughtmachine.workspace_capabilities.ensure_workspace_dirs",
        lambda *a, **k: None,
    )

    provision_calls = []

    def _fake_provision(workspace_id, workspace_path, session_permissions=None):
        provision_calls.append((workspace_id, workspace_path))
        return {"mode": "containerized"}

    monkeypatch.setattr(rcm, "provision_workspace_resource", _fake_provision)

    setup_workspace.main()

    assert registered == [expected_root]
    assert provision_calls == [("ws-setup", expected_root)]


# ------------------------------------------------- startup sweep tests


def _monkeypatch_registry(monkeypatch, entry_ids):
    """Point server.WorkspaceRegistry at a fake default + list_workspaces."""
    registry = server.WorkspaceRegistry.__new__(server.WorkspaceRegistry)
    monkeypatch.setattr(
        server.WorkspaceRegistry, "get_default", staticmethod(lambda: registry)
    )
    monkeypatch.setattr(
        server.WorkspaceRegistry,
        "list_workspaces",
        staticmethod(lambda: [_FakeEntry(wid, "/root") for wid in entry_ids]),
    )
    return registry


def test_startup_sweep_wiring(monkeypatch):
    _monkeypatch_registry(monkeypatch, ["ws-1", "ws-2"])

    sweep_calls = []

    def _fake_sweep(ids):
        sweep_calls.append(list(ids))
        return {"removed": 1, "skipped_in_use": 1, "detail": ""}

    monkeypatch.setattr(rcm, "sweep_stale_resource_containers", _fake_sweep)

    prune_calls = []

    def _fake_prune():
        prune_calls.append(True)
        return {"removed_images": [], "remaining_containers": 1, "detail": ""}

    monkeypatch.setattr(rcm, "prune_unreferenced_resource_images", _fake_prune)

    server._sweep_orphan_resource_containers()

    assert sweep_calls == [["ws-1", "ws-2"]]
    assert prune_calls == [True]


def test_sweep_failure_never_breaks_startup(monkeypatch):
    _monkeypatch_registry(monkeypatch, ["ws-1"])

    def _boom_sweep(ids):
        raise RuntimeError("sweep boom")

    monkeypatch.setattr(rcm, "sweep_stale_resource_containers", _boom_sweep)

    def _boom_prune():
        raise RuntimeError("prune boom")

    monkeypatch.setattr(rcm, "prune_unreferenced_resource_images", _boom_prune)

    # Must not raise: failures are logged and swallowed.
    server._sweep_orphan_resource_containers()


def test_prune_unreferenced_resource_images(monkeypatch):
    # Scenario A: no resource containers remain -> image is removed.
    images = _FakeImages(present=True)
    containers = _FakeContainers([])
    monkeypatch.setattr(
        rcm, "docker", _FakeDockerModule(images=images, containers=containers)
    )
    rcm._RESOURCE_IMAGE_READY = True

    result = rcm.prune_unreferenced_resource_images()
    assert result["removed_images"] == [rcm.RESOURCE_IMAGE_TAG]
    assert result["remaining_containers"] == 0
    assert images.get_calls == [rcm.RESOURCE_IMAGE_TAG]
    assert images.remove_calls == [{"tag": rcm.RESOURCE_IMAGE_TAG, "force": True}]
    assert rcm._RESOURCE_IMAGE_READY is False

    # Scenario B: a resource container still exists -> image is KEPT.
    images_b = _FakeImages(present=True)
    containers_b = _FakeContainers(
        [_FakeContainer("c1", labels=_resource_labels("ws-1"))]
    )
    monkeypatch.setattr(
        rcm, "docker", _FakeDockerModule(images=images_b, containers=containers_b)
    )

    result_b = rcm.prune_unreferenced_resource_images()
    assert result_b["removed_images"] == []
    assert result_b["remaining_containers"] == 1
    assert images_b.get_calls == []
    assert images_b.remove_calls == []
