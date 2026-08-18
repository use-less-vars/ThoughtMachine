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
# Execution-time self-healing: ensure_resource("git") drives the ACTUAL mode
# ---------------------------------------------------------------------------
class _FakeEnsureManager:
    """Manager fake exposing ensure_resource + exec for mode routing."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def ensure_resource(self, name):
        self.calls.append(("ensure_resource", name))
        return self.result

    def exec(self, command, **kwargs):
        self.calls.append(("exec", command, kwargs))
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}


class TestSelfHealingModeRouting:
    """ensure_resource outcome decides container vs hardened-host routing."""

    HOST_FALLBACK_DETAIL = (
        "resource image unavailable (auto-build failed or Docker unreachable); "
        "manual build: docker build ..."
    )
    POLICY_DENIAL_DETAIL = (
        "container resources disabled/denied: session/workspace policy "
        "denies container usage (effective container=False)"
    )

    def _container_tool(self, tmp_path, operation="commit"):
        """Tool configured for container mode with a registry workspace."""
        tool = GitInfoTool(operation=operation, message="x")
        object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
        object.__setattr__(tool, "_resolved_workspace_id", "test-ws")
        return tool

    def _host_sandbox(self, monkeypatch):
        _FakeSandbox.instances.clear()
        monkeypatch.setattr("tools.git_info_tool.SandboxedExecution", _FakeSandbox)
        return _FakeSandbox

    def test_containerized_uses_container_exec_path(self, tmp_path):
        manager = _FakeEnsureManager(
            {
                "mode": "containerized",
                "container_id": "c1",
                "status": "running",
                "image": "tm-resource-git",
                "detail": "",
            }
        )
        tool = self._container_tool(tmp_path)
        object.__setattr__(tool, "_resource_manager", manager)

        tool._run_git_raw(tmp_path, ["commit", "-m", "x"])

        execs = [c for c in manager.calls if c[0] == "exec"]
        assert len(execs) == 1
        command = execs[0][1]
        assert command == [
            "git", "-c", "core.hooksPath=/workspace/.githooks",
            "commit", "-m", "x",
        ]
        assert "--no-verify" not in command

    def test_host_fallback_commit_uses_hardened_host_path(self, tmp_path, monkeypatch):
        _FakeSandbox = self._host_sandbox(monkeypatch)
        manager = _FakeEnsureManager(
            {
                "mode": "host_fallback",
                "container_id": None,
                "status": None,
                "image": None,
                "detail": self.HOST_FALLBACK_DETAIL,
            }
        )
        tool = self._container_tool(tmp_path)
        object.__setattr__(tool, "_resource_manager", manager)

        tool._run_git_raw(tmp_path, ["commit", "-m", "x"])

        # Degradation must NOT reach the container exec path.
        assert not [c for c in manager.calls if c[0] == "exec"]
        assert len(_FakeSandbox.instances) == 1
        command, _kwargs = _FakeSandbox.instances[0].calls[0]
        assert command[0] == "git"
        assert command.index("core.hooksPath=/dev/null") < command.index("commit")
        assert "--no-verify" in command
        assert "core.hooksPath=.githooks" not in command

    def test_unavailable_policy_denial_raises_clear_error(self, tmp_path):
        manager = _FakeEnsureManager(
            {
                "mode": "unavailable",
                "container_id": None,
                "status": None,
                "image": None,
                "detail": self.POLICY_DENIAL_DETAIL,
            }
        )
        tool = self._container_tool(tmp_path, operation="status")
        object.__setattr__(tool, "_resource_manager", manager)

        with pytest.raises(RuntimeError) as excinfo:
            tool._run_git_raw(tmp_path, ["status"])
        assert "containerized git execution unavailable" in str(excinfo.value)
        assert "container resources disabled/denied" in str(excinfo.value)
        assert not [c for c in manager.calls if c[0] == "exec"]

    def test_host_fallback_status_runs_without_no_verify(self, tmp_path, monkeypatch):
        _FakeSandbox = self._host_sandbox(monkeypatch)
        manager = _FakeEnsureManager(
            {
                "mode": "host_fallback",
                "container_id": None,
                "status": None,
                "image": None,
                "detail": self.HOST_FALLBACK_DETAIL,
            }
        )
        tool = self._container_tool(tmp_path, operation="status")
        object.__setattr__(tool, "_resource_manager", manager)

        tool._run_git_raw(tmp_path, ["status"])

        assert not [c for c in manager.calls if c[0] == "exec"]
        assert len(_FakeSandbox.instances) == 1
        command, _kwargs = _FakeSandbox.instances[0].calls[0]
        assert "--no-verify" not in command
        assert command.index("core.hooksPath=/dev/null") < command.index("status")

    def test_manager_ctor_receives_session_permissions(self, tmp_path, monkeypatch):
        captured = {}

        class _CtorCapture:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self._inner = _FakeEnsureManager(
                    {
                        "mode": "containerized",
                        "container_id": "c1",
                        "status": "running",
                        "image": "tm-resource-git",
                        "detail": "",
                    }
                )

            def ensure_resource(self, name):
                return self._inner.ensure_resource(name)

            def exec(self, command, **kwargs):
                return self._inner.exec(command, **kwargs)

        monkeypatch.setattr(
            "infra.resource_container_manager.ResourceContainerManager", _CtorCapture
        )
        tool = GitInfoTool(
            operation="status",
            message="x",
            session_permissions={"git": "read"},
            effective_permissions={"git": "read"},
        )
        object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
        object.__setattr__(tool, "_resolved_workspace_id", "test-ws")

        tool._run_git_raw(tmp_path, ["status"])

        assert captured.get("session_permissions") == {"git": "read"}
        assert captured.get("workspace_path") == str(tmp_path)
        assert captured.get("workspace_id") == "test-ws"


    def test_host_fallback_failure_reason_plumbing(self, tmp_path, monkeypatch):
        """failure_reason/fallback_used from ensure_resource reach the trailer."""
        _FakeSandbox = self._host_sandbox(monkeypatch)
        manager = _FakeEnsureManager(
            {
                "mode": "host_fallback",
                "container_id": None,
                "status": None,
                "image": None,
                "detail": self.HOST_FALLBACK_DETAIL,
                "failure_reason": "build_failed",
                "fallback_used": True,
            }
        )
        tool = self._container_tool(tmp_path)
        object.__setattr__(tool, "_resource_manager", manager)

        tool._run_git_raw(tmp_path, ["status"])

        assert tool._last_failure_reason == "build_failed"
        assert tool._last_fallback_used is True
        trailer = tool._with_mode("out")
        assert "execution_mode: host_fallback" in trailer
        assert "failure_reason: build_failed" in trailer
        assert "fallback_used: true" in trailer

    def test_containerized_no_failure_keys_defaults(self, tmp_path):
        """Missing failure_reason/fallback_used keys default to None/False."""
        manager = _FakeEnsureManager(
            {
                "mode": "containerized",
                "container_id": "c1",
                "status": "running",
                "image": "tm-resource-git",
                "detail": "",
            }
        )
        tool = self._container_tool(tmp_path, operation="status")
        object.__setattr__(tool, "_resource_manager", manager)

        tool._run_git_raw(tmp_path, ["status"])

        assert tool._last_failure_reason is None
        assert tool._last_fallback_used is False

    def test_unavailable_with_failure_reason_suffix(self, tmp_path):
        """unavailable with a failure_reason includes it in the raise message."""
        manager = _FakeEnsureManager(
            {
                "mode": "unavailable",
                "container_id": None,
                "status": None,
                "image": None,
                "detail": self.POLICY_DENIAL_DETAIL,
                "failure_reason": "policy_denied",
            }
        )
        tool = self._container_tool(tmp_path, operation="status")
        object.__setattr__(tool, "_resource_manager", manager)

        with pytest.raises(RuntimeError) as excinfo:
            tool._run_git_raw(tmp_path, ["status"])
        assert "(failure_reason: policy_denied)" in str(excinfo.value)


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

    @staticmethod
    def _probe(monkeypatch, result):
        """Replace the module-level resource_status with a fixed return value.

        The capabilities query imports ``resource_status`` lazily at call
        time, so patching the ``infra.resource_container_manager`` module
        attribute is sufficient. Returns the list of (resource, kwargs) calls
        for assertion.
        """
        calls = []

        def _fake_resource_status(resource, **kwargs):
            calls.append((resource, kwargs))
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(
            "infra.resource_container_manager.resource_status",
            _fake_resource_status,
        )
        return calls

    def test_container_mode_surfaced(self, tmp_path, monkeypatch):
        self._probe(
            monkeypatch,
            {
                "mode": "containerized",
                "container_id": "c1",
                "status": "running",
                "image": "tm-resource-git",
                "detail": "",
            },
        )
        result = self._make(
            {"git_execution_mode": "container"}, str(tmp_path)
        )._query_capabilities("ws-1", str(tmp_path))
        assert result["git"]["mode"] == "containerized"
        assert result["resources"]["git"]["container_id"] == "c1"
        assert result["resources"]["git"]["image"] == "tm-resource-git"

    def test_host_mode_surfaced(self, tmp_path, monkeypatch):
        self._probe(
            monkeypatch,
            {
                "mode": "host_fallback",
                "container_id": None,
                "status": None,
                "image": None,
                "detail": "resource image unavailable",
            },
        )
        result = self._make(
            {"git_execution_mode": "host"}, str(tmp_path)
        )._query_capabilities("ws-1", str(tmp_path))
        assert result["git"]["mode"] == "host_fallback"
        assert result["resources"]["git"]["detail"] == "resource image unavailable"

    def test_unavailable_without_workspace(self, tmp_path, monkeypatch):
        self._probe(
            monkeypatch,
            {"mode": "unavailable", "detail": "unknown resource 'git'"},
        )
        result = self._make({}, None)._query_capabilities(None, None)
        assert result["git"]["mode"] == "unavailable"
        assert result["resources"]["git"]["mode"] == "unavailable"

    def test_probe_exception_keeps_resolver_value(self, tmp_path, monkeypatch):
        self._probe(monkeypatch, RuntimeError("probe exploded"))
        result = self._make(
            {"git_execution_mode": "host"}, str(tmp_path)
        )._query_capabilities("ws-1", str(tmp_path))
        assert result["git"]["mode"] == "host_fallback"
        assert result["resources"]["git"] == {
            "mode": "host_fallback",
            "detail": "probe unavailable",
        }

    def test_probe_unavailable_keeps_resolver_containerized(self, tmp_path, monkeypatch):
        self._probe(
            monkeypatch,
            {"mode": "unavailable", "detail": "resource policy denied"},
        )
        result = self._make(
            {"git_execution_mode": "container"}, str(tmp_path)
        )._query_capabilities("ws-1", str(tmp_path))
        assert result["git"]["mode"] == "containerized"
        assert result["resources"]["git"] == {
            "mode": "unavailable",
            "detail": "resource policy denied",
        }

    def test_no_workspace_id_probe_containerized(self, monkeypatch):
        calls = self._probe(
            monkeypatch,
            {
                "mode": "containerized",
                "container_id": None,
                "status": None,
                "image": "tm-resource-git",
                "detail": "container state unknown (no workspace_id)",
            },
        )
        result = self._make({}, None)._query_capabilities(None, None)
        assert result["git"]["mode"] == "containerized"
        assert (
            result["resources"]["git"]["detail"]
            == "container state unknown (no workspace_id)"
        )
        assert calls and calls[0][1].get("workspace_id") is None

    def test_probe_receives_workspace_id_and_session_permissions(self, tmp_path, monkeypatch):
        calls = self._probe(
            monkeypatch,
            {
                "mode": "containerized",
                "container_id": "c1",
                "status": "running",
                "image": "tm-resource-git",
                "detail": "",
            },
        )
        tool = self._make({"git_execution_mode": "container"}, str(tmp_path))
        object.__setattr__(tool, "session_permissions", {"container": True})
        tool._query_capabilities("ws-9", str(tmp_path))
        resource, kwargs = calls[0]
        assert resource == "git"
        assert kwargs.get("workspace_id") == "ws-9"
        assert kwargs.get("session_permissions") == {"container": True}
