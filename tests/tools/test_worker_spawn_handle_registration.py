"""S7a-3: the spawn path registers a handle, and everything about it fences.

Before this slice the substrate was asleep. ``attach_thread_frame_sink`` built a
``WorkerRuntimeHandle`` at the one production construction site and threw it
away, so ``_RUNTIME_HANDLES`` was empty in production, ``runtime_handle_for``
always missed, and the supervisor's command path could only ever reach a worker
through ``command.json``.

Three things have to hold for waking it to be safe, and this file pins all
three:

1. REGISTRATION -- the handle a spawn builds is reachable under the identity the
   routes look up with (raw ``session_id``, worker name, instance id), and it
   latches the spawned incarnation's epoch.
2. THE FENCE IS THE SAFETY NET -- a handle whose incarnation has been replaced
   raises :class:`StaleWorkerFence` instead of dispatching, or driving a control
   verb, into whatever occupies the slot now. That is why the handle store needs
   no high-water epoch map of its own (unlike the S7a-1 event bus): a stale
   handle that survives cannot be *used*.
3. DEREGISTRATION IS FENCED -- a dying predecessor's teardown must not unplug
   its successor's handle. Same bug class as S7a-1, one store further out.

The positive path is exercised through the same two calls ``_action_spawn``
makes (``attach_thread_frame_sink`` then ``register_runtime_handle``) rather
than by driving a real spawn: a real spawn needs an agent definition, a vault
and an LLM turn, none of which belong in a unit test. That the spawn site
really makes those two calls, in that order and with those arguments, is
pinned by ``test_the_spawn_site_registers_the_handle_it_attached``.

Deterministic by construction: no threads, no processes, no sleeps, and every
worker double carries a real ``int`` ``_generation``.
"""

from __future__ import annotations

import inspect
import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.workspace import worker_runtime
from tools.workspace.worker_registry import WorkerRegistry
from tools.workspace.worker_runtime import (
    MODE_THREAD,
    StaleWorkerFence,
    attach_thread_frame_sink,
    register_runtime_handle,
    runtime_handle_for,
    unregister_runtime_handle,
)
from web_ui.backend.workspace_routes import router


SESSION = "sess-s7a3"
WORKER = "w1"
INSTANCE = 1

#: Deliberately not 1: a fence that defaulted its epoch would pass by accident.
_PREDECESSOR_EPOCH = 7
_SUCCESSOR_EPOCH = 8

_VERBS = ("stop", "pause", "resume", "kill", "cancel")


@pytest.fixture(autouse=True)
def _isolated_stores():
    """Snapshot and restore the two stores a handle's life touches.

    The handle store, because a leaked registration would leave
    ``runtime_mode_report`` reporting a non-zero ``registered_handles`` for
    every later test (the seam suite asserts exactly zero), and the worker
    registry, because the fence reads evidence out of its epoch map and a
    leaked record would fence an unrelated handle.

    Yields the registry singleton the tests need to seed that evidence.
    """
    registry = WorkerRegistry.get_instance()
    saved_handles = dict(worker_runtime._RUNTIME_HANDLES)
    worker_runtime._RUNTIME_HANDLES.clear()
    with registry._registry_lock:
        saved_workers = dict(registry._worker_registry)
        saved_epochs = dict(registry._worker_epochs)
        registry._worker_registry.clear()
        registry._worker_epochs.clear()
    try:
        yield registry
    finally:
        worker_runtime._RUNTIME_HANDLES.clear()
        worker_runtime._RUNTIME_HANDLES.update(saved_handles)
        with registry._registry_lock:
            registry._worker_registry.clear()
            registry._worker_registry.update(saved_workers)
            registry._worker_epochs.clear()
            registry._worker_epochs.update(saved_epochs)


