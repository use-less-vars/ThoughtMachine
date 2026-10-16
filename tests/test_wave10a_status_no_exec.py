"""Wave 10a commit 7 -- the container status path reads processes PASSIVELY.

Ticket ``bug/container-status-tool-executes`` / field report: the status path
enumerated processes by exec'ing a shell inside the *target* container::

    /bin/sh -c for p in /proc/[0-9]*; do ... done

That turns a nominally read-only tool into an execution path, and the exec was
not recorded in ``recent_commands``. Commit 7 replaces it with the passive
Docker daemon call ``container.top()`` (``GET /containers/{id}/top``), which
never execs anything inside the container.

These tests pin that behaviour:

  * the manager's exec seam (``_exec_checked`` -- the exact attribute the
    removed code called) is guarded so that a process-enumeration exec, i.e.
    the ``/proc/`` probe, raises ``AssertionError``; the status path must still
    succeed and still report the process list (now sourced from ``top()``);

  * the Docker exec entry point (``container.exec_run``) is forced to raise for
    ANY call; both ``manager.status()`` and the public
    ``ContainerStatusTool.execute()`` describe path must nonetheless succeed.
    If the ``/proc`` exec is ever reintroduced the degraded path drops
    ``running_processes`` and the assertions fail.

The manager is built with ``__new__`` (no Docker daemon); the container is a
``MagicMock`` -- mirrors ``tests/tools/test_container_control.py``.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from infra.container_manager import ContainerManager
from tools.container_control import ContainerStatusTool

# The shell probe the removed code issued (and which must never come back).
_PROC_COMMAND_MARKERS = ("/proc/", "cmdline")

_TOP = {
    "Titles": ["PID", "CMD"],
    "Processes": [[1, "/bin/sh"], [42, "python3 main.py"]],
}
_EXPECTED_PROCESSES = [
    {"pid": 1, "command": "/bin/sh"},
    {"pid": 42, "command": "python3 main.py"},
]


def _iso_dt(seconds_ago):
    return (
        datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    ).isoformat()


def _build_manager(*, top=None, top_error=False, status="running"):
    """Hermetic ContainerManager + its fake docker container (no daemon)."""
    manager = ContainerManager.__new__(ContainerManager)  # skip docker.from_env()
    client = MagicMock()
    fake = MagicMock()
    fake.id = "c1"
    fake.name = "tm-1"
    fake.status = status
    fake.attrs = {"State": {"StartedAt": _iso_dt(120)}}
    fake.labels = {"thoughtmachine.workspace_id": "ws-1"}
    if top_error:
        fake.top.side_effect = RuntimeError("container is not running")
    else:
        fake.top.return_value = _TOP if top is None else top
    client.containers.get.return_value = fake
    client.stats.return_value = {"memory_stats": {"usage": 1234}}
    manager.client = client
    manager.workspace_id = "ws-1"
    manager.session_id = "sess-1"
    manager.container_notes = {}
    manager.workspace_config = {}
    return manager, fake


def test_status_makes_no_process_exec(monkeypatch):
    """The manager's exec seam is never used for a ``/proc`` process scan."""
    seen = []
    real = ContainerManager._exec_checked

    def guard(self, container, command, timeout=10):
        seen.append(command)
        for marker in _PROC_COMMAND_MARKERS:
            assert marker not in command, (
                "status path exec'd a process enumeration (%r); use "
                "container.top() instead" % command
            )
        return real(self, container, command, timeout=timeout)

    monkeypatch.setattr(ContainerManager, "_exec_checked", guard)
    manager, fake = _build_manager()

    result = manager.status("c1")

    assert result["status"] == "running"
    assert result["running_processes"] == _EXPECTED_PROCESSES
    fake.top.assert_called_once()
    # The seam was exercised (pip/du/history still exec) but never for a
    # /proc process scan.
    assert seen, "expected the legitimate introspection execs to run"
    assert all(
        all(marker not in command for marker in _PROC_COMMAND_MARKERS)
        for command in seen
    )


def test_status_succeeds_when_exec_entry_point_raises():
    """ANY ``container.exec_run`` call raises -> status must still succeed.

    The process list no longer depends on exec; it comes from ``top()``. If the
    ``/proc`` exec were reintroduced, ``_exec_checked`` would swallow the
    ``AssertionError`` and drop ``running_processes`` -- this assertion fails.
    """
    manager, fake = _build_manager()
    fake.exec_run.side_effect = AssertionError("exec must not be touched")

    result = manager.status("c1")

    assert result["status"] == "running"
    assert result["running_processes"] == _EXPECTED_PROCESSES
    fake.top.assert_called_once()


def test_status_tool_describe_path_is_passive():
    """The public ContainerStatusTool describe path makes no exec for procs."""
    manager, fake = _build_manager()
    fake.exec_run.side_effect = AssertionError("exec must not be touched")

    tool = ContainerStatusTool(container_id="c1")
    with patch.object(ContainerStatusTool, "_make_manager", return_value=manager):
        payload = json.loads(tool.execute())

    assert payload["success"] is True
    assert payload["status"] == "running"
    assert payload["running_processes"] == _EXPECTED_PROCESSES
    fake.top.assert_called_once()


def test_top_failure_degrades_without_process_field():
    """A failing ``top()`` -> field absent + recorded, never a raise."""
    manager, fake = _build_manager(top_error=True)

    result = manager.status("c1")

    assert result["status"] == "running"
    assert "running_processes" not in result
    assert "process scan failed" in result["introspection_errors"]
    fake.top.assert_called_once()
