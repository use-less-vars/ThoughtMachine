"""
Presence contract for worker liveness (E1/E2).

The measured bug this file pins: ``_resolve_worker_thread`` decided liveness
with the registry entry's own ``is_alive()``, but a process-mode worker's
supervisor thread is *deliberately never started* (``WorkerThread.start``
returns early so no second body runs beside the child). Its ``is_alive()`` is
therefore ``False`` for a worker whose body is a healthy child, so every
name-only lookup of a live process-mode worker failed with "Worker 'X' is not
running".

Surfaces under test:

  1. ``worker_runtime.worker_is_live`` -- the ONE liveness predicate, truth
     table for both modes including the fail-open directions;
  2. ``WorkerRuntimeHandle.alive`` -- the handle's own answer, which must never
     construct a runtime to answer it;
  3. ``worker_thread._resolve_worker_thread`` -- ACCEPTANCE: a process-stamped
     worker with a live handle resolves; today's thread-mode behaviour and the
     ambiguity error are preserved;
  4. retention: a completed thread-mode entry (``is_alive()`` False) stays
     filtered and unresolvable, and is NOT removed by the lookup.

Unit only: no child process is spawned, no ``fork``, no real ``WorkerThread``.

Run:  python -m pytest tests/test_worker_presence_contract.py -q
"""

from __future__ import annotations

import unittest
from unittest import mock

import tools.workspace.worker_thread as worker_thread_module
from tools.workspace.worker_registry import WorkerRegistry
from tools.workspace.worker_runtime import (
    MODE_PROCESS,
    MODE_THREAD,
    WorkerRuntimeHandle,
    worker_is_live,
)

SESSION = "sess-presence-contract"

#: Distinguishes "the attribute was not given" from "it was given as None".
_MISSING = object()


class _FakeWorker:
    """A worker stand-in that has ONLY the attributes it was given.

    Deliberately not a ``MagicMock``: a mock answers every attribute access, so
    ``_runtime_mode`` would come back as a Mock and every case would land in the
    process branch. Here an unset attribute really is absent.
    """

    def __init__(self, *, probe=_MISSING, mode=_MISSING, handle=_MISSING):
        if probe is not _MISSING:
            if isinstance(probe, BaseException):
                def _raise(exc=probe):
                    raise exc

                self.is_alive = _raise
            else:
                self.is_alive = lambda: probe
        if mode is not _MISSING:
            self._runtime_mode = mode
        if handle is not _MISSING:
            self._runtime_handle = handle


class _FakeHandle:
    """A handle stand-in exposing ``alive`` (and ``mode``) as plain attributes."""

    def __init__(self, alive=_MISSING, *, mode=MODE_PROCESS):
        if alive is not _MISSING:
            self.alive = alive
        self.mode = mode


class _FakeRuntime:
    """Stands in for ``ProcessWorkerRuntime``: the handle only reads ``.alive``."""

    def __init__(self, alive):
        self.alive = alive


class _FakeThread:
    """A registry entry double: no auto-created attributes, no real thread."""

    def __init__(self, *, alive=False, mode=_MISSING, handle=_MISSING, status="ready"):
        self._alive = alive
        self.status = status
        if mode is not _MISSING:
            self._runtime_mode = mode
        if handle is not _MISSING:
            self._runtime_handle = handle

    def is_alive(self):
        return self._alive


