"""
Operation semantics for the instance-aware worker registry (Unit A).

Covers the 3-tuple ``(session_id, worker_name, instance_id)`` registry
semantics end-to-end:

  1. name-only spawns allocate fresh instance ids (1, 2, ...) and every
     instance is a separate registry entry;
  2. every live instance counts toward the per-session spawn cap;
  3. name-only ``check`` against multiple live instances errors with an
     ambiguity message (labels included); ``instance_id`` disambiguates;
  4. a name-only spawn when a single live instance exists allocates the
     next free instance id;
  5. a name-only spawn when the single live instance is PAUSED auto-resumes
     it instead of spawning a duplicate;
  6. ``force=True`` stops *all* instances of the name (across sessions) and
     respawns a fresh instance 1;
  7. the job registry tracks jobs per ``instance_id``;
  8. the WorkerRegistry singleton API is 3-tuple aware end-to-end.

Harness mirrors tests/test_worker_max_workers.py (real WorkerRegistry
singleton; ``resolve_workspace_id`` / ``_workspace_dir`` / ``WorkerThread``
patched).  The mocked ``WorkerThread`` is given a side-effect factory so the
spawn return payload carries real ``instance_id`` / ``instance_label`` values
instead of MagicMock serializations.
"""

from __future__ import annotations

import json
import queue
import unittest
from unittest import mock
from unittest.mock import MagicMock

from tools.workspace.worker import Worker

SESSION = "sess-op-semantics"
WORKER_NAMES = [f"w{i}" for i in range(1, 11)]  # w1..w10 in workers.json


def _parse_result(result: str) -> dict:
    return json.loads(result)


def _live_thread(status: str = "ready") -> MagicMock:
    t = MagicMock()
    t.is_alive.return_value = True
    t.status = status
    t._manual_only_pause = False  # explicit: bare MagicMock auto-creates truthy attrs
    t._timeout_seconds = 30  # keep the force-path join loop bounded
    # The check success path reads ``_worker_ctx.user_history`` for
    # conversation_length; a MagicMock would blow up on len().
    t._worker_ctx = None
    return t


