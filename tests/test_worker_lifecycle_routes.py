"""HTTP-level tests for the operator-only worker lifecycle routes.

PHASE 2.1a split the per-worker lifecycle into three distinct verbs, all
served by ``web_ui/backend/workspace_routes.py``::

    POST /api/workspace/{ws_id}/workers/{name}/stop    -> DETACH  (park, revivable)
    POST /api/workspace/{ws_id}/workers/{name}/kill    -> TERMINATE (terminal)
    POST /api/workspace/{ws_id}/workers/{name}/resume  -> revive a detached worker

Why this module instead of extending ``tests/test_worker_instance_ui.py``:
that suite calls the handlers directly (``asyncio.run(stop_worker(...))``) and
monkeypatches ``_workspace_dir``, so a typo'd decorator path, a dropped
``instance_id`` ``Query`` parameter, a swapped status code or a mislabelled
command file would sail straight through it.  These tests drive the real
router through a real ``TestClient`` against a real on-disk vault (the same
harness as ``tests/test_worker_blueprint_routes.py``), so what is asserted is
the URL/method/status-code wiring, the response body, the on-disk command
artifacts and the live registry thread's own state -- never a mock call log.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.workspace import worker as worker_module
from tools.workspace.worker import WorkerThread
from web_ui.backend import workspace_routes
from web_ui.backend.workspace_routes import router

from tests.test_worker_sync_query_timeout_containment import _spawn_worker, _wait_until


WS_ID = "ws-lifecycle"
WORKERS = f"/api/workspace/{WS_ID}/workers"


# ── fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _registry_teardown():
    """Pop registry entries added during a test and stop/join stragglers, so a
    live worker thread can never leak into a later test (same pattern as
    ``tests/test_worker_instance_ui.py``)."""
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


@contextmanager
def _client(monkeypatch, tmp_path):
    """The real router over a real tmp vault (``THOUGHTMACHINE_VAULT_ROOT``)."""
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
    ws_dir = tmp_path / "workspaces" / WS_ID
    (ws_dir / "workers").mkdir(parents=True, exist_ok=True)

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client, ws_dir


def _url(name: str, verb: str) -> str:
    return f"{WORKERS}/{name}/{verb}"


def _seed_worker_dir(ws_dir, name: str):
    """Real worker directory in the LEGACY layout (``workers/<name>/``)."""
    worker_dir = ws_dir / "workers" / name
    worker_dir.mkdir(parents=True, exist_ok=True)
    return worker_dir


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _fake_tool_loop(self, query):  # pragma: no cover - executed by the thread
    self._last_elapsed_val = 0.0
    return json.dumps({"response": "done"})


# ── POST .../kill ───────────────────────────────────────────────────────────


def test_kill_returns_200_and_writes_kill_artifacts(monkeypatch, tmp_path):
    """``POST .../kill`` is served at that exact URL with 200 and the
    ``{"status": "ok", "name": ...}`` envelope, and it leaves the TERMINATING
    artifacts on disk: command.json ``{"action": "kill"}`` plus the optimistic
    ``runtime_status: "killed"`` status.json (not the detach pair)."""
    with _client(monkeypatch, tmp_path) as (client, ws_dir):
        worker_dir = _seed_worker_dir(ws_dir, "kw-kill")
        resp = client.post(_url("kw-kill", "kill"))

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok", "name": "kw-kill"}

    assert _read_json(worker_dir / "command.json") == {"action": "kill"}

    status = _read_json(worker_dir / "status.json")
    assert status["runtime_status"] == "killed"
    assert status["current_task"] is None
    assert status["error"] is None
    assert isinstance(status["last_heartbeat"], str) and status["last_heartbeat"]


def test_kill_unknown_worker_returns_404(monkeypatch, tmp_path):
    """An unknown worker name 404s at the HTTP layer (not a 200 with an error
    envelope) and the detail names the worker."""
    with _client(monkeypatch, tmp_path) as (client, ws_dir):
        resp = client.post(_url("kw-ghost", "kill"))

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == {"status": "not_found", "name": "kw-ghost"}
    # Nothing was created for the unknown worker.
    assert not (ws_dir / "workers" / "kw-ghost").exists()


# ── POST .../resume ─────────────────────────────────────────────────────────


def test_resume_returns_200_and_revives_a_detached_worker(monkeypatch, tmp_path):
    """``POST .../resume`` answers 200 ``{"status": "resumed"}`` for a DETACHED
    worker that has work queued, and the live thread is genuinely no longer
    detached afterwards -- asserted on the thread's own detach state, not on
    the response body alone."""
    monkeypatch.setattr(WorkerThread, "_run_tool_loop", _fake_tool_loop)

    with _client(monkeypatch, tmp_path) as (client, ws_dir):
        worker_dir = _seed_worker_dir(ws_dir, "rw-resume")

        # A real, running worker instance (its own dir lives outside the vault
        # workspace, so the route's command.json is not consumed by it).
        thread = _spawn_worker(
            tmp_path / "live",
            monkeypatch,
            name="rw-resume",
            session_id="sess-life",
        )
        thread.stop()
        assert thread.status == "detached"
        assert thread._detach_requested is True
        assert thread._detached_at is not None
        # Work arriving while detached is QUEUED (it never silently resumes
        # the worker): that is the state the operator resumes out of.
        thread.enqueue_query("job-1", "hi")
        assert thread.pending_query_jobs() == ["job-1"]

        resp = client.post(_url("rw-resume", "resume"))

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "resumed", "name": "rw-resume"}
    assert _read_json(worker_dir / "command.json") == {"action": "resume"}

    # The in-memory fast-path really resumed THIS thread: the detach markers
    # are cleared and it left the detached state.  (The resume/pause events are
    # transient wake signals the run loop clears again, so they are not
    # asserted; the detach markers and the status are what persist.)
    assert thread._detach_requested is False
    assert thread._detached_at is None
    assert _wait_until(lambda: thread.status != "detached", timeout=10.0), (
        f"worker stayed detached after resume (status={thread.status})"
    )


def test_resume_reports_refused_when_a_stop_is_in_flight(monkeypatch, tmp_path):
    """With a stop already in flight the live thread refuses to resume
    (``WorkerThread.resume()`` returns False), so the route must NOT claim
    "resumed": HTTP 200 with ``status: "refused"``, the refused instance
    listed with its reason, and the instance left detached.  The resume
    command file is still written first (as the note says)."""
    with _client(monkeypatch, tmp_path) as (client, ws_dir):
        worker_dir = _seed_worker_dir(ws_dir, "rw-dying")

        # A real WorkerThread in the exact state ``resume()`` refuses: a stop
        # is already in flight (``_stop_event`` set).  Never started, so there
        # is no run loop racing this test.
        dying = WorkerThread.__new__(WorkerThread)
        dying.worker_name = "rw-dying"
        dying.status = "detached"
        dying._stop_event = threading.Event()
        dying._stop_event.set()
        with workspace_routes._registry_lock:
            workspace_routes._worker_registry[("sess-life", "rw-dying", 1)] = dying

        resp = client.post(_url("rw-dying", "resume"))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "refused"
    assert body["name"] == "rw-dying"
    assert body["workers_refused"] == [
        {
            "worker_name": "rw-dying",
            "instance_id": 1,
            "reason": "stop already in flight",
        }
    ]
    assert "Spawn a fresh instance instead" in body["note"]

    # The command file is written before the refusal is decided ...
    assert _read_json(worker_dir / "command.json") == {"action": "resume"}
    # ... and the live instance was genuinely NOT revived.
    assert dying.status == "detached"


def test_resume_unknown_worker_returns_404(monkeypatch, tmp_path):
    """Same 404 contract as kill/stop: unknown worker name, HTTP 404, detail
    names the worker."""
    with _client(monkeypatch, tmp_path) as (client, ws_dir):
        resp = client.post(_url("rw-ghost", "resume"))

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == {"status": "not_found", "name": "rw-ghost"}
    assert not (ws_dir / "workers" / "rw-ghost").exists()


# ── stop vs kill ────────────────────────────────────────────────────────────


def test_stop_and_kill_are_distinguishable_over_http(monkeypatch, tmp_path):
    """The two verbs share a success envelope but are NOT interchangeable over
    HTTP: ``stop`` writes command.json ``{"action": "stop"}`` with the
    optimistic ``runtime_status: "detached"``, ``kill`` writes
    ``{"action": "kill"}`` with ``"killed"``."""
    with _client(monkeypatch, tmp_path) as (client, ws_dir):
        stop_dir = _seed_worker_dir(ws_dir, "vw-stop")
        kill_dir = _seed_worker_dir(ws_dir, "vw-kill")

        stop_resp = client.post(_url("vw-stop", "stop"))
        kill_resp = client.post(_url("vw-kill", "kill"))

    assert stop_resp.status_code == 200, stop_resp.text
    assert kill_resp.status_code == 200, kill_resp.text
    assert stop_resp.json() == {"status": "ok", "name": "vw-stop"}
    assert kill_resp.json() == {"status": "ok", "name": "vw-kill"}

    stop_cmd = _read_json(stop_dir / "command.json")
    kill_cmd = _read_json(kill_dir / "command.json")
    assert stop_cmd == {"action": "stop"}
    assert kill_cmd == {"action": "kill"}
    assert stop_cmd != kill_cmd

    assert _read_json(stop_dir / "status.json")["runtime_status"] == "detached"
    assert _read_json(kill_dir / "status.json")["runtime_status"] == "killed"
