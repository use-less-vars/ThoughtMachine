"""A spawned worker must inherit the PARENT session's workspace, not a stale
``SessionRegistry`` entry (workspace cross-wiring regression).

Field report: in workspace ``A`` (parent session gated with full filesystem
permission) a spawned worker came up with every category banned
(``filesystem:banned``).  The worker had been resolved onto a *different*,
deleted workspace id.

Two independent resolution paths could produce that, and both are pinned here:

  1. ``WorkerTool.execute`` used to ask ``SessionRegistry`` for the session's
     ``workspace_id`` FIRST and only fall back to the injected
     ``workspace_path``.  SessionRegistry is rebuilt from disk by scanning the
     legacy ``sessions/`` dir *and* every ``workspaces/*/sessions/`` dir, and a
     session file found under two workspace dirs is read "first found wins" --
     so a moved/duplicated workspace (or a rebind that left the old record
     behind) resolves the session to the wrong, possibly deleted, workspace.

  2. ``WorkerTool._build_agent_config`` passed the *registry's* workspace root
     to the worker even when it disagreed with this tool's own injected
     ``workspace_path``.  The worker's ToolExecutor gates every call against
     that path, so a foreign root re-creates the same failure inside the
     worker.

The parent's injected ``workspace_path`` is authoritative in both places: it
is exactly the value the parent's own permission gate resolved its workspace
id from (``agent/core/tool_executor.py`` injects
``tool_args['workspace_path'] = config.workspace_path`` and gates on
``resolve_workspace_id(config.workspace_path)``).

Consumers under test (real symbols, not re-implementations)::

    tools.workspace.worker_thread.WorkerTool._resolve_worker_workspace_id
    tools.workspace.worker_thread.WorkerTool._build_agent_config
    tools.workspace.worker.Worker(...).execute()

Run:  python -m pytest tests/test_worker_workspace_resolution.py -q
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import unittest
from unittest import mock
from unittest.mock import MagicMock

from tools.workspace.worker import Worker

# The parent session really is running in this workspace ...
PARENT_WORKSPACE_PATH = os.path.join(
    os.path.expanduser("~"), "PycharmProjects", "ThoughtMachine-dev"
)
PARENT_WORKSPACE_ID = "fierce-owl-43"
# ... while the (stale) registry entry still says the deleted one.
STALE_WORKSPACE_ID = "gentle-wolf-72"

SESSION = "sess-crosswire"
WORKER_NAME = "default"
LOGGER = "tools.workspace.worker"


def _resolve_parent_only(path):
    """Stand-in for resolve_workspace_id: only the parent path is known."""
    return PARENT_WORKSPACE_ID if path == PARENT_WORKSPACE_PATH else None


@contextlib.contextmanager
def _captured_warnings():
    """Collect log records emitted on the worker logger.

    ``assertLogs`` is deliberately NOT used: it fails the test when nothing is
    logged, which forces the masking ``assertRaises(AssertionError)`` idiom to
    assert a *negative*.  A raw handler lets a negative assertion be written
    normally, and a positive one cannot be satisfied by an unrelated
    AssertionError raised inside the block.
    """
    records = []

    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[assignment]

    target = logging.getLogger(LOGGER)
    previous_level = target.level
    target.addHandler(handler)
    target.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        target.removeHandler(handler)
        target.setLevel(previous_level)


def _warning_text(records) -> str:
    return "\n".join(record.getMessage() for record in records)


class _RegistryStub:
    """Patches SessionRegistry.get_default() to report a fixed session."""

    def __init__(self, workspace_id):
        self._workspace_id = workspace_id
        self._patcher = mock.patch(
            "session.session_registry.SessionRegistry.get_default"
        )

    def __enter__(self):
        self._registry = self._patcher.start()
        self._registry.return_value.get.return_value = {
            "session_id": SESSION,
            "workspace_id": self._workspace_id,
        }
        return self._registry

    def __exit__(self, *exc_info):
        self._patcher.stop()
        return False


def _make_tool(**overrides):
    kwargs = dict(
        action="spawn",
        worker_name=WORKER_NAME,
        session_id=SESSION,
        workspace_path=PARENT_WORKSPACE_PATH,
        agent_config={"provider": "openai", "model": "gpt-4"},
    )
    kwargs.update(overrides)
    return Worker(**kwargs)


# ============================================================================
# 1. Id resolution: parent path beats the registry
# ============================================================================


class TestResolveWorkerWorkspaceId(unittest.TestCase):
    """``_resolve_worker_workspace_id`` is the single source of the worker id."""

    def test_parent_path_wins_over_stale_registry(self):
        """WHY: the ID the worker inherits decides every permission gate it
        will ever hit.  A stale registry entry must not be able to point the
        worker at another (dead) workspace.  Dropping the precedence flip ->
        RED (the registry id comes back)."""
        tool = _make_tool()
        with _RegistryStub(STALE_WORKSPACE_ID), mock.patch(
            "tools.workspace.worker.resolve_workspace_id",
            side_effect=_resolve_parent_only,
        ):
            with _captured_warnings() as records:
                ws_id = tool._resolve_worker_workspace_id()

        self.assertEqual(ws_id, PARENT_WORKSPACE_ID)

        # The divergence is reported, and it names BOTH ids: an operator
        # reading the log learns which record is stale.
        joined = _warning_text(records)
        self.assertIn("cross-wiring", joined)
        self.assertIn(STALE_WORKSPACE_ID, joined)
        self.assertIn(PARENT_WORKSPACE_ID, joined)
        self.assertIn(SESSION, joined)

    def test_registry_still_used_without_a_workspace_path(self):
        """Registry-primary behaviour must survive for callers that pass only
        a session_id (the deprecated path may legitimately be absent)."""
        tool = _make_tool(workspace_path=None)
        with _RegistryStub(STALE_WORKSPACE_ID), mock.patch(
            "tools.workspace.worker.resolve_workspace_id",
            side_effect=AssertionError(
                "resolve_workspace_id must not be called without a workspace_path"
            ),
        ):
            self.assertEqual(
                tool._resolve_worker_workspace_id(), STALE_WORKSPACE_ID
            )

    def test_path_used_when_registry_has_no_entry(self):
        """An unknown session (or an unreadable registry) must not veto the
        path-derived id."""
        tool = _make_tool()
        with _RegistryStub(None), mock.patch(
            "tools.workspace.worker.resolve_workspace_id",
            side_effect=_resolve_parent_only,
        ):
            self.assertEqual(
                tool._resolve_worker_workspace_id(), PARENT_WORKSPACE_ID
            )

    def test_no_warning_when_both_sources_agree(self):
        """A healthy session must not log a cross-wiring warning."""
        tool = _make_tool()
        with _RegistryStub(PARENT_WORKSPACE_ID), mock.patch(
            "tools.workspace.worker.resolve_workspace_id",
            side_effect=_resolve_parent_only,
        ):
            with _captured_warnings() as records:
                ws_id = tool._resolve_worker_workspace_id()

        self.assertEqual(ws_id, PARENT_WORKSPACE_ID)
        self.assertNotIn("cross-wiring", _warning_text(records))

    def test_unresolvable_path_falls_back_to_registry(self):
        """A path that maps to no workspace at all (bare test doubles, an
        unregistered checkout) leaves the registry answer in charge rather
        than dropping the id entirely."""
        tool = _make_tool(workspace_path="/tmp/not-a-registered-workspace")
        with _RegistryStub(STALE_WORKSPACE_ID), mock.patch(
            "tools.workspace.worker.resolve_workspace_id", return_value=None
        ):
            self.assertEqual(
                tool._resolve_worker_workspace_id(), STALE_WORKSPACE_ID
            )


# ============================================================================
# 2. execute() hands that id to the spawn path
# ============================================================================


class TestExecutePassesParentWorkspaceId(unittest.TestCase):
    """The resolution above must actually reach ``_action_spawn``."""

    def test_spawn_is_dispatched_with_the_parent_workspace_id(self):
        """WHY: pinning the helper alone would not prove the wiring.  Before
        the fix execute() dispatched the REGISTRY's id here."""
        tool = _make_tool()
        definition = {"name": WORKER_NAME, "status": "ready"}

        tool._load_workers = mock.Mock(return_value=[definition])
        tool._action_spawn = mock.Mock(return_value={"spawned": True})

        with _RegistryStub(STALE_WORKSPACE_ID), mock.patch(
            "tools.workspace.worker.resolve_workspace_id",
            side_effect=_resolve_parent_only,
        ):
            payload = json.loads(tool.execute())

        self.assertEqual(payload, {"spawned": True})
        # workers.json lookup used the parent id ...
        tool._load_workers.assert_called_once_with(PARENT_WORKSPACE_ID)
        # ... and so did the spawn dispatch.
        args, kwargs = tool._action_spawn.call_args
        self.assertEqual(args, ([definition], PARENT_WORKSPACE_ID), kwargs)


