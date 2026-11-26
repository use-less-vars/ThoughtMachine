"""A synchronous ``query`` is a RECORDABLE job -- registered before it blocks.

Two defects are pinned here:

1. ``Worker._action_query`` touched the job registry only AFTER the blocking
   ``deliver_query_to_worker`` returned, so for the whole dispatch -- the exact
   window in which a caller asks "what is this worker doing?" -- a second tool
   call's ``action='job_status'`` answered "nothing", and a timed-out query left
   no trace at all.  The record is now opened BEFORE the delivery and closed on
   every exit path (completed / timeout / error).
2. ``_action_job_status``'s enumeration passed ``instance_id=self.instance_id``
   to ``registry.jobs(...)``, silently narrowing "this worker's jobs" to "jobs
   recorded against the spawn generation this call happens to address" -- a
   caller whose worker had been re-spawned was told there was no work.

Hermetic: ``_resolve_worker_thread`` and ``deliver_query_to_worker`` are faked;
the real ``_action_query`` / ``_action_job_status`` and the real
``WorkerJobRegistry`` run unpatched.

Run:  python3 -m pytest tests/test_worker_dispatch_job_registry.py -q
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.workspace import worker_thread as wt  # noqa: E402
from tools.workspace.job_registry import WorkerJobRegistry  # noqa: E402
from tools.workspace.worker_runtime import MODE_PROCESS  # noqa: E402

WORKER = "w-dispatch"
SESSION = "s-dispatch"
IID = 2
OTHER_IID = 7


class _NullBus:
    """Stand-in event bus: these tests feed the registry directly."""

    def subscribe(self, *a, **k):
        return None

    def publish(self, *a, **k):
        return None


class _StubBody:
    """The supervisor-side object a process-mode spawn leaves behind.

    Stamped non-thread, so ``worker_is_dispatchable`` is True; ``is_alive()``
    is False (the body is in the child), which is what the timeout path's
    bounded exit wait probes.
    """

    def __init__(self):
        self._runtime_mode = MODE_PROCESS
        self.status = "ready"
        self._timeout_seconds = 30
        self.last_heartbeat = None
        self._last_reasoning = None
        self.current_task = None
        self.error = None
        self.killed = False
        self.cleaned = False

    def is_alive(self):
        return False

    def kill(self):
        self.killed = True

    def _cleanup_worker_containers(self):
        self.cleaned = True

    def _last_elapsed(self):
        return 0.4


def _patch(monkeypatch, registry, thread, delivery):
    monkeypatch.setattr(wt, "_get_worker_job_registry", lambda: registry)
    monkeypatch.setattr(
        wt, "_resolve_worker_thread", lambda *a, **k: (thread, SESSION, IID)
    )
    monkeypatch.setattr(wt, "deliver_query_to_worker", delivery)


def _tool(instance_id=IID, worker_query="did the build pass?"):
    return wt.Worker(
        action="query",
        worker_name=WORKER,
        worker_query=worker_query,
        session_id=SESSION,
        instance_id=instance_id,
        agent_config={},
    )


def test_query_registers_its_job_before_it_blocks(monkeypatch):
    """A timed-out query must be visible WHILE in flight and terminal after.

    Prevents the "no record until the call returns" hole: a caller polling
    ``job_status`` during the block used to be told the worker had no work, and
    the timeout left nothing behind.
    """
    registry = WorkerJobRegistry(event_bus=_NullBus())
    thread = _StubBody()
    seen = {}

    def _timed_out(worker, query, *, timeout):
        # Snapshot from INSIDE the blocking delivery -- the only moment that
        # can prove the registration happened first.
        seen["statuses"] = [r["status"] for r in registry.jobs(worker_name=WORKER)]
        raise TimeoutError("no reply within budget")

    _patch(monkeypatch, registry, thread, _timed_out)

    result = _tool()._action_query([])

    assert seen["statuses"] == ["submitted"], (
        "the job was not registered before the blocking delivery: "
        f"{seen['statuses']!r}"
    )
    assert result.get("job_id"), f"timeout envelope carries no job_id: {result!r}"
    assert "error" in result, result
    record = registry.job(result["job_id"])
    assert record is not None, "the timed-out query left no job record"
    assert record["status"] == "timeout", record
    assert record["completed_at"] is not None, record
    assert record["instance_id"] == IID, record
    assert record["worker_name"] == WORKER, record


def test_successful_query_completes_its_job(monkeypatch):
    """A replied query must leave a COMPLETED record, not a stuck "submitted".

    Prevents a finished synchronous query from looking in flight forever to
    every later ``job_status`` / ``join``.
    """
    registry = WorkerJobRegistry(event_bus=_NullBus())
    thread = _StubBody()
    _patch(monkeypatch, registry, thread, lambda worker, query, *, timeout: "all green")

    result = _tool()._action_query([])

    assert result["response"] == "all green", result
    record = registry.job(result["job_id"])
    assert record is not None, "the successful query left no job record"
    assert record["status"] == "completed", record
    assert record["completed_at"] is not None, record
    assert record["result"]["response"] == "all green", record["result"]


def test_job_status_enumeration_is_not_narrowed_to_the_callers_instance(monkeypatch):
    """Enumerating a worker's jobs must not filter by the caller's instance_id.

    Prevents the silent narrowing that hid a re-spawned worker's jobs: the
    answer is the worker's jobs, with the scope it used stated in ``filters``.
    """
    registry = WorkerJobRegistry(event_bus=_NullBus())
    thread = _StubBody()
    _patch(monkeypatch, registry, thread, lambda *a, **k: "unused")
    seeded = registry.register("job-1", WORKER, session_id=SESSION, instance_id=IID)
    assert seeded is not None

    response = _tool(instance_id=OTHER_IID, worker_query="")._action_job_status([])

    assert [job["job_id"] for job in response["jobs"]] == ["job-1"], response
    assert response["filters"]["instance_id"] is None, response

    by_id = _tool(instance_id=OTHER_IID, worker_query="job-1")._action_job_status([])
    assert by_id.get("error") is None, by_id
    assert by_id["status"] == "submitted", by_id
