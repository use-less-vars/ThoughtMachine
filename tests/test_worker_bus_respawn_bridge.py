"""Regression tests for stale per-worker EventBus bindings in ``WebAgentBridge``.

Defect under test
─────────────────
``WebAgentBridge._worker_bus_subs`` used to store only the subscription
handles of a per-worker ``EventBus`` — never the bus object those handles were
created on.  Both "already subscribed" guards
(``_on_worker_spawned`` ``bridge.py:775-779`` and ``_discover_existing_workers``
``bridge.py:650-653``) were plain ``label in dict`` membership tests.

When the registry entry for a ``(session_id, worker_name, instance_id)`` key is
replaced by a *new* ``EventBus`` — the normal stop-and-respawn path when the
global ``WORKER_COMPLETED`` publish never reaches the bridge
(``tools/workspace/worker_thread.py`` wraps those publishes in
``try/except: pass``) — the guard skips re-subscription.  The bridge then stays
bound to the *dead* bus for the rest of its life: the worker panel receives the
spawn lifecycle events but no ``tool_call`` / ``tool_call_start`` /
``assistant_message`` rows.  That is the "intermittent missing tool calls"
symptom.

These tests drive the real ``WorkerRegistry`` and real ``EventBus`` objects and
trigger the bridge through a real ``global_event_bus.publish(WORKER_SPAWNED)``,
so the guard under test is exercised exactly as in production.
"""

import uuid
from unittest import mock

import pytest

from agent.events import (
    EventBus,
    EventType,
    create_event,
    global_event_bus,
)
from tools.workspace.worker import (
    register_worker_event_bus,
    unregister_worker_event_bus,
)
from web_ui.backend import bridge as bridge_module
from web_ui.backend.bridge import WebAgentBridge


@pytest.fixture()
def session_id():
    """A session id unique to one test.

    ``EventBus.subscribe`` returns ``None``, so a bridge's stored global-bus
    subscription handles are ``None`` and ``_unsubscribe_worker_events`` cannot
    actually detach them — bridges from earlier tests in the same process stay
    subscribed to ``global_event_bus`` forever.  A per-test session id keeps
    those stale listeners inert, because ``_on_worker_spawned`` drops any event
    whose ``session_id`` differs from the bridge's own.
    """
    return f"test-session-bus-respawn-{uuid.uuid4().hex[:8]}"


# ── helpers ────────────────────────────────────────────────────────────────


def _make_bridge(sid):
    """Return ``(bridge, received)`` with the session id set the way
    ``start()`` would set it (no agent thread is started)."""
    received = []
    bridge = WebAgentBridge(event_callback=lambda ev: received.append(ev))
    bridge._session_id = sid
    return bridge, received


def _publish_worker_spawned(sid, worker_name, instance_id=1):
    assert global_event_bus is not None, "event system unavailable in this environment"
    global_event_bus.publish(create_event(
        EventType.WORKER_SPAWNED,
        data={
            "session_id": sid,
            "worker_name": worker_name,
            "instance_id": instance_id,
        },
        source=f"worker:{worker_name}",
        session_id=sid,
    ))


def _publish_tool_call_start(sid, bus, worker_name, instance_id=1, tool_name="lookup"):
    bus.publish(create_event(
        EventType.TOOL_CALL_START,
        data={
            "session_id": sid,
            "worker_name": worker_name,
            "instance_id": instance_id,
            "tool_name": tool_name,
        },
        source=f"worker:{worker_name}",
        session_id=sid,
    ))


def _tool_call_starts(received):
    return [
        ev for ev in received
        if "tool_call_start" in (ev.get("type") or "")
    ]


def _teardown(sid, bridge, worker_name, instance_id=1):
    """Undo everything a test created — bus registry entry and the bridge's own
    global/per-worker subscriptions (``bridge.unregister()`` only drops the
    bridge from the active-tab set; it does not touch the event busses)."""
    try:
        unregister_worker_event_bus(sid, worker_name, instance_id=instance_id)
    except Exception:
        pass
    try:
        bridge._unsubscribe_worker_events()
    except Exception:
        pass
    try:
        bridge._unsubscribe_security_events()
    except Exception:
        pass


# ── (i) the defect ─────────────────────────────────────────────────────────


