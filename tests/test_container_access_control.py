"""
tests/test_container_access_control.py

Tests for the resource-container access-control work on branch
feat/resource-container-access-control:

  1. Container-type labeling on creation (``free_use`` vs ``resource``):
       - ContainerRegistry.request_container stamps
         ``thoughtmachine.container_type=free_use`` (preserving caller labels)
       - ContainerRegistry.create_resource_container stamps
         ``thoughtmachine.container_type=resource`` + ``resource_name``
         + ``thoughtmachine.resource``
       - ResourceContainerManager._labels
       - request_container rejects resource-container requests (PermissionError)
  2. security_gate.check_requires_resource (resource grain checks)
  3. Tool-resource binding (ToolBase / GitReadTool / GitWriteTool) and
     ToolExecutor enforcement of the bound resource grain
  4. Vault build-source protection: resource-image builds may only read
     <vault>/docker/resource/ (_resolve_vault_build_source + both context
     preparers)
  5. ContainerManager denies exec on resource-labeled containers

The Docker SDK surface is fully mocked (NO real daemon).
"""

import os
import shutil
import sys
from pathlib import Path
from typing import ClassVar, List, Optional
from unittest import mock

import pytest

# Make the repository root importable when running this file directly.
_SRC_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

from agent.config.defaults import (  # noqa: E402
    CONTAINER_TYPE_FREE_USE,
    CONTAINER_TYPE_LABEL,
    CONTAINER_TYPE_RESOURCE,
    RESOURCE_NAME_LABEL,
)
import infra.container_manager as cm_mod  # noqa: E402
from infra.container_manager import ContainerManager  # noqa: E402
from infra.container_registry import (  # noqa: E402
    CONTAINER_NAME_LABEL,
    RESOURCE_IMAGE_TAG,
    RESOURCE_LABEL,
    WORKSPACE_ID_LABEL,
    ContainerRegistry,
)
import infra.resource_container_manager as rcm  # noqa: E402
from infra.resource_container_manager import (  # noqa: E402
    ResourceContainerManager,
    _prepare_git_overlay_build_context,
    _prepare_resource_build_context,
    _resolve_vault_build_source,
)
from security.security_gate import check_requires_resource  # noqa: E402
from tools.base import ToolBase  # noqa: E402
from tools.git_info_tool import GitReadTool  # noqa: E402
from tools.git_write_tool import GitWriteTool  # noqa: E402
from thoughtmachine.security import SessionPermissions  # noqa: E402


# ---------------------------------------------------------------------------
# Fake Docker surface (mirrors tests/test_container_registry.py)
# ---------------------------------------------------------------------------


class FakeContainer:
    """Minimal stand-in for docker.models.containers.Container."""

    def __init__(self, name):
        self.name = name
        self.id = f"id-{name}"
        self.stopped = False
        self.removed = False

    def stop(self, timeout=None):
        self.stopped = True

    def remove(self, force=False):
        self.removed = True


class FakeClient:
    """Minimal stand-in for docker.client.DockerClient."""

    def __init__(self):
        self.containers = mock.Mock()
        self.images = mock.Mock()
        self.containers.run = mock.Mock(
            side_effect=lambda image, command=None, **kwargs: FakeContainer(kwargs["name"])
        )
        self.containers.get = mock.Mock(
            side_effect=lambda name: FakeContainer(name)
        )


@pytest.fixture
def fake_client():
    return FakeClient()


@pytest.fixture
def registry(fake_client):
    return ContainerRegistry(docker_client=fake_client, feature_flag_check=lambda: True)


def _run_kwargs(fake_client):
    """kwargs of the last containers.run call (folding positional args in)."""
    call = fake_client.containers.run.call_args
    merged = {}
    if call.args:
        merged["image"] = call.args[0]
    if len(call.args) > 1:
        merged["command"] = call.args[1]
    merged.update(call.kwargs)
    return merged


# ---------------------------------------------------------------------------
# 1. Container-type labeling on creation
# ---------------------------------------------------------------------------


