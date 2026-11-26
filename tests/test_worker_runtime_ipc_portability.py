# tests/test_worker_runtime_ipc_portability.py
"""Portability proof for the process-mode worker IPC channel (§4.1).

The channel between the supervisor and the worker runtime process was built
with one unguarded call::

    parent_sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

``socket.AF_UNIX`` does not exist on every platform Python runs on, and
``socket.socketpair`` is not guaranteed to exist either. Where either is
missing, the ``AttributeError``/``OSError`` was raised in the PARENT before
``multiprocessing`` was ever asked for a child: no process, no traceback in
the envelope, just a dispatch that failed instantly for a reason nothing
recorded.

Three tests below pin the contract the factory owes:

1. a channel is built with NO ``socket.AF_UNIX`` in the ``socket`` module;
2. a channel is built when the stdlib ``socketpair`` itself refuses;
3. when nothing at all can be built, the failure is a NAMED, self-describing
   exception that names the remedy -- never a silent fallback to thread mode
   (a control that cannot do its job must refuse loudly, not become a
   different control).

The fourth test keeps the same discipline at the handle level: a start that
was ATTEMPTED and FAILED is positive evidence that this handle has no body, so
``WorkerRuntimeHandle.alive`` answers ``False`` -- not ``None`` ("no evidence")
and not ``True``.

No process is spawned anywhere in this file: every test stops at the channel or
at the runtime-construction seam, so the file runs in milliseconds and proves
the portability logic rather than the spawn.
"""

from __future__ import annotations

import multiprocessing
import socket
import sys
from types import SimpleNamespace

import pytest

from tools.workspace import worker_runtime, worker_runtime_process
from tools.workspace.worker_runtime_process import (
    ProcessWorkerRuntime,
    WorkerIpcUnavailableError,
    WorkerStartMethodUnsupported,
    _validated_start_method,
    ipc_capability,
    make_ipc_channel,
)


def _round_trip(parent: socket.socket, child: socket.socket) -> None:
    """Prove the pair is *connected* in both directions, not merely built."""
    parent.settimeout(5.0)
    child.settimeout(5.0)
    parent.sendall(b"ping")
    assert child.recv(4) == b"ping"
    child.sendall(b"pong")
    assert parent.recv(4) == b"pong"


def _close_pair(pair: "tuple[socket.socket, socket.socket]") -> None:
    for sock in pair:
        try:
            sock.close()
        except OSError:  # already closed by the assertion that failed
            pass


def test_channel_without_af_unix(monkeypatch) -> None:
    """No ``socket.AF_UNIX`` in the module: the channel must still be built.

    This is the Windows shape of the defect, reproduced on Linux by removing
    the attribute the old code hard-coded.
    """
    monkeypatch.delattr(socket, "AF_UNIX", raising=False)
    assert not hasattr(socket, "AF_UNIX")  # precondition: the defect's trigger

    parent, child = make_ipc_channel()
    try:
        _round_trip(parent, child)
    finally:
        _close_pair((parent, child))

    capability = ipc_capability()
    assert capability["af_unix_available"] is False
    assert isinstance(capability["chosen_family"], str)
    assert capability["chosen_family"] != "unknown"


def test_channel_without_stdlib_socketpair(monkeypatch) -> None:
    """The stdlib ``socketpair`` refuses: the loopback pair must carry bytes."""

    def _refuse(*args, **kwargs):
        raise OSError("socketpair unsupported")

    monkeypatch.setattr(socket, "socketpair", _refuse)

    parent, child = make_ipc_channel()
    try:
        _round_trip(parent, child)
    finally:
        _close_pair((parent, child))


def test_channel_refuses_loudly(monkeypatch) -> None:
    """Every path fails: a named exception naming cause, environment, remedy."""
    monkeypatch.delattr(socket, "AF_UNIX", raising=False)

    def _no_socketpair(*args, **kwargs):
        raise OSError("socketpair unsupported")

    def _no_socket(*args, **kwargs):
        raise OSError("socket unsupported")

    monkeypatch.setattr(socket, "socketpair", _no_socketpair)
    monkeypatch.setattr(socket, "socket", _no_socket)

    with pytest.raises(WorkerIpcUnavailableError) as caught:
        make_ipc_channel()

    message = str(caught.value)
    assert "AF_UNIX" in message
    assert sys.executable in message
    assert (socket.__file__ or "") in message
    assert sys.platform in message
    assert "WORKER_RUNTIME_MODE=thread" in message
    # The underlying cause is chained, not swallowed.
    assert isinstance(caught.value.__cause__, OSError)


