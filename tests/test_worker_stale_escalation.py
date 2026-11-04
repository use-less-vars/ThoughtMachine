# tests/test_worker_stale_escalation.py
"""S7c-3 stale-worker escalation-ladder tests.

The ladder, and the invariant the order encodes:

 1. test_answered_ping_aborts_the_ladder            alive-but-busy is untouched
 2. test_cooperative_cancel_ends_the_ladder         cancel cooperated -> no kill
 3. test_refused_cancel_counts_as_cooperation       "nothing running" is cooperation
 4. test_uncooperative_cancel_escalates_to_kill     maybe-kill is the last rung
 5. test_probe_that_raises_is_an_unanswered_probe   an error is not an answer
 6. test_thread_mode_worker_is_announced_only       no ping seam -> no action
 7. test_worker_without_handle_is_announced_only    no handle -> no action
 8. test_ping_is_reached_through_the_process_runtime handle without ping() today
 9. test_tick_does_not_wait_for_the_episode         out-of-band, tracked daemon
10. test_one_episode_per_episode                    a second tick opens no second thread
11. test_wedged_kill_is_bounded_by_the_kill_timeout  kill budget is real
12. test_kill_failure_is_a_verdict_not_an_escape    kill raising is reported
13. test_ladder_budgets_default_to_the_shipped_constants
14. test_no_runtime_handle_is_announced_never_acted_on  unspawned: no rung
15. test_refused_cancel_stops_the_ladder           refused == already stopped
16. test_thread_mode_handle_never_armed            ping -> None, still no rung
17. test_handle_ping_is_tri_state_and_never_spawns  the verb behind all this
18. test_stop_aborts_and_joins_an_in_flight_ladder  a stop ends the episode

Hermetic: no Docker, no LLM, no real runtime. Every episode runs against a
recording double, so "an answered ping touched nothing" is an assertion rather
than a comment. Budgets are shortened per instance (a few tens of ms) so the
whole file runs in well under a second.

Run:  python -m pytest tests/test_worker_stale_escalation.py -q
"""

import inspect
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.config.defaults import (  # noqa: E402
    WORKER_STALE_CANCEL_TIMEOUT_S,
    WORKER_STALE_KILL_TIMEOUT_S,
    WORKER_STALE_PROBE_TIMEOUT_S,
)
from tools.workspace.worker_runtime import (  # noqa: E402
    CANCEL_TERMINAL_STATUSES,
    MODE_PROCESS,
    WorkerRuntimeHandle,
)
import tools.workspace.worker_stale_watchdog as watchdog_module  # noqa: E402
from tools.workspace.worker_stale_watchdog import WorkerStaleWatchdog  # noqa: E402

# Long enough that no background tick can race a test's own explicit tick().
_NO_TICKS = 3600.0
_KEY = ("s1", "w1", 1)


class _FakeRegistry:
    """Minimal ``get_all_workers()`` store (the only method the scan uses)."""

    def __init__(self, workers=None):
        self._workers = dict(workers or {})

    def get_all_workers(self):
        return dict(self._workers)


class _State:
    """The ``WorkerState`` shape the handle's ``state()`` answers with."""

    def __init__(self, status):
        self.status = status


class _PoisonWorker:
    """A busy worker whose every termination method records a call."""

    def __init__(self, worker_name="w1", session_id="s1", instance_id=1):
        self.worker_name = worker_name
        self.session_id = session_id
        self.instance_id = instance_id
        self.status = "busy"
        self.current_task = "long tool call"
        self.poison_calls = []

    def _record(self, name):
        self.poison_calls.append(name)

    def kill(self, *args, **kwargs):
        self._record("kill")

    def stop(self, *args, **kwargs):
        self._record("stop")

    def cancel(self, *args, **kwargs):
        self._record("cancel")

    def interrupt(self, *args, **kwargs):
        self._record("interrupt")

    def terminate_all(self, *args, **kwargs):
        self._record("terminate_all")

    def terminate_executions(self, *args, **kwargs):
        self._record("terminate_executions")


