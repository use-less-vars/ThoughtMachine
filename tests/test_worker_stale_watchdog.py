# tests/test_worker_stale_watchdog.py
"""S7c phase-A stale-watchdog tests -- the six names the ticket fixed, plus
 two regression guards the review pass added:

 1. test_ensure_watchdog_start_is_idempotent
 2. test_stale_frame_announced_exactly_once
 3. test_unknown_frame_age_is_never_announced
 4. test_heartbeating_worker_is_never_announced
 5. test_watchdog_announces_but_never_kills_cancels_or_interrupts
 6. test_stale_watchdog_constants_ship_announce_only_defaults
 7. test_default_observer_publishes_on_a_private_bus
 8. test_departed_worker_is_forgotten_and_can_announce_again

Hermetic: no Docker, no LLM, no real WorkerRegistry. Every worker is a fake
whose termination methods record calls, so "announce only" is an assertion
rather than a comment.

Run:  python -m pytest tests/test_worker_stale_watchdog.py -q
"""

import ast
import inspect
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tools.workspace.worker_stale_watchdog as watchdog_module  # noqa: E402
from agent.config.defaults import (  # noqa: E402
    HEARTBEAT_INTERVAL_S,
    HEARTBEAT_STALE_AFTER_S,
    WORKER_HUNG_GRACE_S,
    WORKER_STALE_ANNOUNCE_AFTER_S,
    WORKER_STALE_CANCEL_TIMEOUT_S,
    WORKER_STALE_KILL_TIMEOUT_S,
    WORKER_STALE_PROBE_TIMEOUT_S,
    WORKER_STALE_WATCHDOG_INTERVAL_S,
)
from tools.workspace.worker_manager import PeriodicStaleCheck, WorkerManager  # noqa: E402
from tools.workspace.worker_stale_watchdog import (  # noqa: E402
    WorkerStaleWatchdog,
    ensure_watchdog,
    stop_watchdog,
)

# Long enough that no background tick can race a test's own explicit tick().
_NO_TICKS = 3600.0


class _FakeRegistry:
    """Minimal ``get_all_workers()`` store (the only method the scan uses)."""

    def __init__(self, workers=None):
        self._workers = dict(workers or {})

    def get_all_workers(self):
        return dict(self._workers)


class _FakeHandle:
    """Stands in for ``WorkerRuntimeHandle``: only its two cached clocks."""

    def __init__(self, heartbeat_at=None, received_at=None):
        self._last_heartbeat_at = heartbeat_at
        self._last_received_at = received_at


class _PoisonWorker:
    """A busy worker whose every termination method records a call.

    Nothing in phase A may reach one of these; the tests assert the list stays
    empty rather than trusting a comment.
    """

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

    def _terminate_tracked_executions(self, *args, **kwargs):
        self._record("_terminate_tracked_executions")

    def terminate_executions(self, *args, **kwargs):
        self._record("terminate_executions")


class _RecordingObserver:
    """Observer double: counts scan calls, returns a fixed newly-stale count."""

    def __init__(self, newly_stale=0):
        self.newly_stale = newly_stale
        self.scans = 0

    def check_stale_transitions(self, now=None):
        self.scans += 1
        return self.newly_stale


def _stale_watchdog(registry, resolver, observer=None, callback=None, after=90.0):
    return WorkerStaleWatchdog(
        interval=_NO_TICKS,
        announce_after=after,
        registry_provider=lambda: registry,
        observer_provider=lambda: observer,
        handle_resolver=resolver,
        announce_callback=callback,
    )


@pytest.fixture(autouse=True)
def _isolate_announce_observer():
    """Keep the process-wide announce observer out of unrelated tests.

    Test 1 starts the real watchdog, and ``_default_observer_provider`` caches a
    *subscribed* ``WorkerLifecycleObserver`` in a module global. Restoring the
    pre-test value afterwards drops whatever a test created, so no later test
    inherits a live observer it never asked for.
    """
    saved = watchdog_module._ANNOUNCE_OBSERVER
    watchdog_module._ANNOUNCE_OBSERVER = None
    try:
        yield
    finally:
        watchdog_module._ANNOUNCE_OBSERVER = saved


# -- 1 -----------------------------------------------------------------------


def test_ensure_watchdog_start_is_idempotent():
    """Two starts are one thread; ``start_stale_check`` is idempotent too."""
    stop_watchdog(timeout=5.0)
    first = ensure_watchdog(interval=_NO_TICKS)
    second = ensure_watchdog(interval=_NO_TICKS)
    try:
        assert first is second
        assert first.is_alive()
        named = [t for t in threading.enumerate() if t.name == "worker-stale-watchdog"]
        assert len(named) == 1
    finally:
        assert stop_watchdog(timeout=5.0) is True
    assert not first.is_alive()
    # A second stop is "already gone", not an error.
    assert stop_watchdog(timeout=5.0) is False

    # WorkerManager.start_stale_check: the periodic (interrupting) checker is
    # started at most once per manager.
    manager = WorkerManager(registry=_FakeRegistry())
    checker = manager.start_stale_check(interval=_NO_TICKS)
    try:
        assert isinstance(checker, PeriodicStaleCheck)
        assert manager.start_stale_check(interval=_NO_TICKS) is checker
    finally:
        checker.stop()
        checker.join(timeout=5.0)
    assert not checker.is_alive()


