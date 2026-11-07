# tests/test_host_bash_uses_host_port.py
"""RED-first pin for a migration that has NOT been done yet (host_bash spawn).

The host-execution chokepoint is ``thoughtmachine/host_port.py`` (+
``host_run``), which owns permission resolution, workspace policy and the
host-execution audit trail.  ``tools/host_bash_tool.py`` predates it: it imports
``subprocess`` at module scope and ``HostBashTool.execute()`` runs the command
itself with::

    result = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                            timeout=timeout)

plus its own ``subprocess.TimeoutExpired`` handling.  So the one tool whose
whole job is host execution is the one surface that skips the chokepoint.

Migration (NOT DONE yet): ``execute()`` must run the command through
``host_run(..., grain="host_bash")`` and let the port report the timeout
(``HostRunResult.timed_out``) instead of catching ``subprocess.TimeoutExpired``;
``tools/host_bash_tool.py`` must lose its ``subprocess`` import.  The tool keeps
its own outer gate (workspace ``allow_host_resources`` + the ``ask``/``allow``
grain), its JSON envelope and its six-field audit record.

Tests here FAIL until that lands, except
``test_denials_never_reach_the_host_port``, which is a parity guard that is
already green (it exists so the migration cannot move the gate behind the
spawn).  All execution is faked: no real shell, no network, no real subprocess.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
from types import SimpleNamespace

import pytest

from tools import host_bash_tool
from tools.host_bash_tool import HostBashTool

AUDIT_FIELDS = {"timestamp", "workspace_id", "session_id", "command", "outcome", "reason"}


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


def _host_port():
    """Import the (not yet existing) host port chokepoint, or fail loudly."""
    try:
        return importlib.import_module("thoughtmachine.host_port")
    except ModuleNotFoundError as exc:  # pragma: no cover - RED path today
        pytest.fail(
            "thoughtmachine.host_port does not exist yet: HostBashTool.execute() must "
            "run its command through host_port.host_run(..., grain='host_bash') "
            "instead of subprocess.run (%s)" % exc
        )


def _port_result(**overrides):
    """A ``HostRunResult`` stand-in (the port's documented field set)."""
    result = SimpleNamespace(
        ok=True,
        exit_code=0,
        stdout="hello\n",
        stderr="",
        duration_ms=1.0,
        timed_out=False,
        argv=["/bin/sh", "-c", "echo hello"],
        grain="host_bash",
    )
    for key, value in overrides.items():
        setattr(result, key, value)
    return result


def _install_port_spy(monkeypatch, *, result=None, required=True):
    """Spy on ``host_port.host_run`` (and any module-level alias in the tool).

    ``required=False`` returns ``None`` instead of failing when the port module
    does not exist yet, for the gating parity test that must pass both before
    and after the migration.
    """
    try:
        host_port = importlib.import_module("thoughtmachine.host_port")
    except ModuleNotFoundError:
        if required:
            _host_port()
        return None

    calls = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return result if result is not None else _port_result()

    monkeypatch.setattr(host_port, "host_run", spy)
    monkeypatch.setattr(host_bash_tool, "host_run", spy, raising=False)
    return SimpleNamespace(calls=calls)


def _forbid_spawn(monkeypatch):
    """Record and refuse any direct spawn attempt from tools/host_bash_tool.py."""
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("host_bash must not spawn its own process")

    subprocess_module = getattr(host_bash_tool, "subprocess", None)
    if subprocess_module is not None:
        # Tolerant on purpose: after the migration the module no longer imports
        # subprocess at all (see the AST test below).
        for name in ("run", "Popen"):
            monkeypatch.setattr(subprocess_module, name, forbidden, raising=False)
    return attempts


def _make_tool(
    tmp_path,
    monkeypatch,
    *,
    ws_allow=True,
    grain="allow",
    command="echo hello",
    session_id="sess-hp",
    ws_id="ws-hp",
    is_worker_context=False,
):
    """HostBashTool wired to tmp_path vault fixtures + tmp_path audit log."""
    vault = tmp_path / "vault"
    ws_cfg = vault / "workspaces" / ws_id
    ws_cfg.mkdir(parents=True, exist_ok=True)
    (ws_cfg / "config.json").write_text(
        json.dumps({"allow_host_resources": ws_allow}), encoding="utf-8"
    )
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))
    agent_config = {
        "allow_host_resources": ws_allow,
        "workspace_id": ws_id,
        "log_dir": str(tmp_path / "audit"),
    }
    return HostBashTool(
        command=command,
        effective_permissions={"host_bash": grain} if grain is not None else {},
        session_permissions=None,
        agent_config=agent_config,
        session_id=session_id,
        workspace_path=None,
        audit_log_path=None,
        is_worker_context=is_worker_context,
    )


