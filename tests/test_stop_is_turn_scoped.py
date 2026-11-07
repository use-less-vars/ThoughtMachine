"""Stop is TURN-scoped: ``request_stop()`` must leave the session's workers alone.

The Stop button path is ``server.py`` ``stop_session`` -> ``bridge.request_stop()``.
It truncates the parent agent's CURRENT TURN.  Workers are children of the
SESSION, not of the turn: a Stop press must NOT stop, detach or kill them -- they
are stopped only through the explicit worker routes (``POST /workers/stop_all``
or the per-worker stop/kill endpoints).  The one reach the turn has into a worker
is indirect: a tool blocked in a Worker join polls the MAIN agent's turn token, so
setting that token unblocks the join with ``status='interrupted'`` while the job
itself is NOT cancelled and stays in the registry, retrievable after the turn.

Contract under test:

  * ``request_stop()`` calls NO worker method at all -- no ``kill()``, no
    ``stop()`` -- for this session or for any other.
  * It still does its turn-scoped job: the main agent's turn token is set (with
    ``reason='stopped_by_operator'``), the controller gets ``request_halt()``,
    and ``controller.stop()``/``_stop_event`` stay untouched on the controller
    path, so the session and the bridge loop survive.
  * Fail-closed without a session id, and idempotent: pressing Stop twice
    touches nothing twice, and no worker method runs while the registry lock is
    held.

NOTE: this file previously pinned the OPPOSITE contract.  It was
``tests/test_stop_cascade_to_workers.py`` and asserted that ``request_stop()``
KILLED every worker of the session (``kill_calls == 1``,
``lock_acquired_during_kill is True``).  That cascade was deliberately removed by
operator ruling -- Stop ends the turn, not the session's workers -- so every
assertion below was inverted.  The fakes still RECORD ``kill()``/``stop()`` so a
regression that re-introduces the cascade fails loudly instead of passing
silently.

Determinism: hand-written fake worker threads plus a fake controller; no real
worker threads, no LLM, no Docker, no sleeps beyond one bounded lock probe.
"""

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import web_ui.backend.bridge as bridge_mod
from tools.workspace import worker as worker_module
from web_ui.backend.bridge import WebAgentBridge
from session.store import FileSystemSessionStore

SID = "sess-stop-cascade"
OTHER_SID = "sess-stop-cascade-other"


# -- fakes -------------------------------------------------------------------


class _FakeController:
    """Records exactly which stop lever the bridge pulled."""

    def __init__(self, busy=False):
        self.is_busy = busy
        self.halt_calls = 0
        self.stop_calls = 0

    def request_halt(self):
        self.halt_calls += 1

    def stop(self):
        self.stop_calls += 1


class _FakeAgent:
    """Stand-in for the main agent: only the turn token matters here."""

    def __init__(self):
        self._turn_cancel_event = threading.Event()


class _FakeWorker:
    """Registry stand-in: records ``kill()``/``stop()`` and probes the lock.

    PHASE 2.1a: ``stop()`` DETACHES (parks the worker, thread survives) and
    ``kill()`` TERMINATES.  Stop must call NEITHER: both counters must stay 0.
    They are kept (rather than deleted) so the inverted contract is asserted
    against the very methods the old cascade used.

    A real ``WorkerThread.kill()`` sets its status, writes status/command files
    and can touch the worker registry, so the probe below turns "a worker method
    ran while holding ``_registry_lock``" into a clean failure instead of a
    deadlock.  ``lock_acquired_during_kill is None`` therefore also proves that
    ``kill()`` was never reached.
    """

    def __init__(self, name, *, alive=True, status="ready", probe_lock=None):
        self.name = name
        self.status = status
        self.alive = alive
        self.stop_calls = 0
        self.kill_calls = 0
        self.lock_acquired_during_kill = None
        self._probe_lock = probe_lock

    def is_alive(self):
        return self.alive

    def stop(self):
        # PHASE 2.1a: detach only -- the thread and its containers survive.
        self.stop_calls += 1
        if self.alive:
            self.status = "detached"

    def kill(self):
        self.kill_calls += 1
        # Faithful to WorkerThread.kill(): the thread marks itself 'killed'
        # synchronously and exits its run loop from there.
        if self.alive:
            self.status = "killed"
        if self._probe_lock is not None and self.lock_acquired_during_kill is None:
            acquired = self._probe_lock.acquire(timeout=0.5)
            self.lock_acquired_during_kill = acquired
            if acquired:
                self._probe_lock.release()


# -- fixtures / helpers ------------------------------------------------------


def _record_log(monkeypatch):
    """Capture ``web_ui.backend.bridge.log(level, category, message, ...)``."""
    events = []

    def recorder(level, category, message, *args, **kwargs):
        events.append((level, category, message))

    monkeypatch.setattr(bridge_mod, "log", recorder)
    return events


@pytest.fixture(autouse=True)
def _clean_registry():
    """Drop registry entries created by the test (they are shared process-wide)."""
    with worker_module._registry_lock:
        preexisting = set(worker_module._worker_registry.keys())
    yield
    with worker_module._registry_lock:
        added = [k for k in worker_module._worker_registry.keys() if k not in preexisting]
        for key in added:
            worker_module._worker_registry.pop(key, None)


@pytest.fixture
def bridge(tmp_path):
    store = FileSystemSessionStore(
        sessions_dir=str(tmp_path / "sessions"),
        state_dir=str(tmp_path / "state"),
    )
    bridge = WebAgentBridge(event_callback=lambda e: None, session_store=store)
    bridge._session_id = SID
    return bridge


