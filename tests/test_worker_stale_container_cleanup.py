"""Unit tests for stale worker-container cleanup in infra/container_manager.

Covers the module-level ``cleanup_stale_worker_containers`` helper and its
invocation from ``ContainerManager.start(worker_name=...)`` on the fresh-create
path: when a worker session crashed/hung without teardown, its leftover
containers (same owner identity, state created/exited/dead) must be removed
BEFORE a new container for the same owner is spawned.

No live Docker: docker SDK objects are faked with ``SimpleNamespace`` shaped
like ``docker.models.containers.Container`` (``id`` / ``name`` / ``labels`` /
``status`` / ``attrs`` plus ``remove(force=True)``).

NOTE: ``infra.container_manager`` is imported LAZILY (inside setUp / tests),
never at module import time — see tests/docker/test_container_lifecycle.py:
a top-level import triggers a circular-import cascade (agent.logging is left
mid-import while thoughtmachine.security runs its ``from agent.events
import global_event_bus, EventType, create_event``).
"""

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

_SRC_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

_WORKER_LABEL = "thoughtmachine.worker"
_RESOURCE_LABEL = "thoughtmachine.resource"


def _fake_container(cid, status, labels=None, name=None):
    """SimpleNamespace shaped like a docker Container (unit-test friendly)."""
    container = SimpleNamespace(
        id=cid,
        name=name or cid,
        labels=labels or {},
        status=status,
    )
    container.remove = mock.Mock(return_value=None)
    return container


class _FakeDockerClient:
    """Minimal docker client: containers.list returns a fixed list, records kwargs."""

    def __init__(self, containers):
        self._containers = containers
        self.list_calls = []
        self.containers = SimpleNamespace(list=self._list)

    def _list(self, all=False, filters=None):
        self.list_calls.append({"all": all, "filters": filters})
        return self._containers


