# tools/workspace/worker_stale_watchdog.py
"""``WorkerStaleWatchdog`` -- the periodic, ANNOUNCE-ONLY liveness scan (phase A).

The bug this closes
-------------------
``WorkerManager.start_stale_check()`` had no production caller: ``grep -rn
"start_stale_check" --include="*.py" .`` returned its definition
(``worker_manager.py:804``) and tests, nothing else. The stale machinery
therefore existed and never ran, and ``WorkerLifecycleObserver
.check_stale_transitions()`` was only ever reached from the *event* path
(``worker_lifecycle.py:126``) -- so a worker that stopped emitting events
stopped being examined at exactly the moment it needed to be.

Phase A is announcement only
----------------------------
This watchdog never kills, cancels, pauses or interrupts anything. It holds no
reference to ``terminate_all`` / ``terminate_executions`` /
``_terminate_tracked_executions`` / ``cancel`` / ``kill`` and has no branch that
could acquire one: the announce-only property is a property of the call graph,
pinned by ``tests/test_worker_stale_watchdog.py`` (a busy worker whose every
termination method records a call, plus a source-level guard on this file).

The observer pass needs one guarantee the verb guard cannot give it:
``check_stale_transitions()`` PUBLISHES a ``WORKER_TIMEOUT`` event, and
``EventBus.publish`` is synchronous. Published on the shared
``global_event_bus``, that event reaches ``worker_thread``'s lifecycle observer,
whose ``stale_callback`` is ``_on_worker_stale`` -- ``terminate_executions()`` /
``terminate_all()`` on the worker. So the observer built here is given a PRIVATE
bus (``EventBus()``, created for it and subscribed by nobody else): its
announcements are published into a bus nothing consumes, and the terminating
chain stays unreachable from this thread. ``stale_callback=None`` closes the
direct edge; the private bus closes the published one.

Two reasons, both about order of operations:

* the pre-existing periodic machinery (``PeriodicStaleCheck``) INTERRUPTS. A
  thread that announces and interrupts in the same pass cannot be observed
  before it acts, so this watchdog must not reach it -- and does not.
* the cooperative ladder (cancel -> pause -> kill) needs a liveness answer the
  worker must participate in, and no such frame exists yet (``Ping`` is
  schema-only, unanswered). Until it does, acting on an age reading is a guess
  about a worker the supervisor cannot see into.

What a tick does
----------------
1. ``observer.check_stale_transitions()`` -- the scan that until now only ran
   when an event arrived. The observer is INJECTED and defaults to a dedicated
   announce-only instance (``stale_callback=None`` on a private bus). It is
   deliberately NOT ``worker_thread._get_worker_lifecycle_observer()``, whose
   ``stale_callback`` terminates the hung worker's in-flight executions;
   phase A must not reach that callback.
2. Frame age per registered worker, through the existing pure helpers in
   ``worker_runtime``: ``heartbeat_age_seconds`` (what the worker said) first,
   ``frame_arrival_age_seconds`` (when the transport delivered) as the backstop.
   ``None`` -- no frame ever seen -- never announces: absence of evidence is not
   an age, and 0.0 would claim we just heard from a worker we never heard from.

Each worker is announced at most once per stale episode; a worker whose age
falls back under the threshold is forgotten, so a later stale episode is
announced again. The bookkeeping is pruned to the keys present in the registry
on every tick, so a worker that disappears is forgotten at once -- if the same
``(session_id, worker_name, instance_id)`` comes back stale it is announced
again, and no key outlives the worker it named.

Import cycles
-------------
Module-level imports here are only ``agent.config.defaults``,
``agent.events`` (for the private ``EventBus``) and
``tools.workspace.worker_runtime``. The registry, the observer and the handle
resolver are resolved through *callables* that import lazily, which is what lets
``worker_manager.ensure_stale_monitor()`` reach this module without
``worker_manager`` importing ``worker_thread`` (``worker_thread`` imports
``worker_manager``; the reverse edge would close the cycle).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from agent.config.defaults import (
    WORKER_STALE_ANNOUNCE_AFTER_S,
    WORKER_STALE_WATCHDOG_INTERVAL_S,
)
from agent.events import EventBus
from tools.workspace.worker_runtime import (
    frame_arrival_age_seconds,
    heartbeat_age_seconds,
    runtime_handle_for,
)

logger = logging.getLogger(__name__)

#: Supplies the worker store the scan walks (``get_all_workers()``).
RegistryProvider = Callable[[], Any]
#: Supplies the lifecycle observer to drive; ``None`` disables the observer pass.
ObserverProvider = Callable[[], Any]
#: Resolves ``(session_id, worker_name, instance_id)`` to a runtime handle.
HandleResolver = Callable[[Any, str, Any], Any]
#: Receives every announcement (never raises into the loop).
AnnounceCallback = Callable[[Dict[str, Any]], None]

#: The dedicated announce-only observer, created once per process.
_ANNOUNCE_OBSERVER: Optional[Any] = None
_ANNOUNCE_OBSERVER_LOCK = threading.Lock()


def _default_registry_provider() -> Any:
    """The process-wide :class:`WorkerRegistry` (lazy: keeps the graph acyclic)."""
    from tools.workspace.worker_registry import WorkerRegistry

    return WorkerRegistry.get_instance()


def _default_observer_provider() -> Optional[Any]:
    """A dedicated ANNOUNCE-ONLY lifecycle observer, or ``None`` if unavailable.

    ``stale_callback=None`` covers the direct edge: the observer's hung action
    then calls nothing. The PRIVATE ``event_bus`` covers the published one: the
    hung action still publishes ``WORKER_TIMEOUT``, and on the shared global bus
    that publication would reach ``worker_thread``'s observer, whose callback
    terminates the worker's in-flight executions. A bus created here and
    subscribed by nobody has no such consumer, so driving this scan from a
    background thread stays an announcement. Created once and reused -- a fresh
    observer per tick would re-subscribe every interval and lose the heartbeat
    history that makes the scan meaningful.
    """
    global _ANNOUNCE_OBSERVER
    if _ANNOUNCE_OBSERVER is not None:
        return _ANNOUNCE_OBSERVER
    with _ANNOUNCE_OBSERVER_LOCK:
        if _ANNOUNCE_OBSERVER is None:
            try:
                from tools.workspace.worker_lifecycle import WorkerLifecycleObserver

                observer = WorkerLifecycleObserver(
                    event_bus=EventBus(), stale_callback=None
                )
                observer.ensure_subscribed()
            except Exception as exc:  # noqa: BLE001 - an observer is optional
                logger.warning(
                    "worker stale watchdog: no lifecycle observer available (%s)",
                    exc,
                )
                return None
            _ANNOUNCE_OBSERVER = observer
    return _ANNOUNCE_OBSERVER


class WorkerStaleWatchdog(threading.Thread):
    """Daemon thread: periodic announce-only stale scan over registered workers.

    Never auto-started by importing this module; call :func:`ensure_watchdog`
    (process-wide, idempotent) or construct one and ``start()`` it. Shut down
    with :func:`stop_watchdog` or the instance's own ``stop()`` / ``join()``.
    """

    def __init__(
        self,
        *,
        interval: float = WORKER_STALE_WATCHDOG_INTERVAL_S,
        announce_after: float = WORKER_STALE_ANNOUNCE_AFTER_S,
        registry_provider: Optional[RegistryProvider] = None,
        observer_provider: Optional[ObserverProvider] = None,
        handle_resolver: Optional[HandleResolver] = None,
        announce_callback: Optional[AnnounceCallback] = None,
    ) -> None:
        super().__init__(name="worker-stale-watchdog", daemon=True)
        self._interval = float(interval)
        self._announce_after = float(announce_after)
        self._registry_provider = registry_provider or _default_registry_provider
        self._observer_provider = (
            observer_provider
            if observer_provider is not None
            else _default_observer_provider
        )
        self._handle_resolver = handle_resolver or runtime_handle_for
        self._announce_callback = announce_callback
        self._stop_event = threading.Event()
        self._announced: Set[Tuple] = set()
        self._announced_lock = threading.Lock()

    def run(self) -> None:  # noqa: D401 - threading entry point
        """Scan every ``interval`` seconds until stopped (never raises out)."""
        while not self._stop_event.wait(self._interval):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - one bad tick is not fatal
                logger.warning("worker stale watchdog tick failed: %s", exc)

    def tick(self) -> List[Dict[str, Any]]:
        """One announce-only pass; returns the announcements it made.

        Public and side-effect-shaped on purpose: the background loop and the
        tests drive the very same code path, so nothing about "announce exactly
        once" can hold in only one of them.
        """
        announcements: List[Dict[str, Any]] = []
        self._scan_observer(announcements)
        self._scan_frames(announcements)
        return announcements

    # -- the two scans ------------------------------------------------------

    def _scan_observer(self, sink: List[Dict[str, Any]]) -> None:
        """Drive ``check_stale_transitions()`` -- the scan events used to own."""
        try:
            observer = self._observer_provider()
        except Exception as exc:  # noqa: BLE001 - provider is foreign code
            logger.warning(
                "worker stale watchdog: observer provider failed (%s)", exc
            )
            return
        if observer is None:
            return
        try:
            newly_stale = observer.check_stale_transitions()
        except Exception as exc:  # noqa: BLE001 - keep the frame pass running
            logger.warning(
                "worker stale watchdog: observer scan failed (%s)", exc
            )
            return
        if not newly_stale:
            return
        self._announce(
            {
                "kind": "observer_stale",
                "newly_stale": int(newly_stale),
                "announce_after_s": self._announce_after,
            },
            sink,
        )

    def _scan_frames(self, sink: List[Dict[str, Any]]) -> None:
        """Announce each worker whose cached runtime frame is too old.

        Every tick also prunes the bookkeeping to the keys that are present AND
        still stale right now, so a worker that disappears (or whose age becomes
        unknown again) is forgotten at once and is announced again if it ever
        returns -- and the set cannot grow with workers that no longer exist.
        """
        try:
            registry = self._registry_provider()
            workers = registry.get_all_workers()
        except Exception as exc:  # noqa: BLE001 - provider is foreign code
            logger.warning(
                "worker stale watchdog: worker store unavailable (%s)", exc
            )
            return
        # Keys still present AND still stale: the only ones worth remembering.
        keep: Set[Tuple] = set()
        for key, thread in list(dict(workers).items()):
            session_id, worker_name, instance_id = self._key_parts(key, thread)
            age = self._frame_age(session_id, worker_name, instance_id)
            if age is None:
                # No frame evidence: never announced, and no episode to keep.
                continue
            if age > self._announce_after:
                keep.add(key)
                self._announce_frame(
                    key, session_id, worker_name, instance_id, age, sink
                )
                continue
            # Fresh (or fresh again): the next stale episode is a new one.
            self._forget(key)
        # A worker that vanished is forgotten now, not never.
        self._retain(keep)

    def _announce_frame(
        self,
        key: Tuple,
        session_id: Any,
        worker_name: Any,
        instance_id: Any,
        age: float,
        sink: List[Dict[str, Any]],
    ) -> None:
        if not self._first_announce(key):
            return
        self._announce(
            {
                "kind": "frame_stale",
                "worker_name": worker_name,
                "session_id": session_id,
                "instance_id": instance_id,
                "age_seconds": round(float(age), 1),
                "announce_after_s": self._announce_after,
            },
            sink,
        )

    # -- readings -----------------------------------------------------------

    def _frame_age(
        self, session_id: Any, worker_name: Any, instance_id: Any
    ) -> Optional[float]:
        """Age of the worker's last runtime frame, or ``None`` when unknown.

        ``None`` covers both "no handle" and "handle that has seen no frame" --
        the two cases in which this watchdog has nothing to reason from, and
        which must therefore never announce.
        """
        try:
            handle = self._handle_resolver(session_id, worker_name, instance_id)
        except Exception as exc:  # noqa: BLE001 - a lookup may never break a tick
            logger.debug(
                "worker stale watchdog: handle lookup failed for %r (%s)",
                worker_name,
                exc,
            )
            return None
        if handle is None:
            return None
        age = heartbeat_age_seconds(getattr(handle, "_last_heartbeat_at", None))
        if age is not None:
            return age
        return frame_arrival_age_seconds(getattr(handle, "_last_received_at", None))

    @staticmethod
    def _key_parts(key: Any, thread: Any) -> Tuple[Any, Any, Any]:
        """``(session_id, worker_name, instance_id)`` from a registry entry."""
        session_id: Any = None
        worker_name: Any = None
        instance_id: Any = None
        if isinstance(key, tuple):
            if len(key) >= 3:
                session_id, worker_name, instance_id = key[0], key[1], key[2]
            elif len(key) == 2:
                session_id, worker_name = key[0], key[1]
        else:
            worker_name = key
        if worker_name is None:
            worker_name = getattr(thread, "worker_name", None)
        if session_id is None:
            session_id = getattr(thread, "session_id", None)
        if instance_id is None:
            instance_id = getattr(thread, "instance_id", None)
        return session_id, worker_name, instance_id

    # -- announcement bookkeeping -------------------------------------------

    def _first_announce(self, key: Tuple) -> bool:
        with self._announced_lock:
            if key in self._announced:
                return False
            self._announced.add(key)
            return True

    def _forget(self, key: Tuple) -> None:
        with self._announced_lock:
            self._announced.discard(key)

    def _retain(self, keep: Set[Tuple]) -> None:
        """Drop every remembered key that is not in *keep*.

        One set operation per tick: the bookkeeping mirrors "announced and still
        stale and still registered", so it can neither leak a departed worker
        nor silence one that comes back.
        """
        with self._announced_lock:
            self._announced &= keep

    def _announce(self, info: Dict[str, Any], sink: List[Dict[str, Any]]) -> None:
        """Record one announcement: sink, log line, caller callback.

        Announcement is the entire output of this class, so all three channels
        are here rather than at each call site -- and the callback is guarded
        because a subscriber's exception may never stop the scan.
        """
        sink.append(info)
        logger.warning(
            "worker stale (announce-only, no action taken): %s",
            ", ".join(f"{name}={value!r}" for name, value in info.items()),
        )
        if self._announce_callback is not None:
            try:
                self._announce_callback(info)
            except Exception as exc:  # noqa: BLE001 - subscriber code
                logger.warning(
                    "worker stale watchdog: announce callback failed (%s)", exc
                )

    # -- lifecycle ----------------------------------------------------------

    def stop(self) -> None:
        """Signal the scan loop to exit after the current tick."""
        self._stop_event.set()

    def join(self, timeout: Optional[float] = None) -> None:
        """Join the watchdog thread (bounded by *timeout* when given)."""
        super().join(timeout=timeout)


# -- process-wide singleton ---------------------------------------------------

_WATCHDOG_LOCK = threading.Lock()
_WATCHDOG: Optional[WorkerStaleWatchdog] = None


def ensure_watchdog(**kwargs: Any) -> WorkerStaleWatchdog:
    """Start the process-wide watchdog once and return it (idempotent).

    Idempotence is what makes this safe to call from more than one seam: the
    web_ui lifespan start and ``WorkerManager.get_manager()`` both want "the
    watchdog is running", neither wants a second thread. An existing *live*
    watchdog is returned untouched (so a second caller's arguments are ignored);
    a dead one is replaced.
    """
    global _WATCHDOG
    with _WATCHDOG_LOCK:
        existing = _WATCHDOG
        if existing is not None and existing.is_alive():
            return existing
        watchdog = WorkerStaleWatchdog(**kwargs)
        watchdog.start()
        _WATCHDOG = watchdog
        return watchdog


def get_watchdog() -> Optional[WorkerStaleWatchdog]:
    """The process-wide watchdog, or ``None`` when it was never started."""
    return _WATCHDOG


def stop_watchdog(timeout: Optional[float] = None) -> bool:
    """Stop the process-wide watchdog; ``True`` when there was one.

    Idempotent: a second call (or a shutdown path racing a ``stop_watchdog``)
    answers ``False`` instead of raising, the same shape
    ``unregister_runtime_handle`` uses for "already gone".
    """
    global _WATCHDOG
    with _WATCHDOG_LOCK:
        watchdog = _WATCHDOG
        _WATCHDOG = None
    if watchdog is None:
        return False
    watchdog.stop()
    watchdog.join(timeout=timeout)
    return True
