# tests/test_worker_fork_regression.py
"""C73 -- process-mode fork regression: the spawn query must reach the child.

Two defects made process mode 100% dead while every existing test stayed green.

1. **The freeze.** The spawn site waited on ``thread._output_queue`` for the
   auto-query reply. In process mode nothing ever writes to that queue: the
   briefing crosses the process boundary with the query *stripped*
   (``_briefing_only``), precisely so the child does not run it a second time.
   The queue read cannot fail, it can only wait, so a healthy worker was
   reported as "did not respond within 63.0s" after blocking the spawn for the
   full query budget -- and no child was ever asked anything.

2. **The fence.** ``WorkerRuntimeHandle._assert_current`` used
   ``worker.is_alive() is False`` as proof of death. In process mode the
   supervisor thread is *deliberately never started* (the body is the child
   interpreter), so that predicate is true for every live worker there: each
   handle verb fenced itself out with ``StaleWorkerFence``, which removed the
   only route to the child that existed.

Why the existing process-mode test misses both
----------------------------------------------
``tests/test_worker_process_e2e.py::_drive_the_production_spawn_site`` drives
the same real spawn site, but (a) it passes no ``context``, so the auto-query
wait was never reached, (b) its ``_FakeThread`` has no ``is_alive``, so the
death fence was never resolved, and (c) it never dispatches the spawn query at
all. Every one of those three holes is closed here.

What is real, and what is a leaf
--------------------------------
Real: ``Worker._action_spawn`` (the production spawn site), the capability
resolution, ``attach_thread_frame_sink`` (which resolves the runtime mode and
builds the handle), the ``WorkerRuntimeHandle``, the ``ProcessWorkerRuntime``,
and the child interpreter. Faked: only the thread object the spawn site builds
(it would otherwise start a turn loop against an LLM) and the workspace
directory / container-manager leaves. The substitute is a
``threading.Thread`` that is never started, which is exactly what the
supervisor-side object is in process mode -- so ``is_alive()`` here is the
real predicate that used to fence, not a stub that hides it.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, List, Optional

import pytest

import tools.workspace.worker as worker_module
import tools.workspace.worker_runtime as worker_runtime_module
import tools.workspace.worker_thread as worker_thread_module
from tools.workspace.job_registry import WorkerJobRegistry
from tools.workspace.worker_registry import WorkerRegistry
from tools.workspace.worker_runtime import (
    MODE_PROCESS,
    WORKER_RUNTIME_MODE_ENV,
    resolve_runtime_mode,
)

WORKER = "c73-w"

#: Stamped by the child on its answer, so a reply produced in this interpreter
#: cannot be mistaken for one that crossed the process boundary.
SENTINEL = "c73-sentinel"
_REPLY_PREFIX = f"{SENTINEL}:pid="

#: The task handed to the spawn site as ``context['query']``.
QUERY = "prove-the-fork"

#: A spawn that reaches the child answers in well under a second; the budget
#: the bug burned was 63 s. Anything near the budget means the wait primitive
#: went back to reading a queue that has no producer.
MAX_SPAWN_SECONDS = 30.0


# ─────────────────────────────────────────────────────────────────────────────
# The child side: a worker factory resolved by ``module:attr`` IN the child
# ─────────────────────────────────────────────────────────────────────────────


def build_child_worker(**kwargs: Any) -> Any:
    """Build the stand-in worker *inside* the spawned child process.

    The spec's ``worker_kwargs`` are the real ``WorkerThread`` constructor
    arguments; they are accepted and ignored, because the only thing this
    stand-in has to prove is *where* the answer came from. It answers on the
    same ``(query_id, query, reply_q)`` triple ``worker_runtime_main`` feeds
    every worker, and stamps its own pid into the reply.
    """

    class _ChildWorker(threading.Thread):
        def __init__(self) -> None:
            super().__init__(name="c73-child", daemon=True)
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


# ─────────────────────────────────────────────────────────────────────────────
# The supervisor-side thread: a real Thread that is never started
# ─────────────────────────────────────────────────────────────────────────────


class _NeverStartedBody(threading.Thread):
    """What the spawn site builds in process mode: a handle, not a body.

    A ``threading.Thread`` subclass on purpose. ``is_alive()`` is therefore the
    genuine predicate -- ``False``, because nothing was ever started -- which is
    the exact condition that used to make ``_assert_current`` declare a live
    child dead. A duck-typed double without ``is_alive`` would silently skip
    that assertion and let the regression back in.

    ``send_query`` raises rather than answering: in process mode the queues have
    no consumer, so a spawn that reaches this method has taken the thread-mode
    route and must fail loudly instead of being "answered" in-process.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(name=str(kwargs["name"]), daemon=True)
        self.worker_name = kwargs["name"]
        self.instance_id = kwargs["instance_id"]
        self.instance_label = WorkerRegistry.instance_label(
            kwargs["name"], kwargs["instance_id"]
        )
        self.definition = kwargs.get("definition") or {}
        self.agent_config = kwargs.get("agent_config") or {}
        self.workspace_dir = Path(kwargs.get("workspace_dir") or ".")
        self.session_id = kwargs.get("session_id")
        self.workspace_id = kwargs.get("workspace_id")
        self._initial_context = kwargs.get("initial_context") or {}
        # PINNED, not inherited. ``store_budget_on_thread`` hands the real
        # spawn a resolved budget (600 s by default), and ``auto_wait`` is that
        # budget plus a grace window -- so a regression would hang this test for
        # ~11 minutes instead of failing. 30 s is the default worker request
        # budget, which is what the production bug burned (a 63 s "did not
        # respond within" for a worker that was healthy the whole time).
        self._timeout_seconds = 30
        self._frame_sink = kwargs.get("frame_sink")
        # A REAL output queue with no producer -- the defect in one object.
        # Handing the spawn site a missing attribute would fail it with a
        # ``AttributeError`` and prove nothing; the production bug is that this
        # queue exists, is read, and nothing on the process-mode path will ever
        # write to it. So the budget is really burned here, exactly as it was in
        # production (63 s for a 30 s worker budget), which is what makes the
        # ``elapsed`` assertion below able to catch the regression.
        self._output_queue: "queue.Queue[object]" = queue.Queue()
        # The child factory: the one attribute that keeps the child from
        # building a real WorkerThread (which would need a model call).
        self._runtime_worker_factory = f"{__name__}:build_child_worker"
        # ``_action_spawn`` seeds the registry epoch from ``_generation``; a
        # duck-typed/mocked value degrades to unversioned, an int does not.
        self._generation = 1
        self.status = "ready"
        self.error = None
        self.current_task = None
        self.last_heartbeat = None
        self._last_reasoning = None
        self.start_called = False
        self.queue_sends: List[str] = []

    # ── The two methods that must NOT run a body / use the queues ──
    def start(self) -> None:
        """Record the call; never run a second body beside the child."""
        self.start_called = True

    def send_query(self, query: str, timeout: float = 120.0) -> str:
        self.queue_sends.append(query)
        raise AssertionError(
            "the spawn site delivered the query through the thread's queues in "
            "process mode; nothing consumes them there, so this would block for "
            "the whole budget and then report a healthy worker as dead"
        )

    # ── Attributes the spawn envelope reads back ──
    def _last_elapsed(self) -> Optional[float]:
        return None