class StaleWorkerContainerCleanupTest(unittest.TestCase):
    def setUp(self):
        from infra import container_manager  # lazy (see module docstring)
        self.container_manager = container_manager
        self.cleanup = container_manager.cleanup_stale_worker_containers

    # ── standalone helper: cleanup_stale_worker_containers ─────────────────

    def test_removes_created_exited_dead_matching_owner(self):
        owner = "sess-1:w1"
        created = _fake_container("c1", "created", {_WORKER_LABEL: owner})
        exited = _fake_container("c2", "exited", {_WORKER_LABEL: owner})
        dead = _fake_container("c3", "dead", {_WORKER_LABEL: owner})
        client = _FakeDockerClient([created, exited, dead])

        removed = self.cleanup(client, owner)

        self.assertEqual(removed, ["c1", "c2", "c3"])
        for container in (created, exited, dead):
            container.remove.assert_called_once_with(force=True)
        self.assertEqual(len(client.list_calls), 1)
        self.assertTrue(client.list_calls[0]["all"])
        self.assertEqual(client.list_calls[0]["filters"],
                         {"label": {_WORKER_LABEL: owner}})

    def test_skips_running_and_paused_matching_owner(self):
        owner = "sess-1:w1"
        running = _fake_container("run1", "running", {_WORKER_LABEL: owner})
        paused = _fake_container("pau1", "paused", {_WORKER_LABEL: owner})
        client = _FakeDockerClient([running, paused])

        removed = self.cleanup(client, owner)

        self.assertEqual(removed, [])
        running.remove.assert_not_called()
        paused.remove.assert_not_called()

    def test_skips_resource_labeled_containers(self):
        owner = "sess-1:w1"
        resource = _fake_container("res1", "exited",
                                   {_WORKER_LABEL: owner, _RESOURCE_LABEL: "1"})
        client = _FakeDockerClient([resource])

        removed = self.cleanup(client, owner)

        self.assertEqual(removed, [])
        resource.remove.assert_not_called()

    def test_skips_other_owner_identity(self):
        container = _fake_container("o1", "exited", {_WORKER_LABEL: "other-sess:w9"})
        client = _FakeDockerClient([container])

        removed = self.cleanup(client, "sess-1:w1")

        self.assertEqual(removed, [])
        container.remove.assert_not_called()

    def test_skips_containers_without_labels(self):
        container = _fake_container("nol1", "exited", {})
        client = _FakeDockerClient([container])

        removed = self.cleanup(client, "sess-1:w1")

        self.assertEqual(removed, [])
        container.remove.assert_not_called()

    def test_returns_empty_list_when_no_matches(self):
        client = _FakeDockerClient([])

        removed = self.cleanup(client, "sess-1:w1")

        self.assertEqual(removed, [])
        self.assertEqual(len(client.list_calls), 1)
        self.assertTrue(client.list_calls[0]["all"])

    # ── invocation from the fresh-create path in start() ───────────────────

    def test_start_invokes_cleanup_before_fresh_worker_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.container_manager.ContainerManager.__new__(
                self.container_manager.ContainerManager)
            manager.image = "agent-executor"
            manager.workspace_path = tmp
            manager.session_id = "sess-1"
            manager.session_permissions = {}
            manager._session_config = None
            manager.workspace_id = "ws1"
            manager.vault_root = tmp
            manager.workspace_config = {}
            manager.max_containers = 4
            manager.container_notes = {}
            manager._containers = {}
            manager.mem_limit = "512m"
            manager.cpu_quota = 50000

            events = []
            client = mock.Mock()
            created = SimpleNamespace(id="new1", reload=lambda: None)
            client.containers.run.side_effect = \
                lambda **kw: (events.append("create"), created)[1]
            client.containers.list.return_value = []
            manager.client = client

            # Shadow methods whose real implementations need __init__-built
            # docker state (fresh create path only exercises these).
            manager.list_containers = lambda: []
            manager._get_max_containers = lambda: 4
            manager._compute_config = lambda *a, **k: ("none", "rw")
            manager._find_by_labels = lambda name: None
            manager._save_container_notes = lambda: None

            with mock.patch.object(
                    self.container_manager, "cleanup_stale_worker_containers",
                    side_effect=lambda *a, **k: events.append("cleanup")) as cleanup_mock, \
                    mock.patch.object(self.container_manager, "is_registry_active",
                                      return_value=False), \
                    mock.patch.object(self.container_manager, "_audit"), \
                    mock.patch.object(self.container_manager, "log_container_event"), \
                    mock.patch.object(self.container_manager, "Mount",
                                      return_value="mount"):
                result = manager.start(worker_name="sess-1:w1")

            cleanup_mock.assert_called_once_with(client, "sess-1:w1")
            self.assertEqual(events, ["cleanup", "create"])
            self.assertEqual(result["status"], "created")
            self.assertEqual(result["id"], "new1")


