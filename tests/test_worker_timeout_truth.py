"""Truth tests for the worker timeout-harness fixes (D1/D2/D3).

Covers the behaviour that the audit tests only approximate with mocks:

  1. ``_worker_query_wait_timeout`` — effective-timeout + grace math, and the
     fallback path (None / MagicMock auto-attribute / non-positive values).
  2. ``_worker_timeout_detected`` — the extracted D3 detection: lowercase
     production TimeState 'critical' matches, uppercase fake values match,
     restriction_reason='timeout' matches, elapsed >= budget matches.
  3. Status override — a soft-triggered attempt reports envelope status
     'timeout' even when the agent already emitted a 'progress' status
     (the old ``and not status`` guard skipped the override).
  4. Spawn auto-query uses the worker's effective timeout + grace
     (timeout=120.0 for a 60s worker) instead of the fixed 600s cap.
  5. Query action passes the effective timeout to send_query.
  6. Ghost unregister — a timeout-terminated run() removes its registry
     entry; a normally-terminated run() KEEPS it (cooperative-stop contract).
  7. Spawn-reuse deliver-and-block (D2) — query delivered to the live
     instance, worker never stopped on reuse timeout, legacy message when
     no query was supplied.

Run:  python3 -m pytest tests/test_worker_timeout_truth.py -v
"""

import json
import queue
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.workspace.worker import (  # noqa: E402
    Worker,
    WorkerThread,
    SPAWN_QUEUE_TIMEOUT,
    QUERY_WAIT_GRACE_SECONDS,
    _worker_query_wait_timeout,
    _worker_timeout_detected,
    _worker_registry,
    _registry_lock,
)

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


class _FakeEventBus:
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


def _pop_registry(*keys):
    with _registry_lock:
        for k in keys:
            _worker_registry.pop(k, None)


# ── 1. Query wait timeout math ──────────────────────────────────────────


class TestQueryWaitTimeoutTruth(unittest.TestCase):
    def test_effective_timeout_plus_grace(self):
        self.assertEqual(_worker_query_wait_timeout(600), 600.0 + QUERY_WAIT_GRACE_SECONDS)
        self.assertEqual(_worker_query_wait_timeout(60), 120.0)
        self.assertEqual(_worker_query_wait_timeout(60, fallback=SPAWN_QUEUE_TIMEOUT), 120.0)

    def test_none_falls_back(self):
        self.assertEqual(_worker_query_wait_timeout(None), 300.0)
        self.assertEqual(
            _worker_query_wait_timeout(None, fallback=SPAWN_QUEUE_TIMEOUT),
            SPAWN_QUEUE_TIMEOUT,
        )

    def test_mock_auto_attribute_falls_back(self):
        # MagicMock configures __float__ to return 1.0; the old try/float
        # path would have produced 61.0 instead of the legacy fallback.
        self.assertEqual(_worker_query_wait_timeout(mock.MagicMock()), 300.0)

    def test_non_positive_falls_back(self):
        self.assertEqual(_worker_query_wait_timeout(0), 300.0)
        self.assertEqual(_worker_query_wait_timeout(-5), 300.0)


# ── 2. D3 timeout detection ─────────────────────────────────────────────


class TestTimeoutDetectionTruth(unittest.TestCase):
    def test_lowercase_production_critical_matches(self):
        agent = make_fake_agent("critical", "timeout")
        self.assertTrue(_worker_timeout_detected(agent.state, None, 60))
        self.assertTrue(_worker_timeout_detected(agent.state, 1.0, 60))

    def test_uppercase_fake_critical_matches(self):
        agent = make_fake_agent("CRITICAL", None)
        self.assertTrue(_worker_timeout_detected(agent.state, None, 60))

    def test_restriction_reason_timeout_matches(self):
        agent = make_fake_agent("LOW", "timeout")
        self.assertTrue(_worker_timeout_detected(agent.state, None, 60))

    def test_no_timeout_signal_is_false(self):
        agent = make_fake_agent("LOW", None)
        self.assertFalse(_worker_timeout_detected(agent.state, 5.0, 60))

    def test_elapsed_over_budget_fallback(self):
        agent = make_fake_agent("LOW", None)
        self.assertTrue(_worker_timeout_detected(agent.state, 61.0, 60))
        self.assertFalse(_worker_timeout_detected(agent.state, 59.9, 60))

    def test_none_agent_is_false(self):
        self.assertFalse(_worker_timeout_detected(None, 61.0, 60))


# ── 3. Status override ──────────────────────────────────────────────────