class TestContainerTypeLabeling:
    def test_request_container_stamps_free_use_label(self, registry, fake_client):
        registry.request_container("w", "s", {}, workspace_id="ws1")
        labels = _run_kwargs(fake_client)["labels"]
        assert labels[CONTAINER_TYPE_LABEL] == CONTAINER_TYPE_FREE_USE

    def test_request_container_preserves_caller_labels(self, registry, fake_client):
        registry.request_container(
            "w", "s", {}, workspace_id="ws1", labels={"a": "b"}
        )
        labels = _run_kwargs(fake_client)["labels"]
        assert labels["a"] == "b"
        assert labels[CONTAINER_TYPE_LABEL] == CONTAINER_TYPE_FREE_USE

    def test_create_resource_container_stamps_resource_labels(self, registry, fake_client):
        with mock.patch.object(
            registry, "_ensure_resource_image_or_raise", return_value=None
        ):
            handle = registry.create_resource_container(
                "sess-1", "ws-1", "none", workspace_path="/tmp/ws-x"
            )
        assert handle["container_type"] == "resource"
        labels = _run_kwargs(fake_client)["labels"]
        assert labels[CONTAINER_TYPE_LABEL] == CONTAINER_TYPE_RESOURCE
        assert labels[RESOURCE_NAME_LABEL] == "git"
        assert labels[RESOURCE_LABEL] == "git"
        assert labels[WORKSPACE_ID_LABEL] == "ws-1"
        assert labels[CONTAINER_NAME_LABEL] == handle["name"]

    def test_request_container_rejects_resource_requests(self, registry):
        with pytest.raises(PermissionError, match="Resource container access denied"):
            registry.request_container("w", "s", {}, container_type="resource")
        with pytest.raises(PermissionError, match="Resource container access denied"):
            registry.request_container("w", "s", {}, image=RESOURCE_IMAGE_TAG)
        with pytest.raises(PermissionError, match="Resource container access denied"):
            registry.request_container("w", "s", {}, name="tm-res-x")

    def test_resource_container_manager_labels(self):
        # object.__new__ avoids __init__'s docker.from_env() call.
        mgr = object.__new__(ResourceContainerManager)
        mgr.workspace_id = "ws-9"
        mgr.workspace_path = "/tmp/ws-y"
        labels = mgr._labels()
        assert labels[WORKSPACE_ID_LABEL] == "ws-9"
        assert labels[RESOURCE_LABEL] == "git"
        assert labels[CONTAINER_TYPE_LABEL] == CONTAINER_TYPE_RESOURCE
        assert labels[RESOURCE_NAME_LABEL] == "git"
        assert labels[CONTAINER_NAME_LABEL] == mgr.container_name
        # Explicit name override wins over the derived container name.
        assert mgr._labels("tm-res-explicit")[CONTAINER_NAME_LABEL] == "tm-res-explicit"


# ---------------------------------------------------------------------------
# 2. security_gate.check_requires_resource
# ---------------------------------------------------------------------------


class TestCheckRequiresResource:
    def test_allowed_with_git_read(self):
        ok, err = check_requires_resource("git", {"git": "read"}, "GitReadTool")
        assert ok is True
        assert err == ""

    def test_denied_with_git_banned(self):
        ok, err = check_requires_resource("git", {"git": "banned"}, "GitReadTool")
        assert ok is False
        assert "Tool requires resource 'git'" in err
        assert "git:read" in err

    def test_denied_when_permission_key_missing(self):
        ok, err = check_requires_resource("git", {"filesystem": "read"}, "GitReadTool")
        assert ok is False
        assert "git:read" in err

    def test_unknown_resource_fails_closed(self):
        ok, err = check_requires_resource("no_such_resource", {"git": "read"}, "ToolX")
        assert ok is False
        assert "unknown resource 'no_such_resource'" in err


# ---------------------------------------------------------------------------
# 3. Tool-resource binding + ToolExecutor enforcement
# ---------------------------------------------------------------------------


class TestToolResourceBinding:
    def test_toolbase_default_is_none(self):
        assert ToolBase.requires_resource is None

    def test_git_read_tool_bound_to_git(self):
        assert GitReadTool.requires_resource == "git"

    def test_git_write_tool_inherits_git_binding(self):
        assert GitWriteTool.requires_resource == "git"


