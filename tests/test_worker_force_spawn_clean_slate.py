"""
``force=True`` on a worker spawn must be a genuine clean slate.

Field report: a freshly spawned worker came up already consuming ~92k of its
own token budget because it had rehydrated its old transcript.  ``force``
replaced the previous incarnation's *process* but not its *conversation*:
``run()`` always called ``_load_context()``, and ``_action_spawn`` had a
comment stating outright that "context.json is never deleted on spawn".

Contract pinned here (``Worker._action_spawn``):

  * FORCED spawn  -> the previous ``workers/<name>/context.json`` is removed
    before the replacement ``WorkerThread`` is constructed, so ``run()`` loads
    nothing and starts from an empty conversation;
  * NON-forced spawn -> ``context.json`` is left untouched, so the loaded
    conversation is still restored (continuity is the default and must not be
    silently changed).

Harness mirrors ``tests/test_worker_force_spawn_resurrection.py``: the real
``WorkerRegistry`` singleton is snapshotted, ``resolve_workspace_id`` /
``_workspace_dir`` / ``WorkerThread`` are patched so ``_action_spawn`` runs,
but the workspace directory is a REAL temp dir so ``context.json`` is a real
file whose fate can be asserted.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

from agent.core.worker_context import WorkerContext
from tools.workspace.worker import Worker

SESSION = "sess-clean-slate"
WORKSPACE = "ws_clean_slate"
#: A message only the PREVIOUS incarnation's transcript carries.
STALE_MARKER = "STALE-TRANSCRIPT-FROM-THE-PREVIOUS-INCARNATION"


def _stale_transcript() -> list[dict]:
    """The message list a previous incarnation's transcript carries.

    Deliberately just the messages: how they are wrapped into ``context.json``
    is the serialiser's business (see ``_plant_transcript``), never this
    fixture's.
    """
    return [
        {"role": "system", "content": "worker system prompt"},
        {"role": "system", "content": f"Initial context: {STALE_MARKER}"},
        {"role": "assistant", "content": "old turn: " + STALE_MARKER},
    ]


class TestForceSpawnCleanSlate(unittest.TestCase):
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

        self._ws = Path(tempfile.mkdtemp(prefix="ws_force_clean_slate_"))
        self.addCleanup(shutil.rmtree, self._ws, True)
        (self._ws / "workers.json").write_text(
            json.dumps(
                [
                    {"name": "forced-worker", "status": "ready"},
                    {"name": "keeping-worker", "status": "ready"},
                ]
            ),
            encoding="utf-8",
        )

        self._p_resolve = mock.patch(
            "tools.workspace.worker.resolve_workspace_id",
            side_effect=lambda *a, **k: WORKSPACE,
        )
        self._p_ws_dir = mock.patch(
            "tools.workspace.worker._workspace_dir", return_value=self._ws
        )
        self._p_thread_cls = mock.patch("tools.workspace.worker.WorkerThread")
        self._p_resolve.start()
        self._p_ws_dir.start()
        self._mock_thread_cls = self._p_thread_cls.start()
        self.addCleanup(self._p_resolve.stop)
        self.addCleanup(self._p_ws_dir.stop)
        self.addCleanup(self._p_thread_cls.stop)
        self._created_threads: list[MagicMock] = []
        self._mock_thread_cls.side_effect = self._thread_factory

    def _thread_factory(self, *args, **kwargs) -> MagicMock:
        from tools.workspace.worker_registry import WorkerRegistry

        t = MagicMock()
        t.instance_id = kwargs.get("instance_id", 1)
        t.instance_label = WorkerRegistry.instance_label(
            kwargs.get("name", "worker"), t.instance_id
        )
        t.status = "ready"
        t._timeout_seconds = 30
        t._worker_ctx = None
        t._worker_dir = MagicMock()
        t._output_queue = MagicMock()
        t._last_elapsed = MagicMock(return_value=None)
        self._created_threads.append(t)
        return t

    def _restore_registry(self) -> None:
        with self._registry._registry_lock:
            self._registry._worker_registry.clear()
            self._registry._worker_registry.update(self._old_registry)
        with self._registry._bus_registry_lock:
            self._registry._worker_event_bus_registry.clear()
            self._registry._worker_event_bus_registry.update(self._old_buses)

    def _context_path(self, worker_name: str) -> Path:
        """The transcript path the REAL save/load path uses for this worker.

        Built by ``WorkerRegistry.worker_dir`` — the single production builder —
        because the layout is ``workers/<session_id>/<label>``: a hand-written
        ``workers/<name>/context.json`` is a directory no worker writes to, which
        made this fixture assert against an empty path.
        """
        from tools.workspace.worker_registry import WorkerRegistry

        return (
            WorkerRegistry.worker_dir(self._ws, SESSION, worker_name)
            / "context.json"
        )

    def _plant_transcript(self, worker_name: str) -> Path:
        """Leave behind a previous incarnation's transcript via the REAL save path.

        This does NOT hand-write ``context.json``.  It used to, and the
        hand-written shape had silently drifted from the schema: the serialiser
        stores the message list under ``conversation``
        (``WorkerContext.to_persistable_dict``), so a planted top-level
        ``user_history`` key was ignored by
        ``WorkerContext.from_persistable_dict`` -- the loader returned a
        context whose history was empty and the continuity assertion failed
        with ``the restored conversation lost its history: []``.  Driving the
        real ``_save_context()`` means this fixture can never drift from the
        schema ``_load_context()`` actually reads.
        """
        thread = self._real_thread(worker_name)
        thread._worker_ctx = WorkerContext(
            session_id=SESSION,
            workspace_id=WORKSPACE,
            worker_name=worker_name,
            user_history=_stale_transcript(),
        )
        thread._save_context()
        path = self._context_path(worker_name)
        self.assertTrue(
            path.exists(),
            "the real save path wrote no context.json, so this fixture cannot "
            "exercise continuity at all",
        )
        return path

    def _spawn(self, name: str, force: bool) -> dict:
        tool = Worker(
            action="spawn",
            worker_name=name,
            session_id=SESSION,
            workspace_path=str(self._ws),
            agent_config={"provider": "openai", "model": "gpt-4"},
            force=force,
            context={"query": "do the thing"},
        )
        return json.loads(tool.execute())

    def _real_thread(self, worker_name: str):
        """A real WorkerThread over this workspace dir (thread never started)."""
        from tools.workspace.worker_thread import WorkerThread

        return WorkerThread(
            name=worker_name,
            definition={},
            agent_config={"provider": "openai", "model": "gpt-4"},
            workspace_dir=self._ws,
            session_id=SESSION,
            workspace_id=WORKSPACE,
            instance_id=1,
        )

    def test_force_spawn_starts_from_an_empty_conversation(self):
        forced_ctx = self._plant_transcript("forced-worker")
        kept_ctx = self._plant_transcript("keeping-worker")

        # ── FORCED spawn: the previous conversation is cleared ────────────
        forced = self._spawn("forced-worker", force=True)
        self.assertTrue(forced.get("spawned"), f"forced spawn failed: {forced}")
        self.assertFalse(
            forced_ctx.exists(),
            "force=True restored the previous incarnation's conversation: "
            "workers/<name>/context.json was left on disk",
        )
        # ...so the replacement's run() finds nothing to load, i.e. it starts
        # from an empty conversation instead of the 92k-token transcript.
        self.assertIsNone(
            self._real_thread("forced-worker")._load_context(),
            "a forced spawn still rehydrates a conversation",
        )

        # ── NON-forced spawn: continuity is untouched ─────────────────────
        kept = self._spawn("keeping-worker", force=False)
        self.assertTrue(kept.get("spawned"), f"non-forced spawn failed: {kept}")
        self.assertTrue(
            kept_ctx.exists(),
            "a NON-forced spawn deleted the previous conversation: force must "
            "be the only thing that clears it",
        )
        restored = self._real_thread("keeping-worker")._load_context()
        self.assertIsNotNone(
            restored, "a non-forced spawn no longer restores its conversation"
        )
        self.assertTrue(
            any(STALE_MARKER in str(msg.get("content", ""))
                for msg in restored.user_history),
            f"the restored conversation lost its history: {restored.user_history}",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
