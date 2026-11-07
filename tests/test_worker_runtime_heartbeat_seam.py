# tests/test_worker_runtime_heartbeat_seam.py
"""The child's frame sink (S6b): a ``Heartbeat`` must cross child -> parent.

``worker_thread._heartbeat_tick`` has emitted a ``Heartbeat`` frame since S5,
but ``_emit_frame`` is a no-op while ``_frame_sink`` is ``None`` -- and
``worker_runtime_main`` never installed one, so a process-mode worker's tick
reached the global bus and stopped there: the supervisor's ``on_frame`` cache
(``_last_heartbeat_at``) stayed empty, and ``heartbeat_age_seconds`` had nothing
to answer from in exactly the mode it was written for. The child now hands
``_emit_frame`` this process's outbox.

What is pinned, in order:

* the sink is installed BEFORE ``worker.start()``, so the tick that happens on
  the way in is not the one that gets lost;
* the frame the REAL ``WorkerThread._emit_frame`` produces decodes back to a
  ``Heartbeat`` carrying the fence and the child's own ``at``;
* feeding that decoded frame to the REAL ``WorkerRuntimeHandle.on_frame`` is
  what makes ``heartbeat_age_seconds`` answer -- the parent half, not just the
  bytes on the wire;
* a tick after the handshake still crosses, which is when a heartbeat actually
  matters (``HEARTBEAT_INTERVAL_S`` apart, from a running turn loop).

In-process only (no ``multiprocessing``): a socketpair and a duck-typed worker
are all this seam needs, and ``test_worker_runtime_process.py`` owns the
cross-process proof.
"""

from __future__ import annotations

import socket
import threading
import time
from datetime import datetime

import pytest

from agent.events import EventBus
from tools.workspace.worker_runtime import WorkerRuntimeHandle, heartbeat_age_seconds
from tools.workspace.worker_runtime_main import worker_runtime_main
from tools.workspace.worker_runtime_protocol import (
    DIRECTION_FROM_RUNTIME,
    DIRECTION_TO_RUNTIME,
    FrameDecoder,
    FrameEmitter,
    Heartbeat,
    Hello,
    Shutdown,
)
from tools.workspace.worker_thread import WorkerThread


_AT = "2024-05-01T12:00:00+00:00"
_FENCE = {"worker": "alpha", "instance": 3, "epoch": 11}


@pytest.fixture(autouse=True)
def _fresh_global_bus(monkeypatch):
    """Keep the child's wildcard subscription off the process-wide singleton.

    ``_subscribe_child_events`` subscribes ``agent.events.global_event_bus``;
    left on the real one, this file would add a subscriber to every other test.
    """
    monkeypatch.setattr("agent.events.global_event_bus", EventBus())


class _TickingWorker:
    """A worker whose tick does what ``_heartbeat_tick`` ends with (S5, 3992).

    It calls the real ``WorkerThread._emit_frame``, so what arrives on the wire
    is the production emitter's output: the only thing faked here is the worker.
    ``start`` raises when no sink was installed, which turns "the child never
    wired this" into one readable failure instead of a timeout.
    """

    status = "idle"
    event_bus = None

    def __init__(self) -> None:
        self._frame_sink = None

    def start(self) -> None:
        if self._frame_sink is None:
            raise RuntimeError("the child installed no frame sink before start()")
        self.tick()

    def tick(self) -> None:
        WorkerThread._emit_frame(
            self,
            Heartbeat(
                at=_AT,
                status=self.status,
                current_context_tokens=0,
                max_context_tokens=0,
            ),
        )

    def kill(self) -> None:
        """Link-loss hook of ``worker_runtime_main``; nothing to clean up."""


