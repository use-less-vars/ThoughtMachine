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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from tools.workspace.worker_runtime_protocol import Event, Heartbeat, WorkerState

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

    *frame_at* is one of the two epoch timestamps
    :meth:`WorkerRuntimeHandle.on_frame` caches -- ``Heartbeat.at`` parsed to
    UTC epoch seconds (:func:`heartbeat_age_seconds`) or the supervisor's own
    receipt time (:func:`frame_arrival_age_seconds`). ``None`` (nothing cached
    yet) answers ``None``: absence of evidence is not an age, and 0.0 would
    claim we just heard from a worker we have never heard from.

    Pure in its arguments; the only impurity is the clock read supplying
    *now*. Clock skew (or a caller's slightly-behind *now*) clamps to 0.0
    rather than going negative.

    Kept as the one primitive the two wrappers below share: the arithmetic is
    identical and only the QUESTION differs, so the difference belongs in the
    names and their docstrings rather than in two copies of ``max(0.0, ...)``.
    """
    if frame_at is None:
        return None
    try:
        reference = time.time() if now is None else float(now)
        return max(0.0, reference - float(frame_at))
    except (TypeError, ValueError):
        return None


def heartbeat_age_seconds(
    at: Optional[float], *, now: Optional[float] = None
) -> Optional[float]:
    """How long since the worker SAID it ticked (D1, primary reading).

    *at* is ``Heartbeat.at`` parsed to UTC epoch seconds -- the child's own
    statement of when it last ran a tick -- so this answers, for a worker whose
    thread the supervisor cannot see, the question
    ``worker_thread._heartbeat_summary`` / ``worker_manager._heartbeat_age`` /
    ``worker_lifecycle._heartbeat_age`` answer for a ``WorkerThread`` in
    process.

    Why this one is primary, and why the other one is kept rather than
    collapsed into it: the supervisor and the runtime child are two processes
    on the same host, so ``datetime.now(timezone.utc)`` is the same clock on
    both sides to within microseconds and ``at`` can be believed. The one
    failure it has is a clock STEP (NTP) landing between the child writing
    ``at`` and the supervisor comparing it, which makes a live worker read as
    stale or a stale one read as fresh -- and that is precisely the case
    :func:`frame_arrival_age_seconds` cannot get wrong, because receipt time
    never leaves this machine's own clock. One reading, one blind spot; the
    pair covers each other, so both stay.

    ``None`` in, ``None`` out: no heartbeat yet is not an age of zero.
    """
    return cached_frame_age_seconds(at, now=now)


def frame_arrival_age_seconds(
    received_at: Optional[float], *, now: Optional[float] = None
) -> Optional[float]:
    """How long since frames ARRIVED (D1, NTP-jump backstop).

    *received_at* is the supervisor's own receipt time -- the clock no child
    can influence, and the one that keeps ticking when the child's does not.
    It moves for any accepted frame, not only a heartbeat, so it says "the
    transport is delivering" rather than "the worker is ticking"; the two
    agree unless a clock step sits between them, which is the single case
    :func:`heartbeat_age_seconds` cannot see.

    ``None`` in, ``None`` out: no frame yet is not an age of zero.
    """
    return cached_frame_age_seconds(received_at, now=now)


def _epoch_seconds_from_iso8601(value: Any) -> Optional[float]:
    """UTC epoch seconds for an ISO-8601 timestamp, or ``None`` if unreadable.

    The same idiom ``worker_manager._heartbeat_age`` and
    ``worker_lifecycle._heartbeat_age`` already apply to a ``WorkerThread``:
    ``datetime.fromisoformat``, then "naive means UTC". ``Heartbeat.at``
    carries the very string ``WorkerThread.last_heartbeat`` holds -- written
    into ``status.json`` as TEXT -- so whatever parses there parses here,
    including the trailing ``Z`` Python 3.11+ accepts.

    ``None`` instead of an exception: this is called from
    :meth:`WorkerRuntimeHandle.on_frame`, which runs on the socket read loop
    and has to survive a timestamp it cannot read. A caller can tell "no
    heartbeat yet" from "heartbeat we could not date" only by which slot it
    passed in -- both are ``None``, and a guess here would be worse than the
    ignorance.
    """
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _epoch_or_none(value: Any) -> Optional[int]:
    """*value* only if it is genuinely an incarnation epoch, else ``None``.

    The SAME rule the worker registry applies to its own ``epoch=`` arguments
    (``WorkerRegistry._coerce_epoch``), imported rather than copied: an epoch
    this module believes is real while the registry believes it is absent
    would make the fence compare against a number no mutation ever recorded.
    ``bool`` is excluded on purpose -- ``True`` is an ``int`` in Python and
    would silently mean epoch 1.
    """
    from tools.workspace.worker_registry import _coerce_epoch

    return _coerce_epoch(value)


class StaleWorkerFence(RuntimeError):
    """This handle's incarnation is gone; it must not dispatch again.

    The invariant: a handle speaks for exactly ONE incarnation. Once a
    successor owns the worker key -- proven by an epoch that moved, or by a
    dead body, or by a newer epoch in the registry -- the handle is a leftover
    of the past and any query sent through it would reach either a corpse or
    the successor's queue under the predecessor's identity. Neither is a
    dispatch; both are lost work wearing a reply.

    A ``RuntimeError`` and not a ``ValueError``: nothing about the ARGUMENTS
    is wrong, the world moved. ``expected_epoch`` is what the handle was built
    for, ``observed_epoch`` what it found (``None`` when the observation was
    not an int, e.g. a body that cannot say).

    Fail open on absence, closed on evidence: an unreadable or missing epoch
    is NOT staleness, because a build with no incarnation identity at all is
    the shipped state -- so only a positively established successor raises.
    """

    def __init__(
        self,
        message: str,
        *,
        expected_epoch: Optional[int] = None,
        observed_epoch: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.expected_epoch = expected_epoch
        self.observed_epoch = observed_epoch


class WorkerRuntimeHandle:
    """The supervisor's view of one worker runtime (S2: thread or process).

    Construct one per worker *instance* (D1) around the ``WorkerThread`` that
    already exists -- the handle never constructs or starts a thread itself,
    so ``run()``, the fence, ``_turn_cancel`` and every other in-process
    invariant stay exactly as they are (§3.3, D7).
    """

    def __init__(
        self,
        worker: Any,
        *,
        mode: str = MODE_THREAD,
        session_id: Optional[str] = None,
        worker_name: Optional[str] = None,
        instance_id: Optional[int] = None,
    ) -> None:
        if mode not in RUNTIME_MODES:
            raise InvalidRuntimeModeError(
                f"mode must be one of {RUNTIME_MODES}, got {mode!r}"
            )
        self._worker = worker
        self._mode = mode
        # S7a-2: the incarnation this handle speaks for, captured ONCE here.
        # A handle is the supervisor's memory of one incarnation, so re-reading
        # the live attributes on every dispatch would let it silently become
        # the successor's handle. Explicit arguments win (S7a-3 will pass the
        # identity it already knows); otherwise the worker answers for itself,
        # and anything it cannot answer stays ``None`` -- "unknown", never a
        # guess. The epoch goes through the registry's int-not-bool rule so a
        # duck-typed or mocked ``_generation`` becomes unknown instead of a
        # truthy object that would compare unequal to every real epoch.
        self._session_id = (
            session_id if session_id is not None
            else getattr(worker, "session_id", None)
        )
        self._worker_name = (
            worker_name if worker_name is not None
            else getattr(worker, "worker_name", None) or getattr(worker, "name", None)
        )
        self._instance_id = (
            instance_id if instance_id is not None
            else getattr(worker, "instance_id", None)
        )
        self._epoch = _epoch_or_none(getattr(worker, "_generation", None))
        # Process mode is built lazily: constructing a handle must not spawn an
        # OS process (§7), so the runtime spec and the runtime itself stay
        # ``None`` until the first ``dispatch_query`` asks for them.
        self._spec: Optional[dict] = None
        self._process_runtime: Optional[Any] = None
        # S4: what the last frame from a process-mode worker said, and the two
        # clocks it carried (D1). Filled by ``on_frame``; ``state`` reads the
        # state cache before it falls back to the ``status.json`` mirror.
        self._last_worker_state: Optional[WorkerState] = None
        # S4b: the last ``event`` a process-mode worker published, verbatim.
        # The LAST one only -- a cache is not a queue: no consumer reads a
        # backlog here yet, and an unbounded buffer with no drain would be a
        # leak with a nicer name.
        self._last_event: Optional[Event] = None
        # ``Heartbeat.at`` as UTC epoch seconds -- when the worker says it
        # ticked (``heartbeat_age_seconds``).
        self._last_heartbeat_at: Optional[float] = None
        # Local receipt time of the last accepted frame of EITHER kind -- when
        # the transport last delivered (``frame_arrival_age_seconds``). Two
        # slots because they answer two questions; see ``on_frame``.
        self._last_received_at: Optional[float] = None

    @classmethod
    def spawn(
        cls,
        worker: Any,
        *,
        environ: Optional[Mapping[str, str]] = None,
        session_id: Optional[str] = None,
        worker_name: Optional[str] = None,
        instance_id: Optional[int] = None,
    ) -> "WorkerRuntimeHandle":
        """Build a handle using the mode in force *now* (§10, D11).

        This is the spawn-time seam: the flag is read here and nowhere else,
        so "read at spawn time" is a property of the call graph rather than a
        convention. In S2 ``process`` resolves to a handle that spawns its
        runtime on the first ``dispatch_query``, so spawning a handle still
        drives nothing (§7).

        The three identity arguments are passed straight through to
        ``__init__`` and resolved there, once: a caller that knows the worker's
        ``(session, name, instance)`` can hand it over in the same call that
        picks the mode, instead of constructing a handle and correcting it
        afterwards.
        """
        return cls(
            worker,
            mode=resolve_runtime_mode(environ),
            session_id=session_id,
            worker_name=worker_name,
            instance_id=instance_id,
        )

    @property
    def mode(self) -> str:
        """The mode this handle was built in."""
        return self._mode

    @property
    def epoch(self) -> Optional[int]:
        """The incarnation epoch this handle was built for; ``None`` if unknown.

        Fixed at construction and never re-derived: it is the identity the
        dispatch fence compares against, so a value that drifted with the live
        worker would make the fence agree with whatever it found.
        """
        return self._epoch

    def _fence_message(self, reason: str, *, observed: Optional[int]) -> str:
        """Describe *reason* with this handle's identity and both epochs."""
        return (
            f"worker runtime handle is stale: {reason} "
            f"(session_id={self._session_id!r}, "
            f"worker_name={self._worker_name!r}, "
            f"instance_id={self._instance_id!r}, "
            f"expected epoch={self._epoch!r}, "
            f"observed epoch={'unknown' if observed is None else observed!r})"
        )

    def _assert_current(self) -> None:
        """Raise :class:`StaleWorkerFence` if this handle's incarnation is gone.

        A handle speaks for exactly ONE incarnation, so before it touches the
        worker it checks the three things that can prove that incarnation is
        no longer the live one (S7a-2):

        1. **epoch drift** -- the worker now reports a different
           ``_generation`` than the one latched at construction;
        2. **death** -- the worker's own ``is_alive()`` answers ``False``, so
           no dispatch can ever be answered;
        3. **a successor in the registry** -- the registry has recorded a
           higher epoch for this ``(session_id, worker_name)`` pair, i.e. the
           replacement was announced even if this object cannot see it.

        Absence of evidence is never a fence: a worker that reports no epoch,
        has no ``is_alive``, or matches no registry entry passes. That keeps
        the check fail-open for hand-built doubles and for the window before
        S7a-3 registers anything, and it is why only *positive* evidence
        raises. Any other exception raised while checking is a bug in the
        check rather than evidence about the incarnation, so it is logged at
        DEBUG and the call proceeds.
        """
        try:
            worker = self._worker
            expected = self._epoch
            observed = _epoch_or_none(getattr(worker, "_generation", None))
            if (
                isinstance(expected, int)
                and isinstance(observed, int)
                and expected != observed
            ):
                raise StaleWorkerFence(
                    self._fence_message(
                        "the worker's _generation moved", observed=observed
                    ),
                    expected_epoch=expected,
                    observed_epoch=observed,
                )
            is_alive = getattr(worker, "is_alive", None)
            if callable(is_alive) and is_alive() is False:
                raise StaleWorkerFence(
                    self._fence_message(
                        "the worker is no longer alive", observed=observed
                    ),
                    expected_epoch=expected,
                    observed_epoch=observed,
                )
            from tools.workspace.worker_registry import stored_epoch_for

            stored = stored_epoch_for(self._session_id, self._worker_name)
            if isinstance(stored, int) and (expected is None or stored > expected):
                raise StaleWorkerFence(
                    self._fence_message(
                        "the registry has recorded a newer epoch", observed=stored
                    ),
                    expected_epoch=expected,
                    observed_epoch=stored,
                )
        except StaleWorkerFence:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "runtime handle fence could not be evaluated for (%r, %r): %s; "
                "treating it as no evidence",
                self._session_id,
                self._worker_name,
                exc,
            )

    def dispatch_query(self, query: str, timeout: float = 120.0) -> str:
        """Run *query* and return the reply.

        Thread mode: the reply is the return value of the same in-process
        call, which is also the "receive" half -- ``send_query`` already
        correlates on a fresh ``query_id`` and drains stale replies (D9).

        Process mode: the query becomes a ``dispatch_query`` frame, the reply
        is matched on its ``query_id`` against the ``reply`` frame (§4.8), and
        the wait happens on the pending future inside
        ``ProcessWorkerRuntime``.

        The S7a-2 fence runs first, in both modes: a handle whose incarnation
        has been replaced raises :class:`StaleWorkerFence` instead of
        dispatching into whatever now occupies the worker slot.
        """
        self._assert_current()
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

        The S7a-2 fence is re-evaluated on *every* call, not only the one that
        builds the runtime: that is what makes the control verbs routing
        through here (:meth:`stop`, :meth:`pause`, :meth:`resume`,
        :meth:`kill`, :meth:`cancel`) raise :class:`StaleWorkerFence` rather
        than act on a successor, and it means a stale handle cannot build a
        *second* :class:`ProcessWorkerRuntime` either.
        """
        self._assert_current()
        if self._process_runtime is None:
            from tools.workspace.worker_runtime_process import (
                ProcessWorkerRuntime,
                spec_from_worker,
            )

            self._spec = spec_from_worker(
                self._worker, epoch=getattr(self._worker, "_generation", 0)
            )
            runtime = ProcessWorkerRuntime(self._spec, on_frame=self.on_frame)
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

        Three frame kinds are dispatched -- ``WorkerState`` refreshes the state
        cache, ``Heartbeat`` refreshes the heartbeat timestamp, ``Event``
        refreshes the last-event cache -- and anything else is ignored: a frame
        kind this stage does not know is not an error, it is simply not ours to
        interpret.

        Two clocks are cached, because D1 asks two questions and they do not
        have the same answer on the same frame:

        * ``_last_heartbeat_at`` -- ``Heartbeat.at`` parsed to UTC epoch
          seconds, i.e. when the WORKER says it ticked. The primary liveness
          reading (:func:`heartbeat_age_seconds`): both processes are on the
          same host, so the clocks agree to microseconds.
        * ``_last_received_at`` -- local receipt time, refreshed by BOTH frame
          kinds, i.e. when the TRANSPORT last delivered
          (:func:`frame_arrival_age_seconds`). The backstop for the one case
          ``at`` can lie about, a clock step between the child writing ``at``
          and the supervisor reading it.

        A ``Heartbeat.at`` that will not parse caches ``None`` rather than a
        guess; the arrival time is still recorded, so a malformed timestamp
        costs the ``at`` reading and nothing else.

        Never raises, does no I/O, takes no lock and calls nothing back into
        the worker: this runs on the socket read loop, and a frame that
        surprises us must not be able to take the transport down.
        ``Heartbeat`` has a producer as of S6b: the child side installs an
        outbox-backed sink in ``worker_runtime_main``, so process mode emits one
        on every ``_heartbeat_tick`` and this branch is what keeps
        ``heartbeat_age_seconds`` answerable. ``Event`` is the
        other kind with a producer today (S4b): the child forwards every event it
        publishes, so this branch is where a forwarder reads one without
        touching the socket.
        """
        try:
            if isinstance(frame, WorkerState):
                self._last_worker_state = frame
            elif isinstance(frame, Heartbeat):
                self._last_heartbeat_at = _epoch_seconds_from_iso8601(frame.at)
            elif isinstance(frame, Event):
                self._last_event = frame
            else:
                return
            self._last_received_at = time.time()
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

        Process mode goes through :meth:`_ensure_process_runtime`, so a stale
        handle raises :class:`StaleWorkerFence` instead of parking a successor.
        """
        if self._mode == MODE_PROCESS:
            return self._ensure_process_runtime().stop()
        self._worker.stop()
        return True

    def pause(self) -> bool:
        """Pause the worker's turn loop (§4.3); see :meth:`stop`.

        Process mode goes through :meth:`_ensure_process_runtime`, so a stale
        handle raises :class:`StaleWorkerFence` instead of pausing a successor.
        """
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

        Process mode goes through :meth:`_ensure_process_runtime`, so a stale
        handle raises :class:`StaleWorkerFence` instead of resuming a
        successor.
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

        Process mode goes through :meth:`_ensure_process_runtime`, so a stale
        handle raises :class:`StaleWorkerFence` instead of killing a successor.
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

        Process mode goes through :meth:`_ensure_process_runtime`, so a stale
        handle raises :class:`StaleWorkerFence` instead of cancelling a
        successor's turn.
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


def attach_thread_frame_sink(
    worker: Any, *, mode: str = MODE_THREAD
) -> "WorkerRuntimeHandle":
    """Build a handle around *worker* and install its ``on_frame`` as the sink.

    This is the S5 production construction site: the one place a real
    ``WorkerThread`` gets a :class:`WorkerRuntimeHandle` and the runtime ->
    supervisor frame seam is closed in-process. The worker keeps its own
    ``run()`` loop, fence and ``_turn_cancel`` -- the handle receives only what
    the worker chooses to emit through ``_frame_sink`` (S4a ``WorkerState``,
    S5 ``Heartbeat``), so thread mode stays byte-identical in observable
    behaviour to calling the thread directly (§11.1).

    Registration in :data:`_RUNTIME_HANDLES` is deliberately deferred: the
    supervisor command path (``web_ui.backend.workspace_routes`` ->
    ``runtime_handle_for`` -> ``_dispatch_via_runtime``) prefers a registered
    handle and would then bypass ``command.json``, which is the only command
    channel the running worker actually reads.

    The handle is also recorded as ``worker._runtime_handle`` so its lifetime
    is tied to the worker's: the worker owns the only reference either side
    needs, and nothing else has to remember to close a handle that owns no
    resource of its own in thread mode (``close`` is a no-op there).
    """
    handle = WorkerRuntimeHandle(worker, mode=mode)
    worker._frame_sink = handle.on_frame
    worker._runtime_handle = handle
    return handle