# -- 2 -----------------------------------------------------------------------


def test_stale_frame_announced_exactly_once():
    """A stale cached frame announces once, not once per tick."""
    announced = []
    worker = _PoisonWorker()
    registry = _FakeRegistry({("s1", "w1", 1): worker})
    handle = _FakeHandle(heartbeat_at=time.time() - 1000.0)
    watchdog = _stale_watchdog(
        registry,
        lambda session_id, worker_name, instance_id: handle,
        callback=announced.append,
    )

    first = watchdog.tick()
    second = watchdog.tick()
    third = watchdog.tick()

    assert len(first) == 1
    assert first[0]["kind"] == "frame_stale"
    assert first[0]["worker_name"] == "w1"
    assert first[0]["session_id"] == "s1"
    assert first[0]["age_seconds"] >= 999.0
    assert second == [] and third == []
    assert len(announced) == 1

    # A worker that comes back fresh starts a NEW episode, and is announced
    # again when it goes quiet a second time.
    handle._last_heartbeat_at = time.time()
    assert watchdog.tick() == []
    handle._last_heartbeat_at = time.time() - 1000.0
    assert len(watchdog.tick()) == 1


# -- 3 -----------------------------------------------------------------------


def test_unknown_frame_age_is_never_announced():
    """No frame evidence is never an announcement (``None`` is not an age)."""
    announced = []
    registry = _FakeRegistry({("s1", "w1", 1): _PoisonWorker()})

    # (a) no handle for the worker at all.
    no_handle = _stale_watchdog(registry, lambda *a: None, callback=announced.append)
    assert no_handle.tick() == []

    # (b) a handle that has cached no frame yet.
    blank = _stale_watchdog(registry, lambda *a: _FakeHandle(), callback=announced.append)
    assert blank.tick() == []

    # (c) a resolver that raises is "unknown", not a crash.
    def exploding(*_args):
        raise RuntimeError("lookup blew up")

    assert _stale_watchdog(registry, exploding).tick() == []
    assert announced == []


# -- 4 -----------------------------------------------------------------------


def test_heartbeating_worker_is_never_announced():
    """A worker whose frames keep arriving is never announced."""
    announced = []
    worker = _PoisonWorker()
    registry = _FakeRegistry({("s1", "w1", 1): worker})
    handle = _FakeHandle(heartbeat_at=time.time(), received_at=time.time())
    watchdog = _stale_watchdog(registry, lambda *a: handle, callback=announced.append)

    for _ in range(3):
        handle._last_heartbeat_at = time.time()
        handle._last_received_at = time.time()
        assert watchdog.tick() == []

    # The arrival clock alone is enough to keep it fresh, even when the
    # heartbeat timestamp is unreadable (the NTP-step backstop).
    handle._last_heartbeat_at = None
    handle._last_received_at = time.time()
    assert watchdog.tick() == []
    assert announced == []


# -- 5 -----------------------------------------------------------------------


def test_watchdog_announces_but_never_kills_cancels_or_interrupts():
    """A stale busy worker is announced and left strictly alone.

    The worker exposes every termination seam the rest of the tree uses; the
    assertion is that none of them is called, and that this module's source
    contains no call to one either.
    """
    announced = []
    observer = _RecordingObserver(newly_stale=1)
    worker = _PoisonWorker()
    registry = _FakeRegistry({("s1", "w1", 1): worker})
    handle = _FakeHandle(heartbeat_at=time.time() - 900.0)
    watchdog = _stale_watchdog(
        registry,
        lambda *a: handle,
        observer=observer,
        callback=announced.append,
    )

    announcements = watchdog.tick()
    watchdog.tick()

    assert worker.status == "busy"  # untouched, still doing its work
    assert worker.poison_calls == []
    assert observer.scans == 2  # the observer scan really is driven
    kinds = [a["kind"] for a in announcements]
    assert "frame_stale" in kinds and "observer_stale" in kinds
    # The frame announcement is strictly once-per-episode; the observer pass is
    # re-driven on every tick (the observer owns its own dedupe upstream).
    assert sum(1 for a in announced if a["kind"] == "frame_stale") == 1
    assert len(announced) == 3

    # Source-level guard, phase-aware since S7c-3: the SCAN path (tick and
    # everything it calls) may never reach a control verb, so a future edit
    # cannot quietly turn a tick into an action; and the interrupting verbs stay
    # out of the whole module -- the escalation ladder asks first (ping) and
    # never interrupts, it cancels.
    tree = ast.parse(inspect.getsource(watchdog_module))
    scan_path = {
        "run",
        "tick",
        "_scan_observer",
        "_scan_frames",
        "_announce_frame",
        "_frame_age",
        "_key_parts",
        "_announce",
        "_first_announce",
        "_forget",
        "_retain",
        "_start_escalation",
    }
    for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        names = {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(func) if isinstance(n, ast.Attribute)}
        identifiers = names | attrs
        for verb in (
            "interrupt",
            "terminate_all",
            "terminate_executions",
            "_terminate_tracked_executions",
        ):
            assert verb not in identifiers, (
                f"{verb!r} must not be reachable in {func.name}"
            )
        if func.name in scan_path:
            for verb in ("kill", "cancel", "ping"):
                assert verb not in attrs, (
                    f"{verb!r} must not be reachable from the scan path "
                    f"({func.name})"
                )


