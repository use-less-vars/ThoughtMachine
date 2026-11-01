"""PHASE 2.1b -- FIFO query queue for a detached worker.

Proves the contracts in ``docs/design-worker-lifecycle.md`` ("Queries
submitted while the worker is detached"):

  1. A query submitted while the worker is DETACHED is queued and accepted
     (never rejected), and the caller gets its ``job_id`` back at enqueue
     time -- the submission does not resume the worker.
  2. Strict FIFO, one queue per worker INSTANCE: the queue is drained
     head-first and a later submission appends behind the running turn.
  3. No token burn while queued: enqueueing never sets ``_resume_event``,
     so a parked worker stays parked.
  4. TTL expiry rejects LOUDLY: the operator-facing message is
     ``worker '<name>' detached for longer than WORKER_TIMEOUT_SECONDS
     (600 s) - queued query discarded``, and the rejection surfaces on the
     runtime channel (WORKER_ERROR carrying the query_id) AND in the audit
     log -- not merely as a polled job status.
  5. Kill (and therefore the session Stop cascade) discards the queue the
     same way.

The tests drive the REAL ``WorkerThread`` run loop and the REAL
``WorkerJobRegistry``; only the LLM/agent layer is faked. No Docker, no
network, no LLM.

Run:  python3 -m pytest tests/test_worker_detached_queue.py -q
"""

import json
import time

import pytest

from agent.config.defaults import WORKER_TIMEOUT_SECONDS
from agent.events import EventType
import thoughtmachine.audit_logger as audit_logger
from tools.workspace import worker as worker_module
from tools.workspace.job_registry import WorkerJobRegistry
from tools.workspace.worker import Worker, WorkerThread
from tools.workspace.worker_manager import PeriodicStaleCheck

# -- fakes -----------------------------------------------------------------


class _FakeContext:
    """Minimal WorkerContext stand-in (run() reads a few methods only)."""

    def __init__(self):
        self.user_history = [{"role": "system", "content": "test worker"}]

    def compact_after_summary(self):
        pass

    def estimated_context_tokens(self):
        return 10

    def to_persistable_dict(self):
        return {"user_history": self.user_history}


class _FakeAgent:
    """Deliberately has NO ``state`` attribute (worker.py gates on hasattr)."""


class _RecordingBus:
    """Captures published events; satisfies ``subscribe`` for the registry."""

    def __init__(self):
        self.events = []
        self._callbacks = []

    def publish(self, event):
        self.events.append(event)
        for cb in list(self._callbacks):
            cb(event)

    def subscribe(self, event_type, callback):
        self._callbacks.append(callback)


class _FakeStaleRegistry:
    """Registry-shaped double for PeriodicStaleCheck (get_all + unregister)."""

    def __init__(self, workers):
        self._workers = dict(workers)
        self.unregistered = []

    def get_all_workers(self):
        return dict(self._workers)

    def unregister_worker(self, *args, **kwargs):
        self.unregistered.append((args, kwargs))
        return True


