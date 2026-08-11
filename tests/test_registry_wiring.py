"""Phase 3: ContainerRegistry wiring (infra/registry_wiring.py) tests.

Covers the process-wide singleton behaviour:
- one shared registry per enabled/disabled state,
- the disabled registry never touches docker,
- ``is_registry_active`` reflects flag AND docker availability.

Plus the Phase 3 delegation contracts:
- ``ContainerManager.start/stop/remove`` delegate to the registry when it is
  active (registry request on create, name-keyed destroy on stop/remove),
  and fall back to the legacy docker path for containers the registry does
  not track;
- ``WorkerSupervisor.request_container/release_container`` delegate to the
  registry and fall back to the legacy container manager otherwise.
"""
from unittest import mock

import docker
import pytest

from infra import registry_wiring
from infra.registry_wiring import get_active_registry, is_registry_active

from infra import container_manager
from infra.container_manager import ContainerManager
from infra import workspace_lifecycle_manager
from infra.workspace_lifecycle_manager import WorkerSupervisor


class _FakeRegistry:
    """Test double mirroring ContainerRegistry's constructor semantics."""

    def __init__(self, docker_client=None, feature_flag_check=None):
        self.docker_client = docker_client
        self.feature_flag_check = feature_flag_check
        self._docker_available = False
        self.destroyed = []
        if docker_client is not None:
            self._docker_client = docker_client
            self._docker_available = True
        elif self.is_enabled():
            try:
                self._docker_client = docker.from_env()
                self._docker_available = True
            except docker.errors.DockerException:
                self._docker_available = False

    def is_enabled(self):
        if self.feature_flag_check is None:
            return True
        return bool(self.feature_flag_check())

    def destroy_container(self, name):
        self.destroyed.append(name)

    def list_all(self):
        return []


@pytest.fixture(autouse=True)
def _clear_cache():
    registry_wiring._registries.clear()
    yield
    registry_wiring._registries.clear()


@pytest.fixture
def fake_registry_cls(monkeypatch):
    monkeypatch.setattr(registry_wiring, "ContainerRegistry", _FakeRegistry)
    return _FakeRegistry


@pytest.fixture
def no_daemon():
    """docker.from_env() raises -> registry degrades to docker-less."""
    with mock.patch(
        "docker.from_env",
        side_effect=docker.errors.DockerException("no daemon"),
    ):
        yield


def test_get_active_registry_is_singleton_per_state(no_daemon):
    disabled_a = get_active_registry({})
    disabled_b = get_active_registry(None)
    assert disabled_a is disabled_b
    assert not disabled_a.is_enabled()

    enabled_cfg = {"use_container_registry": True}
    enabled_a = get_active_registry(enabled_cfg)
    enabled_b = get_active_registry(enabled_cfg)
    assert enabled_a is enabled_b
    assert enabled_a is not disabled_a


def test_enabled_singleton_shared_across_configs(fake_registry_cls):
    a = get_active_registry({"use_container_registry": True})
    b = get_active_registry({"use_container_registry": True, "other": 1})
    assert a is b
    assert a._docker_available is False  # no injected client -> docker-less


def test_disabled_registry_never_touches_docker():
    with mock.patch("docker.from_env") as from_env:
        reg = get_active_registry({})
        reg_again = get_active_registry(None)
        assert reg is reg_again
        assert not reg.is_enabled()
        assert reg._docker_available is False
        from_env.assert_not_called()


def test_is_registry_active_false_for_disabled_config():
    assert is_registry_active({}) is False
    assert is_registry_active(None) is False


def test_is_registry_active_true_for_enabled_with_client(fake_registry_cls):
    client = object()
    with mock.patch("docker.from_env", return_value=client):
        assert is_registry_active({"use_container_registry": True}) is True


def test_is_registry_active_false_when_daemon_unavailable(no_daemon):
    assert is_registry_active({"use_container_registry": True}) is False


def test_get_active_registry_constructs_enabled_instance_with_flag(fake_registry_cls):
    enabled = get_active_registry({"use_container_registry": True})
    assert enabled.is_enabled()
    # feature_flag_check=None -> is_enabled() True, docker_client None.
    assert enabled.docker_client is None
    assert enabled._docker_available is False