class ContainerLimitCountsActiveOnlyTest(unittest.TestCase):
    """Regression: terminal containers must not occupy a limit slot.

    Bug: ``start()`` compared ``len(list_containers(all=True))`` against the
    limit, so a workspace holding only EXITED/DEAD/REMOVING containers refused
    every fresh create with "Workspace container limit (N) reached." Only
    non-terminal containers may count; terminal states free a slot.

    Fully mocked (``ContainerManager.__new__`` harness): no Docker daemon.
    """

    def setUp(self):
        from infra import container_manager  # lazy (see module docstring)
        self.container_manager = container_manager
        self.ContainerManager = container_manager.ContainerManager

    def _entry(self, cid, status, name=None):
        """A list_containers() dict shaped like the real return value."""
        return {
            "container_id": cid,
            "name": name or f"ws-{cid}",
            "image": "agent-executor",
            "status": status,
            "uptime_seconds": 0,
            "workspace_id": "ws1",
            "note": "",
            "labels": {},
        }

    def _make_manager(self, tmp, entries, limit):
        manager = self.ContainerManager.__new__(self.ContainerManager)
        manager.image = "agent-executor"
        manager.workspace_path = tmp
        manager.session_id = "sess-1"
        manager.session_permissions = {}
        manager._session_config = None
        manager.workspace_id = "ws1"
        manager.vault_root = tmp
        manager.workspace_config = {}
        manager.max_containers = limit
        manager.container_notes = {}
        manager._containers = {}
        manager.mem_limit = "512m"
        manager.cpu_quota = 50000

        client = mock.Mock()
        created = SimpleNamespace(id="new1", reload=lambda: None)
        client.containers.run.side_effect = lambda **kw: created
        client.containers.list.return_value = []
        manager.client = client

        # Shadow methods whose real implementations need __init__-built state.
        # ``list(entries)`` copies the LIST but keeps the SAME dict objects, so
        # tests can mutate an entry's status to model a container transition.
        manager.list_containers = lambda: list(entries)
        manager._get_max_containers = lambda: limit
        manager._compute_config = lambda *a, **k: ("none", "rw")
        manager._find_by_labels = lambda name: None
        manager._save_container_notes = lambda: None
        return manager

    def _start(self, manager, worker_name=None):
        with mock.patch.object(self.container_manager,
                               "cleanup_stale_worker_containers",
                               return_value=[]), \
                mock.patch.object(self.container_manager, "is_registry_active",
                                  return_value=False), \
                mock.patch.object(self.container_manager, "_audit"), \
                mock.patch.object(self.container_manager, "log_container_event"), \
                mock.patch.object(self.container_manager, "Mount",
                                  return_value="mount"):
            return manager.start(worker_name=worker_name)

    # ── helper unit tests ───────────────────────────────────────────────

    def test_counts_toward_limit_terminal_states_free_slot(self):
        manager = self.ContainerManager.__new__(self.ContainerManager)
        for status in ("exited", "dead", "removing", "EXITED", "Dead"):
            with self.subTest(status=status):
                self.assertFalse(
                    manager._counts_toward_limit(self._entry("c1", status)))

    def test_counts_toward_limit_active_states_occupy_slot(self):
        manager = self.ContainerManager.__new__(self.ContainerManager)
        for status in ("running", "created", "restarting", "paused",
                       "new-status"):
            with self.subTest(status=status):
                self.assertTrue(
                    manager._counts_toward_limit(self._entry("c1", status)))

    def test_counts_toward_limit_missing_status_is_fail_safe(self):
        manager = self.ContainerManager.__new__(self.ContainerManager)
        self.assertTrue(manager._counts_toward_limit({}))
        self.assertTrue(manager._counts_toward_limit(None))

    # ── (a) limit full of EXITED containers -> start() SUCCEEDS ─────────

    def test_limit_full_of_exited_containers_allows_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            entries = [self._entry("c1", "exited"),
                       self._entry("c2", "exited")]
            manager = self._make_manager(tmp, entries, limit=2)
            result = self._start(manager)
            self.assertEqual(result.get("status"), "created", result)
            self.assertEqual(result.get("id"), "new1")

    # ── (b) limit full of RUNNING containers -> refused ──────────────

    def test_limit_full_of_running_containers_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            entries = [self._entry("c1", "running"),
                       self._entry("c2", "running")]
            manager = self._make_manager(tmp, entries, limit=2)
            result = self._start(manager)
            self.assertIn("error", result, result)
            self.assertIn("limit (2)", result["error"])
            self.assertIn("2 active container(s)", result["error"])

    # ── (c) stop one of N running -> next start() SUCCEEDS ───────────────

    def test_start_succeeds_after_a_running_container_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = [self._entry("c1", "running"),
                     self._entry("c2", "running")]
            manager = self._make_manager(tmp, state, limit=2)
            self.assertIn("error", self._start(manager))

            # stop() leaves the container in a terminal state, which is what
            # list_containers() then reports for it.
            state[0]["status"] = "exited"
            result = self._start(manager)
            self.assertEqual(result.get("status"), "created", result)

    # ── (d) unknown/new status counts toward limit (fail-safe) ───────────

    def test_unknown_status_container_counts_toward_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            entries = [self._entry("c1", "running"),
                       self._entry("c2", "restarting")]
            manager = self._make_manager(tmp, entries, limit=2)
            result = self._start(manager)
            self.assertIn("error", result, result)
            self.assertIn("2 active container(s)", result["error"])

    def test_mixed_statuses_count_only_non_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            entries = [self._entry("c1", "running"),
                       self._entry("c2", "exited"),
                       self._entry("c3", "dead")]
            manager = self._make_manager(tmp, entries, limit=2)
            # 1 active < limit 2 -> create allowed despite 3 total entries.
            result = self._start(manager)
            self.assertEqual(result.get("status"), "created", result)


if __name__ == "__main__":
    unittest.main()
