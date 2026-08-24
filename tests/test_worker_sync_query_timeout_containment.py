"""Worker sync-query timeout containment tests.

Verifies the cooperative-stop contract in ``Worker._action_query``
(tools/workspace/worker.py): when a synchronous query times out
(``TimeoutError`` from ``send_query``), the worker thread is STOPPED
COOPERATIVELY (``thread.stop()`` — never ``Thread.kill``), its run()
terminal path executes, and its containers are reclaimed while resource
containers are left untouched.

The default test path is 100% Docker-free: container cleanup is exercised
against a ``_FakeContainerManager`` that records stop/remove calls. The live
Docker variant (``test_live_timeout_reclaims_worker_owned_container``) runs
only when ``WORKER_LIVE_DOCKER=1`` is set AND a docker daemon is reachable.
"""

import json
import os
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

from llm_providers.base import LLMProvider, ProviderConfig, LLMResponse
from llm_providers.factory import ProviderFactory

# ── fakes ──────────────────────────────────────────────────────────────────


class _FakeContext:
    """Minimal stand-in for WorkerContext: run() reads ``.user_history``
    (token accounting / conversation length), calls
    ``.compact_after_summary()`` after each query, and
    ``get_current_context_tokens()`` calls ``.estimated_context_tokens()``
    (worker.py L1045, reached from the unguarded _write_status_file).
    """

    def __init__(self):
        self.user_history = [{"role": "system", "content": "test worker"}]

    def compact_after_summary(self):
        pass

    def estimated_context_tokens(self):
        return 10

    def to_persistable_dict(self):
        return {"user_history": self.user_history}


class _FakeContainer:
    def __init__(self, cid, name, labels=None, image=""):
        self.id = cid
        self.name = name
        self.labels = dict(labels or {})
        self.image = image
        self.status = "running"

class _FakeContainerManager:
    """Records stop/remove; ``remove()`` also drops the container from the
    listing (emulating docker semantics) so the 'no owned container remains'
    assertion is meaningful."""

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


class _FakeDictContainerManager:
    """Real-ContainerManager-shaped fake: ``list_containers()`` returns DICTS
    with exactly the keys the real ``ContainerManager.list_containers()`` emits
    (including ``labels``), so ``_cleanup_worker_containers`` exercises the
    worker.py dict branch (``container.get("labels")`` at L1806-1807 and the
    ``container_id``-target resolution at L1846-1849) — a path the
    object-shaped ``_FakeContainerManager`` never covers."""

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
        self.containers = [
            c for c in self.containers
            if c.get("container_id") != container and c.get("name") != container
        ]


def _owned_container_dict(cid, name, owner):
    """Real-CM-style dict entry for a worker-owned container (copy of the
    exact key set infra/container_manager.py list_containers() emits)."""
    return {
        "container_id": cid,
        "name": name,
        "image": "agent-executor:latest",
        "status": "running",
        "uptime_seconds": 5,
        "workspace_id": "sess-d",
        "note": "",
        "labels": {_WORKER_CONTAINER_LABEL: owner},
    }


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
    """run() does NOT unregister the thread — tests register manually and
    must pop their entries afterwards; stop/join any stragglers so nothing
    leaks into later tests."""
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
        worker_module._worker_registry[(session_id, name, 1)] = thread
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


# ── default (100% Docker-free) tests ───────────────────────────────────────


