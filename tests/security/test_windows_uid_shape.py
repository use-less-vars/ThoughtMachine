"""Windows-shaped host uid/gid handling for the four container-create stacks.

Every host ``uid``/``gid`` read in the production code must route through the
single ``agent.config.defaults._host_ids`` source of truth.  Windows has NO
host uid:gid concept (``os.getuid``/``os.getgid`` do not exist there), so on
Windows the host-root guards must NOT fire and the tmpfs/chown artifacts must
omit ``uid=``/``gid=``.

These tests simulate a Windows-shaped ``os`` module (``os.name == "nt"`` and
``os.getuid``/``os.getgid`` absent) by monkeypatching the ``os`` name inside
each production module, then exercise the four guarded create stacks at their
entry points.  Before the routing fix the un-guarded code called
``os.getuid`` directly and raised ``AttributeError`` on the simulated Windows
host; the fix calls the patchable ``_host_ids()`` seam instead.

A companion test pins the POSIX behaviour: patching the seam to return
``(0, 0)`` must still refuse (the host-root guard is wired, not deleted) and
must still emit the ``chown`` in the exec path.

Honesty note: this is verified on Linux with a *simulated* Windows-shaped
``os``, not on a real Windows host.
"""

import os
import time
from contextlib import contextmanager

import pytest

import docker

import security.admission_gate as admission_gate
from security.admission_gate import AdmissionDenied

import docker_executor
from docker_executor import DockerExecutor

import infra.container_manager as container_manager
from infra.container_manager import ContainerManager

import infra.resource_container_manager as resource_container_manager
from infra.resource_container_manager import ResourceContainerManager

import infra.container_registry as container_registry
from infra.container_registry import ContainerProfile, create_hardened_container

from thoughtmachine.container_record import LIFECYCLE_PERSISTENT


class _WindowsOSView:
    """A faithful Windows-shaped view of ``os``.

    ``os.name`` reads ``"nt"`` and ``os.getuid``/``os.getgid`` raise
    ``AttributeError`` (they do not exist on Windows).  Every other attribute
    delegates to the real ``os`` module so path handling keeps working.

    This is the RED mechanism: un-hardened code that calls ``os.getuid``
    directly blows up; code that routes through the patchable ``_host_ids``
    seam is unaffected.
    """

    name = "nt"

    def __getattr__(self, attr):
        if attr in ("getuid", "getgid"):
            raise AttributeError(f"module 'os' has no attribute {attr!r}")
        return getattr(os, attr)


# ── shared fakes (mirror tests/test_container_record_hook.py) ──────────────


class _FakeCtr:
    """Minimal docker container stand-in that records exec_run calls."""

    def __init__(self, id="ctr-1", name="c1", labels=None, attrs=None,
                 status="running"):
        self.id = id
        self.name = name
        self.labels = labels or {}
        self.attrs = attrs or {}
        self.status = status
        self.exec_calls = []

    def reload(self):
        return None

    def stop(self, timeout=None):
        return None

    def remove(self, force=False):
        return None

    def kill(self):
        return None

    def exec_run(self, *args, **kwargs):
        self.exec_calls.append({"args": args, "kwargs": kwargs})
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
            raise docker.errors.NotFound(name)
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


class _FakeRecord:
    def __init__(self):
        self.id = "rec-win"
        self.attached = None

    def attach(self, container, **kwargs):
        self.attached = container


@contextmanager
def _fake_record_creation(*args, **kwargs):
    yield _FakeRecord()


def _exec_cmds(ctr):
    """Every shell command handed to ``exec_run`` on ``ctr`` (positional or kw)."""
    cmds = []
    for call in ctr.exec_calls:
        if call["args"]:
            cmds.append(call["args"][0])
        elif "cmd" in call["kwargs"]:
            cmds.append(call["kwargs"]["cmd"])
    return cmds


