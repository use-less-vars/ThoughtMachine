"""Tests for container identity environment (session_id / workspace_id) injection.

Covers:
  A. the pure merge helper in infra/container_env.py,
  B. ContainerManager registry-path start() environment,
  C. ContainerManager direct/legacy start() environment,
  D. ContainerManager.exec() environment merge,
  E. ResourceContainerManager direct-create environment,
  F. ResourceContainerManager registry-path create environment,
  G. ResourceContainerManager.exec() environment merge,
  H. GitReadTool plumbing forwarding session_id into ResourceContainerManager.
"""

import copy
import os
import types

import pytest

from infra.container_env import (
    SESSION_ID_ENV,
    WORKSPACE_ID_ENV,
    merge_container_identity_env,
)
import infra.container_manager as container_manager
import infra.resource_container_manager as rcm
from infra.container_manager import ContainerManager
from tools.git_info_tool import GitReadTool

# ---------------------------------------------------------------------------
# Fake shapes (kept minimal per-test; mirror tests/test_registry_wiring.py)
# ---------------------------------------------------------------------------


class _FakeImageRef:
    def __init__(self, image_id):
        self.id = image_id

    def __str__(self):
        return self.id


class _FakeContainer:
    def __init__(
        self,
        container_id,
        name=None,
        image_id=None,
        status="running",
        labels=None,
        attrs=None,
        exec_result=None,
    ):
        self.id = container_id
        self.name = name or container_id
        self.image = _FakeImageRef(image_id or "sha256:" + container_id)
        self.status = status
        self.labels = dict(labels or {})
        self.attrs = attrs or {"State": {"Status": status}}
        self.started = []
        self.removed = []
        self.reloaded = []
        self.exec_calls = []
        self._exec_result = exec_result

    def start(self):
        self.started.append(True)
        self.status = "running"

    def remove(self, **kwargs):
        self.removed.append(kwargs)

    def reload(self):
        self.reloaded.append(True)

    def exec_run(self, **kwargs):
        self.exec_calls.append(kwargs)
        if self._exec_result is not None:
            return self._exec_result
        return (0, (b"out", b"err"))


class _FakeContainers:
    def __init__(self, containers=None):
        self.containers = list(containers or [])
        self.run_calls = []
        self.list_calls = []

    def list(self, all=False, filters=None):
        self.list_calls.append({"all": all, "filters": filters})
        return copy.copy(self.containers)

    def get(self, container_id):
        for c in self.containers:
            if c.id == container_id or c.name == container_id:
                return c
        raise LookupError(container_id)

    def run(self, *args, **kwargs):
        self.run_calls.append({"args": args, "kwargs": kwargs})
        name = kwargs.get("name") or "run-ctr"
        ctr = _FakeContainer(
            container_id="c-run-1",
            name=name,
            status="created",
            labels=kwargs.get("labels") or {},
        )
        self.containers.append(ctr)
        return ctr


class _FakeDockerClient:
    def __init__(self, containers=None):
        self.containers = _FakeContainers(containers or [])


class _FakeDockerModule:
    def __init__(self, containers=None):
        self._client = _FakeDockerClient(containers)

    def from_env(self, **kwargs):
        return self._client


class _FakeDelegateRegistry:
    """Records request_container / destroy_container like the registry wiring."""

    def __init__(self, request_result=None, handles=None):
        self.requested = []
        self.destroyed = []
        self.handles = list(handles or [])
        self.request_result = request_result or {
            "id": "c123",
            "name": "tm-res-abc",
            "status": "running",
            "container_type": "resource",
        }

    def request_container(self, *args, **kwargs):
        self.requested.append((args, kwargs))
        return dict(self.request_result)

    def destroy_container(self, *args, **kwargs):
        self.destroyed.append((args, kwargs))

    def list_all(self, *args, **kwargs):
        return copy.copy(self.handles)