class ResourceBoundTool(ToolBase):
    """Stub tool bound to the git resource (mirrors GitReadTool's binding)."""

    tool: str = "ResourceBoundTool"
    required_categories: ClassVar[List[str]] = []
    requires_resource: ClassVar[Optional[str]] = "git"

    def execute(self) -> str:
        return "RESOURCE OK"


class FakeConfig:
    """Minimal config stub for ToolExecutor (same shape as test_tool_executor)."""

    workspace_path = None
    tool_output_token_limit = None

    def __init__(self, permissions=None):
        self.session_permissions = permissions


class FakeState:
    security_config = None


class TestToolExecutorResourceEnforcement:
    def _make_executor(self, tool_classes, permissions=None):
        from agent.core.tool_executor import ToolExecutor

        return ToolExecutor(
            tool_classes=tool_classes,
            config=FakeConfig(permissions),
            state=FakeState(),
            logger=None,
            security_available=False,
            agent=None,
        )

    def _run(self, executor, tool_class, name):
        return executor._execute_single_tool(
            tool_class, {}, name, 0,
            lambda: False, lambda: None, lambda: 0,
        )

    def test_resource_bound_tool_denied_when_git_banned(self):
        executor = self._make_executor(
            [ResourceBoundTool], permissions=SessionPermissions(git="banned")
        )
        result = self._run(executor, ResourceBoundTool, "ResourceBoundTool")
        assert result["tool_type"] == "normal"
        assert "Tool requires resource 'git'" in result["result"]
        assert "git:read" in result["result"]

    def test_resource_bound_tool_allowed_with_git_read(self):
        # SessionPermissions() defaults git='read'
        executor = self._make_executor([ResourceBoundTool])
        result = self._run(executor, ResourceBoundTool, "ResourceBoundTool")
        assert result["result"] == "RESOURCE OK"
        assert result["tool_type"] == "normal"

    def test_unbound_tool_runs_with_git_banned(self):
        class UnboundTool(ToolBase):
            tool: str = "UnboundTool"
            required_categories: ClassVar[List[str]] = []

            def execute(self) -> str:
                return "OK"

        executor = self._make_executor(
            [UnboundTool], permissions=SessionPermissions(git="banned")
        )
        result = self._run(executor, UnboundTool, "UnboundTool")
        assert result["result"] == "OK"


# ---------------------------------------------------------------------------
# 4. Vault build-source protection
# ---------------------------------------------------------------------------


