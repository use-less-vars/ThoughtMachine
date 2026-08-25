"""Worker instance-awareness UI tests (backend layer).

Covers the instance-aware worker UI upgrade end to end at the backend
boundary:

* ``GET /api/workspace/{ws_id}/workers`` returns one row per worker
  INSTANCE (``instance_id`` / ``instance_label``), merging runtime status,
  persisted-context markers and per-instance token counts; the ``?name=``
  filter narrows to a single worker.
* ``POST .../workers/{name}/stop|pause|resume`` accept an optional
  ``instance_id`` query param: with it, the command.json / status.json files
  are written to the instance directory (``<name>#<N>``) and the registry
  fast-path touches only that instance's thread; without it, legacy by-name
  behavior is preserved (no instance fields in the response).
* ``Worker._action_query`` resolves instances and reports
  ``instance_id``/``instance_label`` in its envelope; a manually paused
  instance returns the paused envelope without disturbing other instances.
* ``WebAgentBridge`` keys per-worker EventBus subscriptions by instance
  label (``<name>#<N>``) and stamps forwarded worker events with
  ``instance_id``/``instance_label``.
* Label helpers ``_worker_instance_label`` / ``_worker_instance_parts``.

All registry mutations are cleaned up by the autouse ``_registry_teardown``
fixture (same pattern as test_worker_sync_query_timeout_containment.py).
"""

import asyncio
import json
import queue
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from agent.events import EventBus, EventType, create_event

from tests.test_worker_sync_query_timeout_containment import _spawn_worker, _wait_until

from tools.workspace import worker as worker_module
from tools.workspace.worker import (
    Worker,
    WorkerThread,
    register_worker_event_bus,
    unregister_worker_event_bus,
)

from web_ui.backend import workspace_routes
from web_ui.backend.bridge import (
    WebAgentBridge,
    _worker_instance_label,
    _worker_instance_parts,
)


# ── fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _registry_teardown():
    """Pop registry entries added during a test and stop/join any stragglers
    so nothing leaks into later tests (mirrors the containment-test harness)."""
    with worker_module._registry_lock:
        preexisting = set(worker_module._worker_registry.keys())
    yield
    with worker_module._registry_lock:
        added = [k for k in worker_module._worker_registry.keys() if k not in preexisting]
        threads = [worker_module._worker_registry.pop(k, None) for k in added]
    for t in threads:
        if t is None:
            continue
        try:
            t.stop()
        except Exception:
            pass
        try:
            t.join(timeout=5)
        except Exception:
            pass


def _use_tmp_workspace(monkeypatch, tmp_path):
    """Point workspace_routes at the pytest tmp_path."""
    monkeypatch.setattr(workspace_routes, "_workspace_dir", lambda ws_id: tmp_path)
    monkeypatch.setattr(workspace_routes, "ensure_workspace_dirs", lambda ws_id: None)


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# ── GET /api/workspace/{ws_id}/workers ──────────────────────────────────────