class _LadderHandle:
    """A stale handle exposing the three ladder seams and recording each call.

    ``_last_heartbeat_at`` is far in the past, so the scan announces it; from
    there the ladder's behaviour is entirely this double's configuration.
    """

    def __init__(
        self,
        *,
        ping=True,
        cancel="accepted",
        stop_on_cancel=True,
        kill_raises=False,
        status="busy",
        age=1000.0,
    ):
        self._last_heartbeat_at = time.time() - age
        self._last_received_at = None
        self.status = status
        self.ping_answer = ping
        self.cancel_verdict = cancel
        self.stop_on_cancel = stop_on_cancel
        self.kill_raises = kill_raises
        self.calls = []
        self.ping_timeouts = []
        # Set by a test that wants the probe to hang until it says so.
        self.ping_gate = None
        # Set by a test that wants a kill that never returns.
        self.kill_gate = None

    # -- the ladder's seams --------------------------------------------------

    def ping(self, *, timeout=None):
        self.calls.append("ping")
        self.ping_timeouts.append(timeout)
        if self.ping_gate is not None:
            self.ping_gate.wait(5.0)
        return self.ping_answer

    def cancel(self):
        self.calls.append("cancel")
        if self.stop_on_cancel:
            self.status = "ready"
        return self.cancel_verdict

    def kill(self):
        self.calls.append("kill")
        if self.kill_gate is not None:
            self.kill_gate.wait(5.0)
        if self.kill_raises:
            raise RuntimeError("kill blew up")
        self.status = "killed"
        return True

    def state(self):
        return _State(self.status)


class _PingOnlyRuntime:
    """The runtime object a process-mode handle would hold; only ``ping`` here."""

    def __init__(self, answer=True):
        self.answer = answer
        self.ping_timeouts = []

    def ping(self, *, timeout=None):
        self.ping_timeouts.append(timeout)
        return self.answer


class _ProcessModeHandle:
    """A process-mode handle: its own tri-state ``ping`` over a live runtime.

    This is today's production shape -- ``WorkerRuntimeHandle.ping`` (S7c-3)
    answers through the runtime the handle built lazily, so the ladder asks the
    handle and the runtime is reached only from inside it.
    """

    def __init__(self, runtime):
        self._last_heartbeat_at = time.time() - 1000.0
        self._last_received_at = None
        self._process_runtime = runtime
        self.status = "busy"
        self.calls = []

    def ping(self, timeout=None):
        runtime = self._process_runtime
        if not runtime:
            return None
        return bool(runtime.ping(timeout=timeout))

    def cancel(self):
        self.calls.append("cancel")
        self.status = "ready"
        return "accepted"

    def kill(self):
        self.calls.append("kill")
        self.status = "killed"
        return True

    def state(self):
        return _State(self.status)


class _ThreadModeHandle:
    """A thread-mode handle: clocks, state, cancel/kill -- and no runtime.

    Its ``ping`` is the real verb's thread-mode answer: ``None``, "there is
    nothing to ask", which is not a silence to escalate on.
    """

    def __init__(self):
        self._last_heartbeat_at = time.time() - 1000.0
        self._last_received_at = None
        self._process_runtime = None
        self.status = "busy"
        self.calls = []
        self.ping_calls = []

    def ping(self, timeout=None):
        self.ping_calls.append(timeout)
        return None

    def cancel(self):
        self.calls.append("cancel")
        return "accepted"

    def kill(self):
        self.calls.append("kill")
        return True

    def state(self):
        return _State(self.status)


