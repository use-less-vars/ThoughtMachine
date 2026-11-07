# tests/test_worker_runtime_process.py
"""Cross-process proof for the S2 worker runtime (§4.1, §4.3, §4.4, §4.8).

Every process here is started with ``get_context("spawn")``, never the Linux
``fork`` default: ``fork`` hands the child a copy of an interpreter that is
already populated -- imported modules, live locks, warm sockets -- which is
exactly the state ``worker_runtime_main`` is *not* allowed to assume. Forking
would therefore hide the very bugs this file exists to catch (a factory ref
that does not re-resolve, an import that only worked because the parent had
done it, a socket that only worked because it was inherited).

The fake worker answers with the envelope key ``response`` because that is
what ``ProcessWorkerRuntime.dispatch_query`` reads -- the same key
``deliver_query_and_block`` produces in production.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import pickle
import queue
import signal
import socket
import threading
import time
from pathlib import Path

import pytest

from tools.workspace.worker_runtime_main import worker_runtime_main
from tools.workspace.worker_runtime_process import (
    ProcessWorkerRuntime,
    RuntimeSpecError,
    WorkerQueryTimeout,
    WorkerRuntimeLost,
    spec_from_worker,
)
from tools.workspace.worker_runtime_protocol import (
    DIRECTION_FROM_RUNTIME,
    DIRECTION_TO_RUNTIME,
    DispatchQuery,
    FrameDecoder,
    FrameEmitter,
    Shutdown,
)
from tools.workspace.worker_thread import WorkerThread

mp = multiprocessing.get_context("spawn")


def build_fake_worker(*, delay: float = 0.0, hang: bool = False, marker: str = "ok"):
    """A duck-typed stand-in for ``WorkerThread``, built inside the child.

    It is named by ``module:attr`` in the runtime spec, so the child resolves
    it exactly the way it resolves the real factory -- and, being a factory,
    it can carry per-test knobs into a process that a pickled instance would
    smuggle in by copy instead.
    """

    class _Fake(threading.Thread):
        def __init__(self) -> None:
            super().__init__(name="fake-worker", daemon=True)
            self._input_queue: "queue.Queue[object]" = queue.Queue()
            self._stop = threading.Event()
            self.status = "ready"
            self.current_task = None
            self.error = None
            self._manual_only_pause = False

        def run(self) -> None:
            while True:
                try:
                    item = self._input_queue.get(timeout=0.02)
                except queue.Empty:
                    continue
                if item is None:
                    return
                query_id, query, reply_q = item
                if hang:
                    # Park forever: the point of the hang case is that the
                    # process is killed while a query is in flight, so no
                    # reply may ever be produced.
                    self._stop.wait()
                    return
                if delay:
                    time.sleep(delay)
                reply_q.put(
                    json.dumps(
                        {
                            "status": "ok",
                            "response": f"{marker}:{query}",
                            "query_id": query_id,
                        }
                    )
                )

    return _Fake()


def _spec(**worker_kwargs) -> dict:
    """A runtime spec whose factory re-resolves in a freshly spawned child.

    ``__name__`` rather than the literal ``tests.test_worker_runtime_process``:
    pytest decides what this module is called, and the child inherits
    ``sys.path``, so the module-relative ref is the one that always imports.
    """
    return {
        "worker": "alpha",
        "instance": 1,
        "epoch": 7,
        "worker_factory": f"{__name__}:build_fake_worker",
        "worker_kwargs": dict(worker_kwargs),
    }


class _Frames:
    """Read decoded frames off a socket, buffering more than one per read."""

    def __init__(self, sock, decoder: FrameDecoder) -> None:
        self._sock = sock
        self._decoder = decoder
        self._pending: "list[dict]" = []

    def next(self, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout
        while not self._pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("timed out waiting for a frame")
            self._sock.settimeout(remaining)
            chunk = self._sock.recv(65536)
            if not chunk:
                raise AssertionError("socket closed while a frame was due")
            self._pending.extend(self._decoder.feed(chunk))
        return self._pending.pop(0)


def _drive_child(**spec_kwargs):
    """Start ``worker_runtime_main`` ourselves and return (proc, frames, emitter)."""
    parent_sock, child_sock = socket.socketpair()
    emitter = FrameEmitter(
        DIRECTION_TO_RUNTIME, worker="alpha", instance=1, epoch=7
    )
    frames = _Frames(parent_sock, FrameDecoder(DIRECTION_FROM_RUNTIME))
    proc = mp.Process(target=worker_runtime_main, args=(child_sock, _spec(**spec_kwargs)))
    proc.start()
    child_sock.close()
    return proc, parent_sock, frames, emitter


def _child_process(runtime: ProcessWorkerRuntime):
    """The ``mp.Process`` a runtime owns, whatever it calls the attribute."""
    for value in vars(runtime).values():
        if isinstance(value, multiprocessing.process.BaseProcess):
            return value
    raise AssertionError("the runtime owns no multiprocessing.Process")


def test_dispatch_query_correlates_the_reply_on_query_id():
    """§4.8: the reply is matched on ``query_id``, and acceptance is visible first."""
    runtime = ProcessWorkerRuntime(_spec())
    try:
        runtime.start()
        result = {}

        def run() -> None:
            result["reply"] = runtime.dispatch_query("hello", timeout=30)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not runtime.accepted and time.monotonic() < deadline:
            time.sleep(0.01)
        # query_accepted is observable while the reply is still outstanding,
        # so the reply that follows can only be attributed by its id.
        assert len(runtime.accepted) == 1
        thread.join(30)
        assert result["reply"] == "ok:hello"
        query_id = next(iter(runtime.accepted))
        assert len(query_id) == 32
        int(query_id, 16)
    finally:
        runtime.close(grace_ms=50)


def test_seq_starts_at_one_per_direction_and_advances_independently():
    """§4.6: each direction sequences from 1 on its own; child frames are 1..n."""
    proc, parent_sock, frames, emitter = _drive_child()
    try:
        hello = frames.next()
        assert hello["kind"] == "hello"
        assert hello["seq"] == 1
        parent_sock.sendall(
            emitter.encode(
                DispatchQuery(query_id="a" * 32, query="seq", timeout_seconds=30.0)
            )
        )
        assert emitter.last_seq == 1
        accepted = frames.next()
        assert accepted["kind"] == "query_accepted"
        assert accepted["seq"] == 2
        reply = frames.next()
        assert reply["kind"] == "reply"
        assert reply["seq"] == 3
        assert reply["payload"]["query_id"] == "a" * 32
        parent_sock.sendall(emitter.encode(Shutdown(grace_ms=100)))
        assert emitter.last_seq == 2
        bye = frames.next()
        assert bye["kind"] == "bye"
        assert bye["seq"] == 4
    finally:
        parent_sock.close()
        proc.join(15)
        if proc.is_alive():
            proc.kill()
            proc.join(5)
    assert proc.exitcode == 0


def test_the_child_owns_its_pid_and_reports_the_epoch():
    """§4.4 ``hello``: the pid proves a separate process, ``epoch_seen`` the fence."""
    runtime = ProcessWorkerRuntime(_spec())
    try:
        hello = runtime.start()
        assert hello.pid == runtime.pid
        assert hello.pid != os.getpid()
        assert hello.epoch_seen == 7
    finally:
        runtime.close(grace_ms=50)


def test_a_frame_split_across_two_writes_is_reassembled():
    """§4.2: the length prefix defeats a short read -- the S0 spike's blind spot.

    The body is written as ``payload[:7]`` / sleep / ``payload[7:]``, so the
    second write lands in its own ``recv`` and the decoder has to hold the
    partial frame across reads rather than reject it as truncated.
    """
    proc, parent_sock, frames, emitter = _drive_child()
    try:
        assert frames.next()["kind"] == "hello"
        payload = emitter.encode(
            DispatchQuery(query_id="b" * 32, query="split", timeout_seconds=30.0)
        )
        parent_sock.sendall(payload[:7])
        time.sleep(0.15)
        parent_sock.sendall(payload[7:])
        accepted = frames.next()
        assert accepted["kind"] == "query_accepted"
        assert accepted["payload"]["query_id"] == "b" * 32
        reply = frames.next()
        assert reply["kind"] == "reply"
        assert reply["seq"] == 3
        assert reply["payload"]["query_id"] == "b" * 32
        assert reply["payload"]["ok"] is True
    finally:
        parent_sock.close()
        proc.join(15)
        if proc.is_alive():
            proc.kill()
            proc.join(5)


def test_a_killed_child_fails_the_in_flight_query_terminally():
    """§6: a dead child resolves the pending future as ``runtime_lost``, not a hang."""
    runtime = ProcessWorkerRuntime(_spec(hang=True))
    try:
        runtime.start()
        captured = {}

        def run() -> None:
            try:
                runtime.dispatch_query("never answered", timeout=60)
            except BaseException as exc:  # noqa: BLE001 -- the object under test
                captured["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not runtime.accepted and time.monotonic() < deadline:
            time.sleep(0.01)
        query_id = next(iter(runtime.accepted))
        os.kill(runtime.pid, signal.SIGKILL)
        thread.join(30)
        assert not thread.is_alive()
        error = captured.get("error")
        assert isinstance(error, WorkerRuntimeLost)
        assert error.reply.ok is False
        assert error.reply.query_id == query_id
        assert error.reply.envelope["status"] == "runtime_lost"
        runtime.close(grace_ms=50)
        assert runtime.alive is False
        assert _child_process(runtime).exitcode is not None
    finally:
        runtime.close(grace_ms=50)


def test_two_concurrent_queries_never_cross_talk():
    """§4.8: two in-flight queries each get their own answer -- one map, two ids."""
    runtime = ProcessWorkerRuntime(_spec(delay=0.1))
    try:
        runtime.start()
        answers = {}

        def run(label: str) -> None:
            answers[label] = runtime.dispatch_query(label, timeout=60)

        threads = [
            threading.Thread(target=run, args=(label,), daemon=True)
            for label in ("one", "two")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert answers == {"one": "ok:one", "two": "ok:two"}
    finally:
        runtime.close(grace_ms=50)


def test_a_late_reply_never_satisfies_the_next_query():
    """§4.8: a timed-out query's reply is dropped, not handed to its successor."""
    runtime = ProcessWorkerRuntime(_spec(delay=0.15))
    try:
        runtime.start()
        with pytest.raises(WorkerQueryTimeout) as caught:
            runtime.dispatch_query("first", timeout=0.05)
        assert caught.value.reply.envelope["status"] == "timeout"
        assert runtime.dispatch_query("second", timeout=30) == "ok:second"
        assert runtime.lost is False
    finally:
        runtime.close(grace_ms=50)


