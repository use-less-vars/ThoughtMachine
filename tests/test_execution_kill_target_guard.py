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
12. An UNREADABLE process state (no ``/proc`` at all, or an entry that exists
   and cannot be read) must degrade toward ALIVE, never toward DEAD: the
   escalation burns the whole grace period and STILL issues the kill.
   CHARACTERISATION test -- behaviour already correct by accident, no
   capability guard: the unreadable read is injected at the seam production
   code already uses, TOGETHER WITH the three POSIX steps that run before it
   (``os.getpgid``, ``os.killpg``, ``os.kill(pid, 0)``), so the arm is really
   exercised on Linux AND on Darwin.

Run with::

    pytest tests/test_execution_kill_target_guard.py -v
"""

from __future__ import annotations

import builtins
import functools
import os
import platform
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


def _exc_tag(exc: BaseException) -> str:
    """``Type(errno=..., strerror=..., msg=...)`` -- enough to name an ESRCH.

    Diagnostic only: a raw ``repr`` of an ``OSError`` hides the platform detail
    (Darwin answers ``ESRCH`` where Linux succeeds) that is the whole question.
    """
    try:
        return "%s(errno=%r, strerror=%r, msg=%r)" % (
            type(exc).__name__,
            getattr(exc, "errno", None),
            getattr(exc, "strerror", None),
            str(exc),
        )
    except Exception:  # pragma: no cover - diagnostic only
        return "<untaggable %s>" % type(exc).__name__


def _short(value, limit: int = 120) -> str:
    """``repr(value)`` truncated to ``limit`` chars; never raises."""
    try:
        text = repr(value)
    except Exception:  # pragma: no cover - diagnostic only
        return "<unreprable %s>" % type(value).__name__
    return text if len(text) <= limit else text[:limit] + "..."


def _read_proc_stat(pid) -> str:
    """The REAL ``/proc/<pid>/stat`` head, or the exception -- diagnostic only."""
    try:
        with builtins.open("/proc/%s/stat" % (pid,), "r",
                           encoding="utf-8", errors="replace") as handle:
            return handle.read(200)
    except Exception as exc:
        return "raised " + _exc_tag(exc)


class _CallTrace:
    """Ordered ENTRY/RETURN record of the methods the teardown path uses.

    Installed as INSTANCE attributes via ``setattr``, so every
    ``self.<name>`` lookup inside production code hits the recorder.  An
    instance attribute is NOT a descriptor, so ``self`` is not passed to it:
    the recorder receives exactly the call arguments production supplied.
    Pass-through ONLY: arguments, return value and any exception are recorded
    and forwarded unchanged, so nothing the assertions observe moves.

    An ABSENT entry is the answer this exists to produce: if
    ``_escalate_subprocess_kill`` never appears, the early return happened in
    ``_terminate_subprocess`` (between the SIGTERM and the escalation); if it
    appears but ``_pid_is_alive`` does not, the escalation returned at its own
    re-taken ``refuse`` / ``gone`` arm before the poll.
    """

    TRACED = (
        "terminate_all",
        "_terminate_subprocess",
        "_kill_decision",
        "_escalate_subprocess_kill",
        "_pid_is_alive",
    )

    def __init__(self) -> None:
        self.events = []
        self.installed = {}
        self.failures = {}
        self._depth = 0

    def install(self, tracker, monkeypatch) -> "_CallTrace":
        for name in self.TRACED:
            try:
                original = getattr(tracker, name)
            except AttributeError as exc:
                self.failures[name] = "missing attribute: " + _exc_tag(exc)
                continue
            self.installed[name] = (
                "was an instance attribute" if name in vars(tracker)
                else "bound method -> instance attribute"
            )
            monkeypatch.setattr(tracker, name, self._recorder(name, original))
        return self

    def _recorder(self, name, original):
        @functools.wraps(original)
        def recorder(*args, **kwargs):
            shown = ", ".join(
                [_short(arg, 60) for arg in args]
                + ["%s=%s" % (key, _short(val, 60))
                   for key, val in kwargs.items()]
            )
            depth = self._depth
            self.events.append((depth, "CALL %s(%s)" % (name, shown)))
            self._depth += 1
            try:
                result = original(*args, **kwargs)
            except BaseException as exc:
                self._depth -= 1
                self.events.append(
                    (depth, "RAISE %s -> %s" % (name, _exc_tag(exc)))
                )
                raise
            self._depth -= 1
            self.events.append(
                (depth, "RET   %s -> %s" % (name, _short(result, 80)))
            )
            return result

        return recorder

    def counts(self) -> dict:
        """How many times each traced method was ENTERED."""
        found = {}
        for _depth, text in self.events:
            verb, _sep, rest = text.partition(" ")
            if verb != "CALL":
                continue
            name = rest.split("(", 1)[0]
            found[name] = found.get(name, 0) + 1
        return found

    def absent(self) -> list:
        """Traced methods that were NEVER entered -- the diagnostic's answer."""
        found = self.counts()
        return [name for name in self.TRACED if name not in found]