class FakeWorker:
    """A thread-mode worker double.

    Records every verb it is driven with (``verbs``) and every query it is sent
    (``sent``), so "the wrong worker was reached" is an assertion about an empty
    list rather than an inference from a return value.
    """

    def __init__(
        self,
        generation=_PREDECESSOR_EPOCH,
        *,
        session_id=SESSION,
        worker_name=WORKER,
        instance_id=INSTANCE,
        reply="thread reply",
    ):
        self._generation = generation
        self.session_id = session_id
        self.worker_name = worker_name
        self.instance_id = instance_id
        self.reply = reply
        self.sent: list[tuple[str, float]] = []
        self.verbs: list[str] = []

    def send_query(self, query: str, timeout: float = 120.0) -> str:
        self.sent.append((query, timeout))
        return self.reply

    def is_alive(self) -> bool:
        return True

    def stop(self) -> None:
        self.verbs.append("stop")

    def pause(self) -> None:
        self.verbs.append("pause")

    def resume(self) -> bool:
        self.verbs.append("resume")
        return True

    def kill(self) -> None:
        self.verbs.append("kill")

    def detach(self) -> None:
        self.verbs.append("detach")


def _register_like_the_spawn_site(worker, *, epoch=None):
    """The two calls ``_action_spawn`` makes, with the spawn site's arguments.

    Identity is derived by ``attach_thread_frame_sink`` (it reads it off the
    worker) and passed explicitly to ``register_runtime_handle`` -- the same
    division of labour the spawn site has, so the key these tests register under
    is the key the routes look up with.
    """
    # Mode is pinned: every double here is a thread-mode stand-in (it owns no
    # definition/workspace to rebuild a spec from), and these tests are about
    # thread-mode dispatch, verbs and the fence -- not about the default.
    handle = attach_thread_frame_sink(worker, mode=MODE_THREAD)
    register_runtime_handle(
        handle,
        session_id=worker.session_id,
        worker_name=worker.worker_name,
        instance_id=worker.instance_id,
        epoch=worker._generation if epoch is None else epoch,
    )
    return handle


# -- 1: registration is reachable, and attaches no state of its own -----------


def test_a_registered_handle_is_reachable_by_the_routes_identity(_isolated_stores):
    worker = FakeWorker(generation=_PREDECESSOR_EPOCH)

    handle = _register_like_the_spawn_site(worker)

    assert runtime_handle_for(SESSION, WORKER, INSTANCE) is handle
    assert handle.epoch == worker._generation


def test_the_key_is_instance_scoped_not_name_scoped(_isolated_stores):
    # Name-scoped (2-tuple) lookups are a different key by design: identity is
    # not a predicate, so a name-only lookup must miss rather than guess an
    # instance.
    handle = _register_like_the_spawn_site(FakeWorker())

    assert runtime_handle_for(SESSION, WORKER) is None
    assert runtime_handle_for(SESSION, WORKER, INSTANCE) is handle


def test_attaching_alone_registers_nothing(_isolated_stores):
    # Registration is the spawn site's decision, not a side effect of building
    # the handle: an attached-but-unregistered handle is invisible to the
    # command path, which is exactly what keeps the seam behaviour-neutral.
    worker = FakeWorker()

    handle = attach_thread_frame_sink(worker)

    assert runtime_handle_for(SESSION, WORKER, INSTANCE) is None
    assert worker._runtime_handle is handle


def test_the_spawn_site_registers_the_handle_it_attached():
    """Pin the spawn site itself: build, keep, register -- under the raw session.

    Driving a real spawn is out of reach for a unit test (agent definition,
    vault, LLM turn), so the contract is pinned at the source instead. What
    matters is that the handle is KEPT (it used to be discarded), that the
    registration happens in ``_action_spawn``, and that it keys the handle with
    ``self.session_id`` rather than the registry's ``session_key`` sentinel --
    ``_NO_SESSION_KEY`` is ``"<no-session>"`` while the routes look up ``None``,
    so keying with the sentinel would make every legacy worker's handle
    unreachable.
    """
    from tools.workspace import worker_thread

    source = inspect.getsource(worker_thread.Worker._action_spawn)

    assert "_runtime_handle = attach_thread_frame_sink(thread)" in source
    assert "register_runtime_handle(" in source
    assert "session_id=self.session_id," in source
    assert "instance_id=spawn_iid," in source
    # The registration must FOLLOW the critical section, never sit inside it:
    # the handle store takes its own lock and the order is registry -> handle.
    registered_at = source.index("register_runtime_handle(")
    assert registered_at > source.index("_worker_epochs[_spawn_key] = _spawn_epoch")


