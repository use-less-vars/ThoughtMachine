"""Regression tests for the S8 PARENT-side half of worker event forwarding.

Under ``MODE_PROCESS`` the worker that publishes events lives in a child
interpreter, so the bus the bridge resolves with
``get_worker_event_bus(session_id, worker_name, instance_id)`` can never be
registered by the worker itself: the child's ``EventBus`` object does not exist
in this process. ``WorkerRuntimeHandle`` therefore registers one on the worker's
behalf at CONSTRUCTION (not on the first event, because the lookup happens while
``worker_spawned`` is being published), and ``on_frame`` republishes each
forwarded ``event`` frame onto the bus the frame's ``stream`` names:

* ``global`` -> this process' module-level ``global_event_bus``;
* ``worker`` -> the per-worker bus registered above.

Four properties are covered, one per test file section below: the two streams
land where they should, the worker bus is resolvable before any frame arrives,
and a frame from a peer that predates ``stream`` is treated as ``global``.

Deterministic by construction: no threads, no processes, no sleeps. Every frame
is built with the real protocol encoder and decoded with the real
``Event.from_frame``, so the wire shape under test is the wire shape the child
produces.
"""

from __future__ import annotations

import importlib
import logging
import sys

import pytest

from agent.events import EventType, global_event_bus
from tools.workspace.worker_registry import WorkerRegistry
from tools.workspace.worker_runtime import MODE_PROCESS, MODE_THREAD, WorkerRuntimeHandle
from tools.workspace.worker_runtime_protocol import (
    EVENT_STREAM_GLOBAL,
    EVENT_STREAM_WORKER,
    Event as FrameEvent,
)

SESSION = "sess-forwarding"
WORKER = "w1"
INSTANCE = 1
EPOCH = 7


class FakeWorker:
    """A stand-in carrying the identity a handle latches, and nothing else."""

    def __init__(self, generation=EPOCH):
        self._generation = generation
        self.session_id = SESSION
        self.worker_name = WORKER
        self.instance_id = INSTANCE


def _dumped_payload(status="running"):
    """The ``data`` field the child frames: the WHOLE event, JSON-dumped."""
    from agent.events import create_event

    return create_event(
        EventType.WORKER_STATUS,
        {"worker_name": WORKER, "status": status},
        source="worker_thread",
        session_id=SESSION,
        turn=3,
    ).model_dump(mode="json")


def _frame(*, stream=None, status="running"):
    """A decoded ``event`` frame exactly as ``on_frame`` would receive it.

    *stream* ``None`` builds the frame a pre-field peer emits: the payload key is
    removed, so ``Event.from_frame`` has to fall back to the dataclass default.
    """
    frame = FrameEvent(
        event_type=EventType.WORKER_STATUS.value,
        data=_dumped_payload(status),
        stream=EVENT_STREAM_GLOBAL if stream is None else stream,
    ).to_frame(seq=1, worker=WORKER, instance=INSTANCE, epoch=EPOCH)
    if stream is None:
        frame["payload"].pop("stream")
    return frame


class Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)


@pytest.fixture
def registry():
    reg = WorkerRegistry.get_instance()
    reg.unregister_event_bus(SESSION, WORKER, INSTANCE)
    yield reg
    # ``epoch=None`` is the legacy unversioned pop: it always removes the key,
    # so a later test cannot inherit this one's bus (no cross-test leakage).
    reg.unregister_event_bus(SESSION, WORKER, INSTANCE)


@pytest.fixture
def global_recorder():
    recorder = Recorder()
    global_event_bus.subscribe(None, recorder)
    yield recorder
    global_event_bus.unsubscribe(None, recorder)


def _deliver(handle, frame):
    """Feed a frame the way the transport does: decode, then ``on_frame``."""
    handle.on_frame(FrameEvent.from_frame(frame))


def test_global_stream_event_reaches_the_parent_global_bus(
    registry, global_recorder
):
    """A ``global`` frame lands on this process' module-level bus, revived."""
    handle = WorkerRuntimeHandle(FakeWorker(), mode=MODE_PROCESS)

    _deliver(handle, _frame(stream=EVENT_STREAM_GLOBAL, status="running"))

    assert len(global_recorder.events) == 1
    event = global_recorder.events[0]
    # Revived, not handed over as a dict: subscribers are registered per
    # ``EventType``, so a string ``type`` would miss every one of them.
    assert event.type is EventType.WORKER_STATUS
    assert event.data == {"worker_name": WORKER, "status": "running"}
    assert event.metadata.session_id == SESSION
    assert event.metadata.source == "worker_thread"


