# tests/test_host_bash_tool.py
"""Tests for the supervised host_bash tool.

All execution is mocked — ``host_run`` (the shared host port entry point in
``thoughtmachine/host_port.py``) is patched so no real host shell command
ever runs.  Gating is two-layered: the workspace operator policy
``allow_host_resources`` (``workspaces/<id>/config.json``, read in-tool via
``tools.host_resource_policy`` from the ``THOUGHTMACHINE_VAULT_ROOT`` env,
fail-closed when the config is missing) is checked BEFORE the effective
``host_bash`` permission grain (``ask`` / ``allow``) flow.  The session vault
config and the legacy injected ``agent_config`` flag remain unconsulted
fixtures, written purely to prove host_bash ignores them; the real vault is
never touched.  The audit log (JSONL) is written under ``tmp_path`` too.
"""

import json
import os
import threading
import time
from unittest import mock

from thoughtmachine.host_port import HostRunResult
from tools.host_bash_tool import HostBashTool


def port_result(*, exit_code=0, stdout="hello\n", stderr="", timed_out=False):
    """A ``HostRunResult`` as returned by ``host_run`` (the host port)."""
    return HostRunResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=0,
        timed_out=timed_out,
        argv=["bash", "-c", "echo hello"],
        grain="host_bash",
    )


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
    is_worker_context=False,
):
    """Build a HostBashTool wired to tmp_path vault fixtures + audit log.

    The workspace cfg write is read by the tool as an operator gate
    (``tools.host_resource_policy`` resolves it from the
    ``THOUGHTMACHINE_VAULT_ROOT`` env; missing configs fail closed); the
    session cfg and the legacy injected ``agent_config`` flag are
    unconsulted fixtures kept to prove host_bash ignores them.
    """
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
        is_worker_context=is_worker_context,
    )