class TestStatusOverrideTruth(_RunSafetyPatches):
    def _make_thread(self, tmp):
        return WorkerThread(
            name="w-status",
            definition={"system_prompt": SYS_PROMPT},
            agent_config={"model": "gpt-4o"},
            workspace_dir=Path(tmp),
            session_id="s1",
            timeout_seconds=60,
        )

    def test_timeout_overrides_progress_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            thread = self._make_thread(tmp)
            thread._agent = make_fake_agent("critical", "timeout")
            thread._input_queue.put("query-1")
            thread._input_queue.put(None)

            def fake_run_tool_loop(self, query):
                # Agent already emitted a 'progress' status for this attempt;
                # the D3 timeout fires anyway and must WIN the status.
                self._respond_metadata = {"status": "progress"}
                self._timeout_triggered = True
                self._last_elapsed_val = 61.0
                self._final_token_usage = 10
                return None

            with mock.patch.object(
                thread, "_run_tool_loop", new=fake_run_tool_loop.__get__(thread, WorkerThread)
            ):
                thread.run()

            raw = thread._output_queue.get_nowait()
            envelope = json.loads(raw)
            self.assertEqual(envelope["status"], "timeout")
            self.assertIs(envelope["telemetry"]["timeout_triggered"], True)

    def test_progress_status_kept_without_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            thread = self._make_thread(tmp)
            thread._agent = make_fake_agent("LOW", None)
            thread._input_queue.put("query-1")
            thread._input_queue.put(None)

            def fake_run_tool_loop(self, query):
                self._respond_metadata = {"status": "progress"}
                self._last_elapsed_val = 5.0
                self._final_token_usage = 10
                return None

            with mock.patch.object(
                thread, "_run_tool_loop", new=fake_run_tool_loop.__get__(thread, WorkerThread)
            ):
                thread.run()

            raw = thread._output_queue.get_nowait()
            envelope = json.loads(raw)
            self.assertEqual(envelope["status"], "progress")
            self.assertIs(envelope["telemetry"]["timeout_triggered"], False)


# ── 4. Spawn auto-query effective timeout ───────────────────────────────


class TestSpawnAutoQueryEffectiveTimeout(unittest.TestCase):
    def test_auto_query_uses_effective_timeout_plus_grace(self):
        tool = Worker(action="spawn", worker_name="w-auto",
                      context={"query": "auto task"}, session_id="s1")
        tool._find_worker = mock.Mock(return_value={"name": "w-auto"})
        tool._build_agent_config = mock.Mock(return_value={})
        with tempfile.TemporaryDirectory() as tmp:
            tool._resolve_ws_dir = mock.Mock(return_value=Path(tmp))
            fake_thread = mock.MagicMock()
            fake_thread._timeout_seconds = 60
            fake_thread._output_queue.get.side_effect = queue.Empty
            fake_thread._last_elapsed.return_value = None
            try:
                with mock.patch("tools.workspace.worker.WorkerThread", return_value=fake_thread):
                    result = tool._action_spawn([], "ws-1")
            finally:
                _pop_registry(("s1", "w-auto", 1))

            self.assertIn("did not respond within 120.0s", result["error"])
            self.assertIn("still alive", result["note"])
            self.assertIs(result["spawned"], True)
            fake_thread._output_queue.get.assert_called_once_with(timeout=120.0)


# ── 5. Query action effective timeout ───────────────────────────────────


class TestQueryActionEffectiveTimeout(unittest.TestCase):
    def test_success_passes_effective_timeout(self):
        tool = Worker(action="query", worker_name="w-query",
                      worker_query="hello", session_id="s1")
        fake = mock.MagicMock()
        fake.is_alive.return_value = True
        fake.last_heartbeat = None
        fake._timeout_seconds = 60
        fake.send_query.return_value = "reply"
        fake._last_elapsed.return_value = None
        key = ("s1", "w-query", 1)
        with _registry_lock:
            _worker_registry[key] = fake
        try:
            result = tool._action_query([])
        finally:
            _pop_registry(key)
        self.assertEqual(result["response"], "reply")
        fake.send_query.assert_called_once_with("hello", timeout=120.0)

    def test_timeout_error_carries_effective_timeout(self):
        tool = Worker(action="query", worker_name="w-query",
                      worker_query="hello", session_id="s1")
        fake = mock.MagicMock()
        fake.is_alive.return_value = True
        fake.last_heartbeat = None
        fake._timeout_seconds = 60
        fake.send_query.side_effect = TimeoutError(
            "Worker 'w-query' did not respond within 120.0s"
        )
        key = ("s1", "w-query", 1)
        with _registry_lock:
            _worker_registry[key] = fake
        try:
            result = tool._action_query([])
        finally:
            _pop_registry(key)
        self.assertIn("120.0s", result["error"])
        self.assertIn("stopped cooperatively", result["note"])
        fake.send_query.assert_called_once_with("hello", timeout=120.0)
        fake.stop.assert_called_once()


