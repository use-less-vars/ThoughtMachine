"""test_execution_kill_target_guard.py -- subprocess kills must never target US.

``ExecutionTracker._terminate_subprocess`` signals a PROCESS GROUP with
``os.killpg``.  Two independent defects made that signal able to hit the agent
process itself:

1. ``WorkerThread._run_tool_loop`` mined the tool call's OWN ``arguments`` /
   ``data`` / ``result`` payloads for ``container_id`` / ``exec_id`` / ``pid``,
   so a tool that merely accepted a ``pid`` argument registered that number as
   the kill target of the in-flight execution.
2. The kill path carried no guard at all: ``pid 0`` means "my process group"
   and ``pid == os.getpid()`` (or any pid inside our own group) means "me", so
   teardown signalled the whole agent/test-runner group.

Covered here (one function per case):

1. A pid carried in the tool call's ``arguments`` is NOT registered as a kill
   target, and the teardown therefore signals nothing.
2. ``pid`` 0 / 1 / -5 / True are refused (``privileged_pid`` /
   ``invalid_pid``).
3. Our own pid is refused (``self_pid``).
4. A pid whose process group is OUR group is refused (``own_process_group``).
5. A pid whose group cannot be resolved is refused
   (``unknown_process_group``).
6. ``killpg`` refused with ``PermissionError``: no single-pid ``os.kill``
   fallback, no SIGKILL escalation, and a PROMPT return (a 30s grace must not
   be burned).
7. POSITIVE CONTROL: a pid in a FOREIGN process group is still killed with
   exactly ``os.killpg(9191, SIGTERM)`` -- the guard must not disarm teardown.

Run with::

    pytest tests/test_execution_kill_target_guard.py -v
"""

from __future__ import annotations

import os
import signal
import time

from pathlib import Path

import pytest

import tools.workspace.worker_execution as worker_execution_module
from tools.workspace.worker_execution import ExecutionTracker
from tools.workspace.worker_thread import WorkerThread


# ---------------------------------------------------------------------------
# Harness: a REAL WorkerThread that is never start()ed + a scripted agent
# ---------------------------------------------------------------------------

class _InjectedPidAgent:
    """Agent stand-in yielding ONE ``tool_call`` whose ARGUMENTS carry a pid.

    This is the shape that made the defect reachable: the tool is called with
    ``{"pid": 4242}``, which is a tool input, never an execution identifier.
    """

    def __init__(self, arguments) -> None:
        self.queries = []
        self._arguments = arguments

    # -- the reading-mode seam pause() drives -------------------------------
    def enter_reading_mode(self, reason: str = "pause") -> None:
        pass

    def request_pause(self) -> None:
        self.enter_reading_mode("pause")

    # -- the turn -----------------------------------------------------------
    def process_query(self, query: str):
        self.queries.append(query)
        yield {
            "type": "tool_call",
            "name": "RunCommand",
            "tool_call_id": "call-inject",
            "arguments": dict(self._arguments),
        }
        yield {
            "type": "agent_responded",
            "content": "done",
            "status": "success",
            "confidence": "high",
        }


class _NullStateBridge:
    """Keeps the loop's presenter lazy-init out of these tests."""

    context_length = 0


def _unstarted_thread(tmp_path) -> WorkerThread:
    """A REAL ``WorkerThread`` on ``tmp_path`` that is never ``start()``ed."""
    thread = WorkerThread(
        name="test-worker-kill-guard",
        definition={
            "name": "test-worker-kill-guard",
            "system_prompt": "You are a test worker.",
        },
        agent_config={},
        workspace_dir=Path(tmp_path),
        session_id="sess-kill-guard",
        container_manager=None,
        max_container_count=4,
        max_token_usage=None,
        max_runtime_s=None,
    )
    thread._state_bridge = _NullStateBridge()
    thread._publish_event = lambda etype, data: None
    return thread


