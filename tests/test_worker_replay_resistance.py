# tests/test_worker_replay_resistance.py
"""Replay resistance for the worker-runtime handshake (§4.7).

``hello`` already asserts two things about the process that sent it: its
``pid`` is the one the supervisor spawned, and its ``epoch_seen`` is the epoch
the supervisor asked for. Neither proves the greeting is *fresh*. A pid is
recycled by the OS, and the epoch is deliberately reused across a respawn from
a stale spec (that reuse is what makes adoption work), so a frame captured from
an earlier incarnation can satisfy both checks and be accepted as the runtime
that was just spawned.

``incarnation_id`` closes that hole: the supervisor mints a nonce per spawn,
puts it on the spec it hands the child, and refuses any hello that does not
echo it back. The child can only know the value by reading the spec it was
given, so an echo is evidence that the frame came from this spawn -- which no
replay can forge.

The first test spawns a real child, because it is the child's echo that is
under test. The refusal cases script the hello bytes instead: a real runtime
cannot be asked to greet with somebody else's nonce, and waiting 30 s for a
timeout is not a test.
"""

from __future__ import annotations

import os
import pickle
import types

import pytest

from tools.workspace import worker_runtime_process as process_module
from tools.workspace.worker_runtime_process import (
    ProcessWorkerRuntime,
    WorkerRuntimeLost,
    spec_from_worker,
)
from tools.workspace.worker_runtime_protocol import Hello, encode_frame

#: The nonce this spawn was issued. Hex, like a real ``uuid4().hex``.
SPAWN_NONCE = "0123456789abcdef0123456789abcdef"

#: The nonce an EARLIER spawn of the same worker/instance/epoch was issued. All
#: three fence axes match on purpose: that is exactly the frame replay buys you.
STALE_NONCE = "fedcba9876543210fedcba9876543210"

#: ``worker_factory`` is a module:attr string, so the child re-resolves it the
#: way it re-resolves any factory. The sibling module's puppet is reused rather
#: than copied: it is already the worker every process-mode test in this suite
#: runs, and a second copy would only be a second thing to drift.
PUPPET = "tests.test_worker_runtime_process:build_fake_worker"


def _spec(**overrides: object) -> dict:
    """A runtime spec shaped like ``spec_from_worker``'s, nonce included."""
    spec = {
        "worker": "alpha",
        "instance": 1,
        "epoch": 7,
        "incarnation_id": SPAWN_NONCE,
        "worker_factory": PUPPET,
        "worker_kwargs": {},
    }
    spec.update(overrides)
    return spec


def test_a_child_echoing_the_issued_nonce_is_accepted():
    """§4.7: the happy path -- the spawned child echoes and the runtime starts."""
    runtime = ProcessWorkerRuntime(_spec())
    try:
        hello = runtime.start()
        assert hello.incarnation_id == SPAWN_NONCE
        # Not just "some nonce came back": it is the one this spawn issued.
        assert hello.incarnation_id == runtime._spec["incarnation_id"]
    finally:
        runtime.close(grace_ms=50)


# ── scripted greetings ──────────────────────────────────────────────────────


def _hello_bytes(*, incarnation_id: "str | None", epoch: int = 7) -> bytes:
    """One ``hello`` as wire bytes, with the nonce set, blank, or absent.

    Absent (``None``) is the pre-existing runtime from §4.5's additive rule: it
    sends no such key, and its hello must still *decode* so that the refusal is
    the named mismatch rather than a 30 s handshake timeout.
    """
    hello = Hello(
        pid=os.getpid(),
        started_at="2026-01-01T00:00:00+00:00",
        runtime_version="s2",
        epoch_seen=epoch,
        incarnation_id=incarnation_id or "",
    )
    frame = hello.to_frame(seq=1, worker="alpha", instance=1, epoch=epoch)
    if incarnation_id is None:
        del frame["payload"]["incarnation_id"]
    return encode_frame(frame)


class _ScriptedChild:
    """A ``multiprocessing.Process`` stand-in that greets with given bytes."""

    def __init__(self, sock: object, greeting: bytes) -> None:
        self.pid = os.getpid()
        self._sock = sock
        self._greeting = greeting
        self._held: list = []

    def start(self) -> None:
        # Hold a duplicate of the child end. The runtime closes its own copy
        # right after ``start()``, and an EOF there would race the handshake
        # refusal against the "runtime is gone" path -- a different bug.
        self._held.append(self._sock.dup())
        self._sock.sendall(self._greeting)

    def release(self) -> None:
        for sock in self._held:
            sock.close()
        self._held.clear()


