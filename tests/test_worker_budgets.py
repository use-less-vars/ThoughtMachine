"""Tests for per-worker resource budgets (Phase 3, item 6).

Covers:
  1. default budgets applied when nothing is configured (containers=4,
     tokens=runtime=unlimited — existing behaviour unchanged);
  2. the per-worker container budget is enforced in
     ``WorkerSupervisor.request_container`` (fail closed) and freed by
     ``release_container``;
  3. the token budget fails closed via ``WorkerThread._budget_check()``;
  4. the runtime budget fails closed via ``WorkerThread._budget_check()``;
  5. budgets resolve from the nested ``session_config`` (and explicit
     constructor params win over session config);
  6. under-budget workers are unaffected (the budget check returns None);
  7. defaults are generous / existing behaviour unchanged (case 1 plus the
     regression suite run separately).

The fakes are hermetic: no Agent is ever constructed (so nothing exposes a
``state`` attribute) and the WorkerThread budgets are exercised by calling
``_budget_check()`` directly with ``_cached_context_tokens`` seeded.
"""

from __future__ import annotations

import time

import pytest

from infra.workspace_lifecycle_manager import (
    DEFAULT_MAX_CONTAINERS,
    WorkerSupervisor,
)
from tools.workspace.worker import (
    WORKER_DEFAULT_MAX_CONTAINERS,
    WORKER_DEFAULT_MAX_RUNTIME_S,
    WORKER_DEFAULT_MAX_TOKENS,
    WorkerThread,
)


class FakeCM:
    """Legacy ContainerManager double: start/stop only, tracks requests."""

    def __init__(self):
        self.session_config = {}
        self.started = []
        self.stopped = []

    def start(self, image=None, name=None, note=None):
        self.started.append({"image": image, "name": name, "note": note})
        return {"id": f"c{len(self.started)}", "name": name or "agent-exec-x",
                "status": "created", "note": ""}

    def stop(self, container_id):
        self.stopped.append(container_id)
        return {"status": "stopped", "container_id": container_id}


def make_worker(tmp_path, **kwargs):
    """Build a WorkerThread with hermetic defaults (no Agent is constructed)."""
    agent_config = kwargs.pop(
        "agent_config", {"provider": "openai", "model": "gpt-4"}
    )
    return WorkerThread(
        name=kwargs.pop("name", "budget-w"),
        definition={},
        agent_config=agent_config,
        workspace_dir=tmp_path,
        session_permissions={},
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1 + 7. Defaults applied / defaults generous (existing behaviour unchanged)
# ---------------------------------------------------------------------------

def test_defaults_applied_and_generous(tmp_path):
    worker = make_worker(tmp_path)
    assert worker._max_container_count == WORKER_DEFAULT_MAX_CONTAINERS == 4
    assert worker._max_token_usage is WORKER_DEFAULT_MAX_TOKENS is None
    assert worker._max_runtime_s is WORKER_DEFAULT_MAX_RUNTIME_S is None
    assert worker._budget_started_at > 0
    # With no budgets configured, the check always passes.
    assert worker._budget_check() is None


# ---------------------------------------------------------------------------
# 2. Container budget enforced in WorkerSupervisor.request_container
# ---------------------------------------------------------------------------

def test_container_budget_enforced():
    cm = FakeCM()
    sup = WorkerSupervisor(
        "w1", cm, None, feature_flag_check=lambda: True, max_container_count=2
    )
    assert sup._max_container_count == 2

    r1 = sup.request_container({"image": "python:3.12", "name": "b1"})
    r2 = sup.request_container({"image": "python:3.12", "name": "b2"})
    assert r1["id"] == "c1" and r2["id"] == "c2"
    assert sup._active_container_ids == {"c1", "c2"}

    # 3rd request fails closed BEFORE the manager is called.
    with pytest.raises(RuntimeError, match="limit reached"):
        sup.request_container({"image": "python:3.12", "name": "b3"})
    assert len(cm.started) == 2

    # Releasing a container frees its budget slot.
    sup.release_container("c1")
    assert sup._active_container_ids == {"c2"}
    r3 = sup.request_container({"image": "python:3.12", "name": "b3"})
    assert r3["id"] == "c3"
    assert len(cm.started) == 3

    # Unknown / duplicate releases are idempotent.
    sup.release_container("c1")
    sup.release_container("does-not-exist")
    assert sup._active_container_ids == {"c2", "c3"}


def test_supervisor_default_container_budget_matches_module_default():
    sup = WorkerSupervisor("w-dflt", FakeCM(), None, feature_flag_check=lambda: True)
    assert sup._max_container_count == DEFAULT_MAX_CONTAINERS == 4


# ---------------------------------------------------------------------------
# 3. Token budget exceeded → fail closed
# ---------------------------------------------------------------------------

def test_token_budget_exceeded_fails_closed(tmp_path):
    worker = make_worker(tmp_path, max_token_usage=100)
    worker._cached_context_tokens = 150
    payload = worker._budget_check()
    assert payload is not None
    assert payload["reason"] == "token_budget"
    assert "token" in payload["error"].lower()


# ---------------------------------------------------------------------------
# 4. Runtime budget exceeded → fail closed
# ---------------------------------------------------------------------------

def test_runtime_budget_exceeded_fails_closed(tmp_path):
    worker = make_worker(tmp_path, max_runtime_s=0)
    worker._budget_started_at = time.monotonic() - 100
    payload = worker._budget_check()
    assert payload is not None
    assert payload["reason"] == "runtime_budget"
    assert "runtime" in payload["error"].lower()


# ---------------------------------------------------------------------------
# 5. Budgets resolve from session_config; explicit params win
# ---------------------------------------------------------------------------

def test_budgets_from_session_config(tmp_path):
    session_config = {
        "container_limits": {"max_containers": 2},
        "max_token_usage": 500,
        "max_runtime_s": 60,
    }
    worker = make_worker(
        tmp_path,
        agent_config={"provider": "openai", "model": "gpt-4",
                      "session_config": session_config},
    )
    assert worker._max_container_count == 2
    assert worker._max_token_usage == 500
    assert worker._max_runtime_s == 60

    # Explicit constructor params take precedence over session config.
    worker2 = make_worker(
        tmp_path,
        name="budget-w2",
        agent_config={"provider": "openai", "model": "gpt-4",
                      "session_config": session_config},
        max_container_count=7,
        max_token_usage=700,
    )
    assert worker2._max_container_count == 7
    assert worker2._max_token_usage == 700
    assert worker2._max_runtime_s == 60


# ---------------------------------------------------------------------------
# 6. Under-budget workers are unaffected
# ---------------------------------------------------------------------------

def test_under_budget_unaffected(tmp_path):
    worker = make_worker(tmp_path, max_token_usage=100, max_runtime_s=60)
    worker._cached_context_tokens = 50
    assert worker._budget_check() is None