def test_respawn_after_bus_replacement_still_delivers_tool_events(session_id):
    """A worker respawned onto a NEW EventBus must still deliver its detailed
    events to the bridge.

    Old bus is bound → registry entry replaced by a new bus for the same
    ``(session, worker, instance)`` key → WORKER_SPAWNED is published again →
    ``tool_call_start`` is published on the *new* bus.  The bridge must have
    re-subscribed: today the ``:775-779`` guard sees the label in
    ``_worker_bus_subs`` and skips, so nothing is delivered.
    """
    worker_name = "respawn-worker"
    old_bus = EventBus()
    new_bus = EventBus()
    register_worker_event_bus(session_id, worker_name, old_bus, instance_id=1)
    bridge, received = _make_bridge(session_id)
    try:
        # 1. First spawn binds the bridge to the old bus.
        _publish_worker_spawned(session_id, worker_name)
        assert worker_name in bridge._worker_bus_subs, (
            f"bridge did not subscribe to the per-worker bus on spawn; "
            f"keys={sorted(bridge._worker_bus_subs.keys())}"
        )

        # Baseline evidence that the binding is live before the replacement.
        received.clear()
        _publish_tool_call_start(session_id, old_bus, worker_name)
        assert _tool_call_starts(received), (
            "precondition failed: tool_call_start on the ORIGINAL bus was not "
            f"forwarded; received={[e.get('type') for e in received]}"
        )

        # 2. Registry entry for the same key is replaced by a NEW bus
        #    (stop-and-respawn, no WORKER_COMPLETED reached the bridge).
        register_worker_event_bus(session_id, worker_name, new_bus, instance_id=1)

        # 3. WORKER_SPAWNED is published again.
        _publish_worker_spawned(session_id, worker_name)

        # 4. tool_call_start on the CURRENT bus must reach the frontend.
        received.clear()
        _publish_tool_call_start(session_id, new_bus, worker_name)
        assert _tool_call_starts(received), (
            "tool_call_start published on the worker's CURRENT bus was not "
            "forwarded after respawn — the bridge is still bound to the dead "
            f"bus; received={[e.get('type') for e in received]}"
        )

        # 5. The stored (dead) bus must actually be unsubscribed, not merely
        #    shadowed — otherwise the frontend gets duplicate/ghost rows.
        received.clear()
        _publish_tool_call_start(session_id, old_bus, worker_name)
        assert not _tool_call_starts(received), (
            "the stale bus is still subscribed after respawn — ghost events "
            f"would be forwarded; received={[e.get('type') for e in received]}"
        )
    finally:
        _teardown(session_id, bridge, worker_name)


# ── (ii) bounded retry then one visible degradation ────────────────────────


def test_missing_worker_bus_retries_boundedly_then_degrades_once(session_id):
    """An unresolvable per-worker bus is retried a bounded number of times and
    then surfaced once to the frontend, naming the worker.

    Bound is deliberate: 5 attempts, 250 ms apart, so a WORKER_SPAWNED handler
    can never block the publishing thread for longer than ~1.25 s.
    """
    worker_name = "ghost-worker"
    bridge, received = _make_bridge(session_id)
    attempts = []

    def _missing(sid, name, instance_id=1):
        attempts.append((sid, name, instance_id))
        return None

    try:
        with mock.patch.object(bridge_module, "get_worker_event_bus", side_effect=_missing):
            _publish_worker_spawned(session_id, worker_name)

        assert len(attempts) == 5, (
            f"expected exactly 5 bounded resolution attempts, got {len(attempts)}"
        )
        assert all(a[1] == worker_name for a in attempts), attempts
        assert worker_name not in bridge._worker_bus_subs, (
            "a failed resolution must not leave a subscription entry behind"
        )

        degradations = [
            ev for ev in received
            if ev.get("type") == "worker:system_notification"
        ]
        assert len(degradations) == 1, (
            "expected exactly ONE frontend-visible degradation notification, "
            f"got {len(degradations)}: {degradations}"
        )
        ev = degradations[0]
        assert ev.get("worker_name") == worker_name, ev
        assert ev.get("instance_id") == 1, ev
        assert ev.get("instance_label") == worker_name, ev
        response = ev.get("response") or {}
        assert worker_name in (response.get("message") or ""), (
            f"degradation message must name the worker; got {response!r}"
        )
    finally:
        _teardown(session_id, bridge, worker_name)


# ── (iii) stop-and-respawn with no WORKER_COMPLETED ────────────────────────


def test_stop_and_respawn_without_completion_leaves_no_dead_bus_bound(session_id):
    """Worker stops (registry entry removed) without a WORKER_COMPLETED that the
    bridge can observe, then respawns on a fresh bus.

    The bridge must not keep forwarding from — or stay bound to — the vanished
    bus; the new instance's detailed events must arrive.
    """
    worker_name = "stoprespawn-worker"
    first_bus = EventBus()
    second_bus = EventBus()
    register_worker_event_bus(session_id, worker_name, first_bus, instance_id=1)
    bridge, received = _make_bridge(session_id)
    try:
        _publish_worker_spawned(session_id, worker_name)
        assert worker_name in bridge._worker_bus_subs, sorted(bridge._worker_bus_subs.keys())

        # Worker thread dies without publishing WORKER_COMPLETED to the bridge:
        # the registry entry just disappears (worker_thread.py ``finally`` path).
        unregister_worker_event_bus(session_id, worker_name, instance_id=1)
        assert worker_name in bridge._worker_bus_subs, (
            "precondition: the bridge is expected to still hold the entry for a "
            "worker that died without a delivered WORKER_COMPLETED"
        )

        # Respawn: a fresh bus is registered under the same key.
        register_worker_event_bus(session_id, worker_name, second_bus, instance_id=1)
        _publish_worker_spawned(session_id, worker_name)

        received.clear()
        _publish_tool_call_start(session_id, second_bus, worker_name,
                                 tool_name="second_instance_tool")
        assert _tool_call_starts(received), (
            "the respawned worker's tool_call_start was not forwarded — the "
            "bridge is still bound to the dead bus; "
            f"received={[e.get('type') for e in received]}"
        )

        # And the dead bus must no longer be subscribed.
        received.clear()
        _publish_tool_call_start(session_id, first_bus, worker_name, tool_name="ghost_tool")
        assert not _tool_call_starts(received), (
            f"dead bus still delivered events; received={[e.get('type') for e in received]}"
        )
    finally:
        _teardown(session_id, bridge, worker_name)
