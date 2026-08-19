"""
Regression tests for the per-session worker spawn cap (``max_workers``).

Covered (in order):
  (a) spawning beyond the session cap is refused with a clean error;
  (b) a safe default cap exists when no config key is set;
  (c) the session config key ``max_workers`` raises/lowers the cap;
  (d) force-replacing an existing worker does NOT count as a new spawn.

Harness mirrors tests/tools/test_workspace_tools.py (Worker tool exercised via
``execute()`` with ``resolve_workspace_id`` / ``_workspace_dir`` / ``WorkerThread``
patched) plus the FakeEventBus/_RunSafetyPatches-style unittest scaffolding from
tests/test_worker_timeout_audit.py.  The real WorkerRegistry singleton is used so
both the module-level ``_worker_registry`` alias and
``_find_all_worker_threads`` (used by the force path) observe the same entries.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock
from unittest.mock import MagicMock

from tools.workspace.worker import Worker

SESSION = "sess-max-workers"
WORKER_NAMES = [f"w{i}" for i in range(1, 11)]  # w1..w10 in workers.json


def _parse_result(result: str) -> dict:
    return json.loads(result)


def _live_thread(status: str = "ready") -> MagicMock:
    t = MagicMock()
    t.is_alive.return_value = True
    t.status = status
    t._timeout_seconds = 30  # keep the force-path join loop bounded
    return t


def _dead_thread() -> MagicMock:
    t = MagicMock()
    t.is_alive.return_value = False
    t.status = "stopped"
    t._timeout_seconds = 30
    return t


class TestWorkerMaxWorkers(unittest.TestCase):
    """Per-session worker spawn cap (``max_workers``)."""

    def setUp(self) -> None:
        # ── Snapshot the singleton registry so we can seed and restore it ──
        from tools.workspace.worker_registry import WorkerRegistry

        self._registry = WorkerRegistry.get_instance()
        with self._registry._registry_lock:
            self._old_registry = dict(self._registry._worker_registry)
            self._registry._worker_registry.clear()
        self.addCleanup(self._restore_registry)

        # ── Patch workspace plumbing so _action_spawn reaches the cap logic ──
        self._p_resolve = mock.patch(
            "tools.workspace.worker.resolve_workspace_id", return_value="ws_cap_test"
        )
        self._p_ws_dir = mock.patch("tools.workspace.worker._workspace_dir")
        self._p_thread_cls = mock.patch("tools.workspace.worker.WorkerThread")
        self._mock_ws_dir = self._p_ws_dir.start()
        self._mock_thread_cls = self._p_thread_cls.start()
        self._p_resolve.start()
        self.addCleanup(self._p_resolve.stop)
        self.addCleanup(self._p_ws_dir.stop)
        self.addCleanup(self._p_thread_cls.stop)

        self._setup_workspace()

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

    def _seed_workers(
        self, names: list[str], session: str = SESSION, alive: bool = True
    ) -> None:
        with self._registry._registry_lock:
            for name in names:
                self._registry._worker_registry[(session, name, 1)] = (
                    _live_thread() if alive else _dead_thread()
                )

    def _spawn(
        self,
        name: str,
        session: str = SESSION,
        force: bool = False,
        agent_config: dict | None = None,
    ) -> dict:
        tool = Worker(
            action="spawn",
            worker_name=name,
            session_id=session,
            workspace_path="/tmp/test_ws",
            agent_config=agent_config
            or {"provider": "openai", "model": "gpt-4"},
            force=force,
        )
        return _parse_result(tool.execute())

    # ── (a) beyond-cap spawns are refused ────────────────────────────────

    def test_spawn_beyond_cap_refused(self):
        """Spawn past the cap is refused with a clean error, no thread created."""
        self._seed_workers(["w1", "w2", "w3", "w4", "w5"])
        result = self._spawn("w6")
        self.assertIn("error", result)
        self.assertIn("limit", result["error"].lower())
        self.assertEqual(result.get("max_workers"), 5)
        self.assertEqual(result.get("live_workers"), 5)
        self._mock_thread_cls.assert_not_called()

    def test_dead_workers_do_not_count_toward_cap(self):
        """Only LIVE workers count; a stopped registry entry is ignored."""
        self._seed_workers(["w1", "w2", "w3", "w4"], alive=True)
        self._seed_workers(["w5"], alive=False)
        result = self._spawn("w6")
        self.assertTrue(result.get("spawned"), f"4 live < cap 5: {result}")

    # ── (b) safe default cap without config ─────────────────────────────

    def test_default_cap_without_config_key(self):
        """With no config key, the safe default cap (5) applies."""
        for i in range(1, 6):
            result = self._spawn(f"w{i}")
            self.assertTrue(
                result.get("spawned"), f"spawn w{i} should succeed below cap: {result}"
            )
        result = self._spawn("w6")
        self.assertIn("error", result)
        self.assertEqual(result.get("max_workers"), 5)

    # ── (c) session config key raises/lowers the cap ─────────────────────

    def test_config_key_lowers_cap(self):
        """The session config key lowers the cap below the default."""
        cfg = {
            "provider": "openai",
            "model": "gpt-4",
            "session_config": {"max_workers": 2},
        }
        for i in range(1, 3):
            result = self._spawn(f"w{i}", agent_config=cfg)
            self.assertTrue(
                result.get("spawned"), f"spawn w{i} should succeed below cap 2: {result}"
            )
        result = self._spawn("w3", agent_config=cfg)
        self.assertIn("error", result)
        self.assertEqual(result.get("max_workers"), 2)

    def test_config_key_raises_cap(self):
        """The session config key raises the cap above the default."""
        cfg = {
            "provider": "openai",
            "model": "gpt-4",
            "session_config": {"max_workers": 7},
        }
        for i in range(1, 8):
            result = self._spawn(f"w{i}", agent_config=cfg)
            self.assertTrue(
                result.get("spawned"), f"spawn w{i} should succeed under cap 7: {result}"
            )
        result = self._spawn("w8", agent_config=cfg)
        self.assertIn("error", result)
        self.assertEqual(result.get("max_workers"), 7)

    def test_config_key_top_level_agent_config(self):
        """The key is also honoured at the top level of the injected agent config."""
        cfg = {"provider": "openai", "model": "gpt-4", "max_workers": 1}
        result = self._spawn("w1", agent_config=cfg)
        self.assertTrue(result.get("spawned"), f"first spawn under cap 1: {result}")
        result = self._spawn("w2", agent_config=cfg)
        self.assertIn("error", result)
        self.assertEqual(result.get("max_workers"), 1)

    # ── (d) force-replace does not count as a new spawn ─────────────────

    def test_force_replace_does_not_count_as_new_spawn(self):
        """At the cap, force-replacing an existing worker is still allowed."""
        self._seed_workers(["w1", "w2", "w3", "w4", "w5"])
        result = self._spawn("w3", force=True)
        self.assertTrue(
            result.get("spawned"),
            f"force-replace should not count as a new spawn: {result}",
        )
        self.assertEqual(result.get("worker_name"), "w3")
        # Registry still holds exactly 5 live entries (4 old + 1 replacement).
        with self._registry._registry_lock:
            live = [
                (sid, name)
                for (sid, name, _iid), t in self._registry._worker_registry.items()
                if sid == SESSION and t.is_alive()
            ]
        self.assertEqual(len(live), 5)


if __name__ == "__main__":
    unittest.main()
