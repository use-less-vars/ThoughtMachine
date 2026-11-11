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
  drop (pinned behaviourally: the real ``_action_spawn`` runs, and the fake
  thread snapshots its own ``_frame_sink`` inside ``start()``).

Harness style follows ``test_worker_runtime_child_events_consumer.py``: real
objects where they are cheap, duck-typed fakes where they are not, no sleeps.
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any, List

import tools.workspace.worker as worker_module
import tools.workspace.worker_thread as worker_thread_module
from tools.workspace.worker_registry import WorkerRegistry
from tools.workspace.worker_runtime import (
    MODE_PROCESS,
    MODE_THREAD,
    WORKER_RUNTIME_MODE_ENV,
    WorkerRuntimeHandle,
    attach_thread_frame_sink,
)
from tools.workspace.worker_runtime_process import WorkerRuntimeLost
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


class _FakeThread:
    """Stands in for ``WorkerThread``: records its ctor kwargs, never runs.

    The real attributes are set because ``_action_spawn`` reads them back
    (``instance_id``/``instance_label`` in the envelope and the registry key,
    ``_initial_context`` for the auto-query branch). ``start()`` is where the
    observation happens -- see the test below.
    """

    def __init__(self, **kwargs: Any) -> None:
        self.ctor_kwargs = kwargs
        self.name = kwargs["name"]
        self.instance_id = kwargs["instance_id"]
        self.instance_label = WorkerRegistry.instance_label(
            kwargs["name"], kwargs["instance_id"]
        )
        self.status = "ready"
        self._timeout_seconds = kwargs.get("timeout_seconds") or 30
        self.session_id = kwargs.get("session_id")
        self.workspace_id = kwargs.get("workspace_id")
        self._initial_context: dict = {}
        self.started = False
        self.sink_at_start: Any = None

    def start(self) -> None:
        # Snapshot taken INSIDE start(), i.e. at the instant the real thread
        # would begin emitting: this is the ordering proof, not a source scan.
        self.sink_at_start = getattr(self, "_frame_sink", None)
        self.started = True


def _spawn_against_a_fake_thread(
    tmp_path, monkeypatch, mode, *, thread_cls=_FakeThread, context=None
):
    """Run the REAL ``Worker._action_spawn`` with ``_FakeThread`` as the class.

    Returns ``(result, fake, key)``; the caller pops *key* from the registry.

    The mode is pinned EXPLICITLY (§10): the absent-flag default is ``process``,
    so a test that inherits it would be silently about a different mode than the
    one its name claims.
    """
    # Unique per test: the registry is a process-wide singleton, so an id no
    # other test uses keeps the spawn-cap / continue-existing routing out of it.
    session_id = f"sess-sink-spawn-{abs(hash(str(tmp_path)))}"
    created: List[_FakeThread] = []

    def _factory(**kwargs: Any) -> _FakeThread:
        fake = thread_cls(**kwargs)
        created.append(fake)
        return fake

    # Patch 1: the thread class. ``_action_spawn`` does a function-local
    # ``from tools.workspace.worker import ... WorkerThread``, so the attribute
    # to patch is on THAT module. Nothing is started: ``start()`` is ours.
    monkeypatch.setattr(worker_module, "WorkerThread", _factory)
    # Patch 2: workspace resolution. The spawn path needs a real directory (it
    # reads ``<ws>/config.json`` and tolerates only FileNotFoundError/OSError),
    # and ``_resolve_ws_dir`` short-circuits when capabilities are unavailable.
    monkeypatch.setattr(worker_module, "_workspace_dir", lambda ws_id: tmp_path)
    monkeypatch.setattr(worker_thread_module, "CAPABILITIES_AVAILABLE", True)
    # Patch 3: no ContainerManager. Constructing one probes docker; the spawn
    # site only needs it to be ``None`` (its own try/except would swallow the
    # failure, but not the cost).
    monkeypatch.setattr(worker_thread_module, "_CM", None)
    monkeypatch.setenv(WORKER_RUNTIME_MODE_ENV, mode)

    tool = Worker(
        action="spawn",
        worker_name=WORKER,
        session_id=session_id,
        workspace_path=str(tmp_path),
        agent_config={"provider": "openai", "model": "gpt-4"},
        context=context,
    )

    # A real ws_id is required: with ``None`` the path returns early at
    # "no workspace directory resolved" and never reaches the ctor.
    result = tool._action_spawn([{"name": WORKER, "status": "ready"}], "ws-sink-test")
    (fake,) = created
    return result, fake, (session_id, WORKER, 1)