def _make_container_manager(client, workspace_id="ws-1", session_id="s1",
                            tmp_path=None):
    cm = ContainerManager.__new__(ContainerManager)
    cm.workspace_path = str(tmp_path) if tmp_path else "/tmp/ws"
    cm.image = "agent-executor"
    cm.network = "none"
    cm.mem_limit = "1g"
    cm.cpu_quota = 100000
    cm.session_id = session_id
    cm.workspace_id = workspace_id
    cm.session_permissions = {}
    cm._session_config = {}
    cm._containers = {}
    cm.client = client
    cm._compute_config = lambda: ("none", "ro")
    cm.vault_root = str(tmp_path) if tmp_path else "/tmp/ws"
    cm.container_notes = {}
    cm.max_containers = 10
    cm.workspace_config = {}
    cm.dockerfile_path = None
    return cm


# ═══════════════════════════════════════════════════════════════════════════
# Windows-shaped host: no AttributeError, uid/gid omitted.
# ═══════════════════════════════════════════════════════════════════════════


def test_registry_create_no_uid_on_windows(monkeypatch):
    """create_hardened_container must not touch os.getuid on Windows."""
    monkeypatch.setattr(container_registry, "os", _WindowsOSView())
    monkeypatch.setattr(container_registry, "_host_ids", lambda: None)

    client = _FakeClient(run_result=_FakeCtr())
    create_hardened_container(client, ContainerProfile(), "c-win")

    assert client.containers.run_calls, "containers.run was never called"


def test_fresh_start_tmpfs_omits_uid_on_windows(monkeypatch, tmp_path):
    """_fresh_start must build a uid/gid-free /home/agent tmpfs on Windows."""
    monkeypatch.setattr(container_manager, "os", _WindowsOSView())
    monkeypatch.setattr(container_manager, "_host_ids", lambda: None)
    monkeypatch.setattr(container_manager, "admit", lambda *a, **k: None)
    monkeypatch.setattr(container_manager, "ClientProbes", lambda *a, **k: None)
    monkeypatch.setattr(container_manager, "_load_capabilities", lambda *a, **k: None)
    monkeypatch.setattr(container_manager, "record_creation", _fake_record_creation)

    client = _FakeClient(run_result=_FakeCtr())
    cm = _make_container_manager(client, tmp_path=tmp_path)
    captured = {}

    def _spy(**kwargs):
        captured.update(kwargs)
        return _FakeCtr()

    monkeypatch.setattr(cm, "_run_container", _spy)
    monkeypatch.setattr(cm, "_cache_container", lambda name, c: c)

    cm._fresh_start(
        image="img", name="c1", note=None, worker_name=None,
        lifecycle_class=LIFECYCLE_PERSISTENT,
        network_mode="none", workspace_mode="ro",
    )

    assert captured.get("tmpfs"), "tmpfs was never passed to _run_container"
    assert captured["tmpfs"]["/home/agent"] == "rw,exec,size=256M"


def test_container_manager_exec_omits_chown_on_windows(monkeypatch):
    """ContainerManager.exec must not chown the workdir on Windows."""
    monkeypatch.setattr(container_manager, "os", _WindowsOSView())
    monkeypatch.setattr(container_manager, "_host_ids", lambda: None)

    ctr = _FakeCtr()
    client = _FakeClient(get_result=ctr)
    cm = _make_container_manager(client)
    monkeypatch.setattr(cm, "class_of", lambda c: "user")
    monkeypatch.setattr(cm, "_agent_access_denial", lambda cls: None)
    monkeypatch.setattr(cm, "_check_exec_drift", lambda c, cid: ("ok", None))
    monkeypatch.setattr(cm, "_exceeds_disk_quota", lambda c, mb: False)
    monkeypatch.setattr(cm, "_append_usage_log", lambda *a, **k: None)

    cm.exec("ctr-1", "echo hi", workdir="/wd")

    cmds = _exec_cmds(ctr)
    assert ["sh", "-c", "mkdir -p /wd"] in cmds, cmds
    assert not any("chown" in str(c) for c in cmds), cmds


