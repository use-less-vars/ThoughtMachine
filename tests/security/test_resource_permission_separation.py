"""Per-resource permission separation + access-path hardening tests.

Hermetic unit tests (no Docker daemon, no real vault):

* ``ResourceContainerManager`` gates EACH hidden resource on its own
  permission key declared in ``rcm.RESOURCE_REGISTRY`` (``git`` ->
  ``"permission": "git"``), fail-closed when the gate cannot be consulted.
  Container access (``container=True``) no longer implies git access.

* ``ContainerManager`` (the generic agent-facing manager) refuses every
  access path to hidden resource containers (``tm-res-*``): start (name
  prefix), exec (PermissionError), stop/status/remove (error dict),
  get_logs (RuntimeError), ``_find_by_labels`` and ``list_containers``
  (invisible).
"""

import sys

import pytest

import infra.resource_container_manager as rcm
from infra.container_manager import ContainerManager


# ------------------------------------------------------------------ fakes


class _FakeImageRef:
    """``container.image`` fake with the ``.tags`` list the managers probe."""

    def __init__(self, tags=None):
        self.tags = list(tags or [])


class _FakeContainer:
    """Minimal container fake for both managers."""

    def __init__(
        self,
        container_id,
        name=None,
        labels=None,
        image_tags=None,
        status="running",
        attrs=None,
    ):
        self.id = container_id
        self.name = name or container_id
        self.labels = labels or {}
        self.image = _FakeImageRef(image_tags)
        self.status = status
        self.attrs = attrs or {}


class _FakeContainers:
    """``client.containers`` fake with ``list(all=True, filters=...)`` + ``get``."""

    def __init__(self, containers=None):
        self.containers = list(containers or [])

    def list(self, all=True, filters=None):
        return list(self.containers)

    def get(self, container_id):
        for c in self.containers:
            if c.id == container_id or c.name == container_id:
                return c
        raise KeyError(container_id)


class _FakeClient:
    def __init__(self, containers=None):
        if isinstance(containers, _FakeContainers):
            self.containers = containers
        else:
            self.containers = _FakeContainers(containers)


class _FakeDockerModule:
    """Stands in for the ``docker`` module: ``from_env()`` -> client."""

    def __init__(self, containers=None):
        self.containers = containers

    def from_env(self):
        return _FakeClient(self.containers)


# ------------------------------------------------------------------ helpers


def _make_resource_manager(monkeypatch, containers=None, session_permissions=None):
    monkeypatch.setattr(rcm, "docker", _FakeDockerModule(containers=containers))
    return rcm.ResourceContainerManager(
        workspace_id="ws-1",
        workspace_path="/tmp/tm-perm-ws",
        session_permissions=session_permissions,
    )


def _make_container_manager(containers=None):
    """ContainerManager instance that skips docker.from_env() (test pattern)."""
    manager = ContainerManager.__new__(ContainerManager)
    manager.client = _FakeClient(containers=containers or _FakeContainers())
    manager.image = "agent-executor"
    manager._session_config = None
    manager.workspace_id = "ws-1"
    manager.container_notes = {}
    return manager