def test_get_workers_returns_one_row_per_instance(tmp_path, monkeypatch):
    """workers.json configs merge with per-instance status dirs: legacy
    ``w1``/``w2`` plus instance dir ``w2#2`` yields 4 rows (w1/1, w2/1,
    w2/2, w3/1), each carrying instance_id/instance_label and the right
    runtime/persisted-context fields."""
    now = datetime.now(timezone.utc)
    _use_tmp_workspace(monkeypatch, tmp_path)
    _write_json(tmp_path / "workers.json", [
        {"name": "w1", "system_prompt": "p1"},
        {"name": "w2", "system_prompt": "p2"},
        {"name": "w3", "system_prompt": "p3"},
    ])
    _write_json(tmp_path / "workers" / "w1" / "status.json", {
        "runtime_status": "busy",
        "current_task": "task",
        "last_heartbeat": (now - timedelta(seconds=5)).isoformat(),
        "error": None,
        "session_id": None,
        "current_context_tokens": 123,
        "max_context_tokens": 456,
    })
    _write_json(tmp_path / "workers" / "w1" / "context.json", {
        "pruned_since_last_query": 2,
    })
    _write_json(tmp_path / "workers" / "w2" / "status.json", {
        "runtime_status": "ready",
        "current_task": None,
        "last_heartbeat": now.isoformat(),
        "error": None,
        "session_id": None,
        "current_context_tokens": 10,
        "max_context_tokens": 20,
    })
    _write_json(tmp_path / "workers" / "w2#2" / "status.json", {
        "runtime_status": "busy",
        "current_task": "t2",
        "last_heartbeat": now.isoformat(),
        "error": None,
        "session_id": None,
        "current_context_tokens": 30,
        "max_context_tokens": 40,
    })

    rows = asyncio.run(workspace_routes.get_workers("ws-1"))
    assert len(rows) == 4, f"expected 4 rows, got {len(rows)}: {rows}"
    by_key = {(e["name"], e["instance_id"]): e for e in rows}

    w1 = by_key[("w1", 1)]
    assert w1["instance_label"] == "w1"
    assert w1["runtime_status"] == "busy"
    assert w1["pruned_since_last_query"] == 2
    assert w1["has_persisted_context"] is True
    assert abs(w1["time_since_last_query"] - 5.0) < 3.0, w1["time_since_last_query"]
    assert w1["current_context_tokens"] == 123
    assert w1["max_context_tokens"] == 456

    w2_1 = by_key[("w2", 1)]
    assert w2_1["instance_label"] == "w2"
    assert w2_1["runtime_status"] == "ready"

    w2_2 = by_key[("w2", 2)]
    assert w2_2["instance_id"] == 2
    assert w2_2["instance_label"] == "w2#2"
    assert w2_2["runtime_status"] == "busy"
    assert w2_2["current_task"] == "t2"
    assert w2_2["current_context_tokens"] == 30
    assert w2_2["max_context_tokens"] == 40

    w3 = by_key[("w3", 1)]
    assert w3["instance_label"] == "w3"
    assert w3["runtime_status"] is None
    assert w3["session_id"] is None
    assert w3["has_persisted_context"] is False
    assert w3["time_since_last_query"] is None


def test_get_workers_name_filter(tmp_path, monkeypatch):
    """?name= returns a single dict for a matching worker (with instance
    fields) and 404s for an unknown name."""
    _use_tmp_workspace(monkeypatch, tmp_path)
    _write_json(tmp_path / "workers.json", [
        {"name": "w1", "system_prompt": "p1"},
        {"name": "w2", "system_prompt": "p2"},
    ])
    entry = asyncio.run(workspace_routes.get_workers("ws-1", name="w1"))
    assert isinstance(entry, dict)
    assert entry["name"] == "w1"
    assert entry["instance_id"] == 1
    assert entry["instance_label"] == "w1"

    with pytest.raises(HTTPException) as exc:
        asyncio.run(workspace_routes.get_workers("ws-1", name="nope"))
    assert exc.value.status_code == 404
    assert "nope" in str(exc.value.detail)


# ── POST .../workers/{name}/stop|pause|resume (instance targeting) ──────────


def test_stop_worker_with_instance_id_targets_only_that_instance(tmp_path, monkeypatch):
    """stop_worker(instance_id=2) writes command.json/status.json into the
    <name>#2 dir and fast-paths stop() only on the instance-2 thread."""
    _use_tmp_workspace(monkeypatch, tmp_path)
    with workspace_routes._registry_lock:
        workspace_routes._worker_registry[("sess-x", "default", 1)] = t1 = MagicMock()
        workspace_routes._worker_registry[("sess-x", "default", 2)] = t2 = MagicMock()
    _write_json(tmp_path / "workers" / "default#2" / "status.json", {})

    resp = asyncio.run(workspace_routes.stop_worker("ws-1", "default", instance_id=2))
    assert resp["status"] == "ok"
    assert resp["name"] == "default"
    assert resp["instance_id"] == 2
    assert resp["instance_label"] == "default#2"

    cmd = json.loads((tmp_path / "workers" / "default#2" / "command.json").read_text())
    assert cmd == {"action": "stop"}
    status = json.loads((tmp_path / "workers" / "default#2" / "status.json").read_text())
    assert status["runtime_status"] == "completed"

    t2.stop.assert_called_once()
    t1.stop.assert_not_called()