class TestWorkerIsLiveTruthTable(unittest.TestCase):
    """``worker_is_live``: three rules, each fail direction pinned."""

    def test_unstamped_alive_worker_is_live(self):
        self.assertTrue(worker_is_live(_FakeWorker(probe=True)))

    def test_unstamped_dead_worker_is_not_live(self):
        self.assertFalse(worker_is_live(_FakeWorker(probe=False)))

    def test_unstamped_worker_without_probe_fails_open(self):
        self.assertTrue(worker_is_live(_FakeWorker()))

    def test_unstamped_worker_whose_probe_raises_fails_open(self):
        self.assertTrue(worker_is_live(_FakeWorker(probe=RuntimeError("boom"))))

    def test_thread_stamp_delegates_to_is_alive(self):
        self.assertTrue(worker_is_live(_FakeWorker(probe=True, mode=MODE_THREAD)))
        self.assertFalse(worker_is_live(_FakeWorker(probe=False, mode=MODE_THREAD)))

    def test_thread_stamp_without_probe_fails_open(self):
        self.assertTrue(worker_is_live(_FakeWorker(mode=MODE_THREAD)))

    def test_process_stamp_with_live_handle_is_live(self):
        worker = _FakeWorker(mode=MODE_PROCESS, handle=_FakeHandle(alive=True))
        self.assertTrue(worker_is_live(worker))

    def test_process_stamp_with_dead_handle_is_not_live(self):
        worker = _FakeWorker(mode=MODE_PROCESS, handle=_FakeHandle(alive=False))
        self.assertFalse(worker_is_live(worker))

    def test_process_stamp_with_no_child_yet_fails_open(self):
        # ``None`` is "no child built yet", never evidence of death.
        worker = _FakeWorker(mode=MODE_PROCESS, handle=_FakeHandle(alive=None))
        self.assertTrue(worker_is_live(worker))

    def test_process_stamp_without_handle_is_not_live(self):
        # Stamped process, no transport => no reachable body.
        self.assertFalse(worker_is_live(_FakeWorker(mode=MODE_PROCESS)))

    def test_process_stamp_ignores_the_supervisor_thread(self):
        # The exact measured bug: the supervisor thread is never started, so
        # ``is_alive()`` is False for a worker whose child is healthy.
        worker = _FakeWorker(
            probe=False, mode=MODE_PROCESS, handle=_FakeHandle(alive=True)
        )
        self.assertTrue(worker_is_live(worker))


class TestHandleAliveProperty(unittest.TestCase):
    """``WorkerRuntimeHandle.alive`` -- and it never builds a runtime."""

    def test_thread_mode_delegates_to_the_worker(self):
        handle = WorkerRuntimeHandle(_FakeWorker(probe=True), mode=MODE_THREAD)
        self.assertIs(handle.alive, True)
        handle = WorkerRuntimeHandle(_FakeWorker(probe=False), mode=MODE_THREAD)
        self.assertIs(handle.alive, False)

    def test_thread_mode_without_a_callable_probe_is_unknown(self):
        handle = WorkerRuntimeHandle(_FakeWorker(), mode=MODE_THREAD)
        self.assertIsNone(handle.alive)

    def test_thread_mode_probe_that_raises_is_unknown(self):
        handle = WorkerRuntimeHandle(
            _FakeWorker(probe=RuntimeError("boom")), mode=MODE_THREAD
        )
        self.assertIsNone(handle.alive)

    def test_process_mode_without_runtime_is_unknown_and_builds_nothing(self):
        handle = WorkerRuntimeHandle(_FakeWorker(probe=False), mode=MODE_PROCESS)
        self.assertIsNone(handle._process_runtime)
        self.assertIsNone(handle.alive)
        # The probe must not be the thing that spawns an OS process.
        self.assertIsNone(handle._process_runtime)

    def test_process_mode_mirrors_the_runtime(self):
        handle = WorkerRuntimeHandle(_FakeWorker(probe=False), mode=MODE_PROCESS)
        runtime = _FakeRuntime(True)
        handle._process_runtime = runtime
        self.assertIs(handle.alive, True)
        self.assertIs(handle._process_runtime, runtime)
        runtime.alive = False
        self.assertIs(handle.alive, False)
        self.assertIs(handle._process_runtime, runtime)


