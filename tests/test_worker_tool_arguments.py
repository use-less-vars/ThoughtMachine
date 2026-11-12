"""Worker tool argument contract: optional ``worker_name`` and ``query=``.

Two contradictions in the agent-facing tool are pinned here:

1. ``worker_name`` is genuinely OPTIONAL.  The schema always said so, but
   ``execute()`` used to reject ``None`` with "worker_name is required".  Under
   the contract an omitted (or blank) name resolves to the ``default`` worker;
   an explicit name is untouched.
2. A spawn task may be spelled ``query=`` OR ``context={'query': ...}``.  Both
   run the ONE blocking dispatch path (the context is copied into
   ``thread._initial_context`` and the spawn blocks on the worker's first
   reply).  ``query=`` is folded into the context, so:
     * ``query=`` + a context without 'query'  -> query is the task;
     * both present and DIFFERENT              -> rejected as ambiguous;
     * both present and identical              -> accepted once.

The harness drives the REAL ``Worker.execute()`` / ``_action_spawn`` (no
``_action_spawn`` stubbing) and only replaces leaves: the thread class, the
workspace resolver and the container manager.  The fake thread RECORDS the
query it was given and answers through a real ``queue.Queue``, so a passing
"dispatch" assertion means the worker's own reply came back -- not the idle
``status: "ready"`` envelope a spawn with no task returns.
"""

from __future__ import annotations

import json
import queue
from typing import Any, List

import pytest

from tools.workspace import worker as worker_module
from tools.workspace import worker_thread as worker_thread_module
from tools.workspace.worker_registry import WorkerRegistry

SESSION = "sess-worker-tool-args"


class _RecordingThread:
    """Duck-typed ``WorkerThread``: records its task, answers, never runs."""

    def __init__(self, **kwargs: Any) -> None:
        self.name = kwargs["name"]
        self.worker_name = kwargs["name"]
        self.instance_id = kwargs.get("instance_id") or 1
        self.instance_label = WorkerRegistry.instance_label(
            self.name, self.instance_id
        )
        self.status = "ready"
        self._timeout_seconds = kwargs.get("timeout_seconds") or 30
        self._initial_context: dict = {}
        self._output_queue: "queue.Queue[str]" = queue.Queue()
        self._frame_sink = None
        self.started = False
        # ``spec_from_worker`` reads these when attach_thread_frame_sink builds
        # the runtime handle for the new thread.
        self.definition = kwargs.get("definition") or {}
        self.agent_config = kwargs.get("agent_config")
        self.workspace_dir = str(kwargs.get("workspace_dir") or "")
        self.session_id = kwargs.get("session_id") or ""
        self.workspace_id = kwargs.get("workspace_id")
        self._runtime_worker_factory = None
        self.received: List[str] = []

    def start(self) -> None:
        self.started = True
        task = self._initial_context.get("query")
        if task:
            self.received.append(task)
            self._output_queue.put(
                json.dumps({"status": "ok", "response": f"ran:{task}"})
            )

    def is_alive(self) -> bool:  # registry / spawn-cap probes
        return True

    def join(self, timeout: Any = None) -> None:  # force path only
        return None

    def _last_elapsed(self) -> float:
        return 1.0


@pytest.fixture(autouse=True)
def _clean_registry():
    """Empty the process-wide worker registry around every test."""
    registry = WorkerRegistry.get_instance()
    with registry._registry_lock:
        saved = dict(registry._worker_registry)
        registry._worker_registry.clear()
    with registry._bus_registry_lock:
        saved_buses = dict(registry._worker_event_bus_registry)
        registry._worker_event_bus_registry.clear()
    try:
        yield
    finally:
        with registry._registry_lock:
            registry._worker_registry.clear()
            registry._worker_registry.update(saved)
        with registry._bus_registry_lock:
            registry._worker_event_bus_registry.clear()
            registry._worker_event_bus_registry.update(saved_buses)


