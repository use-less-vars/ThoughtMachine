"""
Phase 2B — non-blocking worker jobs (action='submit_query' / 'job_status').

Proves the 9 Phase 2B contracts:
  1. submit_query returns immediately (no blocking on any reply queue).
  2. WORKER_COMPLETED global event carries the job's query_id.
  3. WORKER_PARTIAL_RESULT (status=running, reason=partial) is emitted while
     work is in progress and carries the query_id.
  4. The synchronous action='query' path is unchanged (still blocks on a
     private reply queue and returns the full response envelope).
  5. Correlation ids are unique per submit and matched per job (no mix-up).
  6. A stale reply on the shared _output_queue cannot satisfy the next
     synchronous send_query caller (drained; only the matching query_id wins).
  7. No cross-talk between workers: jobs are namespaced per worker and events
     are tagged per worker.
  8. submit_query does not wait for the worker (timeout-free submit); a
     WORKER_TIMEOUT event marks the job 'timeout' in the registry.
  9. The registry is observable without any worker-thread interaction; reads
     are lock-protected deep copies.

Hermetic: mocks only, no real LLM, no Docker, no ~/.thoughtmachine, no
sleeps-as-sync, stdlib + agent.events only.

Run:  python3 -m pytest tests/test_worker_jobs.py -q
"""

import json
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.workspace.worker import Worker, WorkerThread  # noqa: E402
from tools.workspace.worker import _registry_lock, _worker_registry  # noqa: E402
from tools.workspace.job_registry import WorkerJobRegistry  # noqa: E402
from agent.events import EventType, create_event  # noqa: E402

SYS_PROMPT = "You are a helpful worker assistant."


def make_thread(workspace_dir, name="w-jobs", timeout=60, session_id="s1"):
    return WorkerThread(
        name=name,
        definition={"system_prompt": SYS_PROMPT},
        agent_config={"model": "gpt-4o"},
        workspace_dir=Path(workspace_dir),
        session_id=session_id,
        timeout_seconds=timeout,
    )


class _FakeEventBus:
    """Callable stand-in for agent.events.EventBus (no real subscription)."""

    def __init__(self, *a, **k):
        pass

    def publish(self, *a, **k):
        return None


class RecordingFake:
    """Records published events AND dispatches them to subscribed callbacks.

    A superset of the heartbeat test bus: add ``subscribe`` so the job
    registry can be fed through the real event flow (ensure_subscribed →
    publish → _on_event) instead of calling _on_event directly.
    """

    def __init__(self):
        self.events = []
        self._callbacks = []

    def subscribe(self, event_type, callback):
        self._callbacks.append(callback)

    def publish(self, evt):
        self.events.append(evt)
        for cb in list(self._callbacks):
            try:
                cb(evt)
            except Exception:
                pass


class _FakeThread:
    """Stand-in WorkerThread for registry/action tests (never processes
    the input queue, so any blocking in the action code would hang)."""

    def __init__(self, alive=True, status="ready", error=None):
        self._alive = alive
        self.status = status
        self.error = error
        self._input_queue = queue.Queue()
        self.send_query_calls = []

    def is_alive(self):
        return self._alive

    def send_query(self, query, timeout=120.0):
        self.send_query_calls.append(query)
        return json.dumps({"content": "sync reply", "query_id": "fake"})

    def _last_elapsed(self):
        return None

    @property
    def _last_reasoning(self):
        return None


class _FakeAgent:
    """Minimal agent for full run()-loop tests.

    Deliberately has NO ``state`` attribute: worker.py gates the per-query
    state reset on ``hasattr(self._agent, 'state')``, and the post-loop
    timeout probe reads ``self._agent.state`` inside try/except.
    """

    def process_query(self, query):
        yield {
            "type": "agent_responded",
            "content": "final answer",
            "reasoning": "r",
            "status": None,
            "confidence": None,
            "meta": None,
        }


