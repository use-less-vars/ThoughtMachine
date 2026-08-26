"""Sync-worker Docker-stop guarantee tests.

Verifies the stop guarantee in ``Worker._action_query``
(tools/workspace/worker.py): when a synchronous query times out while the
worker thread is GENUINELY BUSY inside ``_run_tool_loop`` (stuck in a
DockerCodeRunner call), the timeout branch stops the thread cooperatively,
reclaims worker-owned containers, and then WAITS (bounded join-retry, never
``Thread.kill``) for the thread to actually exit BEFORE returning the error
envelope. The sync call therefore never returns while the worker thread is
still alive, and no orphan thread survives the call. On the normal
(non-timeout) reply path the worker stays alive (async model) and the
response envelope is returned only after the worker actually responded.

Docker-free: container cleanup is exercised against a ``_FakeContainerManager``
that records stop/remove calls.
"""

import json
import queue
import threading
import time
from pathlib import Path

import pytest

from tools.workspace import worker as worker_module
from tools.workspace.worker import (
    Worker,
    WorkerThread,
    _RESOURCE_CONTAINER_LABEL,
    _WORKER_CONTAINER_LABEL,
)

# ── fakes ──────────────────────────────────────────────────────────────────


class _FakeContext:
    """Minimal stand-in for WorkerContext (same contract as the containment
    suite): run() reads ``.user_history``, calls ``.compact_after_summary()``
    after each query, and ``get_current_context_tokens()`` calls
    ``.estimated_context_tokens()``."""

    def __init__(self):
        self.user_history = [{"role": "system", "content": "test worker"}]

    def compact_after_summary(self):
        pass

    def estimated_context_tokens(self):
        return 10


class _FakeContainer:
    def __init__(self, cid, name, labels=None, image=""):
        self.id = cid
        self.name = name
        self.labels = dict(labels or {})
        self.image = image
        self.status = "running"


class _FakeContainerManager:
    """Records stop/remove; ``remove()`` also drops the container from the
    listing (emulating docker semantics)."""

    def __init__(self, containers):
        self.containers = list(containers)
        self.stopped = []
        self.removed = []

    def list_containers(self):
        return list(self.containers)

    def stop(self, container, **kwargs):
        self.stopped.append(container)

    def remove(self, container, **kwargs):
        self.removed.append(container)
        if container in self.containers:
            self.containers.remove(container)


# ── helpers ────────────────────────────────────────────────────────────────


