"""
Unit tests for infra/container_registry (phase 2 — registry core).

The Docker SDK surface is fully mocked (NO real daemon): docker.from_env,
client.containers.run, client.containers.get, client.images.get,
container.stop, container.remove.  A small FakeClient/FakeContainer helper
pair stands in for the SDK.

Run:  pytest tests/test_container_registry.py -q --tb=short
"""

import os
import re
import sys
import threading
import time
from unittest import mock

import docker
import pytest

# Make the repository root importable when running this file directly.
_SRC_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

from infra.container_registry import (  # noqa: E402
    CONTAINER_TYPES,
    DEFAULT_COMMAND,
    DEFAULT_CPU_QUOTA,
    DEFAULT_IMAGE,
    DEFAULT_MAX_CONTAINERS,
    DEFAULT_MEM_LIMIT,
    DEFAULT_RESOURCE_OOM_SCORE_ADJ,
    DEFAULT_TMPFS,
    DEFAULT_USER_OOM_SCORE_ADJ,
    HARDENED_CAP_DROP,
    HARDENED_READ_ONLY,
    HARDENED_SECURITY_OPT,
    HARDENED_USER,
    RESOURCE_IMAGE_TAG,
    STOP_TIMEOUT,
    ContainerProfile,
    ContainerRegistry,
    create_hardened_container,
    get_container_registry,
    is_container_registry_enabled,
)


# ---------------------------------------------------------------------------
# Fake Docker surface
# ---------------------------------------------------------------------------


class FakeContainer:
    """Minimal stand-in for docker.models.containers.Container."""

    def __init__(self, name):
        self.name = name
        self.id = f"id-{name}"
        self.stopped = False
        self.removed = False
        self.force_removed = False
        self.stop_timeout = None

    def stop(self, timeout=None):
        self.stop_timeout = timeout
        self.stopped = True

    def remove(self, force=False):
        if force:
            self.force_removed = True
        self.removed = True


class FakeClient:
    """Minimal stand-in for docker.client.DockerClient.

    ``containers.run`` returns a FakeContainer named after the ``name`` kwarg;
    ``containers.get`` returns a FakeContainer and records it in ``gotten`` so
    tests can inspect stop/remove behavior.
    """

    def __init__(self):
        self.gotten = {}
        self.containers = mock.Mock()
        self.images = mock.Mock()
        self.containers.run = mock.Mock(
            side_effect=lambda image, command=None, **kwargs: FakeContainer(kwargs["name"])
        )
        self.containers.get = mock.Mock(side_effect=self._get)

    def _get(self, name):
        container = FakeContainer(name)
        self.gotten[name] = container
        return container


@pytest.fixture
def fake_client():
    return FakeClient()


@pytest.fixture
def registry(fake_client):
    return ContainerRegistry(docker_client=fake_client, feature_flag_check=lambda: True)


def _run_kwargs(fake_client):
    """kwargs of the last containers.run call, folding the positional
    image/command args in (the factory calls run(image, command, ...))."""
    call = fake_client.containers.run.call_args
    merged = {}
    if call.args:
        merged["image"] = call.args[0]
    if len(call.args) > 1:
        merged["command"] = call.args[1]
    merged.update(call.kwargs)
    return merged


# ---------------------------------------------------------------------------
# ContainerProfile
# ---------------------------------------------------------------------------


class TestContainerProfile:
    def test_oom_default_by_type(self):
        assert ContainerProfile(image="i").oom_score_adj == DEFAULT_USER_OOM_SCORE_ADJ
        assert ContainerProfile(image="i", container_type="user").oom_score_adj == 1000
        assert ContainerProfile(image="i", container_type="resource").oom_score_adj == 500
        assert ContainerProfile(image="i", container_type="mcp").oom_score_adj == 500
        assert ContainerProfile(image="i", container_type="proxy").oom_score_adj == 500

    def test_explicit_oom_wins(self):
        profile = ContainerProfile(image="i", oom_score_adj=777)
        assert profile.oom_score_adj == 777

    def test_defaults_command_tmpfs_limits(self):
        profile = ContainerProfile(image="i")
        assert profile.command == ["tail", "-f", "/dev/null"]
        assert profile.tmpfs == DEFAULT_TMPFS
        assert profile.mem_limit == DEFAULT_MEM_LIMIT
        assert profile.cpu_quota == DEFAULT_CPU_QUOTA
        assert profile.network_mode == "none"
        assert profile.image == "i"

    def test_invalid_container_type_raises(self):
        with pytest.raises(ValueError, match="Unknown container type"):
            ContainerProfile(image="i", container_type="bogus")

    def test_containers_type_constant(self):
        assert CONTAINER_TYPES == ("user", "resource", "mcp", "proxy")


