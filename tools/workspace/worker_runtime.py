# tools/workspace/worker_runtime.py
"""``WorkerRuntimeHandle`` -- stage S0 of the P3 worker-runtime split.

What S0 is (§10)
----------------
S0 is the **seam, not the transport**. It introduces the object every later
stage will talk to -- ``WorkerRuntimeHandle`` -- and makes it stand in front of
the *existing* ``WorkerThread`` without moving a single byte: every method is a
direct in-process call to the thread that was handed in. There is no socket, no
extra thread, no queue and no polling here, and there must not be, because S0
ships with ``WORKER_RUNTIME_MODE=thread`` and has to be byte-identical in
observable behaviour to calling the thread directly (§11.1, "no behaviour
change").

S2 fills the seam in (§10): it turns the same four methods into
``dispatch_query`` / ``query_accepted`` / ``reply`` frames over the
``AF_UNIX`` socketpair (§4.1, §4.3, §4.4), at which point the handle keeps the
pending map and the outbox described in §3.3 while a ``WorkerThread`` runs in
another OS process. Nothing in this module's *signatures* may need to change
for that: a method that cannot survive becoming a frame does not belong on the
handle.

What is deliberately absent
---------------------------
*No separate ``receive_reply``.* In S0 the reply comes back from the call that
asked for it, so a second method would be a stub with nothing to do; S2 makes
the reply arrive as a frame and adds the wait on this same object.
*No ``stop`` / ``pause`` / ``resume`` / ``kill``.* They arrive with the
``control`` frame in S3 (§10) -- faking them now would ship untested
behaviour and give the impression that control has crossed the boundary when
it has not.
*No ``seq`` / framing state.* The handle owns none until there is a stream to
sequence; the messages it will carry already exist in
``tools.workspace.worker_runtime_protocol``.

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

#: Worker body runs in its own OS process. Not implemented before S2.
MODE_PROCESS = "process"

#: Exactly what the flag accepts. Nothing else is a mode.
RUNTIME_MODES = (MODE_THREAD, MODE_PROCESS)

_PROCESS_NOT_IMPLEMENTED = (
    f"{WORKER_RUNTIME_MODE_ENV}={MODE_PROCESS!r} is not implemented by this "
    "build: stage S0 ships only the in-process seam, and the process transport "
    "(spawn + AF_UNIX socketpair + framed dispatch) lands in stage S2 -- see "
    "docs/design-worker-runtime-process.md §10."
)


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
    """The supervisor's view of one worker runtime (S0: the thread itself).

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
        if mode == MODE_PROCESS:
            raise NotImplementedError(_PROCESS_NOT_IMPLEMENTED)
        self._worker = worker
        self._mode = mode

    @classmethod
    def spawn(
        cls, worker: Any, *, environ: Optional[Mapping[str, str]] = None
    ) -> "WorkerRuntimeHandle":
        """Build a handle using the mode in force *now* (§10, D11).

        This is the spawn-time seam: the flag is read here and nowhere else,
        so "read at spawn time" is a property of the call graph rather than a
        convention. In S0 it always resolves to ``thread`` unless the operator
        has explicitly asked for ``process``, which raises.
        """
        return cls(worker, mode=resolve_runtime_mode(environ))

    @property
    def mode(self) -> str:
        """The mode this handle was built in."""
        return self._mode

    def dispatch_query(self, query: str, timeout: float = 120.0) -> str:
        """Run *query* and return the reply.

        S0: the reply is the return value of the same in-process call, which
        is also the "receive" half -- there is no second channel to wait on
        until S2. ``send_query`` already correlates on a fresh ``query_id``
        and drains stale replies (D9), so the identity the frame will carry
        in S2 is minted by the code path that runs today.
        """
        return self._worker.send_query(query, timeout=timeout)

    def state(self) -> WorkerState:
        """The worker's observable state, in the §4.4 ``worker_state`` shape.

        Filled from the thread's live attributes rather than from
        ``status.json``: the file is the supervisor's mirror of this state, and
        reading the mirror back would make a stale file indistinguishable from
        a stale worker. ``paused_manually`` has no attribute of its own -- it
        is derived from ``_manual_only_pause`` by design (see
        ``WorkerThread.__init__``).
        """
        worker = self._worker
        return WorkerState(
            status=worker.status,
            current_task=worker.current_task,
            error=worker.error,
            paused_manually=bool(worker._manual_only_pause),
        )

    def cancel(self) -> None:
        """Interrupt the in-flight turn, leaving the worker alive (§5.1).

        S0 delegates to ``detach()``, which is the existing in-process
        cancellation path: it sets ``_turn_cancel`` and leaves ``_stop_event``
        clear. The ``reason`` attribution a ``cancel`` frame carries (§4.3)
        has no consumer until the S3 frame exists, so it is not a parameter
        here -- adding it now would be an argument the handle silently drops.
        """
        self._worker.detach()
