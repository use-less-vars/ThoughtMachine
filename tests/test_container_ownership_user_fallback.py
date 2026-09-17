"""RED test: the resource-container create must never passthrough ``user=None``.

Focus (single create site)
--------------------------
``ResourceContainerManager._create_resource_container`` in
``infra/resource_container_manager.py`` -- specifically the ``user=host_user()``
argument of the LEGACY ``self.client.containers.run(...)`` call (the direct
create used when the registry facade is inactive).

Why the fallback is needed
--------------------------
``agent.config.defaults.host_user()`` returns ``None`` on Windows (no
``uid:gid`` concept), and docker-py then OMITS ``--user`` entirely.  On Docker
Desktop for Windows a bind-mounted workspace is presented to the container as
``root:root``; a container left at its default image user therefore cannot
write the mount and git fails with "detected dubious ownership".  The intended
fix passes an explicit root fallback (``host_user() or "0:0"``) so the
container user matches the mount owner Docker Desktop presents.

Red / green contract
--------------------
* CASE A (``host_user() -> None``): must create the container with
  ``user == "0:0"``.  This is RED today (the create receives ``None``) and
  GREEN after the one-line fallback lands.
* CASE B (``host_user() -> "1000:1000"``): must still pass the real host
  ``uid:gid`` verbatim.  GREEN today; guards against over-correction that
  would break POSIX ownership (the whole point of matching ``host_user()``).

Test-only: this is the ONE new test file; no production code is modified.
The create is driven against fakes (no docker daemon) following the patterns
in ``tests/test_container_user_and_git_ownership.py``.
"""

import contextlib

import pytest

import infra.resource_container_manager as rc_mgr
from infra.resource_container_manager import ResourceContainerManager

import security.admission_gate as admission_gate


# ── shared fakes (mirror tests/test_container_user_and_git_ownership.py) ──


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


class _FakeContainers:
    def __init__(self, run_result=None):
        self.run_calls = []
        self._run_result = run_result

    def list(self, all=False, filters=None):
        return []

    def get(self, name):
        return None

    def run(self, *args, **kwargs):
        self.run_calls.append({"args": args, "kwargs": kwargs})
        return self._run_result


class _FakeClient:
    def __init__(self, run_result=None):
        self.containers = _FakeContainers(run_result=run_result)

    def ping(self):
        return True


class _FakeRecordHandle:
    """Stand-in for ``_RecordHandle``: ``attach`` is a no-op."""

    def attach(self, container, *args, **kwargs):
        return None


@contextlib.contextmanager
def _dummy_record_creation(*args, **kwargs):
    """Hermetic replacement for ``record_creation`` (no vault/disk write)."""
    yield _FakeRecordHandle()


def _make_resource_manager(client, tmp_path):
    """Build a ResourceContainerManager without ``__init__`` (no daemon)."""
    mgr = ResourceContainerManager.__new__(ResourceContainerManager)
    mgr.workspace_id = "ws-fallback-test"
    mgr.workspace_path = str(tmp_path)
    mgr.network_mode = "none"
    mgr.image = "tm-resource-git"
    mgr.vault_root = None
    mgr.session_config = None
    mgr.session_id = "s-fallback-test"
    mgr.session_permissions = {}
    mgr.mem_limit = "512m"
    mgr.cpu_quota = 50000
    mgr.client = client
    return mgr


def _create_run_kwargs(monkeypatch, tmp_path, host_user_value):
    """Drive the legacy resource-container create; return the run kwargs.

    Only the seams that would otherwise need live infra are stubbed, so the
    ``client.containers.run`` call (and its ``user=`` argument) is the real
    production object under test.
    """
    client = _FakeClient(run_result=_FakeCtr())
    mgr = _make_resource_manager(client, tmp_path)

    # Gate 1 - host-id guard: return a non-root host id so the
    # AdmissionDenied("root_host_unsupported") refusal does not fire.
    monkeypatch.setattr(rc_mgr, "_host_ids", lambda: (1000, 1000))
    # Gate 2 - registry facade OFF, so the LEGACY ``containers.run`` path
    # (the one carrying ``user=host_user()``) is exercised.
    monkeypatch.setattr(rc_mgr, "is_registry_active", lambda _cfg: False)
    # The seam under test.
    monkeypatch.setattr(rc_mgr, "host_user", lambda: host_user_value)
    # Hermetic seams: no vault/record/disk side effects.
    monkeypatch.setattr(rc_mgr, "record_creation", _dummy_record_creation)
    monkeypatch.setattr(rc_mgr, "_load_capabilities", lambda *a, **k: None)
    # Admission allow: the resource create must reach ``containers.run``.
    monkeypatch.setattr(admission_gate, "admit", lambda *a, **k: None)

    mgr._create_resource_container()

    assert client.containers.run_calls, "containers.run was never called"
    return client.containers.run_calls[-1]["kwargs"]