def test_spawn_site_attaches_the_sink_before_starting_the_thread(tmp_path, monkeypatch):
    """S5-A: the REAL spawn path installs the sink before ``thread.start()``.

    Behavioural, not textual: ``Worker._action_spawn`` runs end to end against a
    fake thread class, and the real ``attach_thread_frame_sink`` is allowed to
    run (it only assigns two attributes, so it works on any object). If the
    attach call is deleted from the spawn site, ``sink_at_start`` is ``None``
    and this test goes red.
    """
    result, fake, key = _spawn_against_a_fake_thread(tmp_path, monkeypatch, MODE_THREAD)
    try:
        assert fake.started, "the spawn site never started the thread"
        # 1. Ordering + live wiring: the sink was already installed when
        #    start() ran. Delete the attach call and this is None.
        assert fake.sink_at_start is not None, (
            "the frame sink must be attached BEFORE thread.start(), or the "
            "first turn's worker_state/heartbeat frames are lost"
        )
        # 2. It is the handle's ``on_frame``. ``==`` not ``is``: each attribute
        #    access on a bound method builds a new object.
        assert fake._frame_sink == fake._runtime_handle.on_frame
        # 3. The spawn reported success and the registry holds THIS thread.
        assert result["spawned"] is True
        assert result["worker_name"] == WORKER
        assert result["instance_id"] == 1
        assert worker_module._worker_registry[key] is fake
    finally:
        worker_module._worker_registry.pop(key, None)


def test_spawn_site_does_not_start_the_thread_in_process_mode(tmp_path, monkeypatch):
    """Commit 8: the spawn site starts the thread ONLY in thread mode.

    In process mode the child runtime owns execution (§3.3, D7), so a
    ``thread.start()`` here would leave two run loops draining the same
    ``_output_queue``. The fake records whether ``start()`` was reached at all,
    so deleting the gate and the gate never having existed fail the same way.
    """
    result, fake, key = _spawn_against_a_fake_thread(tmp_path, monkeypatch, MODE_PROCESS)
    try:
        assert fake.started is False, (
            "the spawn site started the thread in process mode: the child "
            "runtime owns execution (§3.3, D7)"
        )
        # The mode reached the seam: the sink is the PROCESS handle's, not a
        # thread-mode one silently attached to a worker nobody will run.
        assert fake._runtime_handle.mode == MODE_PROCESS
        assert fake._frame_sink == fake._runtime_handle.on_frame
        # Dispatching in process mode is still a successful spawn: the gate
        # changes WHO executes, not whether the worker exists.
        assert result["spawned"] is True
        assert result["worker_name"] == WORKER
        assert result["instance_id"] == 1
        assert worker_module._worker_registry[key] is fake
    finally:
        worker_module._worker_registry.pop(key, None)


# ---------------------------------------------------------------------------
# 5. Ruling 1: in process mode the initial query is DISPATCHED, not waited on
# ---------------------------------------------------------------------------


