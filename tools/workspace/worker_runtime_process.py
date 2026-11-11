# tools/workspace/worker_runtime_process.py
"""Supervisor half of the worker-runtime split -- stage S2 (§3.3, §4.8).

What this is
------------
One :class:`ProcessWorkerRuntime` per worker *instance* (D1): an ``spawn``-ed
OS process running :func:`tools.workspace.worker_runtime_main.worker_runtime_main`,
an ``AF_UNIX`` socketpair between the two (§4.1), one :class:`FrameEmitter` and
one :class:`FrameDecoder` **per direction** (§4.2), and a ``pending`` map from
``query_id`` to a :class:`concurrent.futures.Future` (§4.8).

Why the return type does not change
-----------------------------------
``dispatch_query`` returns ``str`` in both modes, exactly as the S0 thread-mode
handle does, and a failure is an **exception that carries the terminal
``Reply``** (:class:`WorkerRuntimeLost`, :class:`WorkerQueryTimeout`). The
tempting alternative -- return the envelope when the reply says ``ok=False`` --
would make the return type mode-dependent and let a caller read a failure
envelope as if it were a response; the caller that wanted the details can read
``exc.reply`` and lose nothing, and the caller that did not keeps working
unchanged. That is the same rule the thread path already follows: trouble is
raised, not returned.

What is *not* here
------------------
The supervisor half of the control plane is here (§10): the ``control`` and
``cancel`` verbs, a per-runtime monotonic ``request_id`` source, and the ack
routing that lets a verb wait for its own ``control_ack``/``cancel_ack`` while
the reader keeps draining the socket. There are no ``worker_state``/``heartbeat``
frames (§10, S4),
no container adoption (§3.4: the supervisor owns that, not the socket), and no
retry. A lost runtime is reported once and stays lost until a new
:class:`ProcessWorkerRuntime` is built, because silently re-spawning a worker
would discard the conversation state the old process held.
"""

from __future__ import annotations

import itertools
import json
import multiprocessing
import queue
import socket
import sys
import threading
import time
import uuid
from concurrent import futures
from typing import Any, Callable, Dict, Optional

from tools.workspace.worker_runtime_main import worker_runtime_main
from tools.workspace.worker_runtime_protocol import (
    CAPABILITY_CONFIG_UPDATE,
    DIRECTION_FROM_RUNTIME,
    DIRECTION_TO_RUNTIME,
    Bye,
    Cancel,
    CancelAck,
    ConfigAck,
    ConfigUpdate,
    Control,
    ControlAck,
    DispatchQuery,
    Event,
    Fatal,
    FrameDecoder,
    FrameEmitter,
    Heartbeat,
    Hello,
    Ping,
    Pong,
    QueryAccepted,
    Reply,
    Shutdown,
    WorkerRuntimeProtocolError,
    WorkerState,
    encode_frame,
)

#: One ``recv`` inside the reader thread; the decoder reassembles frames
#: regardless of how the bytes are chunked (§4.2).
RECV_CHUNK_BYTES = 65536

#: How long :meth:`ProcessWorkerRuntime.start` waits for ``hello`` (§4.4). A
#: runtime that cannot greet us is not a runtime, so this is a start failure and
#: not a retryable condition.
HELLO_TIMEOUT_SECONDS = 30.0

#: How long a ``control`` verb waits for its ``control_ack`` (§4.4, §10). A
#: bound rather than an unbounded wait: the caller is a supervisor deciding
#: whether a worker stopped, and "no answer" has to become an exception it can
#: act on instead of a thread parked forever behind a wedged runtime.
ACK_TIMEOUT_SECONDS = 5.0

#: Default bound on :meth:`ProcessWorkerRuntime.ping` (§4.4). Deliberately the
#: same order as :data:`ACK_TIMEOUT_SECONDS`: a probe only waits for a socket
#: round-trip, never for work to be done, and a caller with a watchdog deadline
#: of its own passes a shorter one.
PING_TIMEOUT_SECONDS = 5.0

#: The ``deadline_ms`` a ``cancel`` carries (§4.3). It rides in the frame so the
#: runtime can honour it; the supervisor's own wait is bounded by
#: :data:`ACK_TIMEOUT_SECONDS` regardless, because a deadline the far side owns
#: must never become the near side's timeout.
CANCEL_DEADLINE_MS = 5000

#: Default ``cancel`` attribution (§4.3): a supervisor asking for a kill, as
#: opposed to a budget ceiling noticing one. A reason is a diagnostic string and
#: not control flow -- the runtime acts on the cancel either way.
CANCEL_REASON_OPERATOR_KILL = "operator_kill"

