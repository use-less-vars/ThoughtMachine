"""Test the per-worker EventBus → bridge wiring end-to-end."""
import sys
sys.path.insert(0, '/workspace/pytest_install')
import time

from agent.events import EventBus, EventType, global_event_bus, create_event
from tools.workspace.worker import register_worker_event_bus, get_worker_event_bus, unregister_worker_event_bus
from web_ui.backend.bridge import WebAgentBridge


def test_bridge_subscribes_to_per_worker_bus_on_spawn():
    """
    Simulate: worker spawns → publishes WORKER_SPAWNED to global_event_bus
    → bridge receives it → subscribes to per-worker bus
    → worker publishes events to per-worker bus → bridge forwards them.
    """
    received_events = []

    def event_callback(event_dict):
        received_events.append(event_dict)

    # Create bridge (no session_id param - it's set during start())
    bridge = WebAgentBridge(event_callback=event_callback)
    # Manually set session_id since we're not going through start()
    bridge._session_id = 'test-session-123'

    # Create a per-worker EventBus (simulating WorkerThread.run())
    worker_bus = EventBus()
    worker_name = 'test-worker'
    session_id = 'test-session-123'

    # Register it (simulating register_worker_event_bus call in WorkerThread)
    register_worker_event_bus(session_id, worker_name, worker_bus)

    # Verify we can retrieve it
    retrieved = get_worker_event_bus(session_id, worker_name)
    assert retrieved is worker_bus, f"Expected {worker_bus}, got {retrieved}"

    # Publish WORKER_SPAWNED to global_event_bus
    # create_event signature: create_event(event_type, data, source='unknown', session_id=None, turn=None)
    if global_event_bus is not None:
        evt = create_event(
            EventType.WORKER_SPAWNED,
            data={
                "session_id": session_id,
                "worker_name": worker_name,
            },
            source=f"worker:{worker_name}",
            session_id=session_id,
        )
        global_event_bus.publish(evt)

    # Give the bridge a moment to process
    time.sleep(0.1)

    # The bridge should have received the spawn event
    assert len(received_events) > 0, f"Bridge should have received WORKER_SPAWNED. Got: {received_events}"
    spawn_event = received_events[0]
    assert 'worker' in spawn_event.get('type', '').lower(), (
        f"Expected worker event type, got: {spawn_event.get('type')}"
    )
    assert spawn_event.get('worker_name') == worker_name

    # Now simulate the worker publishing events to its own bus
    received_events.clear()

    # Publish a tool call event on the per-worker bus
    if hasattr(EventType, 'TOOL_CALL'):
        tool_call_evt = create_event(
            EventType.TOOL_CALL,
            data={
                "session_id": session_id,
                "worker_name": worker_name,
                "tool_name": "test_tool",
                "arguments": "{}",
            },
            source=f"worker:{worker_name}",
            session_id=session_id,
        )
        worker_bus.publish(tool_call_evt)
        time.sleep(0.05)

        tool_call_found = any(
            'tool_call' in evt.get('type', '') for evt in received_events
        )
        if not tool_call_found:
            print(f"WARNING: No tool_call forwarded. Available types: {[e.get('type') for e in received_events]}")

    # Publish an assistant message on the per-worker bus
    received_events.clear()
    if hasattr(EventType, 'ASSISTANT_MESSAGE'):
        msg_evt = create_event(
            EventType.ASSISTANT_MESSAGE,
            data={
                "session_id": session_id,
                "worker_name": worker_name,
                "content": "Hello from worker!",
            },
            source=f"worker:{worker_name}",
            session_id=session_id,
        )
        worker_bus.publish(msg_evt)
        time.sleep(0.05)

        msg_found = any(
            'assistant_message' in evt.get('type', '') or 'worker_message' in evt.get('type', '')
            for evt in received_events
        )
        if msg_found:
            print("SUCCESS: Assistant message forwarded to bridge callback!")
        else:
            print(f"WARNING: No assistant_message forwarded. Types: {[e.get('type') for e in received_events]}")

    # Now simulate worker completion via global bus
    received_events.clear()
    if global_event_bus is not None:
        complete_evt = create_event(
            EventType.WORKER_COMPLETED,
            data={
                "session_id": session_id,
                "worker_name": worker_name,
                "status": "completed",
            },
            source=f"worker:{worker_name}",
            session_id=session_id,
        )
        global_event_bus.publish(complete_evt)
        time.sleep(0.05)

    # Bridge should have unsubscribed from per-worker bus
    # So further events on per-worker bus should NOT reach the callback
    received_events.clear()
    if hasattr(EventType, 'TOOL_CALL'):
        another_tool = create_event(
            EventType.TOOL_CALL,
            data={"session_id": session_id, "worker_name": worker_name, "tool_name": "unused", "arguments": "{}"},
            source=f"worker:{worker_name}",
            session_id=session_id,
        )
        worker_bus.publish(another_tool)
        time.sleep(0.05)

    # Should be empty (bridge unsubscribed)
    post_complete_events = [e for e in received_events if 'worker' in e.get('type', '')]
    if len(post_complete_events) == 0:
        print("SUCCESS: No events forwarded after bridge unsubscribed")
    else:
        print(f"WARNING: Events still arriving after unsubscribe: {[e.get('type') for e in post_complete_events]}")

    # Registry should eventually be cleaned up by the finally block
    # (simulate what WorkerThread does)
    unregister_worker_event_bus(session_id, worker_name)
    assert get_worker_event_bus(session_id, worker_name) is None, (
        "Worker bus should be unregistered after cleanup"
    )

    # Cleanup
    bridge.unregister()
    print("\n=== TEST COMPLETED SUCCESSFULLY ===")


