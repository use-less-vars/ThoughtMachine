"""D2: a spawn briefing is delivered on EVERY routing, not just the fresh one.

A ``spawn`` that collides with a live (or paused) instance is routed to that
existing instance instead of creating a duplicate.  Historically that routing
extracted only ``context['query']`` and threw the rest of the supplied context
away, so a caller who briefed a worker that happened to be already running got
``spawned: false`` and no briefing at all -- silently.

The contract these tests pin:

  1. the reuse routing delivers the briefing with the query (same
     ``initial_context_message`` channel the fresh path uses);
  2. a briefing WITHOUT a query is still delivered (it no longer falls into the
     legacy "nothing to deliver" early return);
  3. a spawn with no context at all keeps the legacy no-dispatch envelope (the
     model's default ``style`` must never manufacture a phantom briefing);
  4. a briefing that cannot be rendered makes the dispatch REFUSE (naming the
     keys) instead of dropping it;
  5. when the transport gap means nothing was delivered, the envelope carries
     the ``context_ignored`` signal naming the undelivered keys;
  6. the FRESH path's payload stays byte-identical.

Harness mirrors tests/test_worker_operation_semantics.py (real WorkerRegistry
singleton, ``resolve_workspace_id`` / ``_workspace_dir`` / ``WorkerThread``
patched, ``deliver_query_to_worker`` patched so the dispatch text is observable).
"""

from __future__ import annotations

import json
import queue
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

from tools.workspace.worker import Worker
from tools.workspace.worker_thread import (
    INITIAL_CONTEXT_PREFIX,
    initial_context_message,
)

SESSION = "sess-spawn-briefing"
WORKER_NAMES = [f"w{i}" for i in range(1, 6)]
DELIVER = "tools.workspace.worker_thread.deliver_query_to_worker"
IGNORED_KEY = "**context_ignored**"


def _parse_result(result: str) -> dict:
    return json.loads(result)


def _live_thread(status: str = "ready") -> MagicMock:
    t = MagicMock()
    t.is_alive.return_value = True
    t.status = status
    t._manual_only_pause = False  # bare MagicMock auto-creates truthy attrs
    t._timeout_seconds = 30
    t._worker_ctx = None  # check path reads _worker_ctx.user_history
    t._last_elapsed.return_value = 0.5  # success envelope rounds it
    return t


