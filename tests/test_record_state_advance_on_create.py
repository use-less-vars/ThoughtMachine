"""RED tests: a container's record must advance to ``running`` on create.

Defect (``bug/record-state-advance-on-create``): every fresh-create site mints
a record in the pre-run ``creating`` state (``api.begin_record``) and then
attaches the freshly created container WITHOUT advancing the record's state.
The record therefore stays ``creating`` after the container is actually
running, so the workspace container-view renders a live container as
``stopped``.

The fix advances the record to ``running`` in the same ``attach`` call at each
of the five fresh-create sites:

    1. infra/container_manager.py        ContainerManager._fresh_start
    2. infra/container_registry.py       ContainerRegistry.request_container
    3. infra/container_registry.py       ContainerRegistry.create_resource_container
    4. infra/container_create.py         create_hardened_container
    5. infra/resource_container_manager.py ResourceContainerManager._create_resource_container

Each site is driven at its real entry point with the module-local
``record_creation`` context manager monkeypatched to a SPY that yields a fake
record object.  The spy's ``attach`` records the keyword arguments it receives,
so we can assert ``state == "running"`` was passed.  This avoids needing a real
record store or a Docker daemon.

RED mechanism: before the fix every ``attach`` is called with NO ``state``
kwarg (so ``.get("state")`` is ``None``); the ``STATE_RUNNING`` constant does
not exist; and the container-view mapper still collapses ``creating`` ->
``stopped``.  All tests here therefore FAIL before the fix.
"""

from __future__ import annotations

import contextlib

import pytest

import security.admission_gate as admission_gate

import infra.container_manager as container_manager
from infra.container_manager import ContainerManager

import infra.container_registry as container_registry
from infra.container_registry import ContainerRegistry

import infra.container_create as container_create
from infra.container_create import ContainerCreateSpec

import infra.resource_container_manager as resource_container_manager
from infra.resource_container_manager import ResourceContainerManager

from thoughtmachine.container_record import LIFECYCLE_PERSISTENT


# ── fakes ───────────────────────────────────────────────────────────────────


class _FakeCtr:
    """Minimal docker container stand-in."""

    def __init__(self, id="ctr-1", name="c1", labels=None, attrs=None,
                 status="running"):
        self.id = id
        self.name = name
        self.labels = labels or {}
        self.attrs = attrs or {}
        self.status = status

    def reload(self):
        return None

    def stop(self, timeout=None):
        return None

    def remove(self, force=False):
        return None

    def kill(self):
        return None

    def exec_run(self, *args, **kwargs):
        return (0, (b"", b""))


class _FakeContainers:
    def __init__(self, run_result=None, get_result=None, get_raises=False):
        self.run_calls = []
        self._run_result = run_result
        self._get_result = get_result
        self._get_raises = get_raises

    def list(self, all=False, filters=None):
        return []

    def get(self, name):
        if self._get_raises:
            raise RuntimeError(name)
        return self._get_result

    def run(self, *args, **kwargs):
        self.run_calls.append({"args": args, "kwargs": kwargs})
        return self._run_result


class _FakeVolumes:
    def get_or_create(self, name):
        return None

    def create(self, name):
        return None


class _FakeClient:
    def __init__(self, run_result=None, get_result=None, get_raises=False):
        self.containers = _FakeContainers(
            run_result=run_result, get_result=get_result, get_raises=get_raises
        )
        self.volumes = _FakeVolumes()

    def ping(self):
        return True


class _SpyRecord:
    """Fake record whose ``attach`` RECORDS the kwargs it is handed."""

    def __init__(self):
        self.id = "rec-spy"
        self.attach_kwargs = []
        self.attached_containers = []

    def attach(self, container, **kwargs):
        self.attach_kwargs.append(kwargs)
        self.attached_containers.append(container)
        return None


def _spy_record_creation(spy):
    """Context-manager factory yielding *spy* (replaces module ``record_creation``)."""

    @contextlib.contextmanager
    def _cm(*args, **kwargs):
        yield spy

    return _cm


def _assert_attached_running(spy):
    assert spy.attach_kwargs, "record.attach was never called"
    last = spy.attach_kwargs[-1]
    assert last.get("state") == "running", (
        f"attach() must advance the record to 'running'; got kwargs={last!r}"
    )
    # And it must be the shared STATE_RUNNING constant.
    from thoughtmachine.container_record import STATE_RUNNING

    assert last.get("state") == STATE_RUNNING


def _make_container_manager(client, tmp_path):
    cm = ContainerManager.__new__(ContainerManager)
    cm.workspace_path = str(tmp_path)
    cm.image = "agent-executor"
    cm.network = "none"
    cm.mem_limit = "1g"
    cm.cpu_quota = 100000
    cm.session_id = "s1"
    cm.workspace_id = "ws-1"
    cm.session_permissions = {}
    cm._session_config = {}
    cm._containers = {}
    cm.client = client
    cm._compute_config = lambda: ("none", "ro")
    cm.vault_root = str(tmp_path)
    cm.container_notes = {}
    cm.max_containers = 10
    cm.workspace_config = {}
    cm.dockerfile_path = None
    return cm