def _drive(worker, spec=None):
    """Run ``worker_runtime_main`` on a socketpair; return (parent, frames, ...)."""
    parent, child = socket.socketpair()
    spec = dict(_FENCE) if spec is None else spec
    outcome = {}

    def run() -> None:
        outcome["code"] = worker_runtime_main(
            child, spec, worker_factory=lambda **kwargs: worker
        )

    runner = threading.Thread(target=run, name="runtime-main", daemon=True)
    runner.start()
    parent.settimeout(10.0)
    return parent, child, runner, outcome


def _read_until(parent, decoder, frames, predicate, timeout=10.0):
    """Read frames off the parent socket until *predicate* holds."""
    deadline = time.monotonic() + timeout
    while not predicate(frames):
        assert time.monotonic() < deadline, f"timed out; frames so far: {frames}"
        chunk = parent.recv(65536)
        assert chunk, f"socket closed; frames so far: {frames}"
        frames.extend(decoder.feed(chunk))
    return frames


def _kind_index(frames, kind):
    return [f["kind"] for f in frames].index(kind)


def test_the_frame_sink_carries_a_heartbeat_from_the_child_to_the_parent():
    worker = _TickingWorker()
    parent, child, runner, outcome = _drive(worker)
    decoder = FrameDecoder(DIRECTION_FROM_RUNTIME)
    frames: list = []
    try:
        _read_until(parent, decoder, frames, lambda f: any(
            fr["kind"] == Hello.KIND for fr in f
        ))

        assert Heartbeat.KIND in [f["kind"] for f in frames], (
            "no heartbeat frame arrived: the child never installed a frame sink"
        )
        # The tick fires inside start(), so it must precede the handshake.
        assert _kind_index(frames, Heartbeat.KIND) < _kind_index(frames, Hello.KIND)

        raw = frames[_kind_index(frames, Heartbeat.KIND)]
        assert (raw["worker"], raw["instance"], raw["epoch"]) == (
            _FENCE["worker"],
            _FENCE["instance"],
            _FENCE["epoch"],
        )
        beat = Heartbeat.from_frame(raw)
        assert beat.at == _AT
        assert beat.status == "idle"

        # The parent half: the very same frame, through the real consumer.
        handle = WorkerRuntimeHandle(object())
        handle.on_frame(beat)
        assert handle._last_heartbeat_at == pytest.approx(
            datetime.fromisoformat(_AT).timestamp()
        )
        age = heartbeat_age_seconds(
            handle._last_heartbeat_at,
            now=datetime.fromisoformat(_AT).timestamp() + 5.0,
        )
        assert age == pytest.approx(5.0)

        parent.sendall(
            FrameEmitter(DIRECTION_TO_RUNTIME, **_FENCE).encode(
                Shutdown(grace_ms=100)
            )
        )
        runner.join(10.0)
        assert outcome == {"code": 0}
    finally:
        parent.close()
        child.close()


def test_a_tick_after_the_handshake_still_crosses():
    """A running turn loop ticks long after ``start``; the sink must still work."""
    worker = _TickingWorker()
    parent, child, runner, outcome = _drive(worker)
    decoder = FrameDecoder(DIRECTION_FROM_RUNTIME)
    frames: list = []
    try:
        _read_until(parent, decoder, frames, lambda f: any(
            fr["kind"] == Hello.KIND for fr in f
        ))
        before = len(frames)

        worker.tick()  # exactly what _heartbeat_tick does on a busy/idle loop
        _read_until(
            parent,
            decoder,
            frames,
            lambda f: any(fr["kind"] == Heartbeat.KIND for fr in f[before:]),
        )

        later = [f for f in frames[before:] if f["kind"] == Heartbeat.KIND]
        assert later, "a heartbeat issued after start() never reached the parent"
        assert Heartbeat.from_frame(later[0]).at == _AT

        parent.sendall(
            FrameEmitter(DIRECTION_TO_RUNTIME, **_FENCE).encode(
                Shutdown(grace_ms=100)
            )
        )
        runner.join(10.0)
        assert outcome == {"code": 0}
    finally:
        parent.close()
        child.close()