def test_docker_executor_ensure_tmpfs_omits_uid_on_windows(monkeypatch):
    """DockerExecutor._ensure_container must omit uid/gid from tmpfs on Windows."""
    monkeypatch.setattr(docker_executor, "os", _WindowsOSView())
    monkeypatch.setattr(docker_executor, "_host_ids", lambda: None)
    monkeypatch.setattr(admission_gate, "admit", lambda *a, **k: None)
    monkeypatch.setattr(admission_gate, "ClientProbes", lambda *a, **k: None)
    monkeypatch.setattr(
        docker_executor, "_load_admission_capabilities",
        lambda *a, **k: None, raising=False,
    )

    ex = DockerExecutor.__new__(DockerExecutor)
    ex.workspace_path = "/tmp/ws"
    ex.image = "agent-executor"
    ex.mem_limit = "1g"
    ex.cpu_quota = 100000
    ex.session_permissions = {}
    ex.workspace_id = "ws-1"
    ex._session_config = None
    ex.container = None
    ex.force_rebuild = False
    ex._ensure_image = lambda: None
    ex._compute_container_config = lambda: ("none", "ro")
    ex.client = _FakeClient(get_raises=True)

    ex._ensure_container()

    runs = ex.client.containers.run_calls
    assert runs, "containers.run was never called"
    assert runs[-1]["kwargs"]["tmpfs"]["/home/agent"] == "rw,exec,size=256M"


def test_docker_executor_execute_omits_chown_on_windows(monkeypatch):
    """DockerExecutor.execute must not chown the workdir on Windows."""
    monkeypatch.setattr(docker_executor, "os", _WindowsOSView())
    monkeypatch.setattr(docker_executor, "_host_ids", lambda: None)

    ex = DockerExecutor.__new__(DockerExecutor)
    ex.workspace_path = "/tmp/ws"
    ex.image = "agent-executor"
    ex.mem_limit = "1g"
    ex.cpu_quota = 100000
    ex.session_permissions = {}
    ex.workspace_id = "ws-1"
    ex.session_id = "s1"
    ex.container = _FakeCtr()
    ex.last_used = time.time()
    ex.idle_timeout = 1e9
    monkeypatch.setattr(ex, "_ensure_container", lambda: None)
    monkeypatch.setattr(
        ex, "_exec_with_timeout", lambda **k: (0, (b"out", b"")),
    )

    ex.execute("echo", workdir="/wd")

    cmds = _exec_cmds(ex.container)
    assert ["sh", "-c", "mkdir -p /wd"] in cmds, cmds
    assert not any("chown" in str(c) for c in cmds), cmds


def test_resource_container_tmpfs_omits_uid_on_windows(monkeypatch):
    """_create_resource_container must omit uid/gid from tmpfs on Windows."""
    monkeypatch.setattr(resource_container_manager, "os", _WindowsOSView())
    monkeypatch.setattr(resource_container_manager, "_host_ids", lambda: None)
    monkeypatch.setattr(
        resource_container_manager, "_resolve_worktree_main_repo",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        resource_container_manager, "_load_capabilities", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        resource_container_manager, "record_creation", _fake_record_creation,
    )
    monkeypatch.setattr(admission_gate, "admit", lambda *a, **k: None)
    monkeypatch.setattr(admission_gate, "ClientProbes", lambda *a, **k: None)

    client = _FakeClient(run_result=_FakeCtr())
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
    rm.client = client

    rm._create_resource_container()

    runs = client.containers.run_calls
    assert runs, "containers.run was never called"
    assert runs[-1]["kwargs"]["tmpfs"]["/home/agent"] == "rw,exec,size=256M"


# ═══════════════════════════════════════════════════════════════════════════
# Companion: the POSIX host-root guard is still WIRED (not deleted).
# ═══════════════════════════════════════════════════════════════════════════


