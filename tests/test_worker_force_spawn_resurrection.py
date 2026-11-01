"""
Force-spawn must never resurrect a STOPPED worker (resurrection regression).

Field report: ``Worker(action="spawn", force=True)`` on a worker that had
already been **stopped** came back with the *stopped* worker's own envelope
(``{"status": "stopped", "message": "Worker stopped by user"}`` with a null
``instance_id``) instead of starting a fresh instance and running the query.
Three distinct defects line up behind that symptom:

  A. **Force is not session-scoped.**  ``_action_spawn``'s force pre-block and
     the registry lookup behind it are global: forcing ``default`` in session A
     stops (and unregisters) session B's ``default`` — a cross-session AND
     cross-workspace kill in one process.
  B. **Stopped threads get resumed.**  Both paused-auto-resume branches
     (explicit-``instance_id`` and name-only) resume the existing thread
     without checking ``force`` or the thread's stop state, so a thread whose
     ``_stop_event`` is already set is "resumed", re-runs, and immediately
     reports ``{"status": "stopped"}`` — the resurrection the report shows.
     ``WorkerThread.resume()`` itself never refuses a stopped thread either
     (it flips ``status`` back to ``"ready"`` and returns ``None``).
  C. **Born-dead stops are real stops (NOT defended).**  A ``WorkerThread``
     whose ``_stop_event`` is already set when ``run()`` starts exits before
     entering the main loop. This used to be "fixed" by clearing the event at
     birth; that is reverted — ``_stop_event`` is created in ``__init__`` and
     never inherited, so an already-set event is a genuine stop for this
     instance and clearing it would revive a worker somebody stopped. The
     behaviour is now observed (WARNING) and deliberately left as is; the
     caller sees a bounded failure, not a hang.

Cases (each maps to the fix it pins):

  1. force spawn over a STOPPED worker returns a fresh run, never the stopped
     envelope, and never calls ``resume()`` on the stopped thread (PRIMARY
     REPRO);
  2. force spawn over a RUNNING worker still stops it and starts fresh
     (regression guard: force keeps working);
  3. force spawn does not touch another session's worker of the same name;
  4. force spawn does not touch a worker belonging to a different workspace;
  5. ``resume()`` refuses a stopped / stopping thread and leaves it stopped;
  6. ``run()`` honours an already-set ``_stop_event`` at birth (warns, does
     NOT clear it) — this pins the deliberately-undefended behaviour;
  7. force over an instance that SURVIVES force-stop refuses with an honest
     error instead of orphaning or duplicating the registry entry.

The mocked threads in cases 1-4 model BOTH verbs (PHASE 2.1a): ``stop()``
detaches (status ``"detached"``, thread still alive, ``_stop_event`` clear) and
``kill()`` terminates (status ``"killed"``, ``_stop_event`` set, ``is_alive()``
False unless ``stubborn_stop=True``), because a thread the registry may legally
unregister is one that actually died.

Harness mirrors ``tests/test_worker_operation_semantics.py`` (real
``WorkerRegistry`` singleton; ``resolve_workspace_id`` / ``_workspace_dir`` /
``WorkerThread`` patched) for cases 1-4, and drives a real ``WorkerThread``
with its I/O edges patched for cases 5-6.
"""

from __future__ import annotations

import json
import queue
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

from tools.workspace.worker import Worker

SESSION = "sess-force-res"
OTHER_SESSION = "sess-force-res-other"
WORKSPACE_A = "ws_force_res_a"
WORKSPACE_B = "ws_force_res_b"
WORKER_NAMES = [f"w{i}" for i in range(1, 11)]  # w1..w10 in workers.json

# The exact envelope a stopped worker reports (worker_thread._run_query).
STOPPED_ENVELOPE = json.dumps(
    {"status": "stopped", "message": "Worker stopped by user"}
)


def _parse_result(result: str) -> dict:
    return json.loads(result)


