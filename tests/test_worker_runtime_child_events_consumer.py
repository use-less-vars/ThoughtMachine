# tests/test_worker_runtime_child_events_consumer.py
"""Parent-side proof for the event seam (§4.4, S4b).

``worker_runtime_main`` turns a published event into an ``Event`` frame;
``test_worker_runtime_child_events.py`` pins that half. This file pins the half
that carries the frame the last metre: ``ProcessWorkerRuntime._route`` decodes
``event`` / ``heartbeat`` / ``worker_state`` and hands each one to the sink it
was built with, and ``WorkerRuntimeHandle`` is that sink in production.

Three things are pinned here:

* the three supervisory kinds reach the sink as typed messages with their
  payloads intact -- they are no longer swallowed by the "no consumer" branch;
* a sink that raises (or a payload that will not decode) costs the stream that
  one frame and nothing else: ``_route`` keeps routing afterwards, and no lock
  is held while the sink runs;
* D3's ordering: the sink is bound in ``__init__``, so a frame that arrives
  BEFORE ``hello`` is delivered instead of racing the handshake. The child
  publishes ``worker_spawned`` before it puts ``hello`` on the wire, so a sink
  installed at ``start`` would drop the first event of every incarnation.

In-process only: ``_route`` is driven with frames built by ``Message.to_frame``,
i.e. exactly the dict a decoded frame is.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent import futures
from types import SimpleNamespace
from typing import Any

from tests.test_worker_runtime_process import _spec as _process_spec

from tools.workspace import worker_runtime
from tools.workspace.worker_runtime_process import ProcessWorkerRuntime
from tools.workspace.worker_runtime_protocol import (
    Event,
    Heartbeat,
    QueryAccepted,
    Reply,
    WorkerState,
)

WORKER = "alpha"
INSTANCE = 1
EPOCH = 7


def _runtime(**kwargs: Any) -> ProcessWorkerRuntime:
    """A runtime that was never started: ``_route`` is what is under test here."""
    return ProcessWorkerRuntime(
        {"worker": WORKER, "instance": INSTANCE, "epoch": EPOCH}, **kwargs
    )


def _frame(message: Any, seq: int = 1) -> dict:
    """The envelope a decoder hands ``_route`` for *message* (§4.2)."""
    return message.to_frame(seq=seq, worker=WORKER, instance=INSTANCE, epoch=EPOCH)


def _event(event_type: str = "worker_spawned", **data: Any) -> Event:
    """An ``Event`` in the shape ``worker_runtime_main`` forwards (§4.4)."""
    return Event(
        event_type=event_type,
        data={
            "type": event_type,
            "data": dict(data),
            "metadata": {"worker_name": WORKER, "source": f"worker:{WORKER}"},
        },
    )


def _heartbeat(at: str = "2026-01-01T00:00:00+00:00") -> Heartbeat:
    return Heartbeat(
        at=at, status="ready", current_context_tokens=3, max_context_tokens=8192
    )


def _worker_state() -> WorkerState:
    return WorkerState(
        status="busy", current_task="t1", error=None, paused_manually=False
    )


def test_an_event_frame_reaches_the_sink_intact():
    """§4.4: an ``event`` frame is decoded and delivered, payload and all."""
    seen: list = []
    runtime = _runtime(on_frame=seen.append)

    runtime._route(_frame(_event("worker_spawned", status="ready")))

    assert len(seen) == 1
    (message,) = seen
    assert isinstance(message, Event)
    assert message.event_type == "worker_spawned"
    assert message.data["type"] == "worker_spawned"
    assert message.data["data"] == {"status": "ready"}
    assert message.data["metadata"]["worker_name"] == WORKER


def test_heartbeat_and_worker_state_frames_are_routed_too():
    """§4.4: one sink, three kinds -- the handle tells them apart by type."""
    seen: list = []
    runtime = _runtime(on_frame=seen.append)

    runtime._route(_frame(_heartbeat(), seq=2))
    runtime._route(_frame(_worker_state(), seq=3))

    assert [type(message) for message in seen] == [Heartbeat, WorkerState]
    assert seen[0].at == "2026-01-01T00:00:00+00:00"
    assert seen[0].current_context_tokens == 3
    assert seen[1].status == "busy"
    assert seen[1].current_task == "t1"


def test_no_sink_drops_the_supervisory_frames_without_a_word(capsys):
    """The production default is silent: a handle-less runtime is not an error."""
    runtime = _runtime()

    runtime._route(_frame(_event()))
    runtime._route(_frame(_heartbeat(), seq=2))
    runtime._route(_frame(_worker_state(), seq=3))

    # These three used to hit the catch-all and be reported as out of scope;
    # a frame this stage routes and nobody consumes is not worth a line.
    assert capsys.readouterr().err == ""


def test_a_raising_sink_costs_the_frame_and_not_the_stream(capsys):
    """A sink runs on the read loop: it must not be able to end the transport."""
    delivered: list = []

    def sink(frame: Any) -> None:
        if isinstance(frame, Event):
            raise RuntimeError("forwarder blew up")
        delivered.append(frame)

    runtime = _runtime(on_frame=sink)
    query_id = "q" * 32
    runtime._pending[query_id] = pending = futures.Future()
    runtime._accepted_events[query_id] = threading.Event()

    runtime._route(_frame(_event()))
    runtime._route(_frame(_heartbeat(), seq=2))
    runtime._route(_frame(QueryAccepted(query_id=query_id), seq=3))
    runtime._route(
        _frame(
            Reply(
                query_id=query_id,
                ok=True,
                envelope={"status": "ok", "response": "hi"},
                elapsed_seconds=0.0,
            ),
            seq=4,
        )
    )

    # The event was lost to the sink; the frames behind it were not, and the
    # runtime is not "lost" because a forwarder misbehaved.
    assert [type(message) for message in delivered] == [Heartbeat]
    assert runtime.accepted == frozenset({query_id})
    assert pending.result(0).envelope["response"] == "hi"
    assert runtime.lost is False
    assert "dropping an unreadable or unhandled event frame" in capsys.readouterr().err


def test_the_sink_is_bound_before_start_so_pre_hello_events_are_delivered():
    """D3: the child says ``worker_spawned`` before ``hello``; nothing may drop it."""
    seen: list = []
    runtime = _runtime(on_frame=seen.append)

    # Constructed, never started: no process, no hello. This is exactly the
    # window the child's first event lands in.
    assert runtime._process is None
    assert runtime.pid is None
    assert runtime.hello is None

    runtime._route(_frame(_event("worker_spawned")))
    runtime._route(_frame(_event("worker_stopped"), seq=2))

    assert [message.event_type for message in seen] == [
        "worker_spawned",
        "worker_stopped",
    ]


def test_the_handle_installs_itself_as_the_sink_and_caches_the_last_event(
    monkeypatch,
):
    """§10 (S4b): the production wiring -- handle.on_frame is the runtime's sink."""
    from tools.workspace import worker_runtime_process as process_module

    captured: dict = {}

    class _FakeRuntime:
        def __init__(self, spec: dict, **kwargs: Any) -> None:
            captured["spec"] = spec
            captured["kwargs"] = kwargs

        def start(self) -> None:
            captured["started"] = True

    monkeypatch.setattr(process_module, "ProcessWorkerRuntime", _FakeRuntime)
    monkeypatch.setattr(
        process_module, "spec_from_worker", lambda worker, **kw: {"worker": WORKER}
    )
    handle = worker_runtime.WorkerRuntimeHandle(
        SimpleNamespace(), mode=worker_runtime.MODE_PROCESS
    )
    assert handle._last_event is None

    handle._ensure_process_runtime()

    assert captured["started"] is True
    # The bound method, not a copy: the runtime calls back into this handle.
    assert captured["kwargs"]["on_frame"] == handle.on_frame

    event = _event("worker_spawned")
    captured["kwargs"]["on_frame"](event)

    assert handle._last_event is event
    assert handle._last_received_at is not None


