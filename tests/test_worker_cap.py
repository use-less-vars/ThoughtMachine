"""Per-session worker spawn cap + continue-existing routing + reset action tests.

Covers:
- the session-level spawn cap (``MAX_WORKERS_PER_SESSION`` default 3,
  configurable per session via ``max_workers_per_session`` top-level or
  nested under ``session_config``; legacy ``max_workers`` honoured);
- continue-existing routing in ``_action_spawn`` (a live, non-paused worker
  is reused with ``spawned=False`` instead of spawning a duplicate);
- the ``reset`` action (cooperative stop + persisted state-file drop +
  spawn-slot release, without touching sibling workers or resource
  containers).

100% Docker-free: container reclaim is exercised against the same
``_FakeContainerManager`` used by ``test_worker_sync_query_timeout_containment``.
"""

import json

import pytest

from tools.workspace import worker as worker_module
from tools.workspace.worker import Worker

from tests.test_worker_sync_query_timeout_containment import (
    _FakeContainerManager,
    _owned_container,
    _resource_container,
    _spawn_worker,
    _wait_until,
)

SID = "sess-cap-1"


def _def(name):
    return {"name": name, "system_prompt": "You are a test worker."}


@pytest.fixture(autouse=True)
def _registry_teardown():
    """Pop any registry entries this test added; stop/join stragglers.

    Copied (not imported) so autouse registration is guaranteed: run() does
    NOT unregister the thread, so tests register manually and must pop their
    entries afterwards.
    """
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


def _spawn_three(tmp_path, monkeypatch):
    threads = {}
    for name in ("w1", "w2", "w3"):
        threads[name] = _spawn_worker(tmp_path, monkeypatch, name=name, session_id=SID)
    return threads


def test_cap_enforcement_blocks_fourth_worker(tmp_path, monkeypatch):
    """At the default cap (3), a 4th spawn is refused with a clear error."""
    _spawn_three(tmp_path, monkeypatch)
    result = Worker(action="spawn", worker_name="w4", session_id=SID)._action_spawn(
        workers=[_def("w4")], ws_id=None
    )
    assert "spawn limit reached" in result["error"]
    assert result["max_workers"] == 3
    assert result["live_workers"] == 3


def test_continue_existing_reuses_worker(tmp_path, monkeypatch):
    """spawn on a live (non-paused) worker reuses the instance instead of
    creating a duplicate; the cap is never double-counted."""
    thread = _spawn_worker(tmp_path, monkeypatch, name="w1", session_id=SID)
    with worker_module._registry_lock:
        keys_before = set(worker_module._worker_registry.keys())

    result = Worker(action="spawn", worker_name="w1", session_id=SID)._action_spawn(
        workers=[_def("w1")], ws_id=None
    )
    assert result["spawned"] is False
    assert result["instance_id"] == 1
    assert result["status"] == thread.status
    assert "reusing" in result["message"].lower()
    # No new registry entry was created for the reuse.
    with worker_module._registry_lock:
        assert set(worker_module._worker_registry.keys()) == keys_before
    assert thread.is_alive()


def test_reset_worker_stops_and_frees_slot(tmp_path, monkeypatch):
    """reset stops the worker, drops its persisted context files, and frees
    the spawn slot so a new worker can be spawned."""
    threads = _spawn_three(tmp_path, monkeypatch)
    target = threads["w2"]
    # Seed a stale persisted context; reset must delete it.
    target._worker_dir.mkdir(exist_ok=True)
    ctx_file = target._worker_dir / "context.json"
    ctx_file.write_text(
        json.dumps({"user_history": [{"role": "system", "content": "stale"}]}),
        encoding="utf-8",
    )

    result = Worker(action="reset", worker_name="w2", session_id=SID)._action_reset(
        workers=[_def("w2")]
    )
    assert result["status"] == "reset"
    assert result["freed_slot"] is True
    assert result["live_workers"] == 2
    assert not ctx_file.exists()
    with worker_module._registry_lock:
        assert (SID, "w2", 1) not in worker_module._worker_registry
    assert not target.is_alive()

    # The freed slot can be reused: a 4th worker spawns fine now.
    _spawn_worker(tmp_path, monkeypatch, name="w4", session_id=SID)
    with worker_module._registry_lock:
        live = [
            t for key, t in worker_module._worker_registry.items()
            if key[0] == SID and t.is_alive()
        ]
    assert len(live) == 3


def test_reset_does_not_touch_other_workers_or_resources(tmp_path, monkeypatch):
    """reset reclaims ONLY the target worker's own container; sibling worker
    containers and shared resource containers are left untouched."""
    cm = _FakeContainerManager(
        [
            _owned_container("c1", "w1-container", f"{SID}:w1"),
            _owned_container("c2", "w2-container", f"{SID}:w2"),
            _resource_container("rc1", "pg-resource"),
        ]
    )
    w1 = _spawn_worker(
        tmp_path, monkeypatch, name="w1", session_id=SID, container_manager=cm
    )
    w2 = _spawn_worker(
        tmp_path, monkeypatch, name="w2", session_id=SID, container_manager=cm
    )

    result = Worker(action="reset", worker_name="w1", session_id=SID)._action_reset(
        workers=[_def("w1")]
    )
    assert result["status"] == "reset"
    assert _wait_until(lambda: not w1.is_alive(), timeout=10.0)
    assert w2.is_alive()
    assert [c.id for c in cm.stopped] == ["c1"]
    assert [c.id for c in cm.removed] == ["c1"]
    assert [c.id for c in cm.containers] == ["c2", "rc1"]
    with worker_module._registry_lock:
        assert (SID, "w2", 1) in worker_module._worker_registry
        assert (SID, "w1", 1) not in worker_module._worker_registry


def test_default_cap_is_three(tmp_path, monkeypatch):
    """The module default is exactly 3 and it is what an unconfigured spawn
    enforces."""
    assert worker_module.MAX_WORKERS_PER_SESSION == 3
    _spawn_three(tmp_path, monkeypatch)
    result = Worker(action="spawn", worker_name="w4", session_id=SID)._action_spawn(
        workers=[_def("w4")], ws_id=None
    )
    assert "spawn limit reached" in result["error"]
    assert result["max_workers"] == 3
    assert result["live_workers"] == 3


def test_user_override_cap_allows_more_workers(tmp_path, monkeypatch):
    """A per-session override raises the cap: 4 live workers are allowed when
    ``max_workers_per_session`` is 5 (both top-level and nested shapes)."""
    _spawn_three(tmp_path, monkeypatch)

    # Top-level override: passes the cap check, fails only later on the
    # missing workspace dir (proving the cap was not the blocker).
    result = Worker(
        action="spawn",
        worker_name="w4",
        session_id=SID,
        agent_config={"max_workers_per_session": 5},
    )._action_spawn(workers=[_def("w4")], ws_id=None)
    assert "spawn limit reached" not in result.get("error", "")
    assert result.get("error") == "Cannot create worker: no workspace directory resolved."

    # Nested session_config shape is honoured too.
    result2 = Worker(
        action="spawn",
        worker_name="w5",
        session_id=SID,
        agent_config={"session_config": {"max_workers_per_session": 5}},
    )._action_spawn(workers=[_def("w5")], ws_id=None)
    assert "spawn limit reached" not in result2.get("error", "")
    assert result2.get("error") == "Cannot create worker: no workspace directory resolved."