# ---------------------------------------------------------------------------
# create_hardened_container
# ---------------------------------------------------------------------------


class TestCreateHardenedContainer:
    def test_passes_profile_and_full_hardening(self, fake_client):
        profile = ContainerProfile(
            image="img",
            command=["echo", "hi"],
            container_type="user",
            mem_limit="2g",
            cpu_quota=50000,
            oom_score_adj=42,
            network_mode="bridge",
            labels={"thoughtmachine.workspace_id": "ws1"},
            environment={"FOO": "bar"},
            mounts=[{"source": "/host", "target": "/guest", "mode": "rw"}],
            tmpfs={"/tmp": "rw,size=8m"},
            extra_hosts={"host.docker.internal": "1.2.3.4"},
            volumes=["vol1:/data"],
        )
        container = create_hardened_container(fake_client, profile, "tm-user-x")
        assert container.name == "tm-user-x"
        kwargs = _run_kwargs(fake_client)
        assert kwargs["image"] == "img"
        assert kwargs["command"] == ["echo", "hi"]
        assert kwargs["name"] == "tm-user-x"
        assert kwargs["oom_score_adj"] == 42
        assert kwargs["network_mode"] == "bridge"
        assert kwargs["mem_limit"] == "2g"
        assert kwargs["cpu_quota"] == 50000
        assert kwargs["cap_drop"] == HARDENED_CAP_DROP
        assert kwargs["security_opt"] == HARDENED_SECURITY_OPT
        assert kwargs["read_only"] is HARDENED_READ_ONLY
        assert kwargs["user"] == HARDENED_USER
        assert kwargs["detach"] is True
        assert kwargs["tty"] is True
        assert kwargs["stdin_open"] is True
        assert kwargs["labels"] == {"thoughtmachine.workspace_id": "ws1"}
        assert kwargs["environment"] == {"FOO": "bar"}
        assert kwargs["tmpfs"] == {"/tmp": "rw,size=8m"}
        assert kwargs["extra_hosts"] == {"host.docker.internal": "1.2.3.4"}
        assert kwargs["volumes"] == ["vol1:/data"]
        assert kwargs["mounts"] == [
            {"source": "/host", "target": "/guest", "type": "bind", "read_only": False}
        ]

    def test_default_profile_hardening(self, fake_client):
        create_hardened_container(fake_client, ContainerProfile(image="i"), "n1")
        kwargs = _run_kwargs(fake_client)
        assert kwargs["command"] == DEFAULT_COMMAND
        assert kwargs["oom_score_adj"] == DEFAULT_USER_OOM_SCORE_ADJ
        assert kwargs["network_mode"] == "none"
        assert kwargs["mem_limit"] == DEFAULT_MEM_LIMIT
        assert kwargs["cpu_quota"] == DEFAULT_CPU_QUOTA
        assert kwargs["tmpfs"] == DEFAULT_TMPFS
        assert kwargs["mounts"] == []
        assert kwargs["volumes"] == []
        assert kwargs["labels"] == {}
        assert kwargs["environment"] == {}
        assert kwargs["extra_hosts"] == {}

    def test_mount_conversion_ro_and_default_mode(self, fake_client):
        profile = ContainerProfile(
            image="i",
            mounts=[
                {"source": "/a", "target": "/b", "mode": "ro"},
                {"source": "/c", "target": "/d"},
                {"source": "/e", "target": "/f", "mode": "rw"},
            ],
        )
        create_hardened_container(fake_client, profile, "n2")
        assert _run_kwargs(fake_client)["mounts"] == [
            {"source": "/a", "target": "/b", "type": "bind", "read_only": True},
            {"source": "/c", "target": "/d", "type": "bind", "read_only": False},
            {"source": "/e", "target": "/f", "type": "bind", "read_only": False},
        ]

    def test_docker_exception_propagates(self, fake_client):
        fake_client.containers.run.side_effect = docker.errors.DockerException("boom")
        with pytest.raises(docker.errors.DockerException):
            create_hardened_container(fake_client, ContainerProfile(image="i"), "n3")

    def test_inputs_are_copied_not_mutated(self, fake_client):
        profile = ContainerProfile(
            image="i", mounts=[{"source": "/a", "target": "/b", "mode": "rw"}]
        )
        create_hardened_container(fake_client, profile, "n4")
        assert profile.mounts == [{"source": "/a", "target": "/b", "mode": "rw"}]


