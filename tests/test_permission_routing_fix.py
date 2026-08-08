"""Permission-routing fix: 'ask' levels must defer to the outer gate.

The ToolExecutor's outer gate (check_required_categories) is the owner of the
interactive ask/prompt flow: when a category is 'ask' and the user approves, the
call proceeds but effective permissions are NOT rewritten (they still read
'ask'). Any in-tool atomic gate that hard-denies 'ask' therefore breaks
approved ask-mode calls.

This suite verifies the routing fix in GitInfoTool and Agent:
- Host path (``_exec_host_raw``): 'ask' git -> required_category stays None
  (SandboxedExecution would treat ASK as denied); 'banned' still denies.
- Container path (``_exec_container_raw``): 'ask' git -> atomic check skipped;
  'banned' still denies (fail closed).
- ``execute()`` network gate: 'ask' network -> deferred; banned/missing stay
  fail-closed (atomic error returned).
- Vault hooks (``_run_vault_hooks``): 'ask' git -> required_category None;
  'banned' still denies.
- ``Agent._apply_pending_config``: when a restart is deferred for a missing API
  key, the session_permissions portion is still applied synchronously.
"""
from types import SimpleNamespace

import pytest

from agent.config.models import AgentConfig
from agent.core.agent import Agent
from thoughtmachine.security import SessionPermissions
from tools.git_info_tool import GitInfoTool


class FakeSandboxExecution:
    """Drop-in replacement for SandboxedExecution that records run() calls.

    Mirrors the real gate's observable contract: a non-None required_category
    raises PermissionError (the real implementation denies any unsatisfied
    category, including 'ASK').
    """

    instances = []

    def __init__(self, *args, **kwargs):
        self.init_kwargs = kwargs
        self.calls = []
        FakeSandboxExecution.instances.append(self)

    def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if kwargs.get("required_category") is not None:
            raise PermissionError(
                f"Permission denied: requires {kwargs['required_category']}, "
                f"but session allows git:read"
            )
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")


class FakeManager:
    """Resource-container stand-in recording exec() invocations."""

    def __init__(self):
        self.calls = []

    def exec(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}


@pytest.fixture
def fake_sandbox(monkeypatch):
    FakeSandboxExecution.instances = []
    monkeypatch.setattr(
        "tools.git_info_tool.SandboxedExecution", FakeSandboxExecution
    )
    return FakeSandboxExecution


def _tool(operation, session_perms=None, effective_perms=None, **kwargs):
    return GitInfoTool(
        operation=operation,
        session_permissions=session_perms,
        effective_permissions=effective_perms,
        **kwargs,
    )


class TestHostPath:
    """_exec_host_raw: ask defers, banned denies (fail closed)."""

    def test_host_ask_commit_defers_gate(self, tmp_path, fake_sandbox):
        tool = _tool("commit", {"git": "ask"}, {"git": "ask"})
        result = tool._exec_host_raw(tmp_path, ["commit", "-m", "x"])
        assert result == (0, "ok", "")
        inst = fake_sandbox.instances[0]
        assert len(inst.calls) == 1
        command, kwargs = inst.calls[0]
        assert command[0] == "git"
        assert "commit" in command
        assert "--no-verify" in command
        # The whole point: 'ask' must NOT hard-deny inside the tool.
        assert kwargs["required_category"] is None

    def test_host_banned_commit_denied(self, tmp_path, fake_sandbox):
        tool = _tool("commit", {"git": "banned"}, {"git": "banned"})
        with pytest.raises(PermissionError):
            tool._exec_host_raw(tmp_path, ["commit", "-m", "x"])
        inst = fake_sandbox.instances[0]
        assert inst.calls[0][1]["required_category"] == "git:write"

    def test_host_ask_read_operation_defers_gate(self, tmp_path, fake_sandbox):
        """Read-level operations defer identically under git=ask."""
        tool = _tool("status", {"git": "ask"}, {"git": "ask"})
        tool._exec_host_raw(tmp_path, ["status"])
        inst = fake_sandbox.instances[0]
        assert inst.calls[0][1]["required_category"] is None


class TestContainerPath:
    """_exec_container_raw: ask defers, banned denies (fail closed)."""

    def _container_tool(self, session_perms, effective_perms, tmp_path, manager):
        tool = _tool("commit", session_perms, effective_perms)
        object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
        object.__setattr__(tool, "_resolved_workspace_id", "test-ws")
        object.__setattr__(tool, "_ensure_resource_container", lambda: manager)
        return tool

    def test_container_ask_commit_defers_gate(self, tmp_path):
        manager = FakeManager()
        tool = self._container_tool(
            {"git": "ask"}, {"git": "ask"}, tmp_path, manager
        )
        result = tool._exec_container_raw(tmp_path, ["commit", "-m", "x"])
        assert result == (0, "ok", "")
        assert len(manager.calls) == 1
        command, _kwargs = manager.calls[0]
        # Container path does NOT inject --no-verify: the resource container
        # is the security boundary, so repo-local hooks run inside it.
        assert command == ["git", "commit", "-m", "x"]

    def test_container_banned_commit_denied(self, tmp_path):
        manager = FakeManager()
        tool = self._container_tool(
            {"git": "banned"}, {"git": "banned"}, tmp_path, manager
        )
        with pytest.raises(PermissionError):
            tool._exec_container_raw(tmp_path, ["commit", "-m", "x"])
        assert manager.calls == []


