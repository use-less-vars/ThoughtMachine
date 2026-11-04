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

Two phases, and the boundary between them is one invariant
-----------------------------------------------------------
Phase A (the SCAN) is announcement only. ``tick()`` never kills, cancels, pauses
or interrupts anything: it announces, and it starts an episode in another
thread. A worker with no runtime handle -- thread mode, another process, a
legacy spawn -- is announced and never acted on at all, because there is no
cooperative channel to ask on and acting on an age reading alone would be a
guess about a worker the supervisor cannot see into.

Phase B (the EPISODE) is the cooperative escalation ladder (S7c-3), run
OUT-OF-BAND: one tracked daemon thread per stale episode, so a probe whose
timeout is minutes can never stretch the 30s scan loop.

1. ``ping`` -- ``WORKER_STALE_PROBE_TIMEOUT_S``. An ANSWERED ping means the
   runtime is alive and reading its socket, i.e. ALIVE BUT BUSY: the episode
   ABORTS and takes nothing away. This is the central safety invariant. A busy
   worker is the normal case, and killing one for being quiet would turn a
   heuristic into an outage.
2. only an UNANSWERED ping (``False`` -- never a raise) reaches ``cancel``
   (``WORKER_STALE_CANCEL_TIMEOUT_S``). Cancellation counts as COOPERATED when
   the worker's status stops being a working one inside the window, or when the
   cancel is *refused* (nothing was running to interrupt).
3. a cancel nobody cooperated with reaches ``kill``
   (``WORKER_STALE_KILL_TIMEOUT_S``) through the existing ``handle.kill()``
   path -- the only terminating verb in this module, reachable from exactly one
   function that the scan path cannot call (pinned by the source-level guard in
   ``tests/test_worker_stale_watchdog.py``).

The ladder still refuses the pre-existing interrupting machinery
``PeriodicStaleCheck`` / ``stale_callback``: those interrupt without asking,
and an episode must ASK first (the ping) before it takes anything away.

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

Why the ladder asks before it acts:

* the pre-existing periodic machinery (``PeriodicStaleCheck``) INTERRUPTS. A
  thread that announces and interrupts in the same pass cannot be observed
  before it acts, so this watchdog must not reach it -- and does not.
