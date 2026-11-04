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


class _AllSessionsSentinel:
    """Type of the :data:`ALL_SESSIONS` sentinel (never instantiated by callers)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "ALL_SESSIONS"


ALL_SESSIONS = _AllSessionsSentinel()
"""Scope marker for :meth:`WorkerRegistry.shutdown_workers`.

Means "every session" and belongs to the process-exit (``atexit``) path only.
Session-scoped callers must pass a session id so that closing one session
cannot stop another session's worker threads.
"""


# ── Epoch fencing (S7a) ────────────────────────────────────────────────────
# Every registry mutation can be tagged with the caller's epoch: the owning
# ``WorkerThread._generation``, a monotonic counter allocated under the
# cross-process spawn lock, so a later incarnation always carries a strictly
# greater epoch than any earlier one -- including across processes.
#
# Why not fold the epoch into the stored value (a record type instead of a
# bare WorkerThread / EventBus)?  Because these dicts' OBJECT IDENTITY is
# aliased by other modules -- ``worker_thread.py`` binds
# ``_worker_registry`` / ``_worker_event_bus_registry`` directly and
# ``check_system.py`` does too -- and a record refactor would force every one
# of those readers to change.  Two parallel epoch maps keep the stored values
# exactly what they are today.
#
# Why keep an epoch record after an unregister succeeds?  Deleting it would
# re-open the very race this closes: a stale late writer (a killed
# incarnation's ``finally:`` block) would find no record, treat the key as
# fresh/unversioned and pop or resurrect the SUCCESSOR's entry.  Retaining the
# newest epoch is precisely what makes every later stale mutation refuse.
#
# Growth is bounded and deliberately unreclaimed: the keys are
# (session id, worker name, instance id), instances per name are few, and the
# payload is one small int, so leaking a handful of ints per worker name per
# process life is cheaper than a sweep that would re-open the race.


def _coerce_epoch(epoch: Any) -> int | None:
    """Return *epoch* only if it is genuinely an int, else ``None``.

    Callers pass ``getattr(thread, '_generation', None)``; duck-typed and
    mocked objects can yield a non-int (a ``MagicMock``, a string), which
    would raise on the int comparison inside the registry.  Treating such a
    value as absent degrades to the legacy unversioned path -- fail open on
    TYPE, never on ORDER: a real int is still compared strictly.
    """
    return epoch if isinstance(epoch, int) and not isinstance(epoch, bool) else None


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
        # (session_id, worker_name, instance_id) → last accepted register epoch.
        # Parallel to _worker_registry, guarded by the SAME _registry_lock (see
        # the epoch-fencing comment above the class).
        self._worker_epochs: dict[tuple[str, str, int], int] = {}

        # (session_id, worker_name, instance_id) → per-worker EventBus
        self._worker_event_bus_registry: Dict[Tuple[str, str, int], Any] = {}
        self._bus_registry_lock = threading.Lock()
        # (session_id, worker_name, instance_id) → last accepted register epoch.
        # Parallel to _worker_event_bus_registry, guarded by _bus_registry_lock.
        self._bus_epochs: dict[tuple[str, str, int], int] = {}

        # Register the shutdown handler once. The whole-registry sweep is the
        # process-exit semantics, so the handler passes the ALL_SESSIONS
        # sentinel explicitly instead of relying on the default.
        atexit.register(self.shutdown_workers, session_id=ALL_SESSIONS)

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
                        instance_id: int = 1, *, epoch: int | None = None) -> Any:
        """Register a worker thread under (session_id, worker_name, instance_id).

        With an int *epoch* the write is compare-and-set: it is refused
        (returns ``False``, registry untouched) when a strictly greater epoch
        is already recorded for this key, so a stale incarnation can never
        displace its successor.  An equal epoch is an idempotent overwrite;
        on success the epoch is recorded and ``True`` is returned.
        ``epoch=None`` is the legacy unversioned path: it always writes and
        returns ``None`` as before, leaving the epoch map untouched.  A
        non-int *epoch* is treated as absent.
        """
        epoch = _coerce_epoch(epoch)
        key = (session_id or "", worker_name, instance_id)
        with self._registry_lock:
            if epoch is not None:
                stored = self._worker_epochs.get(key)
                if stored is not None and stored > epoch:
                    return False
                self._worker_epochs[key] = epoch
            self._worker_registry[key] = thread
        log_worker_event(worker_name, 'spawned', session_id=session_id or '')
        return True if epoch is not None else None

    def get_worker(self, session_id: str, worker_name: str,
                   instance_id: int = 1) -> Any:
        """Get a registered worker thread, or None if not found."""
        key = (session_id or "", worker_name, instance_id)
        with self._registry_lock:
            return self._worker_registry.get(key)

    def unregister_worker(self, session_id: str, worker_name: str,
                          instance_id: int = 1, default: Any = None,
                          *, epoch: int | None = None) -> Any:
        """Unregister and return a worker thread, or *default* if not found.

        With an int *epoch* the pop is compare-and-set on ``stored <= epoch``:
        it happens when the recorded epoch is less than or equal to *epoch* (or
        when no record exists) and then returns ``True`` instead of the popped
        thread; it refuses (``False``, entry left in place) only when the
        recorded epoch is STRICTLY GREATER than *epoch*.  On a successful pop
        the recorded epoch is raised to *epoch* and never lowered.
        ``epoch=None`` is the legacy unversioned path (returns the popped
        value / *default*, epoch map untouched).  A non-int *epoch* is treated
        as absent.
        """
        epoch = _coerce_epoch(epoch)
        key = (session_id or "", worker_name, instance_id)
        if epoch is None:
            with self._registry_lock:
                existed = key in self._worker_registry
                popped = self._worker_registry.pop(key, default)
            if existed:
                log_worker_event(worker_name, 'stopped', session_id=session_id or '')
            return popped
        with self._registry_lock:
            stored = self._worker_epochs.get(key)
            # The record only ever increases, so ``stored > epoch`` is precisely
            # the case where a newer incarnation owns the key; a missing record
            # means the key was never version-registered, and popping it is the
            # pre-S7a behaviour.
            if stored is not None and stored > epoch:
                return False
            existed = key in self._worker_registry
            self._worker_registry.pop(key, None)
            # Retain AND raise the record to this caller's epoch (we only get
            # here when ``stored is None or stored <= epoch``, so this is
            # ``max(stored, epoch)``): the newest epoch survives the pop, so no
            # later stale register or unregister can touch this key.
            self._worker_epochs[key] = epoch
        if existed:
            log_worker_event(worker_name, 'stopped', session_id=session_id or '')
        return True

    def get_all_workers(self) -> dict[tuple[str, str, int], Any]:
        """Return a snapshot of all registered worker threads."""
        with self._registry_lock:
            return dict(self._worker_registry)

    def find_workers_by_name(self, worker_name: str,
                             session_id: str | None = None) -> list[tuple[str, Any]]:
        """
        Search the registry for all entries matching *worker_name*.

        By default the search spans the entire registry, regardless of
        session_id.  When *session_id* is given, only entries belonging to
        that session are returned (session-scoped lookup).

        Returns a list of ``(session_key, thread)`` tuples.
        """
        results: list[tuple[str, Any]] = []
        with self._registry_lock:
            for key, thread in list(self._worker_registry.items()):
                wname = key[1]
                if wname != worker_name:
                    continue
                if session_id is not None and key[0] != session_id:
                    continue
                results.append((key[0], thread))
        return results

    # -- Per-worker EventBus registry ----------------------------------------

    def register_event_bus(self, session_id: str, worker_name: str, event_bus: Any,
                           instance_id: int = 1, *, epoch: int | None = None) -> Any:
        """Register a worker's per-worker EventBus.

        With an int *epoch* the write is compare-and-set against the bus epoch
        map: refused (``False``, nothing written) when a strictly greater epoch
        is recorded for this key, applied and ``True`` otherwise (equal epoch
        is an idempotent overwrite).  ``epoch=None`` is the legacy unversioned
        path: always writes, returns ``None``, epoch map untouched.  A non-int
        *epoch* is treated as absent.
        """
        epoch = _coerce_epoch(epoch)
        key = (session_id or "", worker_name, instance_id)
        with self._bus_registry_lock:
            if epoch is not None:
                stored = self._bus_epochs.get(key)
                if stored is not None and stored > epoch:
                    return False
                self._bus_epochs[key] = epoch
            self._worker_event_bus_registry[key] = event_bus
        return True if epoch is not None else None

    def unregister_event_bus(self, session_id: str, worker_name: str,
                             instance_id: int = 1, *,
                             epoch: int | None = None) -> Any:
        """Unregister a worker's per-worker EventBus.

        With an int *epoch* the pop is compare-and-set on ``stored <= epoch``:
        performed when the recorded epoch is less than or equal to *epoch* (or
        when no record exists) and then returns ``True``; it refuses (``False``,
        bus left registered) only when the recorded epoch is STRICTLY GREATER
        than *epoch*.  On a successful pop the recorded epoch is raised to
        *epoch* and never lowered.
        ``epoch=None`` is the legacy unversioned path (always
        pops, returns ``None``).  A non-int *epoch* is treated as absent.
        """
        epoch = _coerce_epoch(epoch)
        key = (session_id or "", worker_name, instance_id)
        with self._bus_registry_lock:
            if epoch is None:
                self._worker_event_bus_registry.pop(key, None)
                return None
            stored = self._bus_epochs.get(key)
            # The record only ever increases, so ``stored > epoch`` is precisely
            # the case where a newer incarnation owns the key; a missing record
            # means the key was never version-registered, and popping it is the
            # pre-S7a behaviour.
            if stored is not None and stored > epoch:
                return False
            self._worker_event_bus_registry.pop(key, None)
            # Retain AND raise the record to this caller's epoch (we only get
            # here when ``stored is None or stored <= epoch``, so this is
            # ``max(stored, epoch)``): the newest epoch survives the pop, so no
            # later stale register or unregister can touch this key.
            self._bus_epochs[key] = epoch
        return True

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

    def shutdown_workers(self, timeout: float = 5.0, *, session_id) -> None:
        """
        Gracefully stop registered worker threads and persist their context.

        Session-scoped: only keys whose session component matches *session_id*
        are swept. ``session_id`` is keyword-only and required: per-session
        callers MUST pass their id so one session cannot stop another's worker
        threads. The :data:`ALL_SESSIONS` sentinel sweeps the whole registry
        and is the process-exit path only. ``None`` is rejected.

        Called from an ``atexit`` handler and from the bridge's ``close_session``
        so that partial conversation state is not lost when the process exits or
        a session is closed with active workers.
        """
        if session_id is None:
            raise ValueError(
                "shutdown_workers requires an explicit session_id; pass "
                "ALL_SESSIONS only for process-wide shutdown"
            )
        with self._registry_lock:
            if session_id is ALL_SESSIONS:
                keys = list(self._worker_registry.keys())
            else:
                keys = [k for k in self._worker_registry.keys()
                        if isinstance(k, tuple) and k and (k[0] or "") == session_id]
        for key in keys:
            with self._registry_lock:
                thread = self._worker_registry.get(key)
            if thread is None or not thread.is_alive():
                continue
            worker_label = self._key_label(key)
            logger.info("Shutting down worker '%s' (status=%s)", worker_label, thread.status)
            try:
                # PHASE 2.1a: session/process shutdown wants the instance GONE,
                # not parked — stop() only detaches now, and a detached thread
                # would never exit the bounded join below. kill() with a stop()
                # fallback for lightweight thread doubles that predate the split.
                _killer = getattr(thread, "kill", None) or thread.stop
                _killer()
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


def stored_epoch_for(session_id: Any, worker_name: Any) -> int | None:
    """Highest epoch ever recorded for one ``(session_id, worker_name)`` pair.

    The fence is per PAIR, not per exact key. Epochs increase strictly for a
    worker name inside a session, so the newest incarnation of that worker is
    the maximum over every key the pair has used -- the 3-tuple
    ``(session, name, instance)`` of each instance, and the legacy 2-tuple
    ``(session, name)`` if anything ever wrote one. Reading only the exact key
    the caller happens to hold would miss a successor that took a DIFFERENT
    instance id, which is exactly the case this answers: instance ids restart,
    so a successor can reuse instance 1 while the predecessor still holds a
    handle for it.

    Read-only, and never raises: the lock is held for the scan and never
    across a dispatch, an unreadable registry or an entry that cannot even be
    compared is skipped, and ``None`` means "no evidence" -- not "epoch 0".
    The session component is normalized with ``session_id or ""`` to match the
    key form used by ``register_worker`` / ``unregister_worker`` /
    ``register_event_bus`` / ``unregister_event_bus``, so a caller holding
    ``session_id=None`` still finds the evidence.
    """
    try:
        registry = WorkerRegistry.get_instance()
        sid = session_id or ""
        with registry._registry_lock:
            entries = list(registry._worker_epochs.items())
        best: int | None = None
        for key, stored in entries:
            if not isinstance(key, tuple) or len(key) < 2:
                continue
            if key[0] != sid or key[1] != worker_name:
                continue
            epoch = _coerce_epoch(stored)
            if epoch is None:
                continue
            if best is None or epoch > best:
                best = epoch
        return best
    except Exception as exc:  # noqa: BLE001 - a read may never break the caller
        logger.debug(
            "stored_epoch_for(%r, %r) could not read the epoch map (%s); "
            "treating it as no evidence",
            session_id,
            worker_name,
            exc,
        )
        return None
