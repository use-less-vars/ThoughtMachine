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
from agent.logging.lifecycle import log_worker_event


class WorkerRegistry:
    """Thread-safe singleton registry for worker threads and per-worker EventBuses.

    Provides a central point of access for all worker lifecycle operations
    (register, lookup, unregister) and per-worker EventBus discovery used
    by the WebSocket bridge.

    Registry keys are 3-tuples ``(session_id, worker_name, instance_id)`` so
    that multiple live instances of the same worker name can coexist.
    Legacy 2-tuple keys (``(session_id, worker_name)``) are tolerated on
    read paths for backward compatibility.
    """

    _instance: WorkerRegistry | None = None
    _instance_lock: threading.Lock = threading.Lock()

    def __init__(self) -> None:
        if WorkerRegistry._instance is not None:
            raise RuntimeError("Use WorkerRegistry.get_instance() instead")
        # (session_id, worker_name, instance_id) → WorkerThread
        self._worker_registry: dict[tuple[str, str, int], Any] = {}
        self._registry_lock = threading.Lock()

        # (session_id, worker_name, instance_id) → per-worker EventBus
        self._worker_event_bus_registry: Dict[Tuple[str, str, int], Any] = {}
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

    # -- Labels --------------------------------------------------------------

    @staticmethod
    def instance_label(worker_name: str, instance_id: int) -> str:
        """Human-readable label for a worker instance.

        Instance 1 is the legacy/default instance and keeps the bare worker
        name; higher instances are suffixed with ``#N`` (e.g. ``architect#2``).
        """
        if instance_id is None or instance_id == 1:
            return worker_name
        return f"{worker_name}#{instance_id}"

    # -- Worker thread registry ----------------------------------------------

    def register_worker(self, session_id: str, worker_name: str, thread: Any,
                        instance_id: int = 1) -> None:
        """Register a worker thread under (session_id, worker_name, instance_id)."""
        key = (session_id or "", worker_name, instance_id)
        with self._registry_lock:
            self._worker_registry[key] = thread
        log_worker_event(worker_name, 'spawned', session_id=session_id or '')

    def get_worker(self, session_id: str, worker_name: str,
                   instance_id: int = 1) -> Any:
        """Get a registered worker thread, or None if not found."""
        key = (session_id or "", worker_name, instance_id)
        with self._registry_lock:
            return self._worker_registry.get(key)

    def unregister_worker(self, session_id: str, worker_name: str,
                          instance_id: int = 1, default: Any = None) -> Any:
        """Unregister and return a worker thread, or *default* if not found."""
        key = (session_id or "", worker_name, instance_id)
        with self._registry_lock:
            existed = key in self._worker_registry
            popped = self._worker_registry.pop(key, default)
        if existed:
            log_worker_event(worker_name, 'stopped', session_id=session_id or '')
        return popped

    def get_all_workers(self) -> dict[tuple[str, str, int], Any]:
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
            for key, thread in list(self._worker_registry.items()):
                wname = key[1]
                if wname == worker_name:
                    results.append((key[0], thread))
        return results

    # -- Per-worker EventBus registry ----------------------------------------

    def register_event_bus(self, session_id: str, worker_name: str, event_bus: Any,
                           instance_id: int = 1) -> None:
        """Register a worker's per-worker EventBus."""
        key = (session_id or "", worker_name, instance_id)
        with self._bus_registry_lock:
            self._worker_event_bus_registry[key] = event_bus

    def unregister_event_bus(self, session_id: str, worker_name: str,
                             instance_id: int = 1) -> None:
        """Unregister a worker's per-worker EventBus."""
        key = (session_id or "", worker_name, instance_id)
        with self._bus_registry_lock:
            self._worker_event_bus_registry.pop(key, None)

    def get_event_bus(self, session_id: str, worker_name: str,
                      instance_id: int = 1) -> Any:
        """Get a worker's per-worker EventBus, or None if not registered."""
        key = (session_id or "", worker_name, instance_id)
        with self._bus_registry_lock:
            return self._worker_event_bus_registry.get(key)

    def get_event_buses_for_session(self, session_id: str) -> Dict[str, Any]:
        """
        Return dict of ``{instance_label: EventBus}`` for all registered workers
        in a session.

        Keys use the worker instance label (``name`` for instance 1,
        ``name#N`` for higher instances) so multiple instances of the same
        worker name in one session are all discoverable. Used by
        late-arriving bridges to discover already-running workers whose
        WORKER_SPAWNED event was published before the bridge subscribed.
        """
        result: Dict[str, Any] = {}
        with self._bus_registry_lock:
            for key, bus in self._worker_event_bus_registry.items():
                sid, wname = key[0], key[1]
                iid = key[2] if len(key) >= 3 else 1
                if sid == (session_id or ""):
                    result[self.instance_label(wname, iid)] = bus
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
            worker_label = self._key_label(key)
            logger.info("Shutting down worker '%s' (status=%s)", worker_label, thread.status)
            try:
                thread.stop()
                thread.join(timeout=timeout)
            except Exception:
                logger.exception("Error joining worker '%s' during shutdown", worker_label)
            finally:
                try:
                    # F3: compact summarized history before persisting so a
                    # shutdown right after SummarizeTool does not persist ~2x.
                    if thread._worker_ctx is not None:
                        thread._worker_ctx.compact_after_summary()
                    thread._save_context()
                except Exception:
                    logger.exception("Error saving context for worker '%s' during shutdown", worker_label)

    @staticmethod
    def _key_label(key) -> str:
        """Human-readable label for a registry key (2- or 3-tuple tolerant)."""
        if isinstance(key, tuple):
            if len(key) >= 3:
                return WorkerRegistry.instance_label(key[1], key[2])
            return key[1]
        return str(key)
