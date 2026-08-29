# tools/workspace/worker_manager.py
"""WorkerManager - trusted orchestration facade over WorkerRegistry.

Phase: worker lifecycle trust (W2). Introduces a single, testable entry
point for worker lifecycle orchestration that worker.py (and its tests)
can later delegate to without importing the full worker runtime.

Design constraints
------------------
* MUST NOT import ``tools.workspace.worker`` (import cycle: worker.py
  imports worker_registry / worker_timeout / worker_container and will
  import this module later). All thread interactions happen through the
  registry and duck-typed thread objects (``stop()``, ``join()``,
  ``is_alive()``, ``status``).
* Semantics mirror worker.py's existing behaviour exactly:

  - The per-session spawn cap counts only LIVE threads (``is_alive()``);
    dead (completed/stopped/errored) registry entries do not count.
  - Force-replacement (``force_replace`` / ``stop_all_by_name`` and the
    ``force=True`` path of ``request_worker``) stops and REMOVES the stale
    instance from the registry BEFORE any cap check, so a forced
    replacement is never blocked by the cap.
  - Stops are always cooperative (``thread.stop()`` + bounded join via
    ``wait_for_worker_exit``); never ``Thread.kill``.
  - ``stop_worker`` / ``stop_all`` stop threads but leave their registry
    entries in place (terminal state visible, same as worker.py's
    cooperative-stop contract); ``cleanup_dead`` sweeps non-alive entries.

Orchestration ownership
-----------------------
``request_worker`` is the ONLY entry point for delivering a query to a
session's worker: it owns the reuse/spawn/cap/force decisions. No other
module touches the registry for request/stop/reuse decisions — the main
agent asks ``WorkerManager`` only. Deliveries go through
``deliver_query_and_block`` (tools.workspace.worker_query): the call BLOCKS
until the worker replies or is cleaned up, a busy worker raises
``WorkerBusyError`` (no queueing), and a full cap raises
``WorkerCapExceeded`` loudly (never a silent drop). Control actions
(``pause_worker`` / ``resume_worker`` / ``reset_worker``) are thin wrappers
over zero-arg thread methods; a thread without the method raises
``WorkerControlError`` (current worker.py threads expose ``pause``/``resume``
but NO ``reset`` — that action is unsupported until W3 adds it).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from agent.config.defaults import (
    HEARTBEAT_INTERVAL_S,
    HEARTBEAT_STALE_AFTER_S,
    MAX_WORKERS_PER_SESSION,
)
from tools.workspace.worker_query import (
    WorkerBusyError,  # noqa: F401  (re-exported: callers catch one name)
    deliver_query_and_block,
)
from tools.workspace.worker_registry import WorkerRegistry
from tools.workspace.worker_timeout import wait_for_worker_exit

logger = logging.getLogger(__name__)


class WorkerCapExceeded(Exception):
    """Raised when a session's live-worker cap blocks a requested spawn.

    W2 refuses loudly instead of silently dropping the request or queueing
    it: the caller must wait for a worker to free up, stop one, or pass
    ``force=True``.
    """

    def __init__(
        self,
        session_id: str,
        cap: int,
        live: int,
        worker_name: Optional[str] = None,
    ) -> None:
        self.session_id = session_id
        self.cap = cap
        self.live = live
        self.worker_name = worker_name
        target = f" for worker {worker_name!r}" if worker_name else ""
        super().__init__(
            f"Worker spawn cap exceeded for session {session_id or ''!r}{target}: "
            f"{live} live worker(s) at cap {cap}; refusing to spawn — W2 does "
            f"not queue queries (retry when a worker frees, or use force=True)."
        )


class WorkerControlError(Exception):
    """Raised when a control action (pause/resume/reset) cannot be performed.

    E.g. a thread without a ``reset()`` method; the manager reports this
    loudly rather than pretending the action ran.
    """


class PeriodicStaleCheck(threading.Thread):
    """periodic stale-check even when no events arrive; stale worker -> interrupt in-flight blocking calls (dispatch task 4)

    Daemon background thread: every ``interval`` seconds it walks the
    registry and compares each live worker's last heartbeat against
    ``stale_after``. A worker whose heartbeat is older than the threshold is
    interrupted through its own ``_terminate_tracked_executions()`` seam
    (falling back to ``_execution_tracker.terminate_all(...)``), so its
    in-flight blocking executions are killed even when no event ever
    arrives. Workers without a readable heartbeat are treated as fresh and
    never interrupted. Not auto-started by ``WorkerManager``; call
    ``start()`` (or ``WorkerManager.start_stale_check``) and ``stop()`` /
    ``join()`` to shut it down.
    """

    def __init__(
        self,
        registry: Any,
        interval: float = HEARTBEAT_INTERVAL_S,
        stale_after: float = HEARTBEAT_STALE_AFTER_S,
        container_manager: Any = None,
    ) -> None:
        super().__init__(name="periodic-stale-check", daemon=True)
        self._registry = registry
        self._interval = float(interval)
        self._stale_after = float(stale_after)
        self._container_manager = container_manager
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self._interval):
            try:
                self._check_once()
            except Exception as exc:
                logger.warning("periodic stale check failed: %s", exc)

    def _check_once(self) -> None:
        for key, thread in list(self._registry.get_all_workers().items()):
            if not self._is_alive(thread):
                continue
            age = self._heartbeat_age(thread)
            if age is None or age <= self._stale_after:
                continue
            try:
                self._interrupt(thread)
            except Exception as exc:
                logger.warning(
                    "stale-check interrupt failed for worker '%s': %s",
                    getattr(thread, "worker_name", key),
                    exc,
                )

    @staticmethod
    def _is_alive(thread: Any) -> bool:
        """Defensive liveness check (mirrors ``WorkerManager._is_alive``)."""
        alive = getattr(thread, "is_alive", None)
        if callable(alive):
            try:
                return bool(alive())
            except Exception:
                return False
        return True

    def _heartbeat_age(self, thread: Any) -> Optional[float]:
        """Age (seconds) of the worker's last heartbeat, or None if unknown.

        Reads the ISO-8601 ``last_heartbeat`` attribute (set by WorkerThread
        per event / run() start / idle & pause ticks), falling back to the
        monotonic-float ``_last_heartbeat_monotonic``. Missing or unparseable
        heartbeats yield None - the worker is treated as fresh and never
        interrupted (defensive: fakes without heartbeat attrs are skipped).
        """
        hb = getattr(thread, "last_heartbeat", None)
        if hb:
            try:
                parsed = datetime.fromisoformat(str(hb))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - parsed).total_seconds()
                if age >= 0:
                    return age
            except (TypeError, ValueError):
                pass
        mono = getattr(thread, "_last_heartbeat_monotonic", None)
        if isinstance(mono, (int, float)) and not isinstance(mono, bool) and mono > 0:
            return max(0.0, time.monotonic() - float(mono))
        return None

    def _interrupt(self, thread: Any) -> None:
        """Interrupt the worker's in-flight blocking executions.

        Prefers the thread's own ``_terminate_tracked_executions()`` entry
        (the W3b1 interrupt seam; also covers the WLM flag-on path, since the
        flag-on supervisor SHARES the thread's tracker), falling back to
        calling ``_execution_tracker.terminate_all(...)`` directly.
        """
        method = getattr(thread, "_terminate_tracked_executions", None)
        if callable(method):
            method()
            return
        tracker = getattr(thread, "_execution_tracker", None)
        if tracker is not None and hasattr(tracker, "terminate_all"):
            worker_name = getattr(thread, "worker_name", None) or "worker"
            cm = getattr(thread, "_container_manager", None)
            if cm is None:
                cm = self._container_manager
            tracker.terminate_all(
                worker_name,
                cm,
                None,
                session_id=getattr(thread, "session_id", None),
            )

    def stop(self) -> None:
        """Signal the checker loop to exit after the current tick."""
        self._stop_event.set()

    def join(self, timeout: Optional[float] = None) -> None:
        """Join the checker thread (bounded by *timeout* when given)."""
        super().join(timeout=timeout)


class WorkerManager:
    """Thread-safe orchestration facade over a ``WorkerRegistry``-like store.

    The registry may be any object duck-typing the ``WorkerRegistry`` public
    surface (``register_worker`` / ``unregister_worker`` / ``get_worker`` /
    ``get_all_workers`` / ``find_workers_by_name`` / ``instance_label``), so
    tests can inject a dict-backed fake and worker.py keeps using the
    singleton without any coupling here.

    ``request_worker`` is the orchestration entry point: it reuses a live
    context-matching worker (``context_preference``), enforces the spawn cap
    (``WorkerCapExceeded``), spawns+registers new threads via the
    ``spawner`` seam, and delivers every query through
    ``deliver_query_and_block`` (blocking, never early-returning).
    """

    def __init__(
        self,
        registry: Any = None,
        default_max_workers: int = MAX_WORKERS_PER_SESSION,
    ) -> None:
        self._registry: Any = (
            registry if registry is not None else WorkerRegistry.get_instance()
        )
        self._default_max_workers: int = default_max_workers

    # -- registry delegation ------------------------------------------------

    def register_worker(
        self,
        session_id: str,
        worker_name: str,
        thread: Any,
        instance_id: int = 1,
    ) -> None:
        """Register a worker thread under ``(session_id, worker_name, instance_id)``."""
        self._registry.register_worker(
            session_id or "", worker_name, thread, instance_id=instance_id
        )

    def unregister_worker(
        self,
        session_id: str,
        worker_name: str,
        instance_id: int = 1,
        default: Any = None,
    ) -> Any:
        """Unregister and return the thread, or *default* if absent."""
        return self._registry.unregister_worker(
            session_id or "", worker_name, instance_id=instance_id, default=default
        )

    def get_worker(
        self,
        session_id: str,
        worker_name: str,
        instance_id: int = 1,
    ) -> Any:
        """Return the registered thread for the key, or None."""
        return self._registry.get_worker(
            session_id or "", worker_name, instance_id=instance_id
        )

    def find_workers_by_name(self, worker_name: str) -> List[Tuple[str, Any]]:
        """All ``(session_key, thread)`` entries matching *worker_name* across sessions."""
        return list(self._registry.find_workers_by_name(worker_name))

    # -- session views ------------------------------------------------------

    def list_workers(self, session_id: str) -> List[Any]:
        """All registered threads (alive or dead) for a session."""
        session_key = session_id or ""
        return [
            thread
            for key, thread in self._registry.get_all_workers().items()
            if key[0] == session_key
        ]

    def alive_workers(self, session_id: str) -> List[Any]:
        """Live registered threads for a session (``is_alive()`` True)."""
        session_key = session_id or ""
        return [
            thread
            for key, thread in self._registry.get_all_workers().items()
            if key[0] == session_key and self._is_alive(thread)
        ]

    def live_count(self, session_id: str) -> int:
        """Number of LIVE registered threads for a session (the cap metric)."""
        return len(self.alive_workers(session_id))

    # -- per-session spawn budget -------------------------------------------

    def effective_max_workers(self, max_workers: Optional[int] = None) -> int:
        """Resolve a session's spawn cap.

        Mirrors ``Worker._effective_max_workers``: non-int or non-positive
        values fall back to the safe default ``MAX_WORKERS_PER_SESSION``.
        """
        cap = self._default_max_workers if max_workers is None else max_workers
        try:
            cap = int(cap)
        except (TypeError, ValueError):
            return MAX_WORKERS_PER_SESSION
        if cap <= 0:
            return MAX_WORKERS_PER_SESSION
        return cap

    def spawn_capacity(
        self, session_id: str, max_workers: Optional[int] = None
    ) -> int:
        """Remaining spawn slots for a session: ``cap - live``, floored at 0."""
        cap = self.effective_max_workers(max_workers)
        live = self.live_count(session_id)
        return max(0, cap - live)

    def can_spawn(self, session_id: str, max_workers: Optional[int] = None) -> bool:
        """True when a new spawn for the session would not exceed the cap."""
        return self.spawn_capacity(session_id, max_workers=max_workers) > 0

    # -- lifecycle operations -----------------------------------------------

    def stop_worker(
        self, session_id: str, worker_name: str, instance_id: int = 1
    ) -> Dict[str, Any]:
        """Cooperate-stop one worker; the registry entry stays (terminal state).

        Returns ``{"found": ..., "stopped": ..., "worker_name": ...,
        "instance_id": ..., "status": ...}``. ``stopped`` is True when the
        thread exited within the join budget.
        """
        thread = self.get_worker(session_id, worker_name, instance_id)
        if thread is None:
            return {
                "found": False,
                "stopped": False,
                "worker_name": worker_name,
                "instance_id": instance_id,
            }
        exited = self._stop_thread(thread, worker_name)
        return {
            "found": True,
            "stopped": exited,
            "worker_name": worker_name,
            "instance_id": instance_id,
            "status": getattr(thread, "status", None),
        }

    def stop_all(self, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Cooperative-stop every LIVE registered thread (session-filtered).

        Dead threads are skipped untouched; registry entries are left in
        place (use ``cleanup_dead`` to sweep them).
        """
        results: List[Dict[str, Any]] = []
        for key, thread in list(self._registry.get_all_workers().items()):
            if session_id is not None and key[0] != (session_id or ""):
                continue
            if not self._is_alive(thread):
                continue
            exited = self._stop_thread(thread, key[1])
            results.append(
                {
                    "session_id": key[0],
                    "worker_name": key[1],
                    "instance_id": key[2] if len(key) >= 3 else 1,
                    "stopped": exited,
                    "status": getattr(thread, "status", None),
                }
            )
        return results

    def cleanup_dead(self, session_id: Optional[str] = None) -> int:
        """Unregister every non-alive entry (session-filtered); keep live ones.

        Returns the number of entries removed.
        """
        removed = 0
        for key, thread in list(self._registry.get_all_workers().items()):
            if session_id is not None and key[0] != (session_id or ""):
                continue
            if self._is_alive(thread):
                continue
            self._registry.unregister_worker(
                key[0],
                key[1],
                key[2] if len(key) >= 3 else 1,
                default=None,
            )
            removed += 1
        return removed

    def stop_all_by_name(
        self, worker_name: str, session_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Stop AND unregister every instance of *worker_name*.

        This is the force-replacement pre-step: stale instances are removed
        from the registry before any cap check, exactly mirroring the
        ``force=True`` semantics in worker.py, so a subsequent spawn of the
        same name never counts the replaced instances against the cap.
        """
        results: List[Dict[str, Any]] = []
        for key, thread in list(self._registry.get_all_workers().items()):
            if key[1] != worker_name:
                continue
            if session_id is not None and key[0] != (session_id or ""):
                continue
            self._stop_thread(thread, worker_name)
            self._registry.unregister_worker(
                key[0],
                key[1],
                key[2] if len(key) >= 3 else 1,
                default=None,
            )
            results.append(
                {
                    "session_id": key[0],
                    "status": getattr(thread, "status", None),
                }
            )
        return results

    def force_replace(
        self,
        worker_name: str,
        spawner: Optional[Callable[[], Any]] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Force-replace a worker: stop+unregister stale instances, then spawn.

        *spawner* is a zero-arg callable returning the NEW thread (the same
        seam worker.py's spawn path will use later). The replacement is
        registered under ``(session_id or "", worker_name, iid)`` where *iid*
        comes from ``thread.instance_id`` when present. The spawn cap is
        deliberately NOT consulted: stale entries were removed first.
        """
        stopped = self.stop_all_by_name(worker_name, session_id=session_id)
        spawned_thread = None
        if spawner is not None:
            spawned_thread = spawner()
            iid = int(getattr(spawned_thread, "instance_id", 1) or 1)
            self.register_worker(
                session_id or "", worker_name, spawned_thread, instance_id=iid
            )
        return {
            "worker_name": worker_name,
            "stopped": stopped,
            "spawned": spawned_thread is not None,
            "thread": spawned_thread,
        }

    def request_worker(
        self,
        session_id: str,
        query: Any,
        context_preference: Optional[Dict[str, Any]] = None,
        force: bool = False,
        spawner: Optional[Callable[[], Any]] = None,
        max_workers: Optional[int] = None,
        timeout: Optional[float] = None,
        grace: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Deliver a query to a session worker, reusing or spawning as needed.

        *force=True* clears the session (stop + unregister every entry)
        before any reuse/cap logic, mirroring the stale-instance semantics
        of ``force_replace``. When *context_preference* names a worker (a
        dict with ``worker_name`` and optionally ``context_tag``), the first
        LIVE registered thread matching the name/tag is reused; otherwise a
        new worker is spawned through the zero-arg *spawner* callable. A
        full live cap raises ``WorkerCapExceeded``; a missing spawner raises
        ``ValueError``. Delivery BLOCKS via ``deliver_query_and_block`` and
        the returned envelope carries a ``delivery`` marker.
        """
        force_replaced = False
        if force:
            self._clear_session(session_id)
            force_replaced = True

        if isinstance(context_preference, dict) and context_preference.get(
            "worker_name"
        ):
            pref_name = context_preference["worker_name"]
            pref_tag = context_preference.get("context_tag")
            for thread in self.alive_workers(session_id):
                if thread.worker_name != pref_name:
                    continue
                if pref_tag is not None and getattr(thread, "context_tag", None) != pref_tag:
                    continue
                envelope = deliver_query_and_block(
                    thread, query, timeout=timeout, grace=grace, worker_name=pref_name
                )
                envelope["delivery"] = {
                    "reused": True,
                    "spawned": False,
                    "force_replaced": force_replaced,
                }
                return envelope

        cap = self.effective_max_workers(max_workers)
        live = self.live_count(session_id)
        if live >= cap:
            raise WorkerCapExceeded(session_id, cap, live)
        if not callable(spawner):
            raise ValueError("request_worker requires a callable spawner to spawn")
        thread = spawner()
        name = getattr(thread, "worker_name", None) or "worker"
        iid = int(getattr(thread, "instance_id", 1) or 1)
        self.register_worker(session_id, name, thread, instance_id=iid)
        envelope = deliver_query_and_block(
            thread, query, timeout=timeout, grace=grace, worker_name=name
        )
        envelope["delivery"] = {
            "reused": False,
            "spawned": True,
            "force_replaced": force_replaced,
        }
        return envelope

    def pause_worker(
        self, session_id: str, worker_name: str, instance_id: int = 1
    ) -> Dict[str, Any]:
        """Pause a session worker (thin wrapper, raises WorkerControlError)."""
        return self._control_worker("pause", session_id, worker_name, instance_id)

    def resume_worker(
        self, session_id: str, worker_name: str, instance_id: int = 1
    ) -> Dict[str, Any]:
        """Resume a session worker (thin wrapper, raises WorkerControlError)."""
        return self._control_worker("resume", session_id, worker_name, instance_id)

    def reset_worker(
        self, session_id: str, worker_name: str, instance_id: int = 1
    ) -> Dict[str, Any]:
        """Reset a session worker (thin wrapper, raises WorkerControlError)."""
        return self._control_worker("reset", session_id, worker_name, instance_id)

    def start_stale_check(
        self,
        interval: Optional[float] = None,
        stale_after: Optional[float] = None,
    ) -> PeriodicStaleCheck:
        """Start a PeriodicStaleCheck over this manager's registry and return it.

        The checker is NOT auto-started by the manager; callers start it
        explicitly (e.g. once per session) and own its ``stop()``/``join()``.
        Defaults come from the ``PeriodicStaleCheck`` constructor
        (``HEARTBEAT_INTERVAL_S`` / ``HEARTBEAT_STALE_AFTER_S``).
        """
        checker = PeriodicStaleCheck(
            self._registry,
            interval=HEARTBEAT_INTERVAL_S if interval is None else interval,
            stale_after=HEARTBEAT_STALE_AFTER_S if stale_after is None else stale_after,
        )
        checker.start()
        return checker

    def _control_worker(
        self, action: str, session_id: str, worker_name: str, instance_id: int = 1
    ) -> Dict[str, Any]:
        """Run a zero-arg control method on a registered thread."""
        thread = self.get_worker(session_id, worker_name, instance_id=instance_id)
        if thread is None:
            return {
                "found": False,
                "action": action,
                "worker_name": worker_name,
                "instance_id": instance_id,
            }
        method = getattr(thread, action, None)
        if not callable(method):
            raise WorkerControlError(
                "{}({}) has no callable '{}' method (unsupported until W3?)".format(
                    worker_name, instance_id, action
                )
            )
        method()
        return {
            "found": True,
            "action": action,
            "worker_name": worker_name,
            "instance_id": instance_id,
            "status": getattr(thread, "status", None),
        }

    def _clear_session(self, session_id: str) -> None:
        """Stop and unregister every registered thread for a session."""
        self.stop_all(session_id)
        key = session_id or ""
        for (sid, _name, _iid), _thread in list(
            self._registry.get_all_workers().items()
        ):
            if sid == key:
                self.unregister_worker(sid, _name, instance_id=_iid)

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _is_alive(thread: Any) -> bool:
        """Defensive liveness check (same fallback as worker.py: treat an
        object without a working ``is_alive`` as alive so it is never
        spuriously swept as dead)."""
        alive = getattr(thread, "is_alive", None)
        if callable(alive):
            try:
                return bool(alive())
            except Exception:
                return False
        return True

    def _stop_thread(self, thread: Any, worker_name: str) -> bool:
        """Cooperative stop + bounded join (never ``Thread.kill``)."""
        try:
            thread.stop()
        except Exception:
            pass
        return wait_for_worker_exit(thread, worker_name)


# -- module-level singleton accessor ------------------------------------------

_MANAGER_LOCK = threading.Lock()
_MANAGER_INSTANCE: Optional[WorkerManager] = None


def get_manager() -> WorkerManager:
    """Return the process-wide WorkerManager singleton (lazily created)."""
    global _MANAGER_INSTANCE
    if _MANAGER_INSTANCE is None:
        with _MANAGER_LOCK:
            if _MANAGER_INSTANCE is None:
                _MANAGER_INSTANCE = WorkerManager()
    return _MANAGER_INSTANCE
