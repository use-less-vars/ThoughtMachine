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
4. A pid inside OUR process group is signalled as ONE pid (``os.kill``) and
dies; ``os.killpg`` never receives our own pgid.  (This case used to be a
REFUSAL, which was the regression: a plain ``Popen`` child is a non-leader in
our group, so refusing meant it survived teardown.)
5. A pid whose group cannot be resolved is refused
   (``unknown_process_group``), and a pid that is already gone is a silent
SUCCESS (``gone``).
6. ``killpg`` refused with ``PermissionError``: no single-pid ``os.kill``
   fallback, no SIGKILL escalation, and a PROMPT return (a 30s grace must not
   be burned).
7. POSITIVE CONTROL: a GROUP LEADER is still killed with exactly
   ``os.killpg(9191, SIGTERM)`` -- the guard must not disarm teardown.
8. The decision table itself: ``refuse`` / ``gone`` / ``group`` / ``single``.
9. The CI case, standalone (no Docker): a non-leader child registered through
   ``register()`` exactly as the WLM integration test does is really killed.
10. A ``start_new_session=True`` child goes down the ``group`` path.
11. A zombie child (exited, NOT reaped) must not burn the grace period: the
   liveness poll reads ``/proc/<pid>/stat`` state instead of trusting signal 0.

Run with::

    pytest tests/test_execution_kill_target_guard.py -v
