"""Tests for git execution-mode resolution and the hook-path policy.

Covers:
1. ``resolve_git_execution_mode`` — containerized / host_fallback / unavailable,
   mirroring GitInfoTool's execution-mode decision (agent config → workspace
   metadata → default container).
2. ``validate_path`` allows workspace-local ``.githooks`` scripts while still
   blocking ``.git/config``: the hook security boundary is the policy-owned
   ``.githooks`` directory plus the resource container.
3. Container git commits run ONLY the workspace ``.githooks`` via a
   ``-c core.hooksPath=/workspace/.githooks`` override (the container-mapped
   absolute workspace path; no ``--no-verify``); non-commit operations are
   untouched.
4. Host git commits keep full hook neutralization
   (``core.hooksPath=/dev/null`` + ``--no-verify``) and never reference
   ``.githooks``.
5. CheckSystem capabilities surface the effective git execution mode.

These tests are mock-based: no real git binary or docker daemon required.
"""

import os
from types import SimpleNamespace

import pytest

from tools.git_info_tool import GitInfoTool, resolve_git_execution_mode
from thoughtmachine.security import PathOutsideWorkspaceError, validate_path


# ---------------------------------------------------------------------------
# resolve_git_execution_mode
# ---------------------------------------------------------------------------
class TestResolveGitExecutionMode:
    def test_defaults_to_containerized(self):
        assert resolve_git_execution_mode({}, {}, "/ws", "ws-1") == "containerized"

    def test_agent_config_host_wins(self):
        assert (
            resolve_git_execution_mode({"git_execution_mode": "host"}, {}, "/ws", "ws-1")
            == "host_fallback"
        )

    def test_agent_config_container(self):
        assert (
            resolve_git_execution_mode(
                {"git_execution_mode": "container"}, {}, "/ws", "ws-1"
            )
            == "containerized"
        )

    def test_workspace_metadata_fallback(self):
        assert (
            resolve_git_execution_mode({}, {"git_execution_mode": "host"}, "/ws", "ws-1")
            == "host_fallback"
        )

    def test_missing_workspace_id_falls_back_to_host(self):
        assert resolve_git_execution_mode({}, {}, "/ws", None) == "host_fallback"

    def test_missing_path_unavailable(self):
        assert resolve_git_execution_mode({}, {}, None, "ws-1") == "unavailable"
        assert resolve_git_execution_mode({}, {}, None, None) == "unavailable"


# ---------------------------------------------------------------------------
# validate_path: .githooks is allowed, .git/config still blocked
# ---------------------------------------------------------------------------
class TestValidatePathAllowsGithooks:
    def test_githooks_script_allowed(self, tmp_path):
        result = validate_path(
            str(tmp_path / ".githooks" / "pre-commit"),
            mode="read",
            workspace_path=str(tmp_path),
        )
        assert result.endswith(os.path.join(".githooks", "pre-commit"))

    def test_git_config_still_blocked(self, tmp_path):
        with pytest.raises(PathOutsideWorkspaceError):
            validate_path(
                str(tmp_path / ".git" / "config"),
                mode="read",
                workspace_path=str(tmp_path),
            )


# ---------------------------------------------------------------------------
# Container path: commit runs .githooks only (no --no-verify)
# ---------------------------------------------------------------------------
class _FakeManager:
    """Stand-in for the resource-container manager."""

    def __init__(self):
        self.calls = []

    def exec(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}


class TestContainerCommitArgs:
    def _tool(self, tmp_path):
        tool = GitInfoTool(operation="commit", message="x")
        object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
        object.__setattr__(tool, "_resolved_workspace_id", "test-ws")
        return tool

    def test_commit_injects_githooks_hookspath(self, tmp_path):
        manager = _FakeManager()
        tool = self._tool(tmp_path)
        object.__setattr__(tool, "_ensure_resource_container", lambda: manager)

        tool._exec_container_raw(tmp_path, ["commit", "-m", "x"])

        assert len(manager.calls) == 1
        command, _kwargs = manager.calls[0]
        assert command == ["git", "-c", "core.hooksPath=/workspace/.githooks", "commit", "-m", "x"]
        assert "--no-verify" not in command

    def test_non_commit_ops_unaffected(self, tmp_path):
        manager = _FakeManager()
        tool = self._tool(tmp_path)
        object.__setattr__(tool, "_ensure_resource_container", lambda: manager)

        tool._exec_container_raw(tmp_path, ["status"])

        assert len(manager.calls) == 1
        command, _kwargs = manager.calls[0]
        assert command == ["git", "status"]


# ---------------------------------------------------------------------------
# Host path: hooks fully neutralized, .githooks never referenced
# ---------------------------------------------------------------------------
class _FakeSandbox:
    """Stand-in for SandboxedExecution (host path)."""

    instances = []

    def __init__(self, **kwargs):
        _FakeSandbox.instances.append(self)
        self.calls = []

    def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")


class TestHostCommitArgs:
    def test_host_commit_keeps_no_verify_and_hooks_neutralized(self, tmp_path, monkeypatch):
        _FakeSandbox.instances.clear()
        monkeypatch.setattr("tools.git_info_tool.SandboxedExecution", _FakeSandbox)
        tool = GitInfoTool(operation="commit", message="x")

        tool._exec_host_raw(tmp_path, ["commit", "-m", "x"])

        assert len(_FakeSandbox.instances) == 1
        command, _kwargs = _FakeSandbox.instances[0].calls[0]
        assert command[0] == "git"
        assert command.index("core.hooksPath=/dev/null") < command.index("commit")
        assert "--no-verify" in command
        assert "core.hooksPath=.githooks" not in command


# ---------------------------------------------------------------------------
# CheckSystem capabilities: git execution mode surfaced
# ---------------------------------------------------------------------------
class TestCheckSystemGitMode:
    def _make(self, agent_config, workspace_path):
        from tools.workspace.check_system import CheckSystem

        tool = CheckSystem(query="capabilities")
        object.__setattr__(tool, "agent_config", agent_config)
        if workspace_path is not None:
            object.__setattr__(tool, "workspace_path", workspace_path)
        return tool

    def test_container_mode_surfaced(self, tmp_path):
        result = self._make(
            {"git_execution_mode": "container"}, str(tmp_path)
        )._query_capabilities("ws-1", str(tmp_path))
        assert result["git"]["mode"] == "containerized"

    def test_host_mode_surfaced(self, tmp_path):
        result = self._make(
            {"git_execution_mode": "host"}, str(tmp_path)
        )._query_capabilities("ws-1", str(tmp_path))
        assert result["git"]["mode"] == "host_fallback"

    def test_unavailable_without_workspace(self, tmp_path):
        result = self._make({}, None)._query_capabilities(None, None)
        assert result["git"]["mode"] == "unavailable"
