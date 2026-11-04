# tests/test_worker_runtime_ping.py
"""The cooperative liveness probe: ``ping`` out, ``pong`` back (S7c-2, §4.4).

The claim under test is narrow and specific, so the tests are too:

  * the ``pong`` is emitted by the child's *frame pump*, not by the worker's
    turn loop -- a pong therefore means "this process is alive and reading its
    socket", which is the question a supervisor asks precisely when a query is
    still running (``test_a_busy_child_answers_a_ping_from_its_frame_pump``);
  * the nonce is echoed, not regenerated -- correlation is the whole value of
    the frame (``test_two_pings_get_two_pongs_in_order_with_their_own_nonces``);
  * the supervisor side answers ``True``/``False`` and raises *nothing*, so a
    caller never has to wrap a probe in the ``try`` the probe exists to remove
    (``test_ping_says_no_instead_of_raising_when_there_is_no_runtime``).

No kill or cancel policy is wired here: this slice only proves the question
can be asked and answered. What a ``False`` *means* -- announce, respawn,
escalate to ``cancel``/``kill`` -- is the next slice's decision, and these
tests deliberately do not presuppose it.

Two harnesses, on purpose:

  * a socketpair and a thread, for the child side: the ``pong`` has to be
    observed next to the *parked* work loop, which a spawned process would
    make awkward to inspect;
  * ``get_context("spawn")`` and a real ``ProcessWorkerRuntime``, for the
    supervisor side: the probe crosses a process boundary, and the fake
    worker the spec names by ``module:attr`` has to re-resolve in the child
    exactly the way the production factory does.
"""

from __future__ import annotations

import multiprocessing
import queue
import socket
import threading
import time

import pytest

from tools.workspace.worker_runtime_main import worker_runtime_main
from tools.workspace.worker_runtime_process import (
    ProcessWorkerRuntime,
    WorkerRuntimeLost,
)
from tools.workspace.worker_runtime_protocol import (
    DIRECTION_FROM_RUNTIME,
    DIRECTION_TO_RUNTIME,
    KINDS_BY_DIRECTION,
    MESSAGE_TYPES,
    DispatchQuery,
    FrameDecodeError,
    FrameDecoder,
    FrameEmitter,
    Hello,
    Ping,
    Pong,
    QueryAccepted,
    UnexpectedFrameError,
    check_kind,
    encode_frame,
)

mp = multiprocessing.get_context("spawn")

#: The fence, fixed here so failure output stays readable (§4.2).
WORKER = "alpha"
INSTANCE = 1
EPOCH = 7

#: Generous, and only ever waited out on failure: every assertion below is
#: satisfied in milliseconds when the probe works at all.
TIMEOUT = 20.0


# --------------------------------------------------------------------------
# The fake worker: a turn loop that parks mid-query, forever
# --------------------------------------------------------------------------


def build_hung_worker():
    """A duck-typed worker whose turn loop blocks inside a query.

    Named by ``"module:attr"`` in a runtime spec so the spawned child rebuilds
    it from the factory -- the same path the production factory takes. It
    deliberately *cannot* answer anything: the whole claim under test is that
    a pong does not need the work loop, so a worker that could reply would
    prove nothing.
    """

    class _Hung(threading.Thread):
        def __init__(self) -> None:
            super().__init__(name="hung-worker", daemon=True)
            self._input_queue: "queue.Queue[object]" = queue.Queue()
            #: Set once a query has been picked up: from here on the turn loop
            #: is inside a query and cannot be the thread that answers a ping.
            self.busy = threading.Event()
            self.parked = threading.Event()
            self.status = "idle"
            self.current_task = None
            self.error = None
            self._manual_only_pause = False

        def run(self) -> None:
            query_id = self._input_queue.get()[0]
            self.status = "working"
            self.current_task = query_id
            self.busy.set()
            self.parked.wait()

        def kill(self) -> None:
            """Idempotent by contract (§4.3): only used to end a link loss."""
            self.parked.set()

    return _Hung()


