# tools/workspace/worker_runtime_main.py
"""Runtime side of the worker-runtime split -- stage S2 (§3.3).

What runs here
--------------
This is the code that executes **inside the worker's own OS process** once
``WORKER_RUNTIME_MODE=process`` (§10, S2). It is deliberately small: it parks a
``WorkerThread`` in this process, reads framed ``dispatch_query`` messages off
its end of the ``AF_UNIX`` socketpair (§4.1) and writes the answer back as a
``reply``. Nothing here decides *whether* a worker should run in a process, and
nothing here owns a fence of its own -- the supervisor decided, and the fence
is read off the ``spec`` it handed over.

S2 scope is exactly ``dispatch_query`` / ``query_accepted`` / ``reply`` (§10,
S2 row). Control -- stop / pause / resume / kill -- still travels on
``command.json`` until S3, so a frame outside that set is logged and dropped
rather than half-honoured: a control path shipped before it is written is worse
than no control path at all.

Two shape rules this module exists to keep
-----------------------------------------
*The queue is the existing one.* A ``dispatch_query`` is served by putting the
same ``(query_id, query, reply_q)`` triple into the worker's *existing*
``_input_queue`` -- the single-consumer contract ``WorkerThread.run`` already
honours. The adapter adds a transport, not a second way to reach a worker;
nothing in ``WorkerThread`` is wrapped, subclassed or modified.
*The socket is never parked.* ``worker_runtime_main`` is the socket's reader and
every reply is written by a separate ``_Outbox`` thread, so a worker that is
busy inside one query cannot stop the fd from being drained (§11.2 rule 1).
``pump_query`` blocks on the worker's reply queue, never on the socket, and the
outbox is the only thing that ever calls ``sendall`` (§4.6).
"""

from __future__ import annotations

import importlib
import json
import os
import queue
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from tools.workspace.worker_runtime_protocol import (
    DIRECTION_FROM_RUNTIME,
    DIRECTION_TO_RUNTIME,
    Bye,
    Cancel,
    CancelAck,
    Control,
    ControlAck,
    DispatchQuery,
    Fatal,
    FrameDecoder,
    FrameEmitter,
    Hello,
    Ping,
    QueryAccepted,
    Reply,
    Shutdown,
    encode_frame,
)

#: Which runtime build is speaking, reported in ``hello`` (§4.4).
RUNTIME_VERSION = "s2"

#: Size of one ``recv``. Large enough that a full ``MAX_FRAME_BYTES`` frame
#: needs a handful of reads, small enough that the buffer is not a per-worker
#: megabyte: the decoder buffers the *remainder* of a partial frame anyway.
RECV_CHUNK_BYTES = 65536

#: How long ``shutdown`` gets to flush ``bye`` before the process is allowed to
#: exit. The supervisor is waiting on this frame, so dropping it on a slow
#: send would turn an orderly stop into a timeout (§4.4).
SHUTDOWN_DRAIN_SECONDS = 5.0

#: Same idea for ``fatal``: a dying runtime's last words are diagnostic, and a
#: supervisor that never receives them reports a bare EOF instead (§4.4).
FATAL_DRAIN_SECONDS = 2.0

#: Tail of a traceback that fits comfortably in one frame (§4.7).
_TRACEBACK_TAIL_CHARS = 2000

_SENTINEL = object()


class _Outbox:
    """Single writer (§4.6): one thread, one FIFO, so ``seq`` has exactly one owner.

    Every producer -- the hello, the reader's ``query_accepted``, and one
    ``pump_query`` thread per in-flight query -- hands a message to :meth:`put`
    and returns immediately; the ordering on the wire is the queue's order, and
    ``seq`` is allocated inside this one thread, which is why the emitter needs
    no coordination with any of them. ``sendall`` is called from here and
    nowhere else, so a reply that is slow to write can never block the socket
    reader.
    """

    def __init__(self, sock: Any, emitter: FrameEmitter) -> None:
        self._sock = sock
        self._emitter = emitter
        self._q: "queue.Queue[Any]" = queue.Queue()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="runtime-outbox", daemon=True
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
                # The supervisor is gone: there is no peer left to tell, and
                # every later message would fail the same way. Latch the loss
                # and drop the backlog rather than growing a queue nobody
                # drains (§4.7).
                self._closed = True
                self._discard()
                return

    def _discard(self) -> None:
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return

    def drain(self, timeout: float) -> None:
        """Flush what is already queued, then stop the writer (best effort)."""
        self.put(_SENTINEL)
        self._thread.join(timeout)