class TestVaultBuildSourceProtection:
    def _vault(self, tmp_path):
        """Create a fake <vault>/docker/resource/ directory."""
        vault_dir = tmp_path / "vault" / "docker" / "resource"
        vault_dir.mkdir(parents=True)
        (vault_dir / "requirements.txt").write_text("requests==2.31.0\n")
        (vault_dir / "default_runtime.Dockerfile").write_text("FROM base\n")
        (vault_dir / "git_overlay.Dockerfile").write_text("FROM runtime\n")
        return vault_dir

    # -- _resolve_vault_build_source --------------------------------------

    def test_resolve_in_vault_returns_realpath(self, tmp_path, monkeypatch):
        vault_dir = self._vault(tmp_path)
        monkeypatch.setattr(rcm, "VAULT_RESOURCE_DIR", str(vault_dir))
        source = vault_dir / "requirements.txt"
        assert _resolve_vault_build_source(str(source)) == os.path.realpath(str(source))

    def test_resolve_outside_returns_none(self, tmp_path, monkeypatch):
        vault_dir = self._vault(tmp_path)
        monkeypatch.setattr(rcm, "VAULT_RESOURCE_DIR", str(vault_dir))
        outside = tmp_path / "outside.txt"
        outside.write_text("x")
        assert _resolve_vault_build_source(str(outside)) is None

    def test_resolve_symlink_escape_returns_none(self, tmp_path, monkeypatch):
        vault_dir = self._vault(tmp_path)
        monkeypatch.setattr(rcm, "VAULT_RESOURCE_DIR", str(vault_dir))
        outside = tmp_path / "secret.txt"
        outside.write_text("secret")
        link = vault_dir / "escape_link"
        link.symlink_to(outside)
        # realpath follows the symlink out of the vault -> rejected
        assert _resolve_vault_build_source(str(link)) is None

    # -- _prepare_resource_build_context ------------------------------------

    def test_prepare_resource_build_context_rejects_outside(self, tmp_path, monkeypatch):
        vault_dir = self._vault(tmp_path)
        monkeypatch.setattr(rcm, "VAULT_RESOURCE_DIR", str(vault_dir))
        monkeypatch.setattr(
            rcm, "VAULT_REQUIREMENTS", str(tmp_path / "outside-requirements.txt")
        )
        monkeypatch.setattr(
            rcm, "VAULT_RUNTIME_DOCKERFILE", str(vault_dir / "default_runtime.Dockerfile")
        )
        assert _prepare_resource_build_context() is None

    def test_prepare_resource_build_context_stages_vault_files(self, tmp_path, monkeypatch):
        vault_dir = self._vault(tmp_path)
        monkeypatch.setattr(rcm, "VAULT_RESOURCE_DIR", str(vault_dir))
        monkeypatch.setattr(rcm, "VAULT_REQUIREMENTS", str(vault_dir / "requirements.txt"))
        monkeypatch.setattr(
            rcm, "VAULT_RUNTIME_DOCKERFILE", str(vault_dir / "default_runtime.Dockerfile")
        )
        result = _prepare_resource_build_context()
        assert result is not None
        context_dir, build_hash = result
        try:
            staged_req = (Path(context_dir) / "requirements.txt").read_text()
            staged_df = (Path(context_dir) / "Dockerfile").read_text()
            assert staged_req == "requests==2.31.0\n"
            assert staged_df == "FROM base\n"
            expected = rcm._hash_resource_bytes(
                b"requests==2.31.0\n", b"FROM base\n"
            )
            assert build_hash == expected
        finally:
            shutil.rmtree(context_dir, ignore_errors=True)

    # -- _prepare_git_overlay_build_context ---------------------------------

    def test_prepare_git_overlay_rejects_outside(self, tmp_path, monkeypatch):
        vault_dir = self._vault(tmp_path)
        monkeypatch.setattr(rcm, "VAULT_RESOURCE_DIR", str(vault_dir))
        monkeypatch.setattr(
            rcm, "VAULT_OVERLAY_DOCKERFILE", str(tmp_path / "outside-overlay.Dockerfile")
        )
        assert _prepare_git_overlay_build_context() is None

    def test_prepare_git_overlay_stages_dockerfile(self, tmp_path, monkeypatch):
        vault_dir = self._vault(tmp_path)
        monkeypatch.setattr(rcm, "VAULT_RESOURCE_DIR", str(vault_dir))
        monkeypatch.setattr(
            rcm, "VAULT_OVERLAY_DOCKERFILE", str(vault_dir / "git_overlay.Dockerfile")
        )
        context_dir = _prepare_git_overlay_build_context()
        assert context_dir is not None
        try:
            assert (Path(context_dir) / "Dockerfile").read_text() == "FROM runtime\n"
        finally:
            shutil.rmtree(context_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 5. ContainerManager denies access to resource-labeled containers
# ---------------------------------------------------------------------------


class TestContainerManagerResourceDenial:
    def _resource_container(self):
        container = mock.Mock()
        container.labels = {"thoughtmachine.resource": "git"}
        container.name = "tm-res-abc-git"
        container.image = "tm-resource-git"
        return container

    def test_exec_denied_for_resource_labeled_container(self):
        mgr = object.__new__(ContainerManager)
        mgr.client = mock.Mock()
        mgr.client.containers.get.return_value = self._resource_container()
        with pytest.raises(PermissionError, match="Resource container access denied"):
            mgr.exec("tm-res-abc", ["git", "status"])

    def test_is_resource_container_by_label(self):
        container = self._resource_container()
        assert ContainerManager._is_resource_container(container) is True

    def test_is_resource_container_by_name_prefix(self):
        container = mock.Mock()
        container.labels = {}
        container.name = "tm-res-abc-git"
        container.image = "agent-executor"
        assert ContainerManager._is_resource_container(container) is True

    def test_is_resource_container_false_for_free_use(self):
        container = mock.Mock()
        container.labels = {"thoughtmachine.container_type": "free_use"}
        container.name = "agent-exec-123"
        container.image = "agent-executor"
        assert ContainerManager._is_resource_container(container) is False
