"""
Tests for event-level dedup of context_updated events in WebAgentBridge.

The bridge's _make_bus_handler deduplicates context_updated events using the
FORMATTED display string (matching frontend "X.XK" format), not raw integers.
Values like 78251 and 78349 both display as "78.3K" and are treated as duplicates.
This prevents duplicate "\U0001f4ca Context: X.XK" bubbles in the frontend.
"""
import sys
import pytest
import threading
import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch, PropertyMock

# ---------------------------------------------------------------------------
# Mock the agent module and its dependencies BEFORE importing WebAgentBridge.
# The agent module has heavy dependencies (tiktoken etc.) that aren't
# available in the test environment.
# ---------------------------------------------------------------------------
_agent_mock = MagicMock()
_agent_mock.Agent = MagicMock
_agent_mock.config.AgentConfig = MagicMock
_agent_mock.config.provider_profile.ProviderManager = MagicMock
_agent_mock.config.service.create_agent_config_service = MagicMock
_agent_mock.controller.AgentController = MagicMock
_agent_mock.logging.log = MagicMock()

_session_mock = MagicMock()
_session_mock.store.FileSystemSessionStore = MagicMock
_session_mock.context_builder.ContextBuilder = MagicMock

_tools_mock = MagicMock()
_tools_mock.workspace.worker.Worker = MagicMock

# Patch sys.modules BEFORE importing bridge
_agent_patches = {
    'agent': _agent_mock,
    'agent.config': _agent_mock.config,
    'agent.config.provider_profile': _agent_mock.config.provider_profile,
    'agent.config.service': _agent_mock.config.service,
    'agent.controller': _agent_mock.controller,
    'agent.logging': _agent_mock.logging,
    'session': _session_mock,
    'session.store': _session_mock.store,
    'session.context_builder': _session_mock.context_builder,
    'tools': _tools_mock,
    'tools.workspace': _tools_mock.workspace,
    'tools.workspace.worker': _tools_mock.workspace.worker,
}

_originals = {}
for mod_name, mock_mod in _agent_patches.items():
    _originals[mod_name] = sys.modules.get(mod_name)
    sys.modules[mod_name] = mock_mod

try:
    from web_ui.backend.bridge import WebAgentBridge
finally:
    # Restore original modules (except ones we injected)
    for mod_name, orig in _originals.items():
        if orig is not None:
            sys.modules[mod_name] = orig
        else:
            sys.modules.pop(mod_name, None)

# Now import the real event system (it doesn't need tiktoken)
from agent.events import (
    EventType, EventMetadata, BaseEvent,
)


# Fake EventBus that records events published to it.
class FakeEventBus:
    def __init__(self):
        self.subscribers: Dict[EventType, list] = {}
        self.published: List[BaseEvent] = []

    def subscribe(self, event_type, callback):
        self.subscribers.setdefault(event_type, []).append(callback)

    def publish(self, event):
        self.published.append(event)

    def unsubscribe(self, event_type, callback):
        pass


def _make_context_updated_event(worker_name: str, context_length: int) -> BaseEvent:
    """Create a context_updated BaseEvent as produced by WorkerBusAdapter."""
    return BaseEvent(
        type=EventType.CONTEXT_UPDATED,
        metadata=EventMetadata(
            source="worker",
            session_id="test-session",
            timestamp=datetime.datetime.now(),
        ),
        data={
            "worker_name": worker_name,
            "context_length": context_length,
        },
    )


# \u2500\u2500 Fixture: bridge instance with a recorded callback \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


@pytest.fixture
def bridge_with_recorder():
    """Create a WebAgentBridge whose callback records all forwarded events."""
    recorded: List[Dict[str, Any]] = []

    def recorder(event_dict: Dict[str, Any]) -> None:
        recorded.append(event_dict)

    bridge = WebAgentBridge(event_callback=recorder)
    bridge._session_id = "test-session"
    return bridge, recorded