def _spec() -> dict:
    """A runtime spec whose factory re-resolves in a freshly spawned child."""
    return {
        "worker": WORKER,
        "instance": INSTANCE,
        "epoch": EPOCH,
        "worker_factory": f"{__name__}:build_hung_worker",
        "worker_kwargs": {},
    }


# --------------------------------------------------------------------------
# Harness A: the child alone, on a socketpair, so its work loop is inspectable
# --------------------------------------------------------------------------


def _emitter() -> FrameEmitter:
    return FrameEmitter(DIRECTION_TO_RUNTIME, worker=WORKER, instance=INSTANCE, epoch=EPOCH)


def _child(factory):
    """Run ``worker_runtime_main`` on a thread over a socketpair.

    The child's socket is deliberately *not* closed here: it is the same fd the
    runtime thread is reading, and closing it from the test thread would look
    like a link loss rather than a test finishing.
    """
    parent_sock, child_sock = socket.socketpair()
    spec = {"worker": WORKER, "instance": INSTANCE, "epoch": EPOCH}
    threading.Thread(
        target=worker_runtime_main,
        args=(child_sock, spec),
        kwargs={"worker_factory": factory},
        name="runtime-under-test",
        daemon=True,
    ).start()
    return parent_sock


class _Frames:
    """Read decoded frames off the supervisor's end, buffering the rest."""

    def __init__(self, sock) -> None:
        self._sock = sock
        self._decoder = FrameDecoder(DIRECTION_FROM_RUNTIME)
        self._pending: "list[dict]" = []

    def next(self, timeout: float = TIMEOUT) -> dict:
        deadline = time.monotonic() + timeout
        while not self._pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("timed out waiting for a frame")
            self._sock.settimeout(remaining)
            chunk = self._sock.recv(65536)
            if not chunk:
                raise AssertionError("socket closed while a frame was due")
            self._pending.extend(self._decoder.feed(chunk))
        return self._pending.pop(0)

    def until(self, kind: str, timeout: float = TIMEOUT) -> dict:
        """The first frame of *kind*, skipping the ones that arrive before it."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(deadline - time.monotonic(), 0.01)
            frame = self.next(remaining)
            if frame["kind"] == kind:
                return frame


def _busy_child(factory_holder):
    """Start a child whose worker the caller can inspect, and park its loop."""

    def factory():
        worker = build_hung_worker()
        factory_holder.append(worker)
        return worker

    sock = _child(factory)
    frames = _Frames(sock)
    emitter = _emitter()
    frames.until(Hello.KIND)
    sock.sendall(
        encode_frame(
            emitter.emit(
                DispatchQuery(query_id="q-1", query="hold", timeout_seconds=60.0)
            )
        )
    )
    assert frames.until(QueryAccepted.KIND)["payload"]["query_id"] == "q-1"
    assert factory_holder[0].busy.wait(TIMEOUT), "the work loop never picked the query up"
    return sock, frames, emitter, factory_holder[0]


def test_a_busy_child_answers_a_ping_from_its_frame_pump():
    """The pong must not need the turn loop -- that is the whole point (§4.4)."""
    workers: list = []
    sock, frames, emitter, worker = _busy_child(workers)
    try:
        sock.sendall(encode_frame(emitter.emit(Ping(nonce=424242))))

        pong = frames.until(Pong.KIND)

        assert pong["payload"] == {"nonce": 424242}
        assert pong["kind"] == "pong"
        # Nothing about the worker moved to produce it: the turn loop is still
        # inside the query it was handed, and still alive to say so.
        assert worker.is_alive(), "the work loop finished: nothing was proven busy"
        assert worker.current_task == "q-1"
        assert worker.status == "working"
    finally:
        sock.close()


def test_two_pings_get_two_pongs_in_order_with_their_own_nonces():
    """Echoed, not regenerated: a late pong must never satisfy the next ping."""
    workers: list = []
    sock, frames, emitter, worker = _busy_child(workers)
    try:
        sock.sendall(encode_frame(emitter.emit(Ping(nonce=1))))
        sock.sendall(encode_frame(emitter.emit(Ping(nonce=2))))

        first = frames.until(Pong.KIND)
        second = frames.until(Pong.KIND)

        assert [first["payload"]["nonce"], second["payload"]["nonce"]] == [1, 2]
        assert worker.is_alive()
    finally:
        sock.close()


# --------------------------------------------------------------------------
# Protocol facts a supervisor leans on: one direction, an int nonce
# --------------------------------------------------------------------------


def test_a_pong_is_a_runtime_to_supervisor_kind():
    assert MESSAGE_TYPES[Pong.KIND] is Pong
    assert Pong.KIND in KINDS_BY_DIRECTION[DIRECTION_FROM_RUNTIME]
    assert Pong.KIND not in KINDS_BY_DIRECTION[DIRECTION_TO_RUNTIME]
    check_kind(Pong.KIND, DIRECTION_FROM_RUNTIME)
    with pytest.raises(UnexpectedFrameError, match="not part of"):
        check_kind(Pong.KIND, DIRECTION_TO_RUNTIME)


def test_a_pong_carries_its_nonce_and_nothing_else():
    frame = Pong(nonce=99).to_frame(
        seq=1, worker=WORKER, instance=INSTANCE, epoch=EPOCH
    )
    assert frame["payload"] == {"nonce": 99}
    assert Pong.from_frame(frame) == Pong(nonce=99)
    with pytest.raises(FrameDecodeError, match="missing 'nonce'"):
        Pong.from_frame({**frame, "payload": {}})


# --------------------------------------------------------------------------
# Harness B: the supervisor side, across a real spawned process
# --------------------------------------------------------------------------


def _child_process(runtime: ProcessWorkerRuntime):
    """The ``mp.Process`` a runtime owns, whatever it calls the attribute."""
    for value in vars(runtime).values():
        if isinstance(value, multiprocessing.process.BaseProcess):
            return value
    raise AssertionError("the runtime owns no multiprocessing.Process")


def _query_in_flight(runtime: ProcessWorkerRuntime):
    """Dispatch one query that can never be answered, and return its thread."""
    replies: dict = {}

    def run() -> None:
        try:
            replies["reply"] = runtime.dispatch_query("hold", timeout=60)
        except WorkerRuntimeLost:
            # The test closes the runtime with this query still in flight, and a
            # lost stream is a documented answer (§4.7), not a failure here.
            replies["lost"] = True

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + TIMEOUT
    while not runtime.accepted and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runtime.accepted, "the runtime never accepted the query"
    return thread


def test_ping_answers_while_a_query_is_still_in_flight():
    """The supervisor's question, answered by a child that is busy working."""
    runtime = ProcessWorkerRuntime(_spec())
    try:
        runtime.start()
        query = _query_in_flight(runtime)

        started = time.monotonic()
        answered = runtime.ping(timeout=TIMEOUT)
        elapsed = time.monotonic() - started

        assert answered is True
        # A round-trip, not a timeout: the probe must cost the caller its
        # deadline only when the answer is actually "no".
        assert elapsed < TIMEOUT / 2
        assert query.is_alive(), "the query was answered: the pong proved nothing"
    finally:
        runtime.close()


def test_ping_says_no_instead_of_raising_when_there_is_no_runtime():
    """Never started: ``False``, and no exception for the caller to unwrap."""
    runtime = ProcessWorkerRuntime(_spec())
    assert runtime.ping(timeout=0.1) is False


def test_ping_says_no_once_the_runtime_is_gone():
    runtime = ProcessWorkerRuntime(_spec())
    runtime.start()
    runtime.close()
    assert runtime.ping(timeout=0.1) is False


def test_ping_says_no_when_the_child_is_killed_underneath_it():
    runtime = ProcessWorkerRuntime(_spec())
    try:
        runtime.start()
        child = _child_process(runtime)
        child.kill()
        child.join(5)

        started = time.monotonic()
        assert runtime.ping(timeout=1.0) is False
        assert time.monotonic() - started < 5.0
    finally:
        runtime.close()
