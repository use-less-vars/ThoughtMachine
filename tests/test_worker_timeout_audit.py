"""
PHASE 2b — worker-timeout audit tests (no real LLM / no Docker / no source changes).

Audits worker timeout & force-respawn behaviour in tools/workspace/worker.py:

  1. Soft-timeout envelope: when _run_tool_loop returns None and the agent's
     state is CRITICAL+timeout, run() emits an envelope with status="timeout"
     and telemetry.timeout_triggered=True.
  2. Spawn auto-query 600s cutoff: when the first auto-query response does not
     arrive within SPAWN_QUEUE_TIMEOUT, _action_spawn returns an error dict
     ("did not respond within 600s", "still alive") instead of raising.
  3. Query action timeout: send_query raising TimeoutError returns an error
     dict with "Worker did not respond in time" + "still alive" note (no raise).
  4. Force-respawn reload: a worker with persisted context.json gets the NEW
     _initial_context appended as a system message and the NEW query
     auto-queued — NOT the old attempt query. If the previous attempt never
     completed (last_query != last_completed_query) and belongs to a different
     query, it is pruned before the merge (F1) so the conversation does not
     double; completed attempts are preserved. Dedupe case: identical initial
     context is NOT re-appended but the query is still re-queued.
  5. Paused-resume drain ordering: the new spawn query is enqueued FIRST (F4);
     a stale queued query is re-put at the TAIL and runs after the new query.
  6. Force-stop persist without compaction: to_persistable_dict() writes the
     full pre-summary conversation (no pruning), compact_after_summary() prunes.
  7. Context doubling prevented on reload (F1): an incomplete attempt for a
     different query is truncated from the last "Initial context" boundary
     before the fresh initial-context message is appended -> conversation
     stays bounded across repeated force-respawns.
  8. Generation guard (F2): every WorkerThread carries a monotonic generation
     token persisted in context.json; a thread whose generation is OLDER than
     the one already on disk has its _save_context() skipped (stale writer
     rejected) so a still-alive thread from a previous spawn cannot overwrite
     the replacement's file. Files without a generation read as 0.
  9. Stop-path compaction (F3): force-spawn finally, _action_stop finally and
     shutdown_workers compact summarized history (compact_after_summary())
     before _save_context() so a stop right after SummarizeTool does not
     persist pre-summary + summary + post-summary messages (~2x).

Run:  python3 -m pytest tests/test_worker_timeout_audit.py -v
      (or) python3 -m unittest tests.test_worker_timeout_audit -v
"""

import json
import os
import queue
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.workspace.worker import (  # noqa: E402
    Worker,
    WorkerThread,
    SPAWN_QUEUE_TIMEOUT,
    _worker_registry,
    _registry_lock,
)
from agent.core.worker_context import WorkerContext  # noqa: E402

SYS_PROMPT = "You are a helpful worker assistant."


def make_fake_agent(time_state_value="LOW", restriction_reason=None):
    """Minimal fake agent whose .state mirrors what run() resets per query."""
    state = SimpleNamespace(
        current_turn=0,
        turn_state=None,
        last_turn_warning_state=None,
        restrictions_active=False,
        restrictions_pending=False,
        restriction_reason=restriction_reason,
        time_start=0.0,
        last_time_warning_state=None,
        time_state=SimpleNamespace(value=time_state_value),
    )
    return SimpleNamespace(state=state)


def make_thread(workspace_dir, name="w-test", timeout=60, session_id="s1"):
    return WorkerThread(
        name=name,
        definition={"system_prompt": SYS_PROMPT},
        agent_config={"model": "gpt-4o"},
        workspace_dir=Path(workspace_dir),
        session_id=session_id,
        timeout_seconds=timeout,
    )


def initial_ctx_msg(initial_context):
    return {
        "role": "system",
        "content": f"Initial context: {json.dumps(initial_context, default=str, ensure_ascii=True)}",
    }


class _FakeEventBus:
    """Callable stand-in for agent.events.EventBus."""

    def __init__(self, *a, **k):
        pass

    def publish(self, *a, **k):
        return None


