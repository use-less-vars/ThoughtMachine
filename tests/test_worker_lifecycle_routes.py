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


#: Epoch seeded into every fixture worker directory.  Deliberately not 1: a
#: fence that defaulted or dropped its epoch must fail these tests, not pass by
#: accident.
_SEEDED_GENERATION = 7


def _seed_worker_dir(ws_dir, name: str):
    """Real worker directory in the LEGACY layout (``workers/<name>/``).

    Also writes the ``generation`` sidecar that ``WorkerThread.__init__``
    allocates, so routed commands take the FENCED path; the bare-action write
    is only the degraded fallback for a directory that never hosted a thread.
    """
    worker_dir = ws_dir / "workers" / name
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "generation").write_text(str(_SEEDED_GENERATION), encoding="utf-8")
    return worker_dir


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_fenced_command(
    worker_dir, action, *, session_id, worker_name, instance_id, epoch=_SEEDED_GENERATION
):
    """Assert ``command.json`` carries *action* and a complete, correct fence."""
    payload = _read_json(worker_dir / "command.json")
    assert payload["action"] == action, payload
    assert payload["session_id"] == session_id, payload
    assert payload["worker_name"] == worker_name, payload
    assert payload["instance_id"] == instance_id, payload
    assert payload["epoch"] == epoch, payload
    assert set(payload) == {
        "action",
        "session_id",
        "worker_name",
        "instance_id",
        "epoch",
    }, payload


def _fake_tool_loop(self, query):  # pragma: no cover - executed by the thread
    self._last_elapsed_val = 0.0
    return json.dumps({"response": "done"})


# ── POST .../kill ───────────────────────────────────────────────────────────


def test_kill_returns_200_and_writes_kill_artifacts(monkeypatch, tmp_path):
    """``POST .../kill`` is served at that exact URL with 200 and the
    ``{"status": "ok", "name": ...}`` envelope, and it leaves exactly ONE
    artifact on disk: the fenced command.json ``{"action": "kill"}``.  The
    route does not fabricate worker state -- no optimistic ``runtime_status``
    status.json is pre-stamped; runtime status is the worker's own to report."""
    with _client(monkeypatch, tmp_path) as (client, ws_dir):
        worker_dir = _seed_worker_dir(ws_dir, "kw-kill")
        resp = client.post(_url("kw-kill", "kill"))

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok", "name": "kw-kill"}

    _assert_fenced_command(
        worker_dir, "kill", session_id="", worker_name="kw-kill", instance_id=1
    )

    # Truth comes from the worker, not the route: nothing pre-stamped a status.
    assert not (worker_dir / "status.json").exists()


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
    _assert_fenced_command(
        worker_dir, "resume", session_id="", worker_name="rw-resume", instance_id=1
    )

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
    _assert_fenced_command(
        worker_dir, "resume", session_id="", worker_name="rw-dying", instance_id=1
    )
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
    HTTP: they land DIFFERENT ``command.json`` actions -- ``{"action": "stop"}``
    vs ``{"action": "kill"}`` -- each with a complete fence.  That action is the
    only difference the route boundary is responsible for; runtime status is the
    worker's own to report, so neither verb pre-stamps a ``status.json``."""
    with _client(monkeypatch, tmp_path) as (client, ws_dir):
        stop_dir = _seed_worker_dir(ws_dir, "vw-stop")
        kill_dir = _seed_worker_dir(ws_dir, "vw-kill")

        stop_resp = client.post(_url("vw-stop", "stop"))
        kill_resp = client.post(_url("vw-kill", "kill"))

    assert stop_resp.status_code == 200, stop_resp.text
    assert kill_resp.status_code == 200, kill_resp.text
    assert stop_resp.json() == {"status": "ok", "name": "vw-stop"}
    assert kill_resp.json() == {"status": "ok", "name": "vw-kill"}

    _assert_fenced_command(
        stop_dir, "stop", session_id="", worker_name="vw-stop", instance_id=1
    )
    _assert_fenced_command(
        kill_dir, "kill", session_id="", worker_name="vw-kill", instance_id=1
    )
    # The two verbs stay distinguishable on disk, fence and all.
    assert _read_json(stop_dir / "command.json") != _read_json(kill_dir / "command.json")


# ── the command fence helpers ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sidecar, context, expected",
    [
        ("5", None, 5),  # the sidecar is the authority
        (None, {"generation": 4}, 4),  # context.json is the fallback
        ("9", {"generation": 4}, 9),  # sidecar wins when they disagree
        (None, None, None),  # nothing on disk
        ("abc", None, None),  # junk sidecar
        ("0", None, None),  # epoch 0 is not an incarnation
        ("-3", None, None),  # negative epoch
        (None, {"generation": True}, None),  # bool is not an epoch
        (None, {}, None),  # context.json without the key
        ("abc", {"generation": 4}, 4),  # unreadable sidecar -> fallback
    ],
)
def test_read_worker_generation(tmp_path, sidecar, context, expected):
    """``_read_worker_generation`` prefers the sidecar, falls back to
    ``context.json`` and refuses anything that is not a positive int."""
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir()
    if sidecar is not None:
        (worker_dir / "generation").write_text(sidecar, encoding="utf-8")
    if context is not None:
        (worker_dir / "context.json").write_text(json.dumps(context), encoding="utf-8")

    assert workspace_routes._read_worker_generation(worker_dir) == expected


def test_write_worker_command_writes_the_full_fence(tmp_path):
    """With a generation on disk the command carries all five keys: the action
    plus the identity of the incarnation it is addressed to."""
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir()
    (worker_dir / "generation").write_text("11", encoding="utf-8")

    workspace_routes._write_worker_command(
        worker_dir, "stop", session_id="sess-x", worker_name="w1", instance_id=2
    )

    assert _read_json(worker_dir / "command.json") == {
        "action": "stop",
        "session_id": "sess-x",
        "worker_name": "w1",
        "instance_id": 2,
        "epoch": 11,
    }


def test_write_worker_command_maps_missing_session_to_empty_string(tmp_path):
    """A legacy worker has no session dir: the fence carries ``""``, never
    null, so the consumer's ``type(v) is str`` gate still accepts it."""
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir()
    (worker_dir / "generation").write_text("3", encoding="utf-8")

    workspace_routes._write_worker_command(
        worker_dir, "resume", session_id=None, worker_name="w1", instance_id=1
    )

    assert _read_json(worker_dir / "command.json") == {
        "action": "resume",
        "session_id": "",
        "worker_name": "w1",
        "instance_id": 1,
        "epoch": 3,
    }


def test_write_worker_command_without_a_generation_degrades_to_the_bare_action(
    tmp_path,
):
    """A directory that never hosted a thread has no epoch: the command is
    written UNFENCED (documented degradation) rather than dropped."""
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir()

    workspace_routes._write_worker_command(
        worker_dir, "stop", session_id=None, worker_name="w1", instance_id=1
    )

    assert _read_json(worker_dir / "command.json") == {"action": "stop"}