def test_query_timeout_invokes_cooperative_stop_and_returns_envelope(tmp_path, monkeypatch):
    """The core contract: a timed-out query returns the cooperative-stop
    envelope promptly, the worker reaches terminal 'stopped' state in the
    registry, its containers are reclaimed, and resource containers are
    untouched."""
    owner = "sess-1:w1"
    cm = _FakeContainerManager(
        [
            _owned_container("c-owned", "w1-ctr", owner),
            _resource_container("c-res", "tm-res-pg"),
        ]
    )
    thread = _spawn_worker(
        tmp_path, monkeypatch, name="w1", session_id="sess-1", container_manager=cm
    )

    def _timeout(query, timeout=300.0):
        raise TimeoutError(f"Worker 'w1' did not respond within {timeout}s")

    thread.send_query = _timeout

    tool = Worker(action="query", worker_name="w1", worker_query="hello", session_id="sess-1")
    t0 = time.monotonic()
    envelope = tool._action_query([])
    elapsed = time.monotonic() - t0

    # 1) timeout envelope arrives promptly (~5s budget)
    assert elapsed < 5.0, f"envelope took {elapsed:.1f}s"
    assert envelope["error"]
    assert "stopped cooperatively" in envelope["note"]
    assert "Re-spawn" in envelope["note"]
    assert envelope["worker_name"] == "w1"
    assert envelope["status"] in ("stopping", "stopped")

    # 2) terminal state visible in the registry afterwards
    assert _wait_until(lambda: not thread.is_alive(), timeout=10.0), "worker thread still alive"
    with worker_module._registry_lock:
        assert worker_module._worker_registry[("sess-1", "w1", 1)].status == "stopped"

    # 3) owned container stopped + removed
    assert [c.id for c in cm.stopped] == ["c-owned"]
    assert [c.id for c in cm.removed] == ["c-owned"]
    # 6) no worker-owned (label == owner) container remains
    assert not any(c.labels.get(_WORKER_CONTAINER_LABEL) == owner for c in cm.containers)
    # 4/7) resource container untouched, tm-resource-* unaffected
    assert any(c.id == "c-res" for c in cm.containers)
    assert [c.id for c in cm.stopped + cm.removed].count("c-res") == 0
    assert any(c.name.startswith("tm-res-") for c in cm.containers)


def test_cleanup_reclaims_owned_containers_from_real_cm_style_dicts(tmp_path, monkeypatch):
    """Dict-shape list_containers() entries (what the REAL ContainerManager
    returns — including the ``labels`` key added for worker reclaim) must be
    reclaimable: the entry whose ``thoughtmachine.worker`` label equals the
    owner is stopped+removed, while unlabeled and foreign entries are
    skipped. Locks the worker.py dict branch (L1806-1807 / L1844 /
    L1846-1849) that object-shaped fakes never exercise."""
    owner = "sess-d:w-d"
    cm = _FakeDictContainerManager(
        [
            _owned_container_dict("c-owned", "wd-ctr", owner),
            {
                "container_id": "c-unlabeled", "name": "plain-ctr",
                "image": "agent-executor", "status": "running",
                "uptime_seconds": 5, "workspace_id": "sess-d", "note": "",
                "labels": {},
            },
            {
                "container_id": "c-foreign", "name": "other-ctr",
                "image": "agent-executor", "status": "running",
                "uptime_seconds": 5, "workspace_id": "sess-d", "note": "",
                "labels": {_WORKER_CONTAINER_LABEL: "sess-9:w9"},
            },
        ]
    )
    thread = _spawn_worker(
        tmp_path, monkeypatch, name="w-d", session_id="sess-d", container_manager=cm
    )

    thread.stop()
    assert _wait_until(lambda: not thread.is_alive(), timeout=10.0), "worker thread still alive"
    with worker_module._registry_lock:
        assert worker_module._worker_registry[("sess-d", "w-d", 1)].status == "stopped"

    # only the exact-owner dict entry is reclaimed (targeted by container_id)
    assert cm.stopped == ["c-owned"]
    assert cm.removed == ["c-owned"]
    assert [c["container_id"] for c in cm.containers] == ["c-unlabeled", "c-foreign"]


