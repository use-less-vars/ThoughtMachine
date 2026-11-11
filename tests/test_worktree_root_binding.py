"""Opt-in ``worktree_root`` binding: one narrower root, failing closed.

The feature lets a session point path-anchored tool operations at a git
worktree that lives INSIDE the registered workspace (e.g.
``<workspace>/.worktrees/feat``). Six things have to hold, and each has its
own group below:

1. ``TestRedirect`` -- the mandated proof: a relative path resolves under the
   worktree, and the same relative path does not appear in the wider root.
2. ``TestFailsClosed`` -- every unprovable value raises ``ValueError`` and the
   tool does not execute: ``..`` traversal, an absolute sibling that only
   shares a STRING prefix with the root, a non-directory, and a worktree with
   no resolvable registry root.
3. ``TestDefaultAbsent`` -- with no ``worktree_root`` the wiring is
   byte-identical: ``ToolExecutor`` injects no ``worktree_root`` key at all and
   ``_resolve_workspace_root()`` is the registered root.
4. ``TestSchemaHygiene`` -- the field is infrastructure, so it is not
   advertised to the model in the tool schema nor in the "Valid fields:"
   error text.
5. ``TestDockerCodeRunnerBinding`` -- the container bind SOURCE narrows to the
   worktree while the bind TARGET stays ``/workspace`` and the existing
   ``working_dir`` rules are untouched.
6. ``TestRespawnTier`` -- changing ``worktree_root`` re-anchors every tool, so
   it is a restart-required field, not a hot swap.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any, ClassVar, Dict, List

import pytest

from tools.base import ToolBase
from tools.file_editor import FileEditor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _PassthroughTool(ToolBase):
    """The smallest tool that can exercise ``_validate_path``/resolution."""

    tool: str = "PassthroughTool"
    required_categories: ClassVar[List[str]] = []

    def execute(self) -> str:
        return "executed"


def _bound(root, worktree_root=None, **kwargs: Any) -> _PassthroughTool:
    """A tool anchored at *root*, optionally narrowed to *worktree_root*.

    ``session_id`` is left unset so ``_resolve_registry_workspace`` falls back
    to the (deprecated) ``workspace_path`` field instead of touching the
    registries -- no global state, no monkeypatching.
    """
    return _PassthroughTool(
        workspace_path=str(root) if root is not None else None,
        worktree_root=str(worktree_root) if worktree_root is not None else None,
        **kwargs,
    )


@pytest.fixture()
def tree(tmp_path):
    """``(root, worktree)`` with the worktree nested inside the root."""
    root = tmp_path / "workspace"
    worktree = root / ".worktrees" / "feat"
    worktree.mkdir(parents=True)
    return root, worktree


# ---------------------------------------------------------------------------
# 1. The mandated proof
# ---------------------------------------------------------------------------


class TestRedirect:
    def test_relative_write_lands_in_the_worktree(self, tree):
        root, worktree = tree
        tool = FileEditor(
            operation="write",
            filename="proof.txt",
            content="bound to the worktree\n",
            workspace_path=str(root),
            worktree_root=str(worktree),
            effective_permissions={"filesystem": "full"},
        )

        out = tool.execute()

        assert "Error" not in out, out
        assert (worktree / "proof.txt").read_text() == "bound to the worktree\n"
        # The same relative path must NOT appear in the wider root: a silent
        # fallback to the registered workspace is the failure this prevents.
        assert not (root / "proof.txt").exists()

    def test_validate_path_resolves_relative_against_the_worktree(self, tree):
        root, worktree = tree
        tool = _bound(root, worktree)

        resolved = tool._validate_path("sub/file.txt")

        assert resolved == os.path.join(os.path.realpath(worktree), "sub", "file.txt")
        assert not resolved.startswith(os.path.realpath(root) + os.sep + "sub")

    def test_absolute_path_inside_the_worktree_is_accepted(self, tree):
        root, worktree = tree
        tool = _bound(root, worktree)

        resolved = tool._validate_path(str(worktree / "abs.txt"))

        assert resolved == os.path.join(os.path.realpath(worktree), "abs.txt")


# ---------------------------------------------------------------------------
# 2. Fails closed
# ---------------------------------------------------------------------------


class TestFailsClosed:
    def test_traversal_out_of_the_worktree_is_refused(self, tree):
        root, worktree = tree
        tool = _bound(root, worktree)

        with pytest.raises(ValueError):
            tool._validate_path("../../escaped.txt")

    def test_absolute_sibling_sharing_a_string_prefix_is_refused(self, tmp_path):
        root = tmp_path / "workspace"
        root.mkdir()
        # '<root>-evil' shares the STRING prefix with the root: a bare
        # ``startswith(root)`` check would accept it. The component-aware
        # check must not.
        sibling = tmp_path / "workspace-evil"
        sibling.mkdir()

        tool = _bound(root, sibling)

        with pytest.raises(ValueError) as excinfo:
            tool._worktree_root()
        assert "is outside workspace" in str(excinfo.value)

    def test_non_directory_is_refused(self, tree):
        root, worktree = tree
        not_a_dir = worktree / "file.txt"
        not_a_dir.write_text("x")

        tool = _bound(root, not_a_dir)

        with pytest.raises(ValueError) as excinfo:
            tool._worktree_root()
        assert "not an existing directory" in str(excinfo.value)

    def test_unresolvable_registry_root_is_refused(self, tree):
        _root, worktree = tree
        tool = _PassthroughTool(worktree_root=str(worktree))  # no workspace_path

        with pytest.raises(ValueError) as excinfo:
            tool._worktree_root()
        assert "workspace root could not be resolved" in str(excinfo.value)

    def test_a_refused_root_never_executes_a_write(self, tmp_path):
        root = tmp_path / "workspace"
        root.mkdir()
        sibling = tmp_path / "workspace-evil"
        sibling.mkdir()

        tool = FileEditor(
            operation="write",
            filename="proof.txt",
            content="must not be written\n",
            workspace_path=str(root),
            worktree_root=str(sibling),
            effective_permissions={"filesystem": "full"},
        )

        out = tool.execute()

        assert "worktree_root" in out and "outside workspace" in out, out
        assert not (sibling / "proof.txt").exists()
        assert not (root / "proof.txt").exists()


# ---------------------------------------------------------------------------
# 3. Default absent => byte-identical wiring
# ---------------------------------------------------------------------------


class _CaptureTool(ToolBase):
    """Reports whether the infrastructure field reached it at all."""

    tool: str = "CaptureTool"
    required_categories: ClassVar[List[str]] = []

    def execute(self) -> str:
        return json.dumps(
            {
                "present": "worktree_root" in self.model_fields_set,
                "value": self.worktree_root,
            }
        )


class _FakeConfig:
    """The slice of ``AgentConfig`` ``_execute_single_tool`` reads."""

    workspace_path = None
    tool_output_token_limit = None
    worktree_root = None

    def __init__(self, worktree_root=None, workspace_path=None):
        self.worktree_root = worktree_root
        self.workspace_path = workspace_path
        self.session_permissions = None


class _FakeState:
    security_config = None


def _executor(tool_classes, config=None):
    from agent.core.tool_executor import ToolExecutor

    return ToolExecutor(
        tool_classes=list(tool_classes),
        config=config if config is not None else _FakeConfig(),
        state=_FakeState(),
        logger=None,
        security_available=False,
        agent=None,
    )


def _run(executor, tool_class, arguments=None):
    return executor._execute_single_tool(
        tool_class,
        arguments or {},
        tool_class.tool_name(),
        0,
        lambda: False,
        lambda: None,
        lambda: 0,
    )


class TestDefaultAbsent:
    def test_executor_injects_no_worktree_root_by_default(self):
        executor = _executor([_CaptureTool])

        payload = json.loads(_run(executor, _CaptureTool)["result"])

        assert payload["present"] is False
        assert payload["value"] is None

    def test_executor_injects_the_configured_worktree_root(self, tree):
        _root, worktree = tree
        executor = _executor([_CaptureTool], _FakeConfig(worktree_root=str(worktree)))

        payload = json.loads(_run(executor, _CaptureTool)["result"])

        assert payload["present"] is True
        assert payload["value"] == str(worktree)

    def test_resolution_without_a_worktree_is_the_registered_root(self, tree):
        root, _worktree = tree
        tool = _bound(root)

        assert tool._resolve_workspace_root() == str(root)
        assert tool._worktree_root() is None


# ---------------------------------------------------------------------------
# 4. Schema hygiene
# ---------------------------------------------------------------------------


class TestSchemaHygiene:
    def test_tool_schema_does_not_advertise_the_field(self):
        from tools.utils import model_to_openai_tool

        props = model_to_openai_tool(FileEditor)["function"]["parameters"]["properties"]

        assert "worktree_root" not in props
        # The pre-existing infrastructure field is the control: the same rule
        # that hides ``workspace_path`` hides ``worktree_root``.
        assert "workspace_path" not in props

    def test_valid_fields_error_does_not_list_the_field(self):
        executor = _executor([FileEditor])

        result = _run(executor, FileEditor, {"bogus_field": 1})

        assert result.get("success") is False
        assert "Valid fields:" in result["result"]
        assert "worktree_root" not in result["result"]


# ---------------------------------------------------------------------------
# 5. DockerCodeRunner: source narrows, target does not
# ---------------------------------------------------------------------------


class _FakeContainerManager:
    """Records what DockerCodeRunner asks the manager to do."""

    instances: List["_FakeContainerManager"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.exec_calls: List[Dict[str, Any]] = []
        _FakeContainerManager.instances.append(self)

    def start(self, **kwargs):
        return {"id": "fake-container"}

    def exec(self, container_id, **kwargs):
        self.exec_calls.append(kwargs)
        return {"stdout": "", "stderr": "", "exit_code": 0}

    def stop(self, container_id):
        return None

    def build_image(self, tag=None):
        return None


@pytest.fixture()
def fake_manager(monkeypatch):
    from tools import docker_code_runner as module

    _FakeContainerManager.instances = []
    monkeypatch.setattr(module, "ContainerManager", _FakeContainerManager)
    monkeypatch.setattr(module, "DOCKER_AVAILABLE", True)
    return _FakeContainerManager


class TestDockerCodeRunnerBinding:
    def test_bind_source_is_the_worktree_and_the_workdir_stays_workspace(
        self, fake_manager, tree
    ):
        from tools.docker_code_runner import DockerCodeRunner

        root, worktree = tree
        runner = DockerCodeRunner(
            command="true",
            workspace_path=str(root),
            worktree_root=str(worktree),
        )

        payload = json.loads(runner.execute())

        assert payload["success"] is True, payload
        manager = fake_manager.instances[-1]
        # Bind SOURCE: narrowed to the worktree.
        assert manager.kwargs["workspace_path"] == os.path.realpath(worktree)
        # Bind TARGET is untouched: still the fixed /workspace mount point, so
        # every command keeps seeing the same path inside the container.
        assert manager.exec_calls[-1]["workdir"] == "/workspace"

    def test_mount_target_is_still_the_literal_workspace(self):
        import inspect

        import infra.container_manager as container_manager

        source = inspect.getsource(container_manager.ContainerManager)
        # The target is a literal (only the source narrows); if it ever became
        # derived from workspace_path a worktree would silently move the
        # container's mount point and every stored path would break.
        assert 'target="/workspace", source=self.workspace_path' in source

    def test_absolute_working_dir_outside_workspace_is_still_rejected(
        self, fake_manager, tree
    ):
        from tools.docker_code_runner import DockerCodeRunner

        root, worktree = tree
        runner = DockerCodeRunner(
            command="true",
            workspace_path=str(root),
            worktree_root=str(worktree),
            working_dir="/etc",
        )

        payload = json.loads(runner.execute())

        assert payload["success"] is False
        assert payload["error"] == (
            "working_dir '/etc' absolute path must be under /workspace."
        )
        assert fake_manager.instances == []


# ---------------------------------------------------------------------------
# 6. Respawn tier
# ---------------------------------------------------------------------------


def _config(**overrides: Any) -> SimpleNamespace:
    values: Dict[str, Any] = dict(
        provider_type="openai",
        model="gpt-4o",
        api_key="sk-test",
        base_url="https://api.example.com/v1",
        system_prompt="be helpful",
        workspace_path="/workspace",
        worktree_root=None,
        provider_config={},
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class TestRespawnTier:
    def test_a_worktree_root_change_requires_respawn(self):
        from agent.core.agent import config_requires_respawn

        old = _config()
        new = _config(worktree_root="/workspace/.worktrees/feat")

        assert config_requires_respawn(old, new) is True
        # ...and an unchanged value is not a respawn.
        assert config_requires_respawn(old, _config()) is False

    def test_the_tier_table_reports_restart_required(self):
        from agent.core.agent import config_field_tiers
        from tools.workspace.worker_runtime_protocol import CONFIG_TIER_REQUIRES_RESPAWN

        tiers = config_field_tiers()

        assert tiers["worktree_root"] == CONFIG_TIER_REQUIRES_RESPAWN
        # Same tier as the field it is modelled on.
        assert tiers["worktree_root"] == tiers["workspace_path"]

    def test_the_declared_category_agrees(self):
        from agent.config.models import RESTART_REQUIRED, AgentConfig

        categories = AgentConfig.FIELD_CATEGORIES

        assert categories["worktree_root"] == RESTART_REQUIRED
        assert categories["worktree_root"] == categories["workspace_path"]