def test_worker_stream_event_reaches_the_registered_worker_bus(registry):
    """A ``worker`` frame lands on the bus the bridge resolves by identity."""
    handle = WorkerRuntimeHandle(FakeWorker(), mode=MODE_PROCESS)

    from tools.workspace.worker_thread import get_worker_event_bus

    bus = get_worker_event_bus(SESSION, WORKER, INSTANCE)
    assert bus is not None
    recorders = {"all": [], "global": []}

    def on_worker(event):
        recorders["all"].append(event)

    def on_global(event):
        recorders["global"].append(event)

    bus.subscribe(None, on_worker)
    global_event_bus.subscribe(None, on_global)
    try:
        _deliver(handle, _frame(stream=EVENT_STREAM_WORKER, status="running"))
    finally:
        bus.unsubscribe(None, on_worker)
        global_event_bus.unsubscribe(None, on_global)

    assert len(recorders["all"]) == 1
    assert recorders["all"][0].type is EventType.WORKER_STATUS
    # The two streams are disjoint: a worker event must not also be broadcast
    # to every session's wildcard subscribers.
    assert recorders["global"] == []


def test_worker_bus_is_registered_at_construction_not_on_first_event(registry):
    """The lookup runs WHILE ``worker_spawned`` is published, so it must hit.

    Zero frames are delivered here: a bus registered on the first event would
    lose that worker's whole event stream for the incarnation's lifetime.
    """
    handle = WorkerRuntimeHandle(FakeWorker(), mode=MODE_PROCESS)

    from tools.workspace.worker_thread import get_worker_event_bus

    assert get_worker_event_bus(SESSION, WORKER, INSTANCE) is handle._event_bus
    assert handle._event_bus is not None


def test_legacy_frame_without_stream_is_treated_as_global(
    registry, global_recorder
):
    """A peer built before ``stream`` existed publishes on ``global`` only."""
    handle = WorkerRuntimeHandle(FakeWorker(), mode=MODE_PROCESS)

    from tools.workspace.worker_thread import get_worker_event_bus

    bus = get_worker_event_bus(SESSION, WORKER, INSTANCE)
    on_worker = Recorder()
    bus.subscribe(None, on_worker)
    try:
        _deliver(handle, _frame(stream=None, status="running"))
    finally:
        bus.unsubscribe(None, on_worker)

    assert len(global_recorder.events) == 1
    assert global_recorder.events[0].type is EventType.WORKER_STATUS
    assert on_worker.events == []


def test_thread_mode_registers_no_parent_bus(registry, global_recorder):
    """``MODE_THREAD`` keeps registering from inside ``WorkerThread.run``.

    A second, parent-side bus for a thread-mode worker would shadow the real
    one the in-process body registers, so this handle must register nothing --
    and it still must not raise when a ``worker``-stream frame arrives.
    """
    handle = WorkerRuntimeHandle(FakeWorker(), mode=MODE_THREAD)

    from tools.workspace.worker_thread import get_worker_event_bus

    assert handle._event_bus is None
    assert get_worker_event_bus(SESSION, WORKER, INSTANCE) is None

    _deliver(handle, _frame(stream=EVENT_STREAM_WORKER, status="running"))

    assert global_recorder.events == []

# ---------------------------------------------------------------------------
# The end-to-end gap: does the frame survive child -> parent -> REAL bridge?
# ---------------------------------------------------------------------------


def _spawned_frame():
    """The frame the child emits for ``WORKER_SPAWNED``.

    Shape copied from ``worker_thread.py:4524-4540`` -- the dict the worker
    itself passes to ``create_event`` -- and NOT augmented here: if the real
    event does not carry the key the bridge's gate reads, this fixture must fail
    with it rather than paper over it.
    """
    from agent.events import create_event

    event = create_event(
        EventType.WORKER_SPAWNED,
        source=f"worker:{WORKER}",
        session_id=SESSION,
        data={
            "session_id": SESSION,
            "worker_name": WORKER,
            "instance_id": INSTANCE,
            "instance_label": WORKER,
            "current_context_tokens": 0,
            "max_context_tokens": 8000,
            "status": "ready",
        },
    )
    dumped = event.model_dump(mode="json")
    return FrameEvent(
        event_type=str(dumped.get("type")),
        data=dumped,
        stream=EVENT_STREAM_GLOBAL,
    ).to_frame(seq=1, worker=WORKER, instance=INSTANCE, epoch=EPOCH)