class TestWorkerOperationSemantics(unittest.TestCase):
    """Instance-aware worker spawn/check/stop semantics."""

    def setUp(self) -> None:
        # ── Snapshot the singleton registries so we can seed and restore ──
        from tools.workspace.worker_registry import WorkerRegistry

        self._registry = WorkerRegistry.get_instance()
        with self._registry._registry_lock:
            self._old_registry = dict(self._registry._worker_registry)
            self._registry._worker_registry.clear()
        with self._registry._bus_registry_lock:
            self._old_buses = dict(self._registry._worker_event_bus_registry)
            self._registry._worker_event_bus_registry.clear()
        self.addCleanup(self._restore_registry)

        # ── Patch workspace plumbing so _action_spawn reaches the logic ──
        self._p_resolve = mock.patch(
            "tools.workspace.worker.resolve_workspace_id", return_value="ws_op_sem_test"
        )
        self._p_ws_dir = mock.patch("tools.workspace.worker._workspace_dir")
        self._p_thread_cls = mock.patch("tools.workspace.worker.WorkerThread")
        self._mock_ws_dir = self._p_ws_dir.start()
        self._mock_thread_cls = self._p_thread_cls.start()
        self._p_resolve.start()
        self.addCleanup(self._p_resolve.stop)
        self.addCleanup(self._p_ws_dir.stop)
        self.addCleanup(self._p_thread_cls.stop)

        # The spawn return payload uses thread.instance_id / instance_label,
        # so the mocked WorkerThread must produce instances carrying them.
        self._mock_thread_cls.side_effect = self._thread_factory

        self._setup_workspace()

    # ── harness helpers ────────────────────────────────────────────────

    def _thread_factory(self, *args, **kwargs) -> MagicMock:
        from tools.workspace.worker_registry import WorkerRegistry

        t = MagicMock()
        t.instance_id = kwargs.get("instance_id", 1)
        t.instance_label = WorkerRegistry.instance_label(
            kwargs.get("name", "worker"), t.instance_id
        )
        return t

    def _restore_registry(self) -> None:
        with self._registry._registry_lock:
            self._registry._worker_registry.clear()
            self._registry._worker_registry.update(self._old_registry)
        with self._registry._bus_registry_lock:
            self._registry._worker_event_bus_registry.clear()
            self._registry._worker_event_bus_registry.update(self._old_buses)

    def _setup_workspace(self) -> None:
        mock_dir = MagicMock()
        mock_file = MagicMock()
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps(
            [{"name": n, "status": "ready"} for n in WORKER_NAMES]
        )
        mock_dir.__truediv__.return_value = mock_file
        self._mock_ws_dir.return_value = mock_dir

    def _seed_fake(
        self, session: str, name: str, iid: int, status: str = "ready"
    ) -> MagicMock:
        t = _live_thread(status=status)
        with self._registry._registry_lock:
            self._registry._worker_registry[(session, name, iid)] = t
        return t

    def _spawn(
        self,
        name: str,
        session: str = SESSION,
        force: bool = False,
        instance_id: int | None = None,
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
            instance_id=instance_id,
        )
        return _parse_result(tool.execute())

    def _check(self, name: str, session: str = SESSION, instance_id=None) -> dict:
        tool = Worker(
            action="check",
            worker_name=name,
            session_id=session,
            workspace_path="/tmp/test_ws",
            instance_id=instance_id,
        )
        return _parse_result(tool.execute())

    def _registry_keys_for(self, name: str) -> list:
        with self._registry._registry_lock:
            return sorted(
                k for k in self._registry._worker_registry if k[1] == name
            )

    # ── 1. name-only spawns allocate fresh instance ids ────────────────

    def test_spawn_same_name_creates_instances(self):
        """Two name-only spawns of the same worker produce instances 1 and 2."""
        r1 = self._spawn("w1")
        self.assertTrue(r1.get("spawned"), f"first spawn: {r1}")
        self.assertEqual(r1.get("instance_id"), 1)
        self.assertEqual(r1.get("instance_label"), "w1")

        r2 = self._spawn("w1")
        self.assertTrue(r2.get("spawned"), f"second spawn: {r2}")
        self.assertEqual(r2.get("instance_id"), 2)
        self.assertEqual(r2.get("instance_label"), "w1#2")

        self.assertEqual(
            self._registry_keys_for("w1"),
            [(SESSION, "w1", 1), (SESSION, "w1", 2)],
        )

    # ── 2. every live instance counts toward the cap ───────────────────

    def test_spawn_instances_count_toward_cap(self):
        """Instances of the same name count individually toward the cap."""
        for name in ["w1", "w1", "w2", "w2", "w2"]:  # 5 live instances
            result = self._spawn(name)
            self.assertTrue(result.get("spawned"), f"{name}: {result}")

        result = self._spawn("w3")
        self.assertIn("error", result)
        self.assertIn("limit", result["error"].lower())
        self.assertEqual(result.get("max_workers"), 5)
        self.assertEqual(result.get("live_workers"), 5)

    # ── 3. ambiguous name-only actions require instance_id ─────────────

    def test_ambiguous_name_only_actions_error(self):
        """Name-only check against 2 live instances errors with labels."""
        self._seed_fake(SESSION, "wa", 1)
        self._seed_fake(SESSION, "wa", 2)

        result = self._check("wa")
        self.assertIn("error", result)
        self.assertIn("ambiguous", result["error"])
        self.assertIn("wa#2", result["error"])  # labels in the message
        self.assertEqual(result.get("worker_name"), "wa")

        # Explicit instance_id resolves the ambiguity.
        resolved = self._check("wa", instance_id=1)
        self.assertEqual(resolved.get("instance_id"), 1)
        self.assertEqual(resolved.get("instance_label"), "wa")
        self.assertEqual(resolved.get("session_id"), SESSION)
        self.assertEqual(resolved.get("status"), "ready")

    # ── 4. single live instance → next free id on name-only spawn ──────

    def test_spawn_name_only_single_instance_works(self):
        """One live instance present: name-only spawn takes the next id."""
        self._seed_fake(SESSION, "w1", 1)
        result = self._spawn("w1")
        self.assertTrue(result.get("spawned"), f"spawn: {result}")
        self.assertEqual(result.get("instance_id"), 2)
        self.assertEqual(result.get("instance_label"), "w1#2")
        self.assertEqual(
            self._registry_keys_for("w1"),
            [(SESSION, "w1", 1), (SESSION, "w1", 2)],
        )

    # ── 5. sole PAUSED instance → auto-resume instead of duplicate ─────

    def test_spawn_paused_auto_resume_only_single(self):
        """A single paused instance is resumed, not duplicated."""
        fake = _live_thread(status="paused")
        fake.instance_id = 1
        fake.instance_label = "w2"
        fake._input_queue = queue.Queue()
        fake._output_queue = MagicMock()
        fake._output_queue.get.return_value = '{"response": "hi"}'
        fake._last_elapsed.return_value = None
        with self._registry._registry_lock:
            self._registry._worker_registry[(SESSION, "w2", 1)] = fake

        result = self._spawn("w2")
        fake.resume.assert_called_once()
        self.assertTrue(result.get("spawned"), f"resume result: {result}")
        self.assertEqual(result.get("instance_id"), 1)
        self.assertEqual(result.get("instance_label"), "w2")
        self.assertEqual(result.get("response"), "hi")
        # No second instance was created.
        self.assertEqual(self._registry_keys_for("w2"), [(SESSION, "w2", 1)])

    # ── 6. force stops ALL instances of the name, then fresh spawn ─────

    def test_spawn_force_stops_all_same_name(self):
        """force=True stops every instance of the name and spawns fresh #1."""
        fake1 = self._seed_fake(SESSION, "w3", 1)
        fake2 = self._seed_fake(SESSION, "w3", 2)

        result = self._spawn("w3", force=True)
        fake1.stop.assert_called_once()
        fake2.stop.assert_called_once()
        self.assertTrue(result.get("spawned"), f"force spawn: {result}")
        self.assertEqual(result.get("instance_id"), 1)
        self.assertEqual(result.get("instance_label"), "w3")
        # Exactly one registry entry for "w3" remains.
        self.assertEqual(self._registry_keys_for("w3"), [(SESSION, "w3", 1)])

    # ── 7. job registry tracks jobs per instance_id ────────────────────

    def test_job_registry_instance_id(self):
        """Job records are attributable to the worker instance_id."""
        from tools.workspace.job_registry import WorkerJobRegistry

        reg = WorkerJobRegistry(event_bus=MagicMock())
        reg.register("j1", "w", session_id="s", instance_id=2)
        reg.register("j2", "w", session_id="s", instance_id=1)
        reg.register("j3", "w", session_id="s")  # no instance_id

        def ids_for(**filters) -> list:
            return [r["job_id"] for r in reg.jobs(worker_name="w", **filters)]

        self.assertEqual(ids_for(instance_id=2), ["j1"])
        self.assertEqual(ids_for(instance_id=1), ["j2"])
        # No instance_id filter → all jobs for the worker.
        self.assertEqual(ids_for(), ["j1", "j2", "j3"])

        reg.update("j1", instance_id=3)
        self.assertEqual(ids_for(instance_id=3), ["j1"])
        self.assertEqual(ids_for(instance_id=2), [])

        reg.complete("j2", {"content": "done"})
        rec = reg.job("j2")
        self.assertEqual(rec["status"], "completed")
        # The completed record keeps the instance it ran on.
        self.assertEqual(rec["instance_id"], 1)

    # ── 8. WorkerRegistry singleton API is 3-tuple aware ───────────────

    def test_worker_registry_3tuple_keys(self):
        """register/get/unregister/find are keyed by (session, name, iid)."""
        from tools.workspace.worker_registry import WorkerRegistry

        reg = WorkerRegistry.get_instance()
        thread = MagicMock()

        reg.register_worker("s", "w", thread, instance_id=2)
        self.assertIs(reg.get_worker("s", "w", 2), thread)
        # Default instance_id is 1 — a different instance is not found.
        self.assertIsNone(reg.get_worker("s", "w"))
        self.assertIsNone(reg.get_worker("s", "w", 1))

        self.assertIs(reg.unregister_worker("s", "w", 2), thread)
        self.assertIsNone(reg.get_worker("s", "w", 2))

        reg.register_worker("s", "w", thread, instance_id=2)
        self.assertEqual(reg.find_workers_by_name("w"), [("s", thread)])

        self.assertEqual(WorkerRegistry.instance_label("w", None), "w")
        self.assertEqual(WorkerRegistry.instance_label("w", 1), "w")
        self.assertEqual(WorkerRegistry.instance_label("w", 2), "w#2")