def test_pause_resume_bare_name_legacy_behavior(tmp_path, monkeypatch):
    """Without instance_id the endpoints keep legacy by-name behavior: no
    instance fields in the response, command files in the bare <name> dir,
    and every registered instance of that name is fast-pathed."""
    _use_tmp_workspace(monkeypatch, tmp_path)
    (tmp_path / "workers" / "default").mkdir(parents=True, exist_ok=True)
    with workspace_routes._registry_lock:
        workspace_routes._worker_registry[("sess-x", "default", 1)] = t1 = MagicMock()

    resp = asyncio.run(workspace_routes.pause_worker("ws-1", "default"))
    assert resp["status"] == "pausing"
    assert resp["name"] == "default"
    assert "instance_id" not in resp
    assert "instance_label" not in resp
    cmd = json.loads((tmp_path / "workers" / "default" / "command.json").read_text())
    assert cmd == {"action": "pause"}
    status = json.loads((tmp_path / "workers" / "default" / "status.json").read_text())
    assert status["runtime_status"] == "pausing"
    t1.pause.assert_called_once()

    resp2 = asyncio.run(workspace_routes.resume_worker("ws-1", "default"))
    assert resp2["status"] == "resumed"
    assert resp2["name"] == "default"
    assert "instance_id" not in resp2
    cmd2 = json.loads((tmp_path / "workers" / "default" / "command.json").read_text())
    assert cmd2 == {"action": "resume"}
    t1.resume.assert_called_once()


def test_stop_unknown_worker_404(tmp_path, monkeypatch):
    """No workers dir at all -> 404 with a not_found detail naming the
    worker."""
    _use_tmp_workspace(monkeypatch, tmp_path)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(workspace_routes.stop_worker("ws-1", "default"))
    assert exc.value.status_code == 404
    assert "default" in str(exc.value.detail)


def test_pause_with_instance_id_resolves_instance_dir(tmp_path, monkeypatch):
    """pause_worker(instance_id=2) resolves the <name>#2 directory (no
    status file needed) and returns the instance fields."""
    _use_tmp_workspace(monkeypatch, tmp_path)
    (tmp_path / "workers" / "default#2").mkdir(parents=True, exist_ok=True)

    resp = asyncio.run(workspace_routes.pause_worker("ws-1", "default", instance_id=2))
    assert resp["status"] == "pausing"
    assert resp["name"] == "default"
    assert resp["instance_id"] == 2
    assert resp["instance_label"] == "default#2"
    cmd = json.loads((tmp_path / "workers" / "default#2" / "command.json").read_text())
    assert cmd == {"action": "pause"}


# ── Worker tool: instance-aware pause/query semantics ───────────────────────


def test_real_thread_pause_resume_and_query_envelope(tmp_path, monkeypatch):
    """A live instance: pause via the route drives the thread to 'paused',
    a query against the paused instance returns the manual-pause envelope,
    resume restores it, and a subsequent query envelope carries
    instance_id/instance_label."""
    thread = _spawn_worker(tmp_path, monkeypatch, name="w1", session_id="sess-7")
    _use_tmp_workspace(monkeypatch, tmp_path)

    thread._manual_only_pause = True
    resp = asyncio.run(workspace_routes.pause_worker("ws-1", "w1"))
    assert resp["status"] == "pausing"
    assert resp["name"] == "w1"
    # The route writes command.json synchronously (proven by the response
    # above; its content is covered deterministically by the fake-thread
    # tests), but the live thread consumes and DELETES it on pickup, so
    # asserting the file after pause_worker returns is inherently racy.
    # status.json is route-written and never deleted — the durable artifact.
    status = json.loads((tmp_path / "workers" / "w1" / "status.json").read_text())
    assert status["runtime_status"] in ("pausing", "paused")
    assert _wait_until(lambda: thread.status == "paused", timeout=10.0), (
        f"worker did not reach paused (status={thread.status}, error={thread.error})"
    )

    paused = Worker(
        action="query", worker_name="w1", worker_query="hi", session_id="sess-7"
    )._action_query([])
    assert paused["status"] == "paused"
    assert "manually paused" in paused["message"]

    resp2 = asyncio.run(workspace_routes.resume_worker("ws-1", "w1"))
    assert resp2["status"] == "resumed"
    assert thread._manual_only_pause is False
    assert _wait_until(lambda: thread.status == "ready", timeout=10.0), (
        f"worker did not reach ready after resume (status={thread.status})"
    )

    def _fake_tool_loop(self, query):
        self._last_elapsed_val = 0.0
        return json.dumps({"response": "done"})

    monkeypatch.setattr(WorkerThread, "_run_tool_loop", _fake_tool_loop)
    thread._agent = object()
    envelope = Worker(
        action="query", worker_name="w1", worker_query="hi", session_id="sess-7"
    )._action_query([])
    assert envelope["worker_name"] == "w1"
    assert envelope["instance_id"] == 1
    assert envelope["instance_label"] == "w1"
    assert "response" in envelope
    assert "done" in json.dumps(envelope)

    # Stop the thread while the _save_context monkeypatch is still active so
    # run()'s finally-block save doesn't touch the real implementation after
    # teardown reverts the patch (same hygiene as the containment suite).
    thread.stop()
    assert _wait_until(lambda: not thread.is_alive(), timeout=10.0), (
        "worker thread still alive after stop"
    )