def _watchdog(handle, resolver=None, worker=None, **kwargs):
    worker = worker if worker is not None else _PoisonWorker()
    registry = _FakeRegistry({_KEY: worker})
    budgets = dict(
        probe_timeout=0.03,
        cancel_timeout=0.05,
        kill_timeout=1.0,
        poll_interval=0.005,
    )
    budgets.update(kwargs)
    watchdog = WorkerStaleWatchdog(
        interval=_NO_TICKS,
        announce_after=90.0,
        registry_provider=lambda: registry,
        observer_provider=lambda: None,
        handle_resolver=resolver if resolver is not None else (lambda *a: handle),
        **budgets,
    )
    return watchdog, registry, worker


def _episode(handle, **kwargs):
    """Run one tick and return ``(watchdog, worker, episode record)``."""
    watchdog, _registry, worker = _watchdog(handle, **kwargs)
    announcements = watchdog.tick()
    assert len(announcements) == 1, announcements  # the episode always announces
    record = watchdog.wait_for_escalation(_KEY, timeout=10.0)
    assert record is not None, "the episode must record a verdict"
    return watchdog, worker, record


# -- 1 -----------------------------------------------------------------------


def test_answered_ping_aborts_the_ladder():
    """THE invariant: an answered ping means alive-but-busy, so nothing happens.

    The central safety property of the whole ladder. A busy worker is the
    ordinary case; acting on an age reading would turn a heuristic into an
    outage, so the episode must stop at the ping and take nothing away.
    """
    handle = _LadderHandle(ping=True)
    worker = _PoisonWorker()

    _watchdog_obj, _worker, record = _episode(handle, worker=worker)

    assert record["outcome"] == "answered"
    assert record["steps"] == ["ping"]  # the ladder stopped at the first rung
    assert handle.calls == ["ping"]
    assert handle.status == "busy", "a busy worker must stay busy"
    assert worker.poison_calls == [], "the announced thread must be untouched"
    assert worker.status == "busy"
    # The probe was asked with the ladder's own budget, not a literal.
    assert handle.ping_timeouts == [pytest.approx(0.03)]


# -- 2 -----------------------------------------------------------------------


def test_cooperative_cancel_ends_the_ladder():
    """An unanswered ping + a cancel the worker honours never reaches kill."""
    handle = _LadderHandle(ping=False, cancel="accepted", stop_on_cancel=True)

    _watchdog_obj, _worker, record = _episode(handle)

    assert record["steps"] == ["ping", "cancel"]
    assert record["outcome"] == "cancelled"
    assert handle.calls == ["ping", "cancel"]
    assert handle.status == "ready"
    assert "kill" not in handle.calls


# -- 3 -----------------------------------------------------------------------


def test_refused_cancel_counts_as_cooperation():
    """``refused`` is the worker saying nothing was running: cooperation."""
    handle = _LadderHandle(ping=False, cancel="refused", stop_on_cancel=False)

    _watchdog_obj, _worker, record = _episode(handle)

    assert record["steps"] == ["ping", "cancel"]
    assert record["outcome"] == "cancelled"
    assert "kill" not in handle.calls


# -- 4 -----------------------------------------------------------------------


def test_uncooperative_cancel_escalates_to_kill():
    """A cancel the worker ignores for the whole window reaches ``kill``."""
    handle = _LadderHandle(ping=False, cancel="accepted", stop_on_cancel=False)

    _watchdog_obj, _worker, record = _episode(handle, cancel_timeout=0.05)

    assert record["steps"] == ["ping", "cancel", "kill"]
    assert record["outcome"] == "killed"
    assert handle.calls == ["ping", "cancel", "kill"]
    assert handle.status == "killed"


# -- 5 -----------------------------------------------------------------------


def test_probe_that_raises_is_an_unanswered_probe():
    """A probe that raises is no answer: the ladder continues, it never escapes."""

    class _Exploding(_LadderHandle):
        def ping(self, *, timeout=None):
            self.calls.append("ping")
            raise RuntimeError("probe blew up")

    handle = _Exploding(ping=False, stop_on_cancel=True)

    _watchdog_obj, _worker, record = _episode(handle)

    assert record["steps"] == ["ping", "cancel"]
    assert record["outcome"] == "cancelled"