# ---------------------------------------------------------------------------
# The one test here that crosses a real process boundary. Everything above
# drives ``_route`` directly with dict frames on a runtime that was never
# started; this one spawns for real (``ProcessWorkerRuntime.start``: spawn
# context, ``AF_UNIX`` socketpair, ``hello`` handshake), lets the child publish
# a worker event on its own event bus, and reads it back off the parent-side
# sink. The spec and the ``module:attr`` factory ref come from the harness in
# ``test_worker_runtime_process.py`` -- there is no second spawn mechanism.
# ---------------------------------------------------------------------------


def build_event_publishing_worker(*, worker_name: str = WORKER):
    """The child-side worker: publishes ``worker_spawned`` as it starts.

    Resolved inside the spawned child through the spec's ``module:attr`` ref,
    the same route ``build_fake_worker`` takes -- the child imports this module
    in its own interpreter, so the bus being published on is the child's, and
    the only way the event can reach the parent is the real pipe.
    """

    from agent.events import EventType, create_event, global_event_bus

    class _Publisher:
        status = "ready"
        current_task = None
        error = None
        _manual_only_pause = False
        event_bus = None

        def __init__(self) -> None:
            self._stopped = threading.Event()

        def start(self) -> None:
            # ``worker_runtime_main`` subscribes the global bus BEFORE calling
            # this and puts ``hello`` on the wire after it returns, so this
            # publish is the first frame of the incarnation, ahead of ``hello``.
            global_event_bus.publish(
                create_event(
                    EventType.WORKER_SPAWNED,
                    {"worker_name": worker_name, "status": "ready"},
                    source=f"worker:{worker_name}",
                    session_id="s1",
                )
            )

        def stop(self) -> None:
            self._stopped.set()

        def kill(self) -> None:
            self._stopped.set()

        def join(self, timeout=None) -> None:
            self._stopped.wait(timeout)

    return _Publisher()


def test_real_child_event_reaches_parent_sink_end_to_end():
    """§4.4 end-to-end: a real child publishes, the parent-side sink receives it."""
    spec = _process_spec()
    spec["worker_factory"] = f"{__name__}:build_event_publishing_worker"
    assert spec["worker_kwargs"] == {}

    delivered: list = []
    hello_at_delivery: list = []
    ref: dict = {}

    def sink(frame: Any) -> None:
        delivered.append(frame)
        hello_at_delivery.append(ref["runtime"].hello)

    runtime = ProcessWorkerRuntime(spec, on_frame=sink)
    ref["runtime"] = runtime
    try:
        hello = runtime.start()
        assert hello.pid != os.getpid(), "the event has to cross a real process"

        deadline = time.monotonic() + 5.0
        while not delivered and time.monotonic() < deadline:
            time.sleep(0.05)

        assert delivered, "the child's event never reached the parent sink"
        (message,) = delivered
        assert isinstance(message, Event)
        assert message.event_type == "worker_spawned"
        assert message.data["data"]["worker_name"] == WORKER

        # D3: routed while ``hello`` was still unset -- the sink was attached at
        # construction, before ``start`` returned and before the handshake.
        assert hello_at_delivery == [None]
        assert runtime.hello is hello
    finally:
        runtime.close(grace_ms=50)