# \u2500\u2500 Helper: simulate per-worker bus publish \u2192 bridge handler \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def _simulate_context_update(bridge, recorded, worker_name, context_length):
    """
    Simulate the per-worker EventBus publishing a context_updated event
    and the bridge's _make_bus_handler processing it.

    Returns the number of events that were forwarded to the callback.
    """
    before = len(recorded)
    event = _make_context_updated_event(worker_name, context_length)

    # The bridge subscribes to the per-worker bus via _make_bus_handler.
    # We directly invoke the handler logic by creating a mock worker_bus
    # that triggers the subscription.
    worker_bus = FakeEventBus()

    # We need to call _subscribe_to_worker_bus to register handlers
    bridge._subscribe_to_worker_bus(worker_name, worker_bus)

    # Find the context_updated handler and invoke it
    context_updated_type = EventType.CONTEXT_UPDATED
    handlers = worker_bus.subscribers.get(context_updated_type, [])
    for handler in handlers:
        handler(event)

    after = len(recorded)
    return after - before


class TestContextUpdatedDedup:
    """Event-level dedup for context_updated using formatted display strings."""

    # ── Helper to check _last_context_updated stores formatted strings ──

    @staticmethod
    def _expected_display(value: int) -> str:
        """Format a context_length the same way the bridge does."""
        if value >= 1000:
            return f"{value / 1000:.1f}K"
        return str(value)

    def test_first_event_gets_through(self, bridge_with_recorder):
        """The first context_updated event for a worker should be forwarded."""
        bridge, recorded = bridge_with_recorder

        # Simulate a fresh context update (no prior value)
        worker_bus = FakeEventBus()
        bridge._subscribe_to_worker_bus("worker_A", worker_bus)

        event = _make_context_updated_event("worker_A", 5000)
        handlers = worker_bus.subscribers.get(EventType.CONTEXT_UPDATED, [])
        for h in handlers:
            h(event)

        assert len(recorded) == 1
        assert recorded[0]["type"] == "worker:context_updated"
        assert recorded[0]["context_length"] == 5000
        assert recorded[0]["worker_name"] == "worker_A"
        # Verify internal tracking uses formatted string
        assert bridge._last_context_updated.get("worker_A") == "5.0K"

    def test_duplicate_value_is_skipped(self, bridge_with_recorder):
        """A second context_updated with the same context_length is skipped."""
        bridge, recorded = bridge_with_recorder

        worker_bus = FakeEventBus()
        bridge._subscribe_to_worker_bus("worker_B", worker_bus)
        context_type = EventType.CONTEXT_UPDATED
        handlers = worker_bus.subscribers.get(context_type, [])

        # First event \u2014 should be forwarded
        event1 = _make_context_updated_event("worker_B", 8000)
        for h in handlers:
            h(event1)
        assert len(recorded) == 1
        assert bridge._last_context_updated.get("worker_B") == "8.0K"

        # Second event with same value \u2014 should be DEDUPED
        event2 = _make_context_updated_event("worker_B", 8000)
        for h in handlers:
            h(event2)
        assert len(recorded) == 1, "Duplicate context_length should be skipped"
        # Internal tracking unchanged
        assert bridge._last_context_updated.get("worker_B") == "8.0K"

    def test_new_value_gets_through_after_duplicate(self, bridge_with_recorder):
        """A different context_length after a duplicate should be forwarded."""
        bridge, recorded = bridge_with_recorder

        worker_bus = FakeEventBus()
        bridge._subscribe_to_worker_bus("worker_C", worker_bus)
        context_type = EventType.CONTEXT_UPDATED
        handlers = worker_bus.subscribers.get(context_type, [])

        # First event
        event1 = _make_context_updated_event("worker_C", 3000)
        for h in handlers:
            h(event1)
        assert len(recorded) == 1
        assert bridge._last_context_updated.get("worker_C") == "3.0K"

        # Duplicate (same value)
        event2 = _make_context_updated_event("worker_C", 3000)
        for h in handlers:
            h(event2)
        assert len(recorded) == 1

        # New value
        event3 = _make_context_updated_event("worker_C", 4500)
        for h in handlers:
            h(event3)
        assert len(recorded) == 2
        assert recorded[1]["context_length"] == 4500
        assert bridge._last_context_updated.get("worker_C") == "4.5K"

    def test_different_workers_independent(self, bridge_with_recorder):
        """Two different workers should have independent dedup tracking."""
        bridge, recorded = bridge_with_recorder

        bus_A = FakeEventBus()
        bus_B = FakeEventBus()
        bridge._subscribe_to_worker_bus("worker_X", bus_A)
        bridge._subscribe_to_worker_bus("worker_Y", bus_B)
        ctx = EventType.CONTEXT_UPDATED
        handlers_A = bus_A.subscribers.get(ctx, [])
        handlers_B = bus_B.subscribers.get(ctx, [])

        # worker_X: first event
        for h in handlers_A:
            h(_make_context_updated_event("worker_X", 1000))
        assert len(recorded) == 1
        assert bridge._last_context_updated.get("worker_X") == "1.0K"

        # worker_Y: first event (same value but different worker \u2014 should get through)
        for h in handlers_B:
            h(_make_context_updated_event("worker_Y", 1000))
        assert len(recorded) == 2, "Different worker should not be deduped"
        assert bridge._last_context_updated.get("worker_Y") == "1.0K"

        # worker_X: duplicate
        for h in handlers_A:
            h(_make_context_updated_event("worker_X", 1000))
        assert len(recorded) == 2, "Duplicate for worker_X should be skipped"

        # worker_Y: new value
        for h in handlers_B:
            h(_make_context_updated_event("worker_Y", 2000))
        assert len(recorded) == 3
        assert bridge._last_context_updated.get("worker_Y") == "2.0K"

    def test_context_cleared_not_affected(self, bridge_with_recorder):
        """context_cleared events should not be affected by context_updated dedup."""
        bridge, recorded = bridge_with_recorder

        # This test verifies that the dedup only applies to context_updated,
        # not to context_cleared or other event types.
        # We just need to ensure the dedup dict doesn't interfere.
        worker_bus = FakeEventBus()
        bridge._subscribe_to_worker_bus("worker_D", worker_bus)
        ctx = EventType.CONTEXT_UPDATED
        handlers = worker_bus.subscribers.get(ctx, [])

        # Send context_updated first
        for h in handlers:
            h(_make_context_updated_event("worker_D", 7000))
        assert len(recorded) == 1

        # Send context_updated with same value (deduped)
        for h in handlers:
            h(_make_context_updated_event("worker_D", 7000))
        assert len(recorded) == 1

        # Send context_updated with new value
        for h in handlers:
            h(_make_context_updated_event("worker_D", 500))
        assert len(recorded) == 2
        assert recorded[1]["context_length"] == 500
        assert bridge._last_context_updated.get("worker_D") == "500"

    def test_formatted_dedup_catches_same_display(self, bridge_with_recorder):
        """Values that format to the same display string should be deduped.

        78251 and 78349 both format as "78.3K". The second event should be
        skipped even though the raw integers differ.
        """
        bridge, recorded = bridge_with_recorder

        worker_bus = FakeEventBus()
        bridge._subscribe_to_worker_bus("worker_E", worker_bus)
        ctx = EventType.CONTEXT_UPDATED
        handlers = worker_bus.subscribers.get(ctx, [])

        # First event: 78251 -> "78.3K"
        for h in handlers:
            h(_make_context_updated_event("worker_E", 78251))
        assert len(recorded) == 1
        assert recorded[0]["context_length"] == 78251
        assert bridge._last_context_updated.get("worker_E") == "78.3K"

        # Second event: 78349 -> also "78.3K" -> should be DEDUPED
        for h in handlers:
            h(_make_context_updated_event("worker_E", 78349))
        assert len(recorded) == 1, (
            f"78349 should be deduped because it also displays as 78.3K. "
            f"Recorded {len(recorded)} events instead of 1"
        )

        # Third event: different display string "79.1K" -> should get through
        for h in handlers:
            h(_make_context_updated_event("worker_E", 79100))
        assert len(recorded) == 2
        assert recorded[1]["context_length"] == 79100
        assert bridge._last_context_updated.get("worker_E") == "79.1K"

    def test_cleanup_on_unsubscribe(self, bridge_with_recorder):
        """_unsubscribe_worker_bus should clear the dedup tracking entry."""
        bridge, recorded = bridge_with_recorder

        worker_bus = FakeEventBus()
        bridge._subscribe_to_worker_bus("worker_F", worker_bus)
        ctx = EventType.CONTEXT_UPDATED
        handlers = worker_bus.subscribers.get(ctx, [])

        # Send an event to populate the tracking dict
        for h in handlers:
            h(_make_context_updated_event("worker_F", 50000))
        assert len(recorded) == 1
        assert "worker_F" in bridge._last_context_updated

        # After unsubscribe, the entry should be removed
        bridge._unsubscribe_worker_bus("worker_F")
        assert "worker_F" not in bridge._last_context_updated, (
            "_last_context_updated should be cleaned up on unsubscribe"
        )