class _SpecStub:
    """A worker stand-in carrying only the attributes ``spec_from_worker`` reads."""

    def __init__(self, **attrs: object) -> None:
        for key, value in attrs.items():
            setattr(self, key, value)


def _stub(**overrides: object) -> _SpecStub:
    attrs: dict = {
        "worker_name": "alpha",
        "definition": {"name": "alpha"},
        "workspace_dir": Path("/tmp/ws"),
        "instance_id": 3,
        "_agent_config_dict": {"model": "m"},
        "_generation": 7,
    }
    attrs.update(overrides)
    return _SpecStub(**attrs)


def test_spec_from_worker_describes_a_picklable_worker():
    """§4.1: the spec crosses a process boundary, so it must survive pickle."""
    spec = spec_from_worker(_stub())
    assert set(spec) == {
        "worker",
        "instance",
        "epoch",
        "worker_factory",
        "worker_kwargs",
    }
    assert spec["worker"] == "alpha"
    assert spec["instance"] == 3

    reloaded = pickle.loads(pickle.dumps(spec))
    kwargs = reloaded["worker_kwargs"]
    assert kwargs["name"] == "alpha"
    assert kwargs["definition"] == {"name": "alpha"}
    assert kwargs["agent_config"] == {"model": "m"}
    # str(), not Path: a Path is picklable but the child reconstructs arguments
    # from plain data, so the coercion happens supervisor-side.
    assert kwargs["workspace_dir"] == str(Path("/tmp/ws"))
    assert isinstance(kwargs["workspace_dir"], str)

    assert spec["epoch"] == 7
    assert spec_from_worker(_stub(), epoch=99)["epoch"] == 99

    # The factory hook is optional: real WorkerThreads do not set it (production
    # rebuilds the worker through worker_runtime_main's WorkerThread(**kwargs)
    # fallback), so None here is the intended shape, not a missing attribute.
    assert spec["worker_factory"] is None
    assert "worker_factory" in spec
    hooked = spec_from_worker(_stub(_runtime_worker_factory="mod:attr"))
    assert hooked["worker_factory"] == "mod:attr"