class _RunSafetyPatches(unittest.TestCase):
    """Patches event plumbing; global bus is a RecordingFake. Also installs a
    fresh WorkerJobRegistry (fed by the fake bus) as the worker.py singleton
    so every test observes the exact registry the code under test uses."""

    def setUp(self):
        self.fake_bus = RecordingFake()
        patchers = [
            mock.patch("tools.workspace.worker.EventBus", new=_FakeEventBus),
            mock.patch("tools.workspace.worker.register_worker_event_bus",
                       new=lambda *a, **k: None),
            mock.patch("tools.workspace.worker.unregister_worker_event_bus",
                       new=lambda *a, **k: None),
            mock.patch("tools.workspace.worker.global_event_bus", new=self.fake_bus),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        self.registry = WorkerJobRegistry(event_bus=self.fake_bus)
        self.registry.ensure_subscribed()
        reg_patcher = mock.patch("tools.workspace.worker._WORKER_JOB_REGISTRY",
                                 new=self.registry)
        reg_patcher.start()
        self.addCleanup(reg_patcher.stop)
        super().setUp()


def _inject_fake(worker_name, fake, session_id="s1"):
    with _registry_lock:
        _worker_registry[(session_id, worker_name, 1)] = fake
    return fake


def _remove_fake(worker_name, session_id="s1"):
    with _registry_lock:
        _worker_registry.pop((session_id, worker_name, 1), None)


# ---------------------------------------------------------------------------
# 1. submit_query returns immediately
# ---------------------------------------------------------------------------

class TestSubmitQueryReturnsImmediately(_RunSafetyPatches):
    """_action_submit_query enqueues and returns; never waits on the worker."""

    def test_submit_query_returns_immediately(self):
        fake = _inject_fake("w-submit", _FakeThread())
        self.addCleanup(_remove_fake, "w-submit")
        result = json.loads(Worker(
            action="submit_query", worker_name="w-submit",
            worker_query="do it", session_id="s1",
        ).execute())
        self.assertEqual(result["status"], "submitted")
        self.assertTrue(result["job_id"])
        self.assertEqual(result["worker_name"], "w-submit")
        # The fake thread never processes the queue: if _action_submit_query
        # blocked on any reply, this test would hang — it returns instead.
        items = []
        while not fake._input_queue.empty():
            items.append(fake._input_queue.get_nowait())
        self.assertEqual(len(items), 1)
        job_id, query, reply_q = items[0]
        self.assertEqual(job_id, result["job_id"])
        self.assertEqual(query, "do it")
        self.assertIsNone(reply_q)  # async: no private reply queue
        rec = self.registry.job(result["job_id"])
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], "submitted")
        self.assertEqual(rec["worker_name"], "w-submit")
        self.assertEqual(rec["session_id"], "s1")


# ---------------------------------------------------------------------------
# 2. WORKER_COMPLETED carries query_id; 3. partial result while running
# ---------------------------------------------------------------------------

class _FullRunBase(_RunSafetyPatches):
    """Runs a real WorkerThread with a fake agent (no 'state' attr)."""

    def _run_async_query(self, worker_name, job_id):
        with tempfile.TemporaryDirectory() as tmp:
            thread = make_thread(tmp, name=worker_name, timeout=60, session_id="s1")
            thread._agent = _FakeAgent()
            thread._input_queue.put((job_id, "hello", None))
            thread._input_queue.put(None)
            thread.start()
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive(), "worker thread did not exit")
            return self.fake_bus


class TestCompletionEventCarriesQueryId(_FullRunBase):
    """Contract 2: run() completes the job and the global event tags it."""

    def test_completion_event_carries_query_id(self):
        self._run_async_query("w-run-c", "jobA-c")
        completed = [e for e in self.fake_bus.events
                     if e.type == EventType.WORKER_COMPLETED]
        self.assertTrue(completed, "no WORKER_COMPLETED event published")
        self.assertEqual(completed[-1].data["query_id"], "jobA-c")
        rec = self.registry.job("jobA-c")
        self.assertIsNotNone(rec, "job not recorded in registry")
        self.assertEqual(rec["status"], "completed")
        self.assertEqual(rec["result"]["content"], "final answer")


class TestPartialResultDuringRunning(_FullRunBase):
    """Contract 3: WORKER_PARTIAL_RESULT emitted mid-flight with query_id."""

    def test_partial_result_during_running(self):
        self._run_async_query("w-run-p", "jobA-p")
        running_partials = [
            e for e in self.fake_bus.events
            if e.type == EventType.WORKER_PARTIAL_RESULT
            and e.data.get("status") == "running"
            and e.data.get("reason") == "partial"
        ]
        self.assertTrue(running_partials, "no in-flight partial event")
        self.assertEqual(running_partials[0].data["query_id"], "jobA-p")
        self.assertEqual(running_partials[0].data["content"], "final answer")
        self.assertEqual(running_partials[0].data["worker_name"], "w-run-p")
        rec = self.registry.job("jobA-p")
        self.assertEqual(rec["status"], "completed")
        self.assertEqual(rec["result"]["content"], "final answer")


