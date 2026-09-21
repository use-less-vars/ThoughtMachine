"""Host-fallback observability for git execution.

Closes the silent-host-fallback gap: Branch 1 of ``GitReadTool._run_git_raw``
(catalog selects host mode / no container workspace) previously returned the
hardened host path with NO log line, so a host fallback was invisible to
operators and diagnostics.

Both host-fallback branches now emit a single structured WARNING carrying four
tokens -- ``reason`` / ``command`` / ``workspace_id`` / ``kill_switch_state`` --
which is the operator/diagnostic surface for host fallbacks; wiring this state
into a route (e.g. the container-status route) is a follow-up commit.

Host fallback is permission-gated (fail-closed) on the workspace top-level
``allow_host_resources`` key, so the autouse fixture below provisions an
allowing workspace config.
"""

import json
import logging
from types import SimpleNamespace

import pytest

from tools.git_info_tool import GitInfoTool


WS = "test-ws"


# ---------------------------------------------------------------------------
# Fakes (mirror tests/security/test_git_execution_mode.py)
# ---------------------------------------------------------------------------
class _FakeSandbox:
    """Stand-in for SandboxedExecution (hardened host path)."""

    instances = []

    def __init__(self, **kwargs):
        _FakeSandbox.instances.append(self)

    def run(self, command, **kwargs):
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")


class _FakeEnsureManager:
    """Resource manager whose ``ensure_resource("git")`` reports a mode."""

    def __init__(self, result):
        self.result = result

    def ensure_resource(self, name):
        return self.result

    def exec(self, command, **kwargs):
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _host_sandbox(monkeypatch):
    """Replace the host backend with a recording fake (never shells out)."""
    _FakeSandbox.instances.clear()
    monkeypatch.setattr("tools.git_info_tool.SandboxedExecution", _FakeSandbox)
    return _FakeSandbox


@pytest.fixture(autouse=True)
def _allow_host_resources(tmp_path, monkeypatch):
    """Grant ``allow_host_resources: true`` for ``WS`` (host path allowed)."""
    vault = tmp_path / "vault"
    cfg_dir = vault / "workspaces" / WS
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(
        json.dumps({"allow_host_resources": True}), encoding="utf-8")
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _host_mode_tool(tmp_path, monkeypatch):
    """Branch 1: catalog selects host mode -> ``_use_container_mode()`` False.

    The legacy ``git_execution_mode`` agent-config key is still supplied, to
    prove it is ignored (popped) rather than honoured.
    """
    monkeypatch.setattr(
        "tools.git_info_tool.catalog_entry",
        lambda name: {"execution_mode": "host"} if name == "git" else {},
    )
    tool = GitInfoTool(
        operation="status",
        session_permissions={"git": "write"},
        effective_permissions={"git": "write"},
        agent_config={"git_execution_mode": "host"},
    )
    object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
    object.__setattr__(tool, "_resolved_workspace_id", WS)
    return tool


def _container_mode_tool(tmp_path):
    """Branch 2: container mode desired, runtime degrades to host_fallback."""
    tool = GitInfoTool(
        operation="status",
        session_permissions={"git": "write"},
        effective_permissions={"git": "write"},
    )
    object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
    object.__setattr__(tool, "_resolved_workspace_id", WS)
    return tool


def _host_fallback_warnings(caplog):
    """Return messages of the structured host-fallback WARNING records."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "kill_switch_state=" in r.getMessage()
    ]


# ---------------------------------------------------------------------------
# Branch 1: config selects host mode (previously SILENT)
# ---------------------------------------------------------------------------
def test_branch1_host_fallback_emits_structured_warning(tmp_path, caplog, monkeypatch):
    tool = _host_mode_tool(tmp_path, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="tools.git_info_tool"):
        tool._run_git_raw(tmp_path, ["status"])

    msgs = _host_fallback_warnings(caplog)
    assert len(msgs) == 1, msgs
    msg = msgs[0]
    assert "reason=container_unavailable" in msg
    assert "command=status" in msg
    assert f"workspace_id={WS}" in msg
    assert "kill_switch_state=on" in msg


def test_branch1_host_fallback_sets_host_mode_state(tmp_path, caplog, monkeypatch):
    """The warning is emitted on the same call that records host_fallback."""
    tool = _host_mode_tool(tmp_path, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="tools.git_info_tool"):
        tool._run_git_raw(tmp_path, ["status"])

    assert tool._last_execution_mode == "host_fallback"
    assert tool._last_failure_reason is None
    assert tool._last_fallback_used is False
    assert "execution_mode: host_fallback" in tool._with_mode("out")
    # The retired agent-config key is popped (migration), not honoured.
    assert "git_execution_mode" not in tool.agent_config
    assert any(
        "Ignoring legacy git_execution_mode" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    )


# ---------------------------------------------------------------------------
# Branch 2: containerized -> degraded to hardened host
# ---------------------------------------------------------------------------
def test_branch2_degraded_warning_carries_same_tokens(tmp_path, caplog):
    tool = _container_mode_tool(tmp_path)
    manager = _FakeEnsureManager(
        {
            "mode": "host_fallback",
            "container_id": None,
            "status": None,
            "image": None,
            "detail": "resource image unavailable",
        }
    )
    object.__setattr__(tool, "_resource_manager", manager)

    with caplog.at_level(logging.WARNING, logger="tools.git_info_tool"):
        tool._run_git_raw(tmp_path, ["status"])

    msgs = _host_fallback_warnings(caplog)
    assert len(msgs) == 1, msgs
    msg = msgs[0]
    assert "reason=container_unavailable" in msg
    assert "command=status" in msg
    assert f"workspace_id={WS}" in msg
    assert "kill_switch_state=on" in msg


# ---------------------------------------------------------------------------
# Kill-switch state (deny reason present <=> host fallback is refused)
# ---------------------------------------------------------------------------
def test_kill_switch_state_on_when_allowed(tmp_path, monkeypatch):
    tool = _host_mode_tool(tmp_path, monkeypatch)
    assert tool._host_execution_denied_reason() is None
    assert tool._kill_switch_state() == "on"


def test_kill_switch_state_off_when_denied(tmp_path, monkeypatch):
    # A vault with no allow_host_resources config for WS -> fail-closed deny.
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path / "empty-vault"))
    tool = _host_mode_tool(tmp_path, monkeypatch)
    assert tool._host_execution_denied_reason() is not None
    assert tool._kill_switch_state() == "off"
