"""The truth about a FAILED dispatch, and about a worker that is not running.

Two halves of one honesty problem:

* a failed dispatch used to return ``{"error": str(exc), "action": ...}``, which
  forced the caller to re-diagnose the failure from a message alone -- the type,
  the stage and the substrate it happened under were all missing;
* an ``action='list'`` row reported the body's own ``status``, so a process-mode
  worker whose child never started (and never will) read ``ready`` forever.

These tests pin the envelopes that replace both. Nothing here spawns a process:
the helpers under test are pure, and the handles here are stubs carrying exactly
the three facts the envelopes read.
"""

from __future__ import annotations

from tools.workspace.worker_runtime_process import (
    WorkerIpcUnavailableError,
    WorkerStartMethodUnsupported,
)
from tools.workspace.worker_thread import (
    Worker,
    _list_entry_for,
    build_dispatch_error_envelope,
)

#: What the real refusal says, including the remedy it names.
IPC_REFUSAL_MESSAGE = (
    "the process-mode worker IPC channel could not be built: no usable socket "
    "pair exists in this interpreter (tried, in order: socketpair; loopback-tcp). "
    "This is a refusal, not a fallback. Set WORKER_RUNTIME_MODE=thread to run "
    "worker bodies in the supervisor's interpreter."
)

MANDATORY_ENVELOPE_KEYS = (
    "error",
    "error_type",
    "action",
    "runtime_mode",
    "error_stage",
    "traceback_tail",
)


class _StubHandle:
    """The handle facts an envelope reads, and nothing else."""

    def __init__(self, *, alive=None, process_start_error=None, mode=None) -> None:
        self.alive = alive
        self.process_start_error = process_start_error
        self.mode = mode


class _StubThread:
    """A registry thread as ``action='list'`` sees it."""

    def __init__(self, *, status: str = "ready", error=None) -> None:
        self.status = status
        self.current_task = None
        self.last_heartbeat = None
        self.error = error
        # No ``instance_label``: the label must then come from the registry
        # helper, exactly as it did inline before the refactor.


def _envelope_from_handler(factory, **kwargs) -> dict:
    """Build the envelope INSIDE the except handler, as the tool does.

    ``traceback_tail`` is only meaningful when an exception is being handled, so
    calling the helper from the handler is part of what is under test.
    """
    try:
        raise factory()
    except Exception as exc:  # noqa: BLE001 - the failure IS the input here
        return build_dispatch_error_envelope(exc, **kwargs)


def test_an_ipc_refusal_names_itself_and_the_stage_that_refused() -> None:
    """An IPC refusal is a REFUSAL at the ipc_channel stage -- say so.

    Before this envelope the message was all a caller got, and the one thing
    that must never be ambiguous about process mode is whether the substrate
    refused or the handler merely broke.
    """
    envelope = _envelope_from_handler(
        lambda: WorkerIpcUnavailableError(IPC_REFUSAL_MESSAGE),
        action="spawn",
        runtime_mode="process",
        handle=None,
    )

    assert envelope["error"] == IPC_REFUSAL_MESSAGE  # unchanged form
    assert envelope["error_type"] == "WorkerIpcUnavailableError"
    assert envelope["action"] == "spawn"
    assert envelope["runtime_mode"] == "process"
    assert envelope["error_stage"] == "ipc_channel"
    assert envelope["refusal"] is True
    assert "WORKER_RUNTIME_MODE=thread" in envelope["error"]

    tail = envelope["traceback_tail"]
    assert isinstance(tail, list) and tail, "a failed dispatch must carry a tail"
    assert len(tail) <= 5
    assert any("WorkerIpcUnavailableError" in line for line in tail)

    # The capability report explains the refusal, so it belongs here.
    assert set(envelope["ipc_capability"]) == {
        "af_unix_available",
        "chosen_family",
        "how",
    }


def test_an_unsupported_start_method_is_a_refusal_at_its_own_stage() -> None:
    """The other refusal type gets its own stage, not a shared "refused"."""
    envelope = _envelope_from_handler(
        lambda: WorkerStartMethodUnsupported(
            "start method 'forkserver' does not exist here (POSIX offers: fork, "
            "spawn). Set WORKER_RUNTIME_MODE=thread instead."
        ),
        action="spawn",
        runtime_mode="process",
    )

    assert envelope["error_type"] == "WorkerStartMethodUnsupported"
    assert envelope["error_stage"] == "start_method"
    assert envelope["refusal"] is True


def test_a_plain_handler_error_keeps_every_mandatory_key_without_a_handle() -> None:
    """A handler bug with NO handle still produces a complete envelope.

    This is the ordinary case on a platform where nothing was ever dispatched,
    so it must not depend on a handle existing -- and it must not claim a
    stage the failure did not happen at.
    """
    envelope = _envelope_from_handler(
        lambda: RuntimeError("boom"),
        action="query",
        runtime_mode="thread",
        handle=None,
    )

    for key in MANDATORY_ENVELOPE_KEYS:
        assert key in envelope, f"missing {key!r}"
    assert envelope["error"] == "boom"
    assert envelope["error_type"] == "RuntimeError"
    assert envelope["error_stage"] == "handler"
    assert envelope["runtime_mode"] == "thread"
    assert envelope["refusal"] is False
    assert "process_start_error" not in envelope
    # Only a substrate failure earns the capability report.
    assert "ipc_capability" not in envelope