"""

from __future__ import annotations

import os
import signal
import subprocess
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
    """Records every ``os.killpg`` / ``os.kill`` the code under test makes.

    Signal 0 is a LIVENESS PROBE, not a signal: probes are recorded separately
    (``probes``) so ``kill``/``killpg`` keep meaning "a signal was sent".
    """

    def __init__(self, killpg_error=None, dead_probe=False) -> None:
        self.killpg = []
        self.kill = []
        self.probes = []
        self._killpg_error = killpg_error
        self._dead_probe = dead_probe

    def install(self, monkeypatch) -> "_Signals":
        monkeypatch.setattr("os.killpg", self._fake_killpg)
        monkeypatch.setattr("os.kill", self._fake_kill)
        return self

    def _fake_killpg(self, pid, sig):
        if sig == 0:
            # Signal 0 is a LIVENESS PROBE, not a signal.
            self.probes.append(("killpg", pid))
            if self._dead_probe:
                raise ProcessLookupError("no such process group")
            return None
        self.killpg.append((pid, sig))
        if self._killpg_error is not None:
            raise self._killpg_error
        return None

    def _fake_kill(self, pid, sig):
        if sig == 0:
            self.probes.append(("kill", pid))
            if self._dead_probe:
                raise ProcessLookupError("no such process")
            return None
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
# 4. our own process group: ONE pid, never the group
# ---------------------------------------------------------------------------

def _procfs_available() -> bool:
    """True only when the Linux procfs is MOUNTED (``/proc/self`` is a dir).

    Process groups are POSIX-wide; ``/proc`` is a Linux facility.  Darwin is
    ``os.name == "posix"`` and has no procfs at all, so ``os.name`` can never
    stand in for a ``/proc`` capability test -- which is why the old guard,
    whose reason named ``/proc state``, was a no-op on macOS and the state
    reads below fell through to a failure instead of a skip.  Same directory
    probe as ``scripts/prove_worker_pid.py``.
    """
    return os.path.isdir("/proc/self")


POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix",
    reason="requires POSIX process groups (os.killpg/os.getpgid)",
)

PROCFS_REQUIRED_REASON = (
    "requires Linux procfs (/proc) to read a process state; "
    "not available on this platform"
)

PROCFS_ONLY = pytest.mark.skipif(
    not _procfs_available(), reason=PROCFS_REQUIRED_REASON
)


class _RecordingKillpg:
    """Records ``os.killpg`` and forwards it -- EXCEPT on OUR own group.

    Forwarding is what lets the group tests observe a real death. The refusal
    on our own pgid is a safety interlock: if the code under test ever tried
    ``killpg(os.getpgid(0), ...)`` it would take this pytest process with it and
    the failure would read as an unexplained crash instead of a red assertion.
    """

    def __init__(self, forward: bool = True) -> None:
        self.calls = []
        self._forward = forward
        self._real = os.killpg

    def __call__(self, pid, sig):
        self.calls.append((pid, sig))
        if pid == os.getpgid(0):
            raise AssertionError(
                f"os.killpg({pid}, {sig}) targets OUR OWN process group"
            )
        try:
            own_group = os.getpgid(pid) == os.getpgid(0)
        except OSError:
            own_group = False
        if own_group:
            raise AssertionError(
                f"os.killpg({pid}, {sig}) targets a pid whose process group IS "
                "ours -- forwarding it would take this test process down"
            )
        if not self._forward:
            return None
        return self._real(pid, sig)

    def signalled(self, sig):
        return [pid for pid, sent in self.calls if sent == sig]


class _RecordingKill:
    """Records ``os.kill`` and forwards it, so a real child really dies."""

    def __init__(self) -> None:
        self.calls = []
        self._real = os.kill

    def __call__(self, pid, sig):
        self.calls.append((pid, sig))
        return self._real(pid, sig)

    def signalled(self, sig):
        return [pid for pid, sent in self.calls if sent == sig]


def _spawn_sleeper(new_session: bool = False, seconds: str = "30"):
    """A real ``sleep`` child; ``new_session`` makes it its own group leader."""
    return subprocess.Popen(["sleep", seconds], start_new_session=new_session)


def _reap(proc) -> None:
    """Never leak a child, whatever the assertions did."""
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=10.0)
    except subprocess.TimeoutExpired:  # pragma: no cover - belt and braces
        pass


@POSIX_ONLY
def test_own_process_group_is_killed_as_a_single_pid(monkeypatch):
    """A pid inside OUR group is signalled as ONE pid -- and actually dies.

    This REPLACES the old ``own_process_group`` refusal, which was the
    regression: a plain ``Popen`` child (``Popen(["sleep", "60"])``, no
    ``start_new_session``) is a non-leader in OUR group, so refusing meant a
    legitimate teardown target survived and ``proc.wait(timeout=5.0)`` timed
    out on CI.  ``killpg`` on such a pid signals the group it shares with US,
    so the only admissible channel is ``os.kill``.
    """
    logs = _Logs().install(monkeypatch)
    killpg = _RecordingKillpg(forward=True)
    monkeypatch.setattr("os.killpg", killpg)
    kill = _RecordingKill()
    monkeypatch.setattr("os.kill", kill)

    proc = _spawn_sleeper()
    try:
        assert os.getpgid(proc.pid) == os.getpgid(0), (
            "precondition: an unstarted-session Popen child lives in OUR group"
        )
        tracker = ExecutionTracker()
        tracker.kill_grace_seconds = 5.0
        tracker.add("e1", {"type": "subprocess", "pid": proc.pid})

        tracker.terminate_all("w1", None, None)

        assert proc.wait(timeout=5.0) == -signal.SIGTERM, (
            "the pid inside our own group was not killed: it exited "
            f"{proc.returncode!r} instead of dying from SIGTERM"
        )
        assert (proc.pid, signal.SIGTERM) in kill.calls, (
            f"no single-pid SIGTERM was sent: {kill.calls!r}"
        )
        assert os.getpgid(0) not in [pid for pid, _sig in killpg.calls], (
            f"os.killpg was aimed at OUR OWN process group: {killpg.calls!r}"
        )
        assert not any("refusing to signal" in m for m in logs.warnings()), (
            f"the kill path still refused a legitimate non-leader pid: "
            f"{logs.warnings()!r}"
        )
        assert tracker.active_count() == 0
    finally:
        _reap(proc)


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

    The target IS its own group leader (so the guard accepts it and takes the
    ``group`` path); ``killpg`` then fails with ``PermissionError``.  The old
    code fell back to ``os.kill(pid, SIGTERM)`` -- an equally blind signal --
    and then burned the whole grace period polling a group it was never allowed
    to signal.
    """
    monkeypatch.setattr("os.getpgid", lambda p: 5151 if p == 5151 else 111)
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
# 7. POSITIVE CONTROL: a process-group LEADER is still killed
# ---------------------------------------------------------------------------

def test_positive_control_group_leader_is_still_killed(monkeypatch):
    """CONTROL: pid 9191 IS its own group leader: killed once, as a group."""
    monkeypatch.setattr("os.getpgid", lambda p: 9191 if p == 9191 else 111)
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


