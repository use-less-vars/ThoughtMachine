"""
test_worker_registry.py — Tests for the WorkerRegistry singleton.

Validates thread safety, backward-compat module-level access, and
the interaction with worker.py's existing action methods.
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tools.workspace.worker_registry import WorkerRegistry


class TestWorkerRegistrySingleton:
    """WorkerRegistry must be a true singleton."""

    def test_get_instance_returns_same(self):
        wr1 = WorkerRegistry.get_instance()
        wr2 = WorkerRegistry.get_instance()
        assert wr1 is wr2

    def test_get_instance_is_thread_safe(self):
        results: list[WorkerRegistry] = []

        def _get():
            results.append(WorkerRegistry.get_instance())

        threads = [threading.Thread(target=_get) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert all(r is results[0] for r in results)

    def test_direct_instantiation_fails(self):
        with pytest.raises(RuntimeError, match="Use WorkerRegistry.get_instance"):
            WorkerRegistry()


class TestWorkerThreadRegistry:
    """Worker thread register / lookup / unregister operations."""

    @pytest.fixture
    def registry(self):
        # Fresh registry for test isolation — we reach into the singleton's
        # internal dicts (controlled reset only for test purposes).
        wr = WorkerRegistry.get_instance()
        with wr._registry_lock:
            old = dict(wr._worker_registry)
            wr._worker_registry.clear()
        yield wr
        # Restore
        with wr._registry_lock:
            wr._worker_registry.clear()
            wr._worker_registry.update(old)

    def test_register_and_get(self, registry):
        registry.register_worker("sess1", "w1", "thread_a")
        assert registry.get_worker("sess1", "w1") == "thread_a"

    def test_get_missing(self, registry):
        assert registry.get_worker("nonexistent", "x") is None

    def test_unregister(self, registry):
        registry.register_worker("sess1", "w1", "thread_a")
        assert registry.unregister_worker("sess1", "w1") == "thread_a"
        assert registry.get_worker("sess1", "w1") is None

    def test_unregister_missing(self, registry):
        assert registry.unregister_worker("x", "y") is None

    def test_get_all_workers(self, registry):
        registry.register_worker("sess1", "w1", "a")
        registry.register_worker("sess1", "w2", "b")
        registry.register_worker("sess2", "w1", "c")
        all_w = registry.get_all_workers()
        assert len(all_w) == 3
        # Keys are (session, name, instance_id) triples; default instance is 1.
        assert all_w[("sess1", "w1", 1)] == "a"
        assert all_w[("sess1", "w2", 1)] == "b"
        assert all_w[("sess2", "w1", 1)] == "c"

    def test_find_workers_by_name(self, registry):
        registry.register_worker("sess1", "find_me", "thread_a")
        registry.register_worker("sess2", "find_me", "thread_b")
        registry.register_worker("sess1", "other", "thread_c")
        results = registry.find_workers_by_name("find_me")
        assert len(results) == 2
        assert ("sess1", "thread_a") in results
        assert ("sess2", "thread_b") in results

    def test_session_key_empty_normalized(self, registry):
        registry.register_worker(None, "w1", "thread")
        assert registry.get_worker("", "w1") == "thread"
        assert registry.get_worker(None, "w1") == "thread"


class TestEventBusRegistry:
    """Per-worker EventBus register / lookup / unregister operations."""

    @pytest.fixture
    def registry(self):
        wr = WorkerRegistry.get_instance()
        with wr._bus_registry_lock:
            old = dict(wr._worker_event_bus_registry)
            wr._worker_event_bus_registry.clear()
        yield wr
        with wr._bus_registry_lock:
            wr._worker_event_bus_registry.clear()
            wr._worker_event_bus_registry.update(old)

    def test_register_and_get(self, registry):
        bus = object()
        registry.register_event_bus("sess1", "w1", bus)
        assert registry.get_event_bus("sess1", "w1") is bus

    def test_unregister(self, registry):
        bus = object()
        registry.register_event_bus("sess1", "w1", bus)
        registry.unregister_event_bus("sess1", "w1")
        assert registry.get_event_bus("sess1", "w1") is None

    def test_get_event_buses_for_session(self, registry):
        bus_a = object()
        bus_b = object()
        registry.register_event_bus("sess1", "w1", bus_a)
        registry.register_event_bus("sess1", "w2", bus_b)
        registry.register_event_bus("sess2", "w1", object())
        buses = registry.get_event_buses_for_session("sess1")
        assert buses == {"w1": bus_a, "w2": bus_b}

    def test_get_event_buses_empty_session(self, registry):
        assert registry.get_event_buses_for_session("unknown") == {}


class TestWorkerRegistryIntegration:
    """Ensure the module-level backward-compat layer in worker.py works."""

    def test_module_level_vars_match_singleton(self):
        from tools.workspace import worker as w

        wr = WorkerRegistry.get_instance()
        assert w._worker_registry is wr._worker_registry
        assert w._registry_lock is wr._registry_lock
        assert w._worker_event_bus_registry is wr._worker_event_bus_registry
        assert w._bus_registry_lock is wr._bus_registry_lock

    def test_backward_compat_functions_exist(self):
        from tools.workspace import worker as w

        assert callable(w.shutdown_workers)
        assert callable(w.register_worker_event_bus)
        assert callable(w.unregister_worker_event_bus)
        assert callable(w.get_worker_event_bus)
        assert callable(w.get_worker_event_buses_for_session)

    def test_backward_compat_delegates_to_singleton(self):
        from tools.workspace import worker as w

        with patch.object(WorkerRegistry, "get_instance") as mock_get:
            mock_registry = MagicMock()
            mock_get.return_value = mock_registry

            w.shutdown_workers(timeout=3.0)
            mock_registry.shutdown_workers.assert_called_once_with(timeout=3.0)

            w.register_worker_event_bus("s", "w", "bus")
            mock_registry.register_event_bus.assert_called_once_with(
                "s", "w", "bus", instance_id=1
            )

            w.unregister_worker_event_bus("s", "w")
            mock_registry.unregister_event_bus.assert_called_once_with(
                "s", "w", instance_id=1
            )

            w.get_worker_event_bus("s", "w")
            mock_registry.get_event_bus.assert_called_once_with(
                "s", "w", instance_id=1
            )

            w.get_worker_event_buses_for_session("s")
            mock_registry.get_event_buses_for_session.assert_called_once_with("s")

    def test_worker_class_method_delegates(self, monkeypatch):
        """The Worker._find_all_worker_threads method delegates to the singleton."""
        from tools.workspace.worker import Worker, _WorkerRegistry

        called = False
        original = _WorkerRegistry.get_instance().find_workers_by_name

        def tracking_find(name):
            nonlocal called
            called = True
            return original(name)

        monkeypatch.setattr(
            _WorkerRegistry.get_instance(), "find_workers_by_name", tracking_find
        )

        # Instantiate a minimal Worker (needs enough fields to pass pydantic)
        worker = Worker(
            action="list",
            worker_name="test_find",
            context={},
            session_permissions={},
        )
        worker._find_all_worker_threads("test_find")
        assert called, "_find_all_worker_threads did not delegate"