class _NoDrainThread(_FakeThread):
    """A fake thread whose queue must never be drained by the supervisor.

    Process mode hands execution to the child, so nothing in this interpreter
    reads ``_output_queue`` any more. ``get()`` therefore fails the test the
    moment it is reached: waiting on a queue that only the child's run loop
    fills is exactly the hang the dispatch removes.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.queue_gets: List[Any] = []
        self._output_queue = SimpleNamespace(get=self._queue_get)

    def _queue_get(self, *args: Any, **kwargs: Any) -> Any:
        self.queue_gets.append((args, kwargs))
        raise AssertionError(
            "the spawn site drained the child-owned _output_queue in process "
            "mode: the supervisor does not run the worker body"
        )

    def _last_elapsed(self) -> None:
        # The supervisor-side thread ran no turn, so it has nothing to report.
        return None


def test_process_mode_dispatches_the_initial_query_to_the_child_runtime(
    tmp_path, monkeypatch
):
    """Ruling 1: the auto-query is dispatched through the handle, not awaited.

    With a ``query`` in the context the spawn is blocking, and in process mode
    the blocking has to happen where the work happens: ``dispatch_query`` starts
    the child runtime (its first use is the start) and returns the child's
    reply. Exactly one execution owner -- the child does not re-run
    ``_initial_context``, ``spec_from_worker`` does not carry it -- so a deleted
    dispatch call cannot hide behind the child doing the work anyway.

    The child is stubbed at ``_ensure_process_runtime`` (the real one spawns an
    OS process and handshakes); the spy is the assertion: if the spawn site
    never dispatched, nothing would have asked the handle for its runtime.
    """
    dispatched: List[Any] = []

    class _FakeRuntime:
        def dispatch_query(self, query: str, timeout: float = 0.0) -> str:
            dispatched.append((query, timeout))
            return json.dumps({"response": "child-reply", "elapsed_seconds": 1.9})

    monkeypatch.setattr(
        WorkerRuntimeHandle,
        "_ensure_process_runtime",
        lambda self: _FakeRuntime(),
    )

    result, fake, key = _spawn_against_a_fake_thread(
        tmp_path,
        monkeypatch,
        MODE_PROCESS,
        thread_cls=_NoDrainThread,
        context={"query": "the task"},
    )
    try:
        # 1. The child was started and given THE query, once, on the spawn's own
        #    wait budget.
        assert len(dispatched) == 1, (
            "the spawn site never dispatched the initial query to the child "
            "runtime: in process mode the supervisor cannot run it itself"
        )
        query, timeout = dispatched[0]
        assert query == "the task"
        assert timeout > 0
        # 2. One execution owner: the supervisor neither started the thread nor
        #    drained a queue only the child fills.
        assert fake.started is False
        assert fake.queue_gets == []
        # 3. The child's reply is the envelope -- parsed, not flattened.
        assert result["response"] == "child-reply"
        for field in (
            "worker_name",
            "spawned",
            "instance_id",
            "instance_label",
            "worker_self_reported",
            "spawn_started_at",
            "dispatch_started_at",
            "dispatch_finished_at",
            "dispatch_duration_wall",
        ):
            assert field in result, field
        assert result["worker_name"] == WORKER
        assert result["spawned"] is True
        assert result["instance_id"] == 1
        # The child's own report survives (nothing supervisor-side overwrites it
        # when there is no supervisor-side turn to report) ...
        assert result["elapsed_seconds"] == 1.9
        # ... while ``worker_self_reported`` is the thread's, and it ran nothing.
        assert result["worker_self_reported"] is None
        assert result["dispatch_duration_wall"] >= 0
        assert worker_module._worker_registry[key] is fake
    finally:
        worker_module._worker_registry.pop(key, None)


def test_process_mode_dispatch_failure_is_reported_as_a_failure(
    tmp_path, monkeypatch
):
    """Ruling 1: a raising dispatch never becomes a fabricated timeout.

    ``dispatch_query`` raises on failure (``WorkerRuntimeLost``,
    ``WorkerQueryTimeout``); the envelope must say the dispatch failed instead of
    claiming the worker "did not respond within Ns" -- the supervisor waited on
    nothing, so it has no timeout to report.
    """
    class _FailingRuntime:
        def dispatch_query(self, query: str, timeout: float = 0.0) -> str:
            # The exception carries a terminal ``reply`` for mirroring; this test
            # only cares that the raise reaches the caller as a failure.
            raise WorkerRuntimeLost(
                "child exited before replying", reply=SimpleNamespace()
            )

    monkeypatch.setattr(
        WorkerRuntimeHandle,
        "_ensure_process_runtime",
        lambda self: _FailingRuntime(),
    )

    result, fake, key = _spawn_against_a_fake_thread(
        tmp_path,
        monkeypatch,
        MODE_PROCESS,
        thread_cls=_NoDrainThread,
        context={"query": "the task"},
    )
    try:
        assert "error" in result, result
        assert "dispatch failed" in result["error"]
        assert "child exited before replying" in result["error"]
        assert "did not respond" not in json.dumps(result)
        assert "note" in result
        # Still no execution in this interpreter, on either path.
        assert fake.started is False
        assert fake.queue_gets == []
    finally:
        worker_module._worker_registry.pop(key, None)