def test_spec_from_worker_refuses_a_worker_it_cannot_rebuild():
    """§4.1: a spec missing what the child needs is refused here, not there."""
    with pytest.raises(RuntimeSpecError):
        spec_from_worker(
            _SpecStub(
                worker_name="alpha",
                workspace_dir=Path("/tmp/ws"),
                instance_id=3,
                _agent_config_dict={"model": "m"},
            )
        )
    with pytest.raises(RuntimeSpecError):
        spec_from_worker(
            _SpecStub(
                worker_name="alpha",
                definition={"name": "alpha"},
                workspace_dir=Path("/tmp/ws"),
                instance_id=3,
            )
        )

def test_spec_from_worker_accepts_a_real_worker_thread(tmp_path):
    """§3.3: the contract is a real ``WorkerThread``, not a stub that fits.

    ``workspace_dir`` is the one attribute the thread used to hold only in its
    derived form (``workers/<label>/``), so ``spec_from_worker`` refused every
    production worker while the stub-based tests above stayed green -- a stub
    can carry any attribute, and only the real object can be missing one. The
    worker here is the real thing, built the cheapest way ``__init__`` allows:
    it lays down ``workers/<label>/`` and never starts the thread.
    """
    worker = WorkerThread(
        name="probe-w",
        definition={},
        agent_config={"provider": "openai", "model": "gpt-4"},
        workspace_dir=tmp_path,
        session_permissions={},
        instance_id=1,
    )

    spec = spec_from_worker(worker)

    # The canonical root as constructed -- not one re-derived from
    # ``_worker_dir``, which would hand the child ``<root>/workers/probe-w``
    # and nest a second ``workers/`` tree inside the first.
    assert worker.workspace_dir == tmp_path
    assert spec["worker_kwargs"]["workspace_dir"] == str(worker.workspace_dir)
    assert spec["worker_kwargs"]["workspace_dir"] == str(tmp_path)
    assert isinstance(spec["worker_kwargs"]["workspace_dir"], str)
    assert spec["worker"] == "probe-w"
    assert spec["instance"] == 1