# ── Bridge: per-instance event bus subscriptions and event stamping ─────────


def test_bridge_subscribes_per_instance_bus_and_stamps_events():
    """_on_worker_spawned keys the per-worker bus subscription by instance
    label (w2#2) and forwards both the spawned event and per-bus events with
    instance_id/instance_label."""
    bus2 = EventBus()
    register_worker_event_bus("test-session-123", "w2", bus2, instance_id=2)
    received = []
    bridge = WebAgentBridge(event_callback=lambda ev: received.append(ev))
    bridge._session_id = "test-session-123"
    try:
        evt = SimpleNamespace(
            type=SimpleNamespace(value="worker_spawned"),
            data={
                "session_id": "test-session-123",
                "worker_name": "w2",
                "instance_id": 2,
            },
            metadata=SimpleNamespace(timestamp=datetime.now(timezone.utc)),
        )
        bridge._on_worker_spawned(evt)

        assert "w2#2" in bridge._worker_bus_subs, (
            f"expected per-instance subscription under 'w2#2', got "
            f"{sorted(bridge._worker_bus_subs.keys())}"
        )
        assert any(
            e.get("type") == "worker:worker_spawned"
            and e.get("instance_id") == 2
            and e.get("instance_label") == "w2#2"
            for e in received
        ), received

        received.clear()
        bus2.publish(create_event(
            EventType.SYSTEM_NOTIFICATION,
            data={
                "session_id": "test-session-123",
                "worker_name": "w2",
                "instance_id": 2,
                "instance_label": "w2#2",
            },
            source="worker",
            session_id="test-session-123",
        ))
        assert any(
            e.get("type") == "worker:system_notification"
            and e.get("instance_id") == 2
            and e.get("instance_label") == "w2#2"
            for e in received
        ), received
    finally:
        try:
            unregister_worker_event_bus("test-session-123", "w2", instance_id=2)
        except Exception:
            pass
        try:
            bridge.unregister()
        except Exception:
            pass


def test_bridge_forward_worker_event_passthrough():
    """_forward_worker_event passes worker lifecycle events through with the
    instance fields intact."""
    received2 = []
    bridge2 = WebAgentBridge(event_callback=lambda ev: received2.append(ev))
    bridge2._session_id = "test-session-123"
    try:
        evt2 = SimpleNamespace(
            type=SimpleNamespace(value="worker_completed"),
            data={
                "session_id": "test-session-123",
                "worker_name": "w2",
                "instance_id": 2,
                "instance_label": "w2#2",
            },
            metadata=SimpleNamespace(timestamp=datetime.now(timezone.utc)),
        )
        bridge2._forward_worker_event(evt2)
        assert any(
            e.get("type") == "worker:worker_completed"
            and e.get("worker_name") == "w2"
            and e.get("instance_id") == 2
            and e.get("instance_label") == "w2#2"
            for e in received2
        ), received2
    finally:
        try:
            bridge2.unregister()
        except Exception:
            pass


# ── Bridge pause/resume worker-loop guards ─────────────────────────────────


SESSION_T = "sess-pause-loop"


def _fake_worker(status="ready", query_id=None, reply_queue=None):
    """WorkerThread stand-in carrying the attributes bridge.pause()/resume()
    inspect: ``status`` plus the sticky ``_current_query_id`` /
    ``_current_reply_queue`` pair the run loop sets at dequeue (never reset)."""
    t = MagicMock()
    t.status = status
    t._current_query_id = query_id
    t._current_reply_queue = reply_queue
    t._manual_only_pause = False
    return t


def _running_bridge(session_id):
    """A bridge with a live-looking thread so pause() reaches its worker loop."""
    b = WebAgentBridge(event_callback=lambda ev: None)
    b._session_id = session_id
    b._running = True
    b._thread = MagicMock()
    b._thread.is_alive.return_value = True
    return b