def read_audit(tmp_path, audit_log_path=None):
    path = audit_log_path or (tmp_path / "audit" / "host_bash_audit.jsonl")
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.strip().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Workspace-policy + permission-grain gating (workspace allow_host_resources
# is the operator gate, read in-tool and checked before the host_bash
# grain/approval flow; session cfg and agent_config flag are never consulted)
# ---------------------------------------------------------------------------

def test_host_bash_grain_allow_executes(tmp_path, monkeypatch):
    """The 'allow' host_bash grain alone permits execution."""
    with mock.patch("tools.host_bash_tool.host_run") as mock_host_run:
        mock_host_run.return_value = port_result(stdout="hello\n")
        tool = make_tool(tmp_path, monkeypatch)
        result = json.loads(tool.execute())
    assert result["success"] is True
    assert result["outcome"] == "executed"
    assert result["stdout"] == "hello\n"
    assert result["permission_level"] == "allow"
    mock_host_run.assert_called_once()
    records = read_audit(tmp_path)
    assert len(records) == 1
    assert records[0]["outcome"] == "allow"
    assert records[0]["reason"] == ""
    assert records[0]["command"] == "echo hello"


def test_host_bash_disabled_when_workspace_denies_host_resources(tmp_path, monkeypatch):
    """Workspace allow_host_resources=false is an operator gate: denies even an 'allow' grain."""
    msg = "Host execution is disabled for this workspace (allow_host_resources=false)"
    with mock.patch("tools.host_bash_tool.host_run") as mock_host_run:
        mock_host_run.return_value = port_result(stdout="ok\n")
        tool = make_tool(tmp_path, monkeypatch, ws_allow=False, grain="allow")
        result = json.loads(tool.execute())
    assert result["success"] is False
    assert result["outcome"] == "denied"
    assert result["permission_level"] == "allow"
    assert msg in result["error"]
    mock_host_run.assert_not_called()
    records = read_audit(tmp_path)
    assert len(records) == 1
    assert records[0]["outcome"] == "deny"
    assert msg in records[0]["reason"]


def test_host_bash_fails_closed_when_workspace_config_missing(tmp_path, monkeypatch):
    """A missing workspace config fails closed: no config means no host resources."""
    msg = "Host execution is disabled for this workspace (allow_host_resources=false)"
    with mock.patch("tools.host_bash_tool.host_run") as mock_host_run:
        mock_host_run.return_value = port_result(stdout="ok\n")
        tool = make_tool(tmp_path, monkeypatch, create_workspace_cfg=False, grain="allow")
        result = json.loads(tool.execute())
    assert result["success"] is False
    assert result["outcome"] == "denied"
    assert msg in result["error"]
    mock_host_run.assert_not_called()
    records = read_audit(tmp_path)
    assert records[0]["outcome"] == "deny"
    assert msg in records[0]["reason"]


def test_host_bash_workspace_flag_wins_over_ask_never_prompts(tmp_path, monkeypatch):
    """Workspace deny short-circuits before the ask grain: no approval request is made."""
    msg = "Host execution is disabled for this workspace (allow_host_resources=false)"
    with mock.patch.object(
        HostBashTool, "_request_approval", side_effect=AssertionError("must not prompt")
    ):
        with mock.patch("tools.host_bash_tool.host_run") as mock_host_run:
            mock_host_run.return_value = port_result(stdout="ok\n")
            tool = make_tool(tmp_path, monkeypatch, ws_allow=False, grain="ask")
            result = json.loads(tool.execute())
    assert result["success"] is False
    assert result["outcome"] == "denied"
    assert result["permission_level"] == "ask"
    assert msg in result["error"]
    mock_host_run.assert_not_called()
    records = read_audit(tmp_path)
    assert records[0]["outcome"] == "deny"
    assert msg in records[0]["reason"]


def test_host_bash_workspace_flag_wins_over_banned_grain(tmp_path, monkeypatch):
    """Workspace deny dominates banned/missing grains; the grain still labels the denial."""
    msg = "Host execution is disabled for this workspace (allow_host_resources=false)"
    for grain in ("banned", None):
        with mock.patch("tools.host_bash_tool.host_run") as mock_host_run:
            tool = make_tool(tmp_path, monkeypatch, ws_allow=False, grain=grain)
            result = json.loads(tool.execute())
        assert result["success"] is False
        assert result["outcome"] == "denied"
        assert result["permission_level"] == (grain or "banned")
        assert msg in result["error"]
        mock_host_run.assert_not_called()
    records = read_audit(tmp_path)
    assert len(records) == 2
    assert all(r["outcome"] == "deny" for r in records)
    assert all(msg in r["reason"] for r in records)


def test_host_bash_agent_config_flag_irrelevant(tmp_path, monkeypatch):
    """The legacy injected agent_config allow_host_resources flag is never consulted."""
    with mock.patch("tools.host_bash_tool.host_run") as mock_host_run:
        mock_host_run.return_value = port_result(stdout="ok\n")
        tool = make_tool(tmp_path, monkeypatch, agent_allow=False)
        result = json.loads(tool.execute())
    assert result["success"] is True
    assert result["outcome"] == "executed"
    assert "allow_host_resources" not in result.get("error", "")
    mock_host_run.assert_called_once()
    records = read_audit(tmp_path)
    assert records[0]["outcome"] == "allow"


def test_host_bash_denied_when_permission_banned(tmp_path, monkeypatch):
    """A banned (or missing) host_bash grain denies even with vault flags true."""
    for grain in ("banned", None):
        tool = make_tool(tmp_path, monkeypatch, grain=grain)
        result = json.loads(tool.execute())
        assert result["success"] is False
        assert result["outcome"] == "denied"
        assert "not allowed (requires ask or allow)" in result["error"]
        assert "allow_host_resources" not in result["error"]
        assert result["permission_level"] == (grain or "banned")
    records = read_audit(tmp_path)
    assert len(records) == 2
    assert all(r["outcome"] == "deny" for r in records)
    assert all("not allowed (requires ask or allow)" in r["reason"] for r in records)


# ---------------------------------------------------------------------------
# Approval flow (ask grain)
# ---------------------------------------------------------------------------

def test_host_bash_ask_requires_approval(tmp_path, monkeypatch):
    """ask grain requires approval; a denied decision blocks execution."""
    with mock.patch.object(HostBashTool, "_request_approval", return_value="denied"):
        with mock.patch("tools.host_bash_tool.host_run") as mock_host_run:
            tool = make_tool(tmp_path, monkeypatch, grain="ask")
            result = json.loads(tool.execute())
    assert result["success"] is False
    assert result["outcome"] == "denied"
    assert "rejected by user" in result["error"]
    mock_host_run.assert_not_called()
    records = read_audit(tmp_path)
    assert records[0]["outcome"] == "deny"
    assert records[0]["reason"] == "host_bash: command rejected by user"


def test_host_bash_approval_timeout_denies(tmp_path, monkeypatch):
    """An unanswered approval request times out into a denial."""
    with mock.patch.object(HostBashTool, "_request_approval", return_value="timeout"):
        with mock.patch("tools.host_bash_tool.host_run") as mock_host_run:
            tool = make_tool(tmp_path, monkeypatch, grain="ask")
            result = json.loads(tool.execute())
    assert result["success"] is False
    assert result["outcome"] == "approval_timeout"
    assert "timed out" in result["error"]
    mock_host_run.assert_not_called()
    records = read_audit(tmp_path)
    assert records[0]["outcome"] == "deny"
    assert records[0]["reason"] == "host_bash: security approval timed out"


def test_host_bash_allow_executes_without_approval(tmp_path, monkeypatch):
    """allow grain executes with no approval round-trip."""
    with mock.patch.object(
        HostBashTool, "_request_approval", side_effect=AssertionError("approval must not be requested in allow mode")
    ):
        with mock.patch("tools.host_bash_tool.host_run") as mock_host_run:
            mock_host_run.return_value = port_result(stdout="hello\n")
            tool = make_tool(tmp_path, monkeypatch, grain="allow")
            result = json.loads(tool.execute())
    assert result["success"] is True
    assert result["outcome"] == "executed"
    assert result["exit_code"] == 0
    assert result["stdout"] == "hello\n"
    assert result["permission_level"] == "allow"
    mock_host_run.assert_called_once()
    assert mock_host_run.call_args.kwargs["shell"] is True


def test_host_bash_worker_context_ask_denied_without_approval(tmp_path, monkeypatch):
    """Worker context + ask grain: denied in-tool, no approval prompt, no subprocess."""
    msg = "host_bash: ask requires interactive approval; not available in worker context"
    with mock.patch.object(
        HostBashTool,
        "_request_approval",
        side_effect=AssertionError("approval must not be requested in worker context"),
    ):
        with mock.patch("tools.host_bash_tool.host_run") as mock_host_run:
            tool = make_tool(tmp_path, monkeypatch, grain="ask", is_worker_context=True)
            result = json.loads(tool.execute())
    assert result["success"] is False
    assert result["outcome"] == "denied"
    assert result["permission_level"] == "ask"
    assert msg in result["error"]
    mock_host_run.assert_not_called()
    records = read_audit(tmp_path)
    assert len(records) == 1
    assert records[0]["outcome"] == "deny"
    assert records[0]["reason"] == msg


def test_host_bash_worker_context_allow_still_executes(tmp_path, monkeypatch):
    """Worker context does not restrict an 'allow' grain: execution proceeds."""
    with mock.patch.object(
        HostBashTool,
        "_request_approval",
        side_effect=AssertionError("approval must not be requested in allow mode"),
    ):
        with mock.patch("tools.host_bash_tool.host_run") as mock_host_run:
            mock_host_run.return_value = port_result(stdout="ok\n")
            tool = make_tool(tmp_path, monkeypatch, grain="allow", is_worker_context=True)
            result = json.loads(tool.execute())
    assert result["success"] is True
    assert result["outcome"] == "executed"
    assert result["permission_level"] == "allow"
    mock_host_run.assert_called_once()


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
        with mock.patch("tools.host_bash_tool.host_run") as mock_host_run:
            mock_host_run.return_value = port_result(stdout="hello\n")
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
            mock_host_run.assert_called_once()
            records = read_audit(tmp_path)
            assert records[0]["outcome"] == "allow"
    finally:
        global_event_bus.unsubscribe(capture)


# ---------------------------------------------------------------------------
# Audit log format & robustness
# ---------------------------------------------------------------------------

def test_host_bash_audit_jsonl_exact_fields(tmp_path, monkeypatch):
    """Executed commands write one JSONL record with exactly the 6 fields."""
    with mock.patch("tools.host_bash_tool.host_run") as mock_host_run:
        mock_host_run.return_value = port_result(stdout="ok\n")
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
    with mock.patch("tools.host_bash_tool.host_run") as mock_host_run:
        mock_host_run.return_value = port_result(stdout="")
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
    with mock.patch("tools.host_bash_tool.host_run") as mock_host_run:
        mock_host_run.return_value = port_result(stdout="")
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
    with mock.patch("tools.host_bash_tool.host_run") as mock_host_run:
        mock_host_run.return_value = port_result(stdout="ok\n")
        tool = make_tool(tmp_path, monkeypatch, log_dir=str(blocker))
        result = json.loads(tool.execute())
    assert result["success"] is True
    assert result["outcome"] == "executed"


def test_host_bash_empty_command_denies(tmp_path, monkeypatch):
    """Empty/whitespace command denies with an audit entry."""
    with mock.patch("tools.host_bash_tool.host_run") as mock_host_run:
        tool = make_tool(tmp_path, monkeypatch, command="   ")
        result = json.loads(tool.execute())
    assert result["success"] is False
    assert result["outcome"] == "error"
    assert "empty command" in result["error"]
    mock_host_run.assert_not_called()
    records = read_audit(tmp_path)
    assert records[0]["outcome"] == "deny"
    assert records[0]["reason"] == "empty command"


def test_host_bash_subprocess_timeout_audits_timeout(tmp_path, monkeypatch):
    """A port-reported timeout is audited as outcome 'timeout' with the elapsed budget.

    The host port signals a timeout by returning a result with
    ``timed_out=True`` and ``exit_code=-9`` instead of raising; the tool
    normalises that back to ``exit_code: None`` in its envelope.
    """
    with mock.patch("tools.host_bash_tool.host_run") as mock_host_run:
        mock_host_run.return_value = port_result(exit_code=-9, stdout="", stderr="", timed_out=True)
        tool = make_tool(tmp_path, monkeypatch)
        result = json.loads(tool.execute())
    assert result["success"] is False
    assert result["outcome"] == "error"
    # the tool reports the configured budget (default 120s), not the port's exit code
    assert "timed out after 120s" in result["error"]
    assert result["exit_code"] is None
    records = read_audit(tmp_path)
    assert records[0]["outcome"] == "timeout"
    assert records[0]["reason"] == "subprocess timeout after 120s"


# ---------------------------------------------------------------------------
# Effective-grain source (disk-authoritative effective_permissions only)
# ---------------------------------------------------------------------------

def test_host_bash_effective_grain_ignores_session_mirror(tmp_path):
    """The grain is read ONLY from effective_permissions, never the session mirror.

    The session-start mirror in ``session_permissions`` is never consulted,
    so a stale session ``host_bash: allow`` mirror cannot re-open host
    execution when the effective grain is absent/banned.  The declared outer
    gate category is the value form ``["host_bash:allow"]`` (a bare category is
    skipped by the gate, hence the value form).
    """
    audit = tmp_path / "audit" / "host_bash_audit.jsonl"

    def build(effective_permissions, session_permissions):
        return HostBashTool(
            command="echo hi",
            effective_permissions=effective_permissions,
            session_permissions=session_permissions,
            audit_log_path=str(audit),
        )

    # Stale session mirror says allow, but effective_permissions is absent
    # (None) -> fail closed to banned, and the host port is never reached.
    with mock.patch("tools.host_bash_tool.host_run") as mock_host_run:
        result_none = json.loads(build(None, {"host_bash": "allow"}).execute())
    assert result_none["success"] is False
    assert result_none["outcome"] == "denied"
    assert result_none["permission_level"] == "banned"
    assert "not allowed (requires ask or allow)" in result_none["error"]
    mock_host_run.assert_not_called()

    # Empty effective_permissions behaves like absent -> banned.
    with mock.patch("tools.host_bash_tool.host_run") as mock_host_run:
        result_empty = json.loads(build({}, {"host_bash": "allow"}).execute())
    assert result_empty["success"] is False
    assert result_empty["outcome"] == "denied"
    assert result_empty["permission_level"] == "banned"
    assert "not allowed (requires ask or allow)" in result_empty["error"]
    mock_host_run.assert_not_called()

    # The effective grain is the sole authority: allow survives even with a
    # None session mirror.
    with mock.patch("tools.host_bash_tool.host_run") as mock_host_run:
        mock_host_run.return_value = port_result()
        result_allow = json.loads(build({"host_bash": "allow"}, None).execute())
    assert result_allow["success"] is True
    assert result_allow["outcome"] == "executed"
    assert result_allow["permission_level"] == "allow"
    mock_host_run.assert_called_once()

    assert HostBashTool.required_categories == ["host_bash:allow"]


def test_host_bash_required_categories_use_value_form():
    """The declared gate category must carry a ``:value`` -- a bare category is skipped by the gate."""
    reqs = HostBashTool.required_categories
    assert reqs == ["host_bash:allow"]
    assert all(":" in c for c in reqs)

# ---------------------------------------------------------------------------
# Port-owned execution surface (pinned by the host_bash -> host_port migration)
# ---------------------------------------------------------------------------

def test_host_bash_never_passes_a_cwd_to_the_host_port(tmp_path, monkeypatch):
    """The tool must not choose a working directory: the host port owns cwd.

    ``host_run`` resolves cwd from the workspace ``root`` in
    ``workspaces/<id>/config.json`` (refusing a missing or escaping root), so
    the tool must never smuggle a ``cwd=`` kwarg into the port call.
    """
    with mock.patch("tools.host_bash_tool.host_run") as mock_host_run:
        mock_host_run.return_value = port_result(stdout="hello\n")
        tool = make_tool(tmp_path, monkeypatch)
        result = json.loads(tool.execute())
    assert result["outcome"] == "executed"
    mock_host_run.assert_called_once()
    # The port resolves cwd from the workspace root; dropping the old
    # inherited-process-cwd behaviour is deliberate, so no cwd kwarg may appear.
    assert "cwd" not in mock_host_run.call_args.kwargs


def test_host_bash_identityless_call_is_refused_by_the_port(tmp_path, monkeypatch):
    """An identity-less call fails closed: the port refuses, the tool denies.

    The ids handed to the port are the tool's real state -- an empty workspace
    id and a ``None`` session id normalised to ``""`` -- never fabricated or
    replaced with a sentinel, so the port's identity gate sees the truth and
    raises ``PermissionError``.  The tool must map that to a denied envelope
    plus a ``deny`` audit record, not a success envelope and not a crash.
    """
    refusal = "workspace_id must be a non-empty workspace id string"
    with mock.patch("tools.host_bash_tool.host_run") as mock_host_run:
        mock_host_run.side_effect = PermissionError(refusal)
        tool = make_tool(
            tmp_path,
            monkeypatch,
            ws_id="",
            session_id=None,
            create_workspace_cfg=False,
            create_session_cfg=False,
            audit_log_path=str(tmp_path / "audit" / "host_bash_audit.jsonl"),
        )
        result = json.loads(tool.execute())
    mock_host_run.assert_called_once()
    kwargs = mock_host_run.call_args.kwargs
    assert kwargs["workspace_id"] == ""
    assert kwargs["session_id"] == ""
    assert result["success"] is False
    assert result["outcome"] == "denied"
    assert result["permission_level"] == "allow"
    assert "host port refused execution" in result["error"]
    assert refusal in result["error"]
    records = read_audit(tmp_path)
    assert records[-1]["outcome"] == "deny"
    assert "host port refused execution" in records[-1]["reason"]

