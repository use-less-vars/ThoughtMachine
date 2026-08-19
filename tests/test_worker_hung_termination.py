"""
Phase 3 (items 5+7) — ExecutionTracker termination + worker-hung observer tests.

Hermetic unit tests (no LLM / no Docker):

ExecutionTracker (infra.workspace_lifecycle_manager):
  1. register() records type "container_exec" when container_id+pid are given
     (default type "subprocess" otherwise).
  2. container_exec with container_id+pid -> minimal ``exec_run kill <pid>``,
     never a container stop; tracker cleared afterwards.
  3. subprocess with pid -> os.kill(pid, SIGTERM) when killpg fails.
  4. container_exec without pid + non-worker-owned container -> never stopped.
  8. container_exec without pid + worker-owned container -> stop(container_id).

WorkerLifecycleObserver (tools.workspace.worker_lifecycle):
  5. A hung worker emits exactly one WORKER_TIMEOUT event on the bus.
  6. The hung action invokes the stale_callback (terminate_all path).
  7. hung_grace_s delays the hung emission until the grace elapses, while
     staleness is flagged immediately; re-scans re-evaluate hung.

Run:  python3 -m pytest tests/test_worker_hung_termination.py -q
"""

import signal
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.events import EventType, create_event  # noqa: E402
from tools.workspace.worker_lifecycle import (  # noqa: E402
    WorkerLifecycleObserver,
    HEARTBEAT_INTERVAL_S,
    STALE_AFTER_S,
    HEARTBEAT_STALE_AFTER_S,
    WORKER_HUNG_GRACE_S,
)
from infra.workspace_lifecycle_manager import (  # noqa: E402
    ExecutionTracker,
    EXEC_KILL_GRACE,
    WorkerSupervisor,
)


class FakeContainerManager:
    """Minimal container manager fake: records exec_run/stop, returns fixtures."""

    def __init__(self, containers=None):
        self.containers = containers or []
        self.exec_run_calls = []
        self.stopped = []

    def exec_run(self, container_id, cmd):
        self.exec_run_calls.append((container_id, cmd))

    def stop(self, container_id):
        self.stopped.append(container_id)

    def list_containers(self):
        return list(self.containers)


class FakeBus:
    """Minimal pub/sub bus that dispatches to type-matched callbacks."""

    def __init__(self):
        self.cbs = []

    def subscribe(self, event_type, callback):
        self.cbs.append((event_type, callback))

    def publish(self, evt):
        for et, cb in self.cbs:
            if et == evt.type:
                cb(evt)


class RecordingBus(FakeBus):
    """FakeBus that also records every published event."""

    def __init__(self):
        super().__init__()
        self.events = []

    def publish(self, evt):
        self.events.append(evt)
        super().publish(evt)


def make_event(type_name, worker_name="w1", extra=None, source="test", session_id="s1"):
    data = {
        "worker_name": worker_name,
        "status": "ready",
        "reason": "test",
        "error": None,
    }
    if extra:
        data.update(extra)
    return create_event(EventType(type_name), data=data, source=source, session_id=session_id)


class TestExecutionTrackerRegister(unittest.TestCase):
    def test_register_container_exec_records_type(self):
        tracker = ExecutionTracker()
        exec_id = tracker.register(
            worker_id="w1", query_id="q1", tool_call_id="t1",
            container_id="c1", pid=42, type="container_exec",
        )
        self.assertEqual(tracker.active_count(), 1)
        details = tracker._executions[exec_id]
        self.assertEqual(details["type"], "container_exec")
        self.assertEqual(details["container_id"], "c1")
        self.assertEqual(details["pid"], 42)
        # Default type is "subprocess" when no container/pid is supplied.
        exec_id2 = tracker.register(worker_id="w1", query_id="q2", tool_call_id="t2")
        self.assertEqual(tracker._executions[exec_id2]["type"], "subprocess")
        self.assertEqual(tracker.active_count(), 2)


class TestExecutionTrackerTermination(unittest.TestCase):
    def test_terminate_container_exec_minimal_docker_kill(self):
        cm = FakeContainerManager()
        tracker = ExecutionTracker()
        tracker.add("e1", {"type": "container_exec", "container_id": "c1", "pid": 4242})
        tracker.terminate_all("w1", cm, None)
        # Minimal touch: exec_run kill <pid>, never stop the container.
        self.assertEqual(cm.exec_run_calls, [("c1", ["kill", "4242"])])
        self.assertEqual(cm.stopped, [])
        self.assertEqual(tracker.active_count(), 0)

    def test_terminate_container_exec_subprocess_sigterm(self):
        tracker = ExecutionTracker()
        tracker.add("e1", {"type": "subprocess", "pid": 4242})
        killed = []
        with mock.patch("os.killpg", side_effect=ProcessLookupError), \
             mock.patch("os.kill", side_effect=lambda pid, sig: killed.append((pid, sig))):
            tracker.terminate_all("w1", None, None)
        self.assertEqual(killed, [(4242, signal.SIGTERM)])
        self.assertEqual(tracker.active_count(), 0)

    def test_container_exec_no_pid_non_worker_owned_no_stop(self):
        cm = FakeContainerManager(containers=[
            {"container_id": "c1", "name": "tm-res-abc-git",
             "image": "tm-resource-git",
             "labels": {"thoughtmachine.resource": "git"}},
        ])
        tracker = ExecutionTracker()
        tracker.add("e1", {"type": "container_exec", "container_id": "c1"})
        tracker.terminate_all("w1", cm, None, session_id="s1")
        # Resource/shared containers are never touched without a pid.
        self.assertEqual(cm.stopped, [])
        self.assertEqual(cm.exec_run_calls, [])
        self.assertEqual(tracker.active_count(), 0)

    def test_worker_owned_fallback_stop(self):
        # Without a pid the container is stopped ONLY when the
        # thoughtmachine.worker label matches the worker (bare name or
        # "<session_id>:<worker_name>" owner identity).
        for owner in ("w1", "s1:w1"):
            cm = FakeContainerManager(containers=[
                {"container_id": "c2", "name": "agent-exec-x",
                 "labels": {"thoughtmachine.worker": owner}},
            ])
            tracker = ExecutionTracker()
            tracker.add("e1", {"type": "container_exec", "container_id": "c2"})
            tracker.terminate_all("w1", cm, None, session_id="s1")
            self.assertEqual(cm.stopped, ["c2"])
            self.assertEqual(cm.exec_run_calls, [])


