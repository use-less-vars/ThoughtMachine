# tests/test_host_bash_tool.py
"""Tests for the supervised host_bash tool.

All execution is mocked — ``subprocess`` is patched so no real host shell
command ever runs.  Vault feature-flag fixtures (``workspaces/<id>/config.json``
and ``sessions/<id>/config.json``) are written under pytest's ``tmp_path`` and
``THOUGHTMACHINE_VAULT_ROOT`` points at them, so the real vault is never
touched.  The audit log (JSONL) is written under ``tmp_path`` too.
"""

import json
import os
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest import mock

from tools.host_bash_tool import HostBashTool


def make_tool(
    tmp_path,
    monkeypatch,
    *,
    ws_allow=True,
    session_allow=True,
    agent_allow=True,
    grain="allow",
    command="echo hello",
    session_id="sess1",
    ws_id="ws1",
    create_workspace_cfg=True,
    create_session_cfg=True,
    audit_log_path=None,
    log_dir=None,
):
    """Build a HostBashTool wired to tmp_path vault fixtures + audit log."""
    vault = tmp_path / "vault"
    if create_workspace_cfg:
        ws_cfg = vault / "workspaces" / ws_id
        ws_cfg.mkdir(parents=True, exist_ok=True)
        (ws_cfg / "config.json").write_text(
            json.dumps({"allow_host_resources": ws_allow}), encoding="utf-8"
        )
    if create_session_cfg:
        s_cfg = vault / "sessions" / session_id
        s_cfg.mkdir(parents=True, exist_ok=True)
        (s_cfg / "config.json").write_text(
            json.dumps({"allow_host_resources": session_allow}), encoding="utf-8"
        )
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))
    agent_config = {
        "allow_host_resources": agent_allow,
        "workspace_id": ws_id,
        "log_dir": log_dir or str(tmp_path / "audit"),
    }
    effective = {"host_bash": grain} if grain is not None else {}
    return HostBashTool(
        command=command,
        effective_permissions=effective,
        session_permissions=None,
        agent_config=agent_config,
        session_id=session_id,
        workspace_path=None,
        audit_log_path=audit_log_path,
    )


def read_audit(tmp_path, audit_log_path=None):
    path = audit_log_path or (tmp_path / "audit" / "host_bash_audit.jsonl")
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.strip().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Feature-flag gates (vault config)
# ---------------------------------------------------------------------------

def test_host_bash_both_flags_true_allow(tmp_path, monkeypatch):
    """Workspace + session vault flags true (and agent flag) allow execution."""
    with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
        mock_subprocess.run.return_value = SimpleNamespace(returncode=0, stdout="hello\n", stderr="")
        tool = make_tool(tmp_path, monkeypatch)
        result = json.loads(tool.execute())
    assert result["success"] is True
    assert result["outcome"] == "executed"
    assert result["stdout"] == "hello\n"
    mock_subprocess.run.assert_called_once()
    records = read_audit(tmp_path)
    assert len(records) == 1
    assert records[0]["outcome"] == "allow"
    assert records[0]["reason"] == ""
    assert records[0]["command"] == "echo hello"


def test_host_bash_denied_when_workspace_flag_false(tmp_path, monkeypatch):
    """Workspace vault flag false denies even with allow grain; no execution."""
    with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
        tool = make_tool(tmp_path, monkeypatch, ws_allow=False)
        result = json.loads(tool.execute())
    assert result["success"] is False
    assert result["outcome"] == "denied"
    assert "allow_host_resources is false" in result["error"]
    assert "workspaces/ws1/config.json" in result["error"]
    assert "sessions/sess1/config.json" not in result["error"]
    mock_subprocess.run.assert_not_called()
    records = read_audit(tmp_path)
    assert records[0]["outcome"] == "deny"
    assert "workspaces/ws1/config.json" in records[0]["reason"]
    assert records[0]["command"] == "echo hello"


def test_host_bash_denied_when_workspace_config_missing(tmp_path, monkeypatch):
    """Missing workspace vault config fails closed."""
    with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
        tool = make_tool(tmp_path, monkeypatch, create_workspace_cfg=False)
        result = json.loads(tool.execute())
    assert result["success"] is False
    assert result["outcome"] == "denied"
    assert "allow_host_resources is false" in result["error"]
    assert "workspaces/ws1/config.json" in result["error"]
    mock_subprocess.run.assert_not_called()


def test_host_bash_denied_when_session_flag_false(tmp_path, monkeypatch):
    """Session vault flag false (agent flag off too) denies."""
    with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
        tool = make_tool(tmp_path, monkeypatch, session_allow=False, agent_allow=False)
        result = json.loads(tool.execute())
    assert result["success"] is False
    assert result["outcome"] == "denied"
    assert "allow_host_resources is false" in result["error"]
    assert "sessions/sess1/config.json" in result["error"]
    mock_subprocess.run.assert_not_called()
    records = read_audit(tmp_path)
    assert records[0]["outcome"] == "deny"
    assert "sessions/sess1/config.json" in records[0]["reason"]