# ══════════════════════════════════════════════════════════════════════
# Unit B — join / wait_for_job semantics (Phase 2B job polling)
# Unit C — pause/resume v2 (main-agent pause, Pause All, manual-only)
#══════════════════════════════════════════════════════════════════════

import threading
import time
from types import SimpleNamespace

from tools.workspace.job_registry import WorkerJobRegistry
from tools.workspace.worker import WorkerThread


def _join_thread(status: str = "ready") -> MagicMock:
    """Live thread fake with the join-path attributes (real stop event)."""
    t = _live_thread(status=status)
    t._stop_event = threading.Event()
    return t


class _OpSemBase(unittest.TestCase):
    """Shared harness for the Unit B/C suites (mirrors the Unit A fixture)."""

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

        self._p_resolve = mock.patch(
            "tools.workspace.worker.resolve_workspace_id", return_value="ws_op_sem_test"
        )
        self._p_ws_dir = mock.patch("tools.workspace.worker._workspace_dir")
        self._p_thread_cls = mock.patch("tools.workspace.worker.WorkerThread")
        self._mock_ws_dir = self._p_ws_dir.start()
        self._mock_thread_cls = self._p_thread_cls.start()
        self._p_resolve.start()
        self.addCleanup(self._p_resolve.stop)
        self.addCleanup(self._p_ws_dir.stop)
        self.addCleanup(self._p_thread_cls.stop)

        self._mock_thread_cls.side_effect = self._thread_factory

        self._setup_workspace()

    # ── harness helpers (mirror Unit A) ────────────────────────────────────────────────

    def _thread_factory(self, *args, **kwargs) -> MagicMock:
        from tools.workspace.worker_registry import WorkerRegistry

        t = MagicMock()
        t.instance_id = kwargs.get("instance_id", 1)
        t.instance_label = WorkerRegistry.instance_label(
            kwargs.get("name", "worker"), t.instance_id
        )
        return t

    def _restore_registry(self) -> None:
        with self._registry._registry_lock:
            self._registry._worker_registry.clear()
            self._registry._worker_registry.update(self._old_registry)
        with self._registry._bus_registry_lock:
            self._registry._worker_event_bus_registry.clear()
            self._registry._worker_event_bus_registry.update(self._old_buses)

    def _setup_workspace(self) -> None:
        mock_dir = MagicMock()
        mock_file = MagicMock()
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps(
            [{"name": n, "status": "ready"} for n in WORKER_NAMES]
        )
        mock_dir.__truediv__.return_value = mock_file
        self._mock_ws_dir.return_value = mock_dir

    def _seed_fake(
        self, session: str, name: str, iid: int, status: str = "ready"
    ) -> MagicMock:
        t = _live_thread(status=status)
        with self._registry._registry_lock:
            self._registry._worker_registry[(session, name, iid)] = t
        return t

    def _seed_join_fake(
        self, session: str, name: str, iid: int, status: str = "ready"
    ) -> MagicMock:
        t = _join_thread(status=status)
        with self._registry._registry_lock:
            self._registry._worker_registry[(session, name, iid)] = t
        return t

    def _spawn(
        self,
        name: str,
        session: str = SESSION,
        force: bool = False,
        instance_id: int | None = None,
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
            instance_id=instance_id,
        )
        return _parse_result(tool.execute())

    def _registry_keys_for(self, name: str) -> list:
        with self._registry._registry_lock:
            return sorted(
                k for k in self._registry._worker_registry if k[1] == name
            )

    def _join(
        self,
        name: str,
        job_id: str,
        session: str = SESSION,
        timeout_seconds: int | None = None,
        instance_id: int | None = None,
        action: str = "join",
    ) -> dict:
        tool = Worker(
            action=action,
            worker_name=name,
            session_id=session,
            workspace_path="/tmp/test_ws",
            worker_query=job_id,
            timeout_seconds=timeout_seconds,
            instance_id=instance_id,
        )
        return _parse_result(tool.execute())

    def _pause(
        self,
        worker_query: str | None = "",
        name: str | None = None,
        session: str = SESSION,
    ) -> dict:
        tool = Worker(
            action="pause",
            worker_name=name,
            session_id=session,
            workspace_path="/tmp/test_ws",
            worker_query=worker_query,
        )
        return _parse_result(tool.execute())

    def _resume(
        self,
        worker_query: str | None = "",
        name: str | None = None,
        session: str = SESSION,
    ) -> dict:
        tool = Worker(
            action="resume",
            worker_name=name,
            session_id=session,
            workspace_path="/tmp/test_ws",
            worker_query=worker_query,
        )
        return _parse_result(tool.execute())