# -- 6 -----------------------------------------------------------------------


def test_thread_mode_worker_is_announced_only():
    """No runtime to ask -> announced, and every termination seam untouched."""
    handle = _ThreadModeHandle()

    _watchdog_obj, worker, record = _episode(handle)

    assert record["outcome"] == "no_runtime"
    assert record["steps"] == []  # the ladder never took its first rung
    assert handle.calls == []
    assert handle.status == "busy", "a thread worker must not be touched"
    assert worker.poison_calls == []


# -- 7 -----------------------------------------------------------------------


class _RecordingObserver:
    """Observer double: counts scans, answers a fixed newly-stale count."""

    def __init__(self, newly_stale=1):
        self.newly_stale = newly_stale
        self.scans = 0

    def check_stale_transitions(self, now=None):
        self.scans += 1
        return self.newly_stale


def test_worker_without_handle_is_announced_only():
    """No handle means no frame evidence, so the ladder is never even offered.

    A worker the resolver cannot see is announced by the OBSERVER pass (its
    in-process heartbeat reading) and by nothing else: ``_frame_age`` answers
    ``None`` for it -- absence of evidence is not an age -- so no episode is
    started and no termination seam is reached.
    """
    observer = _RecordingObserver(newly_stale=1)
    watchdog, _registry, worker = _watchdog(None)
    watchdog._observer_provider = lambda: observer

    announcements = watchdog.tick()

    assert [a["kind"] for a in announcements] == ["observer_stale"]
    assert observer.scans == 1
    assert watchdog._escalations == {}, "no handle, so no episode"
    assert watchdog.last_episode(_KEY) is None
    assert worker.poison_calls == []
    assert worker.status == "busy"


def test_resolver_that_raises_is_no_handle():
    """A lookup that blows up is "no evidence", not a crash in the episode."""

    def exploding(*_args):
        raise RuntimeError("lookup blew up")

    watchdog, _registry, worker = _watchdog(None, resolver=exploding)

    assert watchdog.tick() == []
    assert watchdog._escalations == {}
    assert worker.poison_calls == []

    # The episode's own degenerate branch: a handle that disappeared between
    # the scan and the episode is "no_handle", never an action.
    watchdog._escalate(_KEY, "s1", "w1", 1)
    assert watchdog.last_episode(_KEY)["outcome"] == "no_handle"
    assert watchdog.last_episode(_KEY)["steps"] == []
    assert worker.poison_calls == []


# -- 8 -----------------------------------------------------------------------


def test_ping_is_reached_through_the_process_runtime():
    """A process-mode handle answers the probe through its live runtime."""
    runtime = _PingOnlyRuntime(answer=True)
    handle = _ProcessModeHandle(runtime)

    _watchdog_obj, _worker, record = _episode(handle)

    assert record["outcome"] == "answered"
    assert runtime.ping_timeouts == [pytest.approx(0.03)]
    assert handle.calls == [], "an answered ping must not cancel or kill"

    # ... and the unanswered case walks down the same seam.
    silent = _ProcessModeHandle(_PingOnlyRuntime(answer=False))
    _watchdog_obj, _worker, record = _episode(silent)
    assert record["steps"] == ["ping", "cancel"]
    assert record["outcome"] == "cancelled"


# -- 9 -----------------------------------------------------------------------


def test_tick_does_not_wait_for_the_episode():
    """The 30s scan loop hands the episode off; it never waits on a probe."""
    handle = _LadderHandle(ping=True)
    handle.ping_gate = threading.Event()  # the probe hangs until released
    watchdog, _registry, _worker = _watchdog(handle, probe_timeout=5.0)

    started = time.monotonic()
    announcements = watchdog.tick()
    tick_elapsed = time.monotonic() - started

    try:
        assert len(announcements) == 1
        assert tick_elapsed < 1.0, (
            f"tick() waited {tick_elapsed:.2f}s on the probe; the episode must "
            f"run out-of-band"
        )
        thread = watchdog._escalations[_KEY]
        assert thread.is_alive(), "the episode is running in its own thread"
        assert thread.daemon is True, "an episode must never hold the process up"
        assert thread.name == "worker-stale-escalation:w1"
        assert watchdog.last_episode(_KEY) is None, "no verdict while it is asking"
    finally:
        handle.ping_gate.set()

    record = watchdog.wait_for_escalation(_KEY, timeout=10.0)
    assert record["outcome"] == "answered"
    assert handle.calls == ["ping"]