def worker_runtime_main(
    sock: Any,
    spec: Dict[str, Any],
    *,
    worker_factory: Optional[Callable[..., Any]] = None,
) -> int:
    """Run one worker runtime until the supervisor closes the socket or says bye.

    Returns ``0`` for an orderly stop (``shutdown``/``bye``, or a clean EOF) and
    ``1`` after reporting a :class:`Fatal` frame. The exit code is the only
    thing a ``multiprocessing.Process`` can report without a channel, so it is
    kept meaningful rather than always zero.

    *spec* is the picklable dict :func:`tools.workspace.worker_runtime_process.
    spec_from_worker` builds: the fence axes (``worker``/``instance``/``epoch``)
    plus the ``worker_kwargs`` the thread is constructed from. *worker_factory*
    overrides ``spec["worker_factory"]`` and exists so a test can hand in a real
    callable instead of a ``"module:attr"`` string.
    """
    worker_name = spec["worker"]
    instance = int(spec["instance"])
    epoch = int(spec["epoch"])
    outbox: Optional[_Outbox] = None
    try:
        factory = worker_factory
        if factory is None:
            factory = _resolve_factory(spec.get("worker_factory"))
        kwargs = dict(spec.get("worker_kwargs") or {})
        worker = _build_worker(factory, kwargs)
        worker.start()
        outbox = _Outbox(
            sock,
            FrameEmitter(
                DIRECTION_FROM_RUNTIME,
                worker=worker_name,
                instance=instance,
                epoch=epoch,
            ),
        )
        outbox.put(
            Hello(
                pid=os.getpid(),
                started_at=datetime.now(timezone.utc).isoformat(),
                runtime_version=RUNTIME_VERSION,
                epoch_seen=epoch,
            )
        )
        decoder = FrameDecoder(DIRECTION_TO_RUNTIME)
        while True:
            chunk = sock.recv(RECV_CHUNK_BYTES)
            if not chunk:
                # Link loss, not an orderly stop (§4.3): the supervisor is
                # gone, so nothing else will ever stop this worker.
                _kill_on_link_loss(worker)
                break
            for frame in decoder.feed(chunk):
                kind = frame["kind"]
                if kind == DispatchQuery.KIND:
                    message = DispatchQuery.from_frame(frame)
                    reply_q: "queue.Queue[Any]" = queue.Queue()
                    # The worker's own queue: the turn loop is the only
                    # consumer, and it already knows this triple's shape
                    # (§3.3, D9).
                    worker._input_queue.put(
                        (message.query_id, message.query, reply_q)
                    )
                    outbox.put(QueryAccepted(query_id=message.query_id))
                    threading.Thread(
                        target=pump_query,
                        args=(message, reply_q, outbox),
                        name=f"runtime-query-{message.query_id}",
                        daemon=True,
                    ).start()
                elif kind == Shutdown.KIND:
                    outbox.put(Bye())
                    outbox.drain(SHUTDOWN_DRAIN_SECONDS)
                    return 0
                elif kind == Control.KIND:
                    _handle_control(
                        worker, outbox, Control.from_frame(frame)
                    )
                elif kind == Cancel.KIND:
                    _handle_cancel(
                        worker, outbox, Cancel.from_frame(frame)
                    )
                elif kind == Ping.KIND:
                    # Liveness probe only: S2 has nothing to answer it with,
                    # and the frame's whole purpose is "the socket is silent
                    # otherwise" (§4.3), which our reader already disproves.
                    continue
                else:
                    print(
                        f"worker_runtime_main: ignoring {kind} frame "
                        "(not in S2 scope)",
                        file=sys.stderr,
                    )
        return 0
    except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
        reason = f"{type(exc).__name__}: {exc}"
        if outbox is None:
            print(
                f"worker_runtime_main: fatal before the outbox existed: {reason}",
                file=sys.stderr,
            )
            return 1
        try:
            outbox.put(
                Fatal(
                    reason=reason,
                    traceback_tail=traceback.format_exc()[-_TRACEBACK_TAIL_CHARS:],
                )
            )
            outbox.drain(FATAL_DRAIN_SECONDS)
        except Exception:  # noqa: BLE001 - the process is already failing
            pass
        return 1
    finally:
        try:
            sock.close()
        except OSError:
            pass


#: ``Control`` action -> the ``WorkerThread`` verb that carries it out
#: (§4.3). ``resume`` answers a bool; the other three return ``None``.
_CONTROL_VERBS = {
    "stop": "stop",
    "pause": "pause",
    "resume": "resume",
    "kill": "kill",
}


def _worker_status(worker: Any) -> str:
    """The worker's own status string, or ``""`` when it has none."""
    return str(getattr(worker, "status", "") or "")


def _handle_control(worker: Any, outbox: _Outbox, message: Control) -> None:
    """Run one ``control`` verb and answer exactly one ``control_ack``.

    Never raises: an unknown action and a verb that throws are both answered
    ``ok=False`` so the supervisor's ladder still closes, and the reader loop
    survives either (§11.2 rule 1).
    """
    verb = _CONTROL_VERBS.get(message.action)
    if verb is None:
        outbox.put(
            ControlAck(
                request_id=message.request_id,
                action=message.action,
                ok=False,
                status=f"unknown action {message.action!r}"[:200],
            )
        )
        return
    try:
        result = getattr(worker, verb)()
    except Exception as exc:  # noqa: BLE001 - answered, never swallowed
        outbox.put(
            ControlAck(
                request_id=message.request_id,
                action=message.action,
                ok=False,
                status=f"{type(exc).__name__}: {exc}"[:200],
            )
        )
        return
    outbox.put(
        ControlAck(
            request_id=message.request_id,
            action=message.action,
            ok=True if result is None else bool(result),
            status=_worker_status(worker),
        )
    )