def _real_bridge_module():
    """``web_ui.backend.bridge`` bound to the REAL modules, or ``None``.

    ``web_ui/backend/tests/test_bridge_dedup.py`` installs ``sys.modules`` stubs
    for ``agent`` / ``session`` / ``tools.workspace.worker``, imports the bridge
    through them, and only THEN restores the stubs -- so the cached module
    object keeps a ``MagicMock`` ``get_worker_event_bus`` for the rest of the
    process, and a run that collects both files would otherwise have to skip
    the one test that drives the real handler.

    Re-importing while the real modules are back in ``sys.modules`` gives a
    module object bound to production code. The old object is restored
    afterwards, so this repairs the measurement without replacing a module out
    from under any other test. ``None`` means even that failed: the caller skips
    rather than passing on a fake bus.
    """
    from tools.workspace.worker_thread import get_worker_event_bus as real_bus

    cached = sys.modules.get("web_ui.backend.bridge")
    if cached is not None and getattr(cached, "get_worker_event_bus", None) is real_bus:
        return cached
    saved = sys.modules.pop("web_ui.backend.bridge", None)
    try:
        importlib.invalidate_caches()
        fresh = importlib.import_module("web_ui.backend.bridge")
    except Exception:  # noqa: BLE001 - unimportable means skip, not fail
        fresh = None
    finally:
        sys.modules.pop("web_ui.backend.bridge", None)
        if saved is not None:
            sys.modules["web_ui.backend.bridge"] = saved
    if fresh is None or getattr(fresh, "get_worker_event_bus", None) is not real_bus:
        return None
    return fresh


def _global_sub_count(event_type):
    """How many handlers the process-wide ``global_event_bus`` holds now.

    Used as a before/after delta rather than an absolute count: a handler left
    on the bus by another test file is not this test's business, but one this
    test added and failed to detach is (item C60).
    """
    return len(global_event_bus._subscribers.get(event_type, ()))


def test_forwarded_worker_spawned_passes_the_real_bridge_session_gate(registry):
    """The revived ``WORKER_SPAWNED`` must satisfy the bridge's session gate.

    ``bridge._on_worker_spawned`` drops the event at ``bridge.py:1226-1231``
    unless ``data['session_id']`` satisfies ``_event_belongs_to_this_session``
    (``bridge.py:1676-1681``) -- and the per-worker bus is only subscribed on
    the branch PAST that gate (``bridge.py:1250-1268``). Driving the real
    handler with a frame the child would really emit is therefore the only proof
    that the revived payload kept the identity the gate reads; a test-local
    ``session_id`` would prove nothing.
    """
    bridge_mod = _real_bridge_module()
    if bridge_mod is None:
        # Another test file imported ``web_ui.backend.bridge`` with
        # ``tools.workspace.worker`` stubbed into sys.modules and a clean
        # re-import did not work either. Skipped rather than passed on a fake
        # bus: a green run against a mock resolver would prove nothing.
        pytest.skip("web_ui.backend.bridge was imported under module mocks")

    # Snapshot the process-wide state this test mutates, so the teardown below
    # can put it back: a singleton left changed for whatever runs next in the
    # same process is item C60, and the bus HANDLER count is the half of it that
    # a flag restore cannot fix.
    announced_before = bridge_mod._BUS_SUBSCRIBE_ANNOUNCED
    spawned_subs_before = _global_sub_count(EventType.WORKER_SPAWNED)

    bridge = bridge_mod.WebAgentBridge()
    try:
        bridge._bind_session_id(SESSION)
        # ``__init__`` already called ``_subscribe_to_worker_events()``. Calling
        # it a second time would register a SECOND pair of global handlers and
        # then overwrite the stored handles with the second pair, leaving
        # teardown able to detach only the second set -- the first pair would
        # stay on the global bus for the rest of the process.
        assert bridge._worker_spawned_sub is not None
        # Pre-condition: no bus registered yet, so the late-discovery path had
        # nothing to subscribe -- any subscription below comes from the spawn.
        assert bridge._worker_bus_subs == {}

        handle = WorkerRuntimeHandle(FakeWorker(), mode=MODE_PROCESS)
        bus = handle._event_bus
        assert bus is not None
        assert bridge._worker_bus_subs == {}

        # The subscribe announcement is process-wide one-shot; reset it so the
        # first-subscription branch (the WARNING one) is the branch taken here,
        # whatever ran before this test.
        bridge_mod._BUS_SUBSCRIBE_ANNOUNCED = False

        # The whole chain, in one call: on_frame -> _republish_event ->
        # parent global_event_bus -> bridge._on_worker_spawned.
        handle.on_frame(FrameEvent.from_frame(_spawned_frame()))

        key = bridge._bus_key(WORKER, SESSION)
        # A KeyError here IS the failure being hunted: the spawn was dropped
        # before the subscription branch, and the UI would stay blank.
        entry = bridge._worker_bus_subs[key]
        assert entry["bus"] is bus
        assert EventType.TOOL_CALL in entry["subs"]
        # The announcement branch that logs 'Subscribed to per-worker bus' at
        # WARNING on the first subscription did run.
        assert bridge_mod._BUS_SUBSCRIBE_ANNOUNCED is True
        # ...and the bridge's handler is really on the bus the handle
        # registered, not merely recorded in the bridge's own bookkeeping.
        assert entry["subs"][EventType.TOOL_CALL][1] in bus._subscribers[
            EventType.TOOL_CALL
        ]
    finally:
        # Restore the process-wide one-shot first-subscription flag the body
        # flipped, so a bridge constructed after this test still takes its own
        # first-subscription branch.
        bridge_mod._BUS_SUBSCRIBE_ANNOUNCED = announced_before
        try:
            # Both pairs were taken in ``__init__``: the worker lifecycle
            # handlers AND ``_security_subscription``. Neither may outlive the
            # test -- a live bridge handler on the global bus would forward this
            # test's events into a torn-down bridge for every later test.
            bridge._unsubscribe_worker_events()
            bridge._unsubscribe_security_events()
        except Exception:  # noqa: BLE001 - teardown must not mask the assertion
            pass
        # Nothing this test subscribed may still be on the global bus.
        assert _global_sub_count(EventType.WORKER_SPAWNED) == spawned_subs_before