class TestExecuteNetworkGate:
    """execute(): the network atomic re-check must defer for 'ask'."""

    def test_network_ask_defers_gate(self):
        tool = _tool(
            "remote",
            {"network": "ask", "git": "ask"},
            {"network": "ask", "git": "ask"},
        )
        result = tool.execute()
        assert "Atomic permission check failed" not in result

    def test_network_banned_denied(self):
        tool = _tool(
            "remote",
            {"network": "banned", "git": "read"},
            {"network": "banned", "git": "read"},
        )
        result = tool.execute()
        assert (
            "Atomic permission check failed: network:outbound required for remote"
            in result
        )

    def test_network_missing_fail_closed(self):
        """No effective_permissions: the gate still runs and denies."""
        tool = _tool("remote", {"git": "read"}, None)
        result = tool.execute()
        assert "Atomic permission check failed" in result

    def test_clone_ask_network_defers_gate(self):
        """clone is a network op too; 'ask' must defer before URL validation."""
        tool = _tool(
            "clone",
            {"network": "ask", "git": "ask"},
            {"network": "ask", "git": "ask"},
            clone_url="https://example.com/repo.git",
        )
        result = tool.execute()
        assert "Atomic permission check failed" not in result


class TestVaultHooks:
    """_run_vault_hooks: ask defers, banned denies."""

    def _install_hook(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        hook = tmp_path / ".thoughtmachine" / "hooks" / "test-ws" / "post-commit"
        hook.parent.mkdir(parents=True)
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o755)
        return hook

    def test_vault_hook_ask_defers_gate(
        self, tmp_path, monkeypatch, fake_sandbox
    ):
        self._install_hook(tmp_path, monkeypatch)
        tool = _tool(
            "commit",
            {"git": "ask"},
            {"git": "ask"},
            workspace_id="test-ws",
        )
        tool._run_vault_hooks(tmp_path, "post-commit")  # must not raise
        inst = fake_sandbox.instances[0]
        assert inst.calls[0][1]["required_category"] is None

    def test_vault_hook_banned_denied(
        self, tmp_path, monkeypatch, fake_sandbox
    ):
        self._install_hook(tmp_path, monkeypatch)
        tool = _tool(
            "commit",
            {"git": "banned"},
            {"git": "banned"},
            workspace_id="test-ws",
        )
        with pytest.raises(PermissionError):
            tool._run_vault_hooks(tmp_path, "post-commit")
        inst = fake_sandbox.instances[0]
        assert inst.calls[0][1]["required_category"] == "git:write"


class TestApplyPendingConfigPermissions:
    """_apply_pending_config: permissions apply even when restart is deferred."""

    def test_session_permissions_applied_when_restart_deferred(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

        old = AgentConfig(
            api_key="test-key",
            enable_logging=False,
            session_permissions=SessionPermissions(git="read"),
        )
        agent = Agent(config=old, session_id="test-session")

        new = AgentConfig(
            api_key="",
            model="gpt-4o-turbo",
            enable_logging=False,
            session_permissions=SessionPermissions(git="ask"),
        )
        agent.request_config_update(new)

        result = agent._apply_pending_config()

        # Restart is impossible (no API key) and the update stays pending...
        assert result is False
        assert agent._pending_config is not None
        assert agent.config.model == "deepseek-reasoner"  # not swapped
        # ...but session_permissions are hot-swappable and must land now.
        assert agent.config.session_permissions.git == "ask"
        assert agent.state.config.session_permissions.git == "ask"
        assert agent.tool_executor.config.session_permissions.git == "ask"

    def test_no_sync_when_permissions_unchanged(self, monkeypatch):
        """Identical permissions: no-op branch keeps everything untouched."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

        old = AgentConfig(
            api_key="test-key",
            enable_logging=False,
            session_permissions=SessionPermissions(git="read"),
        )
        agent = Agent(config=old, session_id="test-session")

        new = AgentConfig(
            api_key="",
            model="gpt-4o-turbo",
            enable_logging=False,
            session_permissions=SessionPermissions(git="read"),
        )
        agent.request_config_update(new)

        result = agent._apply_pending_config()
        assert result is False
        assert agent.config.session_permissions.git == "read"