# -- 2: a replaced incarnation's handle cannot dispatch ----------------------

def test_a_stale_handle_cannot_dispatch_into_its_successor(_isolated_stores):
    """The integration proof: stale handle raises, successor keeps working."""
    registry = _isolated_stores
    predecessor = FakeWorker(generation=_PREDECESSOR_EPOCH, reply="predecessor")
    _register_like_the_spawn_site(predecessor)
    stale = runtime_handle_for(SESSION, WORKER, INSTANCE)

    # The slot moves on: a successor is born at epoch 8 and registers itself,
    # so the registry now has evidence of a newer incarnation for this pair.
    successor = FakeWorker(generation=_SUCCESSOR_EPOCH, reply="successor")
    registry.register_worker(SESSION, WORKER, successor, INSTANCE, epoch=_SUCCESSOR_EPOCH)
    successor_handle = _register_like_the_spawn_site(successor)

    with pytest.raises(StaleWorkerFence):
        stale.dispatch_query("hello")

    # Neither worker was reached by the dead handle, and the successor's own
    # handle still dispatches -- the fence costs the live incarnation nothing.
    assert predecessor.sent == []
    assert successor.sent == []
    assert successor_handle.dispatch_query("hello") == "successor"
    assert successor.sent == [("hello", 120.0)]


# -- 3: deregistration is a compare-and-set ----------------------------------

def test_a_dying_predecessor_cannot_unregister_its_successor(_isolated_stores):
    successor = FakeWorker(generation=_SUCCESSOR_EPOCH, reply="successor")
    _register_like_the_spawn_site(successor)
    successor_handle = runtime_handle_for(SESSION, WORKER, INSTANCE)

    # The predecessor's teardown reaches the fence with ITS epoch.
    assert (
        unregister_runtime_handle(
            SESSION, WORKER, INSTANCE, epoch=_PREDECESSOR_EPOCH
        )
        is False
    )

    assert runtime_handle_for(SESSION, WORKER, INSTANCE) is successor_handle
    assert successor_handle.dispatch_query("hi") == "successor"


def test_an_equal_or_older_incumbent_is_removed(_isolated_stores):
    handle = _register_like_the_spawn_site(FakeWorker(generation=_SUCCESSOR_EPOCH))

    assert (
        unregister_runtime_handle(SESSION, WORKER, INSTANCE, epoch=_SUCCESSOR_EPOCH)
        is True
    )
    assert runtime_handle_for(SESSION, WORKER, INSTANCE) is None

    # An OLDER caller may also remove: the incumbent is not newer than it.
    older = _register_like_the_spawn_site(FakeWorker(generation=_PREDECESSOR_EPOCH))
    assert (
        unregister_runtime_handle(SESSION, WORKER, INSTANCE, epoch=_SUCCESSOR_EPOCH)
        is True
    )
    assert runtime_handle_for(SESSION, WORKER, INSTANCE) is None
    assert older is not None


def test_the_unfenced_legacy_call_still_pops_and_returns_the_handle(_isolated_stores):
    # ``epoch=None`` is the pre-S7a-3 signature contract: pop unconditionally,
    # return the handle, then ``None`` (the seam suite and the routes' fallback
    # suite both rely on both halves).
    handle = _register_like_the_spawn_site(FakeWorker())

    assert unregister_runtime_handle(SESSION, WORKER, INSTANCE) is handle
    assert unregister_runtime_handle(SESSION, WORKER, INSTANCE) is None


