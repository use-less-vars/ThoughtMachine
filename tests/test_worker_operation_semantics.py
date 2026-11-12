"""
Operation semantics for the instance-aware worker registry (Unit A).

Covers the 3-tuple ``(session_id, worker_name, instance_id)`` registry
semantics end-to-end:

  1. name-only spawns allocate fresh instance ids (1, 2, ...) and every
     instance is a separate registry entry;
  2. every live instance counts toward the per-session spawn cap;
  3. name-only ``check`` against multiple live instances errors with an
     ambiguity message (labels included); ``instance_id`` disambiguates;
  4. a name-only spawn when a single live instance exists reuses it
     (continue-existing routing); an explicit ``instance_id`` allocates
     the next free id;
  5. a name-only spawn when the single live instance is PAUSED auto-resumes
     it instead of spawning a duplicate;
  6. ``force=True`` stops all of THIS session's instances of the name (a
     foreign session's instance of the same name survives untouched) and
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

import dataclasses
import json
import queue
import unittest
from unittest import mock
from unittest.mock import MagicMock

from agent.config.defaults import WORKER_TIMEOUT_SECONDS as _DETACHED_TTL_SECONDS
from tools.workspace.command_channel import COMMAND_FILENAME, write_command
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

    # PHASE 2.1a: model BOTH verbs.  stop() DETACHES: the thread parks and
    # stays alive, so a detach never unregisters or frees anything.  kill()
    # TERMINATES: is_alive() turns False, which is the only thing the
    # force-spawn block will unregister (a live one is kept, never orphaned).
    def _detach(*_a, **_k):
        t.status = "detached"
        t.is_alive.return_value = True

    def _kill(*_a, **_k):
        t.status = "killed"
        t.is_alive.return_value = False

    t.stop.side_effect = _detach
    t.kill.side_effect = _kill
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
        t.status = "ready"  # keep continue-existing reuse payloads JSON-serializable
        t._timeout_seconds = 30
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
        from tools.workspace.worker_registry import WorkerRegistry

        t = _live_thread(status=status)
        t.instance_id = iid
        t.instance_label = WorkerRegistry.instance_label(name, iid)
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
        """Same-name spawns: first creates instance 1; a second name-only
        spawn reuses it (continue-existing routing) instead of duplicating;
        an explicit instance_id then allocates the next free id."""
        r1 = self._spawn("w1")
        self.assertTrue(r1.get("spawned"), f"first spawn: {r1}")
        self.assertEqual(r1.get("instance_id"), 1)
        self.assertEqual(r1.get("instance_label"), "w1")

        r2 = self._spawn("w1")
        self.assertFalse(r2.get("spawned"), f"second spawn: {r2}")
        self.assertEqual(r2.get("instance_id"), 1)
        self.assertEqual(r2.get("instance_label"), "w1")
        self.assertIn("reusing", r2.get("message", "").lower())

        r3 = self._spawn("w1", instance_id=2)
        self.assertTrue(r3.get("spawned"), f"explicit iid spawn: {r3}")
        self.assertEqual(r3.get("instance_id"), 2)
        self.assertEqual(r3.get("instance_label"), "w1#2")

        self.assertEqual(
            self._registry_keys_for("w1"),
            [(SESSION, "w1", 1), (SESSION, "w1", 2)],
        )

    # ── 2. every live instance counts toward the cap ───────────────────

    def test_spawn_instances_count_toward_cap(self):
        """Instances of the same name count individually toward the cap."""
        for iid in (1, 2, 3):  # 3 live instances of the same name
            result = self._spawn("w1", instance_id=iid)
            self.assertTrue(result.get("spawned"), f"w1#{iid}: {result}")

        result = self._spawn("w1", instance_id=4)
        self.assertIn("error", result)
        self.assertIn("limit", result["error"].lower())
        self.assertEqual(result.get("max_workers"), 3)
        self.assertEqual(result.get("live_workers"), 3)

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

    # ── 4. single live instance → reuse (continue-existing) / explicit iid ──────

    def test_spawn_name_only_single_instance_works(self):
        """One live instance present: a name-only spawn reuses it instead of
        duplicating; an explicit instance_id then allocates the next free id."""
        self._seed_fake(SESSION, "w1", 1)
        result = self._spawn("w1")
        self.assertFalse(result.get("spawned"), f"spawn: {result}")
        self.assertEqual(result.get("instance_id"), 1)
        self.assertEqual(result.get("instance_label"), "w1")
        self.assertIn("reusing", result.get("message", "").lower())

        result2 = self._spawn("w1", instance_id=2)
        self.assertTrue(result2.get("spawned"), f"explicit iid spawn: {result2}")
        self.assertEqual(result2.get("instance_id"), 2)
        self.assertEqual(result2.get("instance_label"), "w1#2")
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
        """force=True kills every instance of the name and spawns fresh #1."""
        fake1 = self._seed_fake(SESSION, "w3", 1)
        fake2 = self._seed_fake(SESSION, "w3", 2)

        result = self._spawn("w3", force=True)
        # PHASE 2.1a: force TERMINATES (kill); detaching would leave both
        # instances parked and alive, holding their keys.
        fake1.kill.assert_called_once()
        fake2.kill.assert_called_once()
        fake1.stop.assert_not_called()
        fake2.stop.assert_not_called()
        self.assertTrue(result.get("spawned"), f"force spawn: {result}")
        self.assertEqual(result.get("instance_id"), 1)
        self.assertEqual(result.get("instance_label"), "w3")
        # Exactly one registry entry for "w3" remains.
        self.assertEqual(self._registry_keys_for("w3"), [(SESSION, "w3", 1)])

    # ── 6b. force is session-scoped ──────────────────────────────────

    def test_spawn_force_does_not_stop_other_session_instance(self):
        """force=True is session-scoped: another session's instance survives
        BOTH verbs: it is neither detached nor killed."""
        other_session = "sess-op-semantics-other"
        mine = self._seed_fake(SESSION, "w4", 1)
        foreign = self._seed_fake(other_session, "w4", 1)

        result = self._spawn("w4", force=True)

        mine.kill.assert_called_once()
        mine.stop.assert_not_called()
        foreign.stop.assert_not_called()
        foreign.kill.assert_not_called()
        self.assertTrue(result.get("spawned"), f"force spawn: {result}")
        self.assertEqual(result.get("instance_id"), 1)
        with self._registry._registry_lock:
            survivors = dict(self._registry._worker_registry)
        self.assertIs(
            survivors.get((other_session, "w4", 1)),
            foreign,
            "force spawn stopped/unregistered another session's instance",
        )

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

import pytest
import threading
import time
from types import SimpleNamespace

from tools.workspace.job_registry import WorkerJobRegistry
from tools.workspace.worker import WorkerThread