class _FakeContext:
    """``multiprocessing.get_context`` stand-in: no process is created."""

    def __init__(self, greeting: bytes) -> None:
        self._greeting = greeting
        self.child: "_ScriptedChild | None" = None

    def Process(self, *, target, args, kwargs, name, daemon) -> _ScriptedChild:
        # ``args[0]`` is the child end of the real socketpair ``start`` made;
        # writing there is what the spawned interpreter would do.
        self.child = _ScriptedChild(args[0], self._greeting)
        return self.child


def _refuse(monkeypatch, greeting: bytes, **overrides: object):
    """Start a runtime whose child greets with *greeting*; return (exc, events, child)."""
    context = _FakeContext(greeting)
    monkeypatch.setattr(
        process_module,
        "multiprocessing",
        types.SimpleNamespace(get_context=lambda method: context),
    )
    events: list = []
    monkeypatch.setattr(
        "thoughtmachine.audit_logger.audit_event",
        lambda event_type, details: events.append((event_type, details)),
    )
    runtime = ProcessWorkerRuntime(_spec(**overrides))
    with pytest.raises(WorkerRuntimeLost) as caught:
        runtime.start()
    # The refusal leaves the pair open on purpose (nothing was accepted), so
    # release it here rather than leaking a reader thread and two fds.
    assert context.child is not None
    context.child.release()
    if runtime._reader is not None:
        runtime._reader.join(5.0)
    runtime._sock.close()
    return caught.value, events


def test_a_hello_carrying_a_stale_nonce_is_refused(monkeypatch, capsys):
    """§4.7: the fence axes all match; only the nonce says it is a replay."""
    exc, events = _refuse(monkeypatch, _hello_bytes(incarnation_id=STALE_NONCE))

    assert exc.reply.ok is False
    assert exc.reply.envelope["status"] == "incarnation_mismatch"
    # Named both ways, truncated: an operator reading one line can tell this
    # apart from the epoch/pid refusals without a hex dump.
    assert str(exc) == (
        "hello carries incarnation fedcba98..., expected 01234567...: the "
        "runtime is not the incarnation we asked for (§4.7)"
    )

    # Loud on stderr as well as raised: a swallowed exception must still leave
    # a trail, because a refused replay is a security event and not a typo.
    stderr = capsys.readouterr().err
    assert "REFUSING the runtime for 'alpha'" in stderr
    assert "fedcba98..." in stderr and "01234567..." in stderr

    assert events == [
        (
            "WORKER_INCARNATION_MISMATCH",
            "worker=alpha expected=01234567... received=fedcba98...",
        )
    ]


def test_a_hello_without_a_nonce_is_refused(monkeypatch, capsys):
    """§4.7: absent means empty, and empty is refused -- fail closed, not open."""
    exc, events = _refuse(monkeypatch, _hello_bytes(incarnation_id=None))

    assert exc.reply.envelope["status"] == "incarnation_mismatch"
    # The absent case is named as absent rather than rendered as a blank, so
    # "no nonce at all" is distinguishable from "a nonce that truncated to ''".
    assert str(exc) == (
        "hello carries incarnation <missing>, expected 01234567...: the runtime "
        "is not the incarnation we asked for (§4.7)"
    )
    assert "<missing>" in capsys.readouterr().err
    assert events == [
        (
            "WORKER_INCARNATION_MISMATCH",
            "worker=alpha expected=01234567... received=<missing>",
        )
    ]


class _WorkerLike:
    """The attributes ``spec_from_worker`` reads, and nothing else."""

    worker_name = "alpha"
    instance_id = 3
    definition = {"name": "alpha"}
    workspace_dir = "/tmp/ws"
    _agent_config_dict = {"model": "m"}
    _generation = 7


def test_every_spawn_gets_its_own_nonce():
    """§4.7: a nonce that repeats would authenticate the replay it exists to stop.

    Two spawns of the SAME worker at the SAME epoch -- the reuse that adoption
    needs -- must still differ here, or the value proves nothing.
    """
    first = spec_from_worker(_WorkerLike())
    second = spec_from_worker(_WorkerLike())
    assert first["incarnation_id"] and second["incarnation_id"]
    assert first["incarnation_id"] != second["incarnation_id"]
    # Data, not a value the child re-derives: it has to survive the boundary
    # unchanged, or the echo could never match.
    assert pickle.loads(pickle.dumps(first))["incarnation_id"] == (
        first["incarnation_id"]
    )

    # Same guarantee for a spec built by hand, which is how adoption and the
    # child-events harness construct one: the default is minted before the
    # spawn, so both sides still read the same value.
    hand_built = {"worker": "w1", "instance": 0, "epoch": 7, "worker_kwargs": {}}
    one = ProcessWorkerRuntime(dict(hand_built))
    two = ProcessWorkerRuntime(dict(hand_built))
    assert one._spec["incarnation_id"]
    assert one._spec["incarnation_id"] != two._spec["incarnation_id"]