# ── 6. Ghost unregister in run() finally ────────────────────────────────


class TestGhostUnregisterTruth(_RunSafetyPatches):
    def _make_thread(self, tmp):
        return WorkerThread(
            name="w-ghost",
            definition={"system_prompt": SYS_PROMPT},
            agent_config={"model": "gpt-4o"},
            workspace_dir=Path(tmp),
            session_id="s1",
            timeout_seconds=60,
        )

    def _run(self, thread, timeout_triggered):
        thread._agent = make_fake_agent(
            "critical" if timeout_triggered else "LOW",
            "timeout" if timeout_triggered else None,
        )
        thread._input_queue.put("query-1")
        thread._input_queue.put(None)

        def fake_run_tool_loop(self, query):
            self._respond_metadata = {}
            if timeout_triggered:
                self._timeout_triggered = True
            self._last_elapsed_val = 61.0 if timeout_triggered else 5.0
            self._final_token_usage = 10
            return None

        with mock.patch.object(
            thread, "_run_tool_loop", new=fake_run_tool_loop.__get__(thread, WorkerThread)
        ):
            thread.run()

    def test_timeout_terminated_worker_is_unregistered(self):
        with tempfile.TemporaryDirectory() as tmp:
            thread = self._make_thread(tmp)
            key = ("s1", "w-ghost", thread.instance_id)
            with _registry_lock:
                _worker_registry[key] = thread
            try:
                self._run(thread, timeout_triggered=True)
                self.assertNotIn(key, _worker_registry)
            finally:
                _pop_registry(key)

    def test_normal_termination_keeps_registry_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            thread = self._make_thread(tmp)
            key = ("s1", "w-ghost", thread.instance_id)
            with _registry_lock:
                _worker_registry[key] = thread
            try:
                self._run(thread, timeout_triggered=False)
                self.assertIn(key, _worker_registry)
                self.assertEqual(_worker_registry[key].status, "completed")
            finally:
                _pop_registry(key)


# ── 7. Spawn-reuse deliver-and-block (D2) ───────────────────────────────


class TestSpawnReuseDeliverAndBlock(unittest.TestCase):
    def _make_tool(self, context):
        tool = Worker(action="spawn", worker_name="w-reuse",
                      context=context, session_id="s1")
        tool._find_worker = mock.Mock(return_value={"name": "w-reuse"})
        tool._build_agent_config = mock.Mock(return_value={})
        return tool

    def _make_live_fake(self):
        fake = mock.MagicMock()
        fake.is_alive.return_value = True
        fake.status = "running"
        fake._timeout_seconds = 60
        fake._last_elapsed.return_value = None
        return fake

    def test_reuse_delivers_query_and_blocks_for_reply(self):
        fake = self._make_live_fake()
        fake.send_query.return_value = "reuse reply"
        key = ("s1", "w-reuse", 1)
        with _registry_lock:
            _worker_registry[key] = fake
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tool = self._make_tool({"query": "hello"})
                tool._resolve_ws_dir = mock.Mock(return_value=Path(tmp))
                result = tool._action_spawn([], "ws-1")
        finally:
            _pop_registry(key)

        self.assertIs(result["spawned"], False)
        self.assertEqual(result["response"], "reuse reply")
        fake.send_query.assert_called_once_with("hello", timeout=120.0)
        fake.stop.assert_not_called()

    def test_reuse_timeout_returns_error_and_never_stops(self):
        fake = self._make_live_fake()
        fake.send_query.side_effect = TimeoutError("Worker 'w-reuse' did not respond in time")
        key = ("s1", "w-reuse", 1)
        with _registry_lock:
            _worker_registry[key] = fake
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tool = self._make_tool({"query": "hello"})
                tool._resolve_ws_dir = mock.Mock(return_value=Path(tmp))
                result = tool._action_spawn([], "ws-1")
        finally:
            _pop_registry(key)

        self.assertIs(result["spawned"], False)
        self.assertIn("still alive", result["note"])
        fake.send_query.assert_called_once_with("hello", timeout=120.0)
        fake.stop.assert_not_called()

    def test_reuse_without_query_returns_legacy_message(self):
        fake = self._make_live_fake()
        key = ("s1", "w-reuse", 1)
        with _registry_lock:
            _worker_registry[key] = fake
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tool = self._make_tool({})
                tool._resolve_ws_dir = mock.Mock(return_value=Path(tmp))
                result = tool._action_spawn([], "ws-1")
        finally:
            _pop_registry(key)

        self.assertIs(result["spawned"], False)
        self.assertIn("already running", result["message"])
        fake.send_query.assert_not_called()
        fake.stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
