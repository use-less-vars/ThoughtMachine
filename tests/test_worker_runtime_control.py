# tests/test_worker_runtime_control.py
"""Unit proof for the S3 control and cancel handlers (§4.3, §5.4, §10).

The handlers are module-level functions over a duck-typed worker and outbox, so
everything here is in-process: no child is spawned, no socket is opened and no
clock is consulted beyond a one-hundredth-of-a-second ack timeout.
"""

from __future__ import annotations

import threading

import pytest

from tools.workspace import worker_runtime, worker_runtime_main
from tools.workspace.worker_runtime_main import (
    _handle_cancel,
    _handle_control,
    _kill_on_link_loss,
)
from tools.workspace.worker_runtime_process import (
    ProcessWorkerRuntime,
    WorkerQueryTimeout,
    _RequestIdSource,
)
from tools.workspace.worker_runtime_protocol import (
    Cancel,
    CancelAck,
    Control,
    ControlAck,
)


class _FakeOutbox:
    """Duck-typed stand-in for ``worker_runtime_main._Outbox`` (§10).

    The handler only ever calls ``put``, so the fake has to be nothing more
    than a list: what the reader loop would do with the frame is not this
    file's contract.
    """

    def __init__(self) -> None:
        self.messages = []

    def put(self, message: object) -> None:
        self.messages.append(message)


class _FakeWorker:
    """The narrow slice of ``WorkerThread`` the handlers actually touch (§4.3).

    ``calls`` records which verb ran -- the point of the unknown-action and
    throwing-verb tests is that *no* other verb is reached -- and ``raises``
    names one verb that must blow up.
    """

    def __init__(
        self,
        *,
        status: str = "busy",
        resume_result: object = None,
        raises: str = "",
    ) -> None:
        self.status = status
        self.calls = []
        self._resume_result = resume_result
        self._raises = raises
        self._turn_cancel = threading.Event()

    def _verb(self, name: str) -> object:
        self.calls.append(name)
        if self._raises == name:
            raise RuntimeError("boom")
        return self._resume_result if name == "resume" else None

    def stop(self) -> object:
        return self._verb("stop")

    def pause(self) -> object:
        return self._verb("pause")

    def resume(self) -> object:
        return self._verb("resume")

    def kill(self) -> object:
        return self._verb("kill")


def _bare_runtime() -> ProcessWorkerRuntime:
    """A runtime with no process behind it, carrying only ack bookkeeping.

    ``object.__new__`` is legitimate only because the reader-thread helpers
    touch nothing else: ``_register_ack``, ``_deliver_ack`` and ``_await_ack``
    read ``_acks``, ``_acks_lock`` and ``_lost`` and nothing more.
    """
    assert not hasattr(ProcessWorkerRuntime, "__slots__")
    runtime = object.__new__(ProcessWorkerRuntime)
    runtime._acks = {}
    runtime._acks_lock = threading.Lock()
    runtime._lost = False
    # Only ever read to name the failure in an error message.
    runtime._spec = {"worker": "fake-worker"}
    return runtime


def test_control_stop_emits_one_ok_ack() -> None:
    worker = _FakeWorker(status="stopping")
    outbox = _FakeOutbox()

    _handle_control(worker, outbox, Control(request_id="1", action="stop"))

    assert worker.calls == ["stop"]
    assert len(outbox.messages) == 1
    ack = outbox.messages[0]
    assert isinstance(ack, ControlAck)
    assert ack.request_id == "1"
    assert ack.action == "stop"
    assert ack.ok is True
    # The status field reports the worker's own view after the verb ran.
    assert ack.status == "stopping"


def test_control_resume_refusal_is_ok_false() -> None:
    # ``resume`` is the one verb with its own verdict; a False must cross the
    # boundary as ok=False rather than being overwritten by "the call returned".
    worker = _FakeWorker(status="burst_limit_reached", resume_result=False)
    outbox = _FakeOutbox()

    _handle_control(worker, outbox, Control(request_id="2", action="resume"))

    assert worker.calls == ["resume"]
    assert len(outbox.messages) == 1
    ack = outbox.messages[0]
    assert isinstance(ack, ControlAck)
    assert ack.action == "resume"
    assert ack.ok is False


def test_control_unknown_action_acks_not_ok() -> None:
    # Bypass the frame validator: the handler must distrust the closed table
    # rather than assume CONTROL_ACTIONS was checked upstream.
    message = Control(request_id="9", action="stop")
    object.__setattr__(message, "action", "self_destruct")
    worker = _FakeWorker()
    outbox = _FakeOutbox()

    _handle_control(worker, outbox, message)

    assert worker.calls == []
    assert len(outbox.messages) == 1
    ack = outbox.messages[0]
    assert isinstance(ack, ControlAck)
    assert ack.request_id == "9"
    assert ack.ok is False


def test_control_verb_exception_acks_not_ok() -> None:
    worker = _FakeWorker(raises="pause")
    outbox = _FakeOutbox()

    # Must not propagate: the reader loop survives a throwing verb (§11.2 rule 1).
    _handle_control(worker, outbox, Control(request_id="3", action="pause"))

    assert len(outbox.messages) == 1
    ack = outbox.messages[0]
    assert isinstance(ack, ControlAck)
    assert ack.ok is False
    assert "RuntimeError" in ack.status


#: The statuses a cancel is ACCEPTED from (S3b): each still owns a turn loop
#: with a ``_turn_cancel`` event, so there is a turn to interrupt.
_ACCEPTING_STATUSES = ("ready", "busy", "paused")

#: The statuses a cancel is REFUSED from: the turn is already over.
_REFUSING_STATUSES = ("killed", "detached", "error", "completed")