def _wait_until(predicate, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture(autouse=True)
def _registry_teardown():
    """Pop registry entries added by tests; stop/join any stragglers."""
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
        t.join(timeout=5)


def _spawn_worker(tmp_path, monkeypatch, name, session_id, container_manager=None):
    monkeypatch.setattr(WorkerThread, "_load_context", lambda self: _FakeContext())
    monkeypatch.setattr(WorkerThread, "_save_context", lambda self: None)
    monkeypatch.setattr(WorkerThread, "_heartbeat_tick", lambda self: None)
    thread = WorkerThread(
        name=name,
        definition={"name": name, "system_prompt": "You are a test worker."},
        agent_config={},
        workspace_dir=tmp_path,
        session_id=session_id,
        container_manager=container_manager,
        max_container_count=4,
        max_token_usage=None,
        max_runtime_s=None,
    )
    with worker_module._registry_lock:
        worker_module._worker_registry[(session_id, name)] = thread
    thread.start()
    assert _wait_until(lambda: thread.status == "ready"), (
        f"worker {name!r} did not reach ready "
        f"(status={thread.status}, error={thread.error})"
    )
    return thread


def _owned_container(cid, name, owner):
    return _FakeContainer(cid, name, {_WORKER_CONTAINER_LABEL: owner})


def _resource_container(cid, name):
    return _FakeContainer(
        cid,
        name,
        {_RESOURCE_CONTAINER_LABEL: "workspace-resource"},
        image="postgres:16",
    )


def _make_docker_blocked_worker(
    tmp_path, monkeypatch, name, session_id, cm, cleanup_called=None
):
    """Spawn a worker whose _run_tool_loop is stuck in a 'DockerCodeRunner'
    call (blocked on ``release``) and whose send_query times out as soon as
    the worker is busy.

    Returns ``(thread, busy_started, release, reply_q)``. The caller is
    responsible for releasing the worker (the real Docker call would fail and
    return once the timeout path stops/removes its container).
    """
    thread = _spawn_worker(
        tmp_path, monkeypatch, name=name, session_id=session_id, container_manager=cm
    )
    busy_started = threading.Event()
    release = threading.Event()
    reply_q = queue.Queue()

    def _blocking_tool_loop(self, query):
        busy_started.set()
        assert release.wait(timeout=15.0), "release never fired"
        self._last_elapsed_val = 0.0
        return json.dumps({"response": "done", "query": query})

    monkeypatch.setattr(WorkerThread, "_run_tool_loop", _blocking_tool_loop)
    thread._agent = object()  # skip real Agent construction in the busy path

    if cleanup_called is not None:
        orig_cleanup = thread._cleanup_worker_containers
        thread._cleanup_worker_containers = lambda: (
            cleanup_called.set(), orig_cleanup(),
        )[1]

    thread._input_queue.put(("q-busy", "long task", reply_q))

    def _timeout(query, timeout=300.0):
        assert busy_started.wait(timeout=5.0), "worker never became busy"
        raise TimeoutError(f"Worker '{name}' did not respond within {timeout}s")

    thread.send_query = _timeout
    return thread, busy_started, release, reply_q


def _fire_release_after_cleanup(cleanup_called, release):
    """Simulate the real-world Docker behaviour: once the timeout path stops
    the worker's container, the in-flight DockerCodeRunner call fails and
    returns, unblocking ``_run_tool_loop`` and letting run() terminate."""
    t = threading.Thread(
        target=lambda: (
            cleanup_called.wait(timeout=10.0),
            release.set(),
        )[1],
        daemon=True,
    )
    t.start()
    return t


# ── tests ──────────────────────────────────────────────────────────────────


def test_sync_worker_docker_blocked_stop_waits_for_exit(tmp_path, monkeypatch):
    """THE guarantee: the sync query call does NOT return while the worker
    thread is still alive. A worker stuck in a DockerCodeRunner call times
    out; the timeout branch stops it, reclaims its containers, and waits for
    the thread to actually exit before returning the envelope."""
    owner = "sess-s1:w1"
    cm = _FakeContainerManager(
        [
            _owned_container("c-owned", "w1-ctr", owner),
            _resource_container("c-res", "tm-res-cache"),
        ]
    )
    cleanup_called = threading.Event()
    thread, busy_started, release, _reply_q = _make_docker_blocked_worker(
        tmp_path, monkeypatch, name="w1", session_id="sess-s1",
        cm=cm, cleanup_called=cleanup_called,
    )

    # The Docker-block simulation: as soon as the timeout path runs the
    # synchronous cleanup (container stopped), the blocked Docker call fails
    # and returns, unblocking the worker's run() loop.
    _fire_release_after_cleanup(cleanup_called, release)

    tool = Worker(action="query", worker_name="w1", worker_query="hello", session_id="sess-s1")
    t0 = time.monotonic()
    envelope = tool._action_query([])
    elapsed = time.monotonic() - t0

    # Envelope arrives only AFTER the thread exited.
    assert elapsed < 5.0, f"envelope took {elapsed:.1f}s"
    assert envelope["error"]
    assert "stopped cooperatively" in envelope["note"]
    assert "Re-spawn" in envelope["note"]
    assert envelope["status"] in ("stopping", "stopped")

    # The stop guarantee: no alive worker thread when the sync call returns.
    assert not thread.is_alive(), "worker thread still alive after sync call returned"
    assert thread.status == "stopped"

    # Containers: owned reclaimed, resource untouched.
    assert [c.id for c in cm.stopped] == ["c-owned"]
    assert [c.id for c in cm.removed] == ["c-owned"]
    assert not any(c.labels.get(_WORKER_CONTAINER_LABEL) == owner for c in cm.containers)
    assert any(c.id == "c-res" for c in cm.containers)
    assert [c.id for c in cm.stopped + cm.removed].count("c-res") == 0


def test_sync_worker_normal_completion_no_early_return(tmp_path, monkeypatch):
    """Normal (non-timeout) reply path is unchanged: the sync call returns
    the response envelope only after the worker actually responded, and the
    worker STAYS alive (async model — it can be queried again)."""
    thread = _spawn_worker(
        tmp_path, monkeypatch, name="w2", session_id="sess-s2", container_manager=None
    )
    responded = threading.Event()

    def _fake_tool_loop(self, query):
        responded.wait(timeout=10.0)  # worker takes time to 'respond'
        self._last_elapsed_val = 0.25
        return "done"  # plain-text reply, like the real agent path

    monkeypatch.setattr(WorkerThread, "_run_tool_loop", _fake_tool_loop)
    thread._agent = object()

    tool = Worker(action="query", worker_name="w2", worker_query="hi", session_id="sess-s2")
    # Delay the reply slightly so the tool call is genuinely blocked waiting.
    def _delayed_reply():
        time.sleep(0.2)
        responded.set()

    threading.Thread(target=_delayed_reply, daemon=True).start()

    t0 = time.monotonic()
    envelope = tool._action_query([])
    elapsed = time.monotonic() - t0

    try:
        # No early return: the envelope carries the actual response, arrived
        # only after the worker produced it.
        assert elapsed >= 0.15, f"returned before the worker responded ({elapsed:.2f}s)"
        assert "response" in envelope
        # run() wraps the reply in an envelope ({"content": ..., "status": ...})
        # and send_query returns that JSON string.
        reply_env = json.loads(envelope["response"])
        assert reply_env.get("content") == "done"
        assert envelope["elapsed_seconds"] is not None

        # Worker stays alive (async model) — no stop, no orphan-free teardown.
        # (status flips back to "ready" in the worker thread right after the
        # reply is emitted, so only liveness is asserted here.)
        assert thread.is_alive(), "normal reply must leave the worker alive"
    finally:
        # Stop the thread while the _save_context monkeypatch is still active
        # (same hygiene as the containment suite) so run()'s finally-block save
        # does not touch the real implementation after teardown reverts it.
        thread.stop()
    assert _wait_until(lambda: not thread.is_alive(), timeout=10.0), "worker thread still alive"


def test_sync_worker_stop_does_not_touch_resource_containers(tmp_path, monkeypatch):
    """Through the full timeout+wait flow, resource containers are never
    stopped or removed — only the worker's own containers are reclaimed."""
    owner = "sess-s3:w3"
    cm = _FakeContainerManager(
        [
            _owned_container("c-owned", "w3-ctr", owner),
            _resource_container("c-res-a", "tm-res-cache"),
            _resource_container("c-res-b", "tm-res-git"),
        ]
    )
    cleanup_called = threading.Event()
    thread, busy_started, release, _reply_q = _make_docker_blocked_worker(
        tmp_path, monkeypatch, name="w3", session_id="sess-s3",
        cm=cm, cleanup_called=cleanup_called,
    )
    _fire_release_after_cleanup(cleanup_called, release)

    tool = Worker(action="query", worker_name="w3", worker_query="hello", session_id="sess-s3")
    envelope = tool._action_query([])
    assert envelope["error"]
    assert not thread.is_alive()

    assert [c.id for c in cm.stopped] == ["c-owned"]
    assert [c.id for c in cm.removed] == ["c-owned"]
    remaining = {c.id for c in cm.containers}
    assert {"c-res-a", "c-res-b"} <= remaining
    assert any(c.name.startswith("tm-res-") for c in cm.containers)


def test_sync_worker_no_orphan_thread_after_return(tmp_path, monkeypatch):
    """After the timeout flow returns, no orphan thread remains: the worker
    thread has exited, its terminal status is visible in the registry, and
    the in-flight query drained to its reply queue (run()'s teardown ran)."""
    owner = "sess-s4:w4"
    cm = _FakeContainerManager([_owned_container("c-owned", "w4-ctr", owner)])
    cleanup_called = threading.Event()
    thread, busy_started, release, reply_q = _make_docker_blocked_worker(
        tmp_path, monkeypatch, name="w4", session_id="sess-s4",
        cm=cm, cleanup_called=cleanup_called,
    )
    _fire_release_after_cleanup(cleanup_called, release)

    tool = Worker(action="query", worker_name="w4", worker_query="hello", session_id="sess-s4")
    envelope = tool._action_query([])
    assert envelope["error"]
    assert not thread.is_alive(), "orphan thread survived the sync call"
    assert thread.status == "stopped"
    with worker_module._registry_lock:
        entry = worker_module._worker_registry.get(("sess-s4", "w4"))
    assert entry is None or entry.status == "stopped"

    # run()'s terminal path ran: the in-flight query drained with a reply.
    assert not reply_q.empty(), "in-flight query never drained to its reply queue"
    env2 = json.loads(reply_q.get_nowait())
    assert json.loads(env2["content"]).get("response") == "done"