# ---------------------------------------------------------------------------
# 4. synchronous action='query' unchanged
# ---------------------------------------------------------------------------

class TestSyncActionQueryUnchanged(_RunSafetyPatches):
    """action='query' still goes through send_query and returns the envelope."""

    def test_sync_action_query_unchanged(self):
        fake = _inject_fake("w-sync", _FakeThread())
        self.addCleanup(_remove_fake, "w-sync")
        result = json.loads(Worker(
            action="query", worker_name="w-sync",
            worker_query="sync me", session_id="s1",
        ).execute())
        self.assertEqual(result["worker_name"], "w-sync")
        payload = json.loads(result["response"])
        self.assertEqual(payload["content"], "sync reply")
        self.assertIn("elapsed_seconds", result)
        self.assertEqual(fake.send_query_calls, ["sync me"])


# ---------------------------------------------------------------------------
# 5. correlation ids unique + matched
# ---------------------------------------------------------------------------

class TestCorrelationIdsUniqueAndMatched(_RunSafetyPatches):
    """Distinct job_ids per submit; each envelope stored under its own job."""

    def test_correlation_ids_unique_and_matched(self):
        fake = _inject_fake("w-cor", _FakeThread())
        self.addCleanup(_remove_fake, "w-cor")
        r1 = json.loads(Worker(
            action="submit_query", worker_name="w-cor",
            worker_query="one", session_id="s1",
        ).execute())
        r2 = json.loads(Worker(
            action="submit_query", worker_name="w-cor",
            worker_query="two", session_id="s1",
        ).execute())
        self.assertNotEqual(r1["job_id"], r2["job_id"])
        self.registry.complete(r1["job_id"], {"content": "c1", "query_id": r1["job_id"]})
        self.registry.complete(r2["job_id"], {"content": "c2", "query_id": r2["job_id"]})
        rec1 = self.registry.job(r1["job_id"])
        rec2 = self.registry.job(r2["job_id"])
        self.assertEqual(rec1["result"]["query_id"], r1["job_id"])
        self.assertEqual(rec1["result"]["content"], "c1")
        self.assertEqual(rec2["result"]["query_id"], r2["job_id"])
        self.assertEqual(rec2["result"]["content"], "c2")
        # the queued tuples carry the same ids the caller received
        items = []
        while not fake._input_queue.empty():
            items.append(fake._input_queue.get_nowait())
        self.assertEqual(sorted(i[0] for i in items),
                         sorted([r1["job_id"], r2["job_id"]]))


# ---------------------------------------------------------------------------
# 6. stale reply cannot satisfy the next sync caller
# ---------------------------------------------------------------------------

class TestStaleReplyCannotSatisfyNextCaller(_RunSafetyPatches):
    """A stale envelope on _output_queue is drained; sync callers only accept
    envelopes whose query_id matches their own."""

    def test_stale_reply_cannot_satisfy_next_caller(self):
        with tempfile.TemporaryDirectory() as tmp:
            thread = make_thread(tmp, name="w-stale", timeout=60, session_id="s1")
            # A late reply from a previous (async/abandoned) query.
            thread._output_queue.put(json.dumps({"content": "stale", "query_id": "stale1"}))

            def consumer():
                while True:
                    item = thread._input_queue.get()
                    if item is None:
                        break
                    qid, query, rq = item
                    thread._current_query_id = qid
                    thread._current_reply_queue = rq
                    thread._emit_reply(json.dumps(
                        {"content": "fresh:" + query, "query_id": qid}))

            t = threading.Thread(target=consumer, daemon=True)
            t.start()
            try:
                resp1 = json.loads(thread.send_query("q2", timeout=10.0))
                self.assertEqual(resp1["content"], "fresh:q2")
                self.assertNotEqual(resp1["query_id"], "stale1")
                # send_query drained the stale item before enqueueing.
                self.assertTrue(thread._output_queue.empty(),
                                "stale reply was not drained")
                resp2 = json.loads(thread.send_query("q3", timeout=10.0))
                self.assertEqual(resp2["content"], "fresh:q3")
            finally:
                thread._input_queue.put(None)
                t.join(timeout=5)


# ---------------------------------------------------------------------------
# 7. no cross-talk between workers
# ---------------------------------------------------------------------------