def test_root_host_still_refused_and_chown_wired(monkeypatch):
    """With the seam forced to uid 0, every site still refuses; and the exec
    chown path is still emitted (proving routing, not deletion)."""
    monkeypatch.setattr(container_registry, "_host_ids", lambda: (0, 0))
    monkeypatch.setattr(container_manager, "_host_ids", lambda: (0, 0))
    monkeypatch.setattr(resource_container_manager, "_host_ids", lambda: (0, 0))
    monkeypatch.setattr(docker_executor, "_host_ids", lambda: (0, 0))

    # (a) registry create refuses, nothing runs.
    client = _FakeClient(run_result=_FakeCtr())
    with pytest.raises(AdmissionDenied) as exc:
        create_hardened_container(client, ContainerProfile(), "c-root")
    assert exc.value.code == "root_host_unsupported"
    assert client.containers.run_calls == []

    # (b) ContainerManager.start refuses up front.
    client = _FakeClient(run_result=_FakeCtr())
    cm = _make_container_manager(client)
    result = cm.start(image="img", name="c1")
    assert result.get("code") == "root_host_unsupported"
    assert client.containers.run_calls == []

    # (c) ResourceContainerManager refuses with a coded AdmissionDenied.
    rm = ResourceContainerManager.__new__(ResourceContainerManager)
    with pytest.raises(AdmissionDenied) as exc:
        rm._create_resource_container()
    assert exc.value.code == "root_host_unsupported"

    # (d) DockerExecutor._ensure_container refuses before creating.
    monkeypatch.setattr(admission_gate, "admit", lambda *a, **k: None)
    monkeypatch.setattr(admission_gate, "ClientProbes", lambda *a, **k: None)
    monkeypatch.setattr(
        docker_executor, "_load_admission_capabilities",
        lambda *a, **k: None, raising=False,
    )
    ex = DockerExecutor.__new__(DockerExecutor)
    ex.workspace_path = "/tmp/ws"
    ex.image = "agent-executor"
    ex.mem_limit = "1g"
    ex.cpu_quota = 100000
    ex.session_permissions = {}
    ex.workspace_id = "ws-1"
    ex._session_config = None
    ex.container = None
    ex.force_rebuild = False
    ex._ensure_image = lambda: None
    ex._compute_container_config = lambda: ("none", "ro")
    ex.client = _FakeClient(get_raises=True)
    with pytest.raises(RuntimeError, match="root_host_unsupported"):
        ex._ensure_container()
    assert ex.client.containers.run_calls == []

    # (e) ContainerManager.exec still emits the chown for the workdir.
    ctr = _FakeCtr()
    cm2 = _make_container_manager(_FakeClient(get_result=ctr))
    monkeypatch.setattr(cm2, "class_of", lambda c: "user")
    monkeypatch.setattr(cm2, "_agent_access_denial", lambda cls: None)
    monkeypatch.setattr(cm2, "_check_exec_drift", lambda c, cid: ("ok", None))
    monkeypatch.setattr(cm2, "_exceeds_disk_quota", lambda c, mb: False)
    monkeypatch.setattr(cm2, "_append_usage_log", lambda *a, **k: None)
    cm2.exec("ctr-1", "echo hi", workdir="/wd")
    assert ["sh", "-c", "mkdir -p /wd && chown 0:0 /wd"] in _exec_cmds(ctr)

    # (f) DockerExecutor.execute still emits the chown for the workdir.
    ex2 = DockerExecutor.__new__(DockerExecutor)
    ex2.workspace_id = "ws-1"
    ex2.session_id = "s1"
    ex2.container = _FakeCtr()
    ex2.last_used = time.time()
    ex2.idle_timeout = 1e9
    monkeypatch.setattr(ex2, "_ensure_container", lambda: None)
    monkeypatch.setattr(
        ex2, "_exec_with_timeout", lambda **k: (0, (b"out", b"")),
    )
    ex2.execute("echo", workdir="/wd")
    assert ["sh", "-c", "mkdir -p /wd && chown 0:0 /wd"] in _exec_cmds(ex2.container)