# ---------------------------------------------------------------------------
# register / unregister / listing
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_and_listing(self, registry):
        profile = ContainerProfile(image="i", container_type="user")
        registry.register("c1", "sess1", "ws1", "user", profile)
        registry.register("c2", "sess1", "ws1", "resource", profile)
        registry.register("c3", "sess2", "ws2", "user", profile)

        assert registry.list_all() == [
            {"id": "", "name": "c1", "status": "registered", "container_type": "user"},
            {"id": "", "name": "c2", "status": "registered", "container_type": "resource"},
            {"id": "", "name": "c3", "status": "registered", "container_type": "user"},
        ]
        assert registry.get_containers_for_session("sess1") == [
            {"id": "", "name": "c1", "status": "registered", "container_type": "user"},
            {"id": "", "name": "c2", "status": "registered", "container_type": "resource"},
        ]
        assert registry.get_containers_for_session("nope") == []

    def test_register_duplicate_raises_valueerror(self, registry):
        profile = ContainerProfile(image="i")
        registry.register("c1", "s1", "w1", "user", profile)
        with pytest.raises(ValueError, match="already registered"):
            registry.register("c1", "s1", "w1", "user", profile)

    def test_register_invalid_type_raises_valueerror(self, registry):
        with pytest.raises(ValueError, match="Unknown container type"):
            registry.register("c1", "s1", "w1", "bogus", ContainerProfile(image="i"))

    def test_unregister(self, registry):
        profile = ContainerProfile(image="i")
        registry.register("c1", "s1", "w1", "user", profile)
        registry.register("c2", "s1", "w1", "user", profile)
        assert registry.unregister("c1") is True
        assert "c1" not in registry._containers
        assert "c1" not in registry._session_map["s1"]
        assert registry.get_containers_for_session("s1") == [
            {"id": "", "name": "c2", "status": "registered", "container_type": "user"}
        ]

    def test_unregister_absent_returns_false(self, registry):
        assert registry.unregister("ghost") is False


# ---------------------------------------------------------------------------
# request_container
# ---------------------------------------------------------------------------