def _seed_worker(key, thread):
    with worker_module._registry_lock:
        worker_module._worker_registry[key] = thread


def test_bridge_pause_skips_workers_running_async_jobs():
    """bridge.pause() must not pause a worker mid-async-job (submit_query:
    busy + _current_query_id set + no reply queue) but must still pause
    sync-blocked (reply queue set), idle (even with sticky async attrs),
    plain-string spawn and already-paused workers — and only this session."""
    async_busy = _fake_worker(status="busy", query_id="job-1", reply_queue=None)
    sync_busy = _fake_worker(status="busy", query_id="q-1", reply_queue=queue.Queue())
    idle_sticky = _fake_worker(status="ready", query_id="job-2", reply_queue=None)
    idle_plain = _fake_worker(status="ready")
    sync_spawn = _fake_worker(status="busy", query_id=None, reply_queue=None)
    paused = _fake_worker(status="paused")
    foreign = _fake_worker(status="busy", query_id="job-9", reply_queue=None)
    threads = {
        (SESSION_T, "w1", 1): async_busy,
        (SESSION_T, "w1", 2): sync_busy,
        (SESSION_T, "w2", 1): idle_sticky,
        (SESSION_T, "w3", 1): idle_plain,
        (SESSION_T, "w4", 1): sync_spawn,
        (SESSION_T, "w5", 1): paused,
        ("other-session", "w9", 1): foreign,
    }
    for key, t in threads.items():
        _seed_worker(key, t)

    bridge = _running_bridge(SESSION_T)
    try:
        bridge.pause()
    finally:
        try:
            bridge.unregister()
        except Exception:
            pass

    async_busy.pause.assert_not_called()
    foreign.pause.assert_not_called()
    sync_busy.pause.assert_called_once()
    idle_sticky.pause.assert_called_once()
    idle_plain.pause.assert_called_once()
    sync_spawn.pause.assert_called_once()
    paused.pause.assert_called_once()


def test_bridge_resume_resumes_only_paused_workers():
    """bridge.resume() must only call thread.resume() on workers that are
    actually paused (status == 'paused'); running/ready/error workers and
    other sessions are left alone."""
    paused_w = _fake_worker(status="paused")
    async_busy = _fake_worker(status="busy", query_id="job-1", reply_queue=None)
    ready = _fake_worker(status="ready")
    errored = _fake_worker(status="error")
    foreign_paused = _fake_worker(status="paused")
    threads = {
        (SESSION_T, "w1", 1): paused_w,
        (SESSION_T, "w1", 2): async_busy,
        (SESSION_T, "w2", 1): ready,
        (SESSION_T, "w3", 1): errored,
        ("other-session", "w9", 1): foreign_paused,
    }
    for key, t in threads.items():
        _seed_worker(key, t)

    bridge = _running_bridge(SESSION_T)
    try:
        bridge.resume()
    finally:
        try:
            bridge.unregister()
        except Exception:
            pass

    paused_w.resume.assert_called_once()
    async_busy.resume.assert_not_called()
    ready.resume.assert_not_called()
    errored.resume.assert_not_called()
    foreign_paused.resume.assert_not_called()


# ── Label helpers ───────────────────────────────────────────────────────────


def test_instance_label_helpers():
    assert _worker_instance_label("w2", 2) == "w2#2"
    assert _worker_instance_label("w2", 1) == "w2"
    assert _worker_instance_label("w2", None) == "w2"
    assert _worker_instance_parts("w2#2") == ("w2", 2)
    assert _worker_instance_parts("w2") == ("w2", 1)
    assert _worker_instance_parts("w2#x") == ("w2#x", 1)


# ── POST .../workers/stop_all ──────────────────────────────────────────────