class TestResolverPresence(unittest.TestCase):
    """``_resolve_worker_thread`` decides liveness through ``worker_is_live``."""

    def setUp(self) -> None:
        self._registry = WorkerRegistry.get_instance()
        with self._registry._registry_lock:
            self._old_registry = dict(self._registry._worker_registry)
            self._registry._worker_registry.clear()
        self.addCleanup(self._restore_registry)
        # The resolver iterates the ``worker_thread`` module global, which is
        # the very dict the singleton holds. Seeding through the singleton is
        # therefore the same store -- assert it rather than assume it.
        self.addCleanup(self._assert_still_shared)
        self.assertIs(worker_thread_module._worker_registry,
                      self._registry._worker_registry)

    def _assert_still_shared(self) -> None:
        self.assertIs(worker_thread_module._worker_registry,
                      self._registry._worker_registry)

    def _restore_registry(self) -> None:
        with self._registry._registry_lock:
            self._registry._worker_registry.clear()
            self._registry._worker_registry.update(self._old_registry)

    def _seed(self, entry, thread) -> None:
        with self._registry._registry_lock:
            self._registry._worker_registry[entry] = thread

    # ── (a) ACCEPTANCE: the measured bug ───────────────────────────────────
    def test_process_stamped_worker_with_live_handle_resolves(self):
        thread = _FakeThread(
            alive=False, mode=MODE_PROCESS, handle=_FakeHandle(alive=True)
        )
        self._seed((SESSION, "w1", 1), thread)

        resolved, sid, iid = worker_thread_module._resolve_worker_thread(
            "w1", session_id=SESSION
        )

        self.assertIs(resolved, thread)
        self.assertEqual((sid, iid), (SESSION, 1))

    def test_process_stamped_worker_with_no_child_yet_resolves(self):
        # ``alive is None`` ("no child built yet") must not read as death.
        thread = _FakeThread(
            alive=False, mode=MODE_PROCESS, handle=_FakeHandle(alive=None)
        )
        self._seed((SESSION, "w1", 1), thread)

        resolved, _sid, _iid = worker_thread_module._resolve_worker_thread(
            "w1", session_id=SESSION
        )

        self.assertIs(resolved, thread)

    def test_process_stamped_worker_with_dead_child_is_not_running(self):
        thread = _FakeThread(
            alive=False, mode=MODE_PROCESS, handle=_FakeHandle(alive=False)
        )
        self._seed((SESSION, "w1", 1), thread)

        with self.assertRaises(worker_thread_module._WorkerAmbiguityError) as ctx:
            worker_thread_module._resolve_worker_thread("w1", session_id=SESSION)

        self.assertIn("is not running", str(ctx.exception))

    # ── (b) today's thread-mode behaviour, preserved ───────────────────────
    def test_thread_mode_dead_worker_still_raises_not_running(self):
        self._seed((SESSION, "w1", 1), _FakeThread(alive=False, mode=MODE_THREAD))

        with self.assertRaises(worker_thread_module._WorkerAmbiguityError) as ctx:
            worker_thread_module._resolve_worker_thread("w1", session_id=SESSION)

        self.assertIn("is not running", str(ctx.exception))

    def test_thread_mode_live_worker_resolves(self):
        thread = _FakeThread(alive=True, mode=MODE_THREAD)
        self._seed((SESSION, "w1", 1), thread)

        resolved, sid, iid = worker_thread_module._resolve_worker_thread(
            "w1", session_id=SESSION
        )

        self.assertIs(resolved, thread)
        self.assertEqual((sid, iid), (SESSION, 1))

    # ── (c) ambiguity is preserved for two LIVE process instances ──────────
    def test_two_live_process_instances_are_ambiguous(self):
        self._seed(
            (SESSION, "w1", 1),
            _FakeThread(alive=False, mode=MODE_PROCESS, handle=_FakeHandle(alive=True)),
        )
        self._seed(
            (SESSION, "w1", 2),
            _FakeThread(alive=False, mode=MODE_PROCESS, handle=_FakeHandle(alive=True)),
        )

        with self.assertRaises(worker_thread_module._WorkerAmbiguityError) as ctx:
            worker_thread_module._resolve_worker_thread("w1", session_id=SESSION)

        message = str(ctx.exception)
        self.assertIn("is ambiguous", message)
        self.assertNotIn("is not running", message)

    # ── (d) nothing registered ─────────────────────────────────────────────
    def test_empty_registry_raises_not_running(self):
        with self.assertRaises(worker_thread_module._WorkerAmbiguityError) as ctx:
            worker_thread_module._resolve_worker_thread("w1", session_id=SESSION)

        self.assertIn("is not running", str(ctx.exception))