def test_windows_none_host_user_falls_back_to_root_owner(monkeypatch, tmp_path):
    """CASE A: ``host_user() -> None`` must become the explicit ``"0:0"`` user.

    RED today: the create passes ``user=None`` (docker-py omits ``--user``),
    which is exactly the Docker-Desktop-on-Windows mount-ownership failure.
    """
    kwargs = _create_run_kwargs(monkeypatch, tmp_path, None)
    assert kwargs["user"] == "0:0"


def test_posix_host_user_passed_through_unchanged(monkeypatch, tmp_path):
    """CASE B: the fallback must not alter the real POSIX host ``uid:gid``."""
    kwargs = _create_run_kwargs(monkeypatch, tmp_path, "1000:1000")
    assert kwargs["user"] == "1000:1000"


def _registry_create_run_kwargs(monkeypatch, host_user_value):
    """Drive the ``container_registry`` create call; return the run kwargs.

    ``infra.container_registry.create_hardened_container`` is that module's
    single ``client.containers.run(...)`` create path -- the one carrying
    ``user=host_user()``.  Called with ``workspace_id=None`` it stays a *pure*
    create (no admission / daemon probes), so only the host-id guard and the
    ``host_user`` seam need stubbing.  Reuses the SAME fakes
    (``_FakeClient`` / ``_FakeCtr``) as the resource-manager cases above.
    """
    import infra.container_registry as container_registry

    client = _FakeClient(run_result=_FakeCtr())
    # Gate 1 - host-id guard: non-root host id so the
    # AdmissionDenied("root_host_unsupported") refusal does not fire.
    monkeypatch.setattr(container_registry, "_host_ids", lambda: (1000, 1000))
    # The seam under test.
    monkeypatch.setattr(container_registry, "host_user", lambda: host_user_value)

    profile = container_registry.ContainerProfile()
    container_registry.create_hardened_container(client, profile, "tm-res-c")

    assert client.containers.run_calls, "containers.run was never called"
    return client.containers.run_calls[-1]["kwargs"]


def test_registry_none_host_user_falls_back_to_root_owner(monkeypatch):
    """CASE C: the registry create applies the same ``"0:0"`` fallback.

    ``infra.container_registry.create_hardened_container`` carries its OWN
    ``user=host_user()``; with ``host_user() -> None`` it must also create
    with ``user == "0:0"`` so the Windows bind-mount ownership fix is not
    limited to the legacy resource-manager path.
    """
    kwargs = _registry_create_run_kwargs(monkeypatch, None)
    assert kwargs["user"] == "0:0"


# ── Site 2: ContainerManager._run_container (legacy direct create) ─────────


def _container_manager_run_kwargs(monkeypatch, host_user_value):
    """Drive ``ContainerManager._run_container``; return the run kwargs.

    ``_run_container`` is ContainerManager's single ``client.containers.run``
    create site.  The manager is built without ``__init__`` (no daemon); only
    the ``host_user`` seam is stubbed, so the ``user=`` argument under test is
    the real production object.
    """
    import infra.container_manager as container_manager
    from infra.container_manager import ContainerManager
    from thoughtmachine.container_record import LIFECYCLE_PERSISTENT

    client = _FakeClient(run_result=_FakeCtr())
    mgr = ContainerManager.__new__(ContainerManager)
    mgr.client = client
    mgr.mem_limit = "512m"
    mgr.cpu_quota = 50000
    mgr.session_id = "s-fallback-test"
    mgr.workspace_id = "ws-fallback-test"

    # The seam under test.
    monkeypatch.setattr(container_manager, "host_user", lambda: host_user_value)

    mgr._run_container(
        image="tm-resource-git",
        name="tm-res-c",
        labels={"thoughtmachine.workspace_id": "ws-fallback-test"},
        mounts=[],
        tmpfs={"/tmp": "rw,size=8m"},
        network_mode="none",
        lifecycle_class=LIFECYCLE_PERSISTENT,
    )

    assert client.containers.run_calls, "containers.run was never called"
    return client.containers.run_calls[-1]["kwargs"]