class _Signals:
    """Records every ``os.killpg`` / ``os.kill`` the code under test makes."""

    def __init__(self, killpg_error=None, dead_probe=False) -> None:
        self.killpg = []
        self.kill = []
        self._killpg_error = killpg_error
        self._dead_probe = dead_probe

    def install(self, monkeypatch) -> "_Signals":
        monkeypatch.setattr("os.killpg", self._fake_killpg)
        monkeypatch.setattr("os.kill", self._fake_kill)
        return self

    def _fake_killpg(self, pid, sig):
        if sig == 0 and self._dead_probe:
            # Signal 0 is a LIVENESS PROBE, not a signal: the group is gone.
            raise ProcessLookupError("no such process group")
        self.killpg.append((pid, sig))
        if self._killpg_error is not None:
            raise self._killpg_error
        return None

    def _fake_kill(self, pid, sig):
        self.kill.append((pid, sig))
        return None


class _Logs:
    """Records ``_log(level, component, message)`` from worker_execution."""

    def __init__(self) -> None:
        self.entries = []

    def __call__(self, level, component, message) -> None:
        self.entries.append((level, component, message))

    def warnings(self):
        return [m for level, _component, m in self.entries if level == "WARNING"]

    def install(self, monkeypatch) -> "_Logs":
        monkeypatch.setattr(worker_execution_module, "_log", self)
        return self


def _assert_refused(logs: _Logs, signals: _Signals, layer: str, pid) -> None:
    assert signals.killpg == [], (
        f"pid {pid!r} was signalled as a process GROUP: {signals.killpg!r}"
    )
    assert signals.kill == [], f"pid {pid!r} was signalled as a pid: {signals.kill!r}"
    matching = [m for m in logs.warnings() if layer in m and repr(pid) in m]
    assert matching, (
        f"expected a WARNING naming the refusal layer {layer!r} and pid "
        f"{pid!r}; got warnings {logs.warnings()!r}"
    )


# ---------------------------------------------------------------------------
# 1. the injection: a pid inside the tool call's arguments is not a target
# ---------------------------------------------------------------------------

def test_nested_arguments_pid_is_not_registered_as_kill_target(tmp_path, monkeypatch):
    """``arguments={"pid": 4242}`` must not become the registered kill target."""
    thread = _unstarted_thread(tmp_path)
    thread._agent = _InjectedPidAgent({"pid": 4242})
    thread._current_query_id = "q-inject"
    signals = _Signals().install(monkeypatch)
    logs = _Logs().install(monkeypatch)

    thread._run_tool_loop("a query that calls a tool with a pid argument")

    execution_id = f"{thread.worker_name}:call-inject"
    registered = thread._execution_tracker._executions
    assert execution_id in registered, (
        "the tool call was not registered at all: " f"{registered!r}"
    )
    assert registered[execution_id]["pid"] is None, (
        "a pid carried by the tool's own ARGUMENTS was registered as the kill "
        f"target of the in-flight execution: {registered[execution_id]!r}"
    )

    thread._execution_tracker.kill_grace_seconds = 0.5
    thread._execution_tracker.terminate_all("w1", None, None)

    assert signals.killpg == [], (
        f"teardown signalled a tool argument as a process group: {signals.killpg!r}"
    )
    assert signals.kill == [], (
        f"teardown signalled a tool argument as a pid: {signals.kill!r}"
    )
    assert any("has no pid" in m for m in logs.warnings()), logs.warnings()


# ---------------------------------------------------------------------------
# 2. privileged / invalid pids
# ---------------------------------------------------------------------------

def test_privileged_and_invalid_pids_are_refused(monkeypatch):
    """``pid`` 0 (my group), 1 (init), -5 and ``True`` are all refused."""
    for pid, layer in ((0, "privileged_pid"), (1, "privileged_pid"),
                       (-5, "privileged_pid"), (True, "invalid_pid")):
        signals = _Signals().install(monkeypatch)
        logs = _Logs().install(monkeypatch)
        tracker = ExecutionTracker()
        tracker.kill_grace_seconds = 0.1
        tracker.add("e1", {"type": "subprocess", "pid": pid})

        tracker.terminate_all("w1", None, None)

        _assert_refused(logs, signals, layer, pid)
        assert tracker.active_count() == 0


# ---------------------------------------------------------------------------
# 3. our own pid
# ---------------------------------------------------------------------------

def test_own_pid_is_refused(monkeypatch):
    """``pid == os.getpid()`` is refused as ``self_pid``."""
    signals = _Signals().install(monkeypatch)
    logs = _Logs().install(monkeypatch)
    tracker = ExecutionTracker()
    tracker.kill_grace_seconds = 0.1
    pid = os.getpid()
    tracker.add("e1", {"type": "subprocess", "pid": pid})

    tracker.terminate_all("w1", None, None)

    _assert_refused(logs, signals, "self_pid", pid)


