# tests/test_worker_lifecycle_trust.py
"""W2 worker-lifecycle trust tests — exactly ten names:

 1. test_sync_worker_call_never_returns_early
 2. test_reuse_delivers_query_and_blocks
 3. test_worker_manager_enforces_cap
 4. test_worker_manager_reuses_context_worker
 5. test_execution_tracker_interrupts_blocking_docker_call
 6. test_periodic_stale_check_interrupts_stuck_worker
 7. test_soft_timeout_restricts_tools_cooperatively
 8. test_hard_deadline_fallback_stops_worker
 9. test_worker_timeout_constants_single_source
10. test_worker_split_imports_clean

W3 seams
--------
- test 6 drives the real ``PeriodicStaleCheck`` thread: a fake worker whose
  ``last_heartbeat`` is older than ``stale_after`` must be interrupted via its
  ``_terminate_tracked_executions`` seam, while a fresh-heartbeat worker is
  left untouched.
- test 8 drives the real ``_hard_deadline_fallback``: a worker whose thread
  is still alive past its hard deadline must be stopped and reaped with a
  bounded join, and the returned envelope must carry the hard-deadline meta.

Invariant
---------
NEVER touch the ``WorkerRegistry`` singleton from these tests: the manager is
constructed with an injected ``_FakeRegistry`` and every worker is a fake
thread. The only exception is test 10, which imports ``tools.workspace.worker``
(a module-level singleton access that is unavoidable and sanctioned).
"""

import importlib
import json
import threading
import time

from datetime import datetime, timedelta, timezone

import pytest

from infra.workspace_lifecycle_manager import EXEC_KILL_GRACE, ExecutionTracker
from tools.workspace import worker_manager as worker_manager_module
from tools.workspace import worker_timeout as worker_timeout_module
from tools.workspace.worker_manager import (
    PeriodicStaleCheck,
    WorkerCapExceeded,
    WorkerControlError,
    WorkerManager,
)
from tools.workspace.worker_query import deliver_query_and_block


# ---------------------------------------------------------------------------
# fakes (never touch the real WorkerRegistry singleton)
# ---------------------------------------------------------------------------


class _FakeRegistry:
    """Dict-backed WorkerRegistry duck-type (keys: (session, name, iid))."""

    def __init__(self):
        self._workers = {}

    def register_worker(self, session_id, worker_name, thread, instance_id=1):
        self._workers[(session_id or "", worker_name, instance_id)] = thread

    def unregister_worker(self, session_id, worker_name, instance_id=1, default=None):
        return self._workers.pop((session_id or "", worker_name, instance_id), default)

    def get_worker(self, session_id, worker_name, instance_id=1):
        return self._workers.get((session_id or "", worker_name, instance_id))

    def get_all_workers(self):
        return dict(self._workers)

    def find_workers_by_name(self, worker_name):
        return [(key[0], thread) for key, thread in self._workers.items() if key[1] == worker_name]

    @staticmethod
    def instance_label(name, instance_id):
        if instance_id in (1, None):
            return name
        return "{}#{}".format(name, instance_id)


class _FakeThread:
    """Duck-typed worker thread: no worker.py imports, no registry."""

    def __init__(self, worker_name="w1", instance_id=1, context_tag=None):
        self.worker_name = worker_name
        self.instance_id = instance_id
        self.context_tag = context_tag
        self.status = "idle"
        self.alive = True
        self.busy = None
        self.stop_calls = 0
        self.joins = 0
        self.instance_label = worker_name if instance_id in (1, None) else "{}#{}".format(worker_name, instance_id)

    def is_alive(self):
        return self.alive

    def stop(self):
        self.stop_calls += 1
        self.status = "stopping"

    def join(self, timeout=None):
        self.joins += 1

    def send_query(self, query, timeout=None):
        return "reply-ok"

    def pause(self):
        self.status = "paused"

    def resume(self):
        self.status = "ready"