class _RunSafetyPatches(unittest.TestCase):
    """Patches event plumbing so WorkerThread.run() can execute headlessly."""

    def setUp(self):
        patchers = [
            mock.patch("tools.workspace.worker.EventBus", new=_FakeEventBus),
            mock.patch("tools.workspace.worker.register_worker_event_bus",
                       new=lambda *a, **k: None),
            mock.patch("tools.workspace.worker.unregister_worker_event_bus",
                       new=lambda *a, **k: None),
            mock.patch("tools.workspace.worker.global_event_bus", new=None),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        super().setUp()


class TestSoftTimeoutEnvelope(_RunSafetyPatches):
    """Item (f) — envelope status='timeout' when soft timeout triggered."""

    def test_soft_timeout_envelope_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            thread = make_thread(tmp, name="w-env")
            thread._agent = make_fake_agent("CRITICAL", "timeout")
            # Pre-fill queue: one real query, then None to stop the loop.
            thread._input_queue.put("query-1")
            thread._input_queue.put(None)

            def fake_run_tool_loop(self, query):
                # run()'s per-query reset cleared restriction_reason to None;
                # simulate the agent's own timeout-restriction being applied
                # before the timeout-detection snippet runs (worker.py L1076-1091).
                self._agent.state.restriction_reason = "timeout"
                agent_state = self._agent.state
                if (
                    hasattr(agent_state, "time_state")
                    and hasattr(agent_state.time_state, "value")
                    and agent_state.time_state.value == "CRITICAL"
                    and getattr(agent_state, "restriction_reason", None) == "timeout"
                ):
                    self._timeout_triggered = True
                self._last_elapsed_val = 12.3
                self._final_token_usage = self.get_current_context_tokens()
                return None

            with mock.patch.object(thread, "_run_tool_loop", new=fake_run_tool_loop.__get__(thread, WorkerThread)):
                thread.run()

            raw = thread._output_queue.get_nowait()
            envelope = json.loads(raw)
            self.assertEqual(envelope["status"], "timeout")
            self.assertIs(envelope["telemetry"]["timeout_triggered"], True)
            self.assertEqual(envelope["content"], "Worker finished with no output.")
            self.assertIsNotNone(envelope["telemetry"]["elapsed_seconds"])
            self.assertEqual(envelope["telemetry"]["token_usage"], thread._final_token_usage)


class TestSpawnAutoQueryCutoff(unittest.TestCase):
    """Item (d) — spawn auto-query 600s cutoff returns error dict, no raise."""

    def test_spawn_auto_query_600s_cutoff(self):
        tool = Worker(action="spawn", worker_name="w-spawn",
                      context={"query": "auto task"}, session_id="s1")
        tool._find_worker = mock.Mock(return_value={"name": "w-spawn"})
        tool._build_agent_config = mock.Mock(return_value={})
        with tempfile.TemporaryDirectory() as tmp:
            tool._resolve_ws_dir = mock.Mock(return_value=Path(tmp))
            fake_thread = mock.MagicMock()
            fake_thread._output_queue.get.side_effect = queue.Empty
            fake_thread._last_elapsed.return_value = None
            try:
                with mock.patch("tools.workspace.worker.WorkerThread", return_value=fake_thread):
                    result = tool._action_spawn([], "ws-1")
            finally:
                with _registry_lock:
                    _worker_registry.pop(("s1", "w-spawn"), None)

            self.assertIn("did not respond within 600s", result["error"])
            self.assertIn("still alive", result["note"])
            self.assertIs(result["spawned"], True)
            self.assertEqual(result["worker_name"], "w-spawn")
            fake_thread._output_queue.get.assert_called_once_with(timeout=SPAWN_QUEUE_TIMEOUT)


class TestQueryActionTimeout(unittest.TestCase):
    """Item (e) — query action TimeoutError -> error dict, no raise."""

    def test_query_action_timeout_returns_error(self):
        tool = Worker(action="query", worker_name="w-query",
                      worker_query="hello", session_id="s1")
        fake = mock.MagicMock()
        fake.is_alive.return_value = True
        fake.last_heartbeat = None  # heartbeat falsy -> generic timeout branch
        fake.send_query.side_effect = TimeoutError(
            "Worker 'w-query' did not respond within 300.0s"
        )
        fake._last_elapsed.return_value = None
        with _registry_lock:
            _worker_registry[("s1", "w-query")] = fake
        try:
            result = tool._action_query([])
        finally:
            with _registry_lock:
                _worker_registry.pop(("s1", "w-query"), None)

        self.assertIn("error", result)
        self.assertIn("Worker 'w-query' did not respond within 300.0s", result["error"])
        self.assertIn("still alive", result["note"])
        self.assertEqual(result["worker_name"], "w-query")
        fake.send_query.assert_called_once_with("hello", timeout=300.0)


class TestForceRespawnReload(_RunSafetyPatches):
    """Item (a) — loaded-context merge: NEW query queued; an incomplete
    attempt for a different query is pruned (F1)."""

    def _write_context(self, thread, ctx, status="busy",
                       last_query=None, last_completed_query=None):
        thread._worker_dir.mkdir(parents=True, exist_ok=True)
        data = {
            **ctx.to_persistable_dict(),
            "status": status,
            "error": None,
            "last_heartbeat": None,
            "last_query": last_query,
            "last_completed_query": last_completed_query,
        }
        (thread._worker_dir / "context.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

    def _run_and_get_queue(self, thread):
        # Pre-fill None so the loop breaks right after the auto-queued query.
        thread._input_queue.put(None)
        thread.run()
        return list(thread._input_queue.queue)

    def test_force_respawn_queues_new_query_not_old(self):
        # Previous spawn attempt ("OLD QUERY") was cut short by a soft
        # timeout: last_query is set but last_completed_query is None.
        with tempfile.TemporaryDirectory() as tmp:
            thread = make_thread(tmp, name="w-r")
            ctx = WorkerContext(
                session_id="s1",
                worker_name="w-r",
                user_history=[
                    {"role": "system", "content": SYS_PROMPT},
                    initial_ctx_msg({"query": "OLD QUERY", "config": {"v": 1}}),
                    {"role": "user", "content": "old attempt query"},
                    {"role": "assistant", "content": "old attempt partial work"},
                ],
            )
            self._write_context(
                thread, ctx, status="busy",
                last_query="OLD QUERY", last_completed_query=None,
            )
            thread._initial_context = {"query": "NEW QUERY", "config": {"x": 1}}
            thread._agent = make_fake_agent()

            queued = self._run_and_get_queue(thread)

            # Auto-queued item is the NEW query, not the old attempt.
            # (The pre-filled None stop-sentinel is consumed by run()'s loop.)
            self.assertEqual(queued, ["NEW QUERY"])
            # F1: the previous attempt never completed (last_query !=
            # last_completed_query) and belongs to a different query, so it is
            # pruned BEFORE the new initial-context message is merged. The
            # loaded history collapses to just the system prompt + fresh IC.
            history = thread._worker_ctx.user_history
            self.assertEqual(
                [m["content"] for m in history],
                [
                    SYS_PROMPT,
                    initial_ctx_msg({"query": "NEW QUERY", "config": {"x": 1}})["content"],
                ],
            )
            self.assertIn("Initial context", history[-1]["content"])
            self.assertIn("NEW QUERY", history[-1]["content"])
            # Old attempt messages dropped — no context doubling.
            self.assertFalse(any(m["content"] == "old attempt query" for m in history))
            self.assertFalse(any(m["content"] == "old attempt partial work" for m in history))

    def test_force_respawn_dedupe_identical_initial_context(self):
        init1 = {"query": "OLD QUERY", "config": {"v": 1}}
        init1_msg = initial_ctx_msg(init1)
        with tempfile.TemporaryDirectory() as tmp:
            thread = make_thread(tmp, name="w-r2")
            ctx = WorkerContext(
                session_id="s1",
                worker_name="w-r2",
                user_history=[
                    {"role": "system", "content": SYS_PROMPT},
                    init1_msg,
                    {"role": "user", "content": "old attempt query"},
                    {"role": "assistant", "content": "old attempt partial work"},
                ],
            )
            self._write_context(
                thread, ctx, status="busy",
                last_query="OLD QUERY", last_completed_query=None,
            )
            # Same initial context as before -> dedupe must skip re-append...
            thread._initial_context = dict(init1)
            thread._agent = make_fake_agent()

            queued = self._run_and_get_queue(thread)

            # ...but the query is STILL re-queued (unconditional auto-queue).
            self.assertEqual(queued, ["OLD QUERY"])
            # F1: same query as the previous (incomplete) attempt -> no
            # truncation; the history is kept intact for the retry.
            self.assertEqual(len(thread._worker_ctx.user_history), 4)

    def test_two_respawns_bounded_growth(self):
        """F1 regression: repeated force-respawns never accumulate stale attempts."""
        with tempfile.TemporaryDirectory() as tmp:
            # Respawn #1: incomplete Q1 attempt -> run with Q2.
            thread1 = make_thread(tmp, name="w-r2a")
            ctx1 = WorkerContext(
                session_id="s1",
                worker_name="w-r2a",
                user_history=[
                    {"role": "system", "content": SYS_PROMPT},
                    initial_ctx_msg({"query": "Q1", "config": {"v": 1}}),
                    {"role": "user", "content": "attempt-1 query"},
                    {"role": "assistant", "content": "attempt-1 partial work"},
                ],
            )
            self._write_context(
                thread1, ctx1, status="busy",
                last_query="Q1", last_completed_query=None,
            )
            thread1._initial_context = {"query": "Q2", "config": {"v": 2}}
            thread1._agent = make_fake_agent()

            queued1 = self._run_and_get_queue(thread1)
            self.assertEqual(queued1, ["Q2"])
            self.assertEqual(
                [m["content"] for m in thread1._worker_ctx.user_history],
                [SYS_PROMPT, initial_ctx_msg({"query": "Q2", "config": {"v": 2}})["content"]],
            )

            # Respawn #2: Q2 itself was cut short (incomplete attempt persisted)
            # -> run with Q3. History must stay bounded.
            thread2 = make_thread(tmp, name="w-r2b")
            ctx2 = WorkerContext(
                session_id="s1",
                worker_name="w-r2b",
                user_history=[
                    {"role": "system", "content": SYS_PROMPT},
                    initial_ctx_msg({"query": "Q2", "config": {"v": 2}}),
                    {"role": "user", "content": "attempt-2 query"},
                    {"role": "assistant", "content": "attempt-2 partial work"},
                ],
            )
            self._write_context(
                thread2, ctx2, status="busy",
                last_query="Q2", last_completed_query=None,
            )
            thread2._initial_context = {"query": "Q3", "config": {"v": 3}}
            thread2._agent = make_fake_agent()

            queued2 = self._run_and_get_queue(thread2)
            self.assertEqual(queued2, ["Q3"])
            # Bounded: still just [SYS_PROMPT, IC(Q3)] — without F1 every
            # prior attempt would accumulate (~3x growth here).
            self.assertEqual(
                [m["content"] for m in thread2._worker_ctx.user_history],
                [SYS_PROMPT, initial_ctx_msg({"query": "Q3", "config": {"v": 3}})["content"]],
            )


class TestResumeDrainStaleQueryTail(unittest.TestCase):
    """Item (b) — drain re-puts stale query at TAIL; the new query runs FIRST."""

    def test_resume_drain_stale_query_tail(self):
        q = queue.Queue()
        q.put("stale-real-query")
        # Exact drain logic from worker.py _action_spawn resume path (F4):
        # collect the first real (non-None) stale item, then enqueue the NEW
        # query first, then re-queue the stale one at the tail.
        stale_query = None
        try:
            while True:
                item = q.get_nowait()
                if item is not None:
                    stale_query = item
                    break
        except queue.Empty:
            pass
        q.put("new-query")
        if stale_query is not None:
            q.put(stale_query)
        self.assertEqual(list(q.queue), ["new-query", "stale-real-query"])
        # FIFO ordering => the NEW query is consumed FIRST; the stale work is
        # processed after it (previously the stale query ran first, delaying
        # the spawn's fresh query behind unfinished work).


class TestForceStopPersistsUncompactedSummary(unittest.TestCase):
    """Item (h) — persist keeps pre-summary msgs; only compact_after_summary prunes."""

    def test_force_stop_persists_uncompacted_summary(self):
        ctx = WorkerContext(session_id="t6")
        summary_msg = {
            "role": "system",
            "content": "Summary of previous conversation: done.",
            "summary": True,
        }
        ctx.user_history = [
            {"role": "system", "content": SYS_PROMPT},
            {"role": "user", "content": "user turn 1"},
            {"role": "assistant", "content": "assistant turn 1"},
            summary_msg,
        ]
        persisted = ctx.to_persistable_dict()
        # Persistence does NOT prune pre-summary messages.
        self.assertEqual(len(persisted["conversation"]), 4)
        self.assertTrue(any("user turn 1" in m["content"] for m in persisted["conversation"]))

        # compact_after_summary() is the only pruning mechanism.
        self.assertTrue(ctx.compact_after_summary())
        self.assertEqual(
            [m["content"] for m in ctx.user_history],
            [SYS_PROMPT, "Summary of previous conversation: done."],
        )
        persisted2 = ctx.to_persistable_dict()
        self.assertEqual(len(persisted2["conversation"]), 2)


class TestContextDoublingPreventedByTruncation(_RunSafetyPatches):
    """Item (a) extension — F1 truncation prevents ~2x doubling on reload."""

    def test_context_doubling_prevented_by_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            thread = make_thread(tmp, name="w-dbl")
            ctx = WorkerContext(
                session_id="s1",
                worker_name="w-dbl",
                user_history=[
                    {"role": "system", "content": SYS_PROMPT},
                    initial_ctx_msg({"query": "OLD", "config": {"v": 1}}),
                    {"role": "user", "content": "attempt-1 query"},
                    {"role": "assistant", "content": "attempt-1 partial work"},
                ],
            )
            thread._worker_dir.mkdir(parents=True, exist_ok=True)
            (thread._worker_dir / "context.json").write_text(
                json.dumps({**ctx.to_persistable_dict(), "status": "busy",
                            "error": None, "last_heartbeat": None,
                            "last_query": "OLD",
                            "last_completed_query": None}),
                encoding="utf-8",
            )
            thread._initial_context = {"query": "NEW", "config": {"v": 2}}
            thread._agent = make_fake_agent()
            thread._input_queue.put(None)
            thread.run()

            history = thread._worker_ctx.user_history
            # F1: incomplete attempt for a different query ("OLD") pruned
            # before the fresh initial-context message was merged -> no
            # doubling (2 msgs, same as a fresh worker).
            self.assertEqual(
                [m["content"] for m in history],
                [
                    SYS_PROMPT,
                    initial_ctx_msg({"query": "NEW", "config": {"v": 2}})["content"],
                ],
            )
            self.assertEqual(list(thread._input_queue.queue), ["NEW"])


class TestGenerationGuard(_RunSafetyPatches):
    """F2 — generation token guards context.json against stale writers."""

    def test_save_context_writes_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            thread = make_thread(tmp, name="w-gen")
            thread._worker_ctx = WorkerContext(session_id="s1")
            thread._worker_ctx.user_history = [
                {"role": "system", "content": SYS_PROMPT},
                {"role": "user", "content": "hello"},
            ]
            thread._save_context()
            data = json.loads(
                (thread._worker_dir / "context.json").read_text(encoding="utf-8")
            )
            self.assertEqual(data.get("generation"), thread._generation)
            self.assertGreaterEqual(thread._generation, 1)

    def test_stale_thread_save_rejected(self):
        # A thread with an OLDER generation must not overwrite a context.json
        # that already carries a NEWER generation (the replacement's file).
        with tempfile.TemporaryDirectory() as tmp:
            owner = make_thread(tmp, name="w-gen2")
            owner._worker_ctx = WorkerContext(session_id="s1")
            owner._worker_ctx.user_history = [
                {"role": "system", "content": SYS_PROMPT},
                {"role": "user", "content": "owner content"},
            ]
            owner._save_context()
            original = (owner._worker_dir / "context.json").read_text(encoding="utf-8")

            stale = make_thread(tmp, name="w-gen2")
            stale._generation = owner._generation - 1  # simulate older thread
            stale._worker_ctx = WorkerContext(session_id="s1")
            stale._worker_ctx.user_history = [
                {"role": "system", "content": SYS_PROMPT},
                {"role": "user", "content": "stale overwrite attempt"},
            ]
            stale._save_context()
            after = (stale._worker_dir / "context.json").read_text(encoding="utf-8")
            self.assertEqual(after, original)
            self.assertIn("owner content", after)
            self.assertNotIn("stale overwrite attempt", after)

    def test_load_context_restores_generation(self):
        # A thread that loads a context.json written by a newer generation
        # keeps its own (higher) generation so its own saves are not rejected.
        with tempfile.TemporaryDirectory() as tmp:
            first = make_thread(tmp, name="w-gen3")
            first._worker_ctx = WorkerContext(session_id="s1")
            first._worker_ctx.user_history = [
                {"role": "system", "content": SYS_PROMPT},
                {"role": "user", "content": "hello"},
            ]
            first._generation = 7
            first._save_context()

            reloaded = make_thread(tmp, name="w-gen3")
            ctx = reloaded._load_context()
            self.assertIsNotNone(ctx)
            self.assertGreaterEqual(reloaded._generation, 7)

    def test_legacy_context_without_generation_writable(self):
        # Backward compat: context.json without a "generation" key reads as 0,
        # so a thread with generation >= 1 can still write it.
        with tempfile.TemporaryDirectory() as tmp:
            thread = make_thread(tmp, name="w-gen4")
            thread._worker_dir.mkdir(parents=True, exist_ok=True)
            legacy = {
                "session_id": "s1",
                "worker_name": "w-gen4",
                "turn_count": 1,
                "conversation": [{"role": "user", "content": "legacy"}],
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "status": "busy",
            }
            (thread._worker_dir / "context.json").write_text(
                json.dumps(legacy), encoding="utf-8"
            )
            thread._worker_ctx = WorkerContext(session_id="s1")
            thread._worker_ctx.user_history = [
                {"role": "system", "content": SYS_PROMPT},
                {"role": "user", "content": "new content"},
            ]
            thread._save_context()
            data = json.loads(
                (thread._worker_dir / "context.json").read_text(encoding="utf-8")
            )
            self.assertIn("new content",
                          [m["content"] for m in data["conversation"]])
            self.assertEqual(data.get("generation"), thread._generation)


class TestStopPathCompacts(_RunSafetyPatches):
    """F3 — stop paths compact summarized history before persisting."""

    def test_stop_path_save_compacts(self):
        # A force-stop right after SummarizeTool must NOT persist the
        # pre-summary messages (~2x); the real _action_stop path is exercised.
        with tempfile.TemporaryDirectory() as tmp:
            thread = make_thread(tmp, name="w-stop")
            ctx = WorkerContext(session_id="s1")
            ctx.user_history = [
                {"role": "system", "content": SYS_PROMPT},
                {"role": "user", "content": "pre-summary turn"},
                {"role": "assistant", "content": "pre-summary reply"},
                {"role": "system", "content": "Summary of previous conversation: done.",
                 "summary": True},
                {"role": "user", "content": "post-summary turn"},
            ]
            thread._worker_ctx = ctx
            thread._save_context()  # pre-stop state on disk (uncompacted)

            tool = Worker(action="stop", worker_name="w-stop", session_id="s1")
            with _registry_lock:
                _worker_registry[("s1", "w-stop")] = thread
            try:
                tool._action_stop([])
            finally:
                with _registry_lock:
                    _worker_registry.pop(("s1", "w-stop"), None)

            data = json.loads(
                (thread._worker_dir / "context.json").read_text(encoding="utf-8")
            )
            persisted = data["conversation"]
            # Pruned: leading system prompt + last summary + post-summary msgs.
            self.assertEqual(
                [m["content"] for m in persisted],
                [SYS_PROMPT, "Summary of previous conversation: done.",
                 "post-summary turn"],
            )


if __name__ == "__main__":
    unittest.main()