# ---------------------------------------------------------------------------
# 4. our own process group
# ---------------------------------------------------------------------------

def test_own_process_group_is_refused(monkeypatch):
    """A pid inside OUR process group is refused as ``own_process_group``."""
    monkeypatch.setattr("os.getpgid", lambda p: 7777)
    signals = _Signals().install(monkeypatch)
    logs = _Logs().install(monkeypatch)
    tracker = ExecutionTracker()
    tracker.kill_grace_seconds = 0.1
    tracker.add("e1", {"type": "subprocess", "pid": 6060})

    tracker.terminate_all("w1", None, None)

    _assert_refused(logs, signals, "own_process_group", 6060)


# ---------------------------------------------------------------------------
# 5. an unresolvable process group
# ---------------------------------------------------------------------------

def test_unknown_process_group_is_refused(monkeypatch):
    """``os.getpgid`` refused for a non-absent reason: refuse the kill."""
    def _refuse(pid):
        raise OSError("operation not permitted")

    monkeypatch.setattr("os.getpgid", _refuse)
    signals = _Signals().install(monkeypatch)
    logs = _Logs().install(monkeypatch)
    tracker = ExecutionTracker()
    tracker.kill_grace_seconds = 0.1
    tracker.add("e1", {"type": "subprocess", "pid": 7070})

    tracker.terminate_all("w1", None, None)

    _assert_refused(logs, signals, "unknown_process_group", 7070)


# ---------------------------------------------------------------------------
# 6. PermissionError: no fallback, no escalation, no grace burned
# ---------------------------------------------------------------------------

def test_permission_error_refuses_without_fallback_or_escalation(monkeypatch):
    """A refused ``killpg(SIGTERM)`` must not degrade to ``os.kill``.

    The target group is FOREIGN (so the guard accepts it); ``killpg`` then
    fails with ``PermissionError``.  The old code fell back to
    ``os.kill(pid, SIGTERM)`` -- an equally blind signal -- and then burned the
    whole grace period polling a group it was never allowed to signal.
    """
    monkeypatch.setattr("os.getpgid", lambda p: 8888 if p == 5151 else 111)
    signals = _Signals(killpg_error=PermissionError("not permitted"))
    signals.install(monkeypatch)
    logs = _Logs().install(monkeypatch)
    tracker = ExecutionTracker()
    tracker.kill_grace_seconds = 30.0
    tracker.add("e1", {"type": "subprocess", "pid": 5151})

    started = time.monotonic()
    tracker.terminate_all("w1", None, None)
    elapsed = time.monotonic() - started

    assert signals.kill == [], (
        f"a refused killpg degraded to a single-pid signal: {signals.kill!r}"
    )
    assert signals.killpg == [(5151, signal.SIGTERM)], (
        "a refused killpg must return immediately -- no liveness probe and no "
        f"SIGKILL escalation: {signals.killpg!r}"
    )
    assert elapsed < 5.0, (
        f"the refusal burned {elapsed:.2f}s of the 30s grace period instead of "
        "returning promptly"
    )
    assert any("permission_denied" in m for m in logs.warnings()), logs.warnings()
    assert tracker.active_count() == 0


# ---------------------------------------------------------------------------
# 7. POSITIVE CONTROL: a foreign process group is still killed
# ---------------------------------------------------------------------------

def test_positive_control_foreign_group_is_still_killed(monkeypatch):
    """CONTROL: pid 9191 in a FOREIGN group is killed exactly once, SIGTERM."""
    monkeypatch.setattr("os.getpgid", lambda p: 4242 if p == 9191 else 111)
    signals = _Signals(dead_probe=True).install(monkeypatch)
    _Logs().install(monkeypatch)
    tracker = ExecutionTracker()
    tracker.kill_grace_seconds = 5.0
    tracker.add("e1", {"type": "subprocess", "pid": 9191})

    tracker.terminate_all("w1", None, None)

    assert signals.killpg == [(9191, signal.SIGTERM)], (
        "the guard disarmed a legitimate teardown: " f"{signals.killpg!r}"
    )
    assert signals.kill == [], f"unexpected single-pid signal: {signals.kill!r}"
    assert tracker.active_count() == 0