def test_container_manager_none_host_user_falls_back_to_root_owner(monkeypatch):
    """CASE D: ``ContainerManager._run_container`` applies the ``"0:0"`` fallback.

    ``infra/container_manager.py`` carries its OWN ``user=host_user()``; with
    ``host_user() -> None`` it must create with ``user == "0:0"`` so the Windows
    bind-mount ownership fix is not limited to the other two create sites.
    """
    kwargs = _container_manager_run_kwargs(monkeypatch, None)
    assert kwargs["user"] == "0:0"


def test_container_manager_posix_host_user_passed_through_unchanged(monkeypatch):
    """CASE E: the fallback must not alter the real POSIX host ``uid:gid``."""
    kwargs = _container_manager_run_kwargs(monkeypatch, "1000:1000")
    assert kwargs["user"] == "1000:1000"


# ── Site 3: DockerExecutor._ensure_container (the executor create) ────────


def _executor_run_kwargs(monkeypatch, host_user_value):
    """Drive ``DockerExecutor._ensure_container``; return the run kwargs.

    Mirrors the hermetic harness in
    ``tests/integration/test_admission_wiring.py`` (site 1): the image probe,
    container-config resolution, admission gate and host-id probe are stubbed,
    so the single ``client.containers.run`` call -- and its ``user=`` argument
    -- is the production object under test.  No docker daemon is used.
    """
    import hashlib
    from unittest.mock import MagicMock

    import docker_executor
    from docker_executor import DockerExecutor
    import security.admission_gate as admission_gate
    from security.admission_gate import Allow

    try:
        from docker.errors import NotFound
    except Exception:  # pragma: no cover - docker SDK absent
        NotFound = Exception

    workspace_path = "/tmp/tm-fallback-ws"
    container_name = "agent-exec-" + hashlib.sha256(
        workspace_path.encode()).hexdigest()[:12]

    client = MagicMock()
    client.containers.list.return_value = []
    client.containers.get.side_effect = NotFound(container_name)
    client.containers.run.return_value = _FakeCtr(name=container_name)
    client.volumes.get_or_create.return_value = MagicMock()

    ex = DockerExecutor.__new__(DockerExecutor)
    ex.workspace_path = workspace_path
    ex.image = "agent-executor-test"
    ex.network = "none"
    ex.mem_limit = "128m"
    ex.cpu_quota = 50000
    ex.force_rebuild = False
    ex.idle_timeout = 600
    ex.session_permissions = {"container": True}
    ex.workspace_id = "ws-fallback-test"
    ex.client = client
    ex.container = None
    ex.last_used = 0.0
    ex._timeout_warning_printed = False

    monkeypatch.setattr(DockerExecutor, "_ensure_image", lambda self, *a, **k: None)
    monkeypatch.setattr(
        DockerExecutor, "_compute_container_config", lambda self: ("none", "ro")
    )
    # Pass the root-host guard (non-root host id).
    monkeypatch.setattr(docker_executor, "_host_ids", lambda: (1000, 1000))
    # The seam under test.
    monkeypatch.setattr(docker_executor, "host_user", lambda: host_user_value)
    # Admission allow: the create must reach ``containers.run``.
    monkeypatch.setattr(
        admission_gate, "admit", lambda request, **k: Allow(spec=request.spec)
    )

    ex._ensure_container()

    assert client.containers.run.called, "containers.run was never called"
    return client.containers.run.call_args.kwargs


def test_executor_none_host_user_falls_back_to_root_owner(monkeypatch):
    """CASE F: ``DockerExecutor._ensure_container`` applies the ``"0:0"`` fallback."""
    kwargs = _executor_run_kwargs(monkeypatch, None)
    assert kwargs["user"] == "0:0"


def test_executor_posix_host_user_passed_through_unchanged(monkeypatch):
    """CASE G: the fallback must not alter the real POSIX host ``uid:gid``."""
    kwargs = _executor_run_kwargs(monkeypatch, "1000:1000")
    assert kwargs["user"] == "1000:1000"
