# tools/workspace/worker_runtime.py
"""``WorkerRuntimeHandle`` -- the P3 worker-runtime handle (S2: dispatch_query).

What S0 established (§10)
-------------------------
S0 was the **seam, not the transport**. It introduced the object every later
stage talks to -- ``WorkerRuntimeHandle`` -- and made it stand in front of the
*existing* ``WorkerThread`` without moving a single byte: every method was a
direct in-process call to the thread that was handed in. That is still exactly
what ``MODE_THREAD`` does, and it has to stay byte-identical in observable
behaviour to calling the thread directly (§11.1, "no behaviour change").

S2 fills the seam in (§10): under ``MODE_PROCESS`` the same ``dispatch_query``
call becomes a ``dispatch_query`` / ``query_accepted`` / ``reply`` exchange
over the ``AF_UNIX`` socketpair (§4.1, §4.3, §4.4), with
``ProcessWorkerRuntime`` holding the pending map and the outbox described in
§3.3 while a ``WorkerThread`` runs in another OS process. Nothing in this
module's *signatures* changed for that: a method that cannot survive becoming
a frame does not belong on the handle.

What is deliberately absent
---------------------------
*No separate ``receive_reply``.* In thread mode the reply comes back from the
call that asked for it, so a second method would be a stub with nothing to do;
in process mode ``dispatch_query`` waits on the pending future itself (§4.8),
which is why the socket read loop lives in ``ProcessWorkerRuntime`` and not on
this object.
*No verb the frame cannot carry.* The control verbs (``stop`` / ``pause`` /
``resume`` / ``kill``, plus the ``cancel`` ladder) are on the handle because
S3 built the ``control`` frame to carry them (§10): each one is a frame in
``MODE_PROCESS`` and the ``WorkerThread`` method of the same name in
``MODE_THREAD``, so neither mode is a stub of the other.
*No ``seq`` / framing state.* The handle owns none: the emitter, the decoder
and the sequence tracker live in the runtime objects
(``ProcessWorkerRuntime`` supervisor-side, ``worker_runtime_main``
child-side), where the stream actually is.

Mode selection (§10, D11)
-------------------------
One flag, ``WORKER_RUNTIME_MODE`` ∈ ``{thread, process}``, default ``thread``
through S4, read **at spawn time** so a single worker can be moved between
modes without restarting the server (§10). The flag is therefore resolved on
every :meth:`WorkerRuntimeHandle.spawn` call and never cached at import time.

Resolution fails closed: an unrecognised value is refused rather than
silently treated as ``thread``, because "I did not understand the config"
and "run in-process" are very different statements about isolation. Only an
*absent* variable means the default -- an explicitly empty
``WORKER_RUNTIME_MODE=`` is a value, and a wrong one.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from tools.workspace.worker_runtime_protocol import Heartbeat, WorkerState

#: The flag that selects the runtime mode (§10, D11).
WORKER_RUNTIME_MODE_ENV = "WORKER_RUNTIME_MODE"

#: Worker body runs in the supervisor's interpreter. The shipped default.
MODE_THREAD = "thread"

#: Worker body runs in its own OS process (S2: dispatch_query only).
MODE_PROCESS = "process"

#: Exactly what the flag accepts. Nothing else is a mode.
RUNTIME_MODES = (MODE_THREAD, MODE_PROCESS)

#: The worker statuses a cancel is REFUSED for (§5.4) -- and the single source
#: of truth for cancel acceptability across BOTH modes.
#:
#: Two objects consult this constant and neither keeps a copy of the rule: the
#: in-process handle (:meth:`WorkerRuntimeHandle.cancel`) and the runtime child
#: (``worker_runtime_main._handle_cancel``). A cancel is refused iff the worker's
#: status is in this set, and accepted otherwise -- ``ready`` / ``busy`` /
#: ``paused`` all still own a ``_turn_cancel`` event, so there is a turn to
#: interrupt in each of them.
#:
#: ``stopped`` is deliberately absent: ``WorkerThread`` never assigns that
#: status (it survives only in a comment, ``worker_thread.py:1103``), so listing
#: it would describe a state no worker can be in.
CANCEL_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"killed", "detaching", "detached", "error", "completed"}
)

#: Live :class:`WorkerRuntimeHandle` objects, keyed the way the worker registry
#: keys its threads: ``(session_id, worker_name)`` or
#: ``(session_id, worker_name, instance_id)``.
#:
#: EMPTY IN PRODUCTION TODAY. Nothing outside the tests constructs a
#: ``WorkerRuntimeHandle``, so every lookup misses and every caller keeps the
#: behaviour it had before this seam existed. Stage S4 is expected to populate
#: the registry with the handles the supervisor builds; until then the seam must
#: stay behaviour-neutral, which is exactly what "empty" buys.
_RUNTIME_HANDLES: Dict[tuple, "WorkerRuntimeHandle"] = {}

_LOGGER = logging.getLogger(__name__)


def _runtime_handle_key(
    session_id: Optional[str],
    worker_name: str,
    instance_id: Optional[int] = None,
) -> Tuple:
    """The registry key for one worker instance.

    Identity, not a predicate: an instance-scoped lookup is a three-tuple, a
    name-scoped one is a two-tuple, and nothing is inferred about either.
    """
    if instance_id is None:
        return (session_id, worker_name)
    return (session_id, worker_name, instance_id)


def register_runtime_handle(
    handle: "WorkerRuntimeHandle",
    *,
    session_id: Optional[str],
    worker_name: str,
    instance_id: Optional[int] = None,
) -> Tuple:
    """Publish *handle* as the live runtime for one worker; returns its key.

    Called by whoever owns the handle's lifetime (S4). Re-registering the same
    key overwrites: a handle is replaced, never accumulated.
    """
    key = _runtime_handle_key(session_id, worker_name, instance_id)
    _RUNTIME_HANDLES[key] = handle
    return key


def unregister_runtime_handle(
    session_id: Optional[str],
    worker_name: str,
    instance_id: Optional[int] = None,
) -> Optional["WorkerRuntimeHandle"]:
    """Drop the handle for one worker; returns it, or ``None`` if there was none.

    Idempotent on purpose: the owner of a handle may be gone by the time its
    exit path runs, and "already unregistered" is not an error.
    """
    return _RUNTIME_HANDLES.pop(
        _runtime_handle_key(session_id, worker_name, instance_id), None
    )


def runtime_handle_for(
    session_id: Optional[str],
    worker_name: str,
    instance_id: Optional[int] = None,
) -> Optional["WorkerRuntimeHandle"]:
    """The live handle for one worker, or ``None`` when there is none.

    The registry is EMPTY IN PRODUCTION TODAY: nothing outside the tests builds
    a ``WorkerRuntimeHandle`` yet, so this returns ``None`` for every real
    caller and the command path is byte-identical to what it was before the
    seam. Stage S4 is expected to populate the registry.

    Never raises. A miss is an ordinary answer (it means "use the old path"),
    so it is logged at DEBUG and returned as ``None`` -- including for a key
    that cannot even be used as one, because a lookup must never be the thing
    that breaks a supervisor endpoint.
    """
    try:
        key = _runtime_handle_key(session_id, worker_name, instance_id)
        handle = _RUNTIME_HANDLES.get(key)
    except Exception as exc:  # noqa: BLE001 - a lookup may never break the caller
        _LOGGER.debug(
            "runtime handle lookup for worker %r (session %r, instance %r) "
            "failed (%s); treating it as absent",
            worker_name,
            session_id,
            instance_id,
            exc,
        )
        return None
    if handle is None:
        _LOGGER.debug(
            "no runtime handle for worker %r (session %r, instance %r); the "
            "command-file path handles this request",
            worker_name,
            session_id,
            instance_id,
        )
        return None
    return handle


def runtime_mode_report(
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Report the runtime mode in force and how full the seam is.

    ``mode`` is whatever :func:`resolve_runtime_mode` resolves for *environ* --
    the same spawn-time rule, never cached -- so this cannot report a mode a
    spawn would not pick. An unusable flag is reported as ``error`` with
    ``mode`` left ``None`` rather than raised: this helper exists for
    diagnostics, and the fail-closed refusal still happens where it belongs, at
    spawn time.
    """
    mode: Optional[str]
    error: Optional[str]
    try:
        mode = resolve_runtime_mode(environ)
        error = None
    except InvalidRuntimeModeError as exc:
        mode = None
        error = str(exc)
    return {
        "mode": mode,
        "error": error,
        "registered_handles": len(_RUNTIME_HANDLES),
    }