class TestSpawnBriefingDelivery(unittest.TestCase):
    """A supplied briefing survives every spawn routing."""

    def setUp(self) -> None:
        from tools.workspace.worker_registry import WorkerRegistry

        self._registry = WorkerRegistry.get_instance()
        with self._registry._registry_lock:
            self._old_registry = dict(self._registry._worker_registry)
            self._registry._worker_registry.clear()
        with self._registry._bus_registry_lock:
            self._old_buses = dict(self._registry._worker_event_bus_registry)
            self._registry._worker_event_bus_registry.clear()
        self.addCleanup(self._restore_registry)

        self._p_resolve = mock.patch(
            "tools.workspace.worker.resolve_workspace_id", return_value="ws_brief_test"
        )
        self._p_ws_dir = mock.patch("tools.workspace.worker._workspace_dir")
        self._p_thread_cls = mock.patch("tools.workspace.worker.WorkerThread")
        self._mock_ws_dir = self._p_ws_dir.start()
        self._mock_thread_cls = self._p_thread_cls.start()
        self._p_resolve.start()
        self.addCleanup(self._p_resolve.stop)
        self.addCleanup(self._p_ws_dir.stop)
        self.addCleanup(self._p_thread_cls.stop)

        self._created: list[MagicMock] = []
        self._mock_thread_cls.side_effect = self._thread_factory

        self._setup_workspace()

    # ── harness ────────────────────────────────────────────────────────

    def _thread_factory(self, *args, **kwargs) -> MagicMock:
        from tools.workspace.worker_registry import WorkerRegistry

        t = MagicMock()
        t.instance_id = kwargs.get("instance_id", 1)
        t.instance_label = WorkerRegistry.instance_label(
            kwargs.get("name", "worker"), t.instance_id
        )
        t.status = "ready"
        t._timeout_seconds = 30
        self._created.append(t)
        return t

    def _restore_registry(self) -> None:
        with self._registry._registry_lock:
            self._registry._worker_registry.clear()
            self._registry._worker_registry.update(self._old_registry)
        with self._registry._bus_registry_lock:
            self._registry._worker_event_bus_registry.clear()
            self._registry._worker_event_bus_registry.update(self._old_buses)

    def _setup_workspace(self) -> None:
        mock_dir = MagicMock()
        mock_file = MagicMock()
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps(
            [{"name": n, "status": "ready"} for n in WORKER_NAMES]
        )
        mock_dir.__truediv__.return_value = mock_file
        self._mock_ws_dir.return_value = mock_dir

    def _seed_fake(
        self, session: str, name: str, iid: int, status: str = "ready"
    ) -> MagicMock:
        from tools.workspace.worker_registry import WorkerRegistry

        t = _live_thread(status=status)
        t.instance_id = iid
        t.instance_label = WorkerRegistry.instance_label(name, iid)
        with self._registry._registry_lock:
            self._registry._worker_registry[(session, name, iid)] = t
        return t

    def _make_worker(
        self,
        name: str,
        session: str = SESSION,
        context=None,
        style=None,
        purpose=None,
        meta=None,
        workspace_path: str = "/tmp/test_ws",
        agent_config=None,
    ):
        kwargs = dict(
            action="spawn",
            worker_name=name,
            session_id=session,
            workspace_path=workspace_path,
            agent_config=(
                agent_config
                if agent_config is not None
                else {"provider": "openai", "model": "gpt-4"}
            ),
        )
        if context is not None:
            kwargs["context"] = context
        if style is not None:
            kwargs["style"] = style
        if purpose is not None:
            kwargs["purpose"] = purpose
        if meta is not None:
            kwargs["meta"] = meta
        return Worker(**kwargs)

    def _spawn(self, name: str, **kwargs) -> dict:
        return _parse_result(self._make_worker(name, **kwargs).execute())

    def _seed_workspace_kb(self, workspace_path: str) -> tuple:
        """Write a REAL ``<ws>/.thoughtmachine/knowledge/project`` fixture.

        Returns ``(line_number, stripped_line)`` of the only line carrying the
        distinctive identifier ``hollow_promise``.
        """
        kb_dir = (
            Path(workspace_path) / ".thoughtmachine" / "knowledge" / "project"
        )
        kb_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            "unrelated filler about containers and budgets",
            "- the hollow_promise flag is checked before dispatch",
        ]
        (kb_dir / "lessons_learned.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        return 2, lines[1].strip()

    def _real_workspace(self) -> str:
        import shutil
        import tempfile

        ws = tempfile.mkdtemp(prefix="kb-recall-ws-")
        self.addCleanup(shutil.rmtree, ws, True)
        return ws

    def _spawn_and_capture(self, name: str, **kwargs):
        """Spawn against a seeded live instance and return (envelope, calls)."""
        with mock.patch(DELIVER, return_value='{"response": "ok"}') as deliver:
            result = self._spawn(name, **kwargs)
        return result, deliver

    # ── 1. briefing + query reaches the existing instance ──────────────

    def test_reuse_delivers_briefing_with_query(self):
        self._seed_fake(SESSION, "w1", 1)
        result, deliver = self._spawn_and_capture(
            "w1", context={"query": "review src/", "briefing": "B2"}
        )

        self.assertFalse(result.get("spawned"), f"reuse envelope: {result}")
        self.assertNotIn("error", result, f"reuse envelope: {result}")
        deliver.assert_called_once()
        text = deliver.call_args[0][1]
        self.assertIn(INITIAL_CONTEXT_PREFIX, text, f"no briefing block in {text!r}")
        self.assertIn("B2", text, f"briefing value dropped from {text!r}")
        self.assertIn("review src/", text, f"query dropped from {text!r}")

    # ── 2. a briefing without a query is delivered too ─────────────────

    def test_reuse_delivers_briefing_without_query(self):
        self._seed_fake(SESSION, "w1", 1)
        result, deliver = self._spawn_and_capture("w1", context={"briefing": "B3"})

        self.assertNotIn("error", result, f"reuse envelope: {result}")
        deliver.assert_called_once()
        text = deliver.call_args[0][1]
        self.assertIn(INITIAL_CONTEXT_PREFIX, text)
        self.assertIn("B3", text, f"briefing dropped from {text!r}")

    # ── 3. no context at all keeps the legacy no-dispatch envelope ─────

    def test_reuse_without_context_keeps_legacy_envelope(self):
        self._seed_fake(SESSION, "w1", 1)
        result, deliver = self._spawn_and_capture("w1")

        deliver.assert_not_called()
        self.assertFalse(result.get("spawned"))
        self.assertIn("reusing", result.get("message", "").lower())
        self.assertNotIn(IGNORED_KEY, result)

    def test_reuse_default_style_is_not_a_phantom_briefing(self):
        """The model default style must not invent a briefing block."""
        self._seed_fake(SESSION, "w1", 1)
        result, deliver = self._spawn_and_capture("w1", context={"query": "just q"})

        deliver.assert_called_once()
        self.assertEqual(deliver.call_args[0][1], "just q", result)

    def test_filled_recall_slot_is_not_a_phantom_briefing(self):
        """A NON-EMPTY recall slot must not change routing either: it is
        machine-added, so a filled slot is still not a supplied briefing."""
        self._seed_fake(SESSION, "w1", 1)
        fake_payload = {
            "kb_recall": [{"domain": "lessons_learned", "line": 730, "text": "x"}]
        }
        with mock.patch.object(
            Worker, "_spawn_context_payload", return_value=fake_payload
        ):
            result, deliver = self._spawn_and_capture("w1")

        deliver.assert_not_called()
        self.assertFalse(result.get("spawned"))
        self.assertIn("reusing", result.get("message", "").lower())
        self.assertNotIn(IGNORED_KEY, result)

    def test_reuse_supplied_style_is_delivered(self):
        self._seed_fake(SESSION, "w1", 1)
        result, deliver = self._spawn_and_capture(
            "w1", context={"query": "q"}, style="exploratory"
        )

        deliver.assert_called_once()
        text = deliver.call_args[0][1]
        self.assertIn(INITIAL_CONTEXT_PREFIX, text)
        self.assertIn("exploratory", text, f"supplied style dropped from {text!r}")

    # ── 4. an unrenderable briefing REFUSES instead of dropping ────────

    def test_unrenderable_briefing_refuses_dispatch(self):
        self._seed_fake(SESSION, "w1", 1)
        circular: dict = {"query": "review src/"}
        circular["briefing"] = circular  # json.dumps -> ValueError

        result, deliver = self._spawn_and_capture("w1", context=circular)

        deliver.assert_not_called()
        self.assertIn("error", result, f"envelope: {result}")
        self.assertIn("briefing", result["error"], f"reason unnamed: {result}")
        self.assertIn(IGNORED_KEY, result, f"no context_ignored: {result}")
        self.assertIn("briefing", result[IGNORED_KEY])

    # ── 5. a transport gap names the undelivered keys ──────────────────

    def test_transport_gap_reports_context_ignored(self):
        self._seed_fake(SESSION, "w1", 1)
        with mock.patch(DELIVER, side_effect=RuntimeError("no transport")):
            result = self._spawn(
                "w1", context={"query": "review src/", "briefing": "B4"}
            )

        self.assertIn("error", result, f"envelope: {result}")
        self.assertIn(IGNORED_KEY, result, f"no context_ignored: {result}")
        self.assertIn("briefing", result[IGNORED_KEY])

    # ── 6. the paused-resume routing carries the same briefing ─────────

    def test_paused_resume_delivers_briefing(self):
        fake = _live_thread(status="paused")
        fake.instance_id = 1
        fake.instance_label = "w2"
        fake._input_queue = queue.Queue()
        fake._output_queue = MagicMock()
        fake._output_queue.get.return_value = '{"response": "hi"}'
        fake._last_elapsed.return_value = None
        with self._registry._registry_lock:
            self._registry._worker_registry[(SESSION, "w2", 1)] = fake

        result = self._spawn(
            "w2", context={"query": "review src/", "briefing": "B5"}
        )
        fake.resume.assert_called_once()
        self.assertNotIn("error", result, f"resume envelope: {result}")

        items = []
        while True:
            try:
                items.append(fake._input_queue.get_nowait())
            except queue.Empty:
                break
        text = "\n".join(str(i) for i in items)
        self.assertIn(INITIAL_CONTEXT_PREFIX, text, f"no briefing block in {text!r}")
        self.assertIn("B5", text, f"briefing dropped from {text!r}")
        self.assertIn("review src/", text, f"query dropped from {text!r}")

    # ── 7. the FRESH path payload is byte-identical ────────────────────

    def test_fresh_spawn_payload_unchanged(self):
        result = self._spawn(
            "w1",
            context={"query": "review src/", "briefing": "B6", "config": {"k": 1}},
            purpose="coding",
            style="exploratory",
            meta={"m": 2},
        )
        self.assertTrue(result.get("spawned"), f"fresh spawn: {result}")
        self.assertTrue(self._created, "no WorkerThread was constructed")

        expected = {
            "query": "review src/",
            "briefing": "B6",
            "config": {"k": 1},
            "purpose": "coding",
            "style": "exploratory",
            "meta": {"m": 2},
            "kb_recall": [],
        }
        got = self._created[-1]._initial_context
        self.assertEqual(list(got), list(expected))  # ORDER matters (payload bytes)
        self.assertEqual(got, expected)
        self.assertEqual(
            initial_context_message(got)["content"],
            initial_context_message(expected)["content"],
        )

    # ── 8. the reserved KB-recall slot is present, last, and empty ────────

    def test_fresh_spawn_payload_has_empty_kb_recall_slot_last(self):
        """C1: the reserved recall slot exists, is LAST, and injects nothing."""
        result = self._spawn("w1", context={"query": "review src/"})
        self.assertTrue(result.get("spawned"), f"fresh spawn: {result}")
        self.assertTrue(self._created, "no WorkerThread was constructed")

        payload = self._created[-1]._initial_context
        self.assertIn("kb_recall", payload)
        self.assertEqual(payload["kb_recall"], [])
        self.assertEqual(list(payload)[-1], "kb_recall")  # reserved slot is LAST

    def test_kb_recall_stays_empty_for_a_query_naming_a_kb_identifier(self):
        """C2: a query naming a REAL lessons_learned.md identifier still
        injects nothing -- the slot is reserved, not wired."""
        import re
        from pathlib import Path

        kb_file = (
            Path(__file__).resolve().parents[1]
            / ".thoughtmachine/knowledge/project/lessons_learned.md"
        )
        if not kb_file.is_file():
            self.skipTest(f"KB file not present: {kb_file}")
        kb_text = kb_file.read_text(encoding="utf-8")
        identifiers = re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", kb_text)
        self.assertTrue(identifiers, "no snake_case identifier found in the KB")
        identifier = identifiers[0]
        self.assertIn(identifier, kb_text)  # it really came from the KB

        result = self._spawn("w1", context={"query": f"explain {identifier}"})
        self.assertTrue(result.get("spawned"), f"fresh spawn: {result}")
        payload = self._created[-1]._initial_context
        self.assertIn(identifier, payload["query"])
        self.assertEqual(payload["kb_recall"], [])
        self.assertEqual(list(payload)[-1], "kb_recall")

    # ── 9. the recall slot is WIRED (knob read, not phoned in) ─────────

    def test_filled_recall_slot_carries_a_real_kb_pointer(self):
        """D1: a real workspace KB under ``<ws>/.thoughtmachine/knowledge``
        produces a real pointer in the slot -- the wiring is live, not a
        silent ``[]`` fallback."""
        ws = self._real_workspace()
        line_no, line_text = self._seed_workspace_kb(ws)

        worker = self._make_worker(
            "w1", context={"query": "explain hollow_promise"}, workspace_path=ws
        )
        self.assertIsNotNone(
            getattr(worker, "agent_config", None),
            "Worker has no agent_config attribute: the kb_injection_* knobs "
            "cannot be read from it",
        )
        result = _parse_result(worker.execute())
        self.assertTrue(result.get("spawned"), f"fresh spawn: {result}")

        payload = self._created[-1]._initial_context
        recall = payload["kb_recall"]
        self.assertEqual(
            recall,
            [{"domain": "lessons_learned", "line": line_no, "text": line_text}],
        )
        self.assertEqual(list(payload)[-1], "kb_recall")

    def test_zero_max_hits_disables_injection(self):
        """D2: ``kb_injection_max_hits=0`` on the Worker's own config switches
        the slot back off -- proof the knob is READ, not ignored."""
        ws = self._real_workspace()
        self._seed_workspace_kb(ws)
        context = {"query": "explain hollow_promise"}

        on = self._make_worker("w1", context=context, workspace_path=ws)
        self.assertIsNotNone(
            getattr(on, "agent_config", None),
            "Worker has no agent_config attribute: the kb_injection_* knobs "
            "cannot be read from it",
        )
        self.assertTrue(_parse_result(on.execute()).get("spawned"))
        self.assertEqual(len(self._created[-1]._initial_context["kb_recall"]), 1)

        off = self._make_worker(
            "w1",
            context=context,
            workspace_path=ws,
            agent_config={
                "provider": "openai",
                "model": "gpt-4",
                "kb_injection_max_hits": 0,
            },
        )
        self.assertTrue(_parse_result(off.execute()).get("spawned"))
        self.assertEqual(self._created[-1]._initial_context["kb_recall"], [])


if __name__ == "__main__":
    unittest.main()