def test_cooperative_stop_reclaims_worker_containers_only(tmp_path, monkeypatch):
    """Ownership granularity of _cleanup_worker_containers: only the EXACT
    owner-labeled container is reclaimed; resource, foreign (stale label
    value) and unlabeled containers are left untouched."""
    owner = "sess-2:w2"
    cm = _FakeContainerManager(
        [
            _owned_container("c-owned", "w2-ctr", owner),
            _resource_container("c-res", "tm-res-cache"),
            _FakeContainer("c-foreign", "w9-ctr", {_WORKER_CONTAINER_LABEL: "sess-9:w9"}),
            _FakeContainer("c-unlabeled", "plain-ctr"),
        ]
    )
    thread = _spawn_worker(
        tmp_path, monkeypatch, name="w2", session_id="sess-2", container_manager=cm
    )

    thread.stop()
    assert _wait_until(lambda: not thread.is_alive(), timeout=10.0), "worker thread still alive"
    with worker_module._registry_lock:
        assert worker_module._worker_registry[("sess-2", "w2", 1)].status == "stopped"

    assert [c.id for c in cm.stopped] == ["c-owned"]
    assert [c.id for c in cm.removed] == ["c-owned"]
    remaining = {c.id for c in cm.containers}
    assert {"c-res", "c-foreign", "c-unlabeled"} <= remaining
    assert "c-owned" not in remaining
    assert any(c.name.startswith("tm-res-") for c in cm.containers)


