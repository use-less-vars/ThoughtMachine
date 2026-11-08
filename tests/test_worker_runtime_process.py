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
    Cancel,
    Control,
    DispatchQuery,
    FrameDecoder,
    FrameEmitter,
    Ping,
    Shutdown,
)
from tools.workspace.worker_thread import WorkerThread

mp = multiprocessing.get_context("spawn")


# ── C1 fakes: a container manager mirroring tests/test_worker_sync_stop.py ────
#
# Local copies rather than imports: the sync-stop helper ends with
# ``thread.send_query = _timeout``, which forces every test built on it down the
# timeout branch. Nothing in this file may inherit that, so the two fakes below
# are a deliberate sibling, not a reuse.

#: The one container the C1 worker owns; asserted by id so a "no containers at
#: all" bug cannot satisfy "it was not stopped".
C1_CONTAINER_ID = "c1-owned-ctr"


class _C1Container:
    """Stand-in for a docker container object (id / name / labels only)."""

    def __init__(self, cid, name, labels=None):
        self.id = cid
        self.name = name
        self.labels = dict(labels or {})
        self.status = "running"


class _C1ContainerManager:
    """Mirror of ``_FakeContainerManager``; ``remove()`` drops from the listing."""

    def __init__(self, containers):
        self.containers = list(containers)
        self.stopped = []
        self.removed = []

    def list_containers(self):
        return list(self.containers)

    def stop(self, container, **kwargs):
        self.stopped.append(container)

    def remove(self, container, **kwargs):
        self.removed.append(container)
        if container in self.containers:
            self.containers.remove(container)


def build_fake_worker(
    *,
    delay: float = 0.0,
    hang: bool = False,
    marker: str = "ok",
    blocked_tool: bool = False,
    report_path=None,
    release_path=None,
):
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
            # C1 -- the control/cancel surface the runtime's frames reach. The
            # runtime sets ``_frame_sink`` and ``_turn_cancel`` on whatever
            # object the factory returns, so those two names are the contract.
            self._turn_cancel = threading.Event()
            self._stop_event = threading.Event()
            self._frame_sink = None
            self._container_manager = (
                _C1ContainerManager([_C1Container(C1_CONTAINER_ID, "c1-ctr")])
                if blocked_tool
                else None
            )

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
                if blocked_tool:
                    self._run_blocked_tool_loop()
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

        # -- C1 reporting -------------------------------------------------
        def _report(self, **fields) -> None:
            """Append one JSON line the parent can watch (cross-process channel)."""
            if not report_path:
                return
            with open(report_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(fields, sort_keys=True) + "\n")

        def _run_blocked_tool_loop(self) -> None:
            """Block in a 'long tool' that WATCHES THIS CHILD'S OWN cancel flag.

            The loop exits for one of exactly two reasons, and reports which:
            the runtime's ``cancel`` frame reached ``_turn_cancel`` (the
            property C1 is about), or the test's release file appeared (the
            fallback, which is only ever created after the assertion window).
            ``cause`` is the ordering artifact the test asserts on -- never a
            wall-clock sleep.
            """
            self.status = "running"
            self._report(event="tool_loop_entered")
            while not self._turn_cancel.is_set():
                if release_path and os.path.exists(release_path):
                    self._report(event="tool_loop_exited", cause="release")
                    return
                time.sleep(0.01)
            self._report(event="tool_loop_exited", cause="cancel")

        # -- C1 control surface (mirrors WorkerThread's verbs) --------------
        def stop(self) -> None:
            self.detach()

        def detach(self) -> None:
            """Park without terminating: thread, status and containers survive."""
            self.status = "detached"
            self._report(
                event="detached",
                cm_stopped=[c.id for c in self._container_manager.stopped],
                cm_removed=[c.id for c in self._container_manager.removed],
                cm_listed=[c.id for c in self._container_manager.list_containers()],
                worker_alive=self.is_alive(),
            )

        def kill(self) -> None:
            """Terminate: reclaim owned containers, then exit (the kill path)."""
            if self._container_manager is not None:
                for c in list(self._container_manager.list_containers()):
                    self._container_manager.stop(c)
                    self._container_manager.remove(c)
            self.status = "killed"
            self._stop.set()
            self._stop_event.set()

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


# ── C1: cancel must reach a worker that is blocked inside a long tool ─────────


