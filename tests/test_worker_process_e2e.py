# tests/test_worker_process_e2e.py
"""C40 -- process mode, end to end: the worker that answers is the one in a child.

The contract
------------
``WORKER_RUNTIME_MODE=process`` is a promise about *isolation*: when it is set,
the worker body that serves a query is not this interpreter. A runtime that
resolves the flag and then hands back an in-process handle breaks that promise
silently -- the operator is told "process", the code runs on a thread, and
nothing notices. This file is the test that notices.

Which call site is driven, and why it is not higher
---------------------------------------------------
``Worker._action_spawn`` (``tools/workspace/worker_thread.py``, the
``attach_thread_frame_sink(thread)`` line) is the production spawn site: it is
where a real ``WorkerThread`` is constructed and handed to the one function that
builds a ``WorkerRuntimeHandle``. That is precisely the point at which a runtime
mode has to be resolved, so this file drives it for real and replaces only the
*leaves* -- the thread class (which would otherwise start a turn loop against an
LLM), the workspace-directory resolver, and the docker-probing container-manager
factory. The real ``attach_thread_frame_sink`` runs, so the mode under
observation is the mode the production path actually resolved, not one this
test chose.

The entry point *above* it -- a full agent/session turn driving a spawn through
the API -- is not reachable from a unit test: it needs an agent definition, a
vault and a model call. The spawn site is the narrowest honest equivalent, and
the two claims it is asked to support are both asserted here: the handle the
production path built says ``process`` (test 1), and a dispatch through that
handle is answered by a different OS process, whose pid it echoes (test 2).

Test 3 pins the default the other way round: with the flag unset the same
spawn site must resolve ``thread`` and start nothing out of process, because
that is what the production path actually does (``DEFAULT_RUNTIME_MODE`` in
``tools/workspace/worker_runtime.py``). C62 is the item that flips that default
to ``process`` once the spawn site really starts one.

If the runtime silently falls back to threads, test 1 goes red on
``handle.mode``. That red is the entire point of C40 -- a green test over a
thread-mode runtime would be worse than no test at all.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import queue
import threading
from typing import Any, List

import pytest

import tools.workspace.worker as worker_module
import tools.workspace.worker_thread as worker_thread_module
from tools.workspace.worker_registry import WorkerRegistry
from tools.workspace.worker_runtime import (
    MODE_PROCESS,
    MODE_THREAD,
    WORKER_RUNTIME_MODE_ENV,
    WorkerRuntimeHandle,
    resolve_runtime_mode,
)

WORKER = "c40-w"

#: The value the child echoes back, so a reply that never left this process
#: cannot be mistaken for one that did.
SENTINEL = "c40-sentinel"

#: The child echoes ``<SENTINEL>:pid=<its pid>:<query>``.
_REPLY_PREFIX = f"{SENTINEL}:pid="


# ───────────────────────────────────────────────────────────────────────────
# The child side: a worker factory resolved by ``module:attr`` IN the child
# ───────────────────────────────────────────────────────────────────────────


def build_child_worker(**kwargs: Any) -> Any:
    """Build the stand-in worker *inside* the spawned child process.

    The spec's ``worker_kwargs`` are the real ``WorkerThread`` constructor
    arguments (name, definition, agent_config, workspace_dir, ...); they are
    accepted and ignored, because the only thing this stand-in has to prove is
    *where* the answer came from. It answers on the same
    ``(query_id, query, reply_q)`` triple ``worker_runtime_main`` feeds every
    worker, and stamps its own pid into the reply.
    """

    class _ChildWorker(threading.Thread):
        def __init__(self) -> None:
            super().__init__(name="c40-child", daemon=True)
            self._input_queue: "queue.Queue[object]" = queue.Queue()
            self._stop = threading.Event()
            self._turn_cancel = threading.Event()
            self._frame_sink = None
            self.status = "ready"
            self.current_task = None
            self.error = None

        def run(self) -> None:
            while True:
                try:
                    item = self._input_queue.get(timeout=0.02)
                except queue.Empty:
                    continue
                if item is None:
                    return
                query_id, query, reply_q = item
                reply_q.put(
                    json.dumps(
                        {
                            "status": "ok",
                            "response": f"{_REPLY_PREFIX}{os.getpid()}:{query}",
                            "query_id": query_id,
                        }
                    )
                )

        # The control surface ``worker_runtime_main`` may reach for on teardown.
        def stop(self) -> None:
            self.status = "detached"

        def detach(self) -> None:
            self.status = "detached"

        def kill(self) -> None:
            self.status = "killed"
            self._stop.set()

    return _ChildWorker()


def _pid_from_reply(reply: str) -> int:
    """The pid the child stamped on its answer."""
    assert reply.startswith(_REPLY_PREFIX), (
        f"the child's envelope is not the one this test asked for: {reply!r}"
    )
    return int(reply[len(_REPLY_PREFIX) :].split(":", 1)[0])


# ───────────────────────────────────────────────────────────────────────────
# Fakes: leaves only
# ───────────────────────────────────────────────────────────────────────────


class _SpecCarryingWorker:
    """Duck-typed ``WorkerThread`` -- the attributes ``spec_from_worker`` reads.

    ``_runtime_worker_factory`` is the one attribute that keeps the child from
    building a real ``WorkerThread``: it names the factory above, exactly the
    way production names ``None`` and lets the child fall back to the real
    class. Nothing here starts, so the supervisor side stays cheap.
    """

    def __init__(self, *, session_id: str, workspace_dir: str) -> None:
        self.worker_name = WORKER
        self.instance_id = 1
        self.definition: dict = {}
        self.agent_config = {"provider": "openai", "model": "gpt-4"}
        self.workspace_dir = workspace_dir
        self.session_id = session_id
        self._runtime_worker_factory = f"{__name__}:build_child_worker"


class _FakeThread(_SpecCarryingWorker):
    """Stands in for ``WorkerThread`` at the spawn site: records, never runs.

    The attributes ``_action_spawn`` reads back are set for real
    (``instance_id`` / ``instance_label`` for the registry key and the reply
    envelope, ``_initial_context`` for the auto-query branch), and the
    ``spec_from_worker`` attributes are inherited, so the handle the spawn site
    builds can actually be dispatched through.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            session_id=kwargs.get("session_id") or "",
            workspace_dir=str(kwargs.get("workspace_dir") or ""),
        )
        self.ctor_kwargs = kwargs
        self.name = kwargs["name"]
        self.instance_id = kwargs["instance_id"]
        self.instance_label = WorkerRegistry.instance_label(
            kwargs["name"], kwargs["instance_id"]
        )
        self.status = "ready"
        self._timeout_seconds = kwargs.get("timeout_seconds") or 30
        self.workspace_id = kwargs.get("workspace_id")
        self._initial_context: dict = {}
        self.started = False
        self._frame_sink = None

    def start(self) -> None:
        self.started = True

    def send_query(self, query: str, timeout: float = 120.0) -> str:
        """Answer in this interpreter, the way thread mode really answers.

        In thread mode ``WorkerRuntimeHandle.dispatch_query`` delegates to
        ``worker.send_query``, so the answer is produced in-process -- the
        same process that holds the handle. In process mode the handle goes
        to the child instead and never calls this. Stamping our own pid here
        is therefore the honest thread-mode reply, and it is what makes the
        pid in the envelope evidence about *where* the body ran.
        """
        return f"{_REPLY_PREFIX}{os.getpid()}:{query}"