# -- 10 ----------------------------------------------------------------------


def test_one_episode_per_episode():
    """A second tick while an episode is still asking opens no second thread."""
    handle = _LadderHandle(ping=True)
    handle.ping_gate = threading.Event()
    watchdog, _registry, _worker = _watchdog(handle, probe_timeout=5.0)

    try:
        watchdog.tick()
        watchdog.tick()
        watchdog.tick()
        live = [
            t
            for t in threading.enumerate()
            if t.name == "worker-stale-escalation:w1" and t.is_alive()
        ]
        assert len(live) == 1, f"{len(live)} episodes racing one answer"
    finally:
        handle.ping_gate.set()

    assert watchdog.wait_for_escalation(_KEY, timeout=10.0)["outcome"] == "answered"
    assert handle.calls == ["ping"], "one question, one ping"

    # A NEW stale episode (fresh, then stale again) opens a new one.
    handle._last_heartbeat_at = time.time()
    assert watchdog.tick() == []
    handle._last_heartbeat_at = time.time() - 1000.0
    assert len(watchdog.tick()) == 1
    assert watchdog.wait_for_escalation(_KEY, timeout=10.0) is not None
    assert handle.calls == ["ping", "ping"]


# -- 11 ----------------------------------------------------------------------


def test_wedged_kill_is_bounded_by_the_kill_timeout():
    """A kill that never returns is reported at the budget, not waited out."""
    handle = _LadderHandle(ping=False, cancel="accepted", stop_on_cancel=False)
    handle.kill_gate = threading.Event()

    started = time.monotonic()
    _watchdog_obj, _worker, record = _episode(
        handle, cancel_timeout=0.02, kill_timeout=0.05
    )
    elapsed = time.monotonic() - started

    try:
        assert record["outcome"] == "kill_timeout"
        assert record["steps"] == ["ping", "cancel", "kill"]
        assert elapsed < 5.0, "the kill budget must bound the episode"
        assert record["kill_timeout_s"] == pytest.approx(0.05)
    finally:
        handle.kill_gate.set()


# -- 12 ----------------------------------------------------------------------


def test_kill_failure_is_a_verdict_not_an_escape():
    """A raising kill is reported as ``kill_failed``; the episode never raises."""
    handle = _LadderHandle(
        ping=False, cancel="accepted", stop_on_cancel=False, kill_raises=True
    )

    _watchdog_obj, _worker, record = _episode(handle)

    assert record["outcome"] == "kill_failed"
    assert record["steps"] == ["ping", "cancel", "kill"]
    assert handle.calls == ["ping", "cancel", "kill"]


# -- 13 ----------------------------------------------------------------------


# -- 14 ----------------------------------------------------------------------


class _UnspawnedProcessHandle:
    """A process-mode handle that has never dispatched, so owns no runtime.

    Mirrors ``WorkerRuntimeHandle``: ``ping`` reads the lazily built runtime and
    answers ``None`` when there is none. The builder that would create one is a
    live trap -- a probe that reaches it fails the episode loudly instead of
    quietly starting the process it was asking about.
    """

    def __init__(self):
        self._last_heartbeat_at = time.time() - 1000.0
        self._last_received_at = None
        self._process_runtime = None
        self.status = "busy"
        self.calls = []
        self.ping_calls = []
        self.spawn_calls = 0

    def ping(self, timeout=None):
        self.ping_calls.append(timeout)
        runtime = self._process_runtime
        if not runtime:
            return None
        return bool(runtime.ping(timeout=timeout))

    def _ensure_process_runtime(self):
        self.spawn_calls += 1
        raise AssertionError("a liveness probe must never spawn a runtime")

    def cancel(self):
        self.calls.append("cancel")
        return "accepted"

    def kill(self):
        self.calls.append("kill")
        return True

    def state(self):
        return _State(self.status)


