"""Worker tray truth-field tests.

Verifies that ``WorkerThread`` publishes started_at / last_query_at /
paused_manually truth to both ``status.json`` and the ``get_workers``
REST endpoint: the thread stamps ``started_at`` at birth, ``last_query_at``
when a query is dequeued, and ``paused_manually`` exactly while the worker
is manual-only paused (``worker_query="manual_only"``), never for a plain
pause.
"""

import asyncio
import json
import queue
import time
from datetime import datetime, timedelta, timezone

import pytest

# Containment-test parity imports (may be unused here).
from agent.events import EventBus, EventType, create_event  # noqa: F401
from tools.workspace import worker as worker_module
from tools.workspace.worker import Worker, WorkerThread
from web_ui.backend import workspace_routes

from tests.test_worker_sync_query_timeout_containment import _spawn_worker, _wait_until


@pytest.fixture(autouse=True)
def _registry_teardown():
    """Stop/join any worker threads this module's tests registered.

    run() does NOT unregister the thread — tests register manually and
    must pop their entries afterwards; stop/join any stragglers so
    nothing leaks into later tests.
    """
    keys_before = set(worker_module._worker_registry.keys())
    yield
    with worker_module._registry_lock:
        added = [k for k in worker_module._worker_registry.keys() if k not in keys_before]
        for k in added:
            t = worker_module._worker_registry.pop(k, None)
            if t is not None:
                try:
                    t.stop()
                except Exception:
                    pass
    for k in added:
        t = worker_module._worker_registry.get(k)
        if t is not None:
            t.join(timeout=5)


def _fake_tool_loop(self, query):
    """Fake ``_run_tool_loop``: instant JSON reply, no Agent required."""
    self._last_elapsed_val = 0.0
    return json.dumps({"response": "ok", "query": query})


def _read_status(tmp_path, name, session_id=None):
    if session_id is None:
        path = tmp_path / "workers" / name / "status.json"
    else:
        path = tmp_path / "workers" / session_id / name / "status.json"
    # run() flips status to "ready" BEFORE _write_status_file() lands the
    # file (atomic os.replace), so wait for existence instead of racing.
    assert _wait_until(lambda: path.exists()), f"status.json never appeared: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_started_at_set_when_worker_starts(tmp_path, monkeypatch):
    thread = _spawn_worker(tmp_path, monkeypatch, "w1", "sess-start")

    assert thread.started_at is not None
    started = datetime.fromisoformat(thread.started_at)
    age = (datetime.now(timezone.utc) - started).total_seconds()
    assert 0.0 <= age <= 60.0

    status = _read_status(tmp_path, "w1")
    assert status["started_at"] == thread.started_at
    assert status["last_query_at"] is None
    assert status["paused_manually"] is False


def test_last_query_at_updates_after_query(tmp_path, monkeypatch):
    thread = _spawn_worker(tmp_path, monkeypatch, "w1", "sess-q")
    monkeypatch.setattr(WorkerThread, "_run_tool_loop", _fake_tool_loop)
    thread._agent = object()  # skip real Agent construction in the busy path

    reply_q = queue.Queue()
    thread._input_queue.put(("q1", "hello", reply_q))
    envelope = json.loads(reply_q.get(timeout=10))
    assert envelope["query_id"] == "q1"

    first = thread.last_query_at
    assert first is not None
    assert datetime.fromisoformat(first) >= datetime.fromisoformat(thread.started_at)

    time.sleep(0.05)
    reply_q2 = queue.Queue()
    thread._input_queue.put(("q2", "again", reply_q2))
    envelope2 = json.loads(reply_q2.get(timeout=10))
    assert envelope2["query_id"] == "q2"

    second = thread.last_query_at
    assert second is not None
    assert datetime.fromisoformat(second) >= datetime.fromisoformat(first)

    status = _read_status(tmp_path, "w1")
    assert status["last_query_at"] == second


