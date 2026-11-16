"""A ``None`` from the worktree resolver must not block a worker from booting.

The property pinned here is the one that was mistakenly believed to be broken:
``resolve_worker_worktree_root`` is FAIL-OPEN (``tools/workspace/worktree_defaults.py``
logs and returns ``None`` on every unprovable precondition -- no host grant, no
git, ``git worktree add`` exited non-zero, ...).  That ``None`` reaches exactly
one branch, ``worker_thread._build_agent_config``'s ``if _default_worktree_root:``,
so ``worktree_root`` is simply left unset and the worker runs on the fallback
``workspace_path`` (the project root).  Nothing on that path raises, and it is
reached only from ``run()`` -- on the worker's first query, i.e. long after the
spawn side returned.

Covered here:

  * ``_build_agent_config`` still returns a config, with the ``workspace_path``
    fallback and no ``worktree_root``;
  * the worker body still boots: it registers its per-worker event bus and
    publishes ``WORKER_SPAWNED`` (the event the bridge subscribes on).  The
    query loop is never entered (``_stop_event`` is pre-set), so there is no
    LLM call, no tool call, no network and no container;
  * a failing ``WORKER_SPAWNED`` publish is now VISIBLE: the publish is wrapped
    in ``try/except`` because it must never take the worker down, and that
    ``except`` logs a WARNING instead of swallowing the failure silently --
    which is why a production log without the string ``WORKER_SPAWNED`` used to
    prove nothing at all.

Note on the patch target: the resolver is imported INSIDE
``_build_agent_config`` (``from tools.workspace.worktree_defaults import
resolve_worker_worktree_root``), so ``tools.workspace.worker_thread`` has no
module-level ``resolve_worker_worktree_root`` attribute to patch.  The
reference resolved at call time is the attribute on
``tools.workspace.worktree_defaults``, and that is what is patched.

Hermetic: a real but never-started ``WorkerThread`` against pytest ``tmp_path``.
"""

from __future__ import annotations

import logging
from unittest import mock

import pytest

from agent.events import EventType
from tools.workspace.worker_registry import WorkerRegistry
from tools.workspace.worker_thread import WorkerThread

SESSION = "sess-worktree-failopen"
WORKER = "failopen-worker"
PARENT_CFG = {"provider": "openai", "model": "gpt-4o"}

#: Where the symbol is actually resolved: ``_build_agent_config`` imports it
#: lazily, so the defining module's attribute is the one read at call time.
RESOLVER = "tools.workspace.worktree_defaults.resolve_worker_worktree_root"


@pytest.fixture
def registry_snapshot():
    """Snapshot/restore the real ``WorkerRegistry`` singleton.

    Same house pattern as ``tests/test_worker_force_spawn_unstarted_thread.py``.
    """
    registry = WorkerRegistry.get_instance()
    with registry._registry_lock:
        old_workers = dict(registry._worker_registry)
        registry._worker_registry.clear()
    with registry._bus_registry_lock:
        old_buses = dict(registry._worker_event_bus_registry)
        registry._worker_event_bus_registry.clear()
    yield registry
    with registry._registry_lock:
        registry._worker_registry.clear()
        registry._worker_registry.update(old_workers)
    with registry._bus_registry_lock:
        registry._worker_event_bus_registry.clear()
        registry._worker_event_bus_registry.update(old_buses)


@pytest.fixture
def project_root(tmp_path):
    """The workspace root the worker falls back to when no worktree is made."""
    root = tmp_path / "project-root"
    root.mkdir()
    return root


@pytest.fixture
def resolver_calls(monkeypatch):
    """Force the worktree resolver to fail open, recording that it ran."""
    calls = []

    def _resolver(workspace_root, session_id, **kwargs):
        calls.append((workspace_root, session_id, kwargs))
        return None

    monkeypatch.setattr(RESOLVER, _resolver)
    return calls


def _thread(tmp_path, project_root):
    return WorkerThread(
        name=WORKER,
        definition={"system_prompt": "fail-open fixture"},
        agent_config=dict(PARENT_CFG),
        workspace_dir=tmp_path,
        session_permissions={},
        project_root=str(project_root),
        session_id=SESSION,
    )


