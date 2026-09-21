"""Host-fallback EVENT LOG for git execution.

Companion to ``tests/security/test_git_host_fallback_warning.py``: the two
host-fallback branches of ``GitReadTool._run_git_raw`` already emit a single
structured WARNING; this commit additionally PERSISTS each fallback as one
append-only JSON line so a host fallback is a durable, queryable event (the
container-status route surfaces the latest one).

Path: ``<vault>/workspaces/<ws>/resources/git.host_execution.jsonl``.

Invariants proven here: exactly one event per fallback, structured shape,
branch #2 carries the resolver degradation detail, the host-denied path writes
nothing, events are append-only, an unresolvable workspace id is a no-op, and a
write failure can never raise (R3: swallowed but never silent).
"""

import json
import logging
from types import SimpleNamespace

import pytest

from tools.git_info_tool import GitInfoTool


WS = "test-ws"


# ---------------------------------------------------------------------------
# Fakes (mirror tests/security/test_git_host_fallback_warning.py)
# ---------------------------------------------------------------------------
class _FakeSandbox:
    """Stand-in for SandboxedExecution (hardened host path)."""

    def __init__(self, **kwargs):
        pass

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
    """Replace the host backend with a fake (never shells out)."""
    monkeypatch.setattr("tools.git_info_tool.SandboxedExecution", _FakeSandbox)


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
def _event_log(vault):
    return vault / "workspaces" / WS / "resources" / "git.host_execution.jsonl"


def _events(vault):
    path = _event_log(vault)
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


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


def _host_fallback_manager(detail="resource image unavailable"):
    return _FakeEnsureManager(
        {
            "mode": "host_fallback",
            "container_id": None,
            "status": None,
            "image": None,
            "detail": detail,
        }
    )


# ---------------------------------------------------------------------------
# Branch 1: config selects host mode
# ---------------------------------------------------------------------------
def test_branch1_appends_host_execution_event(tmp_path, monkeypatch):
    tool = _host_mode_tool(tmp_path, monkeypatch)
    tool._run_git_raw(tmp_path, ["status"])

    events = _events(tmp_path / "vault")
    assert len(events) == 1, events
    ev = events[0]
    assert ev["event_type"] == "host_execution"
    assert ev["actor"] == "git_read"
    payload = ev["payload"]
    assert payload["fallback"] is True
    assert payload["reason"] == "container_unavailable"
    assert payload["workspace_id"] == WS
    assert payload["operation"] == "status"
    assert payload["kill_switch_state"] == "on"


def test_branch1_event_shape(tmp_path, monkeypatch):
    tool = _host_mode_tool(tmp_path, monkeypatch)
    tool._run_git_raw(tmp_path, ["status"])

    ev = _events(tmp_path / "vault")[0]
    assert set(ev) == {"timestamp", "event_type", "actor", "payload"}
    assert ev["timestamp"].endswith("Z")
    assert set(ev["payload"]) == {
        "fallback",
        "reason",
        "workspace_id",
        "operation",
        "kill_switch_state",
        "detail",
    }
    # Branch 1 has no resolver detail to carry.
    assert ev["payload"]["detail"] is None


# ---------------------------------------------------------------------------
# Branch 2: containerized -> degraded to hardened host
# ---------------------------------------------------------------------------
def test_branch2_appends_host_execution_event_with_detail(tmp_path):
    tool = _container_mode_tool(tmp_path)
    object.__setattr__(tool, "_resource_manager", _host_fallback_manager())

    tool._run_git_raw(tmp_path, ["status"])

    events = _events(tmp_path / "vault")
    assert len(events) == 1, events
    ev = events[0]
    assert ev["event_type"] == "host_execution"
    assert ev["actor"] == "git_read"
    payload = ev["payload"]
    assert payload["fallback"] is True
    assert payload["reason"] == "container_unavailable"
    assert payload["workspace_id"] == WS
    assert payload["operation"] == "status"
    assert payload["kill_switch_state"] == "on"
    assert payload["detail"] == "resource image unavailable"


# ---------------------------------------------------------------------------
# Host-denied path: no fallback happens -> nothing written
# ---------------------------------------------------------------------------
def test_no_event_when_host_denied(tmp_path, monkeypatch):
    # A vault with NO allow_host_resources config -> fail-closed deny.
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path / "empty-vault"))
    tool = _host_mode_tool(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError):
        tool._run_git_raw(tmp_path, ["status"])

    assert not _event_log(tmp_path / "empty-vault").exists()


# ---------------------------------------------------------------------------
# Append-only: N fallbacks -> N events
# ---------------------------------------------------------------------------
def test_events_are_append_only(tmp_path, monkeypatch):
    tool = _host_mode_tool(tmp_path, monkeypatch)
    tool._run_git_raw(tmp_path, ["status"])
    tool._run_git_raw(tmp_path, ["status"])

    events = _events(tmp_path / "vault")
    assert len(events) == 2, events
    assert all(ev["event_type"] == "host_execution" for ev in events)


# ---------------------------------------------------------------------------
# Unresolvable workspace id -> no-op (never raises)
# ---------------------------------------------------------------------------
def test_missing_workspace_id_writes_nothing(tmp_path, monkeypatch):
    tool = _host_mode_tool(tmp_path, monkeypatch)
    object.__setattr__(tool, "_resolved_workspace_id", None)

    tool._record_host_fallback_event()  # must not raise

    assert not _event_log(tmp_path / "vault").exists()


# ---------------------------------------------------------------------------
# R3: a write failure is swallowed (and logged), never raised
# ---------------------------------------------------------------------------
def test_write_failure_never_raises(tmp_path, caplog, monkeypatch):
    tool = _host_mode_tool(tmp_path, monkeypatch)

    # (a) vault root unresolvable -> path resolution fails -> silent no-op.
    def _boom():
        raise OSError("vault unavailable")

    monkeypatch.setattr("thoughtmachine.vault.vault_root", _boom)
    tool._record_host_fallback_event()  # must not raise

    # (b) the write itself fails -> swallowed, but logged (R3: never silent).
    bad_path = tmp_path / "as_dir" / "git.host.execution.jsonl"
    bad_path.mkdir(parents=True, exist_ok=True)  # a directory, not a file
    monkeypatch.setattr(
        type(tool), "_host_fallback_event_path", lambda self: bad_path
    )
    with caplog.at_level(logging.WARNING, logger="tools.git_info_tool"):
        tool._record_host_fallback_event()  # must not raise

    assert any(
        "event_write_failed" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    )
    assert bad_path.is_dir()