class _RecordingThread(_FakeThread):
    """Records every wait timeout passed to send_query; can raise TimeoutError."""

    def __init__(self, worker_name="w1", instance_id=1, context_tag=None, raise_timeout=False):
        super().__init__(worker_name=worker_name, instance_id=instance_id, context_tag=context_tag)
        self.raise_timeout = raise_timeout
        self.captured_timeouts = []

    def send_query(self, query, timeout=None):
        self.captured_timeouts.append(timeout)
        if self.raise_timeout:
            raise TimeoutError("simulated stuck worker")
        return "reply-ok"


class _GateThread(_FakeThread):
    """Blocks inside send_query until the gate opens (or timeout elapses)."""

    def __init__(self, worker_name="w1", instance_id=1, context_tag=None):
        super().__init__(worker_name=worker_name, instance_id=instance_id, context_tag=context_tag)
        self.gate = threading.Event()
        self.started = threading.Event()

    def send_query(self, query, timeout=None):
        self.started.set()
        if not self.gate.wait(timeout=timeout):
            raise TimeoutError("gate never opened")
        return json.dumps(
            {
                "content": "hello",
                "status": "ok",
                "confidence": 0.95,
                "meta": {"qid": query},
                "telemetry": {"tokens": 42},
            }
        )


class _StuckThread(_FakeThread):
    """Raises TimeoutError immediately; stop() really terminates the thread."""

    def send_query(self, query, timeout=None):
        raise TimeoutError("simulated stuck worker")

    def stop(self):
        super().stop()
        self.status = "stopped"
        self.alive = False


class _HeartbeatThread(_FakeThread):
    """Fake thread with a heartbeat attr and a recording interrupt seam."""

    def __init__(self, worker_name="w1", instance_id=1, context_tag=None, heartbeat=None):
        super().__init__(worker_name=worker_name, instance_id=instance_id, context_tag=context_tag)
        self.last_heartbeat = heartbeat
        self.interrupts = 0

    def _terminate_tracked_executions(self):
        self.interrupts += 1


# ---------------------------------------------------------------------------
# 1 / 2 / 3 / 4: blocking delivery + orchestration (injected fake registry)
# ---------------------------------------------------------------------------


def test_sync_worker_call_never_returns_early():
    """deliver_query_and_block must block until the worker replies."""
    thread = _GateThread()
    manager = WorkerManager(registry=_FakeRegistry())
    manager.register_worker("s1", thread.worker_name, thread, instance_id=thread.instance_id)

    results = {}
    finished = threading.Event()

    def deliver():
        results["envelope"] = deliver_query_and_block(
            thread, {"q": 1}, timeout=30.0, worker_name=thread.worker_name
        )
        finished.set()

    t = threading.Thread(target=deliver)
    t.start()
    assert thread.started.wait(timeout=5.0), "worker never entered send_query"
    assert not finished.is_set(), "deliver returned BEFORE the worker replied"
    thread.gate.set()
    assert finished.wait(timeout=5.0), "deliver did not return after the reply"
    t.join(timeout=5.0)
    env = results["envelope"]
    assert env["status"] == "ok"
    assert env["content"] == "hello"
    assert env["response"] is not None


def test_reuse_delivers_query_and_blocks():
    """request_worker with a named context_preference reuses the live worker."""
    manager = WorkerManager(registry=_FakeRegistry())
    thread = _RecordingThread(worker_name="ctxw", instance_id=1)
    manager.register_worker("s1", "ctxw", thread, instance_id=1)

    envelope = manager.request_worker(
        "s1", {"q": 2}, context_preference={"worker_name": "ctxw"}
    )

    assert envelope["delivery"] == {"reused": True, "spawned": False, "force_replaced": False}
    assert envelope["status"] == "ok"
    assert envelope["response"] == "reply-ok"
    assert thread.captured_timeouts, "reused worker must have been sent the query"
    assert manager.live_count("s1") == 1


def test_worker_manager_enforces_cap():
    """A session at its live-worker cap raises WorkerCapExceeded, never a silent drop."""
    manager = WorkerManager(registry=_FakeRegistry(), default_max_workers=3)
    for i in range(3):
        thread = _FakeThread(worker_name="capw", instance_id=i + 1)
        manager.register_worker("s1", "capw", thread, instance_id=i + 1)

    with pytest.raises(WorkerCapExceeded) as exc_info:
        manager.request_worker("s1", {"q": 3}, max_workers=3)
    assert exc_info.value.cap == 3
    assert exc_info.value.live == 3
    assert "s1" in str(exc_info.value)

    with pytest.raises(ValueError):
        manager.request_worker("s1", {"q": 3}, spawner=None, max_workers=5)