def test_host_bash_denied_when_session_config_missing(tmp_path, monkeypatch):
    """Missing session vault config + agent flag off denies."""
    with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
        tool = make_tool(tmp_path, monkeypatch, create_session_cfg=False, agent_allow=False)
        result = json.loads(tool.execute())
    assert result["success"] is False
    assert result["outcome"] == "denied"
    assert "allow_host_resources is false" in result["error"]
    assert "sessions/sess1/config.json" in result["error"]
    mock_subprocess.run.assert_not_called()


def test_host_bash_agent_flag_opens_session_leg(tmp_path, monkeypatch):
    """Legacy injected agent_config flag opens the session leg (vault session false)."""
    with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
        mock_subprocess.run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        tool = make_tool(tmp_path, monkeypatch, session_allow=False, agent_allow=True)
        result = json.loads(tool.execute())
    assert result["success"] is True
    assert result["outcome"] == "executed"
    mock_subprocess.run.assert_called_once()


def test_host_bash_denied_when_both_flags_false(tmp_path, monkeypatch):
    """Both legs off -> deny message names both vault config paths."""
    with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
        tool = make_tool(tmp_path, monkeypatch, ws_allow=False, session_allow=False, agent_allow=False)
        result = json.loads(tool.execute())
    assert result["success"] is False
    assert result["outcome"] == "denied"
    assert "allow_host_resources is false" in result["error"]
    assert "workspaces/ws1/config.json" in result["error"]
    assert "sessions/sess1/config.json" in result["error"]
    mock_subprocess.run.assert_not_called()
    records = read_audit(tmp_path)
    assert records[0]["outcome"] == "deny"
    assert "workspaces/ws1/config.json" in records[0]["reason"]
    assert "sessions/sess1/config.json" in records[0]["reason"]


def test_host_bash_denied_when_permission_banned(tmp_path, monkeypatch):
    """A banned (or missing) host_bash grain denies even with all flags on."""
    for grain in ("banned", None):
        tool = make_tool(tmp_path, monkeypatch, grain=grain)
        result = json.loads(tool.execute())
        assert result["success"] is False
        assert result["outcome"] == "denied"
        assert "not allowed" in result["error"]
        assert result["permission_level"] == grain
    records = read_audit(tmp_path)
    assert len(records) == 2
    assert all(r["outcome"] == "deny" for r in records)
    assert all("not allowed" in r["reason"] for r in records)


# ---------------------------------------------------------------------------
# Approval flow (ask grain)
# ---------------------------------------------------------------------------

def test_host_bash_ask_requires_approval(tmp_path, monkeypatch):
    """ask grain requires approval; a denied decision blocks execution."""
    with mock.patch.object(HostBashTool, "_request_approval", return_value="denied"):
        with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
            tool = make_tool(tmp_path, monkeypatch, grain="ask")
            result = json.loads(tool.execute())
    assert result["success"] is False
    assert result["outcome"] == "denied"
    assert "rejected by user" in result["error"]
    mock_subprocess.run.assert_not_called()
    records = read_audit(tmp_path)
    assert records[0]["outcome"] == "deny"
    assert records[0]["reason"] == "host_bash: command rejected by user"


def test_host_bash_approval_timeout_denies(tmp_path, monkeypatch):
    """An unanswered approval request times out into a denial."""
    with mock.patch.object(HostBashTool, "_request_approval", return_value="timeout"):
        with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
            tool = make_tool(tmp_path, monkeypatch, grain="ask")
            result = json.loads(tool.execute())
    assert result["success"] is False
    assert result["outcome"] == "approval_timeout"
    assert "timed out" in result["error"]
    mock_subprocess.run.assert_not_called()
    records = read_audit(tmp_path)
    assert records[0]["outcome"] == "deny"
    assert records[0]["reason"] == "host_bash: security approval timed out"


def test_host_bash_allow_executes_without_approval(tmp_path, monkeypatch):
    """allow grain executes with no approval round-trip."""
    with mock.patch.object(
        HostBashTool, "_request_approval", side_effect=AssertionError("approval must not be requested in allow mode")
    ):
        with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = SimpleNamespace(returncode=0, stdout="hello\n", stderr="")
            tool = make_tool(tmp_path, monkeypatch, grain="allow")
            result = json.loads(tool.execute())
    assert result["success"] is True
    assert result["outcome"] == "executed"
    assert result["exit_code"] == 0
    assert result["stdout"] == "hello\n"
    assert result["permission_level"] == "allow"
    mock_subprocess.run.assert_called_once()
    assert mock_subprocess.run.call_args.kwargs["shell"] is True


