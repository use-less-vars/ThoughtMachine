# tests/test_worker_runtime_production_sink.py
"""S5-A: the production frame-sink construction site, wired and proven.

S4a gave ``WorkerThread`` a ``_frame_sink`` seam but nothing in production ever
installed one, so every emit was a no-op outside the tests. This file pins the
four claims that turn the seam on:

* ``attach_thread_frame_sink`` installs ONE sink -- the handle's ``on_frame`` --
  so what the worker emits is what the handle caches (no second listener);
* the heartbeat producer (``_heartbeat_tick``) now crosses that seam, with the
  protocol's four fields filled from the thread's own token accounting;
* ``_write_status_file`` emits ``WorkerState`` when a sink is present and emits
  nothing when it is not (the gate, not ``_emit_frame``'s own ``None`` check);
* the spawn site wires the sink BEFORE ``thread.start()`` -- the window the
  S4b2 test proves matters, and the one thing a future refactor can silently
  drop.

Harness style follows ``test_worker_runtime_child_events_consumer.py``: real
objects where they are cheap, duck-typed fakes where they are not, no sleeps.
"""

from __future__ import annotations

import inspect
from datetime import datetime
from types import SimpleNamespace
from typing import Any, List

from tools.workspace.worker_runtime import (
    MODE_THREAD,
    WorkerRuntimeHandle,
    attach_thread_frame_sink,
)
from tools.workspace.worker_runtime_protocol import Heartbeat, WorkerState
from tools.workspace.worker_thread import Worker, WorkerThread

WORKER = "sink-w"


def _fake_worker() -> SimpleNamespace:
    """Duck-typed worker: the wiring itself needs no ``WorkerThread`` at all."""
    return SimpleNamespace(_frame_sink=None)


def _real_worker(tmp_path) -> WorkerThread:
    """A real ``WorkerThread``, built the cheapest way ``__init__`` allows.

    Nothing runs: no thread is started, no Agent is constructed. Same recipe as
    ``test_worker_runtime_process.test_spec_from_worker_accepts_a_real_worker_thread``.
    """
    return WorkerThread(
        name=WORKER,
        definition={},
        agent_config={"provider": "openai", "model": "gpt-4"},
        workspace_dir=tmp_path,
        session_permissions={},
        instance_id=1,
    )


class _Tap:
    """Records frames while forwarding them, so a wire can be observed.

    Taps rather than replaces: the frame the assertion counts is the very frame
    the handle caches.
    """

    def __init__(self, downstream: Any) -> None:
        self.frames: List[Any] = []
        self._downstream = downstream

    def __call__(self, frame: Any) -> None:
        self.frames.append(frame)
        if self._downstream is not None:
            self._downstream(frame)


# ---------------------------------------------------------------------------
# 1. One sink: ``attach_thread_frame_sink`` is the handle's ``on_frame``
# ---------------------------------------------------------------------------


def test_attach_installs_the_handle_sink_on_the_worker():
    """S5-A: one handle, one sink, installed on the worker it describes."""
    worker = _fake_worker()

    handle = attach_thread_frame_sink(worker)

    assert isinstance(handle, WorkerRuntimeHandle)
    assert handle.mode == MODE_THREAD
    assert callable(worker._frame_sink)
    # The SAME callable, not a copy: what the worker emits is what the handle
    # caches, which is the whole point of a single seam.
    assert worker._frame_sink == handle.on_frame
    # Lifetime is tied to the worker (the handle owns no resource of its own).
    assert worker._runtime_handle is handle

    frame = WorkerState(
        status="busy", current_task="t1", error=None, paused_manually=False
    )
    worker._frame_sink(frame)

    assert handle._last_worker_state is frame
    assert handle._last_received_at is not None


# ---------------------------------------------------------------------------
# 2. The heartbeat producer crosses the seam
# ---------------------------------------------------------------------------


def test_heartbeat_tick_emits_a_heartbeat_frame_reaching_the_handle(tmp_path):
    """S5-A: the tick publishes to the bus AND emits a ``Heartbeat`` frame."""
    worker = _real_worker(tmp_path)
    handle = attach_thread_frame_sink(worker)
    tap = _Tap(worker._frame_sink)
    worker._frame_sink = tap

    # ``__init__`` seeds ``_last_heartbeat_monotonic`` at 0.0, so the FIRST
    # tick fires without waiting out ``HEARTBEAT_INTERVAL_S`` -- no sleep.
    worker._heartbeat_tick()

    (frame,) = tap.frames
    assert isinstance(frame, Heartbeat)
    # Four fields, each from the thread's own accounting -- no invented number.
    assert frame.at == worker.last_heartbeat
    assert frame.status == worker.status
    assert frame.current_context_tokens == worker.get_current_context_tokens()
    assert frame.max_context_tokens == worker.max_context_tokens

    # The same frame is what the handle cached, i.e. it travelled the seam.
    assert handle._last_heartbeat_at == datetime.fromisoformat(frame.at).timestamp()

    # A tick inside the interval is still a no-op: the emit did not change when
    # the interval gate fires.
    worker._heartbeat_tick()
    assert len(tap.frames) == 1


# ---------------------------------------------------------------------------
# 3. ``_write_status_file``: one frame with a sink, zero without
# ---------------------------------------------------------------------------


def test_status_write_emits_worker_state_only_with_a_sink(tmp_path):
    """S5-A: the S4a emit block is live in production and silent in the default."""
    # Without a sink: nothing is emitted. ``_emit_frame`` is tapped so the count
    # proves the ``if self._frame_sink is not None`` gate, not ``_emit_frame``'s
    # own no-op path.
    silent = _real_worker(tmp_path / "silent")
    silent_tap = _Tap(None)
    assert silent._frame_sink is None
    silent._emit_frame = silent_tap
    silent._write_status_file()
    assert silent_tap.frames == []

    # With the production sink: exactly one ``WorkerState``, field for field.
    live = _real_worker(tmp_path / "live")
    handle = attach_thread_frame_sink(live)
    tap = _Tap(live._frame_sink)
    live._frame_sink = tap

    live._write_status_file()

    (frame,) = tap.frames
    assert isinstance(frame, WorkerState)
    assert frame.status == live.status
    assert frame.current_task == live.current_task
    assert frame.error == live.error
    assert frame.paused_manually is bool(live._manual_only_pause)
    assert handle._last_worker_state is frame


# ---------------------------------------------------------------------------
# 4. The spawn site cannot forget the wiring
# ---------------------------------------------------------------------------


def test_spawn_site_attaches_the_sink_before_starting_the_thread():
    """S5-A: the sink is installed in the spawn window, before ``thread.start()``.

    Asserts the two anchors and their order, nothing else -- a refactor may move
    or rename everything around them, but a sink installed after the thread is
    running loses the first turn's status write and heartbeat.
    """
    source = inspect.getsource(Worker._action_spawn)
    # Comment lines are dropped first: an anchor mentioned in prose (including
    # the "attach before thread.start()" comment itself) is not a call.
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )

    attach_at = code.find("attach_thread_frame_sink(thread)")
    start_at = code.find("thread.start()")

    assert attach_at != -1, "the spawn site never attaches a frame sink"
    assert start_at != -1, "the spawn site no longer starts a thread"
    assert attach_at < start_at, (
        "the frame sink must be attached BEFORE thread.start(), or the first "
        "turn's worker_state/heartbeat frames are lost"
    )