import tools.workspace.worker_container as worker_container
from thoughtmachine.container_record import LIFECYCLE_EPHEMERAL, RECORD_LABEL_KEY


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

    def _act(
        self,
        action: str,
        name: str | None = None,
        worker_query: str | None = "",
        session: str = SESSION,
    ) -> dict:
        """Run a raw tool action, including the operator-only kill/resume."""
        tool = Worker(
            action=action,
            worker_name=name,
            session_id=session,
            workspace_path="/tmp/test_ws",
            worker_query=worker_query,
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

    def test_join_defaults_worker_name_to_default(self):
        """join without worker_name addresses the 'default' worker.

        workers.json in this harness seeds w1..w10 only and none of them is
        registered, so the call reaches the join handler under the resolved
        name and fails there with the entry check's message --
        ``Worker 'default' is not running``.  What it must never do again is
        fail the old ``worker_name is required`` guard, which contradicted the
        schema's optional ``worker_name``.
        """
        tool = Worker(
            action="join",
            session_id=SESSION,
            workspace_path="/tmp/test_ws",
            worker_query="j1",
        )
        result = _parse_result(tool.execute())
        self.assertEqual(tool.worker_name, "default")
        self.assertEqual(result.get("error"), "Worker 'default' is not running")
        self.assertNotIn("worker_name is required", result.get("error") or "")


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

    def _run_one_agent_turn(self, session_id: str, worker_mode: bool = False) -> list:
        """Drive one REAL Agent turn (puppet LLM, no network).

        ``session_id`` is the same string the Worker tool receives from the
        ToolExecutor, so this exercises the real discard path against the real
        module-level ``_SESSION_MAIN_PAUSED`` set.
        """
        from agent.config.models import AgentConfig
        from agent.core.agent import Agent
        from tests.mocks.puppet_agent import PuppetLLM

        agent = Agent(
            config=AgentConfig(
                api_key="test-key",
                enable_logging=False,
                worker_mode=worker_mode,
            ),
            session_id=session_id,
        )
        puppet = PuppetLLM(scenario=[{"type": "assistant", "content": "ok"}])
        agent.llm_client.stream_completion = puppet.stream_completion
        return list(agent.process_query("continue"))

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

    def test_resume_action_is_operator_only(self):
        """``resume`` is no longer an agent action: it is refused and NOTHING
        is resumed (the main-pause and the paused worker stay as they were)."""
        f1 = self._seed_join_fake(SESSION, "w1", 1)
        self._pause(worker_query="all main")  # main + w1 paused
        self.assertIn(SESSION, self._worker_mod._SESSION_MAIN_PAUSED)
        f1.status = "paused"
        f1._manual_only_pause = True

        result = self._act("resume", name="w1", worker_query="all main")

        self.assertIn("operator-only", result.get("error", ""))
        self.assertNotIn("resume", result.get("available_actions", []))
        self.assertIn(SESSION, self._worker_mod._SESSION_MAIN_PAUSED)
        f1.resume.assert_not_called()
        self.assertEqual(f1.status, "paused")
        self.assertTrue(f1._manual_only_pause)

    # ── Stranded main-pause marker (cleared at the next operator turn) ────

    def test_next_operator_turn_clears_stranded_main_pause(self):
        """A new main-agent turn clears the session marker; the tool cannot.

        Regression: dropping the agent-facing ``resume`` action removed the
        only in-agent discard site for ``_SESSION_MAIN_PAUSED``, so a
        main-scope ``pause`` left the session reporting ``paused`` to
        join/wait_for_job forever.  The prescribed discard event is the start
        of the next genuine operator turn (``Agent.process_query``).
        """
        self._seed_join_fake(SESSION, "w1", 1)
        self._pause(worker_query="main")
        self.assertIn(SESSION, self._worker_mod._SESSION_MAIN_PAUSED)

        # Negative half: the Worker tool has no way to clear it — no
        # ``resume`` action, and an explicit resume request is refused without
        # touching the real set.
        self.assertNotIn("resume", Worker.VALID_ACTIONS)
        refused = self._act("resume", name="w1", worker_query="all main")
        self.assertIn("operator-only", refused.get("error", ""))
        self.assertNotIn("resume", refused.get("available_actions", []))
        self.assertIn(SESSION, self._worker_mod._SESSION_MAIN_PAUSED)

        events = self._run_one_agent_turn(SESSION)

        self.assertTrue(events, "sanity: the agent turn really ran")
        self.assertNotIn(SESSION, self._worker_mod._SESSION_MAIN_PAUSED)

    def test_worker_turn_does_not_clear_main_pause(self):
        """Worker turns share the parent session id but are not operator turns."""
        self._seed_join_fake(SESSION, "w1", 1)
        self._pause(worker_query="main")
        self.assertIn(SESSION, self._worker_mod._SESSION_MAIN_PAUSED)

        events = self._run_one_agent_turn(SESSION, worker_mode=True)

        self.assertTrue(events, "sanity: the worker turn really ran")
        self.assertIn(SESSION, self._worker_mod._SESSION_MAIN_PAUSED)

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
        for name in ["w1", "w2"]:
            self.assertTrue(self._spawn(name).get("spawned"), name)
        self._seed_join_fake(SESSION, "w1", 2, status="paused")  # live → 3 total

        result = self._spawn("w3")

        self.assertIn("error", result)
        self.assertIn("limit", result["error"].lower())
        self.assertEqual(result.get("max_workers"), 3)
        self.assertEqual(result.get("live_workers"), 3)

    # ── exact-owner container cleanup ────────────────────────────────────────────────

    def test_cleanup_worker_containers_exact_owner(self):
        """Cleanup touches only containers labeled with this worker's identity."""
        owned = {
            "container_id": "c1",
            "name": "w1box",
            "labels": {
                "thoughtmachine.worker": "sess-op-semantics:w1",
                RECORD_LABEL_KEY: "rec-owned-c1",
            },
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
            name="w1obj",
            labels={
                "thoughtmachine.worker": "sess-op-semantics:w1",
                RECORD_LABEL_KEY: "rec-owned-obj",
            },
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

        # Fail-closed teardown reclaims a container only when its canonical
        # record resolves to the ephemeral class. Patch the seam the green
        # teardown tests use so the OWNED fakes (c1 dict + owned_obj) reclaim.
        _finder = mock.patch.object(
            worker_container,
            "find_by_docker_label",
            lambda record_id, vault_root=None: SimpleNamespace(
                lifecycle_class=LIFECYCLE_EPHEMERAL
            ),
        )
        _finder.start()
        self.addCleanup(_finder.stop)

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


# ══════════════════════════════════════════════════════════════════════
# Unit D — pause/resume non-destructiveness + job terminalization (E5)
#══════════════════════════════════════════════════════════════════════

import pytest

from tests.test_worker_sync_query_timeout_containment import (
    _FakeContainerManager,
    _FakeContext,
    _owned_container,
    _spawn_worker,
    _wait_until,
)

from tools.workspace import worker as worker_module
import tools.workspace.worker_thread as worker_thread_module


@pytest.fixture(autouse=True)
def _registry_teardown():
    """run() does not unregister threads — pop added registry keys and
    stop/join any stragglers so nothing leaks into later tests."""
    with worker_module._registry_lock:
        preexisting = set(worker_module._worker_registry.keys())
    yield
    with worker_module._registry_lock:
        added = [k for k in worker_module._worker_registry.keys() if k not in preexisting]
        threads = [worker_module._worker_registry.pop(k, None) for k in added]
    for t in threads:
        if t is None:
            continue
        try:
            t.stop()
        except Exception:
            pass
        t.join(timeout=5)


class TestOperatorOnlyActions(_OpSemBase):
    """The agent must NOT be able to terminate (kill) or revive (resume).

    Operator ruling: both are operator-only (workspace UI / the workspace HTTP
    routes).  The agent keeps ``stop``, which DETACHES — a recoverable park,
    not a termination.
    """

    def test_kill_and_resume_are_not_agent_actions(self):
        """The advertised/valid action set excludes the operator-only pair."""
        self.assertNotIn("kill", Worker.VALID_ACTIONS)
        self.assertNotIn("resume", Worker.VALID_ACTIONS)
        # stop (detach) is the agent's option and must stay available.
        self.assertIn("stop", Worker.VALID_ACTIONS)

    def test_action_kill_errors_and_terminates_nothing(self):
        """A kill request returns a teaching error and leaves the worker live."""
        fake = self._seed_fake(SESSION, "w1", 1)

        result = self._act("kill", name="w1")

        self.assertIn("operator-only", result.get("error", ""))
        self.assertIn("kill", result.get("error", ""))
        self.assertIn("stop", result.get("error", ""))
        self.assertEqual(result.get("available_actions"), Worker.VALID_ACTIONS)
        # Nothing was terminated and nothing was detached.
        fake.kill.assert_not_called()
        fake.stop.assert_not_called()
        self.assertEqual(fake.status, "ready")
        self.assertIn((SESSION, "w1", 1), self._registry._worker_registry)

    def test_action_kill_on_detached_worker_keeps_it_detached(self):
        """A kill request cannot reach a parked worker either."""
        fake = self._seed_fake(SESSION, "w1", 1, status="detached")

        result = self._act("kill", name="w1")

        self.assertIn("operator-only", result.get("error", ""))
        fake.kill.assert_not_called()
        self.assertEqual(fake.status, "detached")
        self.assertIn((SESSION, "w1", 1), self._registry._worker_registry)

    def test_action_stop_still_detaches(self):
        """stop remains available and DETACHES: registration and thread live on."""
        fake = self._seed_fake(SESSION, "w1", 1)

        result = self._act("stop", name="w1")

        self.assertEqual(result.get("status"), "detached")
        fake.stop.assert_called_once()
        fake.kill.assert_not_called()
        self.assertEqual(fake.status, "detached")
        self.assertIn((SESSION, "w1", 1), self._registry._worker_registry)


def _fake_tool_loop(self, query):
    """Scripted _run_tool_loop: sets the telemetry attribute run() reads
    directly (worker.py L2485) and returns a JSON reply."""
    self._last_elapsed_val = 0.0
    return json.dumps({"response": "ok", "query": query})


def _capture_events(thread):
    events = []
    orig = thread._publish_event

    def capture(etype, data):
        events.append((etype, dict(data)))
        orig(etype, data)

    thread._publish_event = capture
    return events


def test_pause_idle_worker_is_non_destructive(tmp_path, monkeypatch):
    """Idle pause/resume must not stop/remove containers and must not kill
    the worker; the worker must still answer queries afterwards."""
    cm = _FakeContainerManager([_owned_container("c-owned", "w1-ctr", "sess-p:w1")])
    thread = _spawn_worker(tmp_path, monkeypatch, "w1", "sess-p", container_manager=cm)
    thread._save_context = lambda: None  # instance shadow survives monkeypatch teardown
    events = _capture_events(thread)

    thread.pause()
    assert _wait_until(lambda: thread.status == "paused" and thread.is_alive()), (
        f"worker did not reach paused (status={thread.status})"
    )
    # pause() flips status to 'paused' synchronously; wait for the worker
    # thread to actually publish worker_paused (race-free signal).
    assert _wait_until(lambda: any(e == "worker_paused" for e, _ in events)), (
        f"no worker_paused event: {events}"
    )
    paused = [d for e, d in events if e == "worker_paused"]
    assert paused, f"no worker_paused event: {events}"
    assert paused[-1]["status"] == "paused"
    assert paused[-1]["worker_name"] == "w1"
    assert paused[-1]["session_id"] == "sess-p"
    assert "current_context_tokens" in paused[-1]
    assert "max_context_tokens" in paused[-1]
    # Non-destructive: no container was stopped or removed.
    assert cm.stopped == [] and cm.removed == []

    thread.resume()
    # resume() flips status to 'ready' synchronously; wait for the worker
    # thread to actually publish worker_resumed (race-free signal).
    assert _wait_until(lambda: any(e == "worker_resumed" for e, _ in events)), (
        f"no worker_resumed event: {events}"
    )
    resumed = [d for e, d in events if e == "worker_resumed"]
    assert resumed[-1]["status"] == "ready"
    assert _wait_until(lambda: thread.status == "ready" and thread.is_alive()), (
        f"worker did not resume (status={thread.status})"
    )

    # Still fully functional: run a real query through the queue path.
    monkeypatch.setattr(WorkerThread, "_run_tool_loop", _fake_tool_loop)
    thread._agent = object()
    reply_q = queue.Queue()
    thread._input_queue.put(("q1", "hello", reply_q))
    envelope = json.loads(reply_q.get(timeout=10))
    assert envelope["query_id"] == "q1"
    assert json.loads(envelope["content"])["response"] == "ok"
    assert cm.stopped == [] and cm.removed == []


def test_pause_all_during_async_job_terminalizes_job(tmp_path, monkeypatch):
    """Pause All while a job is running must terminalize the job as 'paused'
    in the job registry, and that terminal state must never be overwritten
    by a later completion of the same job id."""
    reg = WorkerJobRegistry(event_bus=None)
    monkeypatch.setattr(worker_module, "_get_worker_job_registry", lambda: reg)
    thread = _spawn_worker(tmp_path, monkeypatch, "w1", "sess-2")
    thread._save_context = lambda: None  # instance shadow survives monkeypatch teardown

    busy_started = threading.Event()
    release = threading.Event()

    def blocking_tool_loop(self, query):
        busy_started.set()
        assert release.wait(timeout=15)
        self._last_elapsed_val = 0.0
        return json.dumps({"response": "done", "query": query})

    monkeypatch.setattr(WorkerThread, "_run_tool_loop", blocking_tool_loop)
    thread._agent = object()

    job_id = "job-pause-1"
    reg.register(job_id, "w1", session_id="sess-2")
    reply_q = queue.Queue()
    thread._input_queue.put((job_id, "task", reply_q))
    assert busy_started.wait(timeout=5.0), "worker never became busy"
    assert thread.status == "busy"

    thread.pause()  # Pause All while the job is mid-flight
    release.set()
    assert _wait_until(lambda: thread.status == "paused"), thread.status
    assert _wait_until(lambda: reg.job(job_id)["status"] == "paused")
    assert reg.job(job_id)["status"] == "paused"

    thread.resume()
    assert _wait_until(lambda: thread.status == "ready"), thread.status
    assert _wait_until(lambda: reg.job(job_id)["status"] == "paused")
    assert reg.job(job_id)["status"] == "paused"  # terminal, unchanged

    # A later completion of the SAME job id must not clobber the terminal state.
    reply_q2 = queue.Queue()
    thread._input_queue.put((job_id, "again", reply_q2))
    envelope2 = json.loads(reply_q2.get(timeout=10))
    assert json.loads(envelope2["content"])["response"] == "done"
    assert _wait_until(lambda: reg.job(job_id)["status"] == "paused")
    assert reg.job(job_id)["status"] == "paused"


def test_sync_query_to_paused_worker_auto_resumes(tmp_path, monkeypatch):
    """A synchronous query to a paused worker must auto-resume it (non-manual
    pause only) and still deliver the reply."""
    thread = _spawn_worker(tmp_path, monkeypatch, "w1", "sess-3")
    thread._save_context = lambda: None  # instance shadow survives monkeypatch teardown
    thread.pause()
    assert _wait_until(lambda: thread.status == "paused"), thread.status
    events = _capture_events(thread)

    monkeypatch.setattr(WorkerThread, "_run_tool_loop", _fake_tool_loop)
    thread._agent = object()

    tool = Worker(action="query", worker_name="w1", worker_query="hello", session_id="sess-3")
    t0 = time.monotonic()
    envelope = tool._action_query([])
    elapsed = time.monotonic() - t0
    assert elapsed < 10.0, f"auto-resume took {elapsed:.1f}s"
    assert envelope["worker_name"] == "w1"
    payload = json.loads(envelope["response"])
    assert payload["query_id"]
    assert json.loads(payload["content"])["response"] == "ok"
    assert thread.status == "ready" and thread.is_alive()
    resumed = [d for e, d in events if e == "worker_resumed"]
    assert resumed and resumed[-1]["status"] == "ready", f"no worker_resumed: {events}"


def test_sync_query_to_manual_only_paused_worker_is_non_destructive(tmp_path, monkeypatch):
    """A synchronous query to a MANUALLY paused worker must short-circuit with
    a 'paused' result and leave the worker paused and alive (no auto-resume,
    no spawn, no stop)."""
    thread = _spawn_worker(tmp_path, monkeypatch, "w1", "sess-4")
    thread._save_context = lambda: None  # instance shadow survives monkeypatch teardown
    thread._manual_only_pause = True
    thread.pause()
    assert _wait_until(lambda: thread.status == "paused"), thread.status

    tool = Worker(action="query", worker_name="w1", worker_query="hello", session_id="sess-4")
    t0 = time.monotonic()
    envelope = tool._action_query([])
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"manual-pause short-circuit took {elapsed:.1f}s"
    assert envelope == {
        "status": "paused",
        "message": "Worker is manually paused",
        "worker_name": "w1",
        "session_id": "sess-4",
    }
    assert thread.status == "paused" and thread.is_alive()


# ══════════════════════════════════════════════════════════════════════════
# Cross-process command channel: ``command.json`` -> ``_poll_command``
#
# The Web UI never touches the thread object for a cross-process stop/kill:
# ``web_ui/backend/workspace_routes.py:1197-1200`` (``_stop_worker_instance``)
# writes ``{"action": "kill" if terminate else "stop"}`` into
# ``<workspace>/workers/<label>/command.json`` and the worker thread consumes
# it in ``WorkerThread._poll_command`` (tools/workspace/worker_thread.py:1846,
# called from the run loop at :2130 and :2635).  These tests drive that exact
# file through that exact method against REAL threads and assert on real
# thread/registry/job-registry state -- never on mock call logs.
# ══════════════════════════════════════════════════════════════════════════

from infra.container_manager import is_worker_detached, unregister_detached_worker


def _spawn_ready_persisting_context(tmp_path, monkeypatch, name, session_id):
    """Spawn a REAL WorkerThread that KEEPS ``_save_context`` live.

    ``_spawn_worker`` neuters ``_save_context`` (``lambda self: None``), which
    would make every "the detach persisted the context" assertion vacuous.
    Only ``_load_context`` (no on-disk context is needed) and
    ``_heartbeat_tick`` are patched here.
    """
    monkeypatch.setattr(WorkerThread, "_load_context", lambda self: _FakeContext())
    monkeypatch.setattr(WorkerThread, "_heartbeat_tick", lambda self: None)
    thread = WorkerThread(
        name=name,
        definition={"name": name, "system_prompt": "You are a test worker."},
        agent_config={},
        workspace_dir=tmp_path,
        session_id=session_id,
        container_manager=None,
        max_container_count=4,
        max_token_usage=None,
        max_runtime_s=None,
    )
    with worker_module._registry_lock:
        worker_module._worker_registry[(session_id, name, 1)] = thread
    thread.start()
    # ``status`` is already "ready" from __init__ (worker_thread.py:1051), so a
    # status-based wait returns before run() has done anything.  Wait for the
    # STARTUP context save instead (run() calls _save_context() at :2631): it
    # is the first observable effect of run() that the tests rely on.
    ctx_file = thread._context_path()
    assert _wait_until(ctx_file.is_file), (
        f"worker {name!r} never persisted {ctx_file} "
        f"(status={thread.status}, error={thread.error}, "
        f"alive={thread.is_alive()})"
    )
    return thread


def _write_command_file(thread, action):
    """Write a FENCED ``command.json`` through the production writer.

    This mirrors the producer side, ``command_channel.write_command``: the
    payload carries ``thread._fence()``, because an UNFENCED command is now
    refused and quarantined by the consumer (see the CommandFence section
    at the bottom of this module).  A bare ``{"action": ...}`` written here
    would only exercise a wire format the consumer no longer accepts.
    """
    thread._worker_dir.mkdir(parents=True, exist_ok=True)
    assert write_command(thread._worker_dir, action, thread._fence()) is True
    return thread._worker_dir / COMMAND_FILENAME


def _persisted_runtime_status(thread):
    """``status.json``'s ``runtime_status`` -- the CROSS-PROCESS view.

    Returns ``None`` when the file is absent or unreadable: while a detach is
    in flight the file legitimately holds the pre-flip value (or no value at
    all), exactly as the REST backend tolerates, so callers POLL this rather
    than sample it once.  The in-memory ``thread.status`` and this file are
    written in two separate steps, so a test that waits on the attribute
    alone can still read the stale persisted value.
    """
    try:
        status_path = thread._worker_dir / "status.json"
        return json.loads(status_path.read_text(encoding="utf-8"))["runtime_status"]
    except (OSError, ValueError, KeyError):
        return None


def _retire_thread(thread, timeout=10.0):
    """Kill + join so a PARKED (detached) thread never leaks into the next test
    (``_registry_teardown`` only calls ``stop()``, which detaches)."""
    try:
        unregister_detached_worker(thread.owner_identity)
    except Exception:
        pass
    try:
        thread.kill()
    except Exception:
        pass
    thread.join(timeout=timeout)


def test_command_file_stop_detaches_and_keeps_the_registration(tmp_path, monkeypatch):
    """A cross-process ``{"action": "stop"}`` DETACHES.

    PHASE 2.1a contract (worker_thread.py:1853-1873): the parked worker keeps
    its thread, its registry entry and its container-manager detach
    registration, persists its context, and stays revivable.
    """
    thread = _spawn_ready_persisting_context(tmp_path, monkeypatch, "w1", "sess-cmd-stop")
    ctx_file = thread._worker_dir / "context.json"
    assert ctx_file.is_file(), "run() must persist context.json on startup"
    # Drop the startup context so the file reappearing below can only be the
    # STOP branch's own ``_save_context()`` (worker_thread.py:1870-1871).
    ctx_file.unlink()
    cmd = _write_command_file(thread, "stop")

    thread._poll_command()

    assert not cmd.exists(), "the consumed command file must be deleted"
    assert thread.status == "detached"
    assert thread.is_alive(), "stop must NOT terminate the thread"
    assert not thread._stop_event.is_set(), "stop must never set the stop event"
    assert thread._detach_requested is True
    assert thread._detached_at is not None
    with worker_module._registry_lock:
        assert worker_module._worker_registry[("sess-cmd-stop", "w1", 1)] is thread
    assert is_worker_detached(thread.owner_identity) is True
    # The stop branch persisted the live context (same instance/generation,
    # real payload), so the worker is revivable from disk.  The POST-flip
    # runtime status is carried by status.json: _poll_command saves the context
    # BEFORE flipping ``self.status`` to "detached", so context.json still holds
    # the pre-flip status (worker_thread.py:1870-1875).
    assert ctx_file.is_file(), "the stop branch must persist context.json"
    persisted = json.loads(ctx_file.read_text(encoding="utf-8"))
    assert persisted["generation"] == thread._generation
    assert persisted["user_history"], "the persisted context must carry the conversation"
    status_doc = json.loads(
        (thread._worker_dir / "status.json").read_text(encoding="utf-8")
    )
    assert status_doc["runtime_status"] == "detached"
    # A detached worker is revivable -- that is the whole point of a detach.
    assert thread.resume() is True
    assert thread.status == "ready"
    _retire_thread(thread)


def test_command_file_kill_terminates_and_rejects_queued_work(tmp_path, monkeypatch):
    """A cross-process ``{"action": "kill"}`` TERMINATES.

    PHASE 2.1a/2.1b contract (worker_thread.py:1874-1883 -> ``kill()``): the
    thread exits, the detach markers are cleared, the job is NOT left pending
    (``reject_queued_queries`` terminalises it as ``error``) and the worker is
    not revivable.
    """
    reg = WorkerJobRegistry(event_bus=None)
    monkeypatch.setattr(worker_module, "_get_worker_job_registry", lambda: reg)
    thread = _spawn_ready_persisting_context(tmp_path, monkeypatch, "w1", "sess-cmd-kill")
    # Park FIRST so the queued query cannot start executing (a parked worker
    # never drains its queue -- PHASE 2.1b); status == "paused" proves the run
    # loop reached its park branch and is no longer reading the input queue.
    thread.pause()
    assert _wait_until(lambda: thread.status == "paused"), thread.status
    job_id = "job-cmd-kill-1"
    reg.register(job_id, "w1", session_id="sess-cmd-kill")
    thread.enqueue_query(job_id, "queued work")
    assert thread.pending_query_jobs() == [job_id]
    cmd = _write_command_file(thread, "kill")

    thread._poll_command()

    assert not cmd.exists()
    assert thread.status == "killed"
    assert thread._stop_event.is_set()
    assert thread._detach_requested is False
    assert thread._detached_at is None
    assert _wait_until(lambda: not thread.is_alive(), timeout=10.0), (
        "a killed worker must terminate"
    )
    assert thread.pending_query_jobs() == [], "queued work must be rejected, not left pending"
    record = reg.job(job_id)
    assert record["status"] == "error"
    assert "queued query discarded" in record["result"]["error"]
    assert is_worker_detached(thread.owner_identity) is False
    # A killed worker is NOT revivable (resume() refuses without touching state).
    assert thread.resume() is False
    assert thread.status == "killed"


def _drive_command(tmp_path, monkeypatch, name, session_id, action):
    """Bring one worker to an IDENTICAL starting state and apply ``action``.

    Starting state: ready -> parked, with exactly one query queued and not yet
    started.  Returns everything the two actions are supposed to diverge on.
    """
    reg = WorkerJobRegistry(event_bus=None)
    monkeypatch.setattr(worker_module, "_get_worker_job_registry", lambda: reg)
    thread = _spawn_ready_persisting_context(tmp_path, monkeypatch, name, session_id)
    thread.pause()
    assert _wait_until(lambda: thread.status == "paused"), thread.status
    job_id = f"job-{action}"
    reg.register(job_id, name, session_id=session_id)
    thread.enqueue_query(job_id, "queued work")
    started = {
        "status": "paused",
        "alive": thread.is_alive(),
        "queued": thread.pending_query_jobs() == [job_id],
        "job_status": reg.job(job_id)["status"],
        "stop_event": thread._stop_event.is_set(),
    }
    cmd = _write_command_file(thread, action)
    thread._poll_command()
    assert not cmd.exists()
    # Sampled HERE: resume() legitimately lifts the detach registration
    # (worker_thread.py:1645-1650), so post-resume reads cannot see it.
    registered = is_worker_detached(thread.owner_identity)
    return thread, reg, job_id, started, registered


def test_command_stop_and_kill_diverge_from_the_same_starting_state(tmp_path, monkeypatch):
    """``stop`` and ``kill`` are driven through the SAME channel from the SAME
    starting state and must land on opposite outcomes.

    Only the session label differs between the two runs, so the process-global
    detached-identity set (``infra.container_manager._DETACHED_WORKER_IDENTITIES``,
    keyed by ``<session>:<name>``) cannot alias across them.
    """
    stop_thread, stop_reg, stop_job, stop_start, stop_registered = _drive_command(
        tmp_path / "stop", monkeypatch, "w1", "sess-cmd-div-stop", "stop"
    )
    kill_thread, kill_reg, kill_job, kill_start, kill_registered = _drive_command(
        tmp_path / "kill", monkeypatch, "w1", "sess-cmd-div-kill", "kill"
    )

    # ── identical starting state ──
    assert stop_start == kill_start == {
        "status": "paused",
        "alive": True,
        "queued": True,
        "job_status": "submitted",
        "stop_event": False,
    }

    # ── divergent outcomes ──
    assert (stop_thread.status, kill_thread.status) == ("detached", "killed")
    assert stop_thread._stop_event.is_set() is False
    assert kill_thread._stop_event.is_set() is True
    assert _wait_until(lambda: not kill_thread.is_alive(), timeout=10.0)
    assert stop_thread.is_alive() is True
    # Registration: the parked worker keeps its detach registration, the
    # killed one clears it.
    assert (stop_registered, kill_registered) == (True, False)

    # Snapshot the parked worker's queue and job record BEFORE resuming it:
    # resume() un-parks the run loop, which by design re-drains the FIFO and
    # may start the queued job, so reading them afterwards races the worker
    # (a post-resume empty pending set is legitimate, not a defect).
    stop_pending_before_resume = stop_thread.pending_query_jobs()
    stop_job_status_before_resume = stop_reg.job(stop_job)["status"]

    # Revivability: the parked worker comes back, the killed one cannot.
    assert stop_thread.resume() is True
    assert kill_thread.resume() is False

    # The kill terminates the queued job; the detach keeps it queued for later.
    assert kill_thread.pending_query_jobs() == []
    assert kill_reg.job(kill_job)["status"] == "error"
    assert stop_pending_before_resume == [stop_job]
    assert stop_job_status_before_resume == "submitted"

    _retire_thread(stop_thread)


def test_stale_park_sentinel_after_resume_is_not_read_as_stop(tmp_path, monkeypatch):
    """A wake-up marker still queued when ``resume()`` clears the pause event
    must be a NO-OP, never a stop.

    Interleaving under test (deterministic -- no sleep-based racing): the worker
    is BUSY inside ``_run_tool_loop``, blocked on a test gate, so it cannot
    consume its input queue at all.  While it is blocked the test calls
    ``pause()``, which enqueues the park wake-up marker
    (``worker_thread.py:1611``) and sets ``_park_event``, and then ``resume()``,
    which clears ``_park_event`` (``worker_thread.py:1649``) BEFORE the loop
    ever looks at the queue.  When the in-flight query finishes and the loop
    drains the queue it therefore finds that marker with the pause flag already
    cleared -- the exact "leftover marker after resume" shape.

    Pre-fix that marker was a bare ``None``, i.e. the same value as the STOP
    sentinel, so the loop took the ``break`` at ``worker_thread.py:2760``: the
    thread exited, ``status`` reported ``completed``, and every query queued
    behind the marker was silently dropped.  Post-fix the marker is
    ``_PARK_SENTINEL`` and the stale branch at ``worker_thread.py:2753``
    discards it, so the loop keeps running and the next query executes.

    Assertions are on the REAL thread, the real job registry and the real reply
    queue -- never on a mock's call log.
    """
    reg = WorkerJobRegistry(event_bus=None)
    monkeypatch.setattr(worker_module, "_get_worker_job_registry", lambda: reg)
    thread = _spawn_worker(tmp_path, monkeypatch, "w1", "sess-park-sentinel")

    busy_started = threading.Event()
    release = threading.Event()

    def blocking_tool_loop(self, query):
        busy_started.set()
        assert release.wait(timeout=15)
        self._last_elapsed_val = 0.0
        return json.dumps({"response": "done", "query": query})

    monkeypatch.setattr(WorkerThread, "_run_tool_loop", blocking_tool_loop)
    thread._agent = object()

    # ── 1. the worker is genuinely busy: it cannot consume the queue ──────
    reply_q = queue.Queue()
    thread._input_queue.put(("job-inflight", "first", reply_q))
    assert busy_started.wait(timeout=5.0), "worker never became busy"
    assert thread.status == "busy"

    # ── 2. enqueue the marker, then clear the pause flag under it ─────────
    thread.pause()      # put(_PARK_SENTINEL), _park_event.set()   (:1611)
    thread.resume()     # _park_event.clear(), _resume_event.set() (:1649)
    assert thread._park_event.is_set() is False
    assert thread.status == "ready"

    # The in-flight query completes normally, and only THEN does the loop get
    # back to the queue and meet the now-stale marker.
    release.set()
    envelope_a = json.loads(reply_q.get(timeout=10))
    assert json.loads(envelope_a["content"])["response"] == "done"

    # ── 3. the stale marker must not have stopped the loop ────────────────
    # Bounded wait: pre-fix the run loop exits here, post-fix it does not.
    died = _wait_until(lambda: not thread.is_alive(), timeout=2.0)
    assert died is False, (
        "the stale park marker killed the run loop "
        f"(alive={thread.is_alive()}, status={thread.status!r}, "
        f"error={thread.error!r})"
    )
    assert thread.status != "completed", (
        f"the stale park marker reported the worker as completed: {thread.status!r}"
    )
    assert thread._stop_event.is_set() is False

    # ── 4. real work queued AFTER the resume still runs ───────────────────
    reply_q2 = queue.Queue()
    thread._input_queue.put(("job-after", "second", reply_q2))
    try:
        envelope_b = json.loads(reply_q2.get(timeout=10))
    except queue.Empty:
        raise AssertionError(
            "the query enqueued after resume was never answered "
            f"(alive={thread.is_alive()}, status={thread.status!r})"
        )
    assert json.loads(envelope_b["content"])["response"] == "done"
    assert json.loads(envelope_b["content"])["query"] == "second"

    _retire_thread(thread)


# A detached worker is only reaped once it has been detached for longer than
# WORKER_TIMEOUT_SECONDS (tools/workspace/worker_manager.py:179-195), so every
# latency asserted below must be orders of magnitude smaller than this.


def test_command_file_kill_is_seen_while_parked(tmp_path, monkeypatch):
    """A cross-process ``{"action": "kill"}`` must reach a PARKED worker.

    ``kill`` is the only TERMINATING action (PHASE 2.1a), but both park loops
    (worker_thread.py:2698 and :2961) waited on ``_resume_event`` alone: nothing
    polled ``command.json`` while parked, so a kill written by another process
    (web_ui/backend/workspace_routes.py:1197-1200) was never seen -- the worker
    stayed parked until the detached TTL (``WORKER_TIMEOUT_SECONDS`` = 600 s)
    killed it, and even then only through the sweep.  The run loops now poll
    ``_poll_command()`` on each 1 s park wake-up.

    Assertions are on the REAL thread, its real status files and the container
    manager's detached registry -- never on a mock's call log.
    """
    thread = _spawn_ready_persisting_context(tmp_path, monkeypatch, "w1", "sess-park-kill")
    thread.pause()
    assert _wait_until(lambda: thread.status == "paused"), thread.status
    assert thread._park_event.is_set(), "the worker must be genuinely parked"
    # From here on only run()'s ``finally`` (worker_thread.py:3242-3243) saves
    # the context -- kill() does not -- so a REAPPEARING context.json proves
    # the finally block ran after the park loop broke out.
    ctx_file = thread._worker_dir / "context.json"
    ctx_file.unlink()

    cmd = _write_command_file(thread, "kill")
    started = time.monotonic()
    died = _wait_until(lambda: not thread.is_alive(), timeout=10.0)
    elapsed = time.monotonic() - started
    assert died, (
        "a command.json 'kill' was NOT seen while the worker was parked "
        f"(alive={thread.is_alive()}, status={thread.status!r}, "
        f"detach_requested={thread._detach_requested!r}, "
        f"command_file_still_present={cmd.exists()}) -- the park loop is deaf"
    )
    assert elapsed < 10.0, f"kill latency while parked was {elapsed:.1f}s"
    assert elapsed < _DETACHED_TTL_SECONDS / 10.0, (
        f"kill latency while parked ({elapsed:.1f}s) is not far below the "
        f"{_DETACHED_TTL_SECONDS}s detached TTL"
    )

    # The kill path won: the park loop broke out with the stop event set, so
    # the loop's exit branch reported 'killed' (worker_thread.py:3197) and NOT
    # the resume transition (:2703-2706), which would have written 'ready'.
    assert thread.status == "killed"
    assert thread._stop_event.is_set()
    assert not cmd.exists(), "the consumed command file must be deleted"
    assert thread._detach_requested is False
    assert thread._detached_at is None
    assert is_worker_detached(thread.owner_identity) is False
    status_doc = json.loads(
        (thread._worker_dir / "status.json").read_text(encoding="utf-8")
    )
    assert status_doc["runtime_status"] == "killed"
    assert ctx_file.is_file(), "run()'s finally must still persist the context"
    assert thread.resume() is False, "a killed worker is not revivable"
    _retire_thread(thread)


def test_command_file_detach_is_seen_while_parked(tmp_path, monkeypatch):
    """A cross-process ``{"action": "stop"}`` must DETACH a PARKED worker.

    Same deaf-park defect, opposite outcome: ``stop`` never sets the stop event
    (PHASE 2.1a -- it parks the worker), so the park loop keeps waiting and what
    the operator must observe instead is the DETACH transition itself (status
    'detached', the detach markers, the container-manager registration).  It
    must land within a park wake-up, not after the detached TTL.
    """
    thread = _spawn_ready_persisting_context(tmp_path, monkeypatch, "w1", "sess-park-stop")
    thread.pause()
    assert _wait_until(lambda: thread.status == "paused"), thread.status
    assert thread._park_event.is_set(), "the worker must be genuinely parked"
    assert thread._detach_requested is False

    cmd = _write_command_file(thread, "stop")
    started = time.monotonic()
    # DETERMINISTIC WAIT -- was ``thread.status == "detached"`` only.
    #
    # A detach is THREE separate steps on the worker thread:
    #   (1) the container-manager registration (:2522, FIRST by the documented
    #       ordering invariant),
    #   (2) the in-memory ``self.status = "detached"`` (:2523), and
    #   (3) the persisted ``status.json`` write (:2524).
    # Waiting on (2) alone let this test run ahead and read a ``status.json``
    # that still carried the pre-flip "paused", so the assertion further down
    # failed with a bare ``assert 'paused' == 'detached'`` -- an outcome that
    # depended purely on how the GIL interleaved those two writes, which is why
    # it surfaced as a version-dependent race.  Wait for the in-memory state
    # AND the cross-process view (the same poll-the-file idiom the resume test
    # below uses), so the transition is observed only when BOTH have landed.
    # No timeout inflation: still bounded by the same 10 s, and a status write
    # that genuinely never reaches the file now fails HERE, naming the value
    # that was persisted, instead of as an opaque ``==`` mismatch.
    detached = _wait_until(
        lambda: thread.status == "detached"
        and _persisted_runtime_status(thread) == "detached",
        timeout=10.0,
    )
    elapsed = time.monotonic() - started
    assert detached, (
        "a command.json 'stop' was NOT seen while the worker was parked "
        f"(alive={thread.is_alive()}, status={thread.status!r}, "
        f"persisted_status={_persisted_runtime_status(thread)!r}, "
        f"detach_requested={thread._detach_requested!r}, "
        f"command_file_still_present={cmd.exists()}) -- the park loop is deaf "
        "or the detach status write never reached status.json"
    )
    assert elapsed < 10.0, f"detach latency while parked was {elapsed:.1f}s"
    assert elapsed < _DETACHED_TTL_SECONDS / 10.0, (
        f"detach latency while parked ({elapsed:.1f}s) is not far below the "
        f"{_DETACHED_TTL_SECONDS}s detached TTL"
    )

    assert not cmd.exists(), "the consumed command file must be deleted"
    assert thread.is_alive(), "stop must NOT terminate the thread"
    assert thread._stop_event.is_set() is False, "stop must never set the stop event"
    assert thread._park_event.is_set(), "a detached worker keeps parking"
    assert thread._detach_requested is True
    assert thread._detached_at is not None
    assert _wait_until(lambda: is_worker_detached(thread.owner_identity) is True, timeout=10.0), thread.owner_identity
    status_doc = json.loads(
        (thread._worker_dir / "status.json").read_text(encoding="utf-8")
    )
    assert status_doc["runtime_status"] == "detached"
    # A detached worker is revivable -- that is the whole point of a detach --
    # and resume() lifts the registration again (worker_thread.py:1657-1661).
    assert thread.resume() is True
    assert thread.status == "ready"
    _retire_thread(thread)


# ════════════════════════════════════════════════════════════════════════════
# Detach ordering, cross-process resume, unknown command actions
# ════════════════════════════════════════════════════════════════════════════


def _unstarted_thread(tmp_path, name, session_id):
    """A REAL ``WorkerThread`` that is never ``start()``ed.

    The three tests below drive ONE lifecycle transition (or one
    ``_poll_command``) synchronously, so a live ``run()`` loop would only add
    noise: it writes status.json and polls command.json on its own cadence.
    Everything else is the production object.
    """
    return WorkerThread(
        name=name,
        definition={"name": name, "system_prompt": "You are a test worker."},
        agent_config={},
        workspace_dir=tmp_path,
        session_id=session_id,
        container_manager=None,
        max_container_count=4,
        max_token_usage=None,
        max_runtime_s=None,
    )


def _record_lifecycle_order(monkeypatch):
    """Order-instrument every status write and detached-registration call."""
    events = []
    real_write_status = WorkerThread._write_status_file
    real_set_registration = WorkerThread._set_detached_registration

    def spy_write_status(thread_self):
        events.append(("status", thread_self.status))
        return real_write_status(thread_self)

    def spy_set_registration(thread_self, detached):
        events.append(("registration", detached))
        return real_set_registration(thread_self, detached)

    monkeypatch.setattr(WorkerThread, "_write_status_file", spy_write_status)
    monkeypatch.setattr(WorkerThread, "_set_detached_registration", spy_set_registration)
    return events


def _assert_registered_before_detached(events, path):
    """The invariant: no reader may see status 'detached' unregistered."""
    first_detached = [i for i, ev in enumerate(events) if ev == ("status", "detached")]
    registrations = [i for i, ev in enumerate(events) if ev == ("registration", True)]
    assert first_detached, f"{path}: status 'detached' was never written: {events}"
    assert registrations, f"{path}: the worker was never registered as detached: {events}"
    assert registrations[0] < first_detached[0], (
        f"{path}: status 'detached' became observable at event #{first_detached[0]} "
        f"BEFORE the container manager was told the worker is detached "
        f"(event #{registrations[0]}).  In that window the stale-container sweep "
        f"still reaps the detached worker's containers.  Order was: {events}"
    )


def test_detached_status_is_never_observable_before_the_registration(
    tmp_path, monkeypatch
):
    """``status == "detached"`` may only be published once the registration is.

    Both writers of ``status = "detached"`` are driven synchronously here, so
    the assertion is a pure ordering check on a recorded event list -- no
    sleeps, no polling, no threads:

      * ``WorkerThread.detach()``            (worker_thread.py:1501-1504 before
        the fix: the status.json write landed TWO statements before the
        ``register_detached_worker`` call),
      * ``_poll_command``'s 'stop' branch    (worker_thread.py:1881-1883 before
        the fix, same order).

    A reader in between (``GET /api/workspace/{ws}/workers``, or
    ``PeriodicStaleCheck`` running in another process) sees a detached worker
    the sweep does not know to skip.
    """
    events = _record_lifecycle_order(monkeypatch)

    # 1) The operator/REST detach path.
    direct = _unstarted_thread(tmp_path, "w1", "sess-order-detach")
    direct.detach()
    assert direct.status == "detached"
    _assert_registered_before_detached(events, "WorkerThread.detach()")
    unregister_detached_worker(direct.owner_identity)

    # 2) The cross-process command-file 'stop' path.
    polled = _unstarted_thread(tmp_path, "w1", "sess-order-poll")
    _write_command_file(polled, "stop")
    events.clear()
    polled._poll_command()
    assert polled.status == "detached"
    _assert_registered_before_detached(events, "_poll_command() 'stop' branch")
    unregister_detached_worker(polled.owner_identity)


def test_command_file_resume_lifts_the_detached_registration(tmp_path, monkeypatch):
    """A cross-process ``{"action": "resume"}`` must fully revive a detached worker.

    ``_poll_command``'s 'resume' branch used to clear ``_park_event`` only, so
    the park loop exited and wrote ``status = "ready"`` (worker_thread.py:2719)
    while ``_detach_requested`` stayed True and the container manager still had
    the worker registered as detached -- a worker that advertises "ready" but
    whose containers the stale-container sweep keeps skipping.  It now mirrors
    ``WorkerThread.resume()`` (worker_thread.py:1657-1663).

    Assertions are on the REAL thread, its real status.json and the container
    manager's detached registry; only bounded waits are used.
    """
    thread = _spawn_ready_persisting_context(tmp_path, monkeypatch, "w1", "sess-cmd-resume")
    thread.pause()
    assert _wait_until(lambda: thread.status == "paused"), thread.status
    assert thread._park_event.is_set(), "the worker must be genuinely parked"

    detach_cmd = _write_command_file(thread, "stop")
    assert _wait_until(lambda: thread.status == "detached", timeout=10.0), (
        f"the 'stop' command never detached the worker (status={thread.status!r})"
    )
    assert _wait_until(lambda: is_worker_detached(thread.owner_identity) is True, timeout=10.0), (
        thread.owner_identity
    )
    assert thread._detach_requested is True
    # Only write the resume once the park loop has CONSUMED the stop file, so
    # this test cannot race the poller's read/unlink on the same path.
    assert _wait_until(lambda: not detach_cmd.exists(), timeout=10.0)

    resume_cmd = _write_command_file(thread, "resume")
    assert _wait_until(lambda: thread.status == "ready", timeout=10.0), (
        "a command.json 'resume' was NOT seen while the worker was parked "
        f"(alive={thread.is_alive()}, status={thread.status!r}, "
        f"detach_requested={thread._detach_requested!r}, "
        f"command_file_still_present={resume_cmd.exists()})"
    )
    assert not resume_cmd.exists(), "the consumed command file must be deleted"
    assert thread.is_alive(), "resume must not terminate the thread"
    assert thread._stop_event.is_set() is False
    assert thread._park_event.is_set() is False, "resume must unpark the worker"
    assert thread._detach_requested is False
    assert thread._detached_at is None
    assert _wait_until(
        lambda: is_worker_detached(thread.owner_identity) is False, timeout=10.0
    ), (
        "the worker reports 'ready' but the container manager still has it "
        f"registered as detached ({thread.owner_identity})"
    )
    # status.json is the cross-process view of the same state: poll it rather
    # than sampling it, exactly as the REST backend does.
    assert _wait_until(
        lambda: _persisted_runtime_status(thread) == "ready", timeout=10.0
    ), (
        "status.json still reports "
        f"{_persisted_runtime_status(thread)!r} after resume"
    )
    _retire_thread(thread)


def test_unknown_command_action_is_quarantined_without_eating_later_commands(tmp_path):
    """An unknown ACTION is refused, recorded, and does not mute the channel."""
    thread = _unstarted_thread(tmp_path, "w1", "sess-cmd-unknown")
    cmd = _write_command_file(thread, "frobnicate")
    for _ in range(3):
        thread._poll_command()
    assert thread.status == "ready"
    assert thread._stop_event.is_set() is False
    assert thread._park_event.is_set() is False
    assert thread._detach_requested is False
    assert not cmd.exists()
    quarantined = _rejected(thread)
    assert len(quarantined) == 1, quarantined
    assert json.loads(quarantined[0].read_text(encoding="utf-8"))["action"] == "frobnicate"
    _write_command_file(thread, "kill")
    thread._poll_command()
    assert thread._stop_event.is_set() is True, (
        "a legitimate 'kill' written after a quarantined 'frobnicate' was lost"
    )
    assert thread.status == "killed"


def _write_raw_command(thread, payload):
    """Write a payload directly, deliberately BYPASSING write_command."""
    path = thread._worker_dir / COMMAND_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _rejected(thread):
    return sorted(thread._worker_dir.glob(COMMAND_FILENAME + ".rejected-*"))


def _assert_quarantined(thread, cmd, action):
    assert not cmd.exists(), "a refused command must be claimed, not left in place"
    quarantined = _rejected(thread)
    assert len(quarantined) == 1, quarantined
    payload = json.loads(quarantined[0].read_text(encoding="utf-8"))
    assert payload["action"] == action, payload


def test_an_unfenced_kill_payload_is_ignored_and_quarantined(tmp_path):
    thread = _unstarted_thread(tmp_path, "w1", "sess-cmd-unfenced")
    cmd = _write_raw_command(thread, {"action": "kill"})
    thread._poll_command()
    assert thread._stop_event.is_set() is False, "an UNFENCED kill was obeyed"
    assert thread.status == "ready"
    _assert_quarantined(thread, cmd, "kill")


def test_a_foreign_instance_fence_is_ignored_and_quarantined(tmp_path):
    thread = _unstarted_thread(tmp_path, "w1", "sess-cmd-foreign")
    mine = thread._fence()
    foreign = dataclasses.replace(mine, instance_id=mine.instance_id + 1)
    cmd = _write_raw_command(thread, dict({"action": "stop"}, **foreign.to_payload()))
    thread._poll_command()
    assert thread._detach_requested is False
    assert thread._stop_event.is_set() is False
    assert thread.status == "ready"
    _assert_quarantined(thread, cmd, "stop")


@pytest.mark.parametrize("axis,other", [
    ("session_id", lambda f: f.session_id + "-someone-else"),
    ("worker_name", lambda f: f.worker_name + "-other"),
    ("instance_id", lambda f: f.instance_id + 1),
    ("epoch", lambda f: f.epoch + 1),
])
def test_each_mismatched_axis_is_rejected(tmp_path, axis, other):
    thread = _unstarted_thread(tmp_path, "w1", "sess-cmd-axis-" + axis)
    mine = thread._fence()
    wrong = dataclasses.replace(mine, **{axis: other(mine)})
    assert wrong != mine
    cmd = _write_raw_command(thread, dict({"action": "kill"}, **wrong.to_payload()))
    thread._poll_command()
    assert thread._stop_event.is_set() is False, "axis %r is not enforced" % (axis,)
    assert thread.status == "ready"
    _assert_quarantined(thread, cmd, "kill")


def test_a_stale_epoch_command_is_rejected(tmp_path):
    thread = _unstarted_thread(tmp_path, "w1", "sess-cmd-stale-epoch")
    mine = thread._fence()
    stale = dataclasses.replace(mine, epoch=mine.epoch - 1)
    cmd = _write_raw_command(thread, dict({"action": "stop"}, **stale.to_payload()))
    thread._poll_command()
    assert thread._detach_requested is False
    assert thread._stop_event.is_set() is False
    assert thread.status == "ready"
    _assert_quarantined(thread, cmd, "stop")


def test_a_correctly_fenced_command_is_still_obeyed(tmp_path):
    """POSITIVE CONTROL: a fence that rejects EVERYTHING would pass all the above."""
    thread = _unstarted_thread(tmp_path, "w1", "sess-cmd-fenced-ok")
    cmd = _write_command_file(thread, "kill")
    thread._poll_command()
    assert thread._stop_event.is_set() is True
    assert thread.status == "killed"
    assert not cmd.exists()
    assert _rejected(thread) == []


if __name__ == "__main__":
    unittest.main()

