# tests/test_host_bash_tool.py
"""Tests for the supervised host_bash tool.

All execution is mocked — ``subprocess`` is patched so no real host shell
command ever runs, and the audit log is written under pytest's ``tmp_path``
so the real vault is never touched.
"""

import json
import os
import threading
import time
from types import SimpleNamespace
from unittest import mock

from tools.host_bash_tool import HostBashTool


def make_tool(tmp_path, *, allow=True, grain="allow", command="echo hello", session_id="sess1"):
    """Build a HostBashTool wired to a tmp_path audit log."""
    effective = {"host_bash": grain} if grain is not None else {}
    return HostBashTool(
        command=command,
        effective_permissions=effective,
        session_permissions=None,
        agent_config={"allow_host_resources": allow, "log_dir": str(tmp_path)},
        session_id=session_id,
        workspace_path=None,
    )


def read_audit(tmp_path):
    return (tmp_path / "host_bash_audit.log").read_text(encoding="utf-8")


def test_host_bash_denied_when_switch_false(tmp_path):
    """allow_host_resources=False denies even with an allow grain; no execution."""
    with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
        tool = make_tool(tmp_path, allow=False, grain="allow")
        result = json.loads(tool.execute())
    assert result["success"] is False
    assert result["outcome"] == "denied"
    assert "allow_host_resources is false" in result["error"]
    assert result["permission_level"] == "allow"
    mock_subprocess.run.assert_not_called()
    audit = read_audit(tmp_path)
    assert ", denied" in audit
    assert "echo hello" in audit


def test_host_bash_denied_when_permission_banned(tmp_path):
    """A banned (or missing) host_bash grain denies even with the switch on."""
    for grain in ("banned", None):
        tool = make_tool(tmp_path, allow=True, grain=grain)
        result = json.loads(tool.execute())
        assert result["success"] is False
        assert result["outcome"] == "denied"
        assert "not allowed" in result["error"]
        assert result["permission_level"] == grain
    audit = read_audit(tmp_path)
    assert audit.count(", denied") == 2


def test_host_bash_ask_requires_approval(tmp_path):
    """ask grain requires approval; a denied decision blocks execution."""
    with mock.patch.object(HostBashTool, "_request_approval", return_value="denied"):
        with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
            tool = make_tool(tmp_path, allow=True, grain="ask")
            result = json.loads(tool.execute())
    assert result["success"] is False
    assert result["outcome"] == "denied"
    assert "rejected by user" in result["error"]
    mock_subprocess.run.assert_not_called()
    audit = read_audit(tmp_path)
    assert ", denied" in audit


def test_host_bash_allow_executes_without_approval(tmp_path):
    """allow grain executes with no approval round-trip."""
    with mock.patch.object(
        HostBashTool, "_request_approval", side_effect=AssertionError("approval must not be requested in allow mode")
    ):
        with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = SimpleNamespace(returncode=0, stdout="hello\n", stderr="")
            tool = make_tool(tmp_path, allow=True, grain="allow")
            result = json.loads(tool.execute())
    assert result["success"] is True
    assert result["outcome"] == "executed"
    assert result["exit_code"] == 0
    assert result["stdout"] == "hello\n"
    assert result["permission_level"] == "allow"
    mock_subprocess.run.assert_called_once()
    assert mock_subprocess.run.call_args.kwargs["shell"] is True
    audit = read_audit(tmp_path)
    assert audit.rstrip().endswith(", executed")


def test_host_bash_denied_logs_audit_entry(tmp_path):
    """Denials write a single well-formed CSV audit line."""
    tool = make_tool(tmp_path, allow=True, grain="banned")
    with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
        result = json.loads(tool.execute())
    assert result["success"] is False
    mock_subprocess.run.assert_not_called()
    lines = read_audit(tmp_path).strip().splitlines()
    assert len(lines) == 1
    line = lines[0]
    assert line.count(",") >= 5
    parts = [p.strip() for p in line.split(",")]
    # order: timestamp, command, workspace_id, session_id, permission_level, outcome
    assert parts[1] == "echo hello"
    assert parts[2] == ""
    assert parts[3] == "sess1"
    assert parts[4] == "banned"
    assert parts[5] == "denied"


def test_host_bash_approval_timeout_denies(tmp_path):
    """An unanswered approval request times out into a denial."""
    with mock.patch.object(HostBashTool, "_request_approval", return_value="timeout"):
        with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
            tool = make_tool(tmp_path, allow=True, grain="ask")
            result = json.loads(tool.execute())
    assert result["success"] is False
    assert result["outcome"] == "approval_timeout"
    assert "timed out" in result["error"]
    mock_subprocess.run.assert_not_called()
    audit = read_audit(tmp_path)
    assert ", approval_timeout" in audit


# ---------------------------------------------------------------------------
# Extra robustness coverage
# ---------------------------------------------------------------------------

def test_host_bash_ask_approval_via_resolve(tmp_path):
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
            tool = make_tool(tmp_path, allow=True, grain="ask", command="echo hello")
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
            audit = read_audit(tmp_path)
            assert ", executed" in audit
    finally:
        global_event_bus.unsubscribe(capture)


def test_host_bash_redacts_secrets_in_audit(tmp_path, monkeypatch):
    """Environment values embedded in the command are redacted from the audit line."""
    monkeypatch.setenv("HOST_BASH_TEST_SECRET", "supersecretvalue123")
    with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
        mock_subprocess.run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        tool = make_tool(tmp_path, allow=True, grain="allow", command="echo supersecretvalue123")
        result = json.loads(tool.execute())
    assert result["success"] is True
    audit = read_audit(tmp_path)
    assert "supersecretvalue123" not in audit
    assert "<redacted>" in audit


def test_host_bash_audit_error_never_crashes(tmp_path):
    """An unwritable audit location must never break command execution."""
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("i am a file", encoding="utf-8")
    with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
        mock_subprocess.run.return_value = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
        tool = HostBashTool(
            command="echo ok",
            effective_permissions={"host_bash": "allow"},
            session_permissions=None,
            agent_config={"allow_host_resources": True, "log_dir": str(blocker)},
            session_id="sess1",
            workspace_path=None,
        )
        result = json.loads(tool.execute())
    assert result["success"] is True
    assert result["outcome"] == "executed"