def _handle_cancel(worker: Any, outbox: _Outbox, message: Cancel) -> None:
    """Answer the first rung of the cancel ladder without waiting (§5.4).

    The ack is queued and the reader returns -- nothing here sleeps, joins or
    waits on the worker. A worker that is demonstrably not mid-turn has
    nothing to cancel, so it is ``refused``; otherwise the thread's own
    ``_turn_cancel`` event is set (closing/latest-wins: it is an event, not a
    queue) and it is ``accepted``.

    Only this first rung is emitted in stage S3: the later ``observed`` and
    ``completed`` rungs of the same ladder (§5.4) are not implemented yet, so
    no request id is remembered and no turn-completion path closes them.
    """
    if _worker_status(worker) != "busy":
        outbox.put(CancelAck(request_id=message.request_id, phase="refused"))
        return
    try:
        worker._turn_cancel.set()
    except Exception:  # noqa: BLE001 - refused is still an honest answer
        outbox.put(CancelAck(request_id=message.request_id, phase="refused"))
        return
    outbox.put(CancelAck(request_id=message.request_id, phase="accepted"))


def _kill_on_link_loss(worker: Any) -> None:
    """Kill the worker when the supervisor's socket dies (§4.3).

    EOF means nobody is left to stop it, so a live child would be orphaned.
    Idempotent by the worker's own contract: ``kill`` on a stopped or already
    killed thread is a no-op, and any failure here is dropped because the
    link that would report it is gone.
    """
    try:
        worker.kill()
    except Exception:  # noqa: BLE001 - the link is already gone
        pass


def pump_query(
    message: DispatchQuery,
    reply_q: "queue.Queue[Any]",
    outbox: _Outbox,
) -> None:
    """Turn one worker reply into one ``reply`` frame (§4.4).

    Runs on its own thread per in-flight query, so the socket reader is free
    while the worker is thinking (§11.2 rule 1) and two concurrent queries
    never serialise on each other here -- the worker's own queue does that, if
    anything does. The wait is bounded by the caller's ``timeout_seconds``
    rather than by a local default, because that number was already chosen by
    the process that is now waiting for the answer.
    """
    started = time.monotonic()
    try:
        raw = reply_q.get(timeout=float(message.timeout_seconds))
    except queue.Empty:
        outbox.put(
            Reply(
                query_id=message.query_id,
                ok=False,
                envelope={"status": "timeout", "response": None},
                elapsed_seconds=round(time.monotonic() - started, 6),
            )
        )
        return
    envelope = _envelope_from_reply(raw)
    ok = not (isinstance(envelope, dict) and envelope.get("status") == "error")
    outbox.put(
        Reply(
            query_id=message.query_id,
            ok=ok,
            envelope=envelope,
            elapsed_seconds=round(time.monotonic() - started, 6),
        )
    )


def _envelope_from_reply(raw: Any) -> Dict[str, Any]:
    """Coerce what the worker put on its reply queue into the §4.4 envelope.

    ``WorkerThread`` replies with a JSON *string*; passing it through as
    ``{"response": <str>}`` would double-encode it, so a string that parses to
    an object is used as the envelope. Anything that is not an object becomes
    the ``response`` field verbatim, which keeps a malformed reply visible to
    the supervisor instead of silently empty.
    """
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"response": raw}
        if isinstance(parsed, dict):
            return parsed
        return {"response": raw}
    if isinstance(raw, dict):
        return raw
    return {"response": str(raw)}


def _resolve_factory(ref: Optional[str]) -> Optional[Callable[..., Any]]:
    """Resolve a ``"module:attr"`` worker factory, or ``None`` for the default.

    A factory travels as text rather than as a callable because the spec has to
    be picklable into a ``spawn`` child, and a lambda or a bound method would
    not survive the trip.
    """
    if not ref:
        return None
    module_name, _, attribute = ref.partition(":")
    if not module_name or not attribute:
        raise ValueError(
            f"worker factory {ref!r} is not of the form 'module:attr'"
        )
    module = importlib.import_module(module_name)
    return getattr(module, attribute)


def _build_worker(
    factory: Optional[Callable[..., Any]], kwargs: Dict[str, Any]
) -> Any:
    """Construct the worker thread in this process (imported lazily).

    The ``WorkerThread`` import is deferred because this module is imported by
    the supervisor too, where pulling the whole turn loop in at import time
    would drag agent, tool and container machinery into a process that only
    wants to write frames.
    """
    if factory is not None:
        return factory(**kwargs)
    from tools.workspace.worker_thread import WorkerThread

    return WorkerThread(**kwargs)
