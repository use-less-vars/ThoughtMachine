# tools/workspace/worker_registry.py
"""WorkerRegistry — singleton encapsulating the in-process worker thread registry
and per-worker EventBus registry.

Extracted from ``tools/workspace/worker.py`` as part of the registry
refactoring (Pillar 2, Step 4).
"""

from __future__ import annotations

import atexit
import logging
import threading
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)


class WorkerRegistry:
    """Thread-safe singleton registry for worker threads and per-worker EventBuses.

    Provides a central point of access for all worker lifecycle operations
    (register, lookup, unregister) and per-worker EventBus discovery used
    by the WebSocket bridge.
    """

    _instance: WorkerRegistry | None = None
    _instance_lock: threading.Lock = threading.Lock()

    def __init__(self) -> None:
        if WorkerRegistry._instance is not None:
            raise RuntimeError("Use WorkerRegistry.get_instance() instead")
        # (session_id, worker_name) → WorkerThread
        self._worker_registry: dict[tuple[str, str], Any] = {}
        self._registry_lock = threading.Lock()

        # (session_id, worker_name) → per-worker EventBus
        self._worker_event_bus_registry: Dict[Tuple[str, str], Any] = {}
        self._bus_registry_lock = threading.Lock()

        # Register the shutdown handler once
        atexit.register(self.shutdown_workers)

    # -- Singleton -----------------------------------------------------------

    @classmethod
    def get_instance(cls) -> WorkerRegistry:
        """Return the singleton WorkerRegistry instance."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # -- Worker thread registry ----------------------------------------------

    def register_worker(self, session_id: str, worker_name: str, thread: Any) -> None:
        """Register a worker thread under (session_id, worker_name)."""
        key = (session_id or "", worker_name)
        with self._registry_lock:
            self._worker_registry[key] = thread

    def get_worker(self, session_id: str, worker_name: str) -> Any:
        """Get a registered worker thread, or None if not found."""
        key = (session_id or "", worker_name)
        with self._registry_lock:
            return self._worker_registry.get(key)

    def unregister_worker(self, session_id: str, worker_name: str, default: Any = None) -> Any:
        """Unregister and return a worker thread, or *default* if not found."""
        key = (session_id or "", worker_name)
        with self._registry_lock:
            return self._worker_registry.pop(key, default)

    def get_all_workers(self) -> dict[tuple[str, str], Any]:
        """Return a snapshot of all registered worker threads."""
        with self._registry_lock:
            return dict(self._worker_registry)

    def find_workers_by_name(self, worker_name: str) -> list[tuple[str, Any]]:
        """
        Search the entire registry for all entries matching *worker_name*,
        regardless of session_id.

        Returns a list of ``(session_key, thread)`` tuples.
        """
        results: list[tuple[str, Any]] = []
        with self._registry_lock:
            for (sid, wname), thread in list(self._worker_registry.items()):
                if wname == worker_name:
                    results.append((sid, thread))
        return results

    # -- Per-worker EventBus registry ----------------------------------------

    def register_event_bus(self, session_id: str, worker_name: str, event_bus: Any) -> None:
        """Register a worker's per-worker EventBus."""
        key = (session_id or "", worker_name)
        with self._bus_registry_lock:
            self._worker_event_bus_registry[key] = event_bus

    def unregister_event_bus(self, session_id: str, worker_name: str) -> None:
        """Unregister a worker's per-worker EventBus."""
        key = (session_id or "", worker_name)
        with self._bus_registry_lock:
            self._worker_event_bus_registry.pop(key, None)

    def get_event_bus(self, session_id: str, worker_name: str) -> Any:
        """Get a worker's per-worker EventBus, or None if not registered."""
        key = (session_id or "", worker_name)
        with self._bus_registry_lock:
            return self._worker_event_bus_registry.get(key)

    def get_event_buses_for_session(self, session_id: str) -> Dict[str, Any]:
        """
        Return dict of ``{worker_name: EventBus}`` for all registered workers
        in a session.

        Used by late-arriving bridges to discover already-running workers
        whose WORKER_SPAWNED event was published before the bridge subscribed.
        """
        result: Dict[str, Any] = {}
        with self._bus_registry_lock:
            for (sid, wname), bus in self._worker_event_bus_registry.items():
                if sid == (session_id or ""):
                    result[wname] = bus
        return result

    # -- Shutdown ------------------------------------------------------------

    def shutdown_workers(self, timeout: float = 5.0) -> None:
        """
        Gracefully stop all registered worker threads and persist their context.

        Called from an ``atexit`` handler and from the bridge's ``close_session``
        so that partial conversation state is not lost when the process exits or
        a session is closed with active workers.
        """
        with self._registry_lock:
            keys = list(self._worker_registry.keys())
        for key in keys:
            with self._registry_lock:
                thread = self._worker_registry.get(key)
            if thread is None or not thread.is_alive():
                continue
            worker_label = key[1] if isinstance(key, tuple) else str(key)
            logger.info("Shutting down worker '%s' (status=%s)", worker_label, thread.status)
            try:
                thread.stop()
                thread.join(timeout=timeout)
            except Exception:
                logger.exception("Error joining worker '%s' during shutdown", worker_label)
            finally:
                try:
                    thread._save_context()
                except Exception:
                    logger.exception("Error saving context for worker '%s' during shutdown", worker_label)