# ============================================================================
# 3. The worker's own AgentConfig carries the parent's workspace path
# ============================================================================


class TestBuildAgentConfigWorkspacePath(unittest.TestCase):
    """The worker's ToolExecutor gates against this path, so it must be the
    parent's, not the registry's."""

    def test_registry_root_yields_to_parent_path(self):
        """WHY: this is the second, independent cross-wiring path -- even with
        the right workspace id, a foreign root here re-gates the worker onto
        the wrong workspace.  Reverting to the unconditional registry value ->
        RED."""
        tool = _make_tool()
        stale_root = "/home/jojo/PycharmProjects/ThoughtMachine-old"
        tool._resolve_registry_workspace = mock.Mock(return_value=stale_root)

        with _captured_warnings() as records:
            cfg = tool._build_agent_config()

        self.assertEqual(cfg["workspace_path"], PARENT_WORKSPACE_PATH)
        joined = _warning_text(records)
        self.assertIn(stale_root, joined)
        self.assertIn(PARENT_WORKSPACE_PATH, joined)

    def test_registry_root_used_when_parent_path_absent(self):
        """No injected path (test doubles, direct callers) keeps the previous
        registry-first behaviour."""
        tool = _make_tool(workspace_path=None)
        tool._resolve_registry_workspace = mock.Mock(return_value="/some/root")

        with _captured_warnings() as records:
            cfg = tool._build_agent_config()

        self.assertEqual(cfg["workspace_path"], "/some/root")
        self.assertNotIn("differs from this session", _warning_text(records))

    def test_agreeing_sources_do_not_warn(self):
        """Same root spelled differently is still the same workspace."""
        tool = _make_tool()
        same_root = PARENT_WORKSPACE_PATH + os.sep
        tool._resolve_registry_workspace = mock.Mock(return_value=same_root)

        with _captured_warnings() as records:
            cfg = tool._build_agent_config()

        self.assertEqual(cfg["workspace_path"], same_root)
        self.assertNotIn("differs from this session", _warning_text(records))