@pytest.fixture
def drive(monkeypatch, tmp_path):
    """Return a ``run(**tool_kwargs) -> (tool, result, created)`` driver."""
    (tmp_path / "workers.json").write_text(
        json.dumps(
            [
                {"name": "default", "status": "ready", "permission_subset": []},
                {"name": "w1", "status": "ready", "permission_subset": []},
            ]
        ),
        encoding="utf-8",
    )
    created: List[_RecordingThread] = []

    def _factory(**kwargs: Any) -> _RecordingThread:
        thread = _RecordingThread(**kwargs)
        created.append(thread)
        return thread

    def _workspace_dir(_ws_id):
        return tmp_path

    # Leaves only: the thread class, workspace resolution, container manager.
    for module in (worker_module, worker_thread_module):
        monkeypatch.setattr(module, "WorkerThread", _factory, raising=False)
        monkeypatch.setattr(module, "_workspace_dir", _workspace_dir, raising=False)
        monkeypatch.setattr(
            module, "resolve_workspace_id", lambda *_a, **_k: "ws-tool-args",
            raising=False,
        )
    monkeypatch.setattr(worker_thread_module, "_CM", None, raising=False)
    monkeypatch.setattr(
        worker_thread_module, "CAPABILITIES_AVAILABLE", True, raising=False
    )

    def run(**tool_kwargs) -> tuple:
        tool = worker_module.Worker(
            session_id=SESSION,
            workspace_path=str(tmp_path),
            agent_config={"provider": "openai", "model": "gpt-4"},
            **tool_kwargs,
        )
        result = json.loads(tool.execute())
        return tool, result, created

    return run


# ── 1. worker_name is optional ────────────────────────────────────────────


def test_omitted_worker_name_resolves_to_default(drive):
    """No worker_name -> the 'default' worker, not a required-name error."""
    tool, result, created = drive(action="spawn", query="do the thing")

    assert "required" not in result.get("error", "")
    assert tool.worker_name == "default"
    assert created and created[-1].name == "default"
    assert result["worker_name"] == "default"


def test_blank_worker_name_is_omitted(drive):
    """A blank/whitespace name counts as OMITTED (never a worker named '')."""
    tool = worker_module.Worker(action="spawn", worker_name="   ")
    assert tool.worker_name is None  # validator: blank -> omitted

    tool, result, created = drive(action="spawn", worker_name="   ")
    assert tool.worker_name == "default"
    assert created[-1].name == "default"


def test_explicit_worker_name_wins(drive):
    """An explicit name is never replaced by the default."""
    tool, result, created = drive(action="spawn", worker_name="w1", query="hi")

    assert tool.worker_name == "w1"
    assert created[-1].name == "w1"
    assert result["worker_name"] == "w1"


# ── 2. query= is the same task as context['query'] ────────────────────────


def test_top_level_query_dispatches_and_returns_worker_result(drive):
    """query= reaches the worker AND the worker's reply is what comes back."""
    tool, result, created = drive(action="spawn", query="Review src/")

    assert created[-1].received == ["Review src/"], "the query never reached the worker"
    assert result["response"] == "ran:Review src/"
    assert result.get("status") != "ready", (
        "an idle 'ready' envelope means the task was never dispatched"
    )
    assert result["spawned"] is True


def test_conflicting_query_and_context_is_rejected(drive):
    """Different tasks in query= and context['query'] -> explicit error."""
    tool, result, created = drive(
        action="spawn", query="task A", context={"query": "task B"}
    )

    error = result.get("error", "")
    assert "task A" in error and "task B" in error, error
    assert "onflict" in error
    assert created == [], "a rejected call must dispatch nothing"


def test_identical_query_and_context_accepted_once(drive):
    """The same task given twice is accepted once, not dispatched twice."""
    tool, result, created = drive(
        action="spawn", query="same task", context={"query": "same task"}
    )

    assert created[-1].received == ["same task"]
    assert result["response"] == "ran:same task"
    assert result.get("status") != "ready"


def test_query_is_spawn_only(drive):
    """query= on a non-spawn action points at worker_query, dispatches nothing."""
    _tool, result, created = drive(action="query", query="late task")

    assert "only valid with action='spawn'" in result.get("error", "")
    assert "worker_query" in result.get("error", "")
    assert created == []