def _drive_the_production_spawn_site(tmp_path, monkeypatch, created: List[Any]) -> Any:
    """Run the REAL ``Worker._action_spawn`` with a fake thread class.

    Returns the ``(tool, spawn_result, fake_thread)`` triple. Only leaves are
    patched -- the thread class, the workspace resolver and the container
    manager -- so ``attach_thread_frame_sink`` (the mode decision, the thing
    under test) runs unpatched.
    """
    session_id = f"sess-c40-{abs(hash(str(tmp_path)))}-{len(created)}"

    def _factory(**kwargs: Any) -> _FakeThread:
        fake = _FakeThread(**kwargs)
        created.append(fake)
        return fake

    # Leaf 1: the thread class. ``_action_spawn`` imports ``WorkerThread`` from
    # ``tools.workspace.worker`` inside the method, so that is the attribute.
    monkeypatch.setattr(worker_module, "WorkerThread", _factory)
    # Leaf 2: workspace resolution (the spawn path needs a real directory).
    monkeypatch.setattr(worker_module, "_workspace_dir", lambda ws_id: tmp_path)
    monkeypatch.setattr(worker_thread_module, "CAPABILITIES_AVAILABLE", True)
    # Leaf 3: no ContainerManager -- constructing one probes docker.
    monkeypatch.setattr(worker_thread_module, "_CM", None)

    tool = worker_module.Worker(
        action="spawn",
        worker_name=WORKER,
        session_id=session_id,
        workspace_path=str(tmp_path),
        agent_config={"provider": "openai", "model": "gpt-4"},
    )
    result = tool._action_spawn(
        [{"name": WORKER, "status": "ready"}], f"ws-c40-{len(created)}"
    )
    assert created, "the spawn site never constructed a thread"
    return session_id, result, created[-1]


# ───────────────────────────────────────────────────────────────────────────
# 1. The production spawn site must resolve ``process`` from the flag
# ───────────────────────────────────────────────────────────────────────────