def test_worker_manager_reuses_context_worker():
    """context_preference with a context_tag reuses the matching live worker only."""
    manager = WorkerManager(registry=_FakeRegistry())
    other = _RecordingThread(worker_name="ctxw", instance_id=1, context_tag="a")
    match = _RecordingThread(worker_name="ctxw", instance_id=2, context_tag="b")
    manager.register_worker("s1", "ctxw", other, instance_id=1)
    manager.register_worker("s1", "ctxw", match, instance_id=2)

    envelope = manager.request_worker(
        "s1",
        {"q": 4},
        context_preference={"worker_name": "ctxw", "context_tag": "b"},
    )

    assert envelope["delivery"]["reused"] is True
    assert match.captured_timeouts, "matching worker must have been used"
    assert other.captured_timeouts == [], "non-matching worker must be untouched"


# ---------------------------------------------------------------------------
# 5 / 6: interruption of blocking / stuck workers
# ---------------------------------------------------------------------------


class _FakeContainerManager:
    def __init__(self, exec_stop=None, stop=None, remove=None):
        self.exec_stopped = []
        self.stopped = []
        self.removed = []
        self._exec_stop = exec_stop
        self._stop = stop
        self._remove = remove

    def exec_stop(self, container_id, exec_id, timeout=None):
        self.exec_stopped.append((container_id, exec_id, timeout))
        if self._exec_stop:
            self._exec_stop(container_id, exec_id, timeout=timeout)

    def stop(self, container_id):
        self.stopped.append(container_id)
        if self._stop:
            self._stop(container_id)

    def remove(self, container_id):
        self.removed.append(container_id)
        if self._remove:
            self._remove(container_id)


def test_execution_tracker_interrupts_blocking_docker_call():
    """ExecutionTracker.terminate_all stops a blocking docker exec via exec_stop."""
    tracker = ExecutionTracker()
    cm = _FakeContainerManager()
    tracker.add("e1", {"type": "docker_exec", "container_id": "c1", "exec_id": "x1"})
    assert tracker.active_count() == 1

    tracker.terminate_all("w1", cm, None)

    assert cm.exec_stopped == [("c1", "x1", EXEC_KILL_GRACE)]
    assert tracker.active_count() == 0

    cm2 = _FakeContainerManager()
    tracker.add("e2", {"type": "docker_exec", "container_id": "c2", "exec_id": "x2"})
    tracker.terminate_all("w1", cm2, None)
    assert cm2.exec_stopped == [("c2", "x2", EXEC_KILL_GRACE)]