def test_no_runtime_handle_is_announced_never_acted_on():
    """A process-mode worker that has not dispatched has nothing to ask.

    The probe must say so (``None``) and the episode must end at that verdict:
    no cancel, no kill, and -- the point of the whole tri-state -- no spawn. A
    stale reading is not evidence that starting a process is the right move.
    """
    handle = _UnspawnedProcessHandle()

    _watchdog_obj, worker, record = _episode(handle)

    assert record["outcome"] == "no_runtime"
    assert record["steps"] == [], "the ladder never took its first rung"
    assert handle.calls == [], "nothing may be cancelled or killed"
    assert handle.spawn_calls == 0, "asking must never build the runtime"
    assert handle.ping_calls == [pytest.approx(0.03)], "one question, no answer"
    assert handle.status == "busy"
    assert worker.poison_calls == []


# -- 15 ----------------------------------------------------------------------


def test_refused_cancel_stops_the_ladder():
    """``refused`` means "nothing was running": cooperation, never a kill.

    ``WorkerRuntimeHandle.cancel`` refuses exactly when the status is in
    ``CANCEL_TERMINAL_STATUSES`` -- killed, detaching, detached, error,
    completed -- so a refusal is the worker saying it already stopped. Escalating
    on it would terminate an idle worker for the crime of being idle. This
    SUPERSEDES any reading of the ladder in which a refusal escalates.
    """
    handle = _LadderHandle(ping=False, cancel="refused", stop_on_cancel=False)

    _watchdog_obj, worker, record = _episode(handle)

    assert record["steps"] == ["ping", "cancel"]
    assert record["outcome"] == "cancelled"
    assert "kill" not in handle.calls, "refusal is cooperation, not defiance"
    assert worker.poison_calls == []
    # ... and the equivalence the docstring leans on is the real one.
    assert {
        "killed",
        "detaching",
        "detached",
        "error",
        "completed",
    } == set(CANCEL_TERMINAL_STATUSES)


# -- 16 ----------------------------------------------------------------------


def test_thread_mode_handle_never_armed():
    """A thread worker answers the probe with "there is nothing to ask".

    Thread mode has no process to lose, so ``ping`` is ``None`` and the episode
    ends at ``no_runtime``: no rung recorded, no cancel, no kill. The probe is
    asked once (that is how the verb learns there is no runtime); everything
    past it must stay untouched.
    """
    handle = _ThreadModeHandle()

    _watchdog_obj, worker, record = _episode(handle)

    assert record["outcome"] == "no_runtime"
    assert record["steps"] == []
    assert handle.calls == [], "no cancel, no kill"
    assert handle.ping_calls == [pytest.approx(0.03)]
    assert handle.status == "busy"
    assert worker.poison_calls == []


# -- 17 ----------------------------------------------------------------------