class _RecordingBus:
    """Global event bus stand-in that records what the worker publishes."""

    def __init__(self, registry):
        self.registry = registry
        self.events = []
        self.bus_at_spawn_publish = None

    def publish(self, event):
        self.events.append(event)
        if self.bus_at_spawn_publish is None and (
            getattr(event, "type", None) is EventType.WORKER_SPAWNED
        ):
            # Snapshot the per-worker bus while the worker is alive: run()'s
            # finally unregisters it on the way out.
            self.bus_at_spawn_publish = self.registry.get_event_bus(
                SESSION, WORKER, 1
            )


class _BoomBus:
    """Global event bus stand-in whose publish always fails."""

    def publish(self, event):
        raise RuntimeError("event bus unavailable")


def test_resolver_none_still_returns_agent_config(
    tmp_path, project_root, resolver_calls
):
    thread = _thread(tmp_path, project_root)

    cfg = thread._build_agent_config()

    assert cfg is not None, "a failed worktree bootstrap must not cost the config"
    # The fallback root, unchanged from before the worktree feature.
    assert cfg.workspace_path == str(project_root)
    # Worktree root absent -> the config field keeps its default.
    assert cfg.worktree_root is None
    assert resolver_calls, "the resolver must actually have been consulted"


def test_resolver_none_does_not_prevent_spawn(
    tmp_path, project_root, registry_snapshot, resolver_calls
):
    """The worker body boots and publishes WORKER_SPAWNED despite the ``None``.

    Scope note: the resolver is reachable ONLY from ``run()`` -- its single call
    site is ``_build_agent_config``, which ``run()`` invokes when the first query
    is dequeued.  ``Worker._action_spawn`` therefore cannot be affected by it and
    is not driven here.  What is asserted is the thing the operator's hypothesis
    claimed was lost -- the boot of the worker body, its bus registration and its
    WORKER_SPAWNED publish -- driven through the real ``run()`` up to and
    including that first-query Agent build.

    The ``Agent`` class itself is stubbed to raise, which is the path that turns
    any Agent-construction failure into a clean error reply plus a loop exit, so
    the test stops immediately after the config was built: no LLM call, no tool
    call, no network, no container.
    """
    thread = _thread(tmp_path, project_root)
    bus = _RecordingBus(registry_snapshot)
    thread._input_queue.put("fail-open probe")  # triggers the first-query build

    with mock.patch("tools.workspace.worker.global_event_bus", bus), mock.patch(
        "agent.core.agent.Agent",
        side_effect=RuntimeError("agent disabled in test"),
    ):
        thread.run()

    published = [getattr(event, "type", None) for event in bus.events]
    assert EventType.WORKER_SPAWNED in published, (
        "the worker body never published WORKER_SPAWNED"
    )
    assert bus.bus_at_spawn_publish is not None, (
        "the per-worker event bus was not registered at WORKER_SPAWNED time"
    )
    assert resolver_calls, "the resolver must actually have been consulted"
    # Fail-open resolution ran against the project-root fallback, this session.
    assert resolver_calls[0][0] == str(project_root)
    assert resolver_calls[0][1] == SESSION


def test_publish_failure_is_logged(
    tmp_path, project_root, registry_snapshot, resolver_calls, caplog
):
    """A swallowed publish failure must leave a WARNING naming WORKER_SPAWNED."""
    thread = _thread(tmp_path, project_root)

    with mock.patch("tools.workspace.worker.global_event_bus", _BoomBus()):
        thread._stop_event.set()
        with caplog.at_level(logging.WARNING, logger="tools.workspace.worker"):
            thread.run()  # must not raise: the publish is best-effort

    failures = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "WORKER_SPAWNED publish FAILED" in record.getMessage()
    ]
    assert failures, (
        "a failed WORKER_SPAWNED publish was swallowed without a warning; "
        f"captured records: {[r.getMessage() for r in caplog.records]}"
    )
    # The failure text itself, not merely the event name: the success path also
    # logs WORKER_SPAWNED (at WARNING), so only "FAILED" + the cause pin failure.
    failure_text = failures[0].getMessage()
    assert "event bus unavailable" in failure_text, (
        "the failure warning must carry the cause; "
        f"got: {failure_text!r}"
    )


if __name__ == "__main__":  # pragma: no cover - manual runs
    raise SystemExit(pytest.main([__file__]))