def _register(session_id, name, thread, instance_id=None):
    """Put *thread* in the real registry under the real key shapes.

    The registry is keyed ``(session_id, worker_name[, instance_id])``; both
    shapes occur in the wild, so the session filters have to handle both.
    """
    key = (session_id, name) if instance_id is None else (session_id, name, instance_id)
    with worker_module._registry_lock:
        worker_module._worker_registry[key] = thread
    return thread


# -- tests -------------------------------------------------------------------


def test_request_stop_kills_no_worker_of_the_session(bridge, monkeypatch):
    """The operator ruling: press Stop here and now, every worker keeps running."""
    events = _record_log(monkeypatch)
    controller = _FakeController(busy=True)
    bridge._controller = controller
    w1 = _register(SID, "w1", _FakeWorker("w1"))
    w2 = _register(SID, "w2", _FakeWorker("w2"), instance_id=2)

    assert bridge.request_stop() is True

    for worker in (w1, w2):
        assert worker.kill_calls == 0, (
            f"{worker.name} was KILLED by the Stop button: Stop is turn-scoped, "
            "workers stop only via /workers/stop_all"
        )
        assert worker.stop_calls == 0, (
            f"{worker.name} was DETACHED by the Stop button: Stop is turn-scoped"
        )
        assert worker.status == "ready", f"{worker.name} was touched by Stop"
    assert controller.halt_calls == 1, "the turn-level halt must still be requested"
    assert controller.stop_calls == 0, "Stop is turn-scoped: the controller must survive"
    assert not bridge._stop_event.is_set(), "Stop is turn-scoped: the bridge loop must survive"
    assert any("workers_left_running=2" in message for _lvl, _cat, message in events), (
        f"the operator log must report the workers deliberately left running: {events}"
    )


def test_request_stop_sets_the_main_agent_turn_token(bridge, monkeypatch):
    """This is what actually unblocks an in-flight Worker join."""
    _record_log(monkeypatch)
    controller = _FakeController(busy=True)
    bridge._controller = controller
    agent = _FakeAgent()
    bridge._agent = agent
    _register(SID, "w1", _FakeWorker("w1"))

    assert bridge.request_stop() is True

    event = agent._turn_cancel_event
    assert event.is_set(), (
        "the main agent's turn token must be set: a blocked Worker join polls it "
        "between 0.25 s slices and reports status='interrupted'"
    )
    assert event.reason == "stopped_by_operator"
    assert controller.halt_calls == 1


def test_request_stop_leaves_other_sessions_workers_alone(bridge, monkeypatch):
    _record_log(monkeypatch)
    bridge._controller = _FakeController(busy=True)
    ours = _register(SID, "w1", _FakeWorker("w1"))
    theirs = _register(OTHER_SID, "w1", _FakeWorker("w1"))

    assert bridge.request_stop() is True

    assert ours.kill_calls == 0 and ours.stop_calls == 0
    assert theirs.kill_calls == 0 and theirs.stop_calls == 0
    assert theirs.status == "ready"


def test_request_stop_without_session_id_touches_nothing(bridge, monkeypatch):
    """Fail-closed: no session id means no session filter, so touch nothing."""
    _record_log(monkeypatch)
    controller = _FakeController(busy=True)
    bridge._controller = controller
    bridge._session_id = None
    w1 = _register(SID, "w1", _FakeWorker("w1"))

    assert bridge.request_stop() is True  # must not raise

    assert w1.kill_calls == 0 and w1.stop_calls == 0
    assert w1.status == "ready"
    assert controller.halt_calls == 1


def test_request_stop_calls_no_worker_method_at_all(bridge, monkeypatch):
    """Even the read-only accounting must not hold the lock across worker calls."""
    _record_log(monkeypatch)
    bridge._controller = _FakeController(busy=True)
    w1 = _register(SID, "w1", _FakeWorker("w1", probe_lock=worker_module._registry_lock))

    assert bridge.request_stop() is True

    assert w1.kill_calls == 0
    assert w1.lock_acquired_during_kill is None, (
        "kill() ran (it is the only method that probes the lock): Stop must not "
        "touch workers"
    )


def test_request_stop_is_idempotent_and_touches_nothing(bridge, monkeypatch):
    _record_log(monkeypatch)
    controller = _FakeController(busy=True)
    bridge._controller = controller
    w1 = _register(SID, "w1", _FakeWorker("w1"))

    assert bridge.request_stop() is True
    assert bridge.request_stop() is True

    assert controller.halt_calls == 2
    assert w1.kill_calls == 0 and w1.stop_calls == 0
    assert w1.status == "ready"
    assert not bridge._stop_event.is_set()


def test_request_stop_legacy_no_controller_path_sets_stop_event(bridge, monkeypatch):
    """The legacy path still tears the loop down -- and still touches no worker."""
    events = _record_log(monkeypatch)
    bridge._controller = None
    w1 = _register(SID, "w1", _FakeWorker("w1"))

    assert bridge.request_stop() is True

    assert bridge._stop_event.is_set()
    assert bridge._park_event.is_set()
    assert w1.kill_calls == 0 and w1.stop_calls == 0
    assert w1.status == "ready"
    assert any(
        level == "WARNING" and "no controller" in message
        for level, _cat, message in events
    ), events


def test_bridge_has_no_session_wide_worker_stop_helper():
    """The cascade is gone, not merely bypassed."""
    assert not hasattr(WebAgentBridge, "_stop_session_workers"), (
        "the session-wide Stop cascade was deliberately removed: workers stop "
        "only via /workers/stop_all or the per-worker routes"
    )
