"""Hermetic integration tests: admission wiring at the container-create sites.

Every terminal container-create site must consult the pure admission gate
exactly ONCE, *before* it touches the Docker daemon (``containers.run``), and a
``Deny`` must short-circuit the create.  A single shared event recorder proves
both properties:

* **ordering** — the recorded event stream for a fresh create is exactly
  ``["admit", "run"]`` (admit strictly precedes run), and
* **exactly-once** — there is exactly one ``admit`` event per create.

Sites covered:

1. ``DockerExecutor._ensure_container`` (function-local ``admit`` import).
2. ``ContainerManager.start`` legacy branch (module-level ``admit`` import).
3. ``container_registry.create_hardened_container`` (module-level import).
4. ``ResourceContainerManager._create_resource_container`` legacy branch
   (function-local import, guarded by ``if not self._registry_active``).

Facade variants (sites 2 and 4) prove the *direct* site does NOT admit when the
registry facade owns the create — no double-admit.

No Docker daemon and no real vault are used: every ``docker.from_env`` / client
is a hermetic fake and the vault is a temp dir.
"""

from __future__ import annotations

import hashlib
import types
from unittest.mock import MagicMock, patch

import pytest

import security.admission_gate as admission_gate
from security.admission_gate import AdmissionDenied, Allow, Deny

import infra.container_manager as container_manager
from infra.container_manager import ContainerManager

import infra.container_registry as container_registry
from infra.container_registry import ContainerProfile

import infra.resource_container_manager as rcm
from docker_executor import DockerExecutor

try:
    from docker.errors import NotFound
except Exception:  # pragma: no cover - docker SDK absent
    NotFound = Exception


# ── Shared event recorder ────────────────────────────────────────────────────


class _FakeContainer:
    """Minimal Docker-container stand-in the create sites tolerate."""

    def __init__(self, name=None, container_id="c-fake-1", labels=None):
        self.id = container_id
        self.name = name or container_id
        self.status = "running"
        self.labels = dict(labels or {})
        self.attrs = {
            "Id": container_id,
            "Config": {"Labels": dict(labels or {}), "Image": "agent-executor"},
            "HostConfig": {
                "NetworkMode": "none",
                "Memory": 1073741824,
                "CpuQuota": 100000,
                "OomScoreAdj": 1000,
            },
            "State": {"Status": "running"},
            "Image": "sha256:deadbeef",
        }

    def reload(self):
        pass

    def start(self):
        self.status = "running"

    def stop(self, timeout=None):
        pass

    def remove(self, **kwargs):
        pass


class Recorder:
    """Records admission/run events and stands in for ``admit`` / ``run``.

    ``admit`` appends ``("admit", container_type)`` and returns an ``Allow``
    (or a pre-seeded ``Deny``).  ``run`` appends ``("run", name)`` and returns
    a fake container.
    """

    def __init__(self, deny=None):
        self.events = []
        self._deny = deny

    @property
    def kinds(self):
        return [event[0] for event in self.events]

    def admit(self, request, *, probes=None):  # noqa: D401 - mirrors admit()
        self.events.append(("admit", request.spec.container_type))
        if self._deny is not None:
            return self._deny
        return Allow(spec=request.spec)

    def run(self, *args, **kwargs):
        name = kwargs.get("name")
        if name is None and args:
            name = args[0]
        self.events.append(("run", name))
        return _FakeContainer(name=name, labels=kwargs.get("labels"))