class _RecordingKillpg:
    """Records ``os.killpg`` and forwards it -- EXCEPT on OUR own group.

    Forwarding is what lets the group tests observe a real death. The refusal
    on our own pgid is a safety interlock: if the code under test ever tried
    ``killpg(os.getpgid(0), ...)`` it would take this pytest process with it and
    the failure would read as an unexplained crash instead of a red assertion.

    ``absorb_failure_for=<pid>`` turns the forward into a GROUP PROBE: the real
    ``os.killpg`` is still called (so the host really answers, and the answer
    is recorded in ``forwards``), but its failure for THAT pid is answered as
    success instead of being re-raised. That is how a host where the only
    members of the target group are corpses -- Darwin -- is given the same
    starting position as a host where the probe succeeds. ``calls`` is
    appended before the forward, so ``signalled(SIGKILL)`` keeps working even
    when the group was empty and the host refused.
    """

    def __init__(
        self, forward: bool = True, absorb_failure_for: int | None = None
    ) -> None:
        self.calls = []
        # ``forwards`` names the OUTCOME of every forward, exception included:
        # a Darwin ``ESRCH`` from the real ``os.killpg`` used to be swallowed by
        # production's ``except OSError`` and was invisible from the outside.
        self.forwards = []
        self._forward = forward
        self._absorb = absorb_failure_for
        self._real = os.killpg

    def __call__(self, pid, sig):
        self.calls.append((pid, sig))
        if pid == os.getpgid(0):
            exc = AssertionError(
                f"os.killpg({pid}, {sig}) targets OUR OWN process group"
            )
            self.forwards.append(
                (pid, sig, "NOT forwarded, blocked here: " + _exc_tag(exc))
            )
            raise exc
        try:
            own_group = os.getpgid(pid) == os.getpgid(0)
        except OSError:
            own_group = False
        if own_group:
            exc = AssertionError(
                f"os.killpg({pid}, {sig}) targets a pid whose process group IS "
                "ours -- forwarding it would take this test process down"
            )
            self.forwards.append(
                (pid, sig, "NOT forwarded, blocked here: " + _exc_tag(exc))
            )
            raise exc
        if not self._forward:
            self.forwards.append((pid, sig, "NOT forwarded (forward=False)"))
            return None
        try:
            result = self._real(pid, sig)
        except BaseException as exc:
            self.forwards.append((pid, sig, "raised " + _exc_tag(exc)))
            if pid != self._absorb:
                raise
            # The probe was made and the host's answer is on the record; the
            # target pid's refusal is answered as an accepted group kill so
            # the code under test proceeds past its own ``except OSError``.
            return None
        self.forwards.append((pid, sig, result))
        return result

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


def _spawn_sleeper(new_session: bool = False, seconds: str = "30", stdout=None):
    """A real ``sleep`` child; ``new_session`` makes it its own group leader."""
    return subprocess.Popen(
        ["sleep", seconds], start_new_session=new_session, stdout=stdout
    )


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


# ---------------------------------------------------------------------------
# 12. an UNREADABLE process state degrades toward ALIVE, never toward DEAD
# ---------------------------------------------------------------------------