class TestRequestContainer:
    def test_request_creates_registers_and_returns_handle(self, registry, fake_client):
        handle = registry.request_container(
            "worker-1", "sess-1", {"network": "write", "filesystem": "write"},
            workspace_id="ws-123",
        )
        assert set(handle) == {"id", "name", "status", "container_type"}
        assert handle["status"] == "running"
        assert handle["container_type"] == "user"
        assert re.fullmatch(r"tm-user-ws-123-[0-9a-f]{8}", handle["name"])
        assert handle["id"] == f"id-{handle['name']}"

        kwargs = _run_kwargs(fake_client)
        assert kwargs["name"] == handle["name"]
        assert kwargs["image"] == DEFAULT_IMAGE
        assert kwargs["command"] == DEFAULT_COMMAND
        assert kwargs["network_mode"] == "bridge"  # network write -> bridge
        assert kwargs["oom_score_adj"] == DEFAULT_USER_OOM_SCORE_ADJ
        assert kwargs["cap_drop"] == HARDENED_CAP_DROP
        assert kwargs["security_opt"] == HARDENED_SECURITY_OPT
        assert kwargs["read_only"] is True
        assert kwargs["user"] == HARDENED_USER
        assert kwargs["detach"] is True

        # registered with a running status + container id
        state = registry._containers[handle["name"]]
        assert state["status"] == "running"
        assert state["container_id"] == handle["id"]
        assert state["session_id"] == "sess-1"
        assert state["workspace_id"] == "ws-123"
        assert state["quarantined"] is False
        assert registry.get_containers_for_session("sess-1") == [handle]

    def test_request_network_resolution(self, registry, fake_client):
        registry.request_container("w", "s", {"network": "write"})
        assert _run_kwargs(fake_client)["network_mode"] == "bridge"
        registry.request_container("w", "s", {"network": True})
        assert _run_kwargs(fake_client)["network_mode"] == "bridge"
        registry.request_container("w", "s", {"network": False})
        assert _run_kwargs(fake_client)["network_mode"] == "none"
        registry.request_container("w", "s", {})
        assert _run_kwargs(fake_client)["network_mode"] == "none"

    def test_request_unique_names(self, registry):
        h1 = registry.request_container("w", "s", {})
        h2 = registry.request_container("w", "s", {})
        assert h1["name"] != h2["name"]
        assert len(registry._session_map["s"]) == 2

    def test_request_kwargs_flow_into_profile(self, registry, fake_client):
        handle = registry.request_container(
            "w", "s", {}, workspace_id="ws9", mem_limit="512m", cpu_quota=50000,
            oom_score_adj=500, labels={"a": "b"}, environment={"X": "1"},
            mounts=[{"source": "/s", "target": "/t", "mode": "ro"}],
            tmpfs={"/tmp": "rw,size=4m"}, extra_hosts={"h": "1.2.3.4"},
            volumes=["v:/d"],
        )
        kwargs = _run_kwargs(fake_client)
        assert kwargs["name"] == handle["name"]
        assert kwargs["mem_limit"] == "512m"
        assert kwargs["cpu_quota"] == 50000
        assert kwargs["oom_score_adj"] == 500
        assert kwargs["labels"] == {"a": "b"}
        assert kwargs["environment"] == {"X": "1"}
        assert kwargs["tmpfs"] == {"/tmp": "rw,size=4m"}
        assert kwargs["extra_hosts"] == {"h": "1.2.3.4"}
        assert kwargs["volumes"] == ["v:/d"]
        assert kwargs["mounts"] == [
            {"source": "/s", "target": "/t", "type": "bind", "read_only": True}
        ]

    def test_request_network_mode_kwarg_overridden_by_permissions(self, registry, fake_client):
        registry.request_container("w", "s", {"network": False}, network_mode="bridge")
        assert _run_kwargs(fake_client)["network_mode"] == "none"

    def test_request_resource_guard(self, registry):
        with pytest.raises(PermissionError, match="Resource container access denied"):
            registry.request_container("w", "s", {}, image=RESOURCE_IMAGE_TAG)
        with pytest.raises(PermissionError, match="Resource container access denied"):
            registry.request_container("w", "s", {}, container_type="resource")
        with pytest.raises(PermissionError, match="Resource container access denied"):
            registry.request_container("w", "s", {}, name="tm-res-x")

    def test_request_container_limit_reached(self, registry, fake_client):
        profile = ContainerProfile(image="i")
        for i in range(DEFAULT_MAX_CONTAINERS):
            registry.register(f"c{i}", "sess-l", "ws", "user", profile)
        assert fake_client.containers.run.call_count == 0
        with pytest.raises(RuntimeError, match="Container limit reached"):
            registry.request_container("w", "sess-l", {})
        assert fake_client.containers.run.call_count == 0

    def test_request_limit_from_session_config(self, registry, fake_client):
        registry.request_container(
            "w", "sess-c", {}, session_config={"container_limits": {"max_containers": 1}}
        )
        with pytest.raises(RuntimeError, match="Container limit reached"):
            registry.request_container(
                "w", "sess-c", {}, session_config={"container_limits": {"max_containers": 1}}
            )
        # different session is unaffected
        registry.request_container(
            "w", "sess-d", {}, session_config={"container_limits": {"max_containers": 1}}
        )

    def test_request_invalid_type_raises_before_docker(self, registry, fake_client):
        with pytest.raises(ValueError, match="Unknown container type"):
            registry.request_container("w", "s", {}, container_type="bogus")
        assert fake_client.containers.run.call_count == 0

    def test_request_disabled_raises(self):
        reg = ContainerRegistry(docker_client=None, feature_flag_check=lambda: False)
        assert reg.is_enabled() is False
        with pytest.raises(RuntimeError, match="ContainerRegistry is disabled"):
            reg.request_container("w", "s", {})

    def test_request_without_docker_client_raises(self):
        reg = ContainerRegistry(docker_client=None, feature_flag_check=lambda: True)
        with pytest.raises(RuntimeError, match="Docker client unavailable"):
            reg.request_container("w", "s", {})


# ---------------------------------------------------------------------------
# destroy_container
# ---------------------------------------------------------------------------