def _child_pids_of(pid: int) -> List[int]:
    """Direct children of *pid*, from ``/proc`` (no psutil dependency)."""
    children: List[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:  # pragma: no cover - non-Linux
        return children
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
        except OSError:
            continue
        # ``pid (comm) state ppid ...`` -- comm may contain spaces and parens.
        try:
            fields = stat[stat.rindex(")") + 2 :].split()
        except ValueError:  # pragma: no cover - malformed stat
            continue
        if len(fields) < 2:
            continue
        if fields[1].isdigit() and int(fields[1]) == pid:
            children.append(int(entry))
    return children


# ─────────────────────────────────────────────────────────────────────────────
# Driving the real spawn site
# ─────────────────────────────────────────────────────────────────────────────


def _spawn_with_query(tmp_path, monkeypatch, created: List[Any]) -> Any:
    """Run the REAL ``Worker._action_spawn`` with ``context={'query': ...}``.

    Only leaves are patched -- the thread class, the workspace resolver and the
    container manager -- so ``attach_thread_frame_sink`` (the mode decision and
    handle construction) and the auto-query dispatch run unpatched.
    """
    session_id = f"sess-c73-{abs(hash(str(tmp_path)))}"

    def _factory(**kwargs: Any) -> _NeverStartedBody:
        thread = _NeverStartedBody(**kwargs)
        created.append(thread)
        return thread

    monkeypatch.setattr(worker_module, "WorkerThread", _factory)
    monkeypatch.setattr(worker_module, "_workspace_dir", lambda ws_id: tmp_path)
    monkeypatch.setattr(worker_thread_module, "CAPABILITIES_AVAILABLE", True)
    monkeypatch.setattr(worker_thread_module, "_CM", None)

    tool = worker_module.Worker(
        action="spawn",
        worker_name=WORKER,
        session_id=session_id,
        workspace_path=str(tmp_path),
        agent_config={"provider": "openai", "model": "gpt-4"},
        context={"query": QUERY},
    )
    started = time.monotonic()
    result = tool._action_spawn(
        [{"name": WORKER, "status": "ready"}], f"ws-c73-{abs(hash(str(tmp_path)))}"
    )
    elapsed = time.monotonic() - started
    assert created, "the spawn site never constructed a thread"
    return session_id, result, created[-1], elapsed


def _cleanup(session_id: str, thread: Any) -> None:
    handle = getattr(thread, "_runtime_handle", None)
    if handle is not None:
        try:
            handle.close(grace_ms=2000)
        except Exception:  # pragma: no cover - teardown is best effort
            pass
    worker_module._worker_registry.pop((session_id, WORKER, 1), None)


# ─────────────────────────────────────────────────────────────────────────────
# The regression
# ─────────────────────────────────────────────────────────────────────────────


def test_spawn_query_reaches_the_child_not_the_queues(tmp_path, monkeypatch):
    """C73: a process-mode spawn BLOCKS on the child and returns its answer.

    Red on the old tree four different ways, and each red names a distinct
    defect: the envelope carried ``"did not respond within ..."`` (the freeze),
    no child pid existed (nothing was ever asked), the reply pid was this
    interpreter's (the thread's queue answered), or the envelope carried
    ``StaleWorkerFence`` (the handle refused to fork).
    """
    monkeypatch.setenv(WORKER_RUNTIME_MODE_ENV, MODE_PROCESS)
    assert resolve_runtime_mode() == MODE_PROCESS, (
        "precondition: the mode flag must be readable from the environment"
    )

    created: List[Any] = []
    session_id, result, thread, elapsed = _spawn_with_query(
        tmp_path, monkeypatch, created
    )
    try:
        assert thread._runtime_mode == MODE_PROCESS, (
            "the spawn site did not stamp the thread with a process runtime "
            f"mode (got {thread._runtime_mode!r}); this test would then be "
            "asserting nothing about process mode"
        )

        # (1) No error, and the answer is the child's sentinel.
        dumped = json.dumps(result, default=str)
        assert "StaleWorkerFence" not in dumped, (
            "the handle fenced a live child out again: a process-mode worker "
            "has no supervisor thread, so is_alive() must not be used as death "
            f"evidence. envelope: {dumped}"
        )
        assert "error" not in result, (
            f"the spawn reported a failure instead of the child's answer: {result!r}"
        )
        assert result.get("spawned") is True, f"the spawn itself failed: {result!r}"
        response = result.get("response")
        assert isinstance(response, str) and response.startswith(_REPLY_PREFIX), (
            "the spawn envelope does not carry the child's sentinel reply "
            f"(got {response!r}) -- the answer did not come from the child"
        )

        # (2) The answering pid is a different OS process.
        child_pid = int(response[len(_REPLY_PREFIX) :].split(":", 1)[0])
        assert child_pid != os.getpid(), (
            f"the reply claims to come from pid {child_pid}, which is this "
            "interpreter: the 'child' is not a separate OS process"
        )

        # (3) And it really is our child.
        assert child_pid in _child_pids_of(os.getpid()), (
            f"pid {child_pid} answered but is not a child of this process "
            f"({os.getpid()}); children: {_child_pids_of(os.getpid())}"
        )

        # (4) The queue route was never taken.
        assert thread.queue_sends == [], (
            "the spawn query was delivered through the thread's queues in "
            f"process mode: {thread.queue_sends!r}"
        )

        # (5) The predicate that used to fence: still False, and harmless.
        assert thread.is_alive() is False, (
            "precondition of this test: in process mode the supervisor thread "
            "is never started, so is_alive() is False"
        )

        # (6) Nothing waited for the 63 s budget of a queue with no producer.
        assert elapsed < MAX_SPAWN_SECONDS, (
            f"the spawn blocked for {elapsed:.1f}s (budget {MAX_SPAWN_SECONDS}s): "
            "the auto-query wait went back to a primitive with no producer"
        )
    finally:
        _cleanup(session_id, thread)


def test_spawn_fails_fast_when_process_mode_has_no_handle(tmp_path, monkeypatch):
    """The guard that replaced the freeze: no handle in process mode is loud.

    If the spawn site ever builds a non-thread worker without a usable handle,
    the only honest answers are "error now" or "block for 63 s and blame the
    worker". This pins the first one, so a future refactor cannot quietly
    restore the second.
    """
    monkeypatch.setenv(WORKER_RUNTIME_MODE_ENV, MODE_PROCESS)
    created: List[Any] = []

    def _handleless(**kwargs: Any) -> _NeverStartedBody:
        thread = _NeverStartedBody(**kwargs)
        # Simulate the supervisor bug: stamped process, but no handle reached
        # the thread, so nothing can be dispatched.
        object.__setattr__(thread, "_runtime_handle", None)
        created.append(thread)
        return thread

    def _attach_nothing(thread: Any) -> None:
        """Stand in for ``attach_thread_frame_sink``: resolve the mode, no handle.

        This is the only way to reach the guard. ``_action_spawn`` imports
        ``attach_thread_frame_sink`` function-locally and assigns its return
        value to ``thread._runtime_handle``, so clearing the attribute in the
        thread factory is undone a few lines later by the real attachment. The
        seam under test is exactly "mode resolved, handle absent", which is what
        a supervisor bug looks like from the spawn site's point of view.
        """
        thread._runtime_mode = MODE_PROCESS
        thread._runtime_handle = None
        return None

    monkeypatch.setattr(
        worker_runtime_module, "attach_thread_frame_sink", _attach_nothing
    )
    monkeypatch.setattr(
        worker_runtime_module, "register_runtime_handle", lambda *a, **k: None
    )
    monkeypatch.setattr(worker_module, "WorkerThread", _handleless)
    monkeypatch.setattr(worker_module, "_workspace_dir", lambda ws_id: tmp_path)
    monkeypatch.setattr(worker_thread_module, "CAPABILITIES_AVAILABLE", True)
    monkeypatch.setattr(worker_thread_module, "_CM", None)

    session_id = f"sess-c73-nohandle-{abs(hash(str(tmp_path)))}"
    tool = worker_module.Worker(
        action="spawn",
        worker_name=WORKER,
        session_id=session_id,
        workspace_path=str(tmp_path),
        agent_config={"provider": "openai", "model": "gpt-4"},
        context={"query": QUERY},
    )
    started = time.monotonic()
    result = tool._action_spawn(
        [{"name": WORKER, "status": "ready"}], f"ws-c73-nh-{abs(hash(str(tmp_path)))}"
    )
    elapsed = time.monotonic() - started
    try:
        if created and getattr(created[-1], "_runtime_mode", None) != MODE_PROCESS:
            pytest.skip("the spawn site did not resolve process mode on this tree")
        assert "error" in result, (
            "a process-mode spawn with no usable handle returned a success "
            f"envelope: {result!r}"
        )
        assert "no runtime handle" in str(result.get("error")), (
            "the error does not name the real cause (the missing handle): "
            f"{result.get('error')!r}"
        )
        assert elapsed < MAX_SPAWN_SECONDS, (
            f"the spawn blocked for {elapsed:.1f}s before reporting the missing "
            "handle: the fail-fast guard is not in force"
        )
    finally:
        _cleanup(session_id, created[-1] if created else None)


# ──────────────────────────────────────────────────────────────────────────────
# C73 part 2: the same fork hole in query / submit_query (the async routes)
# ──────────────────────────────────────────────────────────────────────────────
#
# The spawn fix above repaired ONE caller. ``action='query'`` (on a detached
# worker) and ``action='submit_query'`` reach the body by a different route and
# carried the same two defects:
#
# 1. **The guard.** Both opened with ``if not thread.is_alive(): return "Worker
#    '...' is no longer alive"``. In process mode the supervisor thread is never
#    started, so that answer was returned for every healthy worker -- before any
#    child was asked anything.
# 2. **The transport.** Both then enqueued into ``thread._input_queue`` /
#    ``withdraw_queued_query`` bookkeeping that has NO consumer in process mode:
#    a query put there is accepted and never runs, and the job record it
#    registered never reaches a terminal state (the child's WORKER_* events stay
#    in the child's interpreter).
#
# The fix has two halves, and these tests pin both without spawning a child:
# ``worker_is_dispatchable`` (mode-first liveness) and
# ``enqueue_query_to_worker`` (frame dispatch + an ``on_result`` reporter that
# closes the registry record cross-process). The handle is the seam: it is
# faked, everything else -- the real ``Worker._action_query`` /
# ``_action_submit_query``, the real helpers, the real ``WorkerJobRegistry`` --
# runs unpatched.


class _FakeReply:
    """The shape ``ProcessWorkerRuntime`` hands back: ``.ok`` + ``.envelope``."""

    def __init__(self, content: str) -> None:
        self.query_id = "unused"
        self.ok = True
        self.envelope = {"status": "ok", "response": content, "content": content}
        self.elapsed_seconds = 0.0


class _RecordingRuntimeHandle:
    """A runtime handle that records the frame instead of crossing a process.

    ``dispatch_query_async`` is the whole transport contract the submission
    paths use. Recording ``on_result`` (rather than only calling it) is what
    lets a test drive the reply *after* the tool has returned -- the ordering
    production has -- and the ``auto_reply`` variant reproduces the opposite
    ordering, a child that answers before the supervisor finishes registering.
    """

    mode = MODE_PROCESS

    def __init__(self, auto_reply: Optional[_FakeReply] = None) -> None:
        self.auto_reply = auto_reply
        self.calls: List[dict] = []
        self.closed = False

    def dispatch_query_async(self, query, *, timeout, query_id=None, on_result=None):
        self.calls.append(
            {
                "query_id": query_id,
                "query": query,
                "timeout": timeout,
                "on_result": on_result,
            }
        )
        if self.auto_reply is not None and on_result is not None:
            on_result(query_id, self.auto_reply, None)
        return query_id

    def close(self, **kwargs: Any) -> None:
        self.closed = True


def _dispatch_via_runtime(
    tmp_path,
    monkeypatch,
    *,
    action: str,
    handle: Optional[_RecordingRuntimeHandle],
    status: str,
    registry: Any,
):
    """Drive the REAL ``_action_query`` / ``_action_submit_query`` for *action*.

    ``_resolve_worker_thread`` and ``_get_worker_job_registry`` are the only
    seams patched: the first hands back the supervisor-side object a process-mode
    spawn leaves behind (a thread that was never started), the second keeps the
    job records of this test out of the shared registry.
    """
    thread = _NeverStartedBody(name=WORKER, instance_id="c73-async-1")
    # Stamped exactly as ``attach_thread_frame_sink`` stamps it in process mode.
    thread._runtime_mode = MODE_PROCESS
    thread._runtime_handle = handle
    thread.status = status
    session_id = f"sess-c73-async-{abs(hash(str(tmp_path)))}"

    monkeypatch.setattr(
        worker_thread_module,
        "_resolve_worker_thread",
        lambda *a, **k: (thread, session_id, thread.instance_id),
    )
    monkeypatch.setattr(
        worker_thread_module, "_get_worker_job_registry", lambda: registry
    )

    tool = worker_thread_module.Worker(
        action=action,
        worker_name=WORKER,
        worker_query=QUERY,
        session_id=session_id,
        workspace_path=str(tmp_path),
        agent_config={"provider": "openai", "model": "gpt-4"},
    )
    if action == "query":
        result = tool._action_query([])
    else:
        result = tool._action_submit_query([])
    return result, thread, session_id


@pytest.mark.parametrize(
    "action,status",
    [("query", "detached"), ("submit_query", "ready")],
)
def test_async_routes_dispatch_to_the_child_runtime_not_the_queues(
    tmp_path, monkeypatch, action, status
):
    """C73: query/submit_query reach a process-mode body and the job CLOSES.

    Red on the old tree twice over: the envelope was
    ``"Worker 'c73-w' is no longer alive (status: detached)"`` (the is_alive
    guard, returned before anything was dispatched), and even past that guard
    the query went into ``_input_queue`` -- ``queue_sends`` non-empty, the
    handle never touched, and the job record stuck at ``submitted`` forever.
    """
    handle = _RecordingRuntimeHandle()
    registry = WorkerJobRegistry()
    result, thread, session_id = _dispatch_via_runtime(
        tmp_path,
        monkeypatch,
        action=action,
        handle=handle,
        status=status,
        registry=registry,
    )
    try:
        # (1) The guard answered with a death that did not happen.
        assert "no longer alive" not in json.dumps(result, default=str), (
            "a live child was reported dead: process mode never starts the "
            f"supervisor thread, so is_alive() must not gate dispatch. {result!r}"
        )
        assert "error" not in result, f"{action} failed instead of dispatching: {result!r}"
        assert result.get("status") == "submitted", f"unexpected envelope: {result!r}"

        # (2) It went through the runtime handle, on the job's own id.
        assert len(handle.calls) == 1, (
            f"the child runtime received {len(handle.calls)} dispatch(es) for "
            f"{action}; expected exactly one: {handle.calls!r}"
        )
        call = handle.calls[0]
        assert call["query"] == QUERY, f"wrong query crossed the boundary: {call!r}"
        assert call["query_id"] == result["job_id"], (
            "the frame's query_id does not match the returned job_id, so the "
            f"child's reply could never be matched to the job: {call!r} vs "
            f"{result['job_id']!r}"
        )
        assert call["on_result"] is not None, (
            "no reply reporter was wired: a cross-process job can then never "
            "reach a terminal registry state (the child's WORKER_* events stay "
            "in the child's interpreter)"
        )
        assert call["timeout"] > 0, f"the dispatch carried no budget: {call!r}"

        # (3) The queues were never used.
        assert thread.queue_sends == [], (
            f"the query went through the thread's queues in process mode: "
            f"{thread.queue_sends!r}"
        )

        # (4) The job is registered, then closed by the child's reply.
        record = registry.job(result["job_id"])
        assert record is not None, "the accepted job was never registered"
        assert record["status"] == "submitted", (
            f"a queued job must start out in flight, got {record['status']!r}"
        )
        call["on_result"](result["job_id"], _FakeReply("child-answer"), None)
        record = registry.job(result["job_id"])
        assert record["status"] == "completed", (
            "the child answered but the job stayed "
            f"{record['status']!r}: the reporter is not wired to the registry"
        )
        assert record["result"]["content"] == "child-answer"

        # (5) The predicate that used to fence: still False, and now harmless.
        assert thread.is_alive() is False, (
            "precondition: in process mode the supervisor thread is never started"
        )
    finally:
        _cleanup(session_id, thread)


def test_fast_cross_process_reply_is_not_resurrected_by_registration(
    tmp_path, monkeypatch
):
    """A reply that beats ``register()`` must leave the job COMPLETED.

    The submission paths enqueue first and register second (all-or-nothing), so
    the reply can arrive before the record exists. ``complete()`` creates it on
    demand -- and a plain unconditional ``register()`` would then overwrite that
    terminal record with ``submitted``, leaving a finished job looking in flight
    to every later ``job_status`` / ``join``. This pins the ordering in the
    worst case rather than the usual one.
    """
    handle = _RecordingRuntimeHandle(auto_reply=_FakeReply("answered-immediately"))
    registry = WorkerJobRegistry()
    result, thread, session_id = _dispatch_via_runtime(
        tmp_path,
        monkeypatch,
        action="submit_query",
        handle=handle,
        status="ready",
        registry=registry,
    )
    try:
        assert "error" not in result, f"submit_query failed: {result!r}"
        record = registry.job(result["job_id"])
        assert record is not None, "no job record at all"
        assert record["status"] == "completed", (
            "registration resurrected a job the child had already answered: "
            f"status is {record['status']!r}, result is {record['result']!r}"
        )
        assert record["worker_name"] == WORKER, (
            "the terminal record lost its worker attribution: "
            f"{record.get('worker_name')!r}"
        )
    finally:
        _cleanup(session_id, thread)


@pytest.mark.parametrize(
    "action,status",
    [("query", "detached"), ("submit_query", "ready")],
)
def test_async_routes_without_a_handle_fail_loudly_not_as_death(
    tmp_path, monkeypatch, action, status
):
    """Stamped process mode, no handle: say THAT, do not invent a death.

    The guard that replaced ``is_alive()`` must not swing all the way to "always
    dispatchable" either: a supervisor bug that loses the handle has to surface
    as itself (``WorkerQueryTransportMissing``), because the alternative is a
    job_id that no body will ever run.
    """
    registry = WorkerJobRegistry()
    result, thread, session_id = _dispatch_via_runtime(
        tmp_path,
        monkeypatch,
        action=action,
        handle=None,
        status=status,
        registry=registry,
    )
    try:
        dumped = json.dumps(result, default=str)
        assert "no longer alive" not in dumped, (
            f"the missing handle was reported as a dead worker: {result!r}"
        )
        assert "error" in result, (
            f"a submission with no transport returned a success envelope: {result!r}"
        )
        assert "WorkerQueryTransportMissing" in str(result.get("error")), (
            "the error does not name the real fault (no runtime handle): "
            f"{result.get('error')!r}"
        )
        assert "job_id" not in result, (
            "a job_id was handed back for a query that nothing will ever run: "
            f"{result!r}"
        )
        assert registry.jobs(worker_name=WORKER) == [], (
            "a job record was created for a query that was never submitted"
        )
    finally:
        _cleanup(session_id, thread)