class TestWorkerJoinSemantics(_OpSemBase):
    """Unit B: join / wait_for_job poll a job to a terminal state."""

    def setUp(self) -> None:
        super().setUp()
        self._job_reg = WorkerJobRegistry(event_bus=MagicMock())
        self._p_jobreg = mock.patch(
            "tools.workspace.worker._get_worker_job_registry",
            return_value=self._job_reg,
        )
        self._p_jobreg.start()
        self.addCleanup(self._p_jobreg.stop)

    def _register(self, job_id: str, name: str = "w1", iid: int = 1) -> None:
        self._job_reg.register(job_id, name, session_id=SESSION, instance_id=iid)

    def test_join_returns_on_completed(self):
        """A completed job returns the terminal payload immediately."""
        self._seed_join_fake(SESSION, "w1", 1)
        self._register("j1")
        # update() whitelists only status/preview/completed_at; result goes via complete().
        self._job_reg.complete("j1", {"content": "done"})

        start = time.monotonic()
        result = self._join("w1", "j1", timeout_seconds=60)
        elapsed = time.monotonic() - start

        self.assertEqual(result.get("status"), "completed")
        self.assertEqual(result.get("instance_id"), 1)
        self.assertEqual(result.get("instance_label"), "w1")
        self.assertTrue(result.get("has_result"))
        self.assertEqual(result.get("result"), {"content": "done"})
        self.assertLess(elapsed, 5.0)

    def test_wait_for_job_alias_returns_on_completed(self):
        """wait_for_job dispatches to the same join handler."""
        self._seed_join_fake(SESSION, "w1", 1)
        self._register("ja")
        self._job_reg.complete("ja", {"content": "done"})

        result = self._join("w1", "ja", action="wait_for_job")
        self.assertEqual(result.get("status"), "completed")

    def test_join_returns_on_partial_result(self):
        """A partial result with preview returns partial_result promptly."""
        self._seed_join_fake(SESSION, "w1", 1)
        self._register("jp")
        self._job_reg.update("jp", status="partial", preview="first chunk")

        result = self._join("w1", "jp")
        self.assertEqual(result.get("status"), "partial_result")
        self.assertEqual(result.get("preview"), "first chunk")

    def test_join_returns_on_terminal_timeout(self):
        """A job in terminal 'timeout' state returns immediately."""
        self._seed_join_fake(SESSION, "w1", 1)
        self._register("jt")
        self._job_reg.update("jt", status="timeout")

        result = self._join("w1", "jt")
        self.assertEqual(result.get("status"), "timeout")

    def test_join_returns_on_terminal_error(self):
        """A job in terminal 'error' state returns immediately."""
        self._seed_join_fake(SESSION, "w1", 1)
        self._register("je")
        self._job_reg.update("je", status="error", preview="boom")

        result = self._join("w1", "je")
        self.assertEqual(result.get("status"), "error")
        self.assertEqual(result.get("preview"), "boom")

    def test_join_paused_worker_returns_promptly(self):
        """A paused worker short-circuits the wait instead of the full 60s."""
        self._seed_join_fake(SESSION, "w1", 1, status="paused")
        self._register("jpw")  # still submitted — never completes

        start = time.monotonic()
        result = self._join("w1", "jpw", timeout_seconds=60)
        elapsed = time.monotonic() - start

        self.assertEqual(result.get("status"), "paused")
        self.assertIn("paused", result.get("note", ""))
        self.assertLess(elapsed, 5.0)

    def test_join_default_timeout_returns_early_on_terminal(self):
        """Default timeout is 60s but a terminal state returns immediately."""
        self._seed_join_fake(SESSION, "w1", 1)
        self._register("jd")
        self._job_reg.update("jd", status="completed")

        start = time.monotonic()
        result = self._join("w1", "jd")  # no timeout_seconds
        elapsed = time.monotonic() - start

        self.assertEqual(result.get("status"), "completed")
        self.assertLess(elapsed, 5.0)

    def test_join_unknown_job_id_errors(self):
        """An unregistered job_id returns an explicit error."""
        self._seed_join_fake(SESSION, "w1", 1)
        result = self._join("w1", "does-not-exist")
        self.assertEqual(result.get("error"), "Job not found")
        self.assertEqual(result.get("job_id"), "does-not-exist")

    def test_join_times_out_after_deadline(self):
        """A never-terminal job returns status timeout at the deadline."""
        self._seed_join_fake(SESSION, "w1", 1)
        self._register("jdl")  # stays 'submitted'

        start = time.monotonic()
        result = self._join("w1", "jdl", timeout_seconds=1)
        elapsed = time.monotonic() - start

        self.assertEqual(result.get("status"), "timeout")
        self.assertIn("submitted", result.get("note", ""))
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertLess(elapsed, 30.0)

    def test_join_registry_unavailable_errors(self):
        """A None job registry yields a clear error instead of crashing."""
        self._seed_join_fake(SESSION, "w1", 1)
        with mock.patch(
            "tools.workspace.worker._get_worker_job_registry", return_value=None
        ):
            result = self._join("w1", "j1")
        self.assertEqual(result.get("error"), "Worker job registry unavailable")

    def test_join_requires_worker_query(self):
        """join without a job_id errors immediately."""
        self._seed_join_fake(SESSION, "w1", 1)
        tool = Worker(
            action="join",
            worker_name="w1",
            session_id=SESSION,
            workspace_path="/tmp/test_ws",
        )
        result = _parse_result(tool.execute())
        self.assertIn("worker_query is required", result.get("error", ""))

    def test_join_requires_worker_name(self):
        """join without worker_name is rejected by the execute() guard."""
        tool = Worker(
            action="join",
            session_id=SESSION,
            workspace_path="/tmp/test_ws",
            worker_query="j1",
        )
        result = _parse_result(tool.execute())
        self.assertIn("worker_name is required", result.get("error", ""))