#: Attribute names :func:`spec_from_worker` copies verbatim from the
#: ``WorkerThread`` it is handed. Listed as data so the "which attributes cross
#: the boundary" question has one answer that a reader can audit.
_OPTIONAL_KWARGS = (
    ("tool_classes", "_tool_classes"),
    ("session_permissions", "_session_permissions"),
    ("project_root", "_project_root"),
    ("timeout_seconds", "_timeout_seconds"),
    ("session_id", "session_id"),
    ("workspace_id", "workspace_id"),
    ("max_container_count", "max_container_count"),
    ("max_token_usage", "max_token_usage"),
    ("max_runtime_s", "max_runtime_s"),
    # The initial context ("briefing") and the query that rides in it. Without
    # this line the child interpreter builds a worker with no brief at all.
    ("initial_context", "_initial_context"),
)


class RuntimeSpecError(ValueError):
    """The worker cannot be described as a picklable runtime spec (§3.3)."""


class WorkerRuntimeLost(RuntimeError):
    """The runtime process is gone, or answered ``ok=False`` (§4.7).

    Carries the terminal :class:`Reply` so the supervisor can mirror the
    ``status`` it would have got in thread mode without parsing a message.
    """

    def __init__(self, message: str, *, reply: Reply) -> None:
        super().__init__(message)
        #: The ``reply`` frame that ends this query, synthesised when the
        #: process died without one (§4.7).
        self.reply = reply


class WorkerRuntimeUnsupported(RuntimeError):
    """The runtime's ``hello`` did not advertise the capability a verb needs.

    A scope fact, not a fault (§4.5): a runtime built before the frame existed
    ignores it in silence, and silence is indistinguishable from a lost frame.
    Feature-detecting in the handshake turns that ambiguity into an exception
    the caller can act on -- respawn the worker, or degrade -- before anything
    is sent.
    """


class WorkerQueryTimeout(TimeoutError):
    """No ``reply`` arrived inside the caller's timeout (§4.4)."""

    def __init__(self, message: str, *, reply: Reply) -> None:
        super().__init__(message)
        #: The terminal ``reply`` this side synthesised; ``ok`` is ``False``.
        self.reply = reply


def spec_from_worker(worker: Any, *, epoch: Optional[int] = None) -> Dict[str, Any]:
    """Describe *worker* as the picklable dict the runtime side reconstructs.

    The handle in thread mode wraps a *live* object; the process mode has to
    rebuild that object in a child interpreter from data alone, so this is
    where "which parts of a ``WorkerThread`` are its identity" is decided --
    once, and in public view. The fence axes (``worker`` / ``instance`` /
    ``epoch``) travel as data (§4.2); the configuration travels as
    ``worker_kwargs`` and is passed straight back into ``WorkerThread``.

    *epoch* defaults to the worker's own generation counter, because that is
    the value the command channel already fences this incarnation by; passing it
    explicitly keeps the spec deterministic in tests.
    """
    for attribute in ("worker_name", "instance_id", "definition", "workspace_dir"):
        if not hasattr(worker, attribute):
            raise RuntimeSpecError(
                f"{type(worker).__name__} has no {attribute!r} attribute: process "
                "mode needs a worker constructed with definition, agent_config "
                "and workspace_dir (§3.3)"
            )
    agent_config = getattr(worker, "_agent_config_dict", None)
    if agent_config is None:
        agent_config = getattr(worker, "agent_config", None)
    if agent_config is None:
        raise RuntimeSpecError(
            f"{type(worker).__name__} has no agent_config: process mode needs a "
            "worker constructed with definition, agent_config and workspace_dir "
            "(§3.3)"
        )
    kwargs: Dict[str, Any] = {
        "name": worker.worker_name,
        "definition": worker.definition,
        "agent_config": agent_config,
        # A str rather than a Path: the spec has to pickle into a spawn child,
        # and the child's working directory is an implementation detail.
        "workspace_dir": str(worker.workspace_dir),
        "instance_id": int(worker.instance_id),
    }
    for target, source in _OPTIONAL_KWARGS:
        kwargs[target] = getattr(worker, source, None)
    # ``container_manager`` is deliberately NOT carried: it holds live handles,
    # locks and possibly client objects and cannot be pickled. Container
    # adoption after a restart is the supervisor's concern (§3.4), not something
    # a worker thread can pass across a process boundary.
    return {
        "worker": str(worker.worker_name),
        "instance": int(worker.instance_id),
        "epoch": int(epoch if epoch is not None else getattr(worker, "_generation", 0) or 0),
        "worker_factory": getattr(worker, "_runtime_worker_factory", None),
        "worker_kwargs": kwargs,
    }


class _Outbox:
    """Single writer for the supervisor -> runtime direction (§4.6).

    One thread, one FIFO, so ``seq`` has exactly one owner. A deliberate local
    copy of the runtime's outbox rather than a shared helper: the two live in
    different processes and each owns a different direction, and a queue shared
    across that boundary is precisely the mutable state §4.6 forbids.
    """

    def __init__(self, sock: Any, emitter: FrameEmitter) -> None:
        self._sock = sock
        self._emitter = emitter
        self._q: "queue.Queue[Any]" = queue.Queue()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="supervisor-outbox", daemon=True
        )
        self._thread.start()

    def put(self, message: Any) -> None:
        """Queue *message* for the writer. Never blocks, never sends."""
        self._q.put(message)

    def _run(self) -> None:
        while True:
            message = self._q.get()
            if message is _SENTINEL:
                return
            if self._closed:
                continue
            try:
                self._sock.sendall(encode_frame(self._emitter.emit(message)))
            except OSError:
                self._closed = True
                self._discard()
                return

    def _discard(self) -> None:
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return