class InvalidRuntimeModeError(ValueError):
    """``WORKER_RUNTIME_MODE`` holds a value this build does not implement."""


def resolve_runtime_mode(environ: Optional[Mapping[str, str]] = None) -> str:
    """Return the runtime mode named by *environ* (default: ``os.environ``).

    Called at spawn time (§10) and never memoised, so flipping the flag
    affects the next spawn without a restart. ``environ`` is injectable so the
    resolution rule can be tested without mutating the process environment.

    Absent -> ``thread``. Anything present that is not exactly
    :data:`MODE_THREAD` or :data:`MODE_PROCESS` -> :class:`InvalidRuntimeModeError`
    (fail closed: no trimming, no case folding, no empty-string-as-default).
    """
    source: Mapping[str, str] = os.environ if environ is None else environ
    raw = source.get(WORKER_RUNTIME_MODE_ENV)
    if raw is None:
        return MODE_THREAD
    if raw in RUNTIME_MODES:
        return raw
    raise InvalidRuntimeModeError(
        f"{WORKER_RUNTIME_MODE_ENV}={raw!r} is not one of {RUNTIME_MODES}; "
        "refusing to guess a runtime mode (unset the variable to get the "
        f"{MODE_THREAD!r} default)"
    )


#: What :meth:`WorkerRuntimeHandle.state` reports when it has neither a cached
#: frame nor a readable ``status.json`` mirror. A status no worker can be in,
#: so "I cannot see this worker" is never mistaken for a real state -- the same
#: reasoning that keeps ``stopped`` out of :data:`CANCEL_TERMINAL_STATUSES`.
UNKNOWN_WORKER_STATUS = "unknown"


