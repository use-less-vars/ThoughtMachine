"""
Wave 10a Commit 8 — dispatcher-side duration instrumentation for worker spawn.

Pins the anti-pattern-4 contract: the PARENT (dispatcher) measures its OWN
wall-clock dispatch window and must NEVER have to trust the worker's own
self-reported clock.

Surfaces under test (``Worker._action_spawn`` in
``tools/workspace/worker_thread.py``):

  * ``spawn_started_at``       — captured at the very start of the action.
  * ``dispatch_started_at``    — captured just before the worker starts / the
                                 query is delivered.
  * ``dispatch_finished_at``   — captured just before the envelope returns.
  * ``dispatch_duration_wall`` — ``finished - started`` (seconds, float).
  * ``worker_self_reported``   — the worker's own number (e.g. elapsed_seconds),
                                 surfaced separately and never trusted.

Every assertion is written so it FAILS if the instrumentation is removed
(the keys simply disappear from the envelope).

Harness mirrors tests/test_worker_operation_semantics.py: the real
WorkerRegistry singleton is seeded/restored; ``resolve_workspace_id`` /
``_workspace_dir`` / ``WorkerThread`` are patched so ``_action_spawn`` reaches
the envelope logic without spawning a real thread.

Run:  python -m pytest tests/test_wave10a_dispatch_duration.py -q
"""

from __future__ import annotations

import json
import queue
import unittest
from unittest import mock
from unittest.mock import MagicMock

from tools.workspace.worker import Worker

SESSION = "sess-wave10a-dispatch"
WORKER_NAMES = [f"w{i}" for i in range(1, 11)]  # w1..w10 in the mocked workers.json

# The four dispatcher-side keys the commit adds to every success envelope.
DURATION_KEYS = (
    "spawn_started_at",
    "dispatch_started_at",
    "dispatch_finished_at",
    "dispatch_duration_wall",
)


def _parse_result(result: str) -> dict:
    return json.loads(result)


def _live_thread(status: str = "ready") -> MagicMock:
    """A MagicMock shaped like a live WorkerThread for registry seeding."""
    t = MagicMock()
    t.is_alive.return_value = True
    t.status = status
    t._manual_only_pause = False
    t._timeout_seconds = 30
    t._worker_ctx = None
    return t