# ── the exported constant ───────────────────────────────────────────────────


def test_state_running_constant_exported():
    """``container_record`` must expose ``STATE_RUNNING == "running"``."""
    import thoughtmachine.container_record as container_record

    assert getattr(container_record, "STATE_RUNNING", None) == "running"


def test_container_view_maps_creating_record_to_creating():
    """A record still in ``creating`` must render as ``creating`` (not stopped)."""
    import web_ui.backend.server as server_module

    assert server_module._map_container_view_state(None, "creating") == "creating"


# ── site 1: ContainerManager._fresh_start ───────────────────────────────────


def test_fresh_start_attaches_state_running(monkeypatch, tmp_path):
    spy = _SpyRecord()
    monkeypatch.setattr(container_manager, "_host_ids", lambda: (1000, 1000))
    monkeypatch.setattr(container_manager, "admit", lambda *a, **k: None)
    monkeypatch.setattr(container_manager, "ClientProbes", lambda *a, **k: None)
    monkeypatch.setattr(container_manager, "_load_capabilities", lambda *a, **k: None)
    monkeypatch.setattr(
        container_manager, "record_creation", _spy_record_creation(spy)
    )

    cm = _make_container_manager(_FakeClient(run_result=_FakeCtr()), tmp_path)
    monkeypatch.setattr(cm, "_run_container", lambda **kw: _FakeCtr())
    monkeypatch.setattr(cm, "_cache_container", lambda name, c: c)

    cm._fresh_start(
        image="img", name="c1", note=None, worker_name=None,
        lifecycle_class=LIFECYCLE_PERSISTENT,
        network_mode="none", workspace_mode="ro",
    )

    _assert_attached_running(spy)


# ── site 2: ContainerRegistry.request_container ─────────────────────────────


def test_registry_request_container_attaches_state_running(monkeypatch):
    spy = _SpyRecord()
    monkeypatch.setattr(
        container_registry, "record_creation", _spy_record_creation(spy)
    )
    monkeypatch.setattr(
        container_registry, "_resolve_network_mode_via_gate",
        lambda *a, **k: "none",
    )
    monkeypatch.setattr(
        container_registry, "create_hardened_container",
        lambda *a, **k: _FakeCtr(id="cid", name="c"),
    )

    reg = ContainerRegistry(docker_client=_FakeClient(run_result=_FakeCtr()))
    reg.request_container("w1", "s1", {}, workspace_id="ws-1")

    _assert_attached_running(spy)


# ── site 3: ContainerRegistry.create_resource_container ─────────────────────


def test_registry_create_resource_container_attaches_state_running(monkeypatch):
    spy = _SpyRecord()
    monkeypatch.setattr(
        container_registry, "record_creation", _spy_record_creation(spy)
    )
    monkeypatch.setattr(
        container_registry, "create_hardened_container",
        lambda *a, **k: _FakeCtr(id="cid", name="c"),
    )

    reg = ContainerRegistry(docker_client=_FakeClient(run_result=_FakeCtr()))
    monkeypatch.setattr(reg, "_ensure_resource_image_or_raise", lambda: None)

    reg.create_resource_container("s1", "ws-1", "none", workspace_path="/tmp/ws")

    _assert_attached_running(spy)


# ── site 4: infra.container_create.create_hardened_container ────────────────


def test_create_hardened_container_attaches_state_running(monkeypatch):
    spy = _SpyRecord()
    monkeypatch.setattr(
        container_create, "record_creation", _spy_record_creation(spy)
    )

    spec = ContainerCreateSpec(
        image="img",
        command=("sleep", "inf"),
        name="c-new",
        container_type="user",
        lifecycle_class=LIFECYCLE_PERSISTENT,
        workspace_id="ws-1",
    )
    container_create.create_hardened_container(
        _FakeClient(run_result=_FakeCtr()), spec
    )

    _assert_attached_running(spy)


# ── site 5: ResourceContainerManager._create_resource_container ─────────────


def test_resource_container_manager_attaches_state_running(monkeypatch):
    spy = _SpyRecord()
    monkeypatch.setattr(
        resource_container_manager, "record_creation", _spy_record_creation(spy)
    )
    monkeypatch.setattr(
        resource_container_manager, "_host_ids", lambda: (1000, 1000)
    )
    monkeypatch.setattr(
        resource_container_manager, "_resolve_worktree_main_repo",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        resource_container_manager, "_load_capabilities", lambda *a, **k: None
    )
    monkeypatch.setattr(admission_gate, "admit", lambda *a, **k: None)
    monkeypatch.setattr(admission_gate, "ClientProbes", lambda *a, **k: None)

    rm = ResourceContainerManager.__new__(ResourceContainerManager)
    rm.workspace_path = "/tmp/ws"
    rm.vault_root = "/tmp/vault"
    rm.network_mode = "none"
    rm.workspace_id = "ws-1"
    rm.session_id = "s1"
    rm.image = "res-img"
    rm.mem_limit = "1g"
    rm.cpu_quota = 100000
    rm.session_config = {}
    rm.client = _FakeClient(run_result=_FakeCtr())

    rm._create_resource_container()

    _assert_attached_running(spy)