# -- 6 -----------------------------------------------------------------------


def test_stale_watchdog_constants_ship_announce_only_defaults():
    """The shipped constants: announce-only, and the existing ones untouched."""
    assert HEARTBEAT_INTERVAL_S == 30
    assert HEARTBEAT_STALE_AFTER_S == 600
    assert WORKER_HUNG_GRACE_S == 0
    assert WORKER_STALE_WATCHDOG_INTERVAL_S == HEARTBEAT_INTERVAL_S
    assert WORKER_STALE_ANNOUNCE_AFTER_S == 3 * HEARTBEAT_INTERVAL_S == 90
    # Announce well before the interrupting threshold: the two must not be the
    # same reading, or an announcement could never precede an interruption.
    assert WORKER_STALE_ANNOUNCE_AFTER_S < HEARTBEAT_STALE_AFTER_S
    # Phase-B placeholders (declared, not yet read by anything).
    assert (
        WORKER_STALE_PROBE_TIMEOUT_S,
        WORKER_STALE_CANCEL_TIMEOUT_S,
        WORKER_STALE_KILL_TIMEOUT_S,
    ) == (15, 15, 15)

    # The class defaults come from those constants.
    watchdog = WorkerStaleWatchdog()
    assert watchdog.daemon is True
    assert watchdog.name == "worker-stale-watchdog"
    params = inspect.signature(WorkerStaleWatchdog.__init__).parameters
    assert params["interval"].default == WORKER_STALE_WATCHDOG_INTERVAL_S
    assert params["announce_after"].default == WORKER_STALE_ANNOUNCE_AFTER_S


# -- 7 -----------------------------------------------------------------------


def test_default_observer_publishes_on_a_private_bus():
    """The production observer publishes onto a bus nothing else listens to.

    ``check_stale_transitions`` emits ``WORKER_TIMEOUT`` through its bus. On the
    shared global bus that publication reaches ``worker_thread``'s own observer,
    whose callback terminates the stale worker's in-flight executions -- an
    announcement turned into an action by a subscriber this module does not own.
    A bus created here has no such consumer.
    """
    from agent.events import EventType, create_event, global_event_bus

    observer = watchdog_module._default_observer_provider()
    if observer is None:  # pragma: no cover - the lifecycle module is present
        pytest.fail("the announce observer is unavailable in this tree")
    assert observer._event_bus is not global_event_bus
    assert observer._stale_callback is None  # the direct edge is closed too
    # Created once: a fresh observer per tick would lose the heartbeat history
    # that makes the scan meaningful.
    assert watchdog_module._default_observer_provider() is observer

    # Cheap proof the isolation is real: an event published on the observer's
    # own bus reaches that bus, and never reaches a global-bus subscriber.
    seen = []
    callback = seen.append
    global_event_bus.subscribe(EventType.WORKER_TIMEOUT, callback)
    try:
        observer._event_bus.publish(
            create_event(
                EventType.WORKER_TIMEOUT,
                {"worker_name": "w1", "reason": "stale_heartbeat"},
                source="test_worker_stale_watchdog",
            )
        )
        assert seen == [], "the private bus must not reach global subscribers"
    finally:
        global_event_bus.unsubscribe(EventType.WORKER_TIMEOUT, callback)
    # The event did go somewhere: it landed on the observer's own bus.
    assert any(r["worker_name"] == "w1" for r in observer.recent_events())


# -- 8 -----------------------------------------------------------------------


def test_departed_worker_is_forgotten_and_can_announce_again():
    """A worker that leaves the registry is pruned from the dedupe set.

    Without pruning, ``_announced`` grows for the lifetime of the process and a
    key that comes back -- same session, same worker, same instance -- is
    silenced forever by an announcement nobody can act on any more.
    """
    announced = []
    worker = _PoisonWorker()
    key = ("s1", "w1", 1)
    registry = _FakeRegistry({key: worker})
    handle = _FakeHandle(heartbeat_at=time.time() - 1000.0)
    watchdog = _stale_watchdog(registry, lambda *a: handle, callback=announced.append)

    assert len(watchdog.tick()) == 1
    assert watchdog._announced == {key}
    assert watchdog.tick() == []  # still deduped while it is present

    registry._workers.pop(key)
    assert watchdog.tick() == []
    assert watchdog._announced == set(), "a departed worker must be forgotten"

    # The same key comes back, still stale: a NEW episode, announced again.
    registry._workers[key] = worker
    assert len(watchdog.tick()) == 1
    assert len(announced) == 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