def test_host_bash_ask_approval_via_resolve(tmp_path, monkeypatch):
    """Real approval flow: event published on the bus, resolved via resolve_security_prompt."""
    from agent.events import EventType, global_event_bus
    from thoughtmachine.security import resolve_security_prompt

    captured = []

    def capture(ev):
        if getattr(ev, "type", None) == EventType.SECURITY_PROMPT and ev.data.get("tool_name") == "host_bash":
            captured.append(ev)

    global_event_bus.subscribe(None, capture)
    try:
        with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = SimpleNamespace(returncode=0, stdout="hello\n", stderr="")
            tool = make_tool(tmp_path, monkeypatch, grain="ask", command="echo hello")
            results = []

            def worker():
                results.append(tool.execute())

            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            deadline = time.time() + 5
            while time.time() < deadline and not captured:
                time.sleep(0.02)
            assert captured, "host_bash SecurityPromptEvent was not published on the global event bus"
            event = captured[0]
            assert event.data["capabilities"] == ["host_bash:execute"]
            assert event.data["arguments"] == {"command": "echo hello"}
            assert event.data["session_id"] == "sess1"
            resolve_security_prompt(event.data["request_id"], True, False)
            thread.join(timeout=5)
            assert not thread.is_alive()
            result = json.loads(results[0])
            assert result["success"] is True
            assert result["outcome"] == "executed"
            mock_subprocess.run.assert_called_once()
            records = read_audit(tmp_path)
            assert records[0]["outcome"] == "allow"
    finally:
        global_event_bus.unsubscribe(capture)


# ---------------------------------------------------------------------------
# Audit log format & robustness
# ---------------------------------------------------------------------------

def test_host_bash_audit_jsonl_exact_fields(tmp_path, monkeypatch):
    """Executed commands write one JSONL record with exactly the 6 fields."""
    with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
        mock_subprocess.run.return_value = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
        tool = make_tool(tmp_path, monkeypatch)
        json.loads(tool.execute())
    records = read_audit(tmp_path)
    assert len(records) == 1
    record = records[0]
    assert set(record.keys()) == {"timestamp", "workspace_id", "session_id", "command", "outcome", "reason"}
    assert record["workspace_id"] == "ws1"
    assert record["session_id"] == "sess1"
    assert record["command"] == "echo hello"
    assert record["outcome"] == "allow"
    assert record["reason"] == ""


def test_host_bash_audit_log_path_injection(tmp_path, monkeypatch):
    """Explicit audit_log_path overrides the default log dir."""
    custom = tmp_path / "custom" / "audit.jsonl"
    with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
        mock_subprocess.run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        tool = make_tool(tmp_path, monkeypatch, audit_log_path=str(custom))
        json.loads(tool.execute())
    records = read_audit(tmp_path, audit_log_path=custom)
    assert len(records) == 1
    assert records[0]["outcome"] == "allow"
    # default location must stay untouched
    assert not (tmp_path / "audit" / "host_bash_audit.jsonl").exists()


def test_host_bash_redacts_secrets_in_audit(tmp_path, monkeypatch):
    """Environment values embedded in the command are redacted from the audit record."""
    monkeypatch.setenv("HOST_BASH_TEST_SECRET", "supersecretvalue123")
    with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
        mock_subprocess.run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        tool = make_tool(tmp_path, monkeypatch, command="echo supersecretvalue123")
        result = json.loads(tool.execute())
    assert result["success"] is True
    records = read_audit(tmp_path)
    assert "supersecretvalue123" not in records[0]["command"]
    assert "<redacted>" in records[0]["command"]


def test_host_bash_audit_error_never_crashes(tmp_path, monkeypatch):
    """An unwritable audit location must never break command execution."""
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("i am a file", encoding="utf-8")
    with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
        mock_subprocess.run.return_value = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
        tool = make_tool(tmp_path, monkeypatch, log_dir=str(blocker))
        result = json.loads(tool.execute())
    assert result["success"] is True
    assert result["outcome"] == "executed"


def test_host_bash_empty_command_denies(tmp_path, monkeypatch):
    """Empty/whitespace command denies with an audit entry."""
    with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
        tool = make_tool(tmp_path, monkeypatch, command="   ")
        result = json.loads(tool.execute())
    assert result["success"] is False
    assert result["outcome"] == "error"
    assert "empty command" in result["error"]
    mock_subprocess.run.assert_not_called()
    records = read_audit(tmp_path)
    assert records[0]["outcome"] == "deny"
    assert records[0]["reason"] == "empty command"


def test_host_bash_subprocess_timeout_audits_timeout(tmp_path, monkeypatch):
    """A subprocess timeout is audited as outcome 'timeout' with the elapsed budget."""
    with mock.patch(
        "tools.host_bash_tool.subprocess.run",
        side_effect=subprocess.TimeoutExpired("echo hi", 7),
    ):
        tool = make_tool(tmp_path, monkeypatch)
        result = json.loads(tool.execute())
    assert result["success"] is False
    assert result["outcome"] == "error"
    # the tool reports the configured budget (default 120s), not the exception's own timeout
    assert "timed out after 120s" in result["error"]
    records = read_audit(tmp_path)
    assert records[0]["outcome"] == "timeout"
    assert records[0]["reason"] == "subprocess timeout after 120s"
