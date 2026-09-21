"""Git execution must NEVER silently fall back to the host.

Silent host fallback has been removed:

* When containerized execution is *required* but the container resource is
  degraded (``host_fallback``) or otherwise unavailable (``unavailable``),
  ``_run_git_raw`` raises ``RuntimeError`` instead of executing on the
  hardened host path -- for BOTH read and write operations.
* The container-status route no longer projects a host-execution event, and
  the writer that produced it is gone (``web_ui.backend.server`` no longer
  exposes ``_host_execution_json``).
* Host-only execution (container mode inactive) remains gated on the
  workspace ``allow_host_resources`` kill-switch and never records a
  host-fallback event.
* The ``allow_host_fallback`` parameter has been dropped from the internal
  git execution helpers.

These tests are mock-based: no real git binary or docker daemon required.
"""

import inspect
import json

import pytest

from tools.git_info_tool import GitInfoTool
from tools.git_write_tool import GitWriteTool


WS = "test-ws"


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


class _HostCallRecorder:
    """Records calls to ``_exec_host_raw`` (must never run in these tests)."""

    def __init__(self):
        self.calls = []

    def __call__(self, repo_root, args, timeout=30, **kwargs):
        self.calls.append((str(repo_root), list(args)))
        return (0, "ok\n", "")


def _container_tool(tmp_path, operation="commit"):
    tool_cls = (
        GitWriteTool
        if operation
        in ("commit", "stage", "unstage", "init", "clone", "branch_create", "checkout")
        else GitInfoTool
    )
    tool = tool_cls(
        operation=operation,
        message="x",
        session_permissions={"git": "write"},
        effective_permissions={"git": "write"},
    )
    object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
    object.__setattr__(tool, "_resolved_workspace_id", WS)
    return tool


def _host_tool(tmp_path, monkeypatch, *, allow_host):
    """Tool whose git mode resolves to host (container mode inactive)."""
    tool = GitInfoTool(
        operation="status",
        session_permissions={"git": "read"},
        effective_permissions={"git": "read"},
    )
    object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
    object.__setattr__(tool, "_resolved_workspace_id", WS)
    monkeypatch.setattr(tool, "_use_container_mode", lambda: False)
    _write_workspace_config(tmp_path, monkeypatch, allow_host)
    return tool


def _write_workspace_config(tmp_path, monkeypatch, allow_host):
    vault = tmp_path / "vault"
    cfg_dir = vault / "workspaces" / WS
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(
        json.dumps({"allow_host_resources": allow_host}), encoding="utf-8"
    )
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))


def _host_event_log(tmp_path):
    return (
        tmp_path
        / "vault"
        / "workspaces"
        / WS
        / "resources"
        / "git.host_execution.jsonl"
    )


# ---------------------------------------------------------------------------
# (a) container unavailable -> hard fail, never host-run
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("operation", ["status", "commit"])
@pytest.mark.parametrize(
    "mode",
    ["host_fallback", "unavailable"],
    ids=["degraded", "unavailable"],
)
def test_container_unavailable_never_runs_on_host(
    tmp_path, monkeypatch, mode, operation
):
    """A degraded/unavailable container MUST raise, never exec on the host."""
    _write_workspace_config(tmp_path, monkeypatch, allow_host=True)
    manager = _FakeEnsureManager(
        {
            "mode": mode,
            "container_id": None,
            "status": None,
            "image": None,
            "detail": (
                "resource image unavailable"
                if mode == "host_fallback"
                else "container resources disabled/denied"
            ),
        }
    )
    tool = _container_tool(tmp_path, operation=operation)
    object.__setattr__(tool, "_resource_manager", manager)
    host = _HostCallRecorder()
    monkeypatch.setattr(tool, "_exec_host_raw", host)

    args = ["commit", "-m", "x"] if operation == "commit" else ["status"]
    with pytest.raises(
        RuntimeError, match="containerized git execution unavailable"
    ):
        tool._run_git_raw(tmp_path, args)

    # Neither backend may run: no container exec, no hardened host exec.
    assert not [c for c in manager.calls if c[0] == "exec"]
    assert host.calls == []
    # No host-execution event is ever recorded.
    assert not _host_event_log(tmp_path).exists()

    # The route-side host-execution projection is gone.
    import web_ui.backend.server as server_mod

    assert not hasattr(server_mod, "_host_execution_json")


# ---------------------------------------------------------------------------
# (b) host-only mode: kill-switch round-trip
# ---------------------------------------------------------------------------
def test_host_mode_kill_switch_round_trip(tmp_path, monkeypatch):
    """Host-only execution is gated: OFF denies, ON runs -- no fallback event."""
    # (1) kill-switch OFF -> denied, no host exec, no event.
    tool = _host_tool(tmp_path, monkeypatch, allow_host=False)
    host = _HostCallRecorder()
    monkeypatch.setattr(tool, "_exec_host_raw", host)
    with pytest.raises(RuntimeError, match="allow_host_resources"):
        tool._run_git_raw(tmp_path, ["status"])
    assert host.calls == []
    assert not _host_event_log(tmp_path).exists()

    # (2) kill-switch ON -> host exec runs, still no fallback event.
    tool2 = _host_tool(tmp_path, monkeypatch, allow_host=True)
    host2 = _HostCallRecorder()
    monkeypatch.setattr(tool2, "_exec_host_raw", host2)
    tool2._run_git_raw(tmp_path, ["status"])
    assert len(host2.calls) == 1
    assert not _host_event_log(tmp_path).exists()


# ---------------------------------------------------------------------------
# (c) internal helpers drop the allow_host_fallback parameter
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cls,method",
    [
        (GitInfoTool, "_run_git"),
        (GitInfoTool, "_run_git_raw"),
        (GitWriteTool, "_git_add"),
    ],
)
def test_internal_helpers_drop_allow_host_fallback(cls, method):
    """The container-mandatory flag is gone from the git execution helpers."""
    params = inspect.signature(getattr(cls, method)).parameters
    assert "allow_host_fallback" not in params
