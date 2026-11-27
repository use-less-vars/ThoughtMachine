"""A spawn that fails after registering must leave the registry as it found it.

``Worker._action_spawn`` registers the new thread in ``WorkerRegistry``, seeds its
epoch record and publishes its runtime handle -- and only THEN asks that handle to
dispatch the spawn query.  The fence that refuses a stale or dead incarnation
therefore runs *after* a registration exists, and the ``except`` that turns the
refusal into an error envelope has no rollback.  The caller gets a failed spawn
while the registry keeps a thread that never ran: the UI draws one panel per
registration, and the phantom entry survives until the process restarts.

The test drives the REAL spawn site with a fake thread class (the recipe
``tests/test_worker_fork_regression.py`` uses for the same reason): only leaves
are patched -- the thread class, the workspace resolver, the container manager --
so ``attach_thread_frame_sink``, the registry write, the epoch seed and the handle
registration all run unpatched.  ``dispatch_query`` raises
:class:`StaleWorkerFence`, which is what the fence raises in production.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Optional

import pytest

from tools.workspace import worker as worker_module
from tools.workspace import worker_thread as worker_thread_module
from tools.workspace.worker_registry import WorkerRegistry
from tools.workspace.worker_runtime import (
    MODE_PROCESS,
    WORKER_RUNTIME_MODE_ENV,
    StaleWorkerFence,
    WorkerRuntimeHandle,
    runtime_handle_for,
    unregister_runtime_handle,
)

WORKER = "w-fenced"
INSTANCE = 1


class _FencedSpawnThread(threading.Thread):
    """Stands in for ``WorkerThread`` at the spawn site: records, never runs.

    A ``threading.Thread`` subclass on purpose -- ``is_alive()`` is the genuine
    predicate -- but nothing is ever started, which is exactly the process-mode
    shape: the body lives in a child, not in this object.
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
        self._initial_context: dict = kwargs.get("initial_context") or {}
        self._timeout_seconds = 30
        self._frame_sink: Any = None
        # The epoch the spawn site seeds into ``_worker_epochs`` and latches onto
        # the runtime handle. An int, so nothing degrades to "unversioned".
        self._generation = 1
        self.status = "ready"
        self.current_task = None
        self.error = None
        self.last_heartbeat = None
        self.started = False

    def start(self) -> None:
        """Record the call; never run a body beside the (fake) child."""
        self.started = True

    def send_query(self, query: str, timeout: float = 120.0) -> str:
        raise AssertionError(
            "the fenced spawn fell back to the thread-mode transport; in process "
            "mode the queues have no consumer, so this is the freeze the handle "
            "exists to prevent"
        )

    def _last_elapsed(self) -> Optional[float]:
        return None


def _drive_the_spawn_site(tmp_path, monkeypatch, session_id: str) -> dict:
    """Run the REAL ``Worker._action_spawn`` whose handle refuses with a fence."""
    monkeypatch.setenv(WORKER_RUNTIME_MODE_ENV, MODE_PROCESS)

    def _raise_stale_fence(
        self: WorkerRuntimeHandle, query: str, timeout: float = 120.0
    ) -> str:
        raise StaleWorkerFence("the worker is no longer alive")

    monkeypatch.setattr(WorkerRuntimeHandle, "dispatch_query", _raise_stale_fence)
    monkeypatch.setattr(worker_module, "WorkerThread", _FencedSpawnThread)
    monkeypatch.setattr(worker_module, "_workspace_dir", lambda ws_id: tmp_path)
    monkeypatch.setattr(worker_thread_module, "CAPABILITIES_AVAILABLE", True)
    # No ContainerManager: constructing one probes docker.
    monkeypatch.setattr(worker_thread_module, "_CM", None)

    tool = worker_module.Worker(
        action="spawn",
        worker_name=WORKER,
        session_id=session_id,
        workspace_path=str(tmp_path),
        agent_config={"provider": "openai", "model": "gpt-4"},
        context={"query": "say hi"},
    )
    return tool._action_spawn([{"name": WORKER, "status": "ready"}], "ws-fenced")


@pytest.fixture
def _clean_registry():
    """Leave no registration or handle behind for the key this module uses."""
    keys: list[tuple[str, str, int]] = []

    def _track(session_id: str) -> None:
        keys.append((session_id, WORKER, INSTANCE))

    yield _track
    for session_id, worker_name, instance_id in keys:
        WorkerRegistry.get_instance().unregister_worker(
            session_id, worker_name, instance_id=instance_id
        )
        unregister_runtime_handle(session_id, worker_name, instance_id=instance_id)


def test_a_fenced_spawn_leaves_no_registration_behind(
    tmp_path, monkeypatch, _clean_registry
):
    """Red before the fix: the registry kept the thread the fence refused.

    The spawn registers the thread, seeds its epoch and publishes the handle
    *before* the fence is consulted, so a refused spawn that does not undo those
    writes leaves a phantom ``default``-style instance behind -- visible in the
    API, stoppable by nobody, and there until restart.
    """
    session_id = "sess-spawn-cleanup"
    _clean_registry(session_id)
    registry = WorkerRegistry.get_instance()

    result = _drive_the_spawn_site(tmp_path, monkeypatch, session_id)

    # The spawn failed at the fence, and said so.
    assert "error" in result, f"expected a failure envelope, got {result}"
    assert "StaleWorkerFence" in result["error"], result["error"]

    # ...and it left the registry, the epoch record and the handle store exactly
    # as it found them.
    assert registry.get_worker(session_id, WORKER, instance_id=INSTANCE) is None
    assert (session_id, WORKER, INSTANCE) not in registry.get_all_workers()
    assert runtime_handle_for(session_id, WORKER, INSTANCE) is None