class TestWorkerPauseResumeSemantics(_OpSemBase):
    """Unit C: pause/resume v2 — main-agent pause, Pause All, manual-only."""

    def setUp(self) -> None:
        super().setUp()
        from tools.workspace import worker as worker_mod

        self._worker_mod = worker_mod
        self._old_main_paused = set(worker_mod._SESSION_MAIN_PAUSED)
        self.addCleanup(self._restore_main_paused)

        self._job_reg = WorkerJobRegistry(event_bus=MagicMock())
        self._p_jobreg = mock.patch(
            "tools.workspace.worker._get_worker_job_registry",
            return_value=self._job_reg,
        )
        self._p_jobreg.start()
        self.addCleanup(self._p_jobreg.stop)

    def _restore_main_paused(self) -> None:
        self._worker_mod._SESSION_MAIN_PAUSED.clear()
        self._worker_mod._SESSION_MAIN_PAUSED.update(self._old_main_paused)

    # ── Pause Main ─────────────────────────────────────────────────────────────

    def test_pause_main_only_empty_query(self):
        """Empty worker_query pauses the main agent, not the workers."""
        fake = self._seed_join_fake(SESSION, "w1", 1)
        result = self._pause(worker_query="")

        self.assertEqual(result.get("status"), "paused")
        self.assertEqual(result.get("scope"), "main")
        self.assertTrue(result.get("main_agent_paused"))
        self.assertEqual(result.get("workers_paused"), [])
        self.assertIn(SESSION, self._worker_mod._SESSION_MAIN_PAUSED)
        fake.pause.assert_not_called()

    def test_pause_main_only_main_token(self):
        """'main' token pauses the main agent only."""
        fake = self._seed_join_fake(SESSION, "w1", 1)
        result = self._pause(worker_query="main")

        self.assertTrue(result.get("main_agent_paused"))
        self.assertIn(SESSION, self._worker_mod._SESSION_MAIN_PAUSED)
        fake.pause.assert_not_called()

    def test_pause_all_pauses_live_workers_only(self):
        """'all' cooperatively pauses every live session worker; main untouched."""
        f1 = self._seed_join_fake(SESSION, "w1", 1)
        f2 = self._seed_join_fake(SESSION, "w1", 2)
        f3 = self._seed_join_fake(SESSION, "w2", 1)
        dead = self._seed_join_fake(SESSION, "w3", 1)
        dead.is_alive.return_value = False

        result = self._pause(worker_query="all")

        self.assertFalse(result.get("main_agent_paused"))
        self.assertNotIn(SESSION, self._worker_mod._SESSION_MAIN_PAUSED)
        f1.pause.assert_called_once()
        f2.pause.assert_called_once()
        f3.pause.assert_called_once()
        dead.pause.assert_not_called()
        paused = {(w["worker_name"], w["instance_id"]) for w in result["workers_paused"]}
        self.assertEqual(paused, {("w1", 1), ("w1", 2), ("w2", 1)})

    def test_pause_all_with_main_token_pauses_both(self):
        """'all main' pauses workers AND the main agent."""
        f1 = self._seed_join_fake(SESSION, "w1", 1)
        result = self._pause(worker_query="all main")

        self.assertTrue(result.get("main_agent_paused"))
        self.assertIn(SESSION, self._worker_mod._SESSION_MAIN_PAUSED)
        f1.pause.assert_called_once()

    def test_pause_worker_name_targeting(self):
        """worker_name targets one worker and leaves the main agent alone."""
        f1 = self._seed_join_fake(SESSION, "w1", 1)
        f2 = self._seed_join_fake(SESSION, "w2", 1)

        result = self._pause(worker_query="all", name="w1")

        f1.pause.assert_called_once()
        f2.pause.assert_not_called()
        self.assertNotIn(SESSION, self._worker_mod._SESSION_MAIN_PAUSED)
        self.assertEqual(
            [(w["worker_name"], w["instance_id"]) for w in result["workers_paused"]],
            [("w1", 1)],
        )

    def test_resume_clears_main_and_workers(self):
        """resume discards the main-pause and resumes paused workers."""
        f1 = self._seed_join_fake(SESSION, "w1", 1)
        self._pause(worker_query="all main")  # main + w1 paused
        self.assertIn(SESSION, self._worker_mod._SESSION_MAIN_PAUSED)
        f1.status = "paused"
        f1._manual_only_pause = True

        result = self._resume(worker_query="all main")

        self.assertEqual(result.get("status"), "resumed")
        self.assertNotIn(SESSION, self._worker_mod._SESSION_MAIN_PAUSED)
        f1.resume.assert_called_once()
        self.assertFalse(f1._manual_only_pause)

    def test_join_returns_paused_when_main_paused(self):
        """While the main agent is paused, join returns paused promptly."""
        self._seed_join_fake(SESSION, "w1", 1)
        self._job_reg.register("jm", "w1", session_id=SESSION, instance_id=1)
        self._pause(worker_query="main")

        start = time.monotonic()
        result = self._join("w1", "jm", timeout_seconds=60)
        elapsed = time.monotonic() - start

        self.assertEqual(result.get("status"), "paused")
        self.assertIn("main agent is paused", result.get("note", ""))
        self.assertLess(elapsed, 5.0)

    # ── manual-only spawn guards ──────────────────────────────────────────────────────

    def _seed_manual_only_paused(
        self, session: str, name: str, iid: int
    ) -> MagicMock:
        from tools.workspace.worker_registry import WorkerRegistry

        fake = _live_thread(status="paused")
        fake._manual_only_pause = True
        fake.instance_id = iid
        fake.instance_label = WorkerRegistry.instance_label(name, iid)
        fake._input_queue = queue.Queue()
        fake._output_queue = MagicMock()
        fake._stop_event = threading.Event()
        with self._registry._registry_lock:
            self._registry._worker_registry[(session, name, iid)] = fake
        return fake

    def test_spawn_manual_only_paused_not_auto_resumed(self):
        """A manual-only paused instance is NOT auto-resumed by a query."""
        fake = self._seed_manual_only_paused(SESSION, "w2", 1)

        result = self._spawn("w2")

        self.assertEqual(result.get("status"), "paused")
        self.assertIn("manual-only", result.get("error", ""))
        fake.resume.assert_not_called()
        self.assertEqual(self._registry_keys_for("w2"), [(SESSION, "w2", 1)])

    def test_spawn_manual_only_paused_explicit_iid_not_auto_resumed(self):
        """Explicit instance_id targeting a manual-only pause also refuses."""
        fake = self._seed_manual_only_paused(SESSION, "w3", 3)

        result = self._spawn("w3", instance_id=3)

        self.assertEqual(result.get("status"), "paused")
        self.assertIn("manual-only", result.get("error", ""))
        fake.resume.assert_not_called()
        self.assertEqual(self._registry_keys_for("w3"), [(SESSION, "w3", 3)])

    def test_spawn_paused_live_instance_counts_toward_cap(self):
        """A paused-but-live instance still consumes a spawn-cap slot."""
        for name in ["w1", "w2", "w3", "w4"]:
            self.assertTrue(self._spawn(name).get("spawned"), name)
        self._seed_join_fake(SESSION, "w1", 2, status="paused")  # live → 5 total

        result = self._spawn("w5")

        self.assertIn("error", result)
        self.assertIn("limit", result["error"].lower())
        self.assertEqual(result.get("max_workers"), 5)
        self.assertEqual(result.get("live_workers"), 5)

    # ── exact-owner container cleanup ────────────────────────────────────────────────

    def test_cleanup_worker_containers_exact_owner(self):
        """Cleanup touches only containers labeled with this worker's identity."""
        owned = {
            "container_id": "c1",
            "name": "w1box",
            "labels": {"thoughtmachine.worker": "sess-op-semantics:w1"},
        }
        resource_label = {
            "container_id": "c2",
            "name": "res",
            "labels": {"thoughtmachine.resource": "1"},
        }
        resource_name = {"container_id": "c3", "name": "tm-res-cache"}
        sibling = {
            "container_id": "c4",
            "name": "w2box",
            "labels": {"thoughtmachine.worker": "sess-op-semantics:w2"},
        }
        other_sess = {
            "container_id": "c5",
            "name": "w1box",
            "labels": {"thoughtmachine.worker": "other-sess:w1"},
        }
        no_labels = {"container_id": "c6", "name": "x"}
        owned_obj = SimpleNamespace(
            name="w1obj", labels={"thoughtmachine.worker": "sess-op-semantics:w1"}
        )
        sibling_obj = SimpleNamespace(
            name="w2obj", labels={"thoughtmachine.worker": "sess-op-semantics:w2"}
        )

        cm = MagicMock()
        cm.list_containers.return_value = [
            owned, resource_label, resource_name, sibling,
            other_sess, no_labels, owned_obj, sibling_obj,
        ]
        fake = SimpleNamespace(
            _containers_cleaned=False,
            _container_manager=cm,
            owner_identity="sess-op-semantics:w1",
        )
        # _cleanup_worker_containers is normally invoked on a real WorkerThread,
        # which provides _is_worker_owned_container from its class. A bare
        # SimpleNamespace lacks it, and the method's defensive except-continue
        # would silently skip every container; bind the helper explicitly.
        fake._is_worker_owned_container = (
            WorkerThread._is_worker_owned_container.__get__(fake, WorkerThread)
        )

        WorkerThread._cleanup_worker_containers(fake)

        stopped = [c.args[0] for c in cm.stop.call_args_list]
        removed = [c.args[0] for c in cm.remove.call_args_list]
        self.assertIn("c1", stopped)
        self.assertIn(owned_obj, stopped)
        for cid in ("c2", "c3", "c4", "c5", "c6"):
            self.assertNotIn(cid, stopped)
            self.assertNotIn(cid, removed)
        self.assertNotIn(sibling_obj, stopped)
        self.assertNotIn(sibling_obj, removed)
        self.assertEqual(removed, stopped)

        # Idempotent: a second call stops/removes nothing more.
        n = len(stopped)
        WorkerThread._cleanup_worker_containers(fake)
        self.assertEqual(len(cm.stop.call_args_list), n)
        self.assertEqual(len(cm.remove.call_args_list), n)

    def test_cleanup_worker_containers_never_raises(self):
        """A failing container manager must not propagate exceptions."""
        cm = MagicMock()
        cm.list_containers.side_effect = RuntimeError("boom")
        fake = SimpleNamespace(
            _containers_cleaned=False,
            _container_manager=cm,
            owner_identity="sess-op-semantics:w1",
        )
        WorkerThread._cleanup_worker_containers(fake)  # must not raise

        no_cm = SimpleNamespace(
            _containers_cleaned=False,
            _container_manager=None,
            owner_identity="sess-op-semantics:w1",
        )
        WorkerThread._cleanup_worker_containers(no_cm)  # must not raise


if __name__ == "__main__":
    unittest.main()