# ---------------------------------------------------------------------------
# 8. the decision table itself: refuse / gone / group / single
# ---------------------------------------------------------------------------

def test_kill_decision_table(monkeypatch):
    """``_kill_decision`` maps every pid shape onto (mode, reason).

    The decision -- not a bare boolean -- is what the kill path acts on: a
    non-leader in OUR group is a legitimate ``single`` target (``os.kill``),
    never a ``killpg``, and a pid that is already gone is a silent success
    rather than a refusal.
    """
    ours = 900
    groups = {2000: ours, 3000: 3000, 4000: 5000}

    def _getpgid(pid):
        if pid == 0:
            return ours
        if pid == 7000:
            raise ProcessLookupError(3, "No such process")
        return groups[pid]

    monkeypatch.setattr("os.getpid", lambda: 1000)
    monkeypatch.setattr("os.getpgid", _getpgid)
    tracker = ExecutionTracker()

    # a non-leader sharing OUR group: one pid, never the group
    assert tracker._kill_decision(2000) == ("single", "shares_own_process_group")
    # a group leader: the group may be signalled
    assert tracker._kill_decision(3000) == ("group", "process_group_leader")
    # a non-leader in somebody else's group: one pid only
    assert tracker._kill_decision(4000) == ("single", "not_group_leader")
    # already gone: nothing to signal, and no warning either
    assert tracker._kill_decision(7000) == ("gone", "no_such_process")

    # a group that cannot be resolved: refuse.  The fake must RAISE for the
    # target: a getpgid that answers anything at all is a resolved group, and
    # a non-leader in somebody else's group is a legitimate ``single`` target
    # (the 4000 row above), so only a non-ProcessLookupError OSError can make
    # membership unknown here.
    def _unresolvable_target(pid):
        if pid == 0:
            return ours
        raise OSError("operation not permitted")

    monkeypatch.setattr("os.getpgid", _unresolvable_target)
    assert tracker._kill_decision(8000) == ("refuse", "unknown_process_group")
    # OUR OWN group unresolvable: also refuse (membership is unknown)
    def _own_group_gone(pid):
        if pid == 0:
            raise OSError("operation not permitted")
        return 5000

    monkeypatch.setattr("os.getpgid", _own_group_gone)
    assert tracker._kill_decision(4000) == ("refuse", "unknown_process_group")

    # invalid / privileged / self are decided before any group lookup
    assert tracker._kill_decision(True) == ("refuse", "invalid_pid")
    assert tracker._kill_decision(None) == ("refuse", "invalid_pid")
    assert tracker._kill_decision("not-a-pid") == ("refuse", "invalid_pid")
    assert tracker._kill_decision(0) == ("refuse", "privileged_pid")
    assert tracker._kill_decision(1) == ("refuse", "privileged_pid")
    assert tracker._kill_decision(-7) == ("refuse", "privileged_pid")
    assert tracker._kill_decision(1000) == ("refuse", "self_pid")


def test_gone_pid_is_a_silent_success(monkeypatch):
    """A pid that is already gone signals nothing, complains about nothing."""
    def _gone(pid):
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr("os.getpgid", _gone)
    signals = _Signals().install(monkeypatch)
    logs = _Logs().install(monkeypatch)
    tracker = ExecutionTracker()
    tracker.kill_grace_seconds = 0.1
    tracker.add("e1", {"type": "subprocess", "pid": 6060})

    tracker.terminate_all("w1", None, None)

    assert signals.kill == [], f"a gone pid was signalled: {signals.kill!r}"
    assert signals.killpg == [], f"a gone pid was killpg'd: {signals.killpg!r}"
    assert signals.probes == [], (
        "a gone pid was polled/escalated instead of returning immediately: "
        f"{signals.probes!r}"
    )
    assert logs.warnings() == [], (
        "a gone pid is a silent SUCCESS, not a refusal: " f"{logs.warnings()!r}"
    )
    assert tracker.active_count() == 0


# ---------------------------------------------------------------------------
# 9. the CI case, standalone: a non-leader child registered like the WLM test
# ---------------------------------------------------------------------------

