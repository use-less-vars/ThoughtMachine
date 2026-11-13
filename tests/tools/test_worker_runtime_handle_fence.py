"""Regression tests for the S7a-2 dispatch fence on ``WorkerRuntimeHandle``.

A handle is the supervisor's memory of ONE incarnation, so it latches the
worker's epoch (``_generation``) once, at construction, and then refuses to
dispatch -- or to drive a control verb that routes through the process runtime
-- when the world has moved on. Three kinds of evidence count:

1. the worker reports a different ``_generation`` than the latched one;
2. the worker's own ``is_alive()`` says ``False``;
3. the registry has recorded a higher epoch for the same
   ``(session_id, worker_name)`` pair.

Absence of evidence is never a fence, so hand-built doubles, workers with no
epoch and registry keys with no record all pass.

Deterministic by construction: no threads, no processes, no sleeps. Both
runtimes the fence sits in front of are plain doubles, and the worker doubles
carry a real ``int`` ``_generation`` rather than a ``MagicMock`` -- a mock
attribute is not an epoch, so mocking it would only ever exercise the type
guard (which :func:`test_non_int_generation_is_unknown_and_dispatches` covers
explicitly).
"""

from __future__ import annotations

import pytest

from tools.workspace.worker_registry import (
    WorkerRegistry,
    _coerce_epoch,
    stored_epoch_for,
)
from tools.workspace.worker_runtime import (
    MODE_PROCESS,
    MODE_THREAD,
    WORKER_RUNTIME_MODE_ENV,
    InvalidRuntimeModeError,
    StaleWorkerFence,
    WorkerRuntimeHandle,
    _epoch_or_none,
)

SESSION = "sess-fence"
WORKER = "w1"
INSTANCE = 1


class FakeWorker:
    """A thread-mode worker double whose epoch is a real ``int``."""

    def __init__(
        self,
        generation=7,
        *,
        session_id=SESSION,
        worker_name=WORKER,
        instance_id=INSTANCE,
        reply="thread reply",
    ):
        self._generation = generation
        self.session_id = session_id
        self.worker_name = worker_name
        self.instance_id = instance_id
        self.reply = reply
        self.sent: list[tuple[str, float]] = []

    def send_query(self, query: str, timeout: float = 120.0) -> str:
        self.sent.append((query, timeout))
        return self.reply


class DeadWorker(FakeWorker):
    """An unstarted/finished thread: ``is_alive()`` is ``False``, never raises."""

    def is_alive(self) -> bool:
        return False


class LiveWorker(FakeWorker):
    """A thread that still claims to be alive, so it must not trip the fence."""

    def is_alive(self) -> bool:
        return True


class FakeProcessRuntime:
    """Stands in for ``ProcessWorkerRuntime``: records, never spawns anything."""

    def __init__(self):
        self.dispatched: list[tuple[str, float]] = []
        self.stopped = 0

    def dispatch_query(self, query: str, timeout: float = 120.0) -> str:
        self.dispatched.append((query, timeout))
        return "process reply"

    def stop(self) -> bool:
        self.stopped += 1
        return True


@pytest.fixture
def registry():
    """The process-wide singleton with its worker-thread maps blanked out.

    The fence reads evidence out of ``_worker_epochs``, so a leaked record from
    another test would fence an unrelated handle; the maps are snapshotted and
    restored rather than only cleared.
    """
    wr = WorkerRegistry.get_instance()
    with wr._registry_lock:
        old_workers = dict(wr._worker_registry)
        old_epochs = dict(wr._worker_epochs)
        wr._worker_registry.clear()
        wr._worker_epochs.clear()
    try:
        yield wr
    finally:
        with wr._registry_lock:
            wr._worker_registry.clear()
            wr._worker_registry.update(old_workers)
            wr._worker_epochs.clear()
            wr._worker_epochs.update(old_epochs)


# -- 1: the epoch is latched at construction ---------------------------------


def test_epoch_is_captured_at_construction(registry):
    handle = WorkerRuntimeHandle(FakeWorker(generation=7))

    assert handle.epoch == 7
    assert handle.mode == MODE_THREAD


# -- 2: an unchanged epoch dispatches and returns the worker's reply ---------


