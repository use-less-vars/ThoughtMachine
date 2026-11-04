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

from tools.workspace.worker_runtime import CANCEL_TERMINAL_STATUSES
from tools.workspace.worker_runtime_protocol import (
    CONFIG_TIER_HOT_SWAP,
    CONFIG_TIER_REQUIRES_RESPAWN,
    DIRECTION_FROM_RUNTIME,
    DIRECTION_TO_RUNTIME,
    HELLO_CAPABILITIES,
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

#: ``config_update`` carries ``AgentConfig`` field names, while the config dict a
#: ``WorkerThread`` is built from uses the ToolExecutor's injection name for the
#: provider. One alias, applied on merge, so a supervisor can speak the model's
#: vocabulary without having to know the injection's (§4.9).
_INJECTED_CONFIG_ALIASES = {"provider_type": "provider"}

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


#: How long the worker's OWN ``EventBus`` gets to appear before the wildcard
#: subscription is given up on (see :func:`_subscribe_worker_bus`). Only the
#: per-worker bus is waited for at all; the module-level one exists already.
BUS_ATTACH_TIMEOUT_SECONDS = 5.0

#: Poll interval while waiting for that bus.
BUS_ATTACH_POLL_SECONDS = 0.01


def _event_publisher(outbox: "_Outbox") -> Callable[[Any], None]:
    """Build the wildcard subscriber that turns a published event into a frame.

    ``EventBus.publish`` runs its subscribers on the PUBLISHER's stack -- the
    worker's own turn loop -- so this callback must never block, raise or do
    I/O. It hands the frame to the single-writer outbox (§4.6) and returns:
    ``encode_frame`` is called by that one thread, not here, which is what keeps
    ``seq`` in emission order. Every failure is swallowed and reported, because
    a bad frame is not worth taking a publisher down for.
    """

    def forward(event: Any) -> None:
        try:
            # ``mode="json"`` on the dump is what makes the payload frame-safe:
            # without it an enum or a datetime inside the event would only fail
            # later, inside ``json.dumps`` on the outbox thread, where the loss
            # is silent and the seq has already been spent.
            dumped = event.model_dump(mode="json")
            outbox.put(
                Event(event_type=str(dumped.get("type") or ""), data=dumped)
            )
        except Exception as exc:  # noqa: BLE001 - the publisher must not see it
            print(
                "worker_runtime_main: dropping an event frame: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    return forward


def _subscribe_worker_bus(worker: Any, publish: Callable[[Any], None]) -> None:
    """Wildcard-subscribe the worker's OWN bus, once the worker has made it.

    ``WorkerThread`` creates ``self._event_bus`` inside ``run``, not in
    ``__init__``, and it offers no hook to be told when that happened -- so the
    bus cannot be reached before the thread starts and there is nothing to
    register against. A bounded poll on a throwaway thread is the smallest thing
    that sees it without delaying the socket reader (§11.2 rule 1); a worker
    that never gets that far costs this daemon its timeout and nothing else, and
    the global bus still carries the lifecycle events.
    """
    deadline = time.monotonic() + BUS_ATTACH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        bus = getattr(worker, "event_bus", None)
        if bus is not None:
            try:
                bus.subscribe(None, publish)
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                print(
                    "worker_runtime_main: could not subscribe the worker's "
                    f"event bus: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
            return
        time.sleep(BUS_ATTACH_POLL_SECONDS)


def _subscribe_child_events(worker: Any, outbox: "_Outbox") -> None:
    """Open the child side of the event seam, on both buses it publishes to.

    One child process is one worker, so a wildcard subscription is all the
    attribution this needs: the events already carry ``worker_name``,
    ``source`` and ``session_id``, and the frame's own fence is stamped by the
    emitter. Called BEFORE ``worker.start()`` so the ``worker_spawned`` that
    ``start`` publishes is not missed -- which is only possible because the
    outbox is built before the worker too (§4.6).
    """
    publish = _event_publisher(outbox)
    try:
        from agent.events import global_event_bus
    except Exception as exc:  # noqa: BLE001 - no bus, no seam, still a worker
        print(
            "worker_runtime_main: no global event bus to subscribe: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        global_event_bus = None
    if global_event_bus is not None:
        try:
            global_event_bus.subscribe(None, publish)
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            print(
                "worker_runtime_main: could not subscribe the global event "
                f"bus: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
    threading.Thread(
        target=_subscribe_worker_bus,
        args=(worker, publish),
        name="runtime-event-subscribe",
        daemon=True,
    ).start()


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
        # The outbox exists BEFORE the worker starts (§4.6). ``start`` runs the
        # turn loop on its own thread and publishes ``worker_spawned`` on the
        # way in, so an outbox built after ``start`` returned would drop -- or,
        # worse, reorder against a later reply -- the first events of this
        # incarnation. The fence comes off the spec, which is in hand already,
        # so nothing has to be buffered to make this work.
        outbox = _Outbox(
            sock,
            FrameEmitter(
                DIRECTION_FROM_RUNTIME,
                worker=worker_name,
                instance=instance,
                epoch=epoch,
            ),
        )
        # S6b: the child side of the frame seam. ``worker_thread._heartbeat_tick``
        # already emits a ``Heartbeat`` (S5), but ``_emit_frame`` no-ops while
        # ``_frame_sink`` is None -- and nothing in this process ever installed
        # one, so a process-mode worker never put a heartbeat on the wire and the
        # supervisor's ``on_frame`` cache stayed empty. The outbox is this
        # process's only writer, so the sink is one ``put``: the fence is stamped
        # by the emitter, ``seq`` keeps its single owner (\u00a74.6), and ``put``
        # never blocks, so a tick never waits on the socket.
        worker._frame_sink = outbox.put
        _subscribe_child_events(worker, outbox)
        worker.start()
        outbox.put(
            Hello(
                pid=os.getpid(),
                started_at=datetime.now(timezone.utc).isoformat(),
                runtime_version=RUNTIME_VERSION,
                epoch_seen=epoch,
                # W9: announce the frames this build can serve. A supervisor that
                # does not see ``config_update`` here must not send it -- this
                # build would answer nothing, and silence is indistinguishable
                # from a lost frame (§4.5).
                capabilities=HELLO_CAPABILITIES,
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
                elif kind == ConfigUpdate.KIND:
                    _handle_config_update(
                        worker, outbox, ConfigUpdate.from_frame(frame)
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
    waits on the worker. A worker in a terminal status
    (:data:`CANCEL_TERMINAL_STATUSES` -- the same constant the in-process handle
    consults) has no turn left to interrupt, so it is ``refused``; otherwise the
    thread's own ``_turn_cancel`` event is set (closing/latest-wins: it is an
    event, not a queue) and it is ``accepted``.

    Only this first rung is emitted in stage S3: the later ``observed`` and
    ``completed`` rungs of the same ladder (§5.4) are not implemented yet, so
    no request id is remembered and no turn-completion path closes them.
    """
    if _worker_status(worker) in CANCEL_TERMINAL_STATUSES:
        outbox.put(CancelAck(request_id=message.request_id, phase="refused"))
        return
    try:
        worker._turn_cancel.set()
    except Exception:  # noqa: BLE001 - refused is still an honest answer
        outbox.put(CancelAck(request_id=message.request_id, phase="refused"))
        return
    outbox.put(CancelAck(request_id=message.request_id, phase="accepted"))


def _handle_config_update(
    worker: Any, outbox: _Outbox, message: ConfigUpdate
) -> None:
    """Queue one config update on the worker's agent mailbox, answer once (§4.9).

    Two destinations, and the ack names which one was used:

    *An agent exists.* ``Agent.request_config_update`` is the SAME mailbox the
    in-process path uses (``agent/core/agent.py``), so the config is applied at
    the top of the next ``process_query`` by the existing
    ``_apply_pending_config`` -- hot-swap or in-place restart, decided there. A
    second way to reach an agent would be a second set of semantics, which is
    exactly what this frame avoids.
    *No agent yet.* Creation is lazy (first turn only, ``WorkerThread.run``), so
    the patch is merged into the config dict that creation reads and nothing has
    to be replayed: the worker's first turn is built from the new values.

    The ``tier`` comes from ``agent.core.agent.config_requires_respawn`` -- the
    verdict ``Agent._can_hot_swap`` reaches -- never from
    ``AgentConfig.FIELD_CATEGORIES``. A ``requires_respawn`` ack still means the
    frame was accepted; it means "do not tell the operator this is live".

    Never raises: every failure is answered ``ok=False`` so the supervisor's
    ladder closes and the reader loop survives (§11.2 rule 1).
    """
    patch = dict(message.config or {})
    merged = dict(_injected_config_dict(worker))
    for key, value in patch.items():
        merged[_INJECTED_CONFIG_ALIASES.get(key, key)] = value

    build = getattr(worker, "_build_agent_config", None)
    if build is None or not callable(build):
        _ack_config(
            outbox,
            message,
            ok=False,
            tier=CONFIG_TIER_REQUIRES_RESPAWN,
            status="worker has no _build_agent_config: cannot build a config",
        )
        return

    try:
        # REBIND rather than mutate: the turn loop reads this dict on its own
        # thread when it builds the agent, and a fresh dict swap is atomic under
        # the GIL where a key-by-key edit is not (§11.2 rule 1).
        worker._agent_config_dict = merged
        new_config = build()
    except Exception as exc:  # noqa: BLE001 - answered, never swallowed
        _ack_config(
            outbox,
            message,
            ok=False,
            tier=CONFIG_TIER_REQUIRES_RESPAWN,
            status=f"{type(exc).__name__}: {exc}"[:200],
        )
        return

    if new_config is None:
        _ack_config(
            outbox,
            message,
            ok=False,
            tier=CONFIG_TIER_REQUIRES_RESPAWN,
            status="invalid agent_config after update "
            "(provider or model missing)",
        )
        return

    agent = getattr(worker, "_agent", None)
    if agent is None:
        # No running agent to contradict: the first turn builds one from the
        # config just merged, so no restart is needed by anybody.
        _ack_config(
            outbox,
            message,
            ok=True,
            tier=CONFIG_TIER_HOT_SWAP,
            status="stashed: no agent yet, applied when the first turn "
            "builds one",
        )
        return

    try:
        from agent.core.agent import config_requires_respawn

        requires = bool(config_requires_respawn(agent.config, new_config))
    except Exception as exc:  # noqa: BLE001 - answered, never swallowed
        _ack_config(
            outbox,
            message,
            ok=False,
            tier=CONFIG_TIER_REQUIRES_RESPAWN,
            status=f"cannot classify the change: {type(exc).__name__}: {exc}"[:200],
        )
        return

    queue_update = getattr(agent, "request_config_update", None)
    if queue_update is None or not callable(queue_update):
        _ack_config(
            outbox,
            message,
            ok=False,
            tier=CONFIG_TIER_REQUIRES_RESPAWN,
            status="agent has no request_config_update mailbox",
        )
        return
    try:
        queue_update(new_config)
    except Exception as exc:  # noqa: BLE001 - answered, never swallowed
        _ack_config(
            outbox,
            message,
            ok=False,
            tier=CONFIG_TIER_REQUIRES_RESPAWN,
            status=f"{type(exc).__name__}: {exc}"[:200],
        )
        return

    _ack_config(
        outbox,
        message,
        ok=True,
        tier=CONFIG_TIER_REQUIRES_RESPAWN if requires else CONFIG_TIER_HOT_SWAP,
        status=(
            "queued for the next turn boundary; the agent rebuilds itself there"
            if requires
            else "queued for the next turn boundary"
        ),
    )


def _injected_config_dict(worker: Any) -> Dict[str, Any]:
    """The config dict *worker* was constructed from, or ``{}`` if it has none."""
    current = getattr(worker, "_agent_config_dict", None)
    if isinstance(current, dict):
        return current
    return {}


def _ack_config(
    outbox: _Outbox,
    message: ConfigUpdate,
    *,
    ok: bool,
    tier: str,
    status: str,
) -> None:
    """Answer exactly one ``config_ack``, with the detail truncated to fit (§4.9)."""
    outbox.put(
        ConfigAck(
            request_id=message.request_id,
            ok=bool(ok),
            tier=tier,
            status=str(status)[:200],
        )
    )


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