@POSIX_ONLY
def test_ci_shaped_non_leader_subprocess_is_killed(monkeypatch):
    """The exact CI shape: ``Popen(["sleep", ...])`` with NO new session.

    ``tests/test_workspace_lifecycle_manager.py::test_wlm_integration_real_docker``
    registers ``terminate_all: subprocess w-wlm-int:tc-sleep`` with the pid of
    a plain ``Popen`` child and then does ``proc.wait(timeout=5.0)``.  Such a
    child is a NON-LEADER in OUR group, so the guard must resolve it to
    ``single`` and kill that one pid; refusing it (the regression) made the
    docker test blow up with ``TimeoutExpired``.
    """
    logs = _Logs().install(monkeypatch)
    killpg = _RecordingKillpg(forward=True)
    monkeypatch.setattr("os.killpg", killpg)

    proc = _spawn_sleeper(new_session=False, seconds="30")
    try:
        assert os.getpgid(proc.pid) == os.getpgid(0), (
            "precondition: an unstarted-session Popen child lives in OUR group"
        )
        tracker = ExecutionTracker()
        tracker.kill_grace_seconds = 5.0
        execution_id = tracker.register(
            worker_id="w-wlm-int",
            query_id="q-1",
            tool_call_id="tc-sleep",
            tool_name="bash",
            pid=proc.pid,
        )
        assert execution_id == "w-wlm-int:tc-sleep", execution_id

        tracker.terminate_all("w-wlm-int", None, None)

        try:
            returncode = proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                "terminate_all did not kill the non-leader subprocess: "
                "proc.wait(timeout=5.0) timed out -- this is exactly the "
                "TimeoutExpired that turned CI red"
            )
        assert returncode == -signal.SIGTERM, (
            f"the child exited {returncode!r} instead of dying from SIGTERM"
        )
        assert proc.poll() is not None
        assert os.getpgid(0) not in [pid for pid, _sig in killpg.calls], (
            f"os.killpg was aimed at OUR OWN process group: {killpg.calls!r}"
        )
        assert not any("refusing to signal" in m for m in logs.warnings()), (
            f"the kill path still refused the CI-shaped pid: {logs.warnings()!r}"
        )
        assert tracker.active_count() == 0
    finally:
        _reap(proc)


# ---------------------------------------------------------------------------
# 10. a session leader child goes down the GROUP path
# ---------------------------------------------------------------------------

@POSIX_ONLY
def test_session_leader_child_is_killed_as_a_group(monkeypatch):
    """A ``start_new_session=True`` child IS a group leader: ``killpg`` it."""
    _Logs().install(monkeypatch)
    killpg = _RecordingKillpg(forward=True)
    monkeypatch.setattr("os.killpg", killpg)

    proc = _spawn_sleeper(new_session=True, seconds="30")
    try:
        assert os.getpgid(proc.pid) == proc.pid, (
            "precondition: start_new_session makes the child its own group leader"
        )
        tracker = ExecutionTracker()
        tracker.kill_grace_seconds = 5.0
        tracker.add("e1", {"type": "subprocess", "pid": proc.pid})

        tracker.terminate_all("w1", None, None)

        assert (proc.pid, signal.SIGTERM) in killpg.calls, (
            f"a group leader was not killed through killpg: {killpg.calls!r}"
        )
        assert proc.wait(timeout=5.0) == -signal.SIGTERM, (
            f"the group leader exited {proc.returncode!r} instead of SIGTERM"
        )
        assert tracker.active_count() == 0
    finally:
        _reap(proc)


# ---------------------------------------------------------------------------
# 11. a zombie child must not burn the grace period
# ---------------------------------------------------------------------------

# ``_proc_state`` has THREE outcomes, never two.  A single ``None`` used to
# stand for both of the non-state ones, so on a platform without procfs "I
# cannot look" was reported downstream in the grammar of "I looked, and the
# child was not a zombie".
_PROC_STATE_GONE = "GONE"
_PROC_STATE_UNMEASURABLE = "UNMEASURABLE"


