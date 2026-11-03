# tests/test_worker_runtime_child_events.py
"""In-process proof for the child side of the event seam (\u00a74.4, S4b).

``worker_runtime_main`` runs inside the worker's own OS process, so the seam it
opens is a wildcard subscription on the buses that process publishes to, and one
translation: ``BaseEvent`` -> ``Event`` frame. Three things are pinned here:

* an event published on either bus reaches the outbox (the subscription is real,
  and it is a wildcard, not a per-type guess);
* what reaches the outbox is a frame the supervisor can decode back to an
  ``Event`` with the same ``event_type`` -- the frame fence included;
* a bad publisher or a bad event cannot take the publisher down, because the
  callback runs on the publisher's stack.

Plus one ordering proof: the outbox and the subscriptions exist BEFORE
``worker.start()``, so an event published inside ``start`` is not lost. That is
the race ``worker_runtime_main`` had when the outbox was built after ``start``
returned, and it is invisible unless the worker publishes on the caller's
thread -- which is what ``_PublishingOnStartWorker`` below does.

In-process only (no ``multiprocessing``): the seam is thread-and-object shaped,
and the cross-process proof already lives in ``test_worker_runtime_process.py``.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from agent.events import EventBus, EventType, create_event
from tools.workspace.worker_runtime_main import (
    _event_publisher,
    _subscribe_child_events,
    worker_runtime_main,
)
from tools.workspace.worker_runtime_protocol import (
    DIRECTION_FROM_RUNTIME,
    DIRECTION_TO_RUNTIME,
    Event,
    FrameDecoder,
    FrameEmitter,
    Hello,
    Shutdown,
    encode_frame,
)


class _RecordingOutbox:
    """Stands in for ``_Outbox``: records the messages instead of sending them."""

    def __init__(self) -> None:
        self.messages: list = []

    def put(self, message) -> None:
        self.messages.append(message)


class _BusWorker:
    """Duck-typed worker: ``event_bus`` is the only attribute this seam reads."""

    def __init__(self, event_bus=None) -> None:
        self.event_bus = event_bus


@pytest.fixture
def bus(monkeypatch):
    """A fresh global bus -- the real one is a process-wide singleton, and a
    subscription left on it would leak into every later test."""
    fresh = EventBus()
    monkeypatch.setattr("agent.events.global_event_bus", fresh)
    return fresh


def _spawned(worker_name: str = "alpha"):
    """The event ``WorkerThread.start`` publishes, in its real shape."""
    return create_event(
        EventType.WORKER_SPAWNED,
        {"worker_name": worker_name, "status": "ready"},
        source=f"worker:{worker_name}",
        session_id="s1",
    )


def _await_subscriber(event_bus, timeout: float = 2.0) -> bool:
    """Wait for the attach daemon to register, bounded."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if event_bus._wildcard_subscribers:
            return True
        time.sleep(0.005)
    return False


def test_a_wildcard_subscriber_on_the_global_bus_sees_a_published_event(bus):
    """\u00a74.4: one wildcard subscription, every event the child publishes."""
    outbox = _RecordingOutbox()

    _subscribe_child_events(_BusWorker(), outbox)
    bus.publish(_spawned())

    assert [m.event_type for m in outbox.messages] == ["worker_spawned"]


def test_the_workers_own_bus_is_subscribed_once_the_worker_makes_it(bus):
    """``WorkerThread`` creates its bus inside ``run``, so the seam must wait."""
    outbox = _RecordingOutbox()
    worker = _BusWorker()
    _subscribe_child_events(worker, outbox)

    own = EventBus()
    worker.event_bus = own  # "the thread got this far"
    assert _await_subscriber(own)

    own.publish(_spawned("gamma"))

    assert [m.event_type for m in outbox.messages] == ["worker_spawned"]