def cached_frame_age_seconds(
    frame_at: Optional[float], *, now: Optional[float] = None
) -> Optional[float]:
    """Age in seconds of the last frame received from a worker runtime.

    The supervisor-side half of D1: ONE function, taking a timestamp rather
    than a worker object. ``worker_thread._heartbeat_summary``,
    ``worker_manager._heartbeat_age`` and ``worker_lifecycle._heartbeat_age``
    all need a ``WorkerThread`` -- which the supervisor does not have for a
    worker whose body runs in another OS process.

    *frame_at* is the epoch timestamp :meth:`WorkerRuntimeHandle.on_frame`
    cached: local receipt time, not the child's reported time, so a skewed
    child clock can neither fake freshness nor fake staleness. ``None`` (no
    frame yet) answers ``None``: absence of evidence is not an age, and 0.0
    would claim we just heard from a worker we have never heard from.

    Pure in its arguments; the only impurity is the clock read supplying
    *now*. Clock skew (or a caller's slightly-behind *now*) clamps to 0.0
    rather than going negative.
    """
    if frame_at is None:
        return None
    try:
        reference = time.time() if now is None else float(now)
        return max(0.0, reference - float(frame_at))
    except (TypeError, ValueError):
        return None


class WorkerRuntimeHandle:
    """The supervisor's view of one worker runtime (S2: thread or process).

    Construct one per worker *instance* (D1) around the ``WorkerThread`` that
    already exists -- the handle never constructs or starts a thread itself,
    so ``run()``, the fence, ``_turn_cancel`` and every other in-process
    invariant stay exactly as they are (§3.3, D7).
    """

    def __init__(self, worker: Any, *, mode: str = MODE_THREAD) -> None:
        if mode not in RUNTIME_MODES:
            raise InvalidRuntimeModeError(
                f"mode must be one of {RUNTIME_MODES}, got {mode!r}"
            )
        self._worker = worker
        self._mode = mode
        # Process mode is built lazily: constructing a handle must not spawn an
        # OS process (§7), so the runtime spec and the runtime itself stay
        # ``None`` until the first ``dispatch_query`` asks for them.
        self._spec: Optional[dict] = None
        self._process_runtime: Optional[Any] = None
        # S4: what the last frame from a process-mode worker said, and when we
        # received it (local receipt time). Filled by ``on_frame``, read by
        # ``state`` before it falls back to the ``status.json`` mirror.
        self._last_worker_state: Optional[WorkerState] = None
        self._last_frame_at: Optional[float] = None

    @classmethod
    def spawn(
        cls, worker: Any, *, environ: Optional[Mapping[str, str]] = None
    ) -> "WorkerRuntimeHandle":
        """Build a handle using the mode in force *now* (§10, D11).

        This is the spawn-time seam: the flag is read here and nowhere else,
        so "read at spawn time" is a property of the call graph rather than a
        convention. In S2 ``process`` resolves to a handle that spawns its
        runtime on the first ``dispatch_query``, so spawning a handle still
        drives nothing (§7).
        """
        return cls(worker, mode=resolve_runtime_mode(environ))

    @property
    def mode(self) -> str:
        """The mode this handle was built in."""
        return self._mode

    def dispatch_query(self, query: str, timeout: float = 120.0) -> str:
        """Run *query* and return the reply.

        Thread mode: the reply is the return value of the same in-process
        call, which is also the "receive" half -- ``send_query`` already
        correlates on a fresh ``query_id`` and drains stale replies (D9).

        Process mode: the query becomes a ``dispatch_query`` frame, the reply
        is matched on its ``query_id`` against the ``reply`` frame (§4.8), and
        the wait happens on the pending future inside
        ``ProcessWorkerRuntime``.
        """
        if self._mode == MODE_PROCESS:
            return self._ensure_process_runtime().dispatch_query(
                query, timeout=timeout
            )
        return self._worker.send_query(query, timeout=timeout)

    def _ensure_process_runtime(self) -> Any:
        """Spawn the process runtime on first use, then reuse it (§7).

        Lazy on purpose: a handle is cheap to build and ``spawn`` must not
        drive the worker, so the ``spawn`` plus hello handshake happen here,
        once, at the first call that actually needs a runtime.

        ``epoch`` is taken from the worker's ``_generation`` so the child can
        report which incarnation it believes it is serving in
        ``Hello.epoch_seen`` (§4.4).
        """
        if self._process_runtime is None:
            from tools.workspace.worker_runtime_process import (
                ProcessWorkerRuntime,
                spec_from_worker,
            )

            self._spec = spec_from_worker(
                self._worker, epoch=getattr(self._worker, "_generation", 0)
            )
            runtime = ProcessWorkerRuntime(self._spec)
            runtime.start()
            self._process_runtime = runtime
        return self._process_runtime

    def state(self) -> WorkerState:
        """The worker's observable state, in the §4.4 ``worker_state`` shape.

        Filled from the thread's live attributes rather than from
        ``status.json``: the file is the supervisor's mirror of this state, and
        reading the mirror back would make a stale file indistinguishable from
        a stale worker. ``paused_manually`` has no attribute of its own -- it
        is derived from ``_manual_only_pause`` by design (see
        ``WorkerThread.__init__``).

        Process mode has no such attributes to read here, so it answers in
        two steps: the frame cache (filled by :meth:`on_frame`) first, then the
        ``status.json`` mirror the worker writes -- the same file
        ``worker_thread.py`` itself falls back to. Cache before file because
        the cache is what the worker actually reported; the file is a mirror
        that may lag. Neither available is not an error: it is
        :data:`UNKNOWN_WORKER_STATUS`, a status no worker can be in, so "I
        cannot see this worker" is never mistaken for a real one.
        """
        if self._mode == MODE_PROCESS:
            cached = self._last_worker_state
            if cached is not None:
                return cached
            return self._mirrored_state()
        worker = self._worker
        return WorkerState(
            status=worker.status,
            current_task=worker.current_task,
            error=worker.error,
            paused_manually=bool(worker._manual_only_pause),
        )

    def on_frame(self, frame: Any) -> None:
        """Cache what the last frame told us about the worker (S4, D1).

        The supervisor-side half of the frame seam: whatever arrives from a
        process-mode worker passes through here, and :meth:`state` answers
        from the cache before it falls back to the ``status.json`` mirror.

        Only the two frame kinds that exist are dispatched -- ``WorkerState``
        refreshes the state cache, ``Heartbeat`` refreshes the arrival
        timestamp -- and anything else is ignored: a frame kind this stage
        does not know is not an error, it is simply not ours to interpret.

        ``_last_frame_at`` is *receipt* time, not ``Heartbeat.at``. The two
        processes share a filesystem but not a clock, so trusting the child's
        own timestamp would let a skewed child fake freshness or staleness;
        liveness is measured on the supervisor's clock or not at all.

        Never raises, does no I/O, takes no lock and calls nothing back into
        the worker: this runs on the socket read loop, and a frame that
        surprises us must not be able to take the transport down.
        ``Heartbeat`` has no producer yet -- nothing emits one -- so that
        branch is the cache doing the right thing in advance.
        """
        try:
            if isinstance(frame, WorkerState):
                self._last_worker_state = frame
            elif isinstance(frame, Heartbeat):
                pass
            else:
                return
            self._last_frame_at = time.time()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("worker runtime frame was not cacheable: %s", exc)

    def _mirrored_state(self) -> WorkerState:
        """Read the worker's ``status.json`` mirror as a ``WorkerState``.

        The fallback for a handle that has received no frame yet: the worker
        writes exactly the file ``worker_thread.py`` writes, so it is the one
        description of the worker both sides already agree on. Used only under
        ``MODE_PROCESS``, and only after the frame cache misses --
        :meth:`state` prefers observed frames to a file that may lag.

        Six of the file's keys are dropped for want of a field in
        ``WorkerState`` (``last_heartbeat``, ``session_id``,
        ``current_context_tokens``, ``max_context_tokens``, ``started_at``,
        ``last_query_at``): the frame shape is the contract, and widening it
        to mirror the file would make the two disagree the other way.
        """
        path = self._status_json_path()
        if path is None:
            return WorkerState(UNKNOWN_WORKER_STATUS, None, None, False)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return WorkerState(UNKNOWN_WORKER_STATUS, None, None, False)
        if not isinstance(data, dict):
            return WorkerState(UNKNOWN_WORKER_STATUS, None, None, False)
        status = data.get("runtime_status")
        if not isinstance(status, str) or not status:
            status = UNKNOWN_WORKER_STATUS
        current_task = data.get("current_task")
        if not isinstance(current_task, str):
            current_task = None
        error = data.get("error")
        if not isinstance(error, str):
            error = None
        return WorkerState(
            status=status,
            current_task=current_task,
            error=error,
            paused_manually=bool(data.get("paused_manually")),
        )

    def _status_json_path(self) -> Optional[Path]:
        """Where this worker's status mirror lives, or ``None`` (S4).

        The same resolution ``worker_thread.py`` uses for its own fallback --
        ``<worker_dir>/status.json`` -- read through the private
        ``_worker_dir`` attribute because that is the only handle either side
        has on the directory. Absent (a stand-in worker, a test double) means
        "no mirror", not an error: the caller degrades to
        :data:`UNKNOWN_WORKER_STATUS`.
        """
        worker_dir = getattr(self._worker, "_worker_dir", None)
        if worker_dir is None:
            return None
        try:
            return Path(worker_dir) / "status.json"
        except TypeError:
            return None

    # ------------------------------------------------------------------
    # Control plane (§10)
    # ------------------------------------------------------------------

    def stop(self) -> bool:
        """Park the worker without terminating it (§4.3, §5.1).

        Thread mode: ``WorkerThread.stop`` is an alias for ``detach`` and
        answers ``None``, so the verdict here is ``True`` -- an in-process call
        either returned or raised, and a thread that cannot be parked raises
        rather than declining quietly.

        Process mode: a ``control`` frame, and the verdict is the ack's ``ok``.
        A refusal is an answer and not an exception -- it says the runtime is
        alive and declined.
        """
        if self._mode == MODE_PROCESS:
            return self._ensure_process_runtime().stop()
        self._worker.stop()
        return True

    def pause(self) -> bool:
        """Pause the worker's turn loop (§4.3); see :meth:`stop`."""
        if self._mode == MODE_PROCESS:
            return self._ensure_process_runtime().pause()
        self._worker.pause()
        return True

    def resume(self) -> bool:
        """Resume a paused worker (§4.3).

        Both modes answer ``bool``, and in both it is the worker's own verdict
        rather than the transport's: ``WorkerThread.resume`` refuses a killed
        worker and says so with ``False``, which is exactly what an ``ok=False``
        ack means on the other side.
        """
        if self._mode == MODE_PROCESS:
            return self._ensure_process_runtime().resume()
        return bool(self._worker.resume())

    def kill(self) -> bool:
        """Terminate the worker (§4.3, §5.1) -- the terminating verb.

        Process mode kills the worker first and the runtime process second:
        ``ProcessWorkerRuntime.kill`` sends the frame while the ack channel is
        still up and then closes, so the handle drops its reference to a runtime
        that has already shut itself down -- the same reference :meth:`close`
        would have dropped.
        """
        if self._mode == MODE_PROCESS:
            runtime = self._ensure_process_runtime()
            try:
                return runtime.kill()
            finally:
                self._process_runtime = None
        self._worker.kill()
        return True

    def detach(self) -> None:
        """Park the worker, leaving it revivable (§5.1); returns nothing.

        Thread mode calls ``WorkerThread.detach`` directly, which is where the
        in-process cancellation token is set and the park flags are raised.
        Process mode has no ``detach`` frame of its own: ``stop`` *is* the
        detach on both sides (``WorkerThread.stop`` is an alias for it), so the
        frame is sent and its verdict dropped to keep this method's return value
        mode-independent.
        """
        if self._mode == MODE_PROCESS:
            self.stop()
            return
        self._worker.detach()

    def cancel(self) -> str:
        """Interrupt the in-flight turn, leaving the worker alive (§5.1, §5.4).

        Answers the first rung of the ack ladder, ``"accepted"`` or
        ``"refused"``, in both modes: refusal is a verdict about the worker
        ("nothing is running"), so folding it into ``False`` would throw away
        the half of the answer a caller can act on.

        Thread mode has no ``cancel`` method to call -- ``_turn_cancel`` is
        already the token every tool consults, so setting it is the whole
        signal. A thread already in one of :data:`CANCEL_TERMINAL_STATUSES` is
        refused, and that set is the *same object* the child refuses on
        (``worker_runtime_main._handle_cancel``): both modes must agree on when
        there is nothing to cancel, so the agreement is one shared constant
        rather than two copies of the rule.
        """
        if self._mode == MODE_PROCESS:
            return self._ensure_process_runtime().cancel()
        worker = self._worker
        if str(getattr(worker, "status", "") or "") in CANCEL_TERMINAL_STATUSES:
            return "refused"
        worker._turn_cancel.set()
        return "accepted"

    def close(self, *, grace_ms: int = 5000) -> None:
        """Stop the process runtime, if one was ever spawned.

        A no-op in thread mode, and for a process-mode handle that never
        dispatched: the handle never owns the ``WorkerThread`` (§3.3), so
        there is nothing of its own to stop.
        """
        runtime = self._process_runtime
        self._process_runtime = None
        if runtime is not None:
            runtime.close(grace_ms=grace_ms)

    def __enter__(self) -> "WorkerRuntimeHandle":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