def _proc_state(pid):
    """State character of ``/proc/<pid>/stat``, or a sentinel saying why not.

    Returns the state character when the kernel answered, ``_PROC_STATE_GONE``
    when procfs answered "no such pid" (the child was already reaped) and
    ``_PROC_STATE_UNMEASURABLE`` when the state cannot be read at all (no
    procfs mounted, or an entry that exists and is unreadable).

    The comm field may contain spaces and parentheses, so the state is the
    first field AFTER THE LAST ``)`` -- never a naive ``split()[2]``.

    ``FileNotFoundError`` on its own does NOT mean "the process is gone": it
    also means "this platform has no procfs".  The two are told apart by
    probing ``/proc`` itself, so that inability-to-measure is never silently
    reported as an observed death.
    """
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as handle:
            data = handle.read()
    except FileNotFoundError:
        if not _procfs_available():
            return _PROC_STATE_UNMEASURABLE
        return _PROC_STATE_GONE
    except OSError:
        # The entry could not be read (permissions, I/O error, ...): that is
        # not an observation of the process state either.
        return _PROC_STATE_UNMEASURABLE
    end = data.rfind(")")
    if end == -1:
        return _PROC_STATE_UNMEASURABLE
    fields = data[end + 1:].split()
    return fields[0] if fields else _PROC_STATE_UNMEASURABLE


# Returned by ``_wait_for_zombie`` when the state could not be read: NOT a
# pass and NOT a failure -- the case simply cannot be measured here.
_NOT_MEASURED = "NOT_MEASURED"


def _wait_for_zombie(pid, timeout: float = 10.0):
    """Block until ``pid`` is a zombie -- WITHOUT reaping it.

    ``proc.poll()`` / ``proc.wait()`` would reap the child and delete the very
    ``/proc`` entry the case is about, so the state is read straight from
    ``/proc`` instead.

    Three outcomes, kept apart on purpose:

    - ``None``: a zombie WAS measured (the caller may assert on it);
    - ``_NOT_MEASURED``: the state cannot be read at all -- no procfs on this
      platform, or an unreadable entry.  Nothing is claimed about the child;
      the caller must skip with ``PROCFS_REQUIRED_REASON`` rather than report
      a dead child;
    - ``AssertionError``: the state WAS read, repeatedly, and was never ``Z``.
      That is a real failure and still raises the historical message.  The
      message is a claim about the child, so it must never be reachable by a
      platform that simply cannot look.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _proc_state(pid)
        if state == "Z":
            return None
        if state == _PROC_STATE_UNMEASURABLE:
            return _NOT_MEASURED
        if state == _PROC_STATE_GONE:
            raise AssertionError(
                f"pid {pid} has no /proc entry and was already reaped, so its "
                "zombie state never became observable"
            )
        time.sleep(0.01)
    raise AssertionError(
        f"pid {pid} never became a zombie within {timeout}s "
        f"(last state {_proc_state(pid)!r})"
    )


@POSIX_ONLY
@PROCFS_ONLY
@pytest.mark.parametrize("new_session", [False, True])
def test_zombie_child_does_not_burn_the_grace_period(monkeypatch, new_session):
    """An exited-but-unreaped child is DEAD: teardown returns, no grace burned.

    Signal 0 still succeeds on a zombie, so a ``os.killpg(pid, 0)`` poll would
    sit out the whole grace period and then SIGKILL a corpse.  Liveness must
    therefore read the ``/proc`` state (``Z`` == dead).

    Two capabilities are needed and they are NOT the same one: POSIX process
    groups (``os.killpg``/``os.getpgid``, for the monkeypatch below) and the
    Linux procfs (to OBSERVE the ``Z`` state this case is about).  The
    ``PROCFS_ONLY`` marker is the collection-time guard; the in-body check
    covers the same condition discovered at run time -- without it the case
    would report an unreadable ``/proc`` as a child that never died.
    """
    _Logs().install(monkeypatch)
    killpg = _RecordingKillpg(forward=True)
    monkeypatch.setattr("os.killpg", killpg)

    proc = _spawn_sleeper(new_session=new_session, seconds="0.1")
    try:
        if _wait_for_zombie(proc.pid) == _NOT_MEASURED:
            pytest.skip(PROCFS_REQUIRED_REASON)
        tracker = ExecutionTracker()
        tracker.kill_grace_seconds = 3.0
        tracker.add("e1", {"type": "subprocess", "pid": proc.pid})

        started = time.monotonic()
        tracker.terminate_all("w1", None, None)
        elapsed = time.monotonic() - started

        assert elapsed < 2.0, (
            f"a zombie child burned {elapsed:.2f}s of the 3.0s grace period "
            "instead of being recognised as dead"
        )
        assert tracker.active_count() == 0
    finally:
        _reap(proc)