def test_a_child_that_never_started_reports_the_runtime_start_stage() -> None:
    """A dead child turns the stage into ``runtime_start`` and names the cause.

    The exception the caller caught is a generic "could not be started"; the
    handle knows WHY, so the envelope must carry that reason rather than the
    symptom.
    """
    handle = _StubHandle(
        alive=False,
        process_start_error="WorkerIpcUnavailableError: no socket pair",
        mode="process",
    )

    envelope = _envelope_from_handler(
        lambda: RuntimeError("worker 'architect' could not be started"),
        action="spawn",
        runtime_mode="process",
        handle=handle,
    )

    assert envelope["error_stage"] == "runtime_start"
    assert envelope["process_start_error"].startswith("WorkerIpcUnavailableError:")
    assert envelope["error_type"] == "RuntimeError"
    assert envelope["refusal"] is False


def test_the_worker_tool_itself_returns_the_self_describing_envelope() -> None:
    """The wiring, not just the helper: execute() must return this envelope.

    A raised handler is the ordinary failure a caller sees (an unreadable
    ``workers.json``, a bad definition, a dead child), and what comes back must
    be the envelope above -- type, stage, traceback tail -- not the two-key
    dict it used to be.
    """
    import json

    tool = Worker(action="list", session_id="sess-dispatch-truth")
    tool._resolve_worker_workspace_id = lambda: None

    def _explode(ws_id):
        raise RuntimeError("workers.json is unreadable")

    tool._load_workers = _explode

    payload = json.loads(tool.execute())

    assert payload["error"] == "workers.json is unreadable"
    assert payload["error_type"] == "RuntimeError"
    assert payload["action"] == "list"
    assert payload["error_stage"] == "handler"
    assert payload["traceback_tail"]
    assert "runtime_mode" in payload


def test_list_reports_a_dead_process_worker_as_failed_not_ready() -> None:
    """The bug itself: a child-less process worker is NOT ``ready``.

    The body never ran, so ``thread.status`` is still its initial ``ready`` --
    a status nothing is backing. ``alive=False`` plus the start error is
    evidence that cannot be argued with, so the row says ``failed``.
    """
    thread = _StubThread(status="ready")
    handle = _StubHandle(
        alive=False,
        process_start_error="WorkerIpcUnavailableError: no socket pair",
        mode="process",
    )

    entry = _list_entry_for({"name": "architect", "provider": "anthropic"}, 2, thread, handle)

    assert entry["runtime_status"] == "failed"
    assert entry["alive"] is False
    assert entry["error"], "a failed row must say why"
    assert "WorkerIpcUnavailableError" in entry["process_start_error"]

    # Nothing that was there before may disappear.
    assert entry["name"] == "architect"
    assert entry["provider"] == "anthropic"
    assert entry["instance_id"] == 2
    assert entry["instance_label"] == "architect#2"
    assert entry["current_task"] is None
    assert entry["last_heartbeat"] is None


def test_list_keeps_an_unprobed_worker_reading_exactly_as_it_did() -> None:
    """``alive=None`` is "not probed yet", not death: the row stays ``ready``.

    Tightening ``ready`` must not turn every not-yet-dispatched worker into a
    failure -- that would break the one reading the row was always for.
    """
    entry = _list_entry_for(
        {"name": "architect"},
        1,
        _StubThread(status="ready"),
        _StubHandle(alive=None, mode="thread"),
    )

    assert entry["runtime_status"] == "ready"
    assert entry["instance_label"] == "architect"
    assert "process_start_error" not in entry
    # Liveness is still reported, resolved by the module's own predicate.
    assert entry["alive"] is True


def test_the_spawn_with_query_path_can_reuse_the_envelope_and_keep_its_own_error() -> None:
    """What ``_action_spawn`` now relies on, tested without spawning anything.

    The spawn-with-query path passes ``stage='spawn_dispatch'`` and then
    OVERWRITES ``envelope['error']`` with its own wording, so two things must
    hold: the DERIVED stage must win over the caller's stage (an IPC refusal
    that never created a child is not a dispatch failure), and ``error`` must be
    a plain string the caller can replace without disturbing the rest.

    Driving the real seam needs a full spawn (registry, body, runtime handle),
    which no unit test here can do honestly; the contract is asserted instead.
    """
    envelope = _envelope_from_handler(
        lambda: WorkerIpcUnavailableError(IPC_REFUSAL_MESSAGE),
        action="spawn",
        runtime_mode="process",
        handle=None,
        stage="spawn_dispatch",
    )

    # The refusal stage WINS over the caller-supplied stage.
    assert envelope["error_stage"] == "ipc_channel"
    assert envelope["refusal"] is True

    tail = envelope["traceback_tail"]
    assert isinstance(tail, list) and tail, "a failed dispatch must carry a tail"

    assert isinstance(envelope["error"], str)
    envelope["error"] = "Worker 'architect' spawn query failed in process mode: X: y"
    assert envelope["error_type"] == "WorkerIpcUnavailableError"
    assert envelope["action"] == "spawn"
    assert envelope["runtime_mode"] == "process"
    assert envelope["error_stage"] == "ipc_channel"
    assert envelope["refusal"] is True
    assert envelope["traceback_tail"] == tail
    assert set(envelope["ipc_capability"]) == {
        "af_unix_available",
        "chosen_family",
        "how",
    }