def _wait_until(predicate, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# -- fixtures --------------------------------------------------------------


@pytest.fixture(autouse=True)
def _thread_teardown():
    """Kill/join every thread registered by a test so nothing leaks."""
    with worker_module._registry_lock:
        preexisting = set(worker_module._worker_registry.keys())
    yield
    with worker_module._registry_lock:
        added = [k for k in worker_module._worker_registry.keys() if k not in preexisting]
        threads = [worker_module._worker_registry.pop(k, None) for k in added]
    for thread in threads:
        if thread is None:
            continue
        try:
            thread.kill()
        except Exception:
            pass
        thread.join(timeout=5)


@pytest.fixture
def registry(monkeypatch):
    """Install a REAL WorkerJobRegistry as the worker.py singleton."""
    reg = WorkerJobRegistry()
    monkeypatch.setattr(worker_module, "_WORKER_JOB_REGISTRY", reg, raising=False)
    return reg


@pytest.fixture
def bus(monkeypatch):
    """Capture global-bus publishes (``_publish_global_worker_event`` reads it)."""
    recording = _RecordingBus()
    monkeypatch.setattr(worker_module, "global_event_bus", recording, raising=False)
    return recording


@pytest.fixture
def audited(monkeypatch):
    """Capture ``thoughtmachine.audit_logger.audit_event`` calls."""
    calls = []

    def _recorder(event_type, details):
        calls.append((event_type, details))

    monkeypatch.setattr(audit_logger, "audit_event", _recorder, raising=False)
    return calls


# -- helpers ---------------------------------------------------------------


def _spawn_worker(tmp_path, monkeypatch, name="w-queue", session_id="s-queue"):
    monkeypatch.setattr(WorkerThread, "_load_context", lambda self: _FakeContext())
    monkeypatch.setattr(WorkerThread, "_save_context", lambda self: None)
    monkeypatch.setattr(WorkerThread, "_heartbeat_tick", lambda self: None)
    thread = WorkerThread(
        name=name,
        definition={"name": name, "system_prompt": "You are a test worker."},
        agent_config={},
        workspace_dir=tmp_path,
        session_id=session_id,
        max_container_count=4,
        max_token_usage=None,
        max_runtime_s=None,
    )
    # A worker with no agent config would break out of the run loop on its
    # first turn ("Cannot create Agent: invalid agent_config") instead of
    # parking; a stand-in agent keeps the loop parked. ``_last_elapsed_val``
    # is normally written by the real ``_run_tool_loop``, which these tests
    # replace, so seed it here.
    thread._agent = _FakeAgent()
    thread._last_elapsed_val = 0.0
    with worker_module._registry_lock:
        worker_module._worker_registry[(session_id, name, 1)] = thread
    thread.start()
    assert _wait_until(lambda: thread.status == "ready"), (
        f"worker {name!r} did not reach ready "
        f"(status={thread.status}, error={thread.error})"
    )
    return thread


def _detach(thread):
    """Detach AND wait for the run loop to actually park on the sentinel.

    ``detach()`` sets ``status`` synchronously, so waiting on the status alone
    races the run loop: a query enqueued (or a resume issued) before the loop
    has consumed the ``None`` sentinel would see it treated as the legacy stop
    sentinel and the worker would exit.  The sentinel is the only item in the
    queue at that point, so an empty queue means the loop consumed it and is
    inside the detached park branch.
    """
    thread.detach()
    assert _wait_until(
        lambda: thread.status == "detached" and thread._pause_event.is_set()
    ), (
        f"worker did not park as detached (status={thread.status})"
    )
    # Give the run loop time to consume the detach sentinel and enter the park
    # branch, so a query enqueued next is never mistaken for a stop signal.
    time.sleep(0.4)
    assert thread.is_alive(), "worker exited instead of parking as detached"


def _record_turns(thread):
    """Replace the tool loop with a recorder so turns are instant."""
    seen = []

    def _run_tool_loop(query):
        seen.append((thread._current_query_id, query))
        return f"reply:{query}"

    thread._run_tool_loop = _run_tool_loop
    return seen


# -- 1 + 3: queued, not resumed, no token burn -----------------------------


class TestEnqueueParksWithoutResuming:
    def test_enqueue_does_not_resume_and_is_introspectable(
        self, tmp_path, monkeypatch, registry
    ):
        thread = _spawn_worker(tmp_path, monkeypatch)
        _detach(thread)
        assert thread._resume_event.is_set() is False

        thread.enqueue_query("job-a", "first")

        # Contract 3: enqueueing must NOT set _resume_event (no token burn).
        assert thread._resume_event.is_set() is False
        assert thread._pause_event.is_set() is True
        # Contract 1: accepted + queued, introspectable in FIFO order.
        assert thread.pending_query_jobs() == ["job-a"]
        time.sleep(0.3)
        assert thread.status == "detached"
        assert thread._current_query_id is None, "a queued query must not start"
        assert thread.pending_query_jobs() == ["job-a"]

    def test_one_queue_per_instance(self, tmp_path, monkeypatch, registry):
        first = _spawn_worker(tmp_path, monkeypatch, name="w-one")
        second = _spawn_worker(tmp_path, monkeypatch, name="w-two")
        for thread in (first, second):
            _detach(thread)

        first.enqueue_query("job-first", "q1")
        second.enqueue_query("job-second", "q2")

        assert first.pending_query_jobs() == ["job-first"]
        assert second.pending_query_jobs() == ["job-second"]

    def test_pending_queue_is_strict_fifo(self, tmp_path, monkeypatch, registry):
        thread = _spawn_worker(tmp_path, monkeypatch)
        _detach(thread)

        for index in range(3):
            thread.enqueue_query(f"job-{index}", f"q{index}")

        assert thread.pending_query_jobs() == ["job-0", "job-1", "job-2"]
        # The queue itself is FIFO too: head-first drain order is observable.
        queued = []
        while not thread._input_queue.empty():
            item = thread._input_queue.get_nowait()
            if isinstance(item, tuple):
                queued.append(item[0])
        assert queued == ["job-0", "job-1", "job-2"]


# -- 2: the run loop drains head-first after an operator resume ------------


class TestResumeDrainsQueueInOrder:
    def test_resume_drains_head_first(self, tmp_path, monkeypatch, registry):
        thread = _spawn_worker(tmp_path, monkeypatch)
        seen = _record_turns(thread)
        _detach(thread)

        for index in range(3):
            thread.enqueue_query(f"job-{index}", f"q{index}")
        assert thread.pending_query_jobs() == ["job-0", "job-1", "job-2"]

        # Operator resume is the ONLY trigger that drains the queue.
        assert thread.resume() is True
        assert _wait_until(lambda: len(seen) == 3, timeout=15.0), (
            f"queue did not drain (seen={seen})"
        )

        assert [query for _job_id, query in seen] == ["q0", "q1", "q2"]
        assert [job_id for job_id, _query in seen] == ["job-0", "job-1", "job-2"]
        assert _wait_until(lambda: thread.pending_query_jobs() == []), (
            f"queue bookkeeping leaked: {thread.pending_query_jobs()}"
        )

    def test_later_submission_queues_behind_the_running_turn(
        self, tmp_path, monkeypatch, registry
    ):
        thread = _spawn_worker(tmp_path, monkeypatch)
        gate = {"open": False}
        seen = []

        def _run_tool_loop(query):
            seen.append(query)
            while not gate["open"]:
                time.sleep(0.02)
            return f"reply:{query}"

        thread._run_tool_loop = _run_tool_loop
        thread.enqueue_query("job-running", "running")
        assert _wait_until(lambda: seen == ["running"], timeout=15.0)

        # While the first turn is in flight, the queue is empty...
        assert thread.pending_query_jobs() == []
        # ...and a later submission simply appends behind it.
        thread.enqueue_query("job-later", "later")
        assert thread.pending_query_jobs() == ["job-later"]

        gate["open"] = True
        assert _wait_until(lambda: seen == ["running", "later"], timeout=15.0), seen
        assert _wait_until(lambda: thread.pending_query_jobs() == [])


# -- 1: the tool actions ---------------------------------------------------


class TestQueryActionAgainstDetachedWorker:
    def test_query_enqueues_and_returns_job_id(self, tmp_path, monkeypatch, registry):
        thread = _spawn_worker(tmp_path, monkeypatch)
        _detach(thread)

        result = json.loads(
            Worker(
                action="query",
                worker_name="w-queue",
                worker_query="do the thing",
                session_id="s-queue",
            ).execute()
        )

        # Contract 1: the caller keeps the submit_query contract.
        assert result["job_id"], result
        assert result["status"] == "submitted"
        assert "response" not in result, "a queued query cannot have a response"
        assert "error" not in result
        assert "queued" in result["note"].lower()

        # It was QUEUED, never a resume (ruling i: operator-only resume).
        assert thread.status == "detached"
        assert thread._resume_event.is_set() is False
        assert thread.pending_query_jobs() == [result["job_id"]]
        record = registry.job(result["job_id"])
        assert record is not None
        assert record["status"] == "submitted"
        assert record["worker_name"] == "w-queue"

    def test_query_against_running_worker_still_blocks(self, tmp_path, monkeypatch, registry):
        """Regression guard: the PAUSED/running paths keep the blocking contract."""
        thread = _spawn_worker(tmp_path, monkeypatch)
        _record_turns(thread)

        result = json.loads(
            Worker(
                action="query",
                worker_name="w-queue",
                worker_query="now",
                session_id="s-queue",
            ).execute()
        )
        # The blocking path returns the worker's envelope, not a bare string.
        payload = json.loads(result["response"])
        assert payload["content"] == "reply:now", payload
        assert "job_id" not in result

    def test_submit_query_appends_to_the_same_queue(self, tmp_path, monkeypatch, registry):
        thread = _spawn_worker(tmp_path, monkeypatch)
        _detach(thread)

        result = json.loads(
            Worker(
                action="submit_query",
                worker_name="w-queue",
                worker_query="async",
                session_id="s-queue",
            ).execute()
        )
        assert result["status"] == "submitted"
        assert thread.pending_query_jobs() == [result["job_id"]]
        assert thread._resume_event.is_set() is False


# -- 4: TTL expiry rejects loudly -----------------------------------------


class TestDetachedTtlRejectsQueue:
    def test_ttl_rejects_on_runtime_channel_and_audit_log(
        self, tmp_path, monkeypatch, registry, bus, audited
    ):
        thread = _spawn_worker(tmp_path, monkeypatch)
        _detach(thread)
        thread.enqueue_query("job-1", "one")
        thread.enqueue_query("job-2", "two")
        thread._detached_at = time.time() - (WORKER_TIMEOUT_SECONDS + 60)

        stale = PeriodicStaleCheck(_FakeStaleRegistry({("s-queue", "w-queue", 1): thread}))
        stale._check_once()

        expected = (
            f"worker 'w-queue' detached for longer than "
            f"WORKER_TIMEOUT_SECONDS ({WORKER_TIMEOUT_SECONDS} s) "
            f"\u2014 queued query discarded"
        )
        for job_id in ("job-1", "job-2"):
            record = registry.job(job_id)
            assert record is not None
            assert record["status"] == "error", record
            assert record["result"]["error"] == expected
        assert thread.pending_query_jobs() == []

        # Ruling (ii): the rejection is announced on the RUNTIME channel with
        # the query_id, not just written into a job record.
        errors = [e for e in bus.events if e.type == EventType.WORKER_ERROR]
        assert [e.data["query_id"] for e in errors] == ["job-1", "job-2"]
        assert all(e.data["error"] == expected for e in errors)

        # ...and in the audit log (the TTL-kill announcement is audited first,
        # so locate the rejection entry rather than trusting the order).
        rejections = [
            details for event_type, details in audited
            if event_type == "WORKER_DETACHED_TTL_QUEUE_REJECTED"
        ]
        assert len(rejections) == 1, audited
        assert "job-1" in rejections[0] and "job-2" in rejections[0]

        # Rejection happened BEFORE the kill: the thread is now dead.
        assert _wait_until(lambda: thread.is_alive() is False, timeout=10.0)


# -- 5: kill discards the queue -------------------------------------------


class TestKillDiscardsQueue:
    def test_kill_rejects_queued_queries(self, tmp_path, monkeypatch, registry, bus, audited):
        thread = _spawn_worker(tmp_path, monkeypatch)
        _detach(thread)
        thread.enqueue_query("job-k1", "one")
        thread.enqueue_query("job-k2", "two")

        thread.kill()

        expected = "worker 'w-queue' killed \u2014 queued query discarded"
        for job_id in ("job-k1", "job-k2"):
            record = registry.job(job_id)
            assert record["status"] == "error", record
            assert record["result"]["error"] == expected
        assert thread.pending_query_jobs() == []
        errors = [e for e in bus.events if e.type == EventType.WORKER_ERROR]
        assert [e.data["query_id"] for e in errors] == ["job-k1", "job-k2"]
        assert [c[0] for c in audited] == ["WORKER_QUEUED_QUERY_REJECTED"]

    def test_kill_with_empty_queue_is_silent(
        self, tmp_path, monkeypatch, registry, bus, audited
    ):
        thread = _spawn_worker(tmp_path, monkeypatch)
        _detach(thread)

        thread.kill()

        assert thread.pending_query_jobs() == []
        assert [e for e in bus.events if e.type == EventType.WORKER_ERROR] == []
        assert audited == []


# -- 6: accept is all-or-nothing (queue AND registry) ----------------------


class TestAcceptIsAllOrNothing:
    """A query is accepted only when it is BOTH queued and registered.

    The positive case resolves the job through the registry's OWN API (the
    ``job_status`` action reading the real ``WorkerJobRegistry``) rather than
    trusting a mocked call log: a drifted ``register`` signature would store
    nothing and the lookup would miss.
    """

    def test_accepted_query_is_retrievable_through_job_status(
        self, tmp_path, monkeypatch, registry
    ):
        thread = _spawn_worker(tmp_path, monkeypatch)
        _detach(thread)

        result = json.loads(
            Worker(
                action="query",
                worker_name="w-queue",
                worker_query="queued work",
                session_id="s-queue",
            ).execute()
        )
        job_id = result["job_id"]
        assert result["status"] == "submitted"
        assert thread.pending_query_jobs() == [job_id]

        polled = json.loads(
            Worker(
                action="job_status",
                worker_name="w-queue",
                worker_query=job_id,
                session_id="s-queue",
            ).execute()
        )
        assert polled.get("error") is None, polled
        assert polled["job_id"] == job_id
        # "submitted" IS the queued state: the job exists and has not run.
        assert polled["status"] == "submitted"
        assert polled["worker_name"] == "w-queue"
        assert polled["has_result"] is False
        # The registry holds exactly that one job, keyed by the returned id.
        assert [j["job_id"] for j in registry.jobs(worker_name="w-queue")] == [job_id]

    def test_register_failure_leaves_no_orphan_and_no_success(
        self, tmp_path, monkeypatch, registry
    ):
        thread = _spawn_worker(tmp_path, monkeypatch)
        _detach(thread)

        def _drifted_register(job_id, worker_name, session_id):
            """Signature drift: the real call also passes ``instance_id=``."""
            raise TypeError(
                "register() got an unexpected keyword argument 'instance_id'"
            )

        monkeypatch.setattr(registry, "register", _drifted_register)

        result = json.loads(
            Worker(
                action="query",
                worker_name="w-queue",
                worker_query="doomed",
                session_id="s-queue",
            ).execute()
        )

        # (a) no success-shaped response: no job handle, no "submitted" status,
        # and the failure is visible to the caller instead of swallowed.
        assert "error" in result, result
        assert "job_id" not in result, result
        assert result.get("status") != "submitted", result
        assert "unexpected keyword argument" in result["error"], result

        # (b) the queued entry was rolled back, not left as an orphan.
        assert thread.pending_query_jobs() == []
        leftover = []
        while not thread._input_queue.empty():
            item = thread._input_queue.get_nowait()
            if isinstance(item, tuple) and len(item) >= 2 and item[1] == "doomed":
                leftover.append(item)
        assert leftover == [], leftover
        # ...and nothing was recorded behind our back.
        assert registry.jobs(worker_name="w-queue") == []
