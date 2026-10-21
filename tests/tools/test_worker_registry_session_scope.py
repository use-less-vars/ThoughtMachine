"""Session-scoped ``shutdown_workers`` regression (gen-47 open item).

The breach these tests lock down: a *legitimate* ``close_session`` for session
A used to call ``shutdown_workers()`` with no argument, which swept the WHOLE
registry and stopped workspace B's still-live worker threads.

The discriminating observable is ``stop_calls`` on the *other* session's
worker.  ``shutdown_workers()`` stops threads but never pops their registry
keys, so a "the other session's entry is still registered" assertion passes
during a breach and proves nothing.

Control legs are mandatory here (branch practice): a scoped sweep that stops
nothing at all would also leave the other session untouched, so every
"B was spared" assertion is paired with "the named session actually died".
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tools.workspace.worker_registry import ALL_SESSIONS, WorkerRegistry


class _FakeLiveWorker:
    """Minimal stand-in for a live WorkerThread registry entry."""

    def __init__(self, worker_name: str, session_id: str) -> None:
        self.worker_name = worker_name
        self.session_id = session_id
        self.status = "ready"
        self.stop_calls = 0
        self.join_timeouts: list[float | None] = []
        self.save_calls = 0
        # shutdown_workers() compacts this before saving when it is not None.
        self._worker_ctx = None

    def is_alive(self) -> bool:
        return self.stop_calls == 0

    def stop(self) -> None:
        self.stop_calls += 1

    def join(self, timeout: float | None = None) -> None:
        self.join_timeouts.append(timeout)

    def _save_context(self) -> None:
        self.save_calls += 1


@pytest.fixture
def registry():
    """Fresh registry view; the singleton's dict is restored afterwards."""
    wr = WorkerRegistry.get_instance()
    with wr._registry_lock:
        old = dict(wr._worker_registry)
        wr._worker_registry.clear()
    yield wr
    with wr._registry_lock:
        wr._worker_registry.clear()
        wr._worker_registry.update(old)


@pytest.fixture
def pair(registry):
    """One live worker registered under session A, one under session B."""
    worker_a = _FakeLiveWorker("w", "sess-a")
    worker_b = _FakeLiveWorker("w", "sess-b")
    with registry._registry_lock:
        registry._worker_registry[("sess-a", "w", 1)] = worker_a
        registry._worker_registry[("sess-b", "w", 1)] = worker_b
    return worker_a, worker_b


class TestSessionScopedShutdown:
    def test_scoped_sweep_spares_other_sessions(self, registry, pair):
        """close_session(A) must not stop workspace B's worker thread."""
        worker_a, worker_b = pair

        registry.shutdown_workers(timeout=0.5, session_id="sess-a")

        # CONTROL: the sweep ran and hit the session it was scoped to.
        assert worker_a.stop_calls == 1, (
            "CONTROL FAILED: shutdown_workers(session_id='sess-a') did not stop "
            "session A's own worker, so 'B was spared' proves nothing"
        )
        assert worker_a.save_calls == 1

        # DISCRIMINATOR: the other session's thread was never touched.
        assert worker_b.stop_calls == 0, (
            "BREACH: closing session 'sess-a' stopped session 'sess-b' worker "
            f"(stop_calls={worker_b.stop_calls})"
        )
        assert worker_b.save_calls == 0
        assert worker_b.join_timeouts == []

    def test_scoped_sweep_hits_named_session_b(self, registry, pair):
        """Control leg: scoping to B stops B and spares A."""
        worker_a, worker_b = pair

        registry.shutdown_workers(timeout=0.5, session_id="sess-b")

        assert worker_b.stop_calls == 1
        assert worker_a.stop_calls == 0, (
            "BREACH: closing session 'sess-b' stopped session 'sess-a' worker"
        )

    def test_unknown_session_scope_is_a_noop(self, registry, pair):
        worker_a, worker_b = pair

        registry.shutdown_workers(timeout=0.5, session_id="sess-does-not-exist")

        assert worker_a.stop_calls == 0
        assert worker_b.stop_calls == 0

    def test_none_session_id_is_rejected(self, registry):
        """Fail closed: ``None`` used to mean the empty-session bucket."""
        empty = _FakeLiveWorker("w", "")
        named = _FakeLiveWorker("w", "sess-a")
        with registry._registry_lock:
            registry._worker_registry[("", "w", 1)] = empty
            registry._worker_registry[("sess-a", "w", 1)] = named

        with pytest.raises(ValueError, match="explicit session_id"):
            registry.shutdown_workers(timeout=0.5, session_id=None)

        assert empty.stop_calls == 0, (
            "a rejected shutdown must not sweep the empty-session bucket"
        )
        assert named.stop_calls == 0

    def test_missing_session_id_is_rejected(self, registry, pair):
        """Fail closed: no implicit whole-registry sweep any more."""
        worker_a, worker_b = pair

        with pytest.raises(TypeError):
            registry.shutdown_workers(timeout=0.5)

        assert worker_a.stop_calls == 0
        assert worker_b.stop_calls == 0

    def test_all_sessions_sentinel_sweeps_every_session(self, registry, pair):
        worker_a, worker_b = pair

        registry.shutdown_workers(timeout=0.5, session_id=ALL_SESSIONS)

        assert worker_a.stop_calls == 1
        assert worker_b.stop_calls == 1

    def test_already_dead_worker_is_skipped(self, registry):
        dead = _FakeLiveWorker("w", "sess-a")
        dead.stop_calls = 1  # is_alive() -> False
        with registry._registry_lock:
            registry._worker_registry[("sess-a", "w", 1)] = dead

        registry.shutdown_workers(timeout=0.5, session_id="sess-a")

        assert dead.stop_calls == 1, "a dead worker must not be stopped twice"
        assert dead.save_calls == 0


class TestFacadeForwardsSessionScope:
    """The backward-compat facade is what bridge.close_session calls."""

    def test_forwards_session_id(self, monkeypatch):
        from tools.workspace import worker as worker_module

        mock_registry = MagicMock()
        monkeypatch.setattr(
            worker_module._WorkerRegistry, "get_instance",
            staticmethod(lambda: mock_registry),
        )

        worker_module.shutdown_workers(timeout=2.0, session_id="sess-a")

        mock_registry.shutdown_workers.assert_called_once_with(
            timeout=2.0, session_id="sess-a"
        )

    def test_missing_session_id_is_rejected_by_facade(self):
        """The facade is fail-closed too: no implicit whole-registry sweep."""
        from tools.workspace import worker as worker_module

        with pytest.raises(TypeError):
            worker_module.shutdown_workers(timeout=2.0)

    def test_none_session_id_is_rejected_by_facade(self):
        """``None`` is forwarded and rejected by the registry, not normalised."""
        from tools.workspace import worker as worker_module

        with pytest.raises(ValueError, match="explicit session_id"):
            worker_module.shutdown_workers(timeout=2.0, session_id=None)

    def test_forwards_all_sessions_sentinel_unchanged(self, monkeypatch):
        """The sentinel is forwarded verbatim; the facade adds no branch."""
        from tools.workspace import worker as worker_module

        mock_registry = MagicMock()
        monkeypatch.setattr(
            worker_module._WorkerRegistry, "get_instance",
            staticmethod(lambda: mock_registry),
        )

        worker_module.shutdown_workers(timeout=2.0, session_id=ALL_SESSIONS)

        mock_registry.shutdown_workers.assert_called_once_with(
            timeout=2.0, session_id=ALL_SESSIONS
        )