def test_unchanged_epoch_dispatches_and_returns_the_reply(registry):
    worker = FakeWorker(generation=7)
    handle = WorkerRuntimeHandle(worker)

    assert handle.dispatch_query("hello", timeout=5.0) == "thread reply"
    assert worker.sent == [("hello", 5.0)]


def test_live_worker_does_not_trip_the_fence(registry):
    worker = LiveWorker(generation=7)
    handle = WorkerRuntimeHandle(worker)

    assert handle.dispatch_query("hello") == "thread reply"
    assert worker.sent == [("hello", 120.0)]


# -- 3: a bumped epoch fences the dispatch off entirely ----------------------


def test_bumped_epoch_raises_and_never_reaches_the_worker(registry):
    worker = FakeWorker(generation=7)
    handle = WorkerRuntimeHandle(worker)
    worker._generation = 8

    with pytest.raises(StaleWorkerFence) as excinfo:
        handle.dispatch_query("hello")

    assert worker.sent == []
    assert excinfo.value.expected_epoch == 7
    assert excinfo.value.observed_epoch == 8
    message = str(excinfo.value)
    assert "the worker's _generation moved" in message
    assert f"session_id={SESSION!r}" in message
    assert f"worker_name={WORKER!r}" in message
    assert f"instance_id={INSTANCE!r}" in message


# -- 4: a dead worker fences the dispatch off entirely ----------------------


def test_dead_worker_raises_and_never_reaches_the_worker(registry):
    worker = DeadWorker(generation=7)
    handle = WorkerRuntimeHandle(worker)

    with pytest.raises(StaleWorkerFence) as excinfo:
        handle.dispatch_query("hello")

    assert worker.sent == []
    assert "the worker is no longer alive" in str(excinfo.value)


# -- 5: registry evidence, and fail-open when there is none -----------------


def test_registry_evidence_of_a_newer_epoch_raises(registry):
    registry.register_worker(SESSION, WORKER, object(), INSTANCE, epoch=9)
    worker = FakeWorker(generation=7)
    handle = WorkerRuntimeHandle(worker)

    with pytest.raises(StaleWorkerFence) as excinfo:
        handle.dispatch_query("hello")

    assert worker.sent == []
    assert excinfo.value.expected_epoch == 7
    assert excinfo.value.observed_epoch == 9
    assert "the registry has recorded a newer epoch" in str(excinfo.value)


def test_registry_evidence_fences_a_handle_that_knows_no_epoch(registry):
    registry.register_worker(SESSION, WORKER, object(), INSTANCE, epoch=9)
    worker = FakeWorker(generation=None)
    handle = WorkerRuntimeHandle(worker)

    assert handle.epoch is None
    with pytest.raises(StaleWorkerFence) as excinfo:
        handle.dispatch_query("hello")

    assert worker.sent == []
    assert excinfo.value.expected_epoch is None
    assert excinfo.value.observed_epoch == 9


def test_no_registry_evidence_fails_open(registry):
    worker = FakeWorker(generation=7)
    handle = WorkerRuntimeHandle(worker)

    assert handle.dispatch_query("hello") == "thread reply"
    assert worker.sent == [("hello", 120.0)]


def test_stored_epoch_for_finds_a_none_session_seeded_through_register_worker(
    registry,
):
    """The reader normalizes the session exactly the way the writers do.

    ``register_worker`` keys on ``(session_id or "", worker_name, instance_id)``,
    so seeding with ``session_id=None`` stores ``""``. Seeding goes through the
    public writer on purpose: poking ``_worker_epochs`` would let the reader and
    the writer disagree about the key form and still pass.
    """
    registry.register_worker(None, WORKER, object(), INSTANCE, epoch=5)

    assert stored_epoch_for(None, WORKER) == 5
    assert stored_epoch_for("", WORKER) == 5
    assert stored_epoch_for(None, "other-worker") is None


