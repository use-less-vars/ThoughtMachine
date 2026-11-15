"""A forced spawn must survive a stale instance that was never started.

Field report: every force-respawn printed a ``RuntimeError: cannot join thread
before it is started`` traceback from the ``_action_spawn`` force-stop loop. The
cause is the DEFAULT runtime mode, not a defect in the worker: in ``process``
mode ``WorkerThread.start()`` deliberately returns without starting an
in-process body (the body lives in the child process), so ``ident`` stays
``None`` and ``thread.join()`` refuses to wait. The wait itself is meaningless
there, so the force-stop must skip it -- and say so at DEBUG -- instead of
failing.

Contract pinned here (``Worker._action_spawn``, force branch):

  * forcing a spawn over a never-started stale instance does NOT raise and does
    NOT record the ``Error killing stale worker`` / ``cannot join`` traceback;
  * the skipped join is reported, once, as a DEBUG one-liner naming the reason;
  * the skip does not pretend the join happened: liveness still comes from
    ``is_alive()``, so the dead instance's registry entry is still recycled
    (keeping it would leave an invisible orphan behind).

Harness mirrors ``tests/test_worker_force_spawn_clean_slate.py``: the real
``WorkerRegistry`` singleton is snapshotted, ``resolve_workspace_id`` /
``_workspace_dir`` / ``WorkerThread`` are patched so ``_action_spawn`` runs
against a REAL temp workspace directory.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

from tools.workspace.worker import Worker

SESSION = "sess-unstarted-join"
WORKSPACE = "ws_unstarted_join"
WORKER = "never-started-worker"


class _NeverStartedInstance(threading.Thread):
    """A real ``Thread`` that was never started -- the process-mode shape.

    ``ident`` is ``None`` and ``join()`` raises ``RuntimeError``, exactly like a
    ``WorkerThread`` whose ``start()`` returned early in ``process`` mode. Only
    the seams the force-stop touches are provided.
    """

    def __init__(self) -> None:
        super().__init__(name=WORKER, daemon=True)
        self.worker_name = WORKER
        self.instance_id = 1
        self.status = "ready"
        self._timeout_seconds = 30
        self._worker_ctx = None
        self.kill_calls = 0
        self.save_calls = 0

    def kill(self) -> None:
        self.kill_calls += 1
        self.status = "killed"

    def _save_context(self) -> None:
        self.save_calls += 1


class TestForceSpawnOnNeverStartedThread(unittest.TestCase):
    def setUp(self) -> None:
        from tools.workspace.worker_registry import WorkerRegistry

        self._registry = WorkerRegistry.get_instance()
        with self._registry._registry_lock:
            self._old_registry = dict(self._registry._worker_registry)
            self._registry._worker_registry.clear()
        with self._registry._bus_registry_lock:
            self._old_buses = dict(self._registry._worker_event_bus_registry)
            self._registry._worker_event_bus_registry.clear()
        self.addCleanup(self._restore_registry)

        self._ws = Path(tempfile.mkdtemp(prefix="ws_unstarted_join_"))
        self.addCleanup(shutil.rmtree, self._ws, True)
        (self._ws / "workers.json").write_text(
            json.dumps([{"name": WORKER, "status": "ready"}]),
            encoding="utf-8",
        )

        for patcher in (
            mock.patch(
                "tools.workspace.worker.resolve_workspace_id",
                side_effect=lambda *a, **k: WORKSPACE,
            ),
            mock.patch("tools.workspace.worker._workspace_dir", return_value=self._ws),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        self._p_thread_cls = mock.patch("tools.workspace.worker.WorkerThread")
        self._mock_thread_cls = self._p_thread_cls.start()
        self.addCleanup(self._p_thread_cls.stop)
        self._mock_thread_cls.side_effect = self._thread_factory

    def _thread_factory(self, *args, **kwargs) -> MagicMock:
        from tools.workspace.worker_registry import WorkerRegistry

        t = MagicMock()
        t.instance_id = kwargs.get("instance_id", 1)
        t.instance_label = WorkerRegistry.instance_label(
            kwargs.get("name", WORKER), t.instance_id
        )
        t.status = "ready"
        t._timeout_seconds = 30
        t._worker_ctx = None
        t._worker_dir = MagicMock()
        t._output_queue = MagicMock()
        t._last_elapsed = MagicMock(return_value=None)
        return t

    def _restore_registry(self) -> None:
        with self._registry._registry_lock:
            self._registry._worker_registry.clear()
            self._registry._worker_registry.update(self._old_registry)
        with self._registry._bus_registry_lock:
            self._registry._worker_event_bus_registry.clear()
            self._registry._worker_event_bus_registry.update(self._old_buses)

    def _register_stale(self) -> _NeverStartedInstance:
        stale = _NeverStartedInstance()
        self.assertIsNone(
            stale.ident, "fixture must be a never-started thread (ident is None)"
        )
        with self._registry._registry_lock:
            self._registry._worker_registry[(SESSION, WORKER, 1)] = stale
        return stale

    def _spawn(self, force: bool) -> dict:
        tool = Worker(
            action="spawn",
            worker_name=WORKER,
            session_id=SESSION,
            workspace_path=str(self._ws),
            agent_config={"provider": "openai", "model": "gpt-4"},
            force=force,
            context={"query": "do the thing"},
        )
        return json.loads(tool.execute())

    def test_force_spawn_skips_the_join_instead_of_raising(self) -> None:
        stale = self._register_stale()

        with self.assertLogs("tools.workspace.worker", level="DEBUG") as captured:
            result = self._spawn(force=True)

        self.assertTrue(result.get("spawned"), f"forced spawn failed: {result}")
        self.assertEqual(stale.kill_calls, 1, "the stale instance was never killed")

        messages = [record.getMessage() for record in captured.records]
        noisy = [
            record.getMessage()
            for record in captured.records
            if record.levelno >= logging.WARNING
            and (
                "Error killing stale worker" in record.getMessage()
                or "cannot join thread before it is started" in record.getMessage()
            )
        ]
        self.assertEqual(
            noisy,
            [],
            "the never-started instance still produced the join traceback",
        )

        skipped = [m for m in messages if "no thread ident" in m]
        self.assertEqual(
            len(skipped), 1, f"the skipped join was not reported once: {messages}"
        )
        self.assertIn("never started", skipped[0])
        self.assertIn(WORKER, skipped[0])

        # The skip must not masquerade as a survivor: the entry is recycled.
        self.assertEqual(stale.save_calls, 1, "the stale context was not persisted")
        self.assertFalse(
            any("survived force-stop" in m for m in messages),
            f"a never-started thread was reported as surviving: {messages}",
        )
        with self._registry._registry_lock:
            leftover = [
                key
                for key, thread in self._registry._worker_registry.items()
                if thread is stale
            ]
        self.assertEqual(
            leftover,
            [],
            "the never-started instance's registry entry was kept: invisible orphan",
        )


if __name__ == "__main__":
    unittest.main()
