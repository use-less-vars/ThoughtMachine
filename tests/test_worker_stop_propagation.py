"""
TESTS-FIRST — worker stop propagation (RED on feat/wlm-phase1-safe-foundation).

Contracts under test (the prod implementation does NOT satisfy them yet):

  A1. A stop mid-turn makes the worker pass through an observable 'stopping'
      status and terminate with 'stopped'. (Today the worker goes
      busy -> ready -> completed; there are no stopping/stopped states.)
  A2. Stop is cooperative, not instant death: the in-flight tool completes,
      the NEXT tool never starts. (The per-event stop check in
      _run_tool_loop already abandons the generator, so the tool-boundary
      asserts pass today; the terminal-status assert is what makes it RED.)
  A3. Stopping a worker stops+removes its worker-owned containers (label
      thoughtmachine.worker=<worker_name>) via the container manager, and
      never touches resource containers (thoughtmachine.resource label /
      tm-res-* name / tm-resource-git image).

No real LLM, no real Docker, no sleeps > 1s, no production code changes.
The container manager is a plain mock: discovery is prod's job, the tests
only assert which containers stop/remove are called with.

Run:  python3 -m pytest tests/test_worker_stop_propagation.py -v
"""

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.workspace.worker import WorkerThread  # noqa: E402

SYS_PROMPT = "You are a helpful worker assistant."


def make_fake_agent(time_state_value="LOW", restriction_reason=None):
    """Minimal fake agent whose .state mirrors what run() resets per query."""
    state = SimpleNamespace(
        current_turn=0,
        turn_state=None,
        last_turn_warning_state=None,
        restrictions_active=False,
        restrictions_pending=False,
        restriction_reason=restriction_reason,
        time_start=0.0,
        last_time_warning_state=None,
        time_state=SimpleNamespace(value=time_state_value),
    )
    return SimpleNamespace(state=state)


def make_thread(workspace_dir, name="w-test", timeout=60, session_id="s1"):
    return WorkerThread(
        name=name,
        definition={"system_prompt": SYS_PROMPT},
        agent_config={"model": "gpt-4o"},
        workspace_dir=Path(workspace_dir),
        session_id=session_id,
        timeout_seconds=timeout,
    )


class _FakeEventBus:
    """Callable stand-in for agent.events.EventBus."""

    def __init__(self, *a, **k):
        pass

    def publish(self, *a, **k):
        return None


