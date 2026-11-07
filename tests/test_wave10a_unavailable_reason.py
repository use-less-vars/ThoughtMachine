"""Wave 10a -- ``ContainerStatusTool`` surfaces ``unavailable_reason``.

``ContainerManager.unavailable_reason`` reports (as a ``"; "``-joined string,
or ``None`` when within bounds) the session requirements a PERSISTENT
container's live isolation fails to satisfy. It is RECOMPUTED on every call
from the live envelope -- never cached -- and ``_describe_container`` only
attaches it (as a neutral ``None``) for persistent containers.

Hermetic: a ``ContainerManager`` built with ``__new__`` + the same three
boundary seams ``tests/test_list_containers_session_scoping.py`` uses (no
Docker daemon).
"""

import json
from unittest.mock import patch

import infra.container_manager as cm_mod
from infra.container_manager import ContainerManager, _ComputedContainerConfig
from thoughtmachine.container_record import LIFECYCLE_PERSISTENT
from tools.container_control import ContainerStatusTool

_BOTH = (
    "needs network:write; session has network:none; "
    "needs filesystem:write; session has filesystem:read"
)
_NETWORK_ONLY = "needs network:write; session has network:none"


# ------------------------------------------------------------------ fakes


class _FakeContainers:
    def __init__(self, container):
        self._container = container

    def get(self, container_id):
        return self._container


class _FakeClient:
    def __init__(self, container):
        self.containers = _FakeContainers(container)


class _FakeContainer:
    def __init__(self):
        self.id = "p1"
        self.name = "tm-persistent"
        self.labels = {}
        self.status = "running"
        self.lifecycle_class = LIFECYCLE_PERSISTENT


# ------------------------------------------------------------------ builders


def _make_manager(container=None):
    manager = ContainerManager.__new__(ContainerManager)
    manager.client = _FakeClient(container or _FakeContainer())
    manager.workspace_id = "ws-1"
    manager.workspace_path = "/tmp/tm-wave10a-ws"
    manager.session_id = "sessA"
    manager.session_permissions = {"container": True}
    return manager


def _patch_boundaries(monkeypatch, manager, *, live, wanted):
    """Install the three hermetic seams (mirrors the listing-scoping tests)."""
    monkeypatch.setattr(cm_mod, "_exec_live_isolation", lambda c: live)
    monkeypatch.setattr(
        cm_mod,
        "_container_lifecycle_class",
        lambda c: getattr(c, "lifecycle_class", LIFECYCLE_PERSISTENT),
    )
    manager._compute_config = lambda *a, **k: _ComputedContainerConfig(
        wanted[0], wanted[1]
    )


# ------------------------------------------------------------------ manager


def test_two_unmet_requirements_are_joined(monkeypatch):
    container = _FakeContainer()
    manager = _make_manager(container)
    _patch_boundaries(monkeypatch, manager, live=("bridge", "rw"), wanted=("none", "ro"))

    assert manager.unavailable_reason(container) == _BOTH


def test_within_bounds_is_none(monkeypatch):
    container = _FakeContainer()
    manager = _make_manager(container)
    _patch_boundaries(monkeypatch, manager, live=("none", "ro"), wanted=("none", "ro"))

    assert manager.unavailable_reason(container) is None


def test_recomputed_each_call(monkeypatch):
    container = _FakeContainer()
    manager = _make_manager(container)
    _patch_boundaries(monkeypatch, manager, live=("none", "ro"), wanted=("none", "ro"))

    assert manager.unavailable_reason(container) is None

    # Flip the live envelope between two consecutive calls: a fresh value must
    # appear (proves the value is recomputed, never cached).
    monkeypatch.setattr(cm_mod, "_exec_live_isolation", lambda c: ("bridge", "ro"))
    assert manager.unavailable_reason(container) == _NETWORK_ONLY


# ------------------------------------------------------------------ status tool


def test_describe_container_surfaces_unavailable_reason(monkeypatch):
    container = _FakeContainer()
    manager = _make_manager(container)
    _patch_boundaries(monkeypatch, manager, live=("bridge", "rw"), wanted=("none", "ro"))

    tool = ContainerStatusTool(container_id="p1")
    described = tool._describe_container(manager)

    assert described["lifecycle_class"] == "persistent"
    assert described["unavailable_reason"] == _BOTH


def test_status_tool_missing_container_has_null_unavailable_reason():
    manager = _make_manager(_FakeContainer())
    manager.status = lambda cid: {"status": "missing", "container_id": "p1"}
    tool = ContainerStatusTool(container_id="p1")
    with patch.object(ContainerStatusTool, "_make_manager", return_value=manager):
        result = json.loads(tool.execute())

    assert result["success"] is True
    assert result["status"] == "missing"
    assert result["unavailable_reason"] is None