class _NoProcfs:
    """Make the process state UNREADABLE -- and nothing else -- for ONE pid.

    FOUR injections, all scoped to ``pid``, in the order production code
    reaches them:

    1. ``os.getpgid(pid)`` answers ``pid``: the target is its OWN group leader,
       so ``_kill_decision`` returns ``("group", "process_group_leader")``.
    2. ``os.killpg(pid, SIGTERM)`` is performed for real by
       :class:`_RecordingKillpg`, which records this host's answer as the
       GROUP PROBE but absorbs the target pid's failure instead of re-raising
       it -- so production's ``except OSError`` fallback is not what ends the
       story on a host that refuses an all-corpse group.
    3. ``os.kill(pid, ...)`` succeeds for the liveness probe ``0`` AND for the
       ``SIGTERM`` the single-pid fallback would send, so that host cannot end
       the story one step later either.
    4. ``open`` INSIDE ``tools.workspace.worker_execution`` -- the module global
       that shadows the builtin for every function of that module -- raises the
       parametrised ``OSError`` for ``/proc/<pid>/stat``, counting attempts.

    (4) is the SUBJECT: ``_pid_is_alive`` keeps its ``os.kill(pid, 0)`` probe,
    its ``except OSError`` arm, ``_kill_decision``, ``_terminate_subprocess``,
    the ``kill_grace_seconds`` poll and the SIGKILL escalation as REAL
    production code -- only the one read a host without a usable procfs cannot
    perform is faked.  Patching ``_pid_is_alive``, ``_escalate_subprocess_kill``
    or the tracker itself would replace the very behaviour this test pins.

    (1), (2) and (3) are NOT the subject.  They are the three POSIX steps
    production runs BEFORE the read, and they are why this test used to prove
    NOTHING on macOS (``attempts == 0``, the reported ``assert 0 > 0``) while
    passing on Linux.  The story is NOT about ``os.getpgid``: (1) is faked, so
    ``_kill_decision`` answers ``("group", "process_group_leader")`` on Darwin
    exactly as on Linux (measured).  What a host whose target GROUP holds no
    live member cannot do is the GROUP PROBE (2) -- the only member of that
    group is a corpse, so the real ``os.killpg(pid, SIGTERM)`` raises ``ESRCH``
    (measured: ``killpg_calls=[(pid, SIGTERM)]`` and a ``forwards`` entry naming
    ``ProcessLookupError``).  Production catches ``OSError`` there, falls back
    to ``os.kill(pid, SIGTERM)``, which raises ``ESRCH`` too (3), and the outer
    handler returns BEFORE ``_escalate_subprocess_kill`` is ever entered:
    ``attempts == 0``, ``elapsed == 0.00s``.  Injecting all three is what lets
    the read be reached on every POSIX host, and what the escalation then did is
    now ASSERTED from the trace instead of assumed.  Every other pid, and every
    signal the fallback does not send, is forwarded to the real call.
    """

    def __init__(self, pid: int, error: type,
                 answer_signals: tuple = (0,)) -> None:
        self.pid = pid
        self.path = f"/proc/{pid}/stat"
        self.error = error
        self.attempts = 0
        # Which signals ``os.kill(pid, ...)`` ANSWERS for the target instead of
        # forwarding: ``0`` is the liveness probe, ``SIGTERM`` is the single-pid
        # fallback production takes when the group probe fails.
        self.answer_signals = tuple(answer_signals)
        # EVERY seeded call, injected or forwarded, in order:
        # ``(name, target, sig-or-None, result-or-exception-tag)``.  This is
        # what shows whether production ever reached ``kill(pid, 0)``, whether
        # the ``killpg -> OSError -> os.kill`` fallback fired, and what the
        # forwarded call did on this host.
        self.calls = []
        self._real_open = builtins.open
        self._real_getpgid = os.getpgid
        self._real_kill = os.kill

    # -- (4) the seam this test is about: the state read --------------------
    def open(self, file, *args, **kwargs):
        if str(file) == self.path:
            self.attempts += 1
            exc = self.error(self.path)
            self.calls.append(
                ("open", self.path, None, "raised " + _exc_tag(exc))
            )
            raise exc
        try:
            result = self._real_open(file, *args, **kwargs)
        except BaseException as exc:
            self.calls.append(
                ("open", _short(file, 60), None, "raised " + _exc_tag(exc))
            )
            raise
        self.calls.append(("open", _short(file, 60), None, "ok"))
        return result

    # -- (1)(2)(3) the POSIX steps that run BEFORE the read -----------------
    def getpgid(self, target):
        if target == self.pid:
            self.calls.append(
                ("getpgid", target, None,
                 "%r (INJECTED: own group leader)" % (self.pid,))
            )
            return self.pid  # its own group leader
        try:
            result = self._real_getpgid(target)
        except BaseException as exc:
            self.calls.append(
                ("getpgid", target, None, "raised " + _exc_tag(exc))
            )
            raise
        self.calls.append(("getpgid", target, None, result))
        return result

    def kill(self, target, sig):
        if target == self.pid and sig in self.answer_signals:
            if sig == 0:
                outcome = "ok (INJECTED: liveness probe)"
            else:
                outcome = (
                    "ok (INJECTED: group probe failed, the single-pid "
                    "fallback was accepted)"
                )
            self.calls.append(("kill", target, sig, outcome))
            return None  # signal 0 still finds it / the fallback is accepted
        try:
            result = self._real_kill(target, sig)
        except BaseException as exc:
            self.calls.append(("kill", target, sig, "raised " + _exc_tag(exc)))
            raise
        self.calls.append(("kill", target, sig, result))
        return result

    def install(self, monkeypatch) -> "_NoProcfs":
        monkeypatch.setattr(worker_execution_module, "open", self.open,
                            raising=False)
        monkeypatch.setattr(os, "getpgid", self.getpgid)
        monkeypatch.setattr(os, "kill", self.kill)
        return self