def _patch_admit(monkeypatch, recorder):
    """Patch ``admit`` at every import site the create sites use."""
    monkeypatch.setattr(admission_gate, "admit", recorder.admit)
    monkeypatch.setattr(container_manager, "admit", recorder.admit)
    monkeypatch.setattr(container_registry, "admit", recorder.admit)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Hermetic vault root (record-creation writes land under the temp dir)."""
    root = tmp_path / "vault"
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(root))
    return root


# ── Fake docker client shared by the manager sites ───────────────────────────


class _FakeContainers:
    def __init__(self, recorder):
        self._recorder = recorder

    def list(self, all=False, filters=None):  # noqa: A002 - docker signature
        return []

    def get(self, container_id):
        raise NotFound(container_id)

    def run(self, *args, **kwargs):
        return self._recorder.run(*args, **kwargs)


class _FakeClient:
    def __init__(self, recorder):
        self.containers = _FakeContainers(recorder)


# ── Site 1: DockerExecutor._ensure_container ─────────────────────────────────


def _executor_container_name(workspace_path="/tmp/tm-adm-ws"):
    digest = hashlib.sha256(workspace_path.encode()).hexdigest()[:12]
    return f"agent-exec-{digest}"


def _make_executor(client, workspace_id="ws-1"):
    with patch("docker_executor.docker.from_env", return_value=client):
        return DockerExecutor(
            workspace_path="/tmp/tm-adm-ws",
            image="agent-executor-test",
            network="none",
            mem_limit="128m",
            cpu_quota=50000,
            force_rebuild=False,
            idle_timeout=600,
            session_permissions={"container": True},
            workspace_id=workspace_id,
        )


def _executor_client(recorder):
    client = MagicMock()
    client.containers.get.side_effect = NotFound(_executor_container_name())
    client.containers.run.side_effect = recorder.run
    client.volumes.get_or_create.return_value = MagicMock()
    return client


@pytest.fixture
def _force_executor_create(monkeypatch):
    monkeypatch.setattr(DockerExecutor, "_ensure_image", lambda self, *a, **k: None)
    monkeypatch.setattr(
        DockerExecutor, "_compute_container_config", lambda self: ("none", "ro")
    )


def test_site1_executor_admits_once_before_run(monkeypatch, _force_executor_create):
    recorder = Recorder()
    _patch_admit(monkeypatch, recorder)
    client = _executor_client(recorder)

    _make_executor(client)._ensure_container()

    assert recorder.kinds == ["admit", "run"]
    assert recorder.events[0] == ("admit", "user")


def test_site1_executor_deny_short_circuits_before_run(
    monkeypatch, _force_executor_create
):
    recorder = Recorder(deny=Deny("policy_denied", "nope"))
    _patch_admit(monkeypatch, recorder)
    client = _executor_client(recorder)

    with pytest.raises(AdmissionDenied):
        _make_executor(client)._ensure_container()

    assert recorder.kinds == ["admit"]
    assert client.containers.run.called is False


# ── Site 2: ContainerManager.start (legacy + facade) ─────────────────────────


def _make_container_manager(client, vault, ws="ws-cm", session_id="sess-cm"):
    cm = ContainerManager.__new__(ContainerManager)
    cm.workspace_path = "/tmp/tm-adm-cm"
    cm.session_id = session_id
    cm.workspace_id = ws
    cm.session_permissions = {"container": True}
    cm._session_config = {"use_container_registry": False}
    cm.image = "agent-executor"
    cm.mem_limit = "1g"
    cm.cpu_quota = 100000
    cm._containers = {}
    cm.client = client
    cm._compute_config = lambda *a, **k: ("none", "ro")
    cm.vault_root = str(vault)
    cm.container_notes = {}
    cm.max_containers = 4
    cm.workspace_config = {"max_containers": 4}
    cm.dockerfile_path = None
    return cm


class _FakeRegistryFacade:
    """Records ``request_container`` (registry-active ContainerManager path)."""

    def __init__(self):
        self.requested = []

    def request_container(self, *args, **kwargs):
        self.requested.append((args, kwargs))
        return {
            "id": "reg-cm-1",
            "name": kwargs.get("name") or "agent-exec-reg",
            "status": "running",
            "note": "",
        }


def test_site2_legacy_start_admits_once_before_run(monkeypatch, vault):
    recorder = Recorder()
    _patch_admit(monkeypatch, recorder)
    monkeypatch.setattr(container_manager, "is_registry_active", lambda cfg: False)
    client = _FakeClient(recorder)
    cm = _make_container_manager(client, vault)

    result = cm.start(image="agent-executor", name="agent-exec-adm")

    assert result["status"] == "created"
    assert recorder.kinds == ["admit", "run"]
    assert recorder.events[0] == ("admit", "user")


def test_site2_facade_skips_direct_admit(monkeypatch, vault):
    recorder = Recorder()
    _patch_admit(monkeypatch, recorder)
    monkeypatch.setattr(container_manager, "is_registry_active", lambda cfg: True)
    facade = _FakeRegistryFacade()
    monkeypatch.setattr(
        container_manager, "get_active_registry", lambda cfg: facade
    )
    client = _FakeClient(recorder)
    cm = _make_container_manager(client, vault)

    result = cm.start(image="agent-executor", name="agent-exec-adm")

    assert result["status"] == "created"
    assert facade.requested, "create must be delegated to the registry facade"
    # No direct admit AND no direct run at site 2 (the registry owns both).
    assert recorder.kinds == []


# ── Site 3: container_registry.create_hardened_container ─────────────────────


def test_site3_registry_create_admits_once_before_run(monkeypatch):
    recorder = Recorder()
    _patch_admit(monkeypatch, recorder)
    client = _FakeClient(recorder)
    profile = ContainerProfile(container_type="resource", mounts=[])

    container_registry.create_hardened_container(
        client,
        profile,
        "tm-adm-reg",
        workspace_id="ws-1",
        capabilities=object(),
        permissions={},
    )

    assert recorder.kinds == ["admit", "run"]
    assert recorder.events[0] == ("admit", "resource")


# ── Site 4: ResourceContainerManager._create_resource_container ──────────────


def _make_resource_manager(monkeypatch, client, vault, session_config=None):
    fake_docker = types.SimpleNamespace(from_env=lambda *a, **k: client)
    monkeypatch.setattr(rcm, "docker", fake_docker)
    monkeypatch.setattr(rcm, "_resolve_worktree_main_repo", lambda wp, vr=None: None)
    return rcm.ResourceContainerManager(
        workspace_id="ws-rcm",
        workspace_path="/tmp/tm-adm-res",
        network_mode="none",
        vault_root=str(vault),
        session_config=session_config,
        session_id="sess-rcm",
        session_permissions={"git": True},
    )


class _FakeResourceRegistry:
    """Records ``create_resource_container`` (registry-active resource path)."""

    def __init__(self):
        self.created = []

    def create_resource_container(self, **kwargs):
        self.created.append(kwargs)
        return {
            "id": "reg-res-1",
            "name": kwargs.get("name") or "tm-res-reg-git",
            "status": "running",
        }


def test_site4_legacy_create_admits_once_before_run(monkeypatch, vault):
    recorder = Recorder()
    _patch_admit(monkeypatch, recorder)
    monkeypatch.setattr(rcm, "is_registry_active", lambda cfg: False)
    client = _FakeClient(recorder)
    mgr = _make_resource_manager(monkeypatch, client, vault)

    mgr._create_resource_container(name="tm-res-adm-git")

    assert recorder.kinds == ["admit", "run"]
    assert recorder.events[0] == ("admit", "resource")


def test_site4_legacy_create_deny_short_circuits_before_run(monkeypatch, vault):
    recorder = Recorder(deny=Deny("policy_denied", "nope"))
    _patch_admit(monkeypatch, recorder)
    monkeypatch.setattr(rcm, "is_registry_active", lambda cfg: False)
    client = _FakeClient(recorder)
    mgr = _make_resource_manager(monkeypatch, client, vault)

    with pytest.raises(AdmissionDenied):
        mgr._create_resource_container(name="tm-res-adm-git")

    assert recorder.kinds == ["admit"]


def test_site4_facade_skips_direct_admit(monkeypatch, vault):
    recorder = Recorder()
    _patch_admit(monkeypatch, recorder)
    monkeypatch.setattr(rcm, "is_registry_active", lambda cfg: True)
    facade = _FakeResourceRegistry()
    monkeypatch.setattr(rcm, "get_active_registry", lambda cfg: facade)
    client = _FakeClient(recorder)
    mgr = _make_resource_manager(
        monkeypatch, client, vault, session_config={"use_container_registry": True}
    )

    mgr._create_resource_container(name="tm-res-adm-git")

    assert facade.created, "create must be delegated to the registry facade"
    # No direct admit AND no direct run at site 4.
    assert recorder.kinds == []