# ---------------------------------------------------------------------------
# ContainerManager facade delegation (Phase 3)
# ---------------------------------------------------------------------------

class _FakeDelegateRegistry:
    """Registry double for facade/WLM delegation tests (records request/destroy)."""

    def __init__(self, handles=None):
        self.handles = handles or []
        self.requested = []  # list of (args, kwargs)
        self.destroyed = []
        self.request_result = {
            "id": "c123", "name": "tm-res-abc",
            "status": "running", "container_type": "resource",
        }
        self.request_error = None

    def request_container(self, *args, **kwargs):
        self.requested.append((args, kwargs))
        if self.request_error is not None:
            raise self.request_error
        return self.request_result

    def list_all(self):
        return self.handles

    def destroy_container(self, name):
        self.destroyed.append(name)


class _FakeDockerClient:
    """Duck-typed docker client: no containers exist at all."""

    class _Containers:
        @staticmethod
        def list(all=False, filters=None):
            return []

        @staticmethod
        def get(name):
            raise docker.errors.NotFound("no such container")

        @staticmethod
        def run(**kwargs):
            raise AssertionError(
                "legacy create path must not run with the registry active"
            )

    def __init__(self):
        self.containers = self._Containers()


def _make_container_manager(client=None):
    """Bare ContainerManager wired for registry-facade tests (no daemon)."""
    cm = ContainerManager.__new__(ContainerManager)
    cm.workspace_path = "/tmp/ws-test"
    cm.session_id = "s1"
    cm.workspace_id = "w1"
    cm.session_permissions = {}
    cm._session_config = {"use_container_registry": True}
    cm.image = "agent-executor"
    cm.mem_limit = "1g"
    cm.cpu_quota = 100000
    cm._containers = {}
    cm.client = client or _FakeDockerClient()
    cm._compute_config = lambda *a, **k: ("none", "ro")
    cm.vault_root = "/tmp/tm-vault-test"
    cm.container_notes = {}
    cm.max_containers = 4
    cm.workspace_config = {"max_containers": 4}
    return cm


def _activate_registry(monkeypatch, fake):
    monkeypatch.setattr(container_manager, "is_registry_active", lambda cfg: True)
    monkeypatch.setattr(container_manager, "get_active_registry", lambda cfg: fake)


def test_container_manager_start_delegates_fresh_create_to_registry(monkeypatch):
    fake = _FakeDelegateRegistry()
    _activate_registry(monkeypatch, fake)
    cm = _make_container_manager()

    result = cm.start(name="my-box")

    assert result == {"id": "c123", "name": "my-box", "status": "created", "note": ""}
    assert cm._containers == {"my-box": "c123"}
    assert len(fake.requested) == 1
    args, kwargs = fake.requested[0]
    # (worker_id=session_id or "unknown", workspace_id=session_id or "default", perms)
    assert args == ("s1", "s1", {})
    assert kwargs["image"] == "agent-executor"
    assert kwargs["mem_limit"] == "1g"
    assert kwargs["cpu_quota"] == 100000
    assert kwargs["oom_score_adj"] == 1000
    assert kwargs["name"] == "my-box"
    assert kwargs["labels"] == {
        "thoughtmachine.container_name": "my-box",
        "thoughtmachine.workspace_id": "w1",
    }
    assert kwargs["environment"] == {"PYTHONUSERBASE": "/home/agent/.local"}
    assert kwargs["mounts"] == [
        {"source": "/tmp/ws-test", "target": "/workspace", "mode": "ro"}
    ]
    assert kwargs["volumes"] == ["tm-packages-w1:/home/agent/.local"]
    assert "/tmp" in kwargs["tmpfs"] and "/home/agent" in kwargs["tmpfs"]


def test_container_manager_start_registry_limit_error_is_mapped(monkeypatch):
    fake = _FakeDelegateRegistry()
    fake.request_error = RuntimeError("Container limit reached")
    _activate_registry(monkeypatch, fake)
    cm = _make_container_manager()

    result = cm.start(name="my-box")

    assert result == {
        "error": "Workspace container limit reached: Container limit reached"
    }
    assert cm._containers == {}