* an age reading alone is a guess about a worker the supervisor cannot see
  into. ``Ping`` / ``Pong`` (S7c-2) is the worker's own participation in the
  question, and the child answers it from its FRAME PUMP rather than its work
  loop -- so "quiet and busy" and "quiet and wedged" are finally
  distinguishable, and only the second one is acted on.

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
3. For each worker announced for the first time in an episode, START the
   escalation episode in its own daemon thread (``_start_escalation``). The
   tick neither joins it nor reads a verdict: the scan's latency is the scan's
   alone. The verdict is recorded on the watchdog (``last_episode`` /
   ``wait_for_escalation``) and logged.

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
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from agent.config.defaults import (
    WORKER_STALE_ANNOUNCE_AFTER_S,
    WORKER_STALE_CANCEL_TIMEOUT_S,
    WORKER_STALE_KILL_TIMEOUT_S,
    WORKER_STALE_PROBE_TIMEOUT_S,
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

#: Statuses that mean the turn loop is still doing the work. A cancel that does
#: not leave one of these inside its window did not cooperate. The shipped
#: vocabulary is exactly ``detaching / detached / killed / ready / paused / busy /
#: error / completed`` (``worker_thread.py``), and ``busy`` is the ONLY one of
#: them that means "a turn is in flight" -- so this set is the vocabulary's, not
#: a list of words that sounded plausible. A status outside it reads as "not
#: working", which ENDS the ladder early: unknown statuses, future statuses and
#: typos all fail towards taking nothing away, which is the only direction a
#: heuristic is allowed to be wrong in here.
_WORKING_STATUSES = frozenset({"busy"})

#: Statuses that are not an answer at all. Unreadable is treated as STILL
#: WORKING: "I cannot see this worker" is not evidence that it stopped, and the
#: rung above this one (the ping) has already said the runtime is not answering.
#: ``""`` is the sentinel :func:`_worker_status` itself answers with; no worker
#: reports it, so the set stays at the sentinel rather than inventing spellings.
_UNREADABLE_STATUSES = frozenset({""})

#: How often an episode re-reads the worker's state while waiting for a cancel.
_POLL_INTERVAL_S = 0.05


def _prober_for(handle: Any) -> Optional[Callable[..., Any]]:
    """The handle's own ``ping`` seam, or ``None`` when it has none.

    Since S7c-3 the verb is public on ``WorkerRuntimeHandle`` and tri-state, so
    this is a plain attribute read: nothing below the handle is reached from
    here, and in particular the handle's lazily built process runtime is never
    touched -- reading it is how a probe would end up SPAWNING one.

    ``None`` is the whole of "announced only, never acted on": a thread-mode
    handle, and any handle from before this verb existed, has no first rung to
    stand on, so the ladder stops before it takes anything away. A handle that
    has the verb but no live runtime answers ``None`` from it instead, and the
    episode reaches the same verdict one rung later.
    """
    ping = getattr(handle, "ping", None)
    return ping if callable(ping) else None


def _probe(prober: Callable[..., Any], timeout: float) -> Optional[bool]:
    """Ask the probe. ``True``/``False`` is its answer; ``None`` is no question.

    ``None`` comes back when the probe itself says there is nothing to ask (a
    handle that owns no live runtime) -- a different statement from ``False``
    ("something is there and stayed silent"), and the one the episode must not
    act on.

    ``ping`` never raises by contract, but a duck-typed handle is foreign code:
    an exception here is "no answer", which sends the episode down the ladder
    rather than out of it.
    """
    try:
        answer = prober(timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - an unanswerable probe is no answer
        logger.warning(
            "worker stale escalation: probe failed, treating as unanswered (%s)",
            exc,
        )
        return False
    return None if answer is None else bool(answer)


def _worker_status(handle: Any) -> str:
    """The worker's status as a lower-cased string, or ``""`` when unreadable."""
    state = getattr(handle, "state", None)
    if callable(state):
        try:
            read = state()
        except Exception as exc:  # noqa: BLE001 - a reading may never break us
            logger.debug("worker stale escalation: state read failed (%s)", exc)
            read = None
        status = getattr(read, "status", None)
        if status is not None:
            return str(status).strip().lower()
    status = getattr(handle, "status", None)
    return str(status).strip().lower() if status is not None else ""


def _turn_stopped(handle: Any) -> bool:
    """Is the worker's turn demonstrably over? (Unreadable is not "over".)"""
    status = _worker_status(handle)
    return status not in _WORKING_STATUSES and status not in _UNREADABLE_STATUSES

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
        probe_timeout: float = WORKER_STALE_PROBE_TIMEOUT_S,
        cancel_timeout: float = WORKER_STALE_CANCEL_TIMEOUT_S,
        kill_timeout: float = WORKER_STALE_KILL_TIMEOUT_S,
        poll_interval: float = _POLL_INTERVAL_S,
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
        # The three ladder budgets, per instance so a test (or a future policy)
        # can shorten them without touching the shipped constants.
        self._probe_timeout = float(probe_timeout)
        self._cancel_timeout = float(cancel_timeout)
        self._kill_timeout = float(kill_timeout)
        self._poll_interval = float(poll_interval)
        # Live episodes, keyed like ``_announced``, and their last verdicts.
        self._escalations: Dict[Tuple, threading.Thread] = {}
        self._episodes: Dict[Tuple, Dict[str, Any]] = {}
        self._episodes_lock = threading.Lock()

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
        # Phase B: one episode per announcement, in its own thread. The tick
        # neither joins it nor waits for a verdict -- the scan's latency stays
        # the scan's alone, whatever the probe or the cancel window costs.
        self._start_escalation(key, session_id, worker_name, instance_id)

    # -- the escalation ladder (phase B) -------------------------------------

    def _start_escalation(
        self,
        key: Tuple,
        session_id: Any,
        worker_name: Any,
        instance_id: Any,
    ) -> Optional[threading.Thread]:
        """Start one tracked daemon episode for *key*; ``None`` if one is live.

        Called from the scan, so it does nothing that can block: it prunes the
        finished episodes, checks that this key has none alive, and starts a
        thread. A second tick while an episode is still asking its question
        must not open a second one -- two probes racing one answer would make
        the ladder's outcome depend on thread scheduling.
        """
        with self._episodes_lock:
            for dead in [k for k, t in self._escalations.items() if not t.is_alive()]:
                self._escalations.pop(dead, None)
            live = self._escalations.get(key)
            if live is not None and live.is_alive():
                return None
            thread = threading.Thread(
                target=self._escalate,
                args=(key, session_id, worker_name, instance_id),
                name=f"worker-stale-escalation:{worker_name}",
                daemon=True,
            )
            self._escalations[key] = thread
        thread.start()
        return thread

    def _escalate(
        self,
        key: Tuple,
        session_id: Any,
        worker_name: Any,
        instance_id: Any,
    ) -> None:
        """One episode: ping -> (maybe) cancel -> (maybe) kill. Never raises.

        The ORDER is the safety property. ``steps`` records the rungs actually
        reached, so a test can assert on the ladder's shape rather than on its
        side effects alone.
        """
        steps: List[str] = []
        outcome = "no_handle"
        try:
            handle = self._resolve_handle(session_id, worker_name, instance_id)
            prober = _prober_for(handle) if handle is not None else None
            if handle is None:
                outcome = "no_handle"
            elif prober is None:
                # LOAD-BEARING: no probe seam means no live runtime to ask, so
                # the episode ends here -- announced and left strictly alone.
                # Never "fix" this branch by reaching for the handle's process
                # runtime: the only runtime a process-mode handle has is the one
                # ``_ensure_process_runtime`` builds, so probing a worker that
                # has not dispatched yet would spawn the process it is asking
                # about. The verdict is the same when the verb exists and answers
                # ``None`` (handled below), which is the ordinary production
                # shape of this case.
                outcome = "no_runtime"
            else:
                answer = _probe(prober, self._probe_timeout)
                if answer is None:
                    # Nothing to ask (no live runtime): same verdict as above,
                    # and still no rung taken -- a thread-mode worker is not a
                    # wedged one, and must never be cancelled on that reading.
                    outcome = "no_runtime"
                else:
                    steps.append("ping")
                    if answer:
                        # THE invariant: alive but busy. Announce, take nothing.
                        outcome = "answered"
                    elif self._stop_event.is_set():
                        # A stopped watchdog takes no further action. The check
                        # sits on the ACTION boundary, not merely in the scan
                        # loop: a stop that lands after the episode was spawned
                        # still must not cancel anything.
                        outcome = "stopped"
                    else:
                        steps.append("cancel")
                        if self._cancel_cooperated(handle):
                            outcome = "cancelled"
                        elif self._stop_event.is_set():
                            # The wait is interrupted by a stop, and "not stopped
                            # yet" is not a licence to kill: this is the last
                            # gate between a shutdown and a termination, and it
                            # is checked immediately before the rung that acts.
                            outcome = "stopped"
                        else:
                            steps.append("kill")
                            outcome = self._kill(handle)
        except Exception as exc:  # noqa: BLE001 - an episode may never escape
            logger.warning(
                "worker stale escalation for %r failed (%s)", worker_name, exc
            )
            outcome = "failed"
        self._record_episode(
            key,
            {
                "kind": "escalation",
                "worker_name": worker_name,
                "session_id": session_id,
                "instance_id": instance_id,
                "steps": steps,
                "outcome": outcome,
                "probe_timeout_s": self._probe_timeout,
                "cancel_timeout_s": self._cancel_timeout,
                "kill_timeout_s": self._kill_timeout,
            },
        )

    def _resolve_handle(
        self, session_id: Any, worker_name: Any, instance_id: Any
    ) -> Optional[Any]:
        """The live handle for this worker, or ``None`` -- never raises."""
        try:
            return self._handle_resolver(session_id, worker_name, instance_id)
        except Exception as exc:  # noqa: BLE001 - a lookup may never break one
            logger.warning(
                "worker stale escalation: handle lookup failed for %r (%s)",
                worker_name,
                exc,
            )
            return None

    def _cancel_cooperated(self, handle: Any) -> bool:
        """Cancel, then judge cooperation within ``WORKER_STALE_CANCEL_TIMEOUT_S``.

        A ``refused`` verdict is cooperation: it is the worker saying nothing
        was running to interrupt, which is the outcome the rung was asking for.
        An ``accepted`` verdict is only a statement about the ACK, not about the
        turn -- the turn is what the wait watches, so acceptance alone never
        counts as cooperation.
        """
        cancel = getattr(handle, "cancel", None)
        if not callable(cancel):
            return False
        try:
            verdict = cancel()
        except Exception as exc:  # noqa: BLE001 - a refusal is not an escape
            logger.warning("worker stale escalation: cancel failed (%s)", exc)
            return False
        if str(verdict).strip().lower() == "refused":
            return True
        return self._wait_for_turn_stop(handle)

    def _wait_for_turn_stop(self, handle: Any) -> bool:
        """Poll the worker's state until its turn is over or the window closes.

        The poll waits on the stop event rather than on a bare ``sleep``, so a
        ``stop()`` interrupts the window instead of being ridden out for the
        whole budget. The answer stays a plain ``False``: a stop says nothing
        about the turn, so it cannot claim the turn ended -- the caller's own
        boundary check is what keeps the kill from happening.
        """
        deadline = time.monotonic() + self._cancel_timeout
        while True:
            if _turn_stopped(handle):
                return True
            if time.monotonic() >= deadline:
                return False
            if self._stop_event.wait(self._poll_interval):
                return False

    def _kill(self, handle: Any) -> str:
        """Terminate through ``handle.kill()`` -- the last rung. Returns a verdict.

        The call itself runs on a throwaway thread and is waited for at most
        ``WORKER_STALE_KILL_TIMEOUT_S``, so a kill whose transport wedges cannot
        hold the episode open forever: the frame has been issued either way, and
        the episode reports ``kill_timeout`` instead of hanging.
        """
        kill = getattr(handle, "kill", None)
        if not callable(kill):
            return "no_kill_seam"
        result: Dict[str, Any] = {}

        def run() -> None:
            try:
                kill()
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                result["error"] = exc

        thread = threading.Thread(
            target=run, name="worker-stale-kill", daemon=True
        )
        thread.start()
        thread.join(self._kill_timeout)
        if thread.is_alive():
            return "kill_timeout"
        if "error" in result:
            logger.warning(
                "worker stale escalation: kill failed (%s)", result["error"]
            )
            return "kill_failed"
        return "killed"

    def _record_episode(self, key: Tuple, record: Dict[str, Any]) -> None:
        """Store (and log) one episode's verdict."""
        with self._episodes_lock:
            self._episodes[key] = record
        logger.warning(
            "worker stale escalation: %s",
            ", ".join(f"{name}={value!r}" for name, value in record.items()),
        )

    def last_episode(self, key: Tuple) -> Optional[Dict[str, Any]]:
        """The verdict of the most recent episode for *key*, or ``None``."""
        with self._episodes_lock:
            record = self._episodes.get(key)
            return dict(record) if record is not None else None

    def wait_for_escalation(
        self, key: Tuple, timeout: float = 5.0
    ) -> Optional[Dict[str, Any]]:
        """Join the key's episode (bounded) and answer its verdict.

        The bounded-join seam a test (or a shutdown path) uses instead of
        polling ``last_episode``: the episode is out-of-band, so "when is it
        done" is a property of the thread, not of the tick that started it.
        """
        with self._episodes_lock:
            thread = self._escalations.get(key)
        if thread is not None:
            thread.join(timeout=timeout)
        return self.last_episode(key)

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
    # Join the in-flight episodes BEFORE answering. An episode is out-of-band
    # and is still able to call cancel()/kill() after the scan loop stops, so a
    # caller that was told "stopped" while one of those posts was in flight
    # would have been told something untrue. ``stop()`` has already set the
    # event every action boundary and every wait checks, so each join is bounded
    # by the episode noticing rather than by its ladder budget.
    episode_timeout = watchdog._kill_timeout + watchdog._poll_interval
    with watchdog._episodes_lock:
        episodes = list(watchdog._escalations.values())
    for episode in episodes:
        episode.join(timeout=episode_timeout)
    watchdog.join(timeout=timeout)
    return True