def _resource_container(container_id="res-ctr"):
    return _FakeContainer(
        container_id,
        name="tm-res-abc123-git",
        labels={"thoughtmachine.resource": "git"},
        image_tags=["tm-resource-git"],
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ResourceContainerManager: per-resource permission separation
# ══════════════════════════════════════════════════════════════════════════════


def test_registry_declares_per_resource_permission():
    """Each registered resource declares its OWN permission key (not 'container')."""
    assert rcm.RESOURCE_REGISTRY["git"]["permission"] == "git"


def test_policy_denied_unavailable_git_banned(monkeypatch):
    mgr = _make_resource_manager(
        monkeypatch, session_permissions={"git": "banned"}
    )
    result = mgr.ensure_resource("git")
    assert result["mode"] == "unavailable"
    assert result["failure_reason"] == "policy_denied"
    assert "disabled/denied" in result["detail"]
    assert "git" in result["detail"].lower()


def test_policy_denied_container_allowed_but_git_missing(monkeypatch):
    """Container permission does NOT imply git: a missing 'git' key denies."""
    mgr = _make_resource_manager(
        monkeypatch,
        session_permissions={"container": True, "network": "none"},
    )
    result = mgr.ensure_resource("git")
    assert result["mode"] == "unavailable"
    assert result["failure_reason"] == "policy_denied"
    assert "denies resource 'git'" in result["detail"]


def test_policy_allowed_with_git_permission_reaches_container(monkeypatch):
    """With the 'git' permission granted the gate passes and the container is created."""
    mgr = _make_resource_manager(
        monkeypatch,
        session_permissions={"container": True, "network": "none", "git": True},
    )
    monkeypatch.setattr(rcm, "_ensure_resource_image", lambda: True)
    monkeypatch.setattr(mgr, "_find_resource_container", lambda: None)
    monkeypatch.setattr(
        mgr, "_create_resource_container", lambda: _FakeContainer("c-res")
    )
    result = mgr.ensure_resource("git")
    assert result["mode"] == "containerized"
    assert result["container_id"] == "c-res"


def test_unknown_resource_fail_closed(monkeypatch):
    mgr = _make_resource_manager(monkeypatch, session_permissions={"git": True})
    result = mgr.ensure_resource("nope")
    assert result["mode"] == "unavailable"
    assert result["failure_reason"] == "unknown_resource"


def test_missing_permission_key_fail_closed_even_without_permissions(monkeypatch):
    """A registry entry without a 'permission' key denies BEFORE the opt-in check."""
    monkeypatch.setattr(rcm, "RESOURCE_REGISTRY", {"noperm": {"kind": "x"}})
    mgr = _make_resource_manager(monkeypatch, session_permissions={"git": True})
    result = mgr.ensure_resource("noperm")
    assert result["mode"] == "unavailable"
    assert result["failure_reason"] == "policy_denied"
    assert "policy missing permission key" in result["detail"]
    # Also fail-closed when no session permissions are supplied at all.
    mgr2 = _make_resource_manager(monkeypatch, session_permissions=None)
    result2 = mgr2.ensure_resource("noperm")
    assert result2["mode"] == "unavailable"
    assert result2["failure_reason"] == "policy_denied"


def test_gate_call_exception_fail_closed(monkeypatch):
    import security.security_gate as sg

    def raiser(*args, **kwargs):
        raise RuntimeError("gate boom")

    monkeypatch.setattr(sg, "check_atomic_operation", raiser)
    mgr = _make_resource_manager(monkeypatch, session_permissions={"git": True})
    result = mgr.ensure_resource("git")
    assert result["mode"] == "unavailable"
    assert result["failure_reason"] == "policy_denied"
    assert "security gate check failed" in result["detail"]


def test_gate_import_exception_fail_closed(monkeypatch):
    monkeypatch.setitem(sys.modules, "security.security_gate", None)
    mgr = _make_resource_manager(monkeypatch, session_permissions={"git": True})
    result = mgr.ensure_resource("git")
    assert result["mode"] == "unavailable"
    assert result["failure_reason"] == "policy_denied"
    assert "security gate unavailable" in result["detail"]


def test_resource_status_policy_denied_git_banned():
    result = rcm.resource_status(
        "git", workspace_id="ws-1", session_permissions={"git": "banned"}
    )
    assert result["mode"] == "unavailable"
    assert result["failure_reason"] == "policy_denied"
    assert "disabled/denied" in result["detail"]
    assert "git" in result["detail"].lower()


def test_resource_policy_denied_direct_unit():
    assert (
        rcm._resource_policy_denied({"git": True}, "nope")
        == "unknown resource permission 'nope'"
    )
    # Opt-in: no session permissions supplied -> no denial.
    assert rcm._resource_policy_denied(None, "git") is None
    # Container granted, git not -> git denied (per-resource separation).
    reason = rcm._resource_policy_denied({"container": True}, "git")
    assert reason is not None
    assert "denies resource 'git'" in reason


# ══════════════════════════════════════════════════════════════════════════════
#  ContainerManager: access-path hardening (resource containers invisible)
# ══════════════════════════════════════════════════════════════════════════════


def test_cm_start_rejects_resource_name_prefix():
    manager = _make_container_manager()
    result = manager.start(name="tm-res-abc123-git")
    assert result == {"error": "Resource container access denied"}


def test_cm_exec_raises_permission_error():
    manager = _make_container_manager(containers=[_resource_container()])
    with pytest.raises(PermissionError, match="Resource container access denied"):
        manager.exec("res-ctr", ["ls"])


def test_cm_stop_rejects_resource_container():
    manager = _make_container_manager(containers=[_resource_container()])
    result = manager.stop("res-ctr")
    assert result["status"] == "error"
    assert result["error"] == "Resource container access denied"


def test_cm_status_rejects_resource_container():
    manager = _make_container_manager(containers=[_resource_container()])
    result = manager.status("res-ctr")
    assert result["status"] == "error"
    assert result["error"] == "Resource container access denied"


def test_cm_get_logs_rejects_resource_container(monkeypatch):
    import infra.container_manager as cm_mod

    monkeypatch.setattr(cm_mod, "DOCKER_AVAILABLE", True)
    manager = _make_container_manager(containers=[_resource_container()])
    with pytest.raises(RuntimeError, match="Resource container access denied"):
        manager.get_logs("res-ctr")


def test_cm_remove_propagates_denial():
    manager = _make_container_manager(containers=[_resource_container()])
    result = manager.remove("res-ctr")
    assert result["status"] == "error"
    assert result["error"] == "Resource container access denied"


def test_cm_list_containers_hides_resource_containers():
    resource = _FakeContainer(
        "res-ctr",
        name="tm-res-abc123-git",
        labels={
            "thoughtmachine.workspace_id": "ws-1",
            "thoughtmachine.resource": "git",
        },
        image_tags=["tm-resource-git"],
        status="running",
        attrs={"State": {}},
    )
    normal = _FakeContainer(
        "abc123",
        name="agent-exec-1",
        labels={"thoughtmachine.workspace_id": "ws-1"},
        image_tags=["agent-executor:latest"],
        status="running",
        attrs={"State": {}},
    )
    manager = _make_container_manager(containers=[resource, normal])
    result = manager.list_containers()
    assert [entry["container_id"] for entry in result] == ["abc123"]


def test_cm_find_by_labels_hides_resource_containers():
    resource = _resource_container()
    manager = _make_container_manager(containers=[resource])
    assert manager._find_by_labels("tm-res-abc123-git") is None
    # A normal container is still found.
    normal = _FakeContainer(
        "abc123",
        name="agent-exec-1",
        labels={
            "thoughtmachine.container_name": "agent-exec-1",
            "thoughtmachine.workspace_id": "ws-1",
        },
        image_tags=["agent-executor:latest"],
    )
    manager2 = _make_container_manager(containers=[normal])
    found = manager2._find_by_labels("agent-exec-1")
    assert found is not None
    assert found.id == "abc123"


def test_cm_is_resource_container_detection():
    # Label marks it regardless of name/image.
    by_label = _FakeContainer("a", name="plain", labels={"thoughtmachine.resource": "git"})
    assert ContainerManager._is_resource_container(by_label) is True
    # Name prefix alone.
    by_name = _FakeContainer("b", name="tm-res-zzz", labels={})
    assert ContainerManager._is_resource_container(by_name) is True
    # Image tag alone.
    by_image = _FakeContainer("c", name="plain", image_tags=["tm-resource-git"])
    assert ContainerManager._is_resource_container(by_image) is True
    # Normal container is not a resource container.
    normal = _FakeContainer("d", name="agent-exec-1", image_tags=["agent-executor:latest"])
    assert ContainerManager._is_resource_container(normal) is False


# ------------------------------------------------------------------ registry mode


class _FakeRegistryTracking:
    """Registry fake: ``list_all()`` exposes a registered resource container."""

    def __init__(self, normal_name=None):
        self.destroyed = []
        self.normal_name = normal_name

    def list_all(self):
        handles = [
            {
                "id": "res-ctr",
                "name": "tm-res-abc123-git",
                "status": "running",
                "container_type": "resource",
            }
        ]
        if self.normal_name:
            handles.append(
                {
                    "id": "abc123",
                    "name": self.normal_name,
                    "status": "running",
                    "container_type": "session",
                }
            )
        return handles

    def destroy_container(self, name):
        self.destroyed.append(name)


def _make_registry_mode_manager(monkeypatch, normal_name=None):
    """ContainerManager with an active registry that tracks a resource container."""
    import infra.container_manager as cm_mod

    manager = _make_container_manager(containers=[])
    manager._session_config = {"use_container_registry": True}
    manager.session_id = "sess-1"
    manager._containers = {}
    registry = _FakeRegistryTracking(normal_name=normal_name)
    monkeypatch.setattr(cm_mod, "get_active_registry", lambda config: registry)
    monkeypatch.setattr(cm_mod, "is_registry_active", lambda config: True)
    return manager, registry


def test_cm_stop_rejects_resource_container_in_registry_mode(monkeypatch):
    """Registry-branch stop() must refuse to destroy a tracked tm-res-* container."""
    manager, registry = _make_registry_mode_manager(monkeypatch)
    result = manager.stop("res-ctr")
    assert result["status"] == "error"
    assert result["error"] == "Resource container access denied"
    assert registry.destroyed == []


def test_cm_remove_rejects_resource_container_in_registry_mode(monkeypatch):
    """Registry-branch remove() must refuse to destroy a tracked tm-res-* container."""
    manager, registry = _make_registry_mode_manager(monkeypatch)
    result = manager.remove("res-ctr")
    assert result["status"] == "error"
    assert result["error"] == "Resource container access denied"
    assert registry.destroyed == []


def test_cm_stop_registry_mode_normal_container_still_destroyed(monkeypatch):
    """The registry branch still destroys ordinary (non-resource) containers."""
    manager, registry = _make_registry_mode_manager(
        monkeypatch, normal_name="agent-exec-1"
    )
    result = manager.stop("agent-exec-1")
    assert result["status"] == "stopped"
    assert registry.destroyed == ["agent-exec-1"]