class _RunSafetyPatches(unittest.TestCase):
    """Patches event plumbing so WorkerThread.run() can execute headlessly."""

    def setUp(self):
        patchers = [
            mock.patch("tools.workspace.worker.EventBus", new=_FakeEventBus),
            mock.patch("tools.workspace.worker.register_worker_event_bus",
                       new=lambda *a, **k: None),
            mock.patch("tools.workspace.worker.unregister_worker_event_bus",
                       new=lambda *a, **k: None),
            mock.patch("tools.workspace.worker.global_event_bus", new=None),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        super().setUp()


class _Record:
    """Thread-safe marker store: record(key) / contains(key) / wait_for(key, timeout)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._events = {}

    def record(self, key):
        with self._lock:
            ev = self._events.get(key)
            if ev is None:
                ev = threading.Event()
                self._events[key] = ev
            ev.set()

    def contains(self, key):
        with self._lock:
            ev = self._events.get(key)
            return ev is not None and ev.is_set()

    def wait_for(self, key, timeout):
        with self._lock:
            ev = self._events.get(key)
            if ev is None:
                ev = threading.Event()
                self._events[key] = ev
        return ev.wait(timeout)


class _StopAwareFakeAgent:
    """Fake agent that runs two tool calls and honours a stop between them.

    The REAL WorkerThread._run_tool_loop drives this generator, so the fake
    must expose everything the loop touches: ``.state`` (per-query reset by
    run()), ``.process_query(query)`` generator, and ``.request_pause()``
    (called at the per-event stop check, worker.py L1170 — a SimpleNamespace
    fake without it would crash the stop path).
    """

    def __init__(self, record, stop_check, tool_sleep=0.25):
        self.state = make_fake_agent().state
        self._record = record
        self._stop_check = stop_check
        self._tool_sleep = tool_sleep

    def request_pause(self):
        return None

    def process_query(self, query):
        # Tool 1 — the in-flight tool that must be allowed to complete.
        yield {"type": "tool_call", "tool_name": "SlowTool",
               "tool_call_id": "call-1", "arguments": {}}
        self._record.record("tool1_started")
        time.sleep(self._tool_sleep)
        self._record.record("tool1_completed")
        yield {"type": "tool_result", "tool_name": "SlowTool",
               "tool_call_id": "call-1", "success": True, "result": "ok"}
        if self._stop_check():
            self._record.record("cooperative_stop")
            yield {"type": "agent_responded", "status": "stopped",
                   "content": "stopped"}
            return
        # Tool 2 — must never start once stop() has been requested.
        self._record.record("tool2_started")
        yield {"type": "tool_call", "tool_name": "SecondTool",
               "tool_call_id": "call-2", "arguments": {}}
        self._record.record("tool2_completed")
        yield {"type": "tool_result", "tool_name": "SecondTool",
               "tool_call_id": "call-2", "success": True, "result": "ok"}
        yield {"type": "agent_responded", "status": "final", "content": "done"}


# Container fakes — the mocked container manager only records calls; prod
# does the discovery. Labels encode the NEW contract: worker-owned containers
# carry thoughtmachine.worker=<worker_name>; resource containers carry
# thoughtmachine.resource and/or tm-res-* / tm-resource-git names.
WORKER_CONTAINER = SimpleNamespace(
    id="c-w1",
    name="tm-worker-w-test",
    labels={"thoughtmachine.worker": "w-test"},
)
RESOURCE_CONTAINER = SimpleNamespace(
    id="c-res",
    name="tm-resource-git",
    labels={"thoughtmachine.resource": "git"},
)


def _called_with(mock_obj, container):
    """True if any recorded call passed the container object, its id or name."""
    for call in mock_obj.call_args_list:
        values = list(call.args or ()) + list((call.kwargs or {}).values())
        for v in values:
            if v == container or v == container.id or v == container.name:
                return True
    return False


def wait_for_status(thread, value, timeout):
    """Poll thread.status until it equals value (or the timeout elapses)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if thread.status == value:
            return True
        time.sleep(0.02)
    return thread.status == value


class TestStopPropagation(_RunSafetyPatches):
    """A1/A2/A3 — stop mid-turn: statuses, cooperativity, container cleanup."""

    def _start_and_stop_mid_turn(self, tmp, name, cm=None, tool_sleep=0.25):
        """Start a worker, enqueue a query, let tool 1 begin, then stop() it.

        Returns (thread, record) with the worker winding down (stop already
        requested while tool 1 was still sleeping).
        """
        record = _Record()
        thread = make_thread(tmp, name=name)
        # Skip StateBridge/EventProcessor lazy-init -> fully headless.
        thread._state_bridge = SimpleNamespace(context_length=0)
        if cm is not None:
            # NEW prod contract: WorkerThread gains a container_manager
            # constructor param stored as self._container_manager.
            thread._container_manager = cm
        thread._agent = _StopAwareFakeAgent(
            record, lambda: thread._stop_event.is_set(), tool_sleep
        )
        thread._input_queue.put("query-1")
        thread.start()
        if not record.wait_for("tool1_started", 5):
            thread.stop()
            thread.join(5)
            raise AssertionError("worker never reached tool 1")
        thread.stop()  # mid-turn: tool 1 is still sleeping
        return thread, record

    def test_stop_mid_turn_transitions_stopping_then_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            thread, record = self._start_and_stop_mid_turn(tmp, name="w-stop-mid")
            try:
                # Contract: between stop() and termination an observable
                # 'stopping' status must appear. RED today: the worker goes
                # busy -> ready -> completed and never reports 'stopping'.
                self.assertTrue(
                    wait_for_status(thread, "stopping", 5),
                    msg=(
                        "'stopping' status was never observed; "
                        f"final thread.status = {thread.status!r}"
                    ),
                )
                # Sanity: the stop really happened mid-turn.
                self.assertTrue(record.contains("tool1_started"))
                thread.join(5)
                self.assertFalse(thread.is_alive())
                # Contract: terminal status is 'stopped'. RED today: 'completed'.
                self.assertEqual(
                    thread.status, "stopped",
                    msg=f"expected terminal status 'stopped', got {thread.status!r}",
                )
            finally:
                thread.stop()
                thread.join(5)

    def test_stop_is_cooperative_at_tool_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            thread, record = self._start_and_stop_mid_turn(tmp, name="w-stop-coop")
            try:
                thread.join(5)
                self.assertFalse(thread.is_alive())
                # The in-flight tool completes (cooperative, not instant death).
                self.assertTrue(record.contains("tool1_started"))
                self.assertTrue(
                    record.contains("tool1_completed"),
                    msg="in-flight tool must be allowed to complete",
                )
                # The NEXT tool never starts.
                self.assertFalse(
                    record.contains("tool2_started"),
                    msg="next tool must not start after stop()",
                )
                self.assertFalse(record.contains("tool2_completed"))
                # RED today: terminal status is 'completed', not 'stopped'.
                self.assertEqual(
                    thread.status, "stopped",
                    msg=f"expected terminal status 'stopped', got {thread.status!r}",
                )
            finally:
                thread.stop()
                thread.join(5)

    def test_stop_cleans_up_worker_owned_containers(self):
        cm = mock.Mock()
        # Harness fix: configure discovery so the cleanup path has containers
        # to iterate (a plain unconfigured Mock is non-iterable — harness
        # bug, not a contract weakening). Contract asserts unchanged.
        cm.list_containers.return_value = [WORKER_CONTAINER, RESOURCE_CONTAINER]
        with tempfile.TemporaryDirectory() as tmp:
            thread, _ = self._start_and_stop_mid_turn(tmp, name="w-stop-clean", cm=cm)
            try:
                thread.join(5)
                self.assertFalse(thread.is_alive())
                # Worker-owned container (thoughtmachine.worker label) must be
                # stopped AND removed. RED today: no container manager calls.
                self.assertTrue(
                    _called_with(cm.stop, WORKER_CONTAINER),
                    msg=f"container_manager.stop never called with worker container; "
                        f"stop calls={cm.stop.call_args_list}",
                )
                self.assertTrue(
                    _called_with(cm.remove, WORKER_CONTAINER),
                    msg=f"container_manager.remove never called with worker container; "
                        f"remove calls={cm.remove.call_args_list}",
                )
                # Resource containers must never be touched by worker cleanup.
                self.assertFalse(_called_with(cm.stop, RESOURCE_CONTAINER))
                self.assertFalse(_called_with(cm.remove, RESOURCE_CONTAINER))
            finally:
                thread.stop()
                thread.join(5)


if __name__ == "__main__":
    unittest.main()