class TestForceSpawnResurrection(unittest.TestCase):
    """force=True must start a fresh instance, never revive a stopped one."""

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
        self._current_ws = WORKSPACE_A
        self._p_resolve = mock.patch(
            "tools.workspace.worker.resolve_workspace_id",
            side_effect=lambda *a, **k: self._current_ws,
        )
        self._p_ws_dir = mock.patch("tools.workspace.worker._workspace_dir")
        self._p_thread_cls = mock.patch("tools.workspace.worker.WorkerThread")
        self._mock_ws_dir = self._p_ws_dir.start()
        self._mock_thread_cls = self._p_thread_cls.start()
        self._p_resolve.start()
        self.addCleanup(self._p_resolve.stop)
        self.addCleanup(self._p_ws_dir.stop)
        self.addCleanup(self._p_thread_cls.stop)

        self._created_threads: list[MagicMock] = []
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
        t.status = "ready"
        t._timeout_seconds = 30
        t._stop_event = threading.Event()  # a freshly born thread: not stopped
        t._worker_ctx = None
        t._worker_dir = MagicMock()
        t._output_queue = MagicMock()
        t._output_queue.get.return_value = json.dumps(
            {"response": "fresh run", "status": "completed"}
        )
        t._last_elapsed = MagicMock(return_value=None)
        self._created_threads.append(t)
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

    def _make_worker_thread(
        self,
        name: str,
        iid: int,
        session: str = SESSION,
        status: str = "ready",
        stopped: bool = False,
        stubborn_stop: bool = False,
    ) -> MagicMock:
        """Register a live worker thread in the 3-tuple registry.

        ``stopped=True`` models the report's worker: the thread is still alive
        but its stop request has already been issued (``_stop_event`` set), it
        sits in the "paused" state a stop-in-flight leaves behind, and its
        output queue still holds the stopped envelope a previous caller never
        consumed.

        PHASE 2.1a: the double models BOTH verbs with their real meanings.
        ``stop()`` is a DETACH: the thread parks with status ``"detached"``,
        ``is_alive()`` stays True and ``_stop_event`` stays CLEAR (a detached
        worker must stay resumable, so nothing may mark it terminated).
        ``kill()`` is the only TERMINATION path: status ``"killed"``,
        ``_stop_event`` set, ``is_alive()`` following the thread's real fate.
        Only a thread that is genuinely gone may be unregistered.  Pass
        ``stubborn_stop=True`` for a thread that ignores termination (survives
        force-stop) — the case that must be reported, not hidden.
        """
        from tools.workspace.worker_registry import WorkerRegistry

        t = MagicMock()
        t.is_alive.return_value = True
        t.status = status
        t.instance_id = iid
        t.instance_label = WorkerRegistry.instance_label(name, iid)
        t._manual_only_pause = False
        t._timeout_seconds = 30
        t._worker_ctx = None
        t._worker_dir = MagicMock()
        t._input_queue = queue.Queue()
        t._output_queue = MagicMock()
        t._output_queue.get.return_value = STOPPED_ENVELOPE
        t._last_elapsed = MagicMock(return_value=None)
        t._stop_event = threading.Event()
        if stopped:
            t._stop_event.set()

        def _detach(*_a, **_k):
            # stop() DETACHES: the thread parks and stays alive; the stop event
            # is deliberately NOT set, because a detached worker is resumable.
            t.status = "detached"
            t.is_alive.return_value = True

        def _kill(*_a, **_k):
            # kill() TERMINATES: is_alive() is what the force block and the
            # registry double-check consult, so it follows the thread's fate.
            t.status = "killed"
            t._stop_event.set()
            t.is_alive.return_value = stubborn_stop

        t.stop.side_effect = _detach
        t.kill.side_effect = _kill
        with self._registry._registry_lock:
            self._registry._worker_registry[(session, name, iid)] = t
        return t

    def _spawn(
        self,
        name: str,
        session: str = SESSION,
        force: bool = False,
        instance_id: int | None = None,
        context: dict | None = None,
    ) -> dict:
        tool = Worker(
            action="spawn",
            worker_name=name,
            session_id=session,
            workspace_path="/tmp/test_ws",
            agent_config={"provider": "openai", "model": "gpt-4"},
            force=force,
            instance_id=instance_id,
            context=context,
        )
        return _parse_result(tool.execute())

    def _registry_keys_for(self, name: str) -> list:
        with self._registry._registry_lock:
            return sorted(k for k in self._registry._worker_registry if k[1] == name)

    def _registered(self, key) -> object:
        with self._registry._registry_lock:
            return self._registry._worker_registry.get(key)

    def _real_worker_thread(self, name: str, instance_id: int = 1):
        """A real WorkerThread in a throwaway workspace dir (thread not started)."""
        from tools.workspace.worker_thread import WorkerThread

        tmp = Path(tempfile.mkdtemp(prefix="wt_force_res_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        return WorkerThread(
            name=name,
            definition={},
            agent_config={"provider": "openai", "model": "gpt-4"},
            workspace_dir=tmp,
            session_id=SESSION,
            workspace_id=WORKSPACE_A,
            instance_id=instance_id,
        )

    # ── 1. PRIMARY REPRO: force spawn over a STOPPED worker ────────────

    def test_force_spawn_over_stopped_worker_runs_fresh(self):
        """A stopped worker must not be resumed/replayed: force spawn starts a
        fresh instance and the fresh instance runs the query."""
        stale = self._make_worker_thread(
            "w1", 1, status="paused", stopped=True
        )

        result = self._spawn("w1", force=True, context={"query": "do the thing"})

        # The stopped thread is never revived and its stale envelope is never
        # handed back to the caller.
        stale.resume.assert_not_called()
        stale._output_queue.get.assert_not_called()
        self.assertNotEqual(
            result.get("status"), "stopped", f"resurrected stopped worker: {result}"
        )
        self.assertEqual(
            result.get("response"), "fresh run", f"stale envelope returned: {result}"
        )
        self.assertTrue(result.get("spawned"), f"no fresh instance: {result}")

        # Exactly one fresh thread was created and it received the query.
        self.assertEqual(len(self._created_threads), 1, self._created_threads)
        fresh = self._created_threads[0]
        self.assertEqual(fresh._initial_context.get("query"), "do the thing")
        self.assertEqual(result.get("instance_id"), fresh.instance_id)

        # The registry holds the new thread, not the stopped one.
        self.assertEqual(self._registry_keys_for("w1"), [(SESSION, "w1", 1)])
        self.assertIs(self._registered((SESSION, "w1", 1)), fresh)

    # ── 2. force still restarts a RUNNING worker ───────────────────────

    def test_force_spawn_over_running_worker_restarts_it(self):
        """Regression guard: force over a live, ready worker still stops it and
        starts a fresh instance that runs the query."""
        live = self._make_worker_thread("w2", 1, status="ready")

        result = self._spawn("w2", force=True, context={"query": "restart"})

        # PHASE 2.1a: force must TERMINATE the old instance (kill), never
        # merely detach it (a detached thread would still hold the key).
        live.kill.assert_called_once()
        live.stop.assert_not_called()
        live.resume.assert_not_called()
        live._output_queue.get.assert_not_called()
        self.assertTrue(result.get("spawned"), f"force spawn: {result}")
        self.assertEqual(result.get("response"), "fresh run")
        self.assertEqual(result.get("instance_id"), 1)

        self.assertEqual(len(self._created_threads), 1, self._created_threads)
        self.assertIs(
            self._registered((SESSION, "w2", 1)), self._created_threads[0]
        )

    # ── 3. force is session-scoped ─────────────────────────────────────

    def test_force_spawn_does_not_stop_other_session_worker(self):
        """Forcing a worker in this session must leave another session's
        worker of the same name untouched (and registered)."""
        foreign = self._make_worker_thread(
            "w3", 1, session=OTHER_SESSION, status="ready"
        )

        result = self._spawn("w3", force=True, context={"query": "mine"})

        # Untouched by EITHER verb: not detached, not killed.
        foreign.stop.assert_not_called()
        foreign.kill.assert_not_called()
        foreign.resume.assert_not_called()
        self.assertIs(
            self._registered((OTHER_SESSION, "w3", 1)),
            foreign,
            "another session's worker was stopped/unregistered by force spawn",
        )
        self.assertTrue(result.get("spawned"), f"spawn: {result}")
        self.assertEqual(result.get("response"), "fresh run")
        # Both survive, each in its own session (sorted: SESSION < OTHER_SESSION).
        self.assertEqual(
            self._registry_keys_for("w3"),
            [(SESSION, "w3", 1), (OTHER_SESSION, "w3", 1)],
        )

    # ── 4. force is workspace-scoped ───────────────────────────────────

    def test_force_spawn_does_not_stop_other_workspace_worker(self):
        """What this actually pins: force is SESSION-scoped, and session
        scoping is the only thing that separates workspaces.

        The registry is process-global and keyed by (session, name, instance),
        with no workspace component; a force spawn can only sweep entries whose
        session matches.  Because each workspace's bridge runs under its own
        session id, workspace A's worker survives a workspace B force spawn as
        a CONSEQUENCE of that scoping — not because anything compares workspace
        ids.  Do not read this test as a standalone workspace-isolation
        guarantee: two workspaces sharing one session id would share workers.
        """
        workspace_a_worker = self._make_worker_thread(
            "w4", 1, session=OTHER_SESSION, status="ready"
        )

        # Switch the tool to workspace B (the registry is process-global).
        self._current_ws = WORKSPACE_B
        self._setup_workspace()

        result = self._spawn("w4", force=True, context={"query": "workspace B work"})

        # Untouched by EITHER verb: not detached, not killed.
        workspace_a_worker.stop.assert_not_called()
        workspace_a_worker.kill.assert_not_called()
        workspace_a_worker.resume.assert_not_called()
        self.assertIs(
            self._registered((OTHER_SESSION, "w4", 1)),
            workspace_a_worker,
            "workspace A's worker was stopped by a workspace B force spawn",
        )
        self.assertTrue(result.get("spawned"), f"spawn: {result}")
        self.assertEqual(result.get("response"), "fresh run")
        # Both survive, each in its own session (sorted: SESSION < OTHER_SESSION).
        self.assertEqual(
            self._registry_keys_for("w4"),
            [(SESSION, "w4", 1), (OTHER_SESSION, "w4", 1)],
        )

    # ── 5. resume() refuses a stopped thread ───────────────────────────

    def test_resume_refuses_stopped_worker(self):
        """resume() must not revive a stopped/stopping thread: it returns
        False, keeps _stop_event set and leaves the status alone."""
        thread = self._real_worker_thread("w5")
        with mock.patch.object(thread, "_write_status_file"):
            for terminal_status in ("stopped", "stopping"):
                with self.subTest(status=terminal_status):
                    thread._stop_event.set()
                    thread._pause_event.set()
                    thread.status = terminal_status

                    outcome = thread.resume()

                    self.assertIs(
                        outcome,
                        False,
                        f"resume() on a {terminal_status} worker must refuse "
                        f"(returned {outcome!r})",
                    )
                    self.assertEqual(thread.status, terminal_status)
                    self.assertTrue(
                        thread._stop_event.is_set(),
                        "resume() cleared the stop event of a stopped worker",
                    )
                    self.assertTrue(
                        thread._pause_event.is_set(),
                        "resume() cleared the pause state of a stopped worker",
                    )

    # ── 6. stop-at-birth is honoured, not cleared ─────────────────────

    def test_run_honours_stop_event_set_before_birth(self):
        """Deliberately NOT defended: a stop request that is already set when
        ``run()`` starts is honoured, not cleared.

        ``_stop_event`` is created in ``WorkerThread.__init__`` and never
        assigned outside that file, so it cannot be inherited from a previous
        life of the same instance id.  An already-set event therefore means
        something really did stop THIS instance (``stop()``, the command-file
        poll, or the WLM) after it was constructed; clearing it at birth — the
        old behaviour — would silently revive a stopped worker.  So run()
        observes the flag (WARNING naming worker + instance) and exits before
        the main loop.

        Residual risk, accepted: such a thread is "born dead" and the spawn
        caller gets a bounded failure (it reads its output queue with a
        timeout and reports \"did not respond within Ns\") rather than an
        immediate, precise error.
        """
        thread = self._real_worker_thread("w6", instance_id=1)
        thread._stop_event.set()  # a real stop for this instance, not a leftover
        thread._input_queue.put(None)  # sentinel: the main loop breaks at once
        logs: list[tuple] = []

        with mock.patch.object(thread, "_load_context", return_value=None), \
                mock.patch.object(thread, "_write_status_file"), \
                mock.patch.object(thread, "_save_context"), \
                mock.patch.object(thread, "_publish_event"), \
                mock.patch.object(thread, "_poll_command") as poll_command, \
                mock.patch.object(thread, "_heartbeat_tick"), \
                mock.patch.object(thread, "_cleanup_worker_containers"), \
                mock.patch("tools.workspace.worker.global_event_bus", None), \
                mock.patch("tools.workspace.worker.EventBus", MagicMock()), \
                mock.patch(
                    "tools.workspace.worker_thread.log",
                    side_effect=lambda *a, **k: logs.append(a),
                ):
            thread.run()

        self.assertTrue(
            thread._stop_event.is_set(),
            "run() cleared a stop request that was genuinely set for this "
            "instance: the worker was revived against the caller's wishes",
        )
        # Honouring the stop means the main loop is never entered.
        self.assertFalse(
            poll_command.called,
            "run() entered the main loop despite an already-set stop request",
        )
        self.assertTrue(
            any(
                "w6" in str(args) and any("WARNING" in str(a) for a in args)
                for args in logs
            ),
            f"no WARNING naming the stopped-at-birth worker: {logs}",
        )
        self.assertTrue(
            any(
                "not cleared" in str(args)
                for args in logs
            ),
            f"the warning does not state that the stop was NOT cleared: {logs}",
        )

    # ── 7. force over an instance that survived force-stop ──────────

    def test_force_spawn_refuses_when_instance_survives_force_stop(self):
        """Force over a live instance that ignores ``kill()`` must return an
        honest error and leave the registry consistent.

        The old code popped the registry entry after stopping regardless of
        whether the thread died, orphaning a live-but-invisible instance; the
        double-checked spawn then registered a second thread under the same key
        (duplicate/leaked entry).  Now the entry is kept and the spawn is
        refused with an error that says the instance survived force-stop.
        """
        stale = self._make_worker_thread(
            "w7", 1, status="ready", stubborn_stop=True
        )

        result = self._spawn(
            "w7", force=True, instance_id=1, context={"query": "x"}
        )

        # PHASE 2.1a: the force block calls kill(); this double survives it.
        stale.kill.assert_called_once()
        stale.stop.assert_not_called()
        self.assertIn("error", result, f"expected a refusal, got {result}")
        message = str(result["error"]).lower()
        self.assertIn(
            "survived force-stop",
            message,
            f"the error does not say the instance survived force-stop: {result}",
        )
        self.assertIn("could not be replaced", message, result)

        # The stale instance is still visible and addressable: no orphan...
        self.assertIs(
            self._registered((SESSION, "w7", 1)),
            stale,
            "the surviving instance was unregistered (orphaned)",
        )
        self.assertEqual(self._registry_keys_for("w7"), [(SESSION, "w7", 1)])

        # ...and no second thread took over its key or was started.
        self.assertEqual(len(self._created_threads), 1, self._created_threads)
        self.assertIsNot(self._created_threads[0], stale)
        self.assertIsNot(self._registered((SESSION, "w7", 1)), self._created_threads[0])
        self._created_threads[0].start.assert_not_called()
        self.assertFalse(result.get("spawned"), result)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