def test_stop_during_busy_query_drains_and_cleans_up(tmp_path, monkeypatch):
    """Stop issued while the worker is BUSY: the in-flight query drains to
    completion, then the loop observes the stop signal, terminates, and
    reclaims only the worker's own containers."""
    owner = "sess-3:w3"
    cm = _FakeContainerManager(
        [
            _owned_container("c-owned", "w3-ctr", owner),
            _resource_container("c-res", "tm-res-cache"),
        ]
    )
    thread = _spawn_worker(
        tmp_path, monkeypatch, name="w3", session_id="sess-3", container_manager=cm
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

    # Drive the busy path deterministically: the _action_query -> send_query
    # rewire (incl. its cooperative stop) is covered by
    # test_query_timeout_invokes_cooperative_stop_and_returns_envelope, so this
    # test enqueues the exact 3-tuple the real send_query uses and waits for the
    # worker to reach the busy state before stopping it.
    thread._input_queue.put(("q-busy", "long task", reply_q))
    if not busy_started.wait(timeout=5.0):
        raise AssertionError(
            "worker never became busy: "
            f"status={thread.status!r} alive={thread.is_alive()} "
            f"error={thread.error!r} stop_event={thread._stop_event.is_set()} "
            f"input_queue_qsize={thread._input_queue.qsize()}"
        )
    # The worker must be genuinely busy (inside _run_tool_loop) when the
    # cooperative stop fires.
    assert thread.status == "busy"
    assert thread.is_alive()

    thread.stop()  # cooperative stop while busy
    assert thread.status == "stopping"
    assert thread._stop_event.is_set()

    # The in-flight query drains to completion AFTER the stop signal, then the
    # loop observes the stop event and terminates via its own run() teardown.
    release.set()
    assert _wait_until(lambda: not thread.is_alive(), timeout=10.0), "worker thread still alive"
    assert thread.status == "stopped"
    with worker_module._registry_lock:
        entry = worker_module._worker_registry.get(("sess-3", "w3", 1))
    assert entry is None or entry.status == "stopped"
    assert not reply_q.empty(), "in-flight query never drained to its reply queue"
    envelope = json.loads(reply_q.get_nowait())
    assert json.loads(envelope["content"]).get("response") == "done"

    assert [c.id for c in cm.stopped] == ["c-owned"]
    assert [c.id for c in cm.removed] == ["c-owned"]
    assert any(c.id == "c-res" for c in cm.containers)
    assert [c.id for c in cm.stopped + cm.removed].count("c-res") == 0
    assert not any(c.labels.get(_WORKER_CONTAINER_LABEL) == owner for c in cm.containers)


def test_query_timeout_while_busy_reclaims_containers_synchronously(tmp_path, monkeypatch):
    """Query timeout while the worker is GENUINELY BUSY inside _run_tool_loop
    (stuck in a DockerCodeRunner call): the timeout branch must reclaim
    worker-owned containers SYNCHRONOUSLY (thread._cleanup_worker_containers)
    right after thread.stop(), not merely via run()'s finally safety net."""
    owner = "sess-4:w4"
    cm = _FakeContainerManager(
        [
            _owned_container("c-owned", "w4-ctr", owner),
            _resource_container("c-res", "tm-res-cache"),
        ]
    )
    thread = _spawn_worker(
        tmp_path, monkeypatch, name="w4", session_id="sess-4", container_manager=cm
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

    # Enqueue the real send_query 3-tuple so run() treats it as a genuine query.
    thread._input_queue.put(("q-busy", "long task", reply_q))

    # Recorders proving DIRECT invocation from the timeout branch.
    stop_calls = []
    orig_stop = thread.stop
    thread.stop = lambda: (stop_calls.append(True), orig_stop())[1]
    cleanup_calls = []
    orig_cleanup = thread._cleanup_worker_containers
    thread._cleanup_worker_containers = lambda: (cleanup_calls.append(True), orig_cleanup())[1]

    # Fake send_query: wait until the worker is genuinely busy, THEN time out.
    def _timeout(query, timeout=300.0):
        assert busy_started.wait(timeout=5.0), "worker never became busy"
        raise TimeoutError(f"Worker 'w4' did not respond within {timeout}s")

    thread.send_query = _timeout

    tool = Worker(action="query", worker_name="w4", worker_query="hello", session_id="sess-4")
    t0 = time.monotonic()
    envelope = tool._action_query([])
    elapsed = time.monotonic() - t0

    # (1) Envelope: prompt cooperative-stop return, no join.
    assert elapsed < 5.0
    assert envelope["error"]
    assert "stopped cooperatively" in envelope["note"]
    assert "Re-spawn" in envelope["note"]
    assert envelope["status"] in ("stopping", "stopped")
    assert envelope["worker_name"] == "w4"

    # (2) thread.stop() invoked from the timeout branch.
    assert stop_calls == [True]

    # (3) _cleanup_worker_containers() invoked DIRECTLY from the timeout branch.
    assert cleanup_calls == [True]

    # (4) IMMEDIATE reclaim while the worker is STILL busy inside _run_tool_loop.
    assert thread.is_alive(), "worker must still be inside _run_tool_loop"
    assert [c.id for c in cm.stopped] == ["c-owned"]
    assert [c.id for c in cm.removed] == ["c-owned"]
    assert not any(c.labels.get(_WORKER_CONTAINER_LABEL) == owner for c in cm.containers)

    # (5) Resource container untouched.
    assert any(c.id == "c-res" for c in cm.containers)
    assert [c.id for c in cm.stopped + cm.removed].count("c-res") == 0

    # (6) Terminal path: the in-flight query still drains, then run() exits.
    release.set()
    assert _wait_until(lambda: not thread.is_alive(), timeout=10.0), "worker thread still alive"
    assert thread.status == "stopped"
    with worker_module._registry_lock:
        entry = worker_module._worker_registry.get(("sess-4", "w4", 1))
    assert entry is None or entry.status == "stopped"
    assert not reply_q.empty(), "in-flight query never drained to its reply queue"
    env2 = json.loads(reply_q.get_nowait())
    assert json.loads(env2["content"]).get("response") == "done"


def test_no_thread_kill_in_worker_module():
    """The cooperative-stop contract forbids Thread.kill anywhere in
    worker.py — the thread must always terminate via its own run() path."""
    src = Path(worker_module.__file__).read_text(encoding="utf-8")
    forbidden = [
        "Thread.kill(",
        "thread.kill(",
        "self.kill(",
        "_thread.kill(",
        "current_thread().kill(",
        "._kill(",
    ]
    for lineno, line in enumerate(src.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue  # comments may discuss the prohibition; calls cannot
        for token in forbidden:
            assert token not in stripped, f"{worker_module.__file__}:{lineno}: {line}"


# ── live docker variant (opt-in, skipif-guarded) ───────────────────────────


@pytest.mark.skipif(
    not os.environ.get("WORKER_LIVE_DOCKER"),
    reason="live docker test: set WORKER_LIVE_DOCKER=1 to enable",
)
def test_live_timeout_reclaims_worker_owned_container(tmp_path, monkeypatch):
    """End-to-end variant against a real docker daemon: a real worker-owned
    container and a real resource container are created, the timeout flow
    runs, and the owned container is actually stopped+removed while the
    resource container survives. Operator evidence: docker ps before/after."""
    docker = pytest.importorskip("docker")
    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:  # daemon missing/offline
        pytest.skip(f"docker daemon unavailable: {exc}")

    owner = "sess-live:wlive"
    stamp = int(time.time())
    owned = client.containers.run(
        "alpine:3.19",
        ["sleep", "300"],
        detach=True,
        name=f"tm-wlive-{stamp}",
        labels={_WORKER_CONTAINER_LABEL: owner, "thoughtmachine.workspace_id": "live-test"},
    )
    resource = client.containers.run(
        "alpine:3.19",
        ["sleep", "300"],
        detach=True,
        name=f"tm-res-live-{stamp}",
        labels={_RESOURCE_CONTAINER_LABEL: "live-test"},
    )
    try:
        class _LiveCM:
            def list_containers(self):
                return client.containers.list(all=True)

            def stop(self, container):
                container.stop(timeout=5)

            def remove(self, container):
                container.remove(force=True)

        thread = _spawn_worker(
            tmp_path,
            monkeypatch,
            name="wlive",
            session_id="sess-live",
            container_manager=_LiveCM(),
        )

        def _timeout(query, timeout=300.0):
            raise TimeoutError(f"Worker 'wlive' did not respond within {timeout}s")

        thread.send_query = _timeout
        tool = Worker(action="query", worker_name="wlive", worker_query="hi", session_id="sess-live")
        envelope = tool._action_query([])
        assert "stopped cooperatively" in envelope["note"]

        assert _wait_until(lambda: not thread.is_alive(), timeout=15.0), "worker thread still alive"
        # The real docker container this worker owns is gone...
        assert not any(c.name == owned.name for c in client.containers.list(all=True)), (
            f"worker-owned container {owned.name} still exists"
        )
        # ...while the resource container is untouched.
        assert any(c.name == resource.name for c in client.containers.list(all=True)), (
            f"resource container {resource.name} was removed"
        )
    finally:
        for c in (owned, resource):
            try:
                c.remove(force=True)
            except Exception:
                pass


# ── live docker variant: REAL ContainerManager + REAL query protocol ────────


class _LiveMockProvider(LLMProvider):
    """Scripted mock LLM for the real-docker test.

    Call 1 returns a DockerCodeRunner tool call so the worker's real Agent
    actually executes a long-running docker command (sleep 60) in a fresh
    container stamped with the worker's ownership label; later calls return a
    plain content response. The mock provider requires an api_key
    (llm_providers/factory.py resolves it from config or the MOCK_API_KEY env
    var, else raises InvalidConfigError)."""

    def __init__(self, config: ProviderConfig):
        self._config = config
        self._call_count = 0

    def chat_completion(self, messages, tools=None, **kwargs):
        self._call_count += 1
        usage = {"prompt_tokens": 10, "completion_tokens": 5}
        if self._call_count == 1:
            return LLMResponse(
                content="",
                reasoning="mock reasoning",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "DockerCodeRunner",
                            "arguments": json.dumps(
                                {
                                    "command": "sleep 60",
                                    "timeout": 120,
                                    "image": "alpine:3.19",
                                }
                            ),
                        },
                    }
                ],
                usage=usage,
                provider="mock",
                model="mock-model",
            )
        return LLMResponse(
            content="done",
            reasoning="mock reasoning",
            tool_calls=None,
            usage=usage,
            provider="mock",
            model="mock-model",
        )

    def count_tokens(self, text: str) -> int:
        return 42