def test_stop_all_stops_multiple_worker_instances(tmp_path, monkeypatch):
    """stop_all_workers() stops every instance (legacy + session-scoped):
    one per-worker result each, command.json/status.json written per
    directory, and every matching in-memory thread fast-pathed.  A
    session_id filter narrows the stop to that session only."""
    _use_tmp_workspace(monkeypatch, tmp_path)
    with workspace_routes._registry_lock:
        workspace_routes._worker_registry[("sess-x", "default", 1)] = t1 = MagicMock()
        workspace_routes._worker_registry[("sess-x", "default", 2)] = t2 = MagicMock()
        workspace_routes._worker_registry[("sess-a", "alpha", 1)] = t3 = MagicMock()
        workspace_routes._worker_registry[("sess-b", "beta", 1)] = t4 = MagicMock()
    for sub in ("default", "default#2", "sess-a/alpha", "sess-b/beta"):
        _write_json(tmp_path / "workers" / sub / "status.json", {})

    results = asyncio.run(workspace_routes.stop_all_workers("ws-1"))
    assert isinstance(results, list)
    assert len(results) == 4
    for res in results:
        assert set(res.keys()) == {"worker_name", "instance_id", "status", "error"}
        assert res["status"] == "ok"
        assert res["error"] is None
    assert sorted(r["instance_id"] for r in results if r["worker_name"] == "default") == [1, 2]
    assert sorted(r["worker_name"] for r in results) == ["alpha", "beta", "default", "default"]

    for sub in ("default", "default#2", "sess-a/alpha", "sess-b/beta"):
        cmd = json.loads((tmp_path / "workers" / sub / "command.json").read_text())
        assert cmd == {"action": "stop"}
        status = json.loads((tmp_path / "workers" / sub / "status.json").read_text())
        assert status["runtime_status"] == "completed"
    t1.stop.assert_called_once()
    t2.stop.assert_called_once()
    t3.stop.assert_called_once()
    t4.stop.assert_called_once()

    # Session filter: only sess-a's workers are touched; other sessions
    # (existing and a freshly added one) are left alone.
    _write_json(tmp_path / "workers" / "sess-c" / "gamma" / "status.json", {})
    results = asyncio.run(workspace_routes.stop_all_workers(
        "ws-1", workspace_routes.StopAllWorkersBody(session_id="sess-a")
    ))
    assert len(results) == 1
    assert results[0]["worker_name"] == "alpha"
    assert results[0]["instance_id"] == 1
    assert results[0]["status"] == "ok"
    assert results[0]["error"] is None
    assert t3.stop.call_count == 2
    assert t1.stop.call_count == 1
    assert t2.stop.call_count == 1
    assert t4.stop.call_count == 1
    assert not (tmp_path / "workers" / "sess-c" / "gamma" / "command.json").exists()


def test_stop_all_returns_per_worker_results_and_continues_on_failure(tmp_path, monkeypatch):
    """stop_all_workers() records per-worker results: a failing worker is
    reported with status/error while the remaining workers are still
    processed and reported ok.  With no workers dir at all it returns an
    empty list."""
    _use_tmp_workspace(monkeypatch, tmp_path)
    with workspace_routes._registry_lock:
        workspace_routes._worker_registry[("sess-x", "good", 1)] = tg = MagicMock()
    _write_json(tmp_path / "workers" / "good" / "status.json", {})
    _write_json(tmp_path / "workers" / "ghost" / "status.json", {})

    real_stop = workspace_routes._stop_worker_instance

    def fake_stop(ws_dir, name, instance_id=None, session_id=None):
        if name == "ghost":
            raise HTTPException(status_code=404, detail={"status": "not_found", "name": name})
        return real_stop(ws_dir, name, instance_id, session_id=session_id)

    monkeypatch.setattr(workspace_routes, "_stop_worker_instance", fake_stop)

    results = asyncio.run(workspace_routes.stop_all_workers("ws-1"))
    assert isinstance(results, list)
    assert len(results) == 2
    by_name = {r["worker_name"]: r for r in results}
    assert set(by_name.keys()) == {"good", "ghost"}

    good = by_name["good"]
    assert good["instance_id"] == 1
    assert good["status"] == "ok"
    assert good["error"] is None
    cmd = json.loads((tmp_path / "workers" / "good" / "command.json").read_text())
    assert cmd == {"action": "stop"}
    tg.stop.assert_called_once()

    ghost = by_name["ghost"]
    assert ghost["instance_id"] == 1
    assert ghost["status"] == "not_found"
    assert ghost["error"] is not None
    # The helper raised before touching disk for the failing worker.
    assert not (tmp_path / "workers" / "ghost" / "command.json").exists()

    # No workers dir at all -> empty list, no error.
    monkeypatch.setattr(workspace_routes, "_workspace_dir", lambda ws_id: tmp_path / "empty")
    assert asyncio.run(workspace_routes.stop_all_workers("ws-1")) == []

