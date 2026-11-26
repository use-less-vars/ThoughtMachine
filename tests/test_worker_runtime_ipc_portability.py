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

import socket
import sys
from types import SimpleNamespace

import pytest

from tools.workspace import worker_runtime, worker_runtime_process
from tools.workspace.worker_runtime_process import (
    WorkerIpcUnavailableError,
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