def _register_mock_provider():
    if "mock" not in ProviderFactory._get_providers():
        ProviderFactory.register_provider("mock", _LiveMockProvider)


class _LiveTimeoutWorker(WorkerThread):
    """WorkerThread whose send_query shims the hardcoded 300s query timeout
    down to 5s. Everything else stays real: the wire protocol (enqueue +
    block on the reply queue, raise TimeoutError) and the docker tool path."""

    def send_query(self, query, timeout=300.0):
        return super().send_query(query, timeout=5.0)


@pytest.mark.skipif(
    not os.environ.get("WORKER_LIVE_DOCKER"),
    reason="live docker test: set WORKER_LIVE_DOCKER=1 to enable",
)
def test_live_sync_query_timeout_reclaims_runner_container(tmp_path, monkeypatch):
    """End-to-end against a real docker daemon exercising the REAL worker
    query protocol and the REAL ContainerManager: the worker's real Agent
    issues a DockerCodeRunner call (long sleep) whose container is stamped
    thoughtmachine.worker='sess-live:wlive'; the sync query times out (5s
    shim), the cooperative-stop branch stops the thread and reclaims the
    runner's container via the real ContainerManager, while a resource
    container survives. Operator evidence: docker ps before/after."""
    docker = pytest.importorskip("docker")
    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:  # daemon missing/offline
        pytest.skip(f"docker daemon unavailable: {exc}")

    # Lazy import: infra.container_manager triggers a circular-import cascade
    # if imported at module top (same pattern as tests/docker/*).
    from infra.container_manager import ContainerManager

    client.images.pull("alpine:3.19")
    stamp = int(time.time())
    owner = "sess-live:wlive"
    resource_name = f"tm-live-res-{stamp}"
    resource = client.containers.run(
        "alpine:3.19",
        ["sleep", "300"],
        detach=True,
        name=resource_name,
        labels={_RESOURCE_CONTAINER_LABEL: "live-test"},
    )

    def _ps_evidence(tag):
        # Operator evidence, FILTERED to this test's containers only: the
        # worker-owned runner (thoughtmachine.worker label), the resource
        # container (thoughtmachine.resource label), and the resource matched
        # by name (tm-live-res-<stamp>). Everything else on the host is noise.
        print(f"\n[{tag}] relevant containers:")
        for c in client.containers.list(all=True):
            labels = dict(c.labels)
            if (
                c.name == resource_name
                or _WORKER_CONTAINER_LABEL in labels
                or _RESOURCE_CONTAINER_LABEL in labels
            ):
                tm_labels = {
                    k: v for k, v in labels.items() if k.startswith("thoughtmachine.")
                }
                print(
                    f"  {c.id[:12]} name={c.name!r} status={c.status} labels={tm_labels}"
                )

    old_api_key = os.environ.get("MOCK_API_KEY")
    os.environ["MOCK_API_KEY"] = "test-key"  # mock provider requires api_key
    _register_mock_provider()
    thread = None
    try:
        _ps_evidence("docker ps BEFORE")

        # Real manager: workspace_id resolves from workspace_path, matching the
        # manager DockerCodeRunner builds internally for the same workspace.
        cm = ContainerManager(
            workspace_path=str(tmp_path),
            session_id="sess-live",
            image="alpine:3.19",
            mem_limit="1g",
            cpu_quota=100000,
        )

        # Do NOT patch _load_context here: this live test builds a REAL Agent,
        # which requires a full WorkerContext. The minimal _FakeContext used by
        # the docker-free tests would crash with AttributeError: '_FakeContext'
        # object has no attribute 'session_id' (Agent.__init__ reads
        # session.session_id at agent/core/agent.py:90, user_history at :91,
        # and total_input/output_tokens at :126). With a fresh tmp_path the
        # real loader finds no context.json, returns None, and run() falls
        # back to a REAL WorkerContext (agent/core/worker_context.py) which
        # supplies session_id/user_history/total_input_tokens/
        # total_output_tokens/_on_conversation_changed etc. so Agent
        # construction succeeds and the thread reaches 'ready'.
        monkeypatch.setattr(WorkerThread, "_save_context", lambda self: None)
        monkeypatch.setattr(WorkerThread, "_heartbeat_tick", lambda self: None)
        thread = _LiveTimeoutWorker(
            name="wlive",
            definition={"name": "wlive", "system_prompt": "You are a test worker."},
            agent_config={
                "provider": "mock",
                "model": "mock-model",
                "api_key": "test-key",  # forwarded to AgentConfig.api_key
                "enabled_tools": ["DockerCodeRunner"],
            },
            workspace_dir=tmp_path,
            session_id="sess-live",
            # Session exposes the categories DockerCodeRunner requires
            # (filesystem:write + container:true, docker_code_runner.py:56).
            # Values must be valid AgentConfig SessionPermissions literals
            # (thoughtmachine/security.py): network in {banned, ask, write,
            # outbound}, execution in {banned, read, write, full, ask}.
            session_permissions={
                "filesystem": "write",
                "container": True,
                "network": "write",
                "execution": "full",
            },
            project_root=str(tmp_path),
            container_manager=cm,
            timeout_seconds=120,
        )
        with worker_module._registry_lock:
            worker_module._worker_registry[("sess-live", "wlive", 1)] = thread
        thread.start()
        assert _wait_until(lambda: thread.status == "ready", timeout=20.0), (
            f"worker did not reach ready (status={thread.status}, error={thread.error})"
        )

        tool = Worker(
            action="query",
            worker_name="wlive",
            worker_query="run the long docker command",
            session_id="sess-live",
        )
        t0 = time.monotonic()
        envelope = tool._action_query([])
        elapsed = time.monotonic() - t0

        # (1) cooperative-stop envelope arrives promptly after the 5s shim
        assert 4.0 <= elapsed <= 25.0, f"elapsed={elapsed:.1f}s"
        assert envelope.get("error"), envelope
        note = envelope.get("note", "")
        assert "stopped cooperatively" in note, note
        assert "Re-spawn" in note, note
        assert envelope["status"] in ("stopping", "stopped")

        # (2) terminal state reached via run()'s own teardown
        assert _wait_until(lambda: not thread.is_alive(), timeout=30.0), (
            "worker thread still alive"
        )
        assert thread.status == "stopped"

        # (3) the runner's real container (owner-labeled) was reclaimed
        leftovers = [
            c
            for c in client.containers.list(all=True)
            if c.labels.get(_WORKER_CONTAINER_LABEL) == owner
        ]
        assert not leftovers, (
            f"owner-labeled containers still exist: {[c.name for c in leftovers]}"
        )

        # (4) resource container survives
        assert any(c.name == resource_name for c in client.containers.list(all=True)), (
            f"resource container {resource_name} was removed"
        )
        _ps_evidence("docker ps AFTER")
    finally:
        try:
            resource.remove(force=True)
        except Exception:
            pass
        try:
            for c in client.containers.list(all=True):
                if c.labels.get(_WORKER_CONTAINER_LABEL) == owner:
                    try:
                        c.remove(force=True)
                    except Exception:
                        pass
        except Exception:
            pass
        with worker_module._registry_lock:
            worker_module._worker_registry.pop(("sess-live", "wlive", 1), None)
        if thread is not None and thread.is_alive():
            try:
                thread.stop()
            except Exception:
                pass
            thread.join(timeout=5)
        if old_api_key is None:
            os.environ.pop("MOCK_API_KEY", None)
        else:
            os.environ["MOCK_API_KEY"] = old_api_key