_SENTINEL = object()


class _AckSlot:
    """One outstanding request waiting for one ``*_ack`` frame (§10).

    The event and the frame slot travel together so the reader can close a
    request with a single ``set()`` and no per-request bookkeeping: whoever is
    waiting owns the object and the reader only has to find it. ``message`` is
    written *before* ``event`` is set, so a waiter that wakes always sees the
    frame that woke it.
    """

    __slots__ = ("event", "message")

    def __init__(self) -> None:
        self.event = threading.Event()
        #: The ack frame, or ``None`` when the stream ended instead (§4.7).
        self.message: Optional[Any] = None


class _RequestIdSource:
    """Monotonic ``request_id`` source for one runtime (§10).

    One counter per runtime, because a request id is only ever correlated
    against acks that came back down *this* socket: a process-wide counter would
    put two runtimes' requests in one namespace for no gain. ``str`` because the
    frame field is a ``str``, and monotonic rather than a uuid because the value
    of a request id is that a log line or a stray ack names its place in this
    runtime's request sequence, not that it is unguessable.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counter = itertools.count(1)

    def next(self) -> str:
        """Return the next id. Verbs are called from any thread (§11.2)."""
        with self._lock:
            return str(next(self._counter))


class ProcessWorkerRuntime:
    """The supervisor's handle on one worker runtime running in its own process.

    Lifecycle: construct -> :meth:`start` (spawn + ``hello``) -> any number of
    :meth:`dispatch_query` calls -> :meth:`close` (``shutdown`` + ``bye``, then
    ``SIGKILL`` if the process will not go). :meth:`start` is not called from
    ``__init__`` because a constructor that spawns a process turns a mode typo
    into an observable side effect; the handle in
    :mod:`tools.workspace.worker_runtime` relies on that when it defers the
    spawn to the first dispatch.
    """

    def __init__(
        self,
        spec: Dict[str, Any],
        *,
        worker_factory: Optional[Callable[..., Any]] = None,
        start_method: str = "spawn",
        on_frame: Optional[Callable[[Any], None]] = None,
    ) -> None:
        self._spec = dict(spec)
        self._worker_factory = worker_factory
        self._start_method = start_method
        #: Sink for the frames this stage routes instead of consuming --
        #: ``event`` / ``heartbeat`` / ``worker_state`` (S4b). Bound at
        #: CONSTRUCTION and not at ``start``: the child emits ``hello`` last,
        #: so a frame that arrives ahead of the handshake (``worker_spawned``
        #: is published before it) is already routable and must not be
        #: dropped on the floor (D3).
        self._on_frame = on_frame
        self._process: Optional[Any] = None
        self._sock: Optional[Any] = None
        self._outbox: Optional[_Outbox] = None
        self._emitter: Optional[FrameEmitter] = None
        self._decoder: Optional[FrameDecoder] = None
        self._reader: Optional[threading.Thread] = None
        #: ``query_id -> Future`` for queries whose ``reply`` has not arrived
        #: (§4.8). Written by dispatchers, drained by the reader thread; every
        #: access is a single GIL-atomic dict operation on a key only one side
        #: touches at a time, so no lock is needed to keep it coherent.
        self._pending: Dict[str, "futures.Future[Reply]"] = {}
        #: ``query_id`` values the runtime acknowledged (§4.4): evidence that a
        #: dispatch reached the worker before its reply did.
        self._accepted: set = set()
        self._accepted_events: Dict[str, threading.Event] = {}
        #: ``request_id -> _AckSlot`` for ``control`` / ``cancel`` requests whose
        #: first ack has not arrived (§10). Guarded by a lock rather than by the
        #: GIL-atomic rule ``_pending`` uses, because these entries are *popped*
        #: by whoever gets there first: a check-then-pop race there would let one
        #: ack close two waiters, or close the wrong one.
        self._acks: Dict[str, _AckSlot] = {}
        #: ``nonce -> _AckSlot`` for outstanding liveness probes (§4.4). Keyed by
        #: nonce rather than by request id because the nonce IS the correlation:
        #: a pong for a ping that already timed out must not close a later one.
        #: Shares ``_acks_lock`` -- the same pop-by-first-arrival hazard applies,
        #: and one lock is cheaper than two orders of acquisition to reason about.
        self._pings: Dict[int, _AckSlot] = {}
        #: Last nonce this runtime issued; bumped under ``_acks_lock`` so two
        #: concurrent probes can never share one.
        self._ping_nonce = 0
        self._acks_lock = threading.Lock()
        #: Monotonic request-id source, one per runtime (§10).
        self._request_ids = _RequestIdSource()
        self._hello: Optional[Hello] = None
        #: Capability names the runtime advertised in ``hello`` (§4.5, W9). Empty
        #: until the handshake lands, which is why every capability-gated verb
        #: checks this set rather than assuming a build.
        self._capabilities: frozenset = frozenset()
        self._hello_event = threading.Event()
        self._bye_seen = False
        self._fatal: Optional[Fatal] = None
        self._lost = False
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> Hello:
        """Spawn the runtime process and wait for its ``hello`` (§4.4)."""
        if self._started:
            raise RuntimeError("ProcessWorkerRuntime.start() may be called once")
        self._started = True
        context = multiprocessing.get_context(self._start_method)
        parent_sock, child_sock = socket.socketpair(
            socket.AF_UNIX, socket.SOCK_STREAM
        )
        process = context.Process(
            target=worker_runtime_main,
            args=(child_sock, self._spec),
            kwargs={"worker_factory": self._worker_factory},
            name=(
                f"worker-runtime-{self._spec['worker']}-{self._spec['instance']}"
            ),
            daemon=False,
        )
        process.start()
        # The child holds its own duplicate now. Keeping this copy open would
        # keep the socket alive after the child dies, so the reader would never
        # see EOF and every pending query would wait out its full timeout
        # instead of failing (§4.7).
        child_sock.close()
        self._process = process
        self._sock = parent_sock
        self._emitter = FrameEmitter(
            DIRECTION_TO_RUNTIME,
            worker=self._spec["worker"],
            instance=int(self._spec["instance"]),
            epoch=int(self._spec["epoch"]),
        )
        self._outbox = _Outbox(parent_sock, self._emitter)
        self._decoder = FrameDecoder(DIRECTION_FROM_RUNTIME)
        self._reader = threading.Thread(
            target=self._read_loop,
            name=f"worker-runtime-reader-{self._spec['worker']}",
            daemon=True,
        )
        self._reader.start()
        if not self._hello_event.wait(HELLO_TIMEOUT_SECONDS):
            self._lost = True
            raise WorkerRuntimeLost(
                f"the runtime for {self._spec['worker']!r} sent no hello within "
                f"{HELLO_TIMEOUT_SECONDS}s (§4.4)",
                reply=_terminal_reply("", "hello_timeout"),
            )
        hello = self._hello
        assert hello is not None  # set together with the event
        if hello.pid != process.pid:
            raise WorkerRuntimeLost(
                f"hello claims pid {hello.pid}, but the process we spawned is "
                f"{process.pid}: frames from another incarnation are refused (§4.7)",
                reply=_terminal_reply("", "pid_mismatch"),
            )
        if hello.epoch_seen != int(self._spec["epoch"]):
            raise WorkerRuntimeLost(
                f"hello reports epoch {hello.epoch_seen}, expected "
                f"{self._spec['epoch']}: the runtime is not the incarnation we "
                "asked for (§4.7)",
                reply=_terminal_reply("", "epoch_mismatch"),
            )
        return hello

    def close(self, grace_ms: int = 5000) -> None:
        """Ask the runtime to stop, then make sure the process is gone (§3.3).

        Best effort on the polite half -- a runtime that is already dead cannot
        receive ``shutdown`` -- and non-negotiable on the forceful half: the
        process is ``SIGKILL``-ed if it outlives the grace period, because a
        supervisor that leaves orphans behind is worse than one that drops a
        few trailing events.
        """
        process = self._process
        if process is None:
            return
        grace_seconds = max(float(grace_ms), 0.0) / 1000.0
        if self._outbox is not None:
            self._outbox.put(Shutdown(grace_ms=int(grace_ms)))
        deadline = time.monotonic() + grace_seconds
        # Stop waiting as soon as the process is gone: waiting the full grace
        # for a ``bye`` that can never arrive would turn every crash into a
        # multi-second shutdown.
        while (
            time.monotonic() < deadline
            and not self._bye_seen
            and process.is_alive()
        ):
            time.sleep(0.01)
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        process.join(timeout=grace_seconds)
        if process.is_alive():
            process.kill()
            process.join(5)
        assert not process.is_alive(), (
            "the runtime process outlived SIGKILL: a supervisor that cannot stop "
            "a runtime has not isolated it (§3.3)"
        )

    def __enter__(self) -> "ProcessWorkerRuntime":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def dispatch_query(self, query: str, timeout: float = 120.0) -> str:
        """Send *query* and return the reply's response, exactly as thread mode does.

        The return type is deliberately ``str`` in both modes: the thread path
        returns whatever ``WorkerThread.send_query`` returns, and a caller must
        not have to know which mode it is in to read an answer. Failures are
        raised with the terminal ``Reply`` attached (§4.7), so nothing is lost
        by not returning the envelope.
        """
        if self._lost:
            raise WorkerRuntimeLost(
                f"the runtime for {self._spec['worker']!r} is gone: no process to "
                "dispatch to (§4.7)",
                reply=_terminal_reply("", "runtime_lost"),
            )
        if self._outbox is None:
            raise WorkerRuntimeLost(
                f"the runtime for {self._spec['worker']!r} was never started (§3.3)",
                reply=_terminal_reply("", "not_started"),
            )
        query_id = uuid.uuid4().hex
        future: "futures.Future[Reply]" = futures.Future()
        acceptance = threading.Event()
        self._pending[query_id] = future
        self._accepted_events[query_id] = acceptance
        try:
            self._outbox.put(
                DispatchQuery(
                    query_id=query_id,
                    query=str(query),
                    timeout_seconds=float(timeout),
                )
            )
            reply = future.result(timeout=timeout)
        except futures.TimeoutError:
            # Forget the query: a reply that arrives later must not be handed
            # to the next dispatch (§4.8), and nothing else may wait on this id.
            self._pending.pop(query_id, None)
            self._accepted_events.pop(query_id, None)
            raise WorkerQueryTimeout(
                f"query {query_id} got no reply within {timeout}s (§4.4)",
                reply=_terminal_reply(query_id, "timeout"),
            ) from None
        finally:
            self._accepted_events.pop(query_id, None)
        if not reply.ok:
            raise WorkerRuntimeLost(
                f"query {query_id} failed in the runtime: "
                f"{reply.envelope.get('status')!r} (§4.4)",
                reply=reply,
            )
        envelope = reply.envelope or {}
        raw = envelope.get("response")
        if isinstance(raw, str):
            return raw
        return json.dumps(envelope)

    # ------------------------------------------------------------------
    # Control plane (§10)
    # ------------------------------------------------------------------

    def stop(self, *, timeout: float = ACK_TIMEOUT_SECONDS) -> bool:
        """Detach the worker without terminating it (§4.3).

        Returns the ``control_ack.ok`` the runtime sent. A refusal is an answer
        and not an exception -- it tells the caller the runtime is alive and
        declined -- so ``False`` here is information, not a failure.
        """
        return self._control("stop", timeout=timeout).ok

    def pause(self, *, timeout: float = ACK_TIMEOUT_SECONDS) -> bool:
        """Pause the worker's turn loop (§4.3); see :meth:`stop`."""
        return self._control("pause", timeout=timeout).ok

    def resume(self, *, timeout: float = ACK_TIMEOUT_SECONDS) -> bool:
        """Resume a paused worker (§4.3); see :meth:`stop`."""
        return self._control("resume", timeout=timeout).ok

    def kill(self, *, timeout: float = ACK_TIMEOUT_SECONDS) -> bool:
        """Kill the worker, then the runtime process (§4.3, §10).

        Two halves, in this order: ``control kill`` kills the *worker* while the
        process -- and therefore the ack channel -- is still up, and
        :meth:`close` then sends ``shutdown`` and joins the process with the same
        bounded patience it always had (§3.3).

        The return value is the ack, not a guarantee. An ack that never arrives
        is tolerated: a runtime that died while handling ``kill`` has already
        produced the outcome this call exists to produce, and :meth:`close`
        asserts the process really is gone before returning. A runtime that was
        already lost *before* the frame was sent still raises
        :class:`WorkerRuntimeLost` -- nothing was asked of it and the caller
        deserves to know -- but only after ``close`` has made sure nothing is
        left running.
        """
        acknowledged = False
        try:
            acknowledged = bool(self._control("kill", timeout=timeout).ok)
        except (WorkerRuntimeLost, WorkerQueryTimeout):
            # "No usable answer from a runtime we are killing anyway." The
            # forceful half below is what actually makes the promise true, so
            # this is not swallowed unverified: close() asserts.
            acknowledged = False
        finally:
            self.close()
        return acknowledged

    def update_config(
        self,
        config: Dict[str, Any],
        *,
        timeout: float = ACK_TIMEOUT_SECONDS,
    ) -> ConfigAck:
        """Push a config patch to the runtime's agent mailbox (§4.9, W9).

        Returns the runtime's own verdict rather than a bool, because the two
        halves of it are not interchangeable: ``ok`` says the frame was accepted
        and ``tier`` says whether the running agent can apply it in place
        (``hot_swap``) or needs a fresh runtime (``requires_respawn``). A caller
        that reports "applied" on ``ok=True`` alone is reporting something this
        side never promised -- and a worker that must be respawned anyway is a
        decision for the supervisor, not something to guess at here.

        Raises :class:`WorkerRuntimeUnsupported` when the handshake did not
        advertise ``config_update``: an old runtime ignores the frame in silence,
        so sending it would look like success and answer nothing.
        """
        if not self.supports(CAPABILITY_CONFIG_UPDATE):
            raise WorkerRuntimeUnsupported(
                f"the runtime for {self._spec['worker']!r} did not advertise "
                f"{CAPABILITY_CONFIG_UPDATE!r} in its hello: it would ignore a "
                f"config_update frame instead of answering it (§4.5)"
            )
        outbox = self._require_sendable("config_update")
        request_id = self._request_ids.next()
        slot = self._register_ack(request_id)
        outbox.put(ConfigUpdate(request_id=request_id, config=dict(config or {})))
        message = self._await_ack(
            request_id, slot, timeout, what="config_update"
        )
        return message

    def cancel(
        self,
        reason: str = CANCEL_REASON_OPERATOR_KILL,
        deadline_ms: int = CANCEL_DEADLINE_MS,
        *,
        timeout: float = ACK_TIMEOUT_SECONDS,
    ) -> str:
        """Ask the runtime to interrupt the in-flight turn (§4.3, §5.4).

        Returns the first rung of the ack ladder -- ``"accepted"`` or
        ``"refused"`` -- rather than a bool, because refusal is a verdict about
        the worker ("nothing is running") and folding it into ``False`` would
        throw away the half of the answer the caller can act on.

        Later rungs (``observed`` / ``completed``) arrive with nobody waiting and
        are dropped by :meth:`_deliver_ack` by design: cancellation here is
        latest-wins, so making the caller's latency depend on how long the turn
        takes to notice would be a latency bug wearing a thoroughness costume.
        """
        outbox = self._require_sendable("cancel")
        request_id = self._request_ids.next()
        slot = self._register_ack(request_id)
        outbox.put(
            Cancel(
                request_id=request_id,
                reason=str(reason),
                deadline_ms=int(deadline_ms),
            )
        )
        message = self._await_ack(request_id, slot, timeout, what="cancel")
        return str(message.phase)

    def ping(self, *, timeout: float = PING_TIMEOUT_SECONDS) -> bool:
        """Ask the runtime whether it is still there; never raises (§4.4).

        ``True`` only when the ``pong`` carrying *our* nonce came back within
        ``timeout``. Every other outcome -- a stream already gone, a runtime
        that was never started, a peer that stays silent, an outbox that
        refuses the frame -- is ``False``. Raising here would force the caller
        to wrap a liveness probe in the very ``try`` the probe exists to make
        unnecessary, and would dress two different *answers* ("gone", "wedged")
        up as two *errors*.

        The frame is answered by the child's frame pump, not its work loop, so
        a ``True`` means "this process is alive and reading its socket" even
        while a query is still running.
        """
        try:
            outbox = self._require_sendable("ping")
        except WorkerRuntimeLost:
            return False
        with self._acks_lock:
            self._ping_nonce += 1
            nonce = self._ping_nonce
            slot = _AckSlot()
            self._pings[nonce] = slot
        try:
            outbox.put(Ping(nonce=nonce))
        except Exception:  # noqa: BLE001 -- an unwritable outbox is "no answer"
            with self._acks_lock:
                self._pings.pop(nonce, None)
            return False
        woken = slot.event.wait(timeout)
        with self._acks_lock:
            self._pings.pop(nonce, None)
        # Woken is not answered: ``_on_eof`` closes the slot *empty*, which is
        # how a pong is told apart from the end of the stream (§4.7).
        return (
            woken
            and slot.message is not None
            and slot.message.nonce == nonce
        )

    def _control(
        self, action: str, *, timeout: float = ACK_TIMEOUT_SECONDS
    ) -> ControlAck:
        """Send one ``control`` verb and wait for exactly one ``control_ack``.

        The two failures are kept apart on purpose: a dead stream is
        :class:`WorkerRuntimeLost` (respawn, do not retry) and a live-but-silent
        runtime is :class:`WorkerQueryTimeout` (look at the worker, retry, or
        escalate to :meth:`kill`). Collapsing them would leave the caller unable
        to tell "there is nothing there" from "there is something there that is
        not answering".
        """
        outbox = self._require_sendable(f"control {action!r}")
        request_id = self._request_ids.next()
        slot = self._register_ack(request_id)
        outbox.put(Control(request_id=request_id, action=action))
        message = self._await_ack(
            request_id, slot, timeout, what=f"control {action!r}"
        )
        return message

    def _register_ack(self, request_id: str) -> _AckSlot:
        """Open a slot for *request_id* before the frame is sent (§10).

        Registered first, sent second: the ack can come back on the reader
        thread the instant the runtime sees the request, and a slot created
        after the send would miss it and cost the caller a full timeout.
        """
        slot = _AckSlot()
        with self._acks_lock:
            self._acks[request_id] = slot
        return slot

    def _await_ack(
        self,
        request_id: str,
        slot: _AckSlot,
        timeout: float,
        *,
        what: str,
    ) -> Any:
        """Wait for *slot* to close, or fail with the reason it could not (§10).

        Three ways out, and they are not interchangeable: woken with a frame
        (the ack), woken without one (the stream ended --
        :class:`WorkerRuntimeLost` now, not in ``timeout`` seconds), or never
        woken (a live runtime that did not answer --
        :class:`WorkerQueryTimeout`).
        """
        if slot.message is None and self._lost:
            with self._acks_lock:
                self._acks.pop(request_id, None)
            raise WorkerRuntimeLost(
                f"the runtime for {self._spec['worker']!r} is gone: {what} "
                f"{request_id} could not be sent (§4.7)",
                reply=_terminal_reply(request_id, "runtime_lost"),
            )
        if not slot.event.wait(timeout):
            # Forget the request: a late ack must not close a later one, and
            # nothing else may wait on this id (§10).
            with self._acks_lock:
                self._acks.pop(request_id, None)
            if self._lost:
                raise WorkerRuntimeLost(
                    f"the runtime for {self._spec['worker']!r} is gone: stream "
                    f"ended while waiting for {what} {request_id} (§4.7)",
                    reply=_terminal_reply(request_id, "runtime_lost"),
                )
            raise WorkerQueryTimeout(
                f"{what} {request_id} got no ack within {timeout}s (§4.4)",
                reply=_terminal_reply(request_id, "ack_timeout"),
            )
        if slot.message is None:
            # Woken by _on_eof: the end of the stream closed the slot empty.
            raise WorkerRuntimeLost(
                f"the runtime for {self._spec['worker']!r} is gone: stream ended "
                f"while waiting for {what} {request_id} (§4.7)",
                reply=_terminal_reply(request_id, "runtime_lost"),
            )
        return slot.message

    def _require_sendable(self, what: str) -> _Outbox:
        """The outbox, or :class:`WorkerRuntimeLost` if there is nobody to ask."""
        if self._lost:
            raise WorkerRuntimeLost(
                f"the runtime for {self._spec['worker']!r} is gone: cannot send "
                f"{what} (§4.7)",
                reply=_terminal_reply("", "runtime_lost"),
            )
        if self._outbox is None:
            raise WorkerRuntimeLost(
                f"the runtime for {self._spec['worker']!r} was never started "
                "(§3.3)",
                reply=_terminal_reply("", "not_started"),
            )
        return self._outbox

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    @property
    def pid(self) -> Optional[int]:
        """OS pid of the runtime process, or ``None`` before :meth:`start`."""
        return None if self._process is None else self._process.pid

    @property
    def alive(self) -> bool:
        """True while the runtime process is running (§4.4 ``hello``/EOF)."""
        return self._process is not None and self._process.is_alive()

    @property
    def lost(self) -> bool:
        """True once the stream ended: EOF, a protocol error, or a fatal frame."""
        return self._lost

    @property
    def hello(self) -> Optional[Hello]:
        """The runtime's ``hello`` frame, once received (§4.4)."""
        return self._hello

    @property
    def capabilities(self) -> frozenset:
        """Capability names from ``hello``; empty before the handshake (§4.5)."""
        return self._capabilities

    def supports(self, capability: str) -> bool:
        """True when the runtime advertised *capability* in its ``hello``."""
        return capability in self._capabilities

    @property
    def accepted(self) -> frozenset:
        """``query_id`` values the runtime acknowledged, for correlation checks."""
        return frozenset(self._accepted)

    # ------------------------------------------------------------------
    # Reader
    # ------------------------------------------------------------------

    def _read_loop(self) -> None:
        """Drain the socket on its own thread until it ends (§11.2 rule 1)."""
        sock = self._sock
        decoder = self._decoder
        assert sock is not None and decoder is not None  # start() ran
        try:
            while True:
                chunk = sock.recv(RECV_CHUNK_BYTES)
                if not chunk:
                    break
                for frame in decoder.feed(chunk):
                    self._route(frame)
        except (OSError, WorkerRuntimeProtocolError) as exc:
            # A decoder that refuses the stream has already decided the
            # connection is unusable (§4.5); letting the exception escape this
            # thread would skip _on_eof and leave every pending query waiting
            # out its timeout instead of failing now.
            print(
                f"worker_runtime_process: reader for {self._spec['worker']!r} "
                f"stopped: {exc}",
                file=sys.stderr,
            )
        finally:
            self._on_eof()

    def _route(self, frame: Dict[str, Any]) -> None:
        """Hand one decoded frame to its consumer (§4.4)."""
        kind = frame["kind"]
        if kind == Hello.KIND:
            self._hello = Hello.from_frame(frame)
            # W9: capabilities are read off the handshake itself. An older
            # runtime sends a hello without them, which decodes to an empty
            # tuple (§4.5) -- an empty set here is the feature-detect result,
            # not a parse failure.
            self._capabilities = frozenset(self._hello.capabilities)
            self._hello_event.set()
            return
        if kind == QueryAccepted.KIND:
            message = QueryAccepted.from_frame(frame)
            self._accepted.add(message.query_id)
            accepted = self._accepted_events.get(message.query_id)
            if accepted is not None:
                accepted.set()
            return
        if kind == Reply.KIND:
            message = Reply.from_frame(frame)
            future = self._pending.pop(message.query_id, None)
            if future is None or future.done():
                # Nothing is waiting: a reply for a timed-out query, or for one
                # this generation never sent. Dropping it is the whole point of
                # correlating on query_id rather than on arrival order (§4.8).
                print(
                    "worker_runtime_process: dropping a reply nobody is waiting "
                    f"for (query_id={message.query_id!r}) (§4.8)",
                    file=sys.stderr,
                )
                return
            future.set_result(message)
            return
        if kind == Bye.KIND:
            self._bye_seen = True
            return
        if kind == Fatal.KIND:
            self._fatal = Fatal.from_frame(frame)
            return
        if kind == ControlAck.KIND:
            self._deliver_ack(ControlAck.from_frame(frame))
            return
        if kind == CancelAck.KIND:
            self._deliver_ack(CancelAck.from_frame(frame))
            return
        if kind == ConfigAck.KIND:
            self._deliver_ack(ConfigAck.from_frame(frame))
            return
        if kind == Pong.KIND:
            self._deliver_pong(Pong.from_frame(frame))
            return
        # S4b: the supervisory frames are routed, not swallowed. The child
        # publishes ``event`` / ``heartbeat`` / ``worker_state``; each one is
        # decoded here and handed to the sink the handle installed. Decode and
        # delivery share one guard (``_deliver``) because both run on the
        # reader thread with no lock held: a frame the sink chokes on may cost
        # the stream that frame, never the connection (§4.7).
        if kind == Event.KIND:
            self._deliver(Event, frame)
            return
        if kind == Heartbeat.KIND:
            self._deliver(Heartbeat, frame)
            return
        if kind == WorkerState.KIND:
            self._deliver(WorkerState, frame)
            return
        # Every from-runtime kind the protocol declares is routed above, so a
        # *valid* frame that still lands here is one this stage has no consumer
        # for. The decoder already owns the "malformed stream" refusal (§4.5),
        # so an unexpected valid frame is a scope fact, not a protocol error:
        # log it and go on.
        print(
            f"worker_runtime_process: ignoring {kind} frame (no consumer at "
            f"this stage)",
            file=sys.stderr,
        )

    def _deliver(self, message_type: Any, frame: Dict[str, Any]) -> None:
        """Decode one supervisory frame and hand it to ``self._on_frame`` (S4b).

        Runs on the reader thread with no lock held, so the sink is free to
        call back into the runtime. A sink that raises -- or a payload this
        side cannot read -- is reported and dropped: these frames are
        observational (§4.4), and letting the exception escape ``_route`` would
        end ``_read_loop`` and mark the runtime lost over something no caller
        can act on.
        """
        sink = self._on_frame
        if sink is None:
            return
        try:
            sink(message_type.from_frame(frame))
        except Exception as exc:  # noqa: BLE001 -- the stream must survive it
            print(
                "worker_runtime_process: dropping an unreadable or unhandled "
                f"{message_type.KIND} frame: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    def _deliver_ack(self, message: Any) -> None:
        """Close the request *message* answers, if anyone is still waiting (§10).

        Runs on the reader thread, so it never blocks and never sleeps: a control
        verb that has not been called yet must not cost the stream a single
        ``recv``. An unknown id is dropped **silently** on purpose -- the later
        rungs of the cancel ladder (``observed`` / ``completed``) arrive after
        their first rung already closed the slot, and a duplicate ack is a
        runtime bug the caller cannot act on, so there is nothing truthful to
        raise and nothing useful to say.
        """
        with self._acks_lock:
            slot = self._acks.pop(message.request_id, None)
        if slot is None:
            return
        slot.message = message
        slot.event.set()

    def _deliver_pong(self, message: Pong) -> None:
        """Close the probe that *message* answers, if anyone is still waiting.

        Runs on the reader thread, so it never blocks and never sleeps. An
        unknown nonce is dropped **silently**: a pong that arrives after its
        ping timed out is the normal race between the two sides' clocks, not a
        runtime bug, and there is nothing truthful to raise about it (§10).
        """
        with self._acks_lock:
            slot = self._pings.pop(message.nonce, None)
        if slot is None:
            return
        slot.message = message
        slot.event.set()

    def _on_eof(self) -> None:
        """Fail every still-pending query with a terminal ``reply`` (§4.7)."""
        self._lost = True
        for query_id, future in list(self._pending.items()):
            if not future.done():
                future.set_result(_terminal_reply(query_id, "runtime_lost"))
        self._pending.clear()
        # A control verb waiting on an ack must fail now rather than wait out
        # its timeout: the ack can never arrive on a dead stream. The slot is
        # closed *empty*, which is how ``_await_ack`` tells "woken with no
        # frame" (lost) from "never woken" (slow).
        # An outstanding ``ping`` must fail just as fast: a probe that waits
        # out its whole timeout on a stream that has already ended would report
        # "wedged" for a runtime that is simply gone (§4.7).
        with self._acks_lock:
            orphans = list(self._acks.values())
            self._acks.clear()
            ping_orphans = list(self._pings.values())
            self._pings.clear()
        for slot in orphans + ping_orphans:
            slot.event.set()


def _terminal_reply(query_id: str, status: str) -> Reply:
    """Build the ``Reply`` that ends a query this side had to give up on.

    Synthesised rather than invented at each call site so every failure path
    hands the caller the same shape it would have got from a live runtime: an
    ``ok=False`` frame carrying a ``status`` (§4.4).
    """
    return Reply(
        query_id=query_id,
        ok=False,
        envelope={"status": status, "response": None},
        elapsed_seconds=0.0,
    )