class TestWorkerHungObserver(unittest.TestCase):
    def test_hung_emits_worker_timeout(self):
        bus = RecordingBus()
        obs = WorkerLifecycleObserver(event_bus=bus, stale_after_s=60)
        self.assertTrue(obs.ensure_subscribed())
        now = datetime.now(timezone.utc)
        old = (now - timedelta(seconds=1200)).isoformat()
        bus.publish(make_event("worker_heartbeat", extra={"last_heartbeat": old}))
        obs.check_stale_transitions(now=now + timedelta(seconds=10))
        timeouts = [e for e in bus.events if e.type == EventType.WORKER_TIMEOUT]
        self.assertTrue(timeouts, "expected a WORKER_TIMEOUT event on the bus")
        evt = timeouts[0]
        self.assertEqual(evt.data.get("worker_name"), "w1")
        self.assertEqual(evt.data.get("reason"), "stale_heartbeat")
        self.assertIsNotNone(evt.data.get("heartbeat_age_seconds"))
        self.assertEqual(evt.data.get("session_id"), "s1")
        self.assertEqual(evt.data.get("source"), "worker_lifecycle_observer")

    def test_hung_triggers_terminate_all(self):
        # Mirrors tools.workspace.worker._on_worker_stale: registry lookup by
        # (session_id, worker_name) then wlm.terminate_executions().
        class FakeWlm:
            def __init__(self):
                self.terminations = 0

            def terminate_executions(self):
                self.terminations += 1

        wlm = FakeWlm()
        thread = type("FakeThread", (), {"_wlm": wlm})()
        registry = {("s1", "w1"): thread}

        def stale_callback(worker_name, info):
            t = registry.get((info.get("session_id") or "", worker_name))
            if t is not None and getattr(t, "_wlm", None) is not None:
                t._wlm.terminate_executions()

        bus = RecordingBus()
        obs = WorkerLifecycleObserver(event_bus=bus, stale_after_s=60,
                                      stale_callback=stale_callback)
        obs.ensure_subscribed()
        old = (datetime.now(timezone.utc) - timedelta(seconds=1200)).isoformat()
        bus.publish(make_event("worker_heartbeat", extra={"last_heartbeat": old}))
        obs.check_stale_transitions(now=datetime.now(timezone.utc) + timedelta(seconds=5))
        # Hung detection fires exactly once per worker.
        self.assertEqual(wlm.terminations, 1)

    def test_hung_grace_respected(self):
        calls = []
        bus = RecordingBus()
        obs = WorkerLifecycleObserver(
            event_bus=bus, stale_after_s=60, hung_grace_s=30,
            stale_callback=lambda w, i: calls.append((w, i)),
        )
        obs.ensure_subscribed()
        now = datetime.now(timezone.utc)
        hb = (now - timedelta(seconds=70)).isoformat()
        bus.publish(make_event("worker_heartbeat", extra={"last_heartbeat": hb}))

        # Stale (70 > 60) but within the grace window (70 <= 60+30): flagged,
        # no WORKER_TIMEOUT, no callback.
        self.assertEqual(obs.check_stale_transitions(now=now), 0)
        self.assertTrue(obs.staleness("w1"))
        self.assertEqual(calls, [])
        self.assertEqual([e for e in bus.events if e.type == EventType.WORKER_TIMEOUT], [])

        # Later scan: heartbeat is now 95s old (95 > 90) -> hung emitted once.
        later = now + timedelta(seconds=25)
        self.assertEqual(obs.check_stale_transitions(now=later), 0)
        timeouts = [e for e in bus.events if e.type == EventType.WORKER_TIMEOUT]
        self.assertEqual(len(timeouts), 1)
        self.assertEqual(len(calls), 1)
        worker, info = calls[0]
        self.assertEqual(worker, "w1")
        self.assertEqual(info["reason"], "stale_heartbeat")
        self.assertIn("last_heartbeat", info)
        self.assertEqual(info["session_id"], "s1")
        self.assertAlmostEqual(info["heartbeat_age_seconds"], 95.0, delta=2.0)

        # Re-scan does not re-emit (one-shot per worker).
        self.assertEqual(obs.check_stale_transitions(now=later), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            len([e for e in bus.events if e.type == EventType.WORKER_TIMEOUT]), 1)


if __name__ == "__main__":
    unittest.main()