def test_periodic_stale_check_interrupts_stuck_worker():
    """PeriodicStaleCheck interrupts only workers whose heartbeat is stale."""
    manager = WorkerManager(registry=_FakeRegistry())
    stale = _HeartbeatThread(
        worker_name="stalew",
        instance_id=1,
        heartbeat=(datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(),
    )
    fresh = _HeartbeatThread(
        worker_name="freshw",
        instance_id=1,
        heartbeat=(datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
    )
    manager.register_worker("s1", "stalew", stale, instance_id=1)
    manager.register_worker("s1", "freshw", fresh, instance_id=1)

    checker = PeriodicStaleCheck(manager._registry, interval=0.05, stale_after=0.1)
    checker.start()
    deadline = time.monotonic() + 5.0
    try:
        while stale.interrupts == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert stale.interrupts > 0, "stale worker must be interrupted"
        assert fresh.interrupts == 0, "fresh worker must be left untouched"
    finally:
        checker.stop()
        checker.join(timeout=5.0)
    assert not checker.is_alive()


# ---------------------------------------------------------------------------
# 7 / 8: timeout machinery
# ---------------------------------------------------------------------------


class _FakeTimeState:
    def __init__(self, value):
        self.value = value


class _FakeAgentState:
    def __init__(self, restriction_reason=None, time_state_value="normal", elapsed=0.0):
        self.restriction_reason = restriction_reason
        self.time_state = _FakeTimeState(time_state_value)
        self.elapsed_seconds = elapsed


def test_soft_timeout_restricts_tools_cooperatively():
    """Soft-timeout detection restricts tools cooperatively, never by force."""
    detect = worker_timeout_module._worker_timeout_detected
    restricted = _FakeAgentState(restriction_reason="timeout")
    assert detect(restricted, 1.0, 300) is True

    critical = _FakeAgentState(time_state_value="CRITICAL")
    assert detect(critical, 1.0, 300) is True

    over_budget = _FakeAgentState(elapsed=310.0)
    assert detect(over_budget, 310.0, 300) is True

    healthy = _FakeAgentState(elapsed=10.0)
    assert detect(healthy, 10.0, 300) is False

    assert detect(None, 1.0, 300) is False


def test_hard_deadline_fallback_stops_worker():
    """_hard_deadline_fallback stops a still-alive worker and reaps it."""
    stuck = _StuckThread(worker_name="deadlinew", instance_id=1)

    envelope = worker_timeout_module._hard_deadline_fallback(
        stuck,
        deadline=time.monotonic() + 0.5,
        join_bound=0.2,
        worker_name="deadlinew",
    )

    assert stuck.stop_calls == 1, "fallback must call stop() on the stuck worker"
    assert stuck.joins >= 1, "fallback must join the worker"
    assert envelope["status"] == "stopped"
    assert envelope["meta"]["hard_deadline"] is True
    assert envelope["cleanup"]["stop_called"] is True
    assert envelope["cleanup"]["join_bounded"] == 0.2
    assert envelope["meta"]["elapsed_seconds"] >= 0


# ---------------------------------------------------------------------------
# 9 / 10: single-source constants + split-import cleanliness
# ---------------------------------------------------------------------------


def test_worker_timeout_constants_single_source():
    """agent.config.defaults is the single source for every timeout constant."""
    from agent.config import defaults
    from tools.workspace import worker_query

    assert defaults.SPAWN_QUEUE_TIMEOUT == 600
    assert defaults.QUERY_WAIT_GRACE_SECONDS == 60
    assert defaults.MAX_WORKERS_PER_SESSION == 3
    assert defaults.HEARTBEAT_INTERVAL_S == 30
    assert defaults.HEARTBEAT_STALE_AFTER_S == 600
    assert defaults.WORKER_DEFAULT_MAX_CONTAINERS == 4
    assert defaults.PER_WORKER_RING_SIZE == 50
    assert defaults.GLOBAL_RING_SIZE == 500
    assert defaults.WORKER_HUNG_GRACE_S == 0
    assert defaults.JOB_REGISTRY_MAX_JOBS == 200
    assert defaults.PREVIEW_CAP == 8000
    assert defaults.PARTIAL_PREVIEW_CAP == 2000
    assert defaults.TERMINAL_STATUSES == (
        "completed", "paused", "timeout", "error", "stopped", "interrupted",
    )
    assert defaults.SOFT_TIMEOUT == 300
    assert defaults.HARD_TIMEOUT == 600
    assert defaults.EXEC_KILL_GRACE == 10
    assert defaults.QUERY_ID_PREFIX == "q_"

    # the constants are the SAME objects, not copies.
    from tools.workspace import worker_execution as worker_execution_module

    assert worker_query.QUERY_WAIT_GRACE_SECONDS is defaults.QUERY_WAIT_GRACE_SECONDS
    assert (
        worker_timeout_module.QUERY_WAIT_GRACE_SECONDS
        is defaults.QUERY_WAIT_GRACE_SECONDS
    )
    assert worker_manager_module.MAX_WORKERS_PER_SESSION is defaults.MAX_WORKERS_PER_SESSION
    assert worker_execution_module.EXEC_KILL_GRACE is defaults.EXEC_KILL_GRACE


def test_worker_split_imports_clean():
    """Importing tools.workspace.worker in isolation stays clean."""
    mod = importlib.import_module("tools.workspace.worker")
    assert mod is not None
    assert hasattr(mod, "Worker")