class TestDestroyContainer:
    def test_graceful_stop_remove_unregister(self, registry, fake_client):
        handle = registry.request_container("w", "sess-d", {})
        registry.destroy_container(handle["name"])
        gotten = fake_client.gotten[handle["name"]]
        assert gotten.stop_timeout == STOP_TIMEOUT
        assert gotten.stopped is True
        assert gotten.removed is True
        assert gotten.force_removed is False
        assert handle["name"] not in registry._containers
        # unregister removes the now-empty session key entirely
        assert handle["name"] not in registry._session_map.get("sess-d", set())

    def test_force_remove_fallback_on_docker_exception(self, registry, fake_client):
        handle = registry.request_container("w", "sess-f", {})

        class BoomStop(FakeContainer):
            def stop(self, timeout=None):
                raise docker.errors.DockerException("stop failed")

        def get_boom(name):
            container = BoomStop(name)
            fake_client.gotten[name] = container
            return container

        fake_client.containers.get.side_effect = get_boom
        registry.destroy_container(handle["name"])
        assert fake_client.gotten[handle["name"]].force_removed is True
        assert handle["name"] not in registry._containers

    def test_destroy_unregistered_is_noop(self, registry, fake_client):
        registry.destroy_container("ghost")
        assert fake_client.containers.get.call_count == 0

    def test_destroy_unregisters_even_when_docker_get_fails(self, registry, fake_client):
        handle = registry.request_container("w", "sess-g", {})
        fake_client.containers.get.side_effect = docker.errors.DockerException("missing")
        registry.destroy_container(handle["name"])
        assert handle["name"] not in registry._containers


# ---------------------------------------------------------------------------
# on_permission_changed
# ---------------------------------------------------------------------------


class TestPermissionReconciliation:
    def test_noop_when_network_unchanged(self, registry, fake_client):
        handle = registry.request_container("w", "sess-p", {"network": False})
        registry.on_permission_changed("sess-p", {"network": False})
        assert fake_client.containers.run.call_count == 1  # only the initial create
        assert fake_client.containers.get.call_count == 0
        assert registry._containers[handle["name"]]["status"] == "running"

    def test_recreate_on_network_change_same_name(self, registry, fake_client):
        handle = registry.request_container("w", "sess-p", {"network": False})
        assert _run_kwargs(fake_client)["network_mode"] == "none"

        registry.on_permission_changed("sess-p", {"network": "write"})
        assert fake_client.containers.run.call_count == 2
        assert _run_kwargs(fake_client)["network_mode"] == "bridge"
        assert _run_kwargs(fake_client)["name"] == handle["name"]  # SAME name
        # teardown used the graceful path on the old container
        assert fake_client.gotten[handle["name"]].stopped is True
        assert fake_client.gotten[handle["name"]].removed is True
        # state updated: still one registered container, running, new profile
        assert registry.get_containers_for_session("sess-p") == [
            {"id": f"id-{handle['name']}", "name": handle["name"],
             "status": "running", "container_type": "user"}
        ]
        state = registry._containers[handle["name"]]
        assert state["profile"].network_mode == "bridge"
        assert state["quarantined"] is False

    def test_idempotent_second_event_is_noop(self, registry, fake_client):
        registry.request_container("w", "sess-p", {"network": False})
        registry.on_permission_changed("sess-p", {"network": "write"})
        assert fake_client.containers.run.call_count == 2
        registry.on_permission_changed("sess-p", {"network": "write"})
        assert fake_client.containers.run.call_count == 2  # no third create

    def test_teardown_failure_quarantines(self, registry, fake_client):
        handle = registry.request_container("w", "sess-q", {"network": False})

        class BoomStop(FakeContainer):
            def stop(self, timeout=None):
                raise docker.errors.DockerException("stop failed")

        fake_client.containers.get.side_effect = lambda name: BoomStop(name)
        registry.on_permission_changed("sess-q", {"network": "write"})
        # quarantined: removed from the session map, no recreate
        assert fake_client.containers.run.call_count == 1
        state = registry._containers[handle["name"]]
        assert state["quarantined"] is True
        assert state["status"] == "quarantined"
        assert handle["name"] not in registry._session_map["sess-q"]

    def test_recreate_failure_does_not_raise(self, registry, fake_client):
        handle = registry.request_container("w", "sess-r", {"network": False})
        fake_client.containers.run.side_effect = docker.errors.DockerException("create failed")
        registry.on_permission_changed("sess-r", {"network": "write"})
        state = registry._containers[handle["name"]]
        assert state["status"] == "recreate_failed"
        assert handle["name"] in registry._session_map["sess-r"]  # slot kept for retry
        assert state["quarantined"] is False