def _read_audit(tmp_path):
    path = tmp_path / "audit" / "host_bash_audit.jsonl"
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.strip().splitlines() if line.strip()]


def _subprocess_imports(module) -> list:
    """Every ``subprocess`` import found in *module*'s source (comments ignored)."""
    tree = ast.parse(inspect.getsource(module))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(
                alias.name for alias in node.names if alias.name.split(".")[0] == "subprocess"
            )
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "subprocess":
                found.append(node.module or "subprocess")
    return found


# ---------------------------------------------------------------------------
# the migration
# ---------------------------------------------------------------------------


def test_host_bash_tool_module_imports_no_subprocess():
    """The tool must not spawn: host execution belongs to the port."""
    assert _subprocess_imports(host_bash_tool) == []


def test_execute_runs_the_command_through_the_host_port(tmp_path, monkeypatch):
    spy = _install_port_spy(monkeypatch)
    attempts = _forbid_spawn(monkeypatch)
    tool = _make_tool(tmp_path, monkeypatch, command="echo hello")

    result = json.loads(tool.execute())

    assert attempts == []
    assert len(spy.calls) == 1
    args, kwargs = spy.calls[0]
    assert kwargs.get("grain") == "host_bash"
    command = kwargs.get("command", args[0] if args else "")
    assert "echo hello" in str(command)
    # Shell semantics are part of the tool's contract (pipes/redirections).
    assert kwargs.get("shell", True) is True

    assert result["success"] is True
    assert result["outcome"] == "executed"
    assert result["command"] == "echo hello"
    assert result["stdout"] == "hello\n"
    assert result["stderr"] == ""
    assert result["exit_code"] == 0
    assert result["permission_level"] == "allow"

    records = _read_audit(tmp_path)
    assert len(records) == 1
    assert set(records[0]) == AUDIT_FIELDS
    assert records[0]["outcome"] == "allow"
    assert records[0]["reason"] == ""
    assert records[0]["command"] == "echo hello"


def test_execute_reports_a_port_timeout(tmp_path, monkeypatch):
    """A timed-out port result keeps the tool's timeout envelope + audit outcome."""
    _install_port_spy(
        monkeypatch,
        result=_port_result(
            ok=False, exit_code=None, stdout="", stderr="", timed_out=True
        ),
    )
    attempts = _forbid_spawn(monkeypatch)
    tool = _make_tool(tmp_path, monkeypatch)

    result = json.loads(tool.execute())

    assert attempts == []
    assert result["success"] is False
    assert result["outcome"] == "error"
    assert result["exit_code"] is None
    assert "timed out" in result["error"]

    records = _read_audit(tmp_path)
    assert records[-1]["outcome"] == "timeout"


def test_denials_never_reach_the_host_port(tmp_path, monkeypatch):
    """Gating parity guard (green today): a banned grain denies before any spawn."""
    spy = _install_port_spy(monkeypatch, required=False)
    attempts = _forbid_spawn(monkeypatch)
    tool = _make_tool(tmp_path, monkeypatch, grain="banned")

    result = json.loads(tool.execute())

    assert attempts == []
    assert spy is None or spy.calls == []
    assert result["success"] is False
    assert result["outcome"] == "denied"
    assert result["permission_level"] == "banned"
    assert "not allowed" in result["error"]

    records = _read_audit(tmp_path)
    assert len(records) == 1
    assert records[0]["outcome"] == "deny"
    assert "not allowed" in records[0]["reason"]