@pytest.mark.parametrize("status", _ACCEPTING_STATUSES)
def test_cancel_accepted_from_a_live_status(status: str) -> None:
    # "busy" is no longer the rule: a READY or PAUSED worker still has a turn
    # loop, so a cancel it can act on is accepted from those too.
    worker = _FakeWorker(status=status)
    outbox = _FakeOutbox()

    _handle_cancel(
        worker,
        outbox,
        Cancel(request_id="5", reason="operator_kill", deadline_ms=5000),
    )

    assert len(outbox.messages) == 1
    ack = outbox.messages[0]
    assert isinstance(ack, CancelAck)
    assert ack.request_id == "5"
    assert ack.phase == "accepted"
    # An "accepted" that did not set the token would be a lie.
    assert worker._turn_cancel.is_set() is True


@pytest.mark.parametrize("status", _REFUSING_STATUSES)
def test_cancel_refused_from_a_terminal_status(status: str) -> None:
    worker = _FakeWorker(status=status)
    outbox = _FakeOutbox()

    _handle_cancel(
        worker,
        outbox,
        Cancel(request_id="5", reason="operator_kill", deadline_ms=5000),
    )

    assert len(outbox.messages) == 1
    ack = outbox.messages[0]
    assert isinstance(ack, CancelAck)
    assert ack.request_id == "5"
    assert ack.phase == "refused"
    assert worker._turn_cancel.is_set() is False


@pytest.mark.parametrize("status", _ACCEPTING_STATUSES)
def test_handle_cancel_accepts_the_same_live_statuses(status: str) -> None:
    # Thread mode must answer exactly what the child answers, from the same set.
    worker = _FakeWorker(status=status)
    handle = worker_runtime.WorkerRuntimeHandle(worker)

    assert handle.cancel() == "accepted"
    assert worker._turn_cancel.is_set() is True


@pytest.mark.parametrize("status", _REFUSING_STATUSES)
def test_handle_cancel_refuses_the_same_terminal_statuses(status: str) -> None:
    worker = _FakeWorker(status=status)
    handle = worker_runtime.WorkerRuntimeHandle(worker)

    assert handle.cancel() == "refused"
    assert worker._turn_cancel.is_set() is False


def test_cancel_predicate_is_one_shared_constant() -> None:
    """One definition, consumed by both modes -- not two copies of the rule."""
    assert (
        worker_runtime_main.CANCEL_TERMINAL_STATUSES
        is worker_runtime.CANCEL_TERMINAL_STATUSES
    )
    assert worker_runtime.CANCEL_TERMINAL_STATUSES == frozenset(
        {"killed", "detached", "error", "completed"}
    )
    # ``stopped`` is never assigned by ``WorkerThread`` (comment-only at
    # worker_thread.py:1103), so a cancel must never be refused on it.
    assert "stopped" not in worker_runtime.CANCEL_TERMINAL_STATUSES


def test_cancel_accepted_when_worker_is_busy() -> None:
    worker = _FakeWorker(status="busy")
    outbox = _FakeOutbox()

    _handle_cancel(
        worker,
        outbox,
        Cancel(request_id="6", reason="operator_kill", deadline_ms=5000),
    )

    assert len(outbox.messages) == 1
    ack = outbox.messages[0]
    assert isinstance(ack, CancelAck)
    assert ack.request_id == "6"
    assert ack.phase == "accepted"
    # The thread's own event is the cancellation mechanism: an "accepted" that
    # did not set it would be a lie.
    assert worker._turn_cancel.is_set() is True


def test_kill_on_link_loss_swallows_worker_errors() -> None:
    worker = _FakeWorker(raises="kill")

    # The link that would report the failure is already gone, so nothing here
    # may reach the supervisor as an exception (§4.3).
    _kill_on_link_loss(worker)

    assert worker.calls == ["kill"]


def test_deliver_ack_closes_the_waiting_slot() -> None:
    runtime = _bare_runtime()
    slot = runtime._register_ack("7")

    runtime._deliver_ack(CancelAck(request_id="7", phase="accepted"))

    assert slot.message is not None
    assert slot.message.phase == "accepted"
    assert slot.event.is_set() is True
    # One ack closes one slot: the id must not be left behind for a second close.
    assert "7" not in runtime._acks


def test_deliver_ack_ignores_unknown_request_id() -> None:
    runtime = _bare_runtime()

    # The later rungs of the ladder (observed/completed) and duplicate acks
    # arrive with nobody waiting on the reader thread, which must not raise.
    runtime._deliver_ack(CancelAck(request_id="nope", phase="completed"))

    assert runtime._acks == {}


def test_request_ids_are_unique_and_monotonic() -> None:
    source = _RequestIdSource()

    ids = [source.next() for _ in range(5)]

    assert len(set(ids)) == 5
    # Increasing: a stray ack names its place in this runtime's sequence. The
    # values are single-digit here, so lexicographic order is numeric order.
    assert ids == sorted(ids)


def test_ack_timeout_raises_worker_query_timeout() -> None:
    runtime = _bare_runtime()
    slot = runtime._register_ack("11")

    with pytest.raises(WorkerQueryTimeout) as raised:
        # A live-but-silent runtime: nothing closes the slot, so the wait ends
        # as a timeout rather than as a lost runtime (§4.4).
        runtime._await_ack("11", slot, 0.01, what="control 'stop'")

    assert raised.value.reply.ok is False
    assert raised.value.reply.envelope["status"] == "ack_timeout"
    # A request nobody answered must be forgotten, or a late ack closes a later one.
    assert "11" not in runtime._acks