def test_container_manager_stop_destroys_via_registry_by_name(monkeypatch):
    fake = _FakeDelegateRegistry(handles=[{"id": "c123", "name": "tm-res-abc"}])
    _activate_registry(monkeypatch, fake)
    cm = _make_container_manager()
    cm._containers["my-box"] = "c123"

    result = cm.stop("c123")

    assert result == {
        "status": "stopped", "container_id": "c123", "name": "tm-res-abc"
    }
    assert fake.destroyed == ["tm-res-abc"]
    assert cm._containers == {}


def test_container_manager_remove_destroys_via_registry_by_name(monkeypatch):
    fake = _FakeDelegateRegistry(handles=[{"id": "c123", "name": "tm-res-abc"}])
    _activate_registry(monkeypatch, fake)
    cm = _make_container_manager()
    cm._containers["my-box"] = "c123"

    result = cm.remove("c123")

    assert result == {
        "status": "removed", "container_id": "c123", "name": "tm-res-abc"
    }
    assert fake.destroyed == ["tm-res-abc"]
    assert cm._containers == {}


def test_container_manager_stop_untracked_falls_back_to_legacy(monkeypatch):
    fake = _FakeDelegateRegistry(handles=[])
    _activate_registry(monkeypatch, fake)
    cm = _make_container_manager()

    result = cm.stop("legacy-1")

    # Not tracked by the registry -> legacy docker path reports missing.
    assert result.get("status") in ("missing", "error")
    assert fake.destroyed == []


# ---------------------------------------------------------------------------
# WorkerSupervisor delegation (Phase 3)
# ---------------------------------------------------------------------------

class _FakeCM:
    """Legacy container-manager double for WLM fallback assertions."""

    def __init__(self, session_config=None):
        self.session_config = session_config or {}
        self.started = []
        self.stopped = []

    def start(self, image=None, name=None, note=None):
        self.started.append({"image": image, "name": name, "note": note})
        return {"id": "legacy-1", "name": name or "agent-exec-x",
                "status": "created", "note": ""}

    def stop(self, container_id):
        self.stopped.append(container_id)
        return {"status": "stopped", "container_id": container_id}


def _activate_wlm_registry(monkeypatch, fake):
    monkeypatch.setattr(
        workspace_lifecycle_manager, "is_registry_active", lambda cfg: True
    )
    monkeypatch.setattr(
        workspace_lifecycle_manager, "get_active_registry", lambda cfg: fake
    )


def _make_supervisor(cm):
    return WorkerSupervisor("w1", cm, None, feature_flag_check=lambda: True)


def test_supervisor_request_container_delegates_to_registry(monkeypatch):
    fake = _FakeDelegateRegistry()
    _activate_wlm_registry(monkeypatch, fake)
    cm = _FakeCM(session_config={"use_container_registry": True})
    sup = _make_supervisor(cm)

    result = sup.request_container(
        {"image": "python:3.12", "name": "my-box"},
        worker_id="w2",
        session_id="s9",
    )

    assert result == fake.request_result
    assert cm.started == []  # legacy manager untouched
    assert len(fake.requested) == 1
    args, kwargs = fake.requested[0]
    assert args == ("w2", "s9", {"image": "python:3.12", "name": "my-box"})
    assert kwargs["image"] == "python:3.12"


def test_supervisor_release_container_destroys_via_registry(monkeypatch):
    fake = _FakeDelegateRegistry(handles=[{"id": "abc", "name": "tm-res-1-git"}])
    _activate_wlm_registry(monkeypatch, fake)
    cm = _FakeCM(session_config={"use_container_registry": True})
    sup = _make_supervisor(cm)

    result = sup.release_container("abc")

    assert result == {
        "status": "stopped", "container_id": "abc", "name": "tm-res-1-git"
    }
    assert fake.destroyed == ["tm-res-1-git"]
    assert cm.stopped == []


def test_supervisor_release_container_untracked_falls_back_to_cm(monkeypatch):
    fake = _FakeDelegateRegistry(handles=[])
    _activate_wlm_registry(monkeypatch, fake)
    cm = _FakeCM(session_config={"use_container_registry": True})
    sup = _make_supervisor(cm)

    result = sup.release_container("legacy-9")

    assert result == {"status": "stopped", "container_id": "legacy-9"}
    assert cm.stopped == ["legacy-9"]
    assert fake.destroyed == []

