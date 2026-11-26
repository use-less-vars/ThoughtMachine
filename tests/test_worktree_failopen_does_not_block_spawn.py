"""Spawning a worker must not create a per-session git worktree.

Per-session worktree isolation is OPT-IN.  ``worker_thread._build_agent_config``
no longer calls ``resolve_worker_worktree_root`` and never sets
``worktree_root`` on its own, so a worker runs in the registered workspace root
-- the tree the agent's git tools and the docker mount use -- and logs one INFO
naming that root.  Everything the resolver does
(``tools/workspace/worktree_defaults.py``) is still FAIL-OPEN, but it only runs
when a caller explicitly asks for isolation by setting ``worktree_root``.

Covered here:

  * ``_build_agent_config`` still returns a config, with the ``workspace_path``
    fallback and no ``worktree_root`` -- and the resolver is NOT consulted:
    the ``resolver_calls`` fixture patches it and the tests assert zero calls;
  * the worker body still boots: it registers its per-worker event bus and
    publishes ``WORKER_SPAWNED`` (the event the bridge subscribes on).  The
    query loop is never entered (``_stop_event`` is pre-set), so there is no
    LLM call, no tool call, no network and no container;
  * a failing ``WORKER_SPAWNED`` publish is now VISIBLE: the publish is wrapped
    in ``try/except`` because it must never take the worker down, and that
    ``except`` logs a WARNING instead of swallowing the failure silently --
    which is why a production log without the string ``WORKER_SPAWNED`` used to
    prove nothing at all.

Note on the patch target: ``resolver_calls`` patches the attribute on the
defining module, ``tools.workspace.worktree_defaults.resolve_worker_worktree_root``
-- the one any lazy ``from ... import`` would read at call time -- and then
asserts it was never called.  Patching the definer rather than a caller's
namespace keeps that assertion independent of how (or whether) a caller
imports the symbol.

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
    """Record any consult of the worktree resolver.

    The spawn path must make none: a per-session worktree is opt-in, so the
    resolver is only reached by a caller that decided to isolate.
    """
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


def test_agent_config_skips_resolver_and_uses_workspace_root(
    tmp_path, project_root, resolver_calls
):
    thread = _thread(tmp_path, project_root)

    cfg = thread._build_agent_config()

    assert cfg is not None, "the config build must never be skipped"
    # The registered workspace root, i.e. what the git tools and the docker
    # mount see -- not a per-session worktree.
    assert cfg.workspace_path == str(project_root)
    # Worktree root absent -> the config field keeps its default.
    assert cfg.worktree_root is None
    assert not resolver_calls, (
        "spawning a worker must not create a per-session worktree; "
        f"resolver calls: {resolver_calls}"
    )


def test_spawn_does_not_consult_resolver(
    tmp_path, project_root, registry_snapshot, resolver_calls
):
    """The worker body boots and publishes WORKER_SPAWNED with no resolver call.

    Scope note: ``_build_agent_config`` is invoked from ``run()`` when the first
    query is dequeued, so driving ``run()`` is what exercises it.
    ``Worker._action_spawn`` builds no worktree either and is not driven here.
    What is asserted is the boot of the worker body, its bus registration and
    its WORKER_SPAWNED publish -- through the real ``run()`` up to and including
    that first-query Agent build -- plus the absence of any worktree resolution.

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
    assert not resolver_calls, (
        "the spawn path must not resolve a per-session worktree; "
        f"resolver calls: {resolver_calls}"
    )


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