def test_paused_manually_true_only_for_manual_only_pause(tmp_path, monkeypatch):
    thread = _spawn_worker(tmp_path, monkeypatch, "w1", "sess-pm")
    thread._save_context = lambda: None

    def _status():
        path = tmp_path / "workers" / "w1" / "status.json"
        # _wait_until does not swallow predicate exceptions: wait for the
        # file here (run() sets status="ready" before the atomic replace).
        assert _wait_until(lambda: path.exists()), f"status.json never appeared: {path}"
        return json.loads(path.read_text(encoding="utf-8"))

    assert _wait_until(lambda: _status()["paused_manually"] is False)

    # Plain pause (Pause All): NOT manual-only -> paused_manually False.
    tool = Worker(action="pause", worker_name="w1", worker_query="all", session_id="sess-pm")
    result = tool._action_pause([])
    assert result["manual_only"] is False
    assert _wait_until(lambda: thread.status == "paused")
    assert _wait_until(lambda: _status()["paused_manually"] is False)

    thread.resume()
    assert _wait_until(lambda: thread.status == "ready")
    assert _wait_until(lambda: _status()["paused_manually"] is False)

    # Manual-only pause: paused_manually True exactly while paused.
    tool2 = Worker(
        action="pause",
        worker_name="w1",
        worker_query="manual_only",
        session_id="sess-pm",
    )
    result2 = tool2._action_pause([])
    assert result2["manual_only"] is True
    assert _wait_until(lambda: thread.status == "paused")
    assert thread._manual_only_pause is True
    assert _wait_until(lambda: _status()["paused_manually"] is True)

    thread.resume()
    assert _wait_until(lambda: thread.status == "ready")
    assert _wait_until(lambda: _status()["paused_manually"] is False)


def test_get_workers_returns_all_three_fields(tmp_path, monkeypatch):
    def _use_tmp_workspace(monkeypatch, tmp_path):
        monkeypatch.setattr(workspace_routes, "_workspace_dir", lambda ws_id: tmp_path)
        monkeypatch.setattr(workspace_routes, "ensure_workspace_dirs", lambda ws_id: None)

    def _write_json(path, obj):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj), encoding="utf-8")

    _use_tmp_workspace(monkeypatch, tmp_path)
    _write_json(tmp_path / "workers.json", [{"name": "w1"}, {"name": "w2"}, {"name": "w3"}])

    now = datetime.now(timezone.utc)
    # Legacy layout: workers/w1/status.json (no session scope).
    _write_json(
        tmp_path / "workers" / "w1" / "status.json",
        {
            "runtime_status": "ready",
            "current_task": None,
            "last_heartbeat": now.isoformat(),
            "error": None,
            "session_id": None,
            "current_context_tokens": 10,
            "max_context_tokens": 4000,
            "started_at": (now - timedelta(minutes=10)).isoformat(),
            "last_query_at": (now - timedelta(minutes=1)).isoformat(),
            "paused_manually": True,
        },
    )
    # Session-scoped layout: workers/sess-a/w2/status.json.
    _write_json(
        tmp_path / "workers" / "sess-a" / "w2" / "status.json",
        {
            "runtime_status": "busy",
            "current_task": "working",
            "last_heartbeat": now.isoformat(),
            "error": None,
            "session_id": "sess-a",
            "current_context_tokens": 20,
            "max_context_tokens": 4000,
            "started_at": (now - timedelta(hours=1)).isoformat(),
            "last_query_at": (now - timedelta(minutes=5)).isoformat(),
            "paused_manually": False,
        },
    )
    # w3 deliberately has no status.json -> all three fields None.

    result = asyncio.run(workspace_routes.get_workers("ws-1"))
    by_name = {e["name"]: e for e in result}

    w1 = by_name["w1"]
    assert w1["started_at"] == (now - timedelta(minutes=10)).isoformat()
    assert w1["last_query_at"] == (now - timedelta(minutes=1)).isoformat()
    assert w1["paused_manually"] is True

    w2 = by_name["w2"]
    assert w2["started_at"] == (now - timedelta(hours=1)).isoformat()
    assert w2["last_query_at"] == (now - timedelta(minutes=5)).isoformat()
    assert w2["paused_manually"] is False

    w3 = by_name["w3"]
    assert w3["started_at"] is None
    assert w3["last_query_at"] is None
    assert w3["paused_manually"] is None