def test_registration_is_fenced_against_a_newer_incumbent(_isolated_stores):
    successor_handle = _register_like_the_spawn_site(
        FakeWorker(generation=_SUCCESSOR_EPOCH)
    )
    stale = attach_thread_frame_sink(FakeWorker(generation=_PREDECESSOR_EPOCH))

    assert (
        register_runtime_handle(
            stale,
            session_id=SESSION,
            worker_name=WORKER,
            instance_id=INSTANCE,
            epoch=_PREDECESSOR_EPOCH,
        )
        is False
    )
    assert runtime_handle_for(SESSION, WORKER, INSTANCE) is successor_handle


# -- 4: every control verb fences in thread mode -----------------------------


@pytest.mark.parametrize("verb", _VERBS)
def test_each_control_verb_fences_a_stale_thread_mode_handle(verb, _isolated_stores):
    worker = FakeWorker(generation=_PREDECESSOR_EPOCH)
    handle = _register_like_the_spawn_site(worker)
    worker._generation = _SUCCESSOR_EPOCH  # the slot moved on

    with pytest.raises(StaleWorkerFence):
        getattr(handle, verb)()

    # Fenced BEFORE the mode branch, so the worker was never touched.
    assert worker.verbs == []
    assert worker.sent == []


def test_a_current_handle_still_drives_the_thread_worker(_isolated_stores):
    # The fence must not cost a live handle anything: same call, no epoch drift.
    worker = FakeWorker(generation=_PREDECESSOR_EPOCH)
    handle = _register_like_the_spawn_site(worker)

    assert handle.pause() is True
    assert worker.verbs == ["pause"]


# -- 5: route level -- a stale handle degrades, never 500s -------------------

_WS_ID = "ws-spawn-handle"
_SEEDED_GENERATION = 11


def test_a_stale_registered_handle_degrades_the_pause_route(
    tmp_path, monkeypatch, caplog
) -> None:
    """The supervisor endpoint must fall back to ``command.json``, not 500."""
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
    worker_dir = tmp_path / "workspaces" / _WS_ID / "workers" / "w1"
    worker_dir.mkdir(parents=True)
    (worker_dir / "generation").write_text(str(_SEEDED_GENERATION), encoding="utf-8")

    # Legacy layout: the route resolves session_id=None and instance 1, which is
    # the key the lookup forms -- so the handle has to be registered under the
    # RAW None (the spawn site's ``self.session_id``), not a sentinel.
    worker = FakeWorker(generation=_PREDECESSOR_EPOCH, session_id=None, worker_name="w1")
    handle = _register_like_the_spawn_site(worker)
    assert runtime_handle_for(None, "w1", INSTANCE) is handle
    worker._generation = _SEEDED_GENERATION + 1  # the on-disk incarnation moved on

    app = FastAPI()
    app.include_router(router)
    with caplog.at_level(logging.WARNING, logger="web_ui.backend.workspace_routes"):
        with TestClient(app) as client:
            response = client.post(f"/api/workspace/{_WS_ID}/workers/w1/pause")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"

    # The durable channel carried the command, fully fenced.
    payload = json.loads((worker_dir / "command.json").read_text(encoding="utf-8"))
    assert payload == {
        "action": "pause",
        "session_id": "",
        "worker_name": "w1",
        "instance_id": INSTANCE,
        "epoch": _SEEDED_GENERATION,
    }

    # The wrong worker was never paused, and the degradation is visible.
    assert worker.verbs == []
    fallbacks = [
        record
        for record in caplog.records
        if "falling back to the fenced command channel" in record.getMessage()
    ]
    assert len(fallbacks) == 1, [r.getMessage() for r in caplog.records]
    assert fallbacks[0].exc_info is not None
    assert fallbacks[0].exc_info[0] is StaleWorkerFence