class TestDispatchDuration(unittest.TestCase):
    """Dispatcher-side duration instrumentation (Commit 8)."""

    def setUp(self) -> None:
        # ── Snapshot the singleton registry so we can seed and restore ──
        from tools.workspace.worker_registry import WorkerRegistry

        self._registry = WorkerRegistry.get_instance()
        with self._registry._registry_lock:
            self._old_registry = dict(self._registry._worker_registry)
            self._registry._worker_registry.clear()
        self.addCleanup(self._restore_registry)

        # ── Patch workspace plumbing so _action_spawn reaches the logic ──
        self._p_resolve = mock.patch(
            "tools.workspace.worker.resolve_workspace_id", return_value="ws_w10a_test"
        )
        self._p_ws_dir = mock.patch("tools.workspace.worker._workspace_dir")
        self._p_thread_cls = mock.patch("tools.workspace.worker.WorkerThread")
        self._mock_ws_dir = self._p_ws_dir.start()
        self._mock_thread_cls = self._p_thread_cls.start()
        self._p_resolve.start()
        self.addCleanup(self._p_resolve.stop)
        self.addCleanup(self._p_ws_dir.stop)
        self.addCleanup(self._p_thread_cls.stop)

        # The mocked WorkerThread must carry real instance_id/label values, and
        # a controllable self-reported elapsed (default: reports nothing).
        self._self_report = None
        self.last_thread: MagicMock | None = None
        self._mock_thread_cls.side_effect = self._thread_factory

        self._setup_workspace()

    # ── harness helpers ────────────────────────────────────────────────

    def _thread_factory(self, *args, **kwargs) -> MagicMock:
        from tools.workspace.worker_registry import WorkerRegistry

        t = MagicMock()
        iid = kwargs.get("instance_id") or 1
        t.instance_id = iid
        t.instance_label = WorkerRegistry.instance_label(
            kwargs.get("name", "worker"), iid
        )
        t.status = "ready"
        t._timeout_seconds = 30
        # Worker self-report (the value the dispatcher must NOT trust).
        t._last_elapsed.return_value = self._self_report
        # Auto-query reply so shape A resolves immediately.
        t._output_queue.get.return_value = '{"content": "done"}'
        t.send_query.return_value = "reuse reply"
        self.last_thread = t
        return t

    def _restore_registry(self) -> None:
        with self._registry._registry_lock:
            self._registry._worker_registry.clear()
            self._registry._worker_registry.update(self._old_registry)

    def _setup_workspace(self) -> None:
        mock_dir = MagicMock()
        mock_file = MagicMock()
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps(
            [{"name": n, "status": "ready"} for n in WORKER_NAMES]
        )
        mock_dir.__truediv__.return_value = mock_file
        self._mock_ws_dir.return_value = mock_dir

    def _spawn(self, name: str, context: dict | None = None) -> dict:
        tool = Worker(
            action="spawn",
            worker_name=name,
            session_id=SESSION,
            workspace_path="/tmp/test_ws",
            agent_config={"provider": "openai", "model": "gpt-4"},
            context=context,
        )
        return _parse_result(tool.execute())

    def _seed_live(self, name: str, iid: int = 1, status: str = "ready") -> MagicMock:
        from tools.workspace.worker_registry import WorkerRegistry

        t = _live_thread(status=status)
        t.instance_id = iid
        t.instance_label = WorkerRegistry.instance_label(name, iid)
        t._last_elapsed.return_value = self._self_report
        with self._registry._registry_lock:
            self._registry._worker_registry[(SESSION, name, iid)] = t
        return t

    def _assert_all_duration_keys(self, envelope: dict) -> None:
        for key in DURATION_KEYS:
            self.assertIn(key, envelope, f"missing {key!r} in envelope: {envelope}")

    # ── (a) every success shape carries all four duration keys ─────────

    def test_shape_a_fresh_spawn_with_query_has_duration_keys(self):
        """Shape A — fresh spawn WITH auto-query returns the four keys."""
        result = self._spawn("w1", context={"query": "do the thing"})
        self.assertEqual(result.get("spawned"), True)
        self._assert_all_duration_keys(result)

    def test_shape_b_fresh_spawn_without_query_has_duration_keys(self):
        """Shape B — fresh spawn WITHOUT a query returns the four keys."""
        result = self._spawn("w2")
        self.assertEqual(result.get("spawned"), True)
        self.assertEqual(result.get("status"), "ready")
        self._assert_all_duration_keys(result)

    def test_shape_c_reuse_live_instance_has_duration_keys(self):
        """Shape C — reuse of a live non-paused instance returns the keys."""
        fake = self._seed_live("w3", 1)
        fake.send_query.return_value = "reuse reply"
        result = self._spawn("w3", context={"query": "hello"})
        self.assertEqual(result.get("spawned"), False)
        self.assertEqual(result.get("response"), "reuse reply")
        self._assert_all_duration_keys(result)

    def test_shape_d_resume_paused_has_duration_keys(self):
        """Shape D — resume-paused worker returns the four keys."""
        fake = self._seed_live("w4", 1, status="paused")
        fake._input_queue = queue.Queue()
        fake._output_queue = MagicMock()
        fake._output_queue.get.return_value = '{"response": "hi"}'
        result = self._spawn("w4", context={"query": "resume me"})
        self.assertEqual(result.get("spawned"), True)
        self.assertEqual(result.get("response"), "hi")
        self._assert_all_duration_keys(result)

    # ── (b) dispatch_duration_wall == finished - started, and >= 0 ─────

    def test_dispatch_duration_equals_finished_minus_started(self):
        result = self._spawn("w5")
        wall = result["dispatch_duration_wall"]
        finished = result["dispatch_finished_at"]
        started = result["dispatch_started_at"]
        self.assertAlmostEqual(wall, finished - started, places=6)
        self.assertGreaterEqual(wall, 0.0)
        self.assertLessEqual(started, finished)
        # The window opens at/after the spawn handling starts.
        self.assertLessEqual(result["spawn_started_at"], finished)

    def test_dispatch_duration_equals_finished_minus_started_shape_c(self):
        fake = self._seed_live("w3", 1)
        fake.send_query.return_value = "reuse reply"
        result = self._spawn("w3", context={"query": "hello"})
        wall = result["dispatch_duration_wall"]
        self.assertAlmostEqual(
            wall, result["dispatch_finished_at"] - result["dispatch_started_at"], places=6
        )
        self.assertGreaterEqual(wall, 0.0)

    # ── (c) dispatch timing is INDEPENDENT of the worker's own clock ───

    def test_dispatch_fields_present_when_worker_self_report_absent(self):
        """Anti-pattern 4: the worker reports nothing; dispatch fields remain."""
        self._self_report = None  # worker self-reports nothing at all
        result = self._spawn("w6", context={"query": "x"})
        self._assert_all_duration_keys(result)
        self.assertNotIn("elapsed_seconds", result)
        self.assertIsNone(result["worker_self_reported"])
        self.assertGreaterEqual(result["dispatch_duration_wall"], 0.0)
        self.assertLess(result["dispatch_duration_wall"], 30.0)

    def test_dispatch_fields_sane_when_worker_self_report_nonsensical(self):
        """Anti-pattern 4: a nonsensical worker clock cannot corrupt dispatch."""
        self._self_report = 10 ** 9  # absurd "elapsed" the parent must not trust
        result = self._spawn("w7", context={"query": "x"})
        self._assert_all_duration_keys(result)
        # The self-report is surfaced as-is...
        self.assertEqual(result["worker_self_reported"], {"elapsed_seconds": 1e9})
        self.assertEqual(result["elapsed_seconds"], 1e9)
        # ...but the dispatcher-side duration is its own, small, sane number.
        self.assertGreaterEqual(result["dispatch_duration_wall"], 0.0)
        self.assertLess(result["dispatch_duration_wall"], 30.0)
        self.assertNotEqual(result["dispatch_duration_wall"], 1e9)

    def test_dispatch_fields_sane_when_worker_self_report_negative(self):
        """A negative worker clock also cannot corrupt the dispatcher window."""
        self._self_report = -42.0
        result = self._spawn("w8", context={"query": "x"})
        self._assert_all_duration_keys(result)
        self.assertEqual(result["worker_self_reported"], {"elapsed_seconds": -42.0})
        self.assertGreaterEqual(result["dispatch_duration_wall"], 0.0)

    # ── (d) worker_self_reported carries the worker's value when present ─

    def test_worker_self_reported_carries_value_shape_a(self):
        self._self_report = 12.3
        result = self._spawn("w9", context={"query": "x"})
        self.assertEqual(result["worker_self_reported"], {"elapsed_seconds": 12.3})
        # Back-compat: the original top-level key is unchanged.
        self.assertEqual(result["elapsed_seconds"], 12.3)

    def test_worker_self_reported_carries_value_shape_c(self):
        fake = self._seed_live("w10", 1)
        fake.send_query.return_value = "reuse reply"
        fake._last_elapsed.return_value = 7.5
        result = self._spawn("w10", context={"query": "hello"})
        self.assertEqual(result["worker_self_reported"], {"elapsed_seconds": 7.5})
        self.assertEqual(result["elapsed_seconds"], 7.5)

    def test_worker_self_reported_is_null_shape_b(self):
        """No query ran, so the worker self-reported nothing → explicit null."""
        result = self._spawn("w2")
        self.assertIn("worker_self_reported", result)
        self.assertIsNone(result["worker_self_reported"])


if __name__ == "__main__":
    unittest.main()