def test_the_emitted_frame_decodes_back_to_the_published_event(bus):
    """\u00a74.4: the payload is json-safe and round-trips as an ``Event``."""
    outbox = _RecordingOutbox()
    _subscribe_child_events(_BusWorker(), outbox)

    bus.publish(_spawned("beta"))
    (message,) = outbox.messages

    # The outbox thread is what encodes (\u00a74.6); this is that one line, so the
    # assertion is about the wire shape rather than about a dataclass.
    emitter = FrameEmitter(
        DIRECTION_FROM_RUNTIME, worker="beta", instance=1, epoch=7
    )
    (frame,) = FrameDecoder(DIRECTION_FROM_RUNTIME).feed(
        encode_frame(emitter.emit(message))
    )
    back = Event.from_frame(frame)

    assert back.event_type == "worker_spawned"
    assert (frame["worker"], frame["instance"], frame["epoch"]) == ("beta", 1, 7)
    assert back.data["type"] == "worker_spawned"
    assert back.data["data"]["worker_name"] == "beta"
    assert back.data["metadata"]["source"] == "worker:beta"
    assert back.data["metadata"]["session_id"] == "s1"


def test_a_raising_subscriber_cannot_escape_publish(bus):
    """The seam's callback runs on the publisher's stack: publish must survive it."""
    outbox = _RecordingOutbox()
    _subscribe_child_events(_BusWorker(), outbox)

    def explode(event):
        raise RuntimeError("subscriber blew up")

    bus.subscribe(EventType.WORKER_SPAWNED, explode)
    bus.publish(_spawned())  # must not raise

    assert [m.event_type for m in outbox.messages] == ["worker_spawned"]


def test_the_frame_callback_swallows_what_it_cannot_translate(capsys):
    """A non-event on the bus is reported, not raised, and not framed."""
    outbox = _RecordingOutbox()

    _event_publisher(outbox)(object())

    assert outbox.messages == []
    assert "dropping an event frame" in capsys.readouterr().err


class _PublishingOnStartWorker:
    """A worker that publishes inside ``start()``, on the caller's thread.

    Exactly the window the old ordering left open: with the outbox and the
    subscriptions created after ``start`` returned, this event had nowhere to
    go and was silently dropped.
    """

    status = "ready"
    event_bus = None

    def __init__(self, event_bus, event) -> None:
        self._bus = event_bus
        self._event = event

    def start(self) -> None:
        self._bus.publish(self._event)


def test_the_outbox_is_live_before_the_worker_starts(bus):
    """The race, pinned: an event published inside ``start`` still reaches the wire."""
    parent, child = socket.socketpair()
    worker = _PublishingOnStartWorker(bus, _spawned())
    outcome = {}

    def drive() -> None:
        outcome["code"] = worker_runtime_main(
            child,
            {"worker": "alpha", "instance": 1, "epoch": 7},
            worker_factory=lambda **kwargs: worker,
        )

    runner = threading.Thread(target=drive, name="runtime-main", daemon=True)
    runner.start()
    decoder = FrameDecoder(DIRECTION_FROM_RUNTIME)
    frames: list = []
    try:
        parent.settimeout(10.0)
        while not any(f["kind"] == Hello.KIND for f in frames):
            chunk = parent.recv(65536)
            assert chunk, "socket closed before hello"
            frames.extend(decoder.feed(chunk))

        kinds = [f["kind"] for f in frames]
        assert Event.KIND in kinds, "the event published inside start() was lost"
        assert kinds.index(Event.KIND) < kinds.index(Hello.KIND)
        assert Event.from_frame(frames[kinds.index(Event.KIND)]).data["data"][
            "worker_name"
        ] == "alpha"

        emitter = FrameEmitter(
            DIRECTION_TO_RUNTIME, worker="alpha", instance=1, epoch=7
        )
        parent.sendall(emitter.encode(Shutdown(grace_ms=100)))
        runner.join(10.0)
        assert outcome["code"] == 0
    finally:
        parent.close()
        child.close()