class TestRetentionPin(unittest.TestCase):
    """A completed thread-mode entry stays in the registry and stays filtered."""

    def setUp(self) -> None:
        self._registry = WorkerRegistry.get_instance()
        with self._registry._registry_lock:
            self._old_registry = dict(self._registry._worker_registry)
            self._registry._worker_registry.clear()
        self.addCleanup(self._restore_registry)

    def _restore_registry(self) -> None:
        with self._registry._registry_lock:
            self._registry._worker_registry.clear()
            self._registry._worker_registry.update(self._old_registry)

    def test_completed_thread_entry_stays_unresolvable_and_retained(self):
        entry = (SESSION, "w1", 1)
        with self._registry._registry_lock:
            self._registry._worker_registry[entry] = _FakeThread(
                alive=False, mode=MODE_THREAD, status="completed"
            )

        with self.assertRaises(worker_thread_module._WorkerAmbiguityError) as ctx:
            worker_thread_module._resolve_worker_thread("w1", session_id=SESSION)

        self.assertIn("is not running", str(ctx.exception))
        # Retention by design (``WorkerThread.run``'s finally unregisters only
        # on a timeout trigger): a lookup is not the thing that evicts it.
        with self._registry._registry_lock:
            self.assertIn(entry, self._registry._worker_registry)


# ===========================================================================
# Only an EXACT ``MODE_PROCESS`` stamp takes the process branch (hardening)
# ---------------------------------------------------------------------------
# ``worker_is_live`` narrows to ``mode != MODE_PROCESS`` -> the historical
# ``is_alive()`` reading. Everything else -- absent, thread, an unrecognised
# value, or a duck-typed double's auto-attribute -- is read through
# ``is_alive()`` alone and never through ``_runtime_handle``.
# ===========================================================================


class TestOnlyAnExactProcessStampTakesTheProcessBranch(unittest.TestCase):
    """Unknown or fabricated stamps degrade to the legacy reading."""

    def setUp(self) -> None:
        self._registry = WorkerRegistry.get_instance()
        with self._registry._registry_lock:
            self._old_registry = dict(self._registry._worker_registry)
            self._registry._worker_registry.clear()
        self.addCleanup(self._restore_registry)

    def _restore_registry(self) -> None:
        with self._registry._registry_lock:
            self._registry._worker_registry.clear()
            self._registry._worker_registry.update(self._old_registry)

    def test_bare_magicmock_registry_entry_reads_dead_and_is_not_counted(self):
        # The measured regression: a bare ``MagicMock`` answers
        # ``_runtime_mode`` with a Mock, so a predicate that only compared
        # against ``None``/``MODE_THREAD`` read it as a process worker with a
        # fabricated, live-looking handle -- and counted it toward the cap.
        thread = mock.MagicMock()
        thread.is_alive.return_value = False
        # Precondition: the stamp exists without anyone having assigned it,
        # which is exactly why an explicit vocabulary test is required.
        self.assertNotIn(thread._runtime_mode, (None, MODE_THREAD, MODE_PROCESS))
        with self._registry._registry_lock:
            self._registry._worker_registry[(SESSION, "w1", 1)] = thread

        self.assertIs(worker_is_live(thread), False)
        self.assertEqual(
            worker_thread_module.Worker._live_workers_in_session(object(), SESSION), 0
        )

    def test_unrecognised_mode_stamp_is_read_through_is_alive_only(self):
        # A stamp from a build this one does not implement is not a stamp, so
        # the handle is never consulted -- in either direction: a dead-looking
        # handle cannot kill a live thread, and a live-looking one cannot
        # resurrect a dead thread.
        live_thread = _FakeWorker(
            probe=True, mode="garbage", handle=_FakeHandle(alive=False)
        )
        dead_thread = _FakeWorker(
            probe=False, mode="garbage", handle=_FakeHandle(alive=True)
        )
        self.assertIs(worker_is_live(live_thread), True)
        self.assertIs(worker_is_live(dead_thread), False)

        # Same rule for a double that was never stamped but auto-answers every
        # attribute: its ``_runtime_handle.alive`` is a truthy Mock.
        auto = mock.MagicMock()
        auto.is_alive.return_value = False
        self.assertTrue(auto._runtime_handle.alive)  # precondition: fabricated
        self.assertIs(worker_is_live(auto), False)
        auto.is_alive.return_value = True
        self.assertIs(worker_is_live(auto), True)

    def test_genuine_process_stamp_still_reads_live(self):
        # Regression guard for the narrowed predicate: the ONE legal
        # non-thread stamp must still reach the handle, or a healthy process
        # worker whose supervisor thread was never started reads dead again.
        thread = _FakeThread(
            alive=False, mode=MODE_PROCESS, handle=_FakeHandle(alive=True)
        )
        self.assertIs(thread.is_alive(), False)
        self.assertIs(worker_is_live(thread), True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