def _spawn_exited_child(new_session: bool = True):
    """A real child that has EXITED and is NOT reaped (a zombie).

    Reuses :func:`_spawn_sleeper`; the only addition is ``stdout=PIPE``.  EOF on
    that pipe is a REAP-FREE, platform-independent proof that the child exited:
    the write end closes when the process exits, and reading it never calls
    ``wait``.  ``proc.poll()`` / ``proc.wait()`` would reap the child, and
    ``_wait_for_zombie`` needs a procfs this test must not require.
    """
    proc = _spawn_sleeper(
        new_session=new_session, seconds="0.1", stdout=subprocess.PIPE
    )
    assert proc.stdout is not None
    assert proc.stdout.read() == b"", "the child wrote to the pipe before exiting"
    proc.stdout.close()
    return proc


@POSIX_ONLY
@pytest.mark.parametrize("error", [FileNotFoundError, PermissionError])
def test_unreadable_proc_state_degrades_toward_alive(monkeypatch, error):
    """CHARACTERISATION test (NOT red-first): unreadable state reads as ALIVE.

    This test documents behaviour that is already correct BY ACCIDENT.  It is
    not a bug reproduction and it did not start red: it is written so that it
    goes red only if that accident is later "tidied" in the wrong direction.

    The behaviour: ``worker_execution._pid_is_alive`` catches ``OSError`` around
    the ``/proc/<pid>/stat`` read and returns ``True`` -- "assume ALIVE".  That
    is the conservative direction, and it is what keeps teardown safe on a host
    with no usable procfs: a genuinely exited child still answers signal 0 (a
    zombie does), and its state cannot be read, so liveness must never be
    inferred as "dead" from an inability to measure.

    Pinned here, on a child that really has EXITED and is NOT reaped (exactly
    the zombie a ``Popen`` child becomes before ``wait()``), with the state read
    made unreadable at the seam production code already uses:

    (a) the escalation BURNS the whole grace period rather than concluding the
        target is dead, and
    (b) a kill IS still issued at the end of it -- teardown is not silently
        skipped, it SIGKILLs a corpse.

    So the price of "cannot measure" is a wasted grace period: never a live
    target left running, and never a false "already dead" that skips the kill.
    Only ``False`` from ``_pid_is_alive`` short-circuits the escalation, which
    is why mapping the unreadable case onto DEAD is observable in BOTH halves:
    it returns immediately (no burn) AND issues no SIGKILL.

    No capability guard is needed, but the injection is FOUR-fold, not one: the
    unreadable state read (the subject), plus the three POSIX steps that run
    BEFORE it is reached in group mode -- ``os.getpgid(pid)`` (the decision
    probe), ``os.killpg(gid, SIGTERM)`` (the group kill) and ``os.kill(pid, 0)``
    / ``os.kill(pid, SIGTERM)`` (the liveness probe and its single-pid fallback)
    -- answered for the target pid so that no host's view of a ZOMBIE can return
    before the read.  The measured reason is NOT ``os.getpgid``: that seam is
    faked, so ``_kill_decision`` answers ``("group", "process_group_leader")``
    on Darwin exactly as on Linux.  What a host cannot do is the GROUP PROBE --
    every member of the target group is a corpse, so the real ``os.killpg``
    raises ``ESRCH``; production catches ``OSError``, falls back to
    ``os.kill(pid, SIGTERM)``, which raises ``ESRCH`` too, and the outer handler
    returns before ``_escalate_subprocess_kill`` is ever entered (measured:
    ``attempts == 0``, ``elapsed == 0.00s``) -- which is exactly why this test
    used to prove NOTHING on macOS while passing on Linux.  ``_NoProcfs``
    carries the measured evidence.  ``POSIX_ONLY`` is not a capability skip for
    this case -- the whole file needs POSIX process groups (``os.killpg`` /
    ``os.getpgid``) to observe a group kill at all.

    Both ``OSError`` shapes are covered because they are two different absences:
    a Darwin host has no ``/proc`` at all (``FileNotFoundError``), while a
    present-but-unreadable entry raises ``PermissionError``.  Both reach the
    same ``except OSError`` arm.
    """
    logs = _Logs().install(monkeypatch)

    # The child comes FIRST: the group probe is answered for ITS pid, and the
    # killpg patch must be installed before the code under test calls it.
    proc = _spawn_exited_child(new_session=True)
    killpg = _RecordingKillpg(forward=True, absorb_failure_for=proc.pid)
    monkeypatch.setattr("os.killpg", killpg)
    try:
        no_procfs = _NoProcfs(
            proc.pid, error, answer_signals=(0, signal.SIGTERM)
        ).install(monkeypatch)

        grace = 1.0
        tracker = ExecutionTracker()
        tracker.kill_grace_seconds = grace
        tracker.add("e1", {"type": "subprocess", "pid": proc.pid})

        # -- DIAGNOSTIC instrumentation: installed BEFORE the call ---------
        # Pass-through recorders on the four methods the subprocess teardown
        # path uses (plus ``terminate_all`` as the boundary marker).  They must
        # be in place BEFORE ``terminate_all`` runs, which is the only reason
        # this part is not inside the ``DIAG_UNAVAILABLE`` block below; it is
        # guarded the same way and it changes no return value, argument or
        # exception, so the assertions see exactly what they would see alone.
        trace = _CallTrace()
        try:
            trace.install(tracker, monkeypatch)
        except Exception as exc:  # pragma: no cover - diagnostic only
            print("DIAG_UNAVAILABLE trace-install", repr(exc))

        started = time.monotonic()
        tracker.terminate_all("w1", None, None)
        elapsed = time.monotonic() - started

        # -- DIAGNOSTIC: snapshot the WHOLE teardown path, not just the seams
        # A host-dependent early return must name itself here instead of
        # surfacing as a bare ``assert 0 > 0`` elsewhere, and the seams alone
        # cannot say WHERE the path stopped: the call record, the decisions, the
        # forwarded signals and the log lines are what separate "never entered
        # the escalation" from "entered it and concluded the target was dead".
        #
        # Every reading is copied out FIRST, while the tracker is still in the
        # state production left it.  The probes at the end deliberately provoke
        # more calls through the same instrumented seams, so a later read would
        # be contaminated by the diagnostic's own traffic.
        try:
            attempts_so_far = no_procfs.attempts
            prod_calls = list(no_procfs.calls)
            prod_events = list(trace.events)
            prod_counts = trace.counts()
            prod_absent = trace.absent()
            prod_killpg = list(killpg.calls)
            prod_forwards = list(killpg.forwards)
            prod_sigkill = killpg.signalled(signal.SIGKILL)
            prod_entries = list(logs.entries)
            grace_attr = getattr(
                tracker, "kill_grace_seconds", "<ABSENT>"
            )

            def _probe(label, fn):
                """Ask a host question directly; never let it break the test."""
                try:
                    print(
                        "PROC_GUARD_DIAG probe %s -> %r" % (label, fn())
                    )
                except BaseException as exc:  # diagnostic only
                    print(
                        "PROC_GUARD_DIAG probe %s raised %s"
                        % (label, _exc_tag(exc))
                    )

            # ``monkeypatch`` stores a FRESH bound method per access, so ``is``
            # on ``no_procfs.getpgid`` is always False; ``__self__`` is the
            # identity that actually says WHICH object is installed.
            seams = {
                "os.getpgid": getattr(os.getpgid, "__self__", None)
                is no_procfs,
                "os.kill": getattr(os.kill, "__self__", None) is no_procfs,
                "module.open": getattr(
                    worker_execution_module.open, "__self__", None
                )
                is no_procfs,
            }
            print(
                "PROC_GUARD_DIAG platform=%s %s"
                % (platform.system(), platform.release())
            )
            print(
                "PROC_GUARD_DIAG python=%s os.name=%r procfs_mounted=%r"
                % (
                    platform.python_version(),
                    os.name,
                    _procfs_available(),
                )
            )
            print("PROC_GUARD_DIAG seams %r" % (seams,))
            print(
                "PROC_GUARD_DIAG trace_installed=%r "
                "trace_install_failures=%r"
                % (trace.installed, trace.failures)
            )
            print(
                "PROC_GUARD_DIAG grace attr=%r type=%s module_default=%r"
                % (
                    grace_attr,
                    type(grace_attr).__name__,
                    getattr(
                        worker_execution_module, "EXEC_KILL_GRACE", "<ABSENT>"
                    ),
                )
            )
            burned = (
                "yes (the poll sat out the grace period)"
                if elapsed >= grace * 0.9
                else "no (the path returned without polling)"
            )
            print(
                "PROC_GUARD_DIAG elapsed=%.4f grace=%.4f burned=%s"
                % (elapsed, grace, burned)
            )
            print(
                "PROC_GUARD_DIAG attempts=%r target_pid=%r self_pid=%r"
                % (attempts_so_far, proc.pid, os.getpid())
            )
            print("PROC_GUARD_DIAG trace_counts=%r" % (prod_counts,))
            print("PROC_GUARD_DIAG trace_absent=%r" % (prod_absent,))
            for depth, text in prod_events:
                print(
                    "PROC_GUARD_DIAG call %s%s" % ("  " * depth, text)
                )
            print(
                "PROC_GUARD_DIAG killpg_calls=%r sigkill_targets=%r"
                % (prod_killpg, prod_sigkill)
            )
            for fwd_pid, fwd_sig, fwd_outcome in prod_forwards:
                print(
                    "PROC_GUARD_DIAG killpg_forward pid=%r sig=%r -> %s"
                    % (fwd_pid, fwd_sig, fwd_outcome)
                )
            for call_index, call_entry in enumerate(prod_calls):
                print(
                    "PROC_GUARD_DIAG seam_call[%d] %r"
                    % (call_index, call_entry)
                )
            print(
                "PROC_GUARD_DIAG logs(%d) %r"
                % (len(prod_entries), [_short(entry) for entry in prod_entries])
            )
            for level, component, message in prod_entries:
                print(
                    "PROC_GUARD_DIAG log %s %s: %s"
                    % (level, component, _short(message, 200))
                )
            # -- probes: what does THIS host say, asked directly? ----------
            # These run LAST and through the real functions wherever possible,
            # so the recorded production traffic above stays unpolluted.
            print("PROC_GUARD_DIAG probes (asked after the snapshot above)")
            _probe(
                "decision(%r)" % (proc.pid,),
                lambda: tracker._kill_decision(proc.pid),
            )
            _probe(
                "real_getpgid(%r)" % (proc.pid,),
                lambda: no_procfs._real_getpgid(proc.pid),
            )
            _probe(
                "real_kill(%r, 0)" % (proc.pid,),
                lambda: no_procfs._real_kill(proc.pid, 0),
            )
            _probe(
                "real_proc_stat(%r)" % (proc.pid,),
                lambda: _read_proc_stat(proc.pid),
            )
            _probe("getpgid(0)", lambda: os.getpgid(0))
            _probe(
                "real_killpg",
                lambda: (
                    killpg._real,
                    getattr(killpg._real, "__module__", None),
                ),
            )
            _probe(
                "tracker_executions", lambda: dict(tracker._executions)
            )
            _probe("active_count", lambda: tracker.active_count())
            _probe("attempts_after_probes", lambda: no_procfs.attempts)
            print(
                "PROC_GUARD_DIAG trace_after_probes=%r"
                % (
                    [
                        text
                        for _depth, text in trace.events[len(prod_events):]
                    ],
                )
            )
        except Exception as exc:  # pragma: no cover - diagnostic only
            print("DIAG_UNAVAILABLE", repr(exc))

        # -- the evidence line, printed BEFORE any assertion -----------------
        # The answer to "why did nothing escalate?" on a Darwin run is here: the
        # REAL ``os.killpg`` refusing a group whose only member is a corpse.
        # Printed OUTSIDE the block above, so it survives a DIAG_UNAVAILABLE:
        # it is cheap and it IS the evidence.
        for _fwd_pid, _fwd_sig, _fwd_outcome in killpg.forwards:
            print(
                "PROC_GUARD_DIAG group_probe killpg(%r, %r) forward -> %s"
                % (_fwd_pid, _fwd_sig, _fwd_outcome)
            )

        assert no_procfs.attempts > 0, (
            "something returned before the process-state read was attempted "
            "-- PROC_GUARD_DIAG above names which seam (the patched "
            "os.getpgid / os.kill / os.killpg probes, or module-level open) -- "
            "so nothing was proved about the unreadable case "
            f"(trace={trace.counts()!r}, installed={trace.installed!r}, "
            f"failures={trace.failures!r})"
        )

        # The escalation must have RUN, not merely been reached by accident of
        # this host's answers: assert it from the trace instead of trusting it.
        measured = trace.counts()
        assert measured.get("_escalate_subprocess_kill", 0) >= 1, (
            "_escalate_subprocess_kill was never entered, so the unreadable "
            "read was not reached through the real production path on this "
            "host -- nothing about the degradation was pinned "
            f"(trace={measured!r}, installed={trace.installed!r}, "
            f"failures={trace.failures!r})"
        )
        assert measured.get("_pid_is_alive", 0) >= 1, (
            "the escalation was entered but never polled liveness, so the "
            "unreadable process-state read was never attempted: the "
            "degradation toward ALIVE was not exercised "
            f"(trace={measured!r}, installed={trace.installed!r}, "
            f"failures={trace.failures!r})"
        )
        assert elapsed >= grace * 0.9, (
            f"escalation returned after {elapsed:.2f}s on an UNREADABLE state: "
            f"it did not burn the {grace}s grace period, so an unmeasurable "
            "process was degraded toward DEAD instead of toward ALIVE"
        )
        assert elapsed < grace + 2.0, (
            f"teardown took {elapsed:.2f}s for a {grace}s grace period: the "
            "poll is not bounded by kill_grace_seconds"
        )
        assert proc.pid in killpg.signalled(signal.SIGKILL), (
            "no SIGKILL was issued after the grace period: an unreadable state "
            f"silently skipped the escalation ({killpg.calls!r})"
        )
        assert any(
            "still alive after" in message
            for _level, _component, message in logs.entries
        ), (
            "the escalation did not report the burned grace period: "
            f"{logs.entries!r}"
        )
        assert tracker.active_count() == 0
    finally:
        _reap(proc)