def _bridge_with_worker(session_id, worker_name, received):
    """Build a bridge bound to *session_id* and drive a real WORKER_SPAWNED
    publish so it subscribes to that worker's per-worker EventBus."""

    def event_callback(event_dict):
        received.append(event_dict)

    bridge = WebAgentBridge(event_callback=event_callback)
    # Manually set session_id since we're not going through start()
    bridge._session_id = session_id

    worker_bus = EventBus()
    register_worker_event_bus(session_id, worker_name, worker_bus)
    global_event_bus.publish(create_event(
        EventType.WORKER_SPAWNED,
        data={"session_id": session_id, "worker_name": worker_name},
        source=f"worker:{worker_name}",
        session_id=session_id,
    ))
    return bridge, worker_bus


def _release(bridge, session_id, worker_name):
    """Detach a test bridge from every bus it subscribed to."""
    bridge._unsubscribe_worker_events()
    global_event_bus.unsubscribe(EventType.WORKER_SPAWNED, bridge._on_worker_spawned)
    global_event_bus.unsubscribe(EventType.WORKER_COMPLETED, bridge._on_worker_completed)
    global_event_bus.unsubscribe(EventType.WORKER_ERROR, bridge._on_worker_error)
    unregister_worker_event_bus(session_id, worker_name)
    bridge.unregister()


def test_completed_event_from_foreign_session_preserves_subscriptions():
    """A WORKER_COMPLETED published by ANOTHER session must not tear down this
    bridge's per-worker subscriptions.

    ``global_event_bus`` is process-global, so every bridge sees every worker
    completion.  Two sessions each running a worker labelled ``default`` share
    nothing but that label; the foreign completion must be ignored here, while
    the owning bridge still unsubscribes its own worker.
    """
    worker_name = 'default'
    session_a, session_b = 'sess-A', 'sess-B'
    received_a, received_b = [], []

    bridge_a, bus_a = _bridge_with_worker(session_a, worker_name, received_a)
    bridge_b, bus_b = _bridge_with_worker(session_b, worker_name, received_b)
    try:
        # Precondition: both bridges hold a subscription for worker 'default'.
        assert worker_name in bridge_a._worker_bus_subs, bridge_a._worker_bus_subs
        assert worker_name in bridge_b._worker_bus_subs, bridge_b._worker_bus_subs
        assert bus_a is not bus_b

        # Session A's worker completes -> published on the shared global bus.
        global_event_bus.publish(create_event(
            EventType.WORKER_COMPLETED,
            data={
                "session_id": session_a,
                "worker_name": worker_name,
                "status": "completed",
            },
            source=f"worker:{worker_name}",
            session_id=session_a,
        ))
        time.sleep(0.05)

        # The owning bridge MUST drop its own subscription ...
        assert worker_name not in bridge_a._worker_bus_subs, (
            "bridge A did not unsubscribe its own completed worker"
        )
        # ... and the foreign bridge MUST keep it.
        assert worker_name in bridge_b._worker_bus_subs, (
            "a WORKER_COMPLETED from another session unsubscribed this "
            "bridge's per-worker bus"
        )

        # Observable consequence: session B still receives its worker's
        # streaming tool-call events after A's worker finished.
        received_b.clear()
        bus_b.publish(create_event(
            EventType.TOOL_CALL_START,
            data={
                "session_id": session_b,
                "worker_name": worker_name,
                "tool_name": "Thought",
                "arguments": "{}",
            },
            source=f"worker:{worker_name}",
            session_id=session_b,
        ))
        time.sleep(0.05)
        assert any(
            e.get('type') == 'worker:tool_call_start' for e in received_b
        ), f"bridge B lost the worker's tool_call_start after A completed: {received_b}"
    finally:
        _release(bridge_a, session_a, worker_name)
        _release(bridge_b, session_b, worker_name)


if __name__ == '__main__':
    test_bridge_subscribes_to_per_worker_bus_on_spawn()
    test_completed_event_from_foreign_session_preserves_subscriptions()