class TestNoCrossTalkBetweenWorkers(_RunSafetyPatches):
    """Jobs are namespaced per worker; events for one worker don't touch
    another worker's records (even in separate registries)."""

    def test_no_cross_talk_between_workers(self):
        reg_a = WorkerJobRegistry(event_bus=self.fake_bus)
        reg_a.ensure_subscribed()
        reg_b = WorkerJobRegistry(event_bus=self.fake_bus)
        reg_b.ensure_subscribed()
        reg_a.register("jA", "w1", "s1")
        reg_a.complete("jA", {"content": "A", "query_id": "jA"})
        reg_b.register("jB", "w2", "s1")
        reg_b.complete("jB", {"content": "B", "query_id": "jB"})
        # Worker A's job never appears in worker B's listing (and vice versa).
        self.assertEqual([j["job_id"] for j in reg_a.jobs(worker_name="w1")], ["jA"])
        self.assertEqual([j["job_id"] for j in reg_b.jobs(worker_name="w2")], ["jB"])
        self.assertEqual(reg_a.jobs(worker_name="w2"), [])
        self.assertEqual(reg_b.jobs(worker_name="w1"), [])
        # Completing A leaves B untouched.
        self.assertEqual(reg_b.job("jB")["result"]["content"], "B")
        # A WORKER_COMPLETED event tagged for A does not change B's record.
        self.fake_bus.publish(create_event(
            EventType.WORKER_COMPLETED,
            data={"query_id": "jA", "worker_name": "w1",
                  "session_id": "s1", "status": "completed"},
        ))
        self.assertEqual(reg_b.job("jB")["status"], "completed")
        self.assertEqual(reg_b.job("jB")["result"]["content"], "B")


# ---------------------------------------------------------------------------
# 8. timeout is non-blocking; WORKER_TIMEOUT event marks the job
# ---------------------------------------------------------------------------

class TestTimeoutNonBlocking(_RunSafetyPatches):
    """submit_query returns immediately even when the worker never replies;
    the WORKER_TIMEOUT event drives the job to 'timeout'."""

    def test_timeout_non_blocking(self):
        fake = _inject_fake("w-timeout", _FakeThread())
        self.addCleanup(_remove_fake, "w-timeout")
        # Worker never replies — a blocking submit would hang this test.
        result = json.loads(Worker(
            action="submit_query", worker_name="w-timeout",
            worker_query="slow", session_id="s1",
        ).execute())
        self.assertEqual(result["status"], "submitted")
        job_id = result["job_id"]
        self.assertEqual(self.registry.job(job_id)["status"], "submitted")
        # Simulate the worker.py timeout publish (now carrying query_id).
        self.fake_bus.publish(create_event(
            EventType.WORKER_TIMEOUT,
            data={"query_id": job_id, "worker_name": "w-timeout",
                  "session_id": "s1", "status": "busy",
                  "reason": "query_timeout"},
        ))
        rec = self.registry.job(job_id)
        self.assertEqual(rec["status"], "timeout")
        self.assertIsNotNone(rec["completed_at"])


# ---------------------------------------------------------------------------
# 9. observe without blocking (deep-copy, lock-protected reads)
# ---------------------------------------------------------------------------

class TestAgentObservesWithoutBlocking(_RunSafetyPatches):
    """job()/jobs() return deep copies; results are retrievable with no
    WorkerThread involved anywhere."""

    def test_agent_observes_without_blocking(self):
        reg = WorkerJobRegistry(event_bus=self.fake_bus)
        reg.ensure_subscribed()
        reg.register("jobObs", "w-obs", "s1")
        reg.complete("jobObs", {"content": "result!", "query_id": "jobObs"})
        # Reads are deep copies: mutating a returned record is harmless.
        rec = reg.job("jobObs")
        rec["result"]["content"] = "MUTATED"
        rec["status"] = "error"
        self.assertEqual(reg.job("jobObs")["result"]["content"], "result!")
        self.assertEqual(reg.job("jobObs")["status"], "completed")
        lst = reg.jobs(worker_name="w-obs")
        self.assertEqual(len(lst), 1)
        lst[0]["status"] = "bogus"
        self.assertEqual(reg.job("jobObs")["status"], "completed")
        # Filtering by worker/status works without any thread interaction.
        self.assertEqual(len(reg.jobs(worker_name="w-obs", status="completed")), 1)
        self.assertEqual(reg.jobs(worker_name="nobody"), [])
        # This whole test never touched _worker_registry or a WorkerThread.
