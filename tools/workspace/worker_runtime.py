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

import os
from typing import Any, Mapping, Optional

from tools.workspace.worker_runtime_protocol import WorkerState

#: The flag that selects the runtime mode (§10, D11).
WORKER_RUNTIME_MODE_ENV = "WORKER_RUNTIME_MODE"

#: Worker body runs in the supervisor's interpreter. The shipped default.
MODE_THREAD = "thread"

#: Worker body runs in its own OS process (S2: dispatch_query only).
MODE_PROCESS = "process"

#: Exactly what the flag accepts. Nothing else is a mode.
RUNTIME_MODES = (MODE_THREAD, MODE_PROCESS)


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

        Process mode has no such attributes to read in this process: the
        ``worker_state`` frame is S4 (§10), so this raises rather than
        returning a state it cannot actually observe.
        """
        if self._mode == MODE_PROCESS:
            raise NotImplementedError(
                f"{WORKER_RUNTIME_MODE_ENV}={MODE_PROCESS!r} cannot report state "
                "before the worker_state frame exists: it lands in stage S4 -- "
                "see docs/design-worker-runtime-process.md §10."
            )
        worker = self._worker
        return WorkerState(
            status=worker.status,
            current_task=worker.current_task,
            error=worker.error,
            paused_manually=bool(worker._manual_only_pause),
        )

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
        signal. A thread that is not mid-turn is refused, for the same reason
        ``worker_runtime_main._handle_cancel`` refuses it: both modes must agree
        on when there is nothing to cancel.
        """
        if self._mode == MODE_PROCESS:
            return self._ensure_process_runtime().cancel()
        worker = self._worker
        if worker.status != "busy":
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