def test_cancel_reaches_a_worker_blocked_in_a_long_tool_and_detaches(tmp_path):
    """C1 (§4.3, §4.4, §5.4): a blocked tool is cancellable, and ``stop`` detaches.

    The child's worker blocks in a "long tool" that polls *its own*
    ``_turn_cancel`` -- the very flag ``worker_runtime_main._handle_cancel``
    sets. The release file is a fallback that is only created in ``finally``, so
    it cannot be what ends the block; the child itself reports which of the two
    conditions fired, and the assertions are made against that report rather
    than against a wall-clock sleep.
    """
    report_path = str(tmp_path / "c1-report.jsonl")
    release_path = str(tmp_path / "c1-release")
    Path(report_path).write_text("", encoding="utf-8")

    proc, parent_sock, frames, emitter = _drive_child(
        blocked_tool=True, report_path=report_path, release_path=release_path
    )

    def _events() -> "list[dict]":
        out = []
        try:
            text = Path(report_path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return out
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn tail line is simply not an event yet
        return out

    def _wait_event(name: str, timeout: float = 15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in _events():
                if event.get("event") == name:
                    return event
            if not proc.is_alive():
                break
            time.sleep(0.02)
        return None

    def _next_of(kind: str, timeout: float = 5.0) -> dict:
        seen = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"no {kind} frame within {timeout}s; saw {seen}")
            try:
                frame = frames.next(timeout=remaining)
            except AssertionError as exc:
                raise AssertionError(
                    f"no {kind} frame within {timeout}s; saw {seen}"
                ) from exc
            seen.append(frame["kind"])
            if frame["kind"] == kind:
                return frame

    try:
        hello = frames.next(timeout=15)
        # catches: a runtime that never announces itself to the parent, which
        # would make every later frame unattributable to this instance/epoch.
        assert hello["kind"] == "hello"

        parent_sock.sendall(
            emitter.encode(
                DispatchQuery(
                    query_id="c" * 32, query="long task", timeout_seconds=300.0
                )
            )
        )
        accepted = _next_of("query_accepted", timeout=10.0)
        # catches: the query being swallowed instead of accepted-and-tracked, so
        # the later cancel would have nothing to cancel.
        assert accepted["payload"]["query_id"] == "c" * 32

        entered = _wait_event("tool_loop_entered", timeout=20.0)
        # catches: the worker never getting as far as the blocked tool, which
        # would make every cancellation assertion below vacuously true.
        assert entered is not None, "the fake worker never entered the tool loop"

        # -- assertion 1: cancel is acknowledged, within 5s ------------------
        request_id = "r" * 32
        parent_sock.sendall(
            emitter.encode(
                Cancel(
                    request_id=request_id,
                    reason="operator: cancel a blocked tool",
                    deadline_ms=5000,
                )
            )
        )
        cancel_ack = _next_of("cancel_ack", timeout=5.0)
        # catches: the cancel being dropped (a worker blocked in a tool that
        # never reaches the runtime's reader is exactly the C1 defect), or the
        # ack losing the correlation id that makes it an ack.
        assert cancel_ack["payload"]["request_id"] == request_id
        # catches: a first rung that answers "refused" for a live, cancellable
        # worker -- i.e. the terminal-status check mis-firing on status
        # "running".
        assert cancel_ack["payload"]["phase"] == "accepted"

        # -- assertion 2: the tool loop exits, and the cancel is why ---------
        exited = _wait_event("tool_loop_exited", timeout=10.0)
        # catches: the ack being emitted while nothing actually interrupts the
        # blocked loop -- the cancel reaches the flag but never the tool.
        assert exited is not None, "the blocked tool loop never exited"
        # catches: the loop ending because the test released it rather than
        # because the cancel propagated.
        assert exited["cause"] == "cancel"
        # catches: the release fallback having fired inside the assertion
        # window, which would make the ordering claim above unfalsifiable.
        assert not os.path.exists(release_path)

        # -- assertion 3: stop means DETACH, not kill ------------------------
        parent_sock.sendall(
            emitter.encode(Control(request_id="s" * 32, action="stop"))
        )
        control_ack = _next_of("control_ack", timeout=5.0)
        # catches: the control frame not being routed to a verb at all.
        assert control_ack["payload"]["action"] == "stop"
        # catches: an unroutable verb, a vanished worker, or an exception
        # swallowed into ok=False.
        assert control_ack["payload"]["ok"] is True
        # catches: "stop" being wired to the kill path -- the §4.3 / §11.2
        # rule 7 defect this test exists for.
        assert control_ack["payload"]["status"] not in ("killed", "terminated")
        # catches: a status reported that is not the detach the caller needs in
        # order to know the worker was parked rather than destroyed.
        assert control_ack["payload"]["status"] == "detached"

        # -- assertion 4: the owned container is neither stopped nor removed --
        detached = _wait_event("detached", timeout=5.0)
        # catches: detach() being reached without the worker observing it.
        assert detached is not None, "no detach was reported by the child"
        # catches: a detach that reclaims containers it does not own back --
        # the resource leak the caller relies on being absent.
        assert detached["cm_stopped"] == []
        # catches: the container being removed instead of merely parked, which
        # silently discards the workspace the worker was using.
        assert detached["cm_removed"] == []
        # catches: the container having disappeared from the manager entirely
        # (a "not in stopped/removed" pass achieved by owning nothing).
        assert detached["cm_listed"] == [C1_CONTAINER_ID]

        # -- assertion 5: the worker survives the detach ---------------------
        # catches: detach() terminating the worker thread, which would make
        # "detach" indistinguishable from "kill" despite the status string.
        assert detached["worker_alive"] is True
        # catches: the process exiting on stop, i.e. a detach that is really a
        # shutdown.
        assert proc.is_alive() is True
        parent_sock.sendall(emitter.encode(Ping(nonce=61)))
        pong = _next_of("pong", timeout=5.0)
        # catches: a process that is still running but no longer reading its
        # socket -- alive in the OS sense only.
        assert pong["payload"]["nonce"] == 61
    finally:
        # Fallback, created only here: the assertion window above is already
        # closed, so this can never be what ended the blocked loop.
        Path(release_path).write_text("release", encoding="utf-8")
        try:
            parent_sock.sendall(emitter.encode(Shutdown(grace_ms=100)))
        except OSError:
            pass
        parent_sock.close()
        proc.join(15)
        if proc.is_alive():
            proc.kill()
            proc.join(5)