def test_fence_sees_evidence_written_under_a_none_session(registry):
    registry.register_worker(None, WORKER, object(), INSTANCE, epoch=5)
    worker = FakeWorker(generation=1, session_id=None)
    handle = WorkerRuntimeHandle(worker, session_id=None)

    with pytest.raises(StaleWorkerFence) as excinfo:
        handle.dispatch_query("hello")

    assert worker.sent == []
    assert excinfo.value.expected_epoch == 1
    assert excinfo.value.observed_epoch == 5
    assert "the registry has recorded a newer epoch" in str(excinfo.value)


def test_fence_ignores_evidence_that_is_not_newer_under_a_none_session(registry):
    registry.register_worker(None, WORKER, object(), INSTANCE, epoch=1)
    worker = FakeWorker(generation=1, session_id=None)
    handle = WorkerRuntimeHandle(worker, session_id=None)

    assert handle.epoch == 1
    assert handle.dispatch_query("hello") == "thread reply"
    assert worker.sent == [("hello", 120.0)]


# -- 6: a non-int epoch is "unknown", never a fence -------------------------


@pytest.mark.parametrize("raw", [None, True, False, "7", 3.0])
def test_non_int_generation_is_unknown_and_dispatches(registry, raw):
    worker = FakeWorker(generation=raw)
    handle = WorkerRuntimeHandle(worker)

    assert handle.epoch is None
    assert handle.epoch == _epoch_or_none(raw) == _coerce_epoch(raw)
    assert handle.dispatch_query("hello") == "thread reply"
    assert worker.sent == [("hello", 120.0)]


# -- 7: process mode -- the fence runs before the runtime is reused ---------


def test_process_mode_bumped_epoch_fences_before_the_runtime(registry):
    worker = FakeWorker(generation=7)
    handle = WorkerRuntimeHandle(worker, mode=MODE_PROCESS)
    runtime = FakeProcessRuntime()
    handle._process_runtime = runtime

    assert handle.dispatch_query("hello", timeout=2.0) == "process reply"
    assert runtime.dispatched == [("hello", 2.0)]

    worker._generation = 8
    with pytest.raises(StaleWorkerFence):
        handle.dispatch_query("again")

    assert runtime.dispatched == [("hello", 2.0)]
    assert handle._process_runtime is runtime


# -- 8: a control verb routes through the same fence -----------------------


def test_control_verb_raises_under_a_bumped_epoch(registry):
    worker = FakeWorker(generation=7)
    handle = WorkerRuntimeHandle(worker, mode=MODE_PROCESS)
    runtime = FakeProcessRuntime()
    handle._process_runtime = runtime

    worker._generation = 8
    with pytest.raises(StaleWorkerFence):
        handle.stop()

    assert runtime.stopped == 0


# -- 9: spawn resolves the mode and carries the identity through -----------


def test_spawn_with_an_explicit_flag_resolves_to_process_and_captures_identity(registry):
    seed = FakeWorker(generation=7)

    handle = WorkerRuntimeHandle.spawn(
        seed, environ={WORKER_RUNTIME_MODE_ENV: MODE_PROCESS}
    )

    # Explicit flag -> process mode, independent of DEFAULT_RUNTIME_MODE.
    assert handle.mode == MODE_PROCESS
    assert handle.epoch == 7
    assert handle._session_id == SESSION
    assert handle._worker_name == WORKER
    assert handle._instance_id == INSTANCE


def test_spawn_process_mode_resolves_mode_and_captures_identity(registry):
    seed = FakeWorker(generation=7)

    handle = WorkerRuntimeHandle.spawn(
        seed, environ={WORKER_RUNTIME_MODE_ENV: MODE_PROCESS}
    )

    assert handle.mode == MODE_PROCESS
    assert handle.epoch == 7


def test_spawn_explicit_identity_wins_over_the_worker(registry):
    seed = FakeWorker(generation=7)

    handle = WorkerRuntimeHandle.spawn(
        seed, environ={}, session_id="other-session", worker_name="other", instance_id=4
    )

    assert handle._session_id == "other-session"
    assert handle._worker_name == "other"
    assert handle._instance_id == 4


def test_spawn_with_an_invalid_mode_fails_closed(registry):
    seed = FakeWorker(generation=7)

    with pytest.raises(InvalidRuntimeModeError):
        WorkerRuntimeHandle.spawn(seed, environ={WORKER_RUNTIME_MODE_ENV: "threads"})