# ---------------------------------------------------------------------------
# feature flag / helpers / docker init
# ---------------------------------------------------------------------------


class TestFeatureFlagAndHelpers:
    def test_is_container_registry_enabled(self):
        assert is_container_registry_enabled(None) is False
        assert is_container_registry_enabled({}) is False
        assert is_container_registry_enabled({"use_container_registry": False}) is False
        assert is_container_registry_enabled({"use_container_registry": True}) is True

    def test_get_container_registry_disabled_config_never_touches_docker(self):
        with mock.patch("docker.from_env") as from_env:
            reg = get_container_registry(session_config={"use_container_registry": False})
            from_env.assert_not_called()
        assert reg.is_enabled() is False
        assert reg._docker_client is None
        with pytest.raises(RuntimeError, match="ContainerRegistry is disabled"):
            reg.request_container("w", "s", {})

    def test_get_container_registry_enabled_config(self, fake_client):
        reg = get_container_registry(
            docker_client=fake_client, session_config={"use_container_registry": True}
        )
        assert reg.is_enabled() is True
        handle = reg.request_container("w", "s", {})
        assert handle["status"] == "running"

    def test_constructor_skips_docker_when_flag_off(self):
        with mock.patch("docker.from_env") as from_env:
            reg = ContainerRegistry(docker_client=None, feature_flag_check=lambda: False)
            from_env.assert_not_called()
        assert reg._docker_available is False

    def test_constructor_connects_when_flag_on(self):
        with mock.patch("docker.from_env", return_value=FakeClient()) as from_env:
            reg = ContainerRegistry(docker_client=None, feature_flag_check=lambda: True)
            from_env.assert_called_once()
        assert reg._docker_available is True

    def test_constructor_from_env_failure_is_graceful(self):
        with mock.patch("docker.from_env", side_effect=docker.errors.DockerException("no daemon")):
            reg = ContainerRegistry(docker_client=None, feature_flag_check=lambda: True)
        assert reg._docker_available is False
        with pytest.raises(RuntimeError, match="Docker client unavailable"):
            reg.request_container("w", "s", {})

    def test_default_flag_check_means_enabled(self, fake_client):
        reg = ContainerRegistry(docker_client=fake_client)
        assert reg.is_enabled() is True
        assert reg.request_container("w", "s", {})["status"] == "running"

    def test_is_resource_image_available(self, fake_client):
        reg = ContainerRegistry(docker_client=fake_client, feature_flag_check=lambda: True)
        fake_client.images.get.return_value = object()
        assert reg.is_resource_image_available() is True
        fake_client.images.get.side_effect = docker.errors.ImageNotFound("missing")
        assert reg.is_resource_image_available() is False
        fake_client.images.get.side_effect = docker.errors.DockerException("daemon down")
        assert reg.is_resource_image_available() is False

    def test_is_resource_image_available_without_client(self):
        reg = ContainerRegistry(docker_client=None, feature_flag_check=lambda: False)
        assert reg.is_resource_image_available() is False

    def test_resolve_network_mode(self):
        assert ContainerRegistry.resolve_network_mode({"network": "write"}) == "bridge"
        assert ContainerRegistry.resolve_network_mode({"network": True}) == "bridge"
        assert ContainerRegistry.resolve_network_mode({"network": False}) == "none"
        assert ContainerRegistry.resolve_network_mode({}) == "none"
        assert ContainerRegistry.resolve_network_mode(None) == "none"


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_concurrent_request_containers(self, fake_client):
        real_run = fake_client.containers.run.side_effect

        def slow_run(image, command=None, **kwargs):
            time.sleep(0.01)
            return real_run(image, command, **kwargs)

        fake_client.containers.run.side_effect = slow_run
        reg = ContainerRegistry(docker_client=fake_client, feature_flag_check=lambda: True)

        errors, results = [], []

        def worker(n):
            try:
                results.append(reg.request_container(f"w{n}", "sess-cc", {"network": False}))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(results) == 2
        assert len({h["name"] for h in results}) == 2  # unique names
        assert len(reg._session_map["sess-cc"]) == 2
        assert len(reg._containers) == 2