class _RegistryRecorder:
    """Records create_resource_container kwargs (resource-container registry path)."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result or {
            "id": "c-r-1",
            "name": "tm-res-x",
            "status": "running",
        }

    def create_resource_container(self, **kwargs):
        self.calls.append(kwargs)
        return dict(self.result)


# ---------------------------------------------------------------------------
# House builders
# ---------------------------------------------------------------------------


def _make_container_manager(
    client=None,
    *,
    session_id="s1",
    workspace_id="w1",
    session_config=None,
):
    cm = ContainerManager.__new__(ContainerManager)
    cm.workspace_path = "/tmp/ws-test"
    cm.session_id = session_id
    cm.workspace_id = workspace_id
    cm.session_permissions = {}
    cm._session_config = session_config or {"use_container_registry": True}
    cm.image = "agent-executor"
    cm.mem_limit = "1g"
    cm.cpu_quota = 100000
    cm._containers = {}
    cm.client = client if client is not None else _FakeDockerClient()
    cm._compute_config = lambda *a, **k: ("none", "ro")
    cm.vault_root = "/tmp/tm-vault-test"
    cm.container_notes = {}
    cm.max_containers = 4
    cm.workspace_config = {"max_containers": 4}
    cm.dockerfile_path = None
    return cm


def _activate_registry(monkeypatch, fake):
    monkeypatch.setattr(
        container_manager, "is_registry_active", lambda cfg: True
    )
    monkeypatch.setattr(
        container_manager, "get_active_registry", lambda cfg: fake
    )


def _deactivate_registry(monkeypatch):
    monkeypatch.setattr(
        container_manager, "is_registry_active", lambda cfg: False
    )


def _make_resource_manager(
    monkeypatch,
    fake_docker=None,
    *,
    session_id="s-r",
    workspace_id="ws-r",
    workspace_path="/tmp/tm-env-ws",
    session_config=None,
):
    if fake_docker is None:
        fake_docker = _FakeDockerModule()
    monkeypatch.setattr(rcm, "docker", fake_docker)
    monkeypatch.setattr(rcm, "_ensure_resource_image", lambda: True)
    return rcm.ResourceContainerManager(
        workspace_id=workspace_id,
        workspace_path=workspace_path,
        network_mode="none",
        vault_root="/tmp/tm-env-vault",
        session_config=session_config,
        session_id=session_id,
    )


# ===========================================================================
# A. Pure helper tests
# ===========================================================================


class TestMergeHelperPure:
    def test_none_returns_empty(self):
        assert merge_container_identity_env() == {}

    def test_session_and_workspace_injected(self):
        env = merge_container_identity_env(
            None, session_id="sess-1", workspace_id="ws-1"
        )
        assert env == {SESSION_ID_ENV: "sess-1", WORKSPACE_ID_ENV: "ws-1"}

    def test_base_env_preserved_and_merged(self):
        env = merge_container_identity_env(
            {"PYTHONUSERBASE": "/home/agent/.local"},
            session_id="sess-1",
            workspace_id="ws-1",
        )
        assert env == {
            "PYTHONUSERBASE": "/home/agent/.local",
            SESSION_ID_ENV: "sess-1",
            WORKSPACE_ID_ENV: "ws-1",
        }

    def test_no_clobber_caller_value_wins(self):
        env = merge_container_identity_env(
            {SESSION_ID_ENV: "caller", WORKSPACE_ID_ENV: "caller-ws"},
            session_id="sess-1",
            workspace_id="ws-1",
        )
        assert env[SESSION_ID_ENV] == "caller"
        assert env[WORKSPACE_ID_ENV] == "caller-ws"

    def test_str_coercion(self):
        env = merge_container_identity_env(None, session_id=7, workspace_id=42)
        assert env == {SESSION_ID_ENV: "7", WORKSPACE_ID_ENV: "42"}

    def test_truthy_only_omits_falsy_values(self):
        env = merge_container_identity_env(
            None, session_id=None, workspace_id=""
        )
        assert env == {}
        env = merge_container_identity_env(None, session_id=0, workspace_id="w")
        assert env == {WORKSPACE_ID_ENV: "w"}

    def test_input_dict_not_mutated_and_fresh_object(self):
        base = {SESSION_ID_ENV: "keep"}
        result = merge_container_identity_env(
            base, session_id="other", workspace_id="ws-1"
        )
        assert base == {SESSION_ID_ENV: "keep"}
        assert result is not base
        assert result[SESSION_ID_ENV] == "keep"
        assert result[WORKSPACE_ID_ENV] == "ws-1"

    def test_list_of_kv_parsed(self):
        env = merge_container_identity_env(
            ["PYTHONUSERBASE=/home/agent/.local", "BROKEN", "=noval", "K="],
            session_id="s1",
            workspace_id="w1",
        )
        # list parsing keeps any item with a key and a "=" separator
        # (empty VALUES are preserved; "BROKEN" / "=noval" are skipped).
        assert env == {
            "PYTHONUSERBASE": "/home/agent/.local",
            "K": "",
            SESSION_ID_ENV: "s1",
            WORKSPACE_ID_ENV: "w1",
        }

    def test_list_input_not_mutated(self):
        base = ["A=B", "C=D"]
        result = merge_container_identity_env(base, session_id="s")
        assert base == ["A=B", "C=D"]
        assert isinstance(result, dict)
        assert result["A"] == "B"

    def test_none_session_omits_session_key(self):
        env = merge_container_identity_env(None, session_id=None, workspace_id="w1")
        assert env == {WORKSPACE_ID_ENV: "w1"}


# ===========================================================================
# B/C. ContainerManager.start() environment
# ===========================================================================


class TestContainerManagerStart:
    def test_registry_path_injects_identity_env(self, monkeypatch):
        fake_reg = _FakeDelegateRegistry()
        _activate_registry(monkeypatch, fake_reg)
        cm = _make_container_manager(
            session_id="s1", workspace_id="w1",
            session_config={"use_container_registry": True},
        )
        result = cm.start(name="my-box")
        assert result["id"] == fake_reg.request_result["id"]
        assert len(fake_reg.requested) == 1
        args, kwargs = fake_reg.requested[0]
        assert kwargs["environment"] == {
            "PYTHONUSERBASE": "/home/agent/.local",
            SESSION_ID_ENV: "s1",
            WORKSPACE_ID_ENV: "w1",
        }

    def test_registry_path_no_session(self, monkeypatch):
        fake_reg = _FakeDelegateRegistry()
        _activate_registry(monkeypatch, fake_reg)
        cm = _make_container_manager(
            session_id=None, workspace_id="w1",
            session_config={"use_container_registry": True},
        )
        cm.start(name="my-box")
        args, kwargs = fake_reg.requested[0]
        assert kwargs["environment"] == {
            "PYTHONUSERBASE": "/home/agent/.local",
            WORKSPACE_ID_ENV: "w1",
        }
        assert SESSION_ID_ENV not in kwargs["environment"]

    def test_direct_path_injects_identity_env(self, monkeypatch):
        _deactivate_registry(monkeypatch)
        client = _FakeDockerClient()
        cm = _make_container_manager(client, session_id="s1", workspace_id="w1")
        result = cm.start(name="my-box")
        assert result["id"] == "c-run-1"
        assert len(client.containers.run_calls) == 1
        env = client.containers.run_calls[0]["kwargs"]["environment"]
        assert env == {
            "PYTHONUSERBASE": "/home/agent/.local",
            SESSION_ID_ENV: "s1",
            WORKSPACE_ID_ENV: "w1",
        }

    def test_direct_path_no_session_id(self, monkeypatch):
        _deactivate_registry(monkeypatch)
        client = _FakeDockerClient()
        cm = _make_container_manager(client, session_id=None, workspace_id="w1")
        cm.start(name="my-box")
        env = client.containers.run_calls[0]["kwargs"]["environment"]
        assert env == {
            "PYTHONUSERBASE": "/home/agent/.local",
            WORKSPACE_ID_ENV: "w1",
        }
        assert SESSION_ID_ENV not in env


# ===========================================================================
# D. ContainerManager.exec() environment merge
# ===========================================================================


class TestContainerManagerExec:
    def _make_exec_cm(self, *, session_id="s1", workspace_id="w1"):
        target = _FakeContainer(
            container_id="c-run-1",
            name="c-run-1",
            status="running",
            labels={},
        )
        client = _FakeDockerClient([target])
        cm = _make_container_manager(
            client, session_id=session_id, workspace_id=workspace_id
        )
        cm.workspace_config = {"disk_quota_mb": 0}
        cm.vault_root = "/tmp/tm-vault-test"
        return cm, client

    def test_environment_base_plus_identity(self):
        cm, client = self._make_exec_cm()
        result = cm.exec(
            "c-run-1", ["echo", "hi"], timeout=10, environment={"CUSTOM": "x"}
        )
        assert result["exit_code"] == 0
        assert result["stdout"] == "out"
        ctr = client.containers.containers[0]
        assert len(ctr.exec_calls) >= 1
        env = ctr.exec_calls[0]["environment"]
        assert env == {
            "CUSTOM": "x",
            SESSION_ID_ENV: "s1",
            WORKSPACE_ID_ENV: "w1",
        }

    def test_environment_none_still_injects_identity(self):
        cm, client = self._make_exec_cm()
        result = cm.exec("c-run-1", ["echo", "hi"], timeout=10, environment=None)
        assert result["exit_code"] == 0
        ctr = client.containers.containers[0]
        env = ctr.exec_calls[0]["environment"]
        assert env == {SESSION_ID_ENV: "s1", WORKSPACE_ID_ENV: "w1"}

    def test_environment_empty_injects_identity(self):
        cm, client = self._make_exec_cm()
        result = cm.exec("c-run-1", ["echo", "hi"], timeout=10, environment={})
        assert result["exit_code"] == 0
        ctr = client.containers.containers[0]
        env = ctr.exec_calls[0]["environment"]
        assert env == {SESSION_ID_ENV: "s1", WORKSPACE_ID_ENV: "w1"}

    def test_no_clobber_at_exec_level(self):
        cm, client = self._make_exec_cm()
        result = cm.exec(
            "c-run-1",
            ["echo", "hi"],
            timeout=10,
            environment={WORKSPACE_ID_ENV: "override-ws"},
        )
        assert result["exit_code"] == 0
        ctr = client.containers.containers[0]
        env = ctr.exec_calls[0]["environment"]
        assert env[WORKSPACE_ID_ENV] == "override-ws"
        assert env[SESSION_ID_ENV] == "s1"

    def test_no_identity_still_executes(self):
        cm, client = self._make_exec_cm(session_id=None, workspace_id=None)
        result = cm.exec("c-run-1", ["echo", "hi"], timeout=10, environment=None)
        assert result["exit_code"] == 0
        ctr = client.containers.containers[0]
        first = ctr.exec_calls[0]
        assert "environment" not in first or first["environment"] == {}


# ===========================================================================
# E/F. ResourceContainerManager create environment
# ===========================================================================


class TestResourceContainerManagerCreate:
    def test_direct_create_workspace_only_when_session_none(self, monkeypatch):
        fake_docker = _FakeDockerModule()
        mgr = _make_resource_manager(monkeypatch, fake_docker, session_id=None)
        monkeypatch.setattr(rcm, "is_registry_active", lambda cfg: False)
        cid = mgr.ensure_container()
        assert cid
        assert len(fake_docker._client.containers.run_calls) == 1
        env = fake_docker._client.containers.run_calls[0]["kwargs"]["environment"]
        assert env == {WORKSPACE_ID_ENV: "ws-r"}

    def test_direct_create_with_session_injects_both(self, monkeypatch):
        fake_docker = _FakeDockerModule()
        mgr = _make_resource_manager(monkeypatch, fake_docker, session_id="s-r")
        monkeypatch.setattr(rcm, "is_registry_active", lambda cfg: False)
        mgr.ensure_container()
        assert len(fake_docker._client.containers.run_calls) == 1
        env = fake_docker._client.containers.run_calls[0]["kwargs"]["environment"]
        assert env == {SESSION_ID_ENV: "s-r", WORKSPACE_ID_ENV: "ws-r"}

    def test_registry_path_injects_identity_env(self, monkeypatch):
        fake_docker = _FakeDockerModule()
        recorder = _RegistryRecorder()
        mgr = _make_resource_manager(
            monkeypatch,
            fake_docker,
            session_id="s-r",
            session_config={"use_container_registry": True},
        )
        monkeypatch.setattr(rcm, "is_registry_active", lambda cfg: True)
        monkeypatch.setattr(rcm, "get_active_registry", lambda cfg: recorder)
        cid = mgr.ensure_container()
        assert cid == "c-r-1"
        assert len(recorder.calls) == 1
        env = recorder.calls[0]["environment"]
        assert env == {SESSION_ID_ENV: "s-r", WORKSPACE_ID_ENV: "ws-r"}
        assert recorder.calls[0]["session_id"] == "s-r"
        assert recorder.calls[0]["workspace_id"] == "ws-r"
        # direct docker run must NOT be used on registry path
        assert fake_docker._client.containers.run_calls == []


# ===========================================================================
# G. ResourceContainerManager.exec() environment merge
# ===========================================================================


class TestResourceContainerManagerExec:
    def _make_mgr(self, monkeypatch, *, session_id="s-r", workspace_id="ws-r"):
        # RCM label constants are CLASS attributes -> resolve via the class.
        RCM = rcm.ResourceContainerManager
        fake_docker = _FakeDockerModule()
        mgr = _make_resource_manager(
            monkeypatch,
            fake_docker,
            session_id=session_id,
            workspace_id=workspace_id,
        )
        seed = _FakeContainer(
            container_id="c-res",
            name=mgr.container_name,
            status="running",
            labels={
                RCM.WORKSPACE_LABEL: workspace_id,
                RCM.RESOURCE_LABEL: RCM.RESOURCE_KIND,
                RCM.CONTAINER_NAME_LABEL: mgr.container_name,
            },
        )
        fake_docker._client.containers.containers.append(seed)
        return mgr, fake_docker, seed

    def test_exec_environment_merged_with_identity(self, monkeypatch):
        mgr, fake_docker, seed = self._make_mgr(monkeypatch)
        result = mgr.exec(["echo", "hi"], environment={"K": "v"}, timeout=10)
        assert result["exit_code"] == 0
        assert len(seed.exec_calls) == 1
        env = seed.exec_calls[0]["environment"]
        assert env == {
            "K": "v",
            SESSION_ID_ENV: "s-r",
            WORKSPACE_ID_ENV: "ws-r",
        }

    def test_exec_environment_none_injects_identity_only(self, monkeypatch):
        mgr, fake_docker, seed = self._make_mgr(monkeypatch)
        result = mgr.exec(["echo", "hi"], environment=None, timeout=10)
        assert result["exit_code"] == 0
        env = seed.exec_calls[0]["environment"]
        assert env == {SESSION_ID_ENV: "s-r", WORKSPACE_ID_ENV: "ws-r"}


# ===========================================================================
# H. GitReadTool plumbing forwards session_id
# ===========================================================================


class TestGitReadToolPlumbing:
    def test_resource_manager_ctor_receives_session_id(self, monkeypatch):
        ctor_kwargs = []
        instances = []

        class _RecorderRCM:
            def __init__(self, **kwargs):
                ctor_kwargs.append(kwargs)
                instances.append(self)

            def ensure_resource(self, kind):
                return {
                    "mode": "containerized",
                    "container_id": "c1",
                    "name": "tm-res-x",
                    "status": "running",
                }

        monkeypatch.setattr(rcm, "ResourceContainerManager", _RecorderRCM)
        # GitReadTool is a pydantic model: build it normally (only
        # ``operation`` is required) and override the container-mode gate at
        # class level (a pydantic instance rejects non-field attributes).
        monkeypatch.setattr(GitReadTool, "_use_container_mode", lambda self: True)

        tool = GitReadTool(
            operation="status",
            session_id="s-git",
            workspace_id="ws-git",
            session_permissions={},
        )
        tool._resolved_workspace_path = "/tmp/ws-git"
        tool._resolved_workspace_id = "ws-git"
        # _resource_manager defaults to None on a fresh instance.
        assert tool._resource_manager is None

        mode, manager = tool._resolve_resource_execution()
        assert mode["mode"] == "containerized"
        assert manager is tool._resource_manager
        assert len(ctor_kwargs) == 1
        assert ctor_kwargs[0]["session_id"] == "s-git"
        assert ctor_kwargs[0]["workspace_id"] == "ws-git"