def test_the_production_spawn_path_resolves_process_mode(tmp_path, monkeypatch):
    """C40: with ``WORKER_RUNTIME_MODE=process``, the spawn site builds a
    process-mode handle and the dispatch is answered by a real child.

    Red on a runtime that ignores the flag: the handle comes back thread-mode
    and no child is ever created. The flag is asserted to be in force first, so
    a failure here cannot be mistaken for "the variable was not set".
    """
    monkeypatch.setenv(WORKER_RUNTIME_MODE_ENV, MODE_PROCESS)
    assert resolve_runtime_mode() == MODE_PROCESS, (
        "precondition: the flag must be readable from the environment"
    )

    created: List[Any] = []
    session_id, result, fake = _drive_the_production_spawn_site(
        tmp_path, monkeypatch, created
    )
    handle = fake._runtime_handle
    try:
        assert result.get("spawned") is True, f"the spawn itself failed: {result!r}"
        # The observation that matters: what the PRODUCTION path resolved.
        assert handle.mode == MODE_PROCESS, (
            f"C40: {WORKER_RUNTIME_MODE_ENV}={MODE_PROCESS!r} is in force, but the "
            f"production spawn site (tools/workspace/worker_thread.py, "
            f"attach_thread_frame_sink(thread)) built a {handle.mode!r} handle. "
            "Process mode is not wired on this tree: the flag is accepted and "
            "then silently ignored."
        )

        # And the envelope must come back from that child process.
        reply = handle.dispatch_query(SENTINEL, timeout=30)
        child_pid = _pid_from_reply(reply)
        assert child_pid != os.getpid(), (
            f"the reply claims to come from pid {child_pid}, which is this "
            f"interpreter: the 'child' is not a separate OS process"
        )
    finally:
        handle.close(grace_ms=2000)
        worker_module._worker_registry.pop((session_id, WORKER, 1), None)


# ───────────────────────────────────────────────────────────────────────────
# 2. ``WorkerRuntimeHandle.spawn`` + a real child: the envelope round-trips
# ───────────────────────────────────────────────────────────────────────────


def _can_spawn_processes() -> bool:
    """Whether this interpreter can start a ``spawn`` child at all.

    A platform without process creation cannot satisfy C40's contract, so the
    child tests are *marked skipped*, never allowed to pass vacuously.
    """
    try:
        multiprocessing.get_context("spawn").Process
    except (ValueError, OSError, AttributeError):  # pragma: no cover - platform
        return False
    return True


@pytest.mark.skipif(
    not _can_spawn_processes(),
    reason="this platform cannot create OS processes; process mode is untestable",
)
def test_process_mode_envelope_comes_back_from_a_spawned_child(tmp_path):
    """The spawn seam's own contract, independent of the spawn *site*.

    ``WorkerRuntimeHandle.spawn`` is the documented spawn-time resolution point;
    driving it directly proves the substrate can carry a query across the
    process boundary and back, which is the half of C40 that does not depend on
    who calls it.
    """
    worker = _SpecCarryingWorker(session_id="sess-c40-direct", workspace_dir=str(tmp_path))
    handle = WorkerRuntimeHandle.spawn(
        worker, environ={WORKER_RUNTIME_MODE_ENV: MODE_PROCESS}
    )
    try:
        assert handle.mode == MODE_PROCESS
        assert handle._process_runtime is None, (
            "process mode must be lazy: spawning a handle drives nothing"
        )
        reply = handle.dispatch_query(SENTINEL, timeout=30)
        child_pid = _pid_from_reply(reply)
        assert child_pid != os.getpid()
        assert reply.endswith(f":{SENTINEL}"), (
            f"the child must echo the sentinel it was given: {reply!r}"
        )
        # The runtime that answered owns a real ``multiprocessing.Process``.
        assert handle._process_runtime is not None
        assert any(
            isinstance(value, multiprocessing.process.BaseProcess)
            for value in vars(handle._process_runtime).values()
        ), "process mode returned an envelope but owns no child process"
    finally:
        handle.close(grace_ms=2000)


# ───────────────────────────────────────────────────────────────────────────
# 3. The default path (flag unset) resolves DEFAULT_RUNTIME_MODE
# ───────────────────────────────────────────────────────────────────────────


def test_the_default_spawn_path_resolves_the_default_mode(tmp_path, monkeypatch):
    """With the flag unset the same spawn site must resolve the DEFAULT mode.

    Bound to :data:`DEFAULT_RUNTIME_MODE` / :func:`resolve_runtime_mode`, never
    to a mode literal: the claim under test is "the production spawn site
    reaches the configured default", which stays true across a flip (C62
    flipped ``thread`` -> ``process``). The concrete mode is pinned elsewhere,
    by tests that set ``WORKER_RUNTIME_MODE`` explicitly.
    """
    monkeypatch.delenv(WORKER_RUNTIME_MODE_ENV, raising=False)
    assert WORKER_RUNTIME_MODE_ENV not in os.environ, (
        "precondition: the flag must genuinely be absent"
    )

    created: List[Any] = []
    session_id, result, fake = _drive_the_production_spawn_site(
        tmp_path, monkeypatch, created
    )
    handle = fake._runtime_handle
    try:
        assert result.get("spawned") is True, f"the spawn itself failed: {result!r}"
        assert handle.mode == resolve_runtime_mode(), (
            f"with {WORKER_RUNTIME_MODE_ENV} unset the production spawn site "
            f"built a {handle.mode!r} handle; the configured default is "
            f"{resolve_runtime_mode()!r} ({DEFAULT_RUNTIME_MODE!r})"
        )
        # Spawn starts no body of either kind: the handle is lazy, so a child
        # (process mode) is only built at the first dispatch_query.
        assert handle._process_runtime is None, (
            "spawn must not build an out-of-process runtime; it is created lazily"
        )
    finally:
        handle.close(grace_ms=2000)
        worker_module._worker_registry.pop((session_id, WORKER, 1), None)