def test_start_failure_is_recorded_and_not_alive(monkeypatch) -> None:
    """An attempted-and-failed start is evidence of no body: ``alive`` is False.

    Handle level only: the runtime is replaced by a spec stub and the channel
    factory is made to refuse, so no child is ever asked for. The point is the
    truth the supervisor reports afterwards -- before the fix the failure left
    no record at all and ``alive`` kept answering ``None`` ("no evidence"),
    which every caller reads as "not dead yet".
    """
    worker = SimpleNamespace(_generation=0)
    handle = worker_runtime.WorkerRuntimeHandle(worker, mode=worker_runtime.MODE_PROCESS)
    assert handle.alive is None  # precondition: no start attempted yet

    monkeypatch.setattr(
        worker_runtime_process,
        "spec_from_worker",
        lambda w, epoch=0: {
            "worker": "ipc-alpha",
            "instance": 1,
            "epoch": 0,
            "worker_factory": None,
        },
    )

    def _boom() -> "tuple[socket.socket, socket.socket]":
        raise WorkerIpcUnavailableError(
            "the process-mode worker IPC channel could not be built: "
            "socket.AF_UNIX does not exist and socket.socketpair() refused; "
            "set WORKER_RUNTIME_MODE=thread"
        )

    monkeypatch.setattr(worker_runtime_process, "make_ipc_channel", _boom)

    with pytest.raises(WorkerIpcUnavailableError):
        handle._ensure_process_runtime()

    # (a) the error is RECORDED on the handle, not just raised once...
    assert handle._process_start_attempted is True
    assert handle._process_runtime is None
    # (b) ...and exposed, so an envelope can carry it verbatim.
    assert "WorkerIpcUnavailableError" in handle.process_start_error
    assert "WORKER_RUNTIME_MODE=thread" in handle.process_start_error
    # (c) the liveness predicate now reports death evidence, not ignorance.
    assert handle.alive is False


# ── start-method validation: a diagnostic, not a new failure ──────────────────
#
# The validation exists only to turn ``get_context``'s bare ``ValueError`` into a
# message naming the platform and the methods it really has (§4.1). It must not
# become a NEW reason a start fails: a ``multiprocessing`` that cannot enumerate
# its methods (a stub, a partial build) is a missing answer, not a refusal.


def _stubbed_multiprocessing(monkeypatch, get_context) -> None:
    """Install the fake ``multiprocessing`` the replay tests install.

    Same shape, deliberately: ``types.SimpleNamespace`` carrying ONLY
    ``get_context``, so ``get_all_start_methods`` is absent -- which is exactly
    the attribute the unguarded call reached for
    (``tests/test_worker_replay_resistance.py:142-146``).
    """
    monkeypatch.setattr(
        worker_runtime_process,
        "multiprocessing",
        SimpleNamespace(get_context=get_context),
    )


def test_start_method_validation_tolerates_a_stubbed_multiprocessing(
    monkeypatch,
) -> None:
    """An un-enumerable module is not a refusal: the method passes through.

    Enumeration is a diagnostic. Where it is impossible the authority
    (``get_context``) decides, so ``start`` works exactly as it did before the
    validation existed. Before the fix this raised ``AttributeError``.
    """
    _stubbed_multiprocessing(monkeypatch, lambda method: None)

    assert _validated_start_method("spawn") == "spawn"


def test_get_context_refusal_becomes_a_named_error(monkeypatch) -> None:
    """The authority refusing is still a NAMED refusal, not a bare ValueError.

    This is the Windows ``fork`` shape reproduced through ``start()``: the
    method cannot be enumerated away, so ``get_context`` refuses, and its
    refusal is re-worded into the §4.1 message with the cause chained.
    """

    def _no_context(method: str) -> None:
        raise ValueError(f"cannot find context for {method!r}")

    _stubbed_multiprocessing(monkeypatch, _no_context)

    def _refuse_channel():
        raise AssertionError("start() must refuse before it builds a channel")

    monkeypatch.setattr(worker_runtime_process, "make_ipc_channel", _refuse_channel)

    runtime = ProcessWorkerRuntime(
        {"worker": "ipc-alpha", "instance": 1, "epoch": 0, "worker_factory": None},
        start_method="fork",
    )

    with pytest.raises(WorkerStartMethodUnsupported) as caught:
        runtime.start()

    message = str(caught.value)
    assert "fork" in message
    # Nothing could be enumerated here, so the message says THAT rather than
    # inventing an empty list of methods.
    assert "could not enumerate" in message
    assert sys.platform in message
    assert isinstance(caught.value.__cause__, ValueError)


def test_start_method_unsupported_names_the_available_methods() -> None:
    """The loud path is unchanged: an invented method lists the real ones.

    Pins that tolerating an un-enumerable module did not soften the refusal
    where enumeration DOES work.
    """
    with pytest.raises(WorkerStartMethodUnsupported) as caught:
        _validated_start_method("forkserver-but-typo")

    message = str(caught.value)
    assert "forkserver-but-typo" in message
    assert sys.platform in message
    real_methods = multiprocessing.get_all_start_methods()
    assert real_methods  # precondition: this platform names its methods
    for method in real_methods:
        assert method in message