# ============================================================================
# 4. End-to-end: the WorkerThread really is constructed with the parent's ids
# ============================================================================


class TestSpawnedWorkerThreadIdentity(unittest.TestCase):
    """Drives the real ``_action_spawn`` so the final kwargs are observed."""

    def setUp(self) -> None:
        from tools.workspace.worker_registry import WorkerRegistry

        self._registry = WorkerRegistry.get_instance()
        with self._registry._registry_lock:
            self._old_registry = dict(self._registry._worker_registry)
            self._registry._worker_registry.clear()
            self._old_epochs = dict(self._registry._worker_epochs)
            self._registry._worker_epochs.clear()
        with self._registry._bus_registry_lock:
            self._old_buses = dict(self._registry._worker_event_bus_registry)
            self._registry._worker_event_bus_registry.clear()
            self._old_bus_epochs = dict(self._registry._bus_epochs)
            self._registry._bus_epochs.clear()
        self.addCleanup(self._restore_registry)

        # The stale, deleted workspace is what SessionRegistry still claims.
        self._registry_stub = _RegistryStub(STALE_WORKSPACE_ID)
        self._registry_stub.__enter__()
        self.addCleanup(self._registry_stub.__exit__, None, None, None)

    def _restore_registry(self) -> None:
        with self._registry._registry_lock:
            self._registry._worker_registry.clear()
            self._registry._worker_registry.update(self._old_registry)
            self._registry._worker_epochs.clear()
            self._registry._worker_epochs.update(self._old_epochs)
        with self._registry._bus_registry_lock:
            self._registry._worker_event_bus_registry.clear()
            self._registry._worker_event_bus_registry.update(self._old_buses)
            self._registry._bus_epochs.clear()
            self._registry._bus_epochs.update(self._old_bus_epochs)

    def test_worker_thread_receives_the_parent_ids(self):
        """WHY: the thread's ``workspace_id`` is what its own ToolExecutor
        resolves permissions against.  Handing it the stale id is exactly the
        reported failure (worker comes up fully banned)."""
        tool = _make_tool()
        # Keep the agent-config build off the real WorkspaceRegistry; the
        # registry root is covered by its own tests above.
        tool._resolve_registry_workspace = mock.Mock(
            return_value=PARENT_WORKSPACE_PATH
        )
        tool._load_workers = mock.Mock(
            return_value=[{"name": WORKER_NAME, "status": "ready"}]
        )

        workspace_dir = MagicMock(name="workspace_dir")
        config_file = MagicMock(name="config.json")
        config_file.read_text.return_value = json.dumps(
            {"root": PARENT_WORKSPACE_PATH}
        )
        workspace_dir.__truediv__ = mock.Mock(return_value=config_file)

        with mock.patch(
            "tools.workspace.worker.WorkerThread"
        ) as worker_thread_cls, mock.patch(
            "tools.workspace.worker.resolve_workspace_id",
            side_effect=_resolve_parent_only,
        ), mock.patch(
            "tools.workspace.worker._workspace_dir", return_value=workspace_dir
        ):
            worker_thread_cls.return_value._generation = 1
            payload = json.loads(tool.execute())

        self.assertNotIn("error", payload, payload)
        worker_thread_cls.assert_called_once()

        kwargs = worker_thread_cls.call_args.kwargs
        self.assertEqual(kwargs["workspace_id"], PARENT_WORKSPACE_ID)
        self.assertNotEqual(kwargs["workspace_id"], STALE_WORKSPACE_ID)
        self.assertEqual(kwargs["session_id"], SESSION)


if __name__ == "__main__":
    unittest.main()