def test_handle_ping_is_tri_state_and_never_spawns():
    """The verb the ladder stands on: ``None`` / ``True`` / ``False``, no raise."""
    handle = WorkerRuntimeHandle(object(), mode=MODE_PROCESS)
    spawns = []

    def _explode():
        spawns.append("spawn")
        raise AssertionError("ping must never spawn the process runtime")

    handle._ensure_process_runtime = _explode

    # No live runtime: "nothing to ask", in either calling convention.
    assert handle.ping() is None
    assert handle.ping(timeout=0.01) is None
    assert spawns == []

    # A live runtime answers the question, with the caller's own budget.
    runtime = _PingOnlyRuntime(answer=True)
    handle._process_runtime = runtime
    assert handle.ping(timeout=0.05) is True
    assert runtime.ping_timeouts == [pytest.approx(0.05)]
    assert handle.ping() is True  # no budget -> the runtime's own default
    assert runtime.ping_timeouts == [pytest.approx(0.05), None]

    # A silent runtime is False; a raising one is still not an exception.
    handle._process_runtime = _PingOnlyRuntime(answer=False)
    assert handle.ping(timeout=0.01) is False

    class _Boom:
        def ping(self, *, timeout=None):
            raise RuntimeError("the pipe is gone")

    handle._process_runtime = _Boom()
    assert handle.ping(timeout=0.01) is False
    assert spawns == [], "nothing above ever built a runtime"


# -- 18 ----------------------------------------------------------------------


def test_stop_aborts_and_joins_an_in_flight_ladder():
    """A stop aborts the episode in front of it, and leaves none behind.

    The episode is genuinely inside ``_wait_for_turn_stop`` when the stop lands:
    cancel is accepted (so the ACK is not the answer), the status stays ``busy``
    (so the window cannot close early) and the window is 3s -- an order of
    magnitude more than the assertions allow, so "prompt" cannot be luck. The
    real module-level ``stop_watchdog()`` is exercised by installing this
    instance as the singleton (it is started, so it can be joined), which is the
    seam production shutdown uses; nothing else in this file touches the global.
    """
    handle = _LadderHandle(ping=False, cancel="accepted", stop_on_cancel=False)
    entered = threading.Event()
    accept = handle.cancel

    def _cancel():
        verdict = accept()
        entered.set()  # the ladder is now waiting on the turn, not the ack
        return verdict

    handle.cancel = _cancel
    watchdog, _registry, _worker = _watchdog(handle, cancel_timeout=3.0)
    watchdog.start()  # the real stop_watchdog() joins the scan thread
    watchdog_module._WATCHDOG = watchdog

    try:
        assert len(watchdog.tick()) == 1
        assert entered.wait(2.0), "the ladder never reached its cancel rung"
        episode = watchdog._escalations[_KEY]
        assert episode.is_alive(), "the episode must be mid-flight"
        assert watchdog.last_episode(_KEY) is None, "no verdict while it waits"

        started = time.monotonic()
        assert watchdog_module.stop_watchdog(timeout=2.0) is True
        elapsed = time.monotonic() - started
    finally:
        handle.kill_gate = None
        watchdog_module._WATCHDOG = None
        watchdog.stop()

    record = watchdog.last_episode(_KEY)
    assert record is not None, "a stopped episode still records a verdict"
    assert record["outcome"] == "stopped"
    assert record["steps"] == ["ping", "cancel"]
    assert "kill" not in handle.calls, "a stop must never be followed by a kill"
    assert handle.status == "busy", "nothing may be terminated after a stop"
    assert not episode.is_alive(), "no episode may outlive the stop"
    assert not watchdog.is_alive(), "nor the scan thread"
    assert elapsed < 2.0, (
        f"the stop rode out the 3.0s cancel window ({elapsed:.2f}s); it must "
        f"interrupt the wait, not wait for it"
    )


# -- 19 ----------------------------------------------------------------------


def test_ladder_budgets_default_to_the_shipped_constants():
    """The three rungs are the constants table's, not literals at a call site."""
    params = inspect.signature(WorkerStaleWatchdog.__init__).parameters
    assert params["probe_timeout"].default == WORKER_STALE_PROBE_TIMEOUT_S
    assert params["cancel_timeout"].default == WORKER_STALE_CANCEL_TIMEOUT_S
    assert params["kill_timeout"].default == WORKER_STALE_KILL_TIMEOUT_S
    assert (WORKER_STALE_PROBE_TIMEOUT_S, WORKER_STALE_CANCEL_TIMEOUT_S) == (15, 15)
    assert WORKER_STALE_KILL_TIMEOUT_S == 15


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