# ---------------------------------------------------------------------------
# The failure paths must be visible: WARNING, once per handle per kind.
# ---------------------------------------------------------------------------


def _unrevivable_frame():
    """A frame whose payload ``create_event`` refuses (WORKER_TIMEOUT needs two keys)."""
    dumped = {
        "type": EventType.WORKER_TIMEOUT.value,
        "metadata": {"source": "worker_thread", "session_id": SESSION, "turn": 1},
        "data": {"status": "missing both worker_name and reason"},
    }
    return FrameEvent(
        event_type=EventType.WORKER_TIMEOUT.value,
        data=dumped,
        stream=EVENT_STREAM_GLOBAL,
    ).to_frame(seq=1, worker=WORKER, instance=INSTANCE, epoch=EPOCH)


def _forwarding_warnings(caplog):
    return [
        r for r in caplog.records
        if r.name == "tools.workspace.worker_runtime"
        and "worker event forwarding" in r.getMessage()
    ]


def test_a_revive_failure_warns_once_per_handle(registry, caplog):
    """First refused event warns at WARNING; the second is silent, not DEBUG-lost."""
    handle = WorkerRuntimeHandle(FakeWorker(), mode=MODE_PROCESS)

    with caplog.at_level(logging.WARNING, logger="tools.workspace.worker_runtime"):
        handle.on_frame(FrameEvent.from_frame(_unrevivable_frame()))
        handle.on_frame(FrameEvent.from_frame(_unrevivable_frame()))

    warnings = _forwarding_warnings(caplog)
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    message = warnings[0].getMessage()
    assert "worker_timeout" in message  # names the event type
    assert "create_event refused" in message  # ...and the reason
    assert handle._revive_failure_logged is True


def test_a_republish_failure_warns_once_per_handle(registry, caplog, monkeypatch):
    """A bus that raises warns once with the reason, and only for its own kind."""
    from tools.workspace import worker_runtime

    class ExplodingBus:
        def publish(self, event):
            raise RuntimeError("bus is on fire")

    monkeypatch.setattr(
        worker_runtime, "_parent_global_event_bus", lambda: ExplodingBus()
    )
    handle = WorkerRuntimeHandle(FakeWorker(), mode=MODE_PROCESS)
    frame = FrameEvent.from_frame(_frame(stream=EVENT_STREAM_GLOBAL, status="running"))

    with caplog.at_level(logging.WARNING, logger="tools.workspace.worker_runtime"):
        handle.on_frame(frame)
        handle.on_frame(frame)

    warnings = _forwarding_warnings(caplog)
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    message = warnings[0].getMessage()
    assert "worker_status" in message  # names the event type
    assert "bus is on fire" in message  # ...and the reason
    assert handle._republish_failure_logged is True
    # Kinds are independent: a republish failure is not a revive failure.
    assert handle._revive_failure_logged is False

