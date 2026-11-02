"""Stop-button cascade: ``request_stop()`` must stop the session's workers.

The Stop button path is ``server.py`` ``stop_session`` -> ``bridge.request_stop()``.
It truncates the parent agent's current turn.  Worker sub-agents spawned by that
parent kept running: nothing on the Stop path touched the worker registry, so a
user who pressed Stop still had workers burning tokens and Docker containers in
the background.  ``pause()``/``resume()`` already cascade to workers; Stop was
the one control that did not.

Contract under test (RED before the fix):

  * ``request_stop()`` TERMINATES (``thread.kill()``) every worker of THIS
    bridge's session and touches no other session's workers.  PHASE 2.1a:
    ``thread.stop()`` now DETACHES, so routing the cascade through it would
    leave the session's workers parked and alive -- the exact leak this
    cascade exists to prevent.
  * ``request_stop()`` keeps its turn-scoped meaning: no ``controller.stop()``,
    no ``_stop_event`` on the controller path -- the session survives.
  * The cascade is fail-closed when the bridge has no session id (stop nothing
    rather than everything), idempotent, skips dead/already-terminal workers,
    and never calls ``kill()`` while holding the registry lock (a worker's kill
    path touches the registry, so holding the lock there would deadlock).
  * The legacy no-controller path cascades too: the workers are a property of
    the session, not of the controller object.

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


# ── fakes ────────────────────────────────────────────────────────────────────


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


class _FakeWorker:
    """Registry stand-in: records ``kill()``/``stop()`` and probes the lock.

    PHASE 2.1a: ``stop()`` DETACHES (parks the worker, thread survives) and
    ``kill()`` TERMINATES.  The Session Stop cascade must call ``kill()``:
    a detach would leave the worker alive and holding its containers, which is
    exactly the leak the cascade exists to prevent.

    A real ``WorkerThread.kill()`` sets its status, writes status/command files
    and can touch the worker registry, so the probe below turns "the cascade
    called ``kill()`` while holding ``_registry_lock``" into a clean failure
    instead of a deadlock.
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


# ── fixtures / helpers ───────────────────────────────────────────────────────


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
    shapes occur in the wild, so the cascade has to handle both.
    """
    key = (session_id, name) if instance_id is None else (session_id, name, instance_id)
    with worker_module._registry_lock:
        worker_module._worker_registry[key] = thread
    return thread


# ── tests ────────────────────────────────────────────────────────────────────


def test_request_stop_stops_every_worker_of_the_session(bridge, monkeypatch):
    """The defect: pressing Stop here and now. Every session worker stops."""
    events = _record_log(monkeypatch)
    controller = _FakeController(busy=True)
    bridge._controller = controller
    w1 = _register(SID, "w1", _FakeWorker("w1"))
    w2 = _register(SID, "w2", _FakeWorker("w2"), instance_id=2)

    assert bridge.request_stop() is True

    assert w1.kill_calls == 1, "worker w1 was left running by the Stop button"
    assert w1.stop_calls == 0, (
        "PHASE 2.1a: stop() only DETACHES -- the cascade must terminate, not park"
    )
    assert w2.kill_calls == 1, (
        "instance-2 workers are keyed (session, name, instance_id) and must "
        "cascade too"
    )

    # ...and Stop is still turn-scoped for the parent.
    assert controller.halt_calls == 1, "the parent turn must still be halted"
    assert controller.stop_calls == 0, "request_stop() must never tear the controller down"
    assert not bridge._stop_event.is_set(), (
        "request_stop() must not set _stop_event — the bridge/session survive a Stop"
    )

    info = [str(m) for (lvl, _cat, m) in events if lvl == "INFO"]
    assert any("w1" in m for m in info), (
        f"no INFO log naming the stopped worker: {events!r}"
    )


def test_request_stop_leaves_other_sessions_workers_alone(bridge, monkeypatch):
    """Session scoping: one session's Stop must not kill another's workers."""
    _record_log(monkeypatch)
    bridge._controller = _FakeController(busy=True)
    mine = _register(SID, "mine", _FakeWorker("mine"))
    foreign = _register(OTHER_SID, "foreign", _FakeWorker("foreign"))
    foreign2 = _register(OTHER_SID, "foreign2", _FakeWorker("foreign2"), instance_id=3)

    bridge.request_stop()

    assert mine.kill_calls == 1
    assert foreign.kill_calls == 0, "another session's worker was stopped"
    assert foreign2.kill_calls == 0, "another session's instance-2 worker was stopped"


def test_request_stop_without_session_id_stops_nothing(bridge, monkeypatch):
    """Fail-closed: an unknown session id must not sweep the whole registry."""
    _record_log(monkeypatch)
    bridge._controller = _FakeController(busy=True)
    bridge._session_id = None
    a = _register(SID, "a", _FakeWorker("a"))
    b = _register(OTHER_SID, "b", _FakeWorker("b"))

    bridge.request_stop()

    assert a.kill_calls == 0, "no session id -> nothing may be stopped"
    assert b.kill_calls == 0


def test_request_stop_cascade_is_idempotent_and_skips_dead_workers(bridge, monkeypatch):
    """Repeated Stops and already-terminal workers must not be re-stopped."""
    _record_log(monkeypatch)
    bridge._controller = _FakeController(busy=True)
    live = _register(SID, "live", _FakeWorker("live"))
    killed = _register(SID, "killed", _FakeWorker("killed", status="killed"))
    stopping = _register(SID, "stopping", _FakeWorker("stopping", status="stopping"))
    stopped = _register(SID, "stopped", _FakeWorker("stopped", status="stopped"))
    dead = _register(SID, "dead", _FakeWorker("dead", alive=False))

    bridge.request_stop()
    bridge.request_stop()

    assert live.kill_calls == 1, "a second Stop must not re-kill a worker"
    assert killed.kill_calls == 0, "an already-killed worker must be left alone"
    assert stopping.kill_calls == 0, "a worker already stopping must be left alone"
    assert stopped.kill_calls == 0, "a stopped worker must be left alone"
    assert dead.kill_calls == 0, "a dead worker has nothing to stop"


def test_request_stop_calls_worker_stop_outside_the_registry_lock(bridge, monkeypatch):
    """A worker's stop path touches the registry: never call it under the lock."""
    _record_log(monkeypatch)
    bridge._controller = _FakeController(busy=True)
    worker = _register(
        SID, "locked", _FakeWorker("locked", probe_lock=worker_module._registry_lock)
    )

    bridge.request_stop()

    assert worker.kill_calls == 1
    assert worker.lock_acquired_during_kill is True, (
        "stop() was called while the cascade held _registry_lock — a real "
        "worker's stop path takes that lock, so this deadlocks in production"
    )


def test_request_stop_cascades_on_the_legacy_no_controller_path(bridge, monkeypatch):
    """Workers belong to the session, not to the controller object."""
    _record_log(monkeypatch)
    bridge._controller = None
    worker = _register(SID, "legacy", _FakeWorker("legacy"))

    bridge.request_stop()

    assert worker.kill_calls == 1, (
        "the legacy no-controller path must still stop the session's workers"
    )
    assert bridge._stop_event.is_set(), "the legacy path keeps its teardown fallback"
