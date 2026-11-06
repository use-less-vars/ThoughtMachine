"""Tests for the EventLogger."""
from __future__ import annotations
import json
import os
import queue
import tempfile
import pytest
from agent.events import (
    EventBus,
    BaseEvent,
    EventType,
    EventMetadata,
    STREAM_ONLY_EVENT_TYPES,
    PERSISTED_EVENT_TYPES,
    is_stream_only_event_type,
)


class TestEventLogger:
    """Test EventLogger with a local EventBus."""

    @pytest.fixture
    def event_bus(self):
        return EventBus()

    @pytest.fixture
    def logger(self, event_bus):
        from agent.logging.event_logger import EventLogger
        # Reset singleton so each test gets a fresh instance
        EventLogger._instance = None
        tmpdir = tempfile.mkdtemp()
        log = EventLogger(workspace_path=tmpdir, event_bus=event_bus)
        yield log
        log.stop()
        # Cleanup
        if log.file_path and os.path.exists(log.file_path):
            os.remove(log.file_path)
        os.rmdir(tmpdir)

    def test_start_stop(self, logger):
        """Starting the logger should create the log file and subscribe."""
        assert not logger._subscribed
        logger.start()
        assert logger._subscribed
        assert logger.file_path is not None
        assert os.path.exists(logger.file_path)
        logger.stop()
        assert not logger._subscribed

    def test_event_written_to_file(self, event_bus, logger):
        """Published events should be written to the log file."""
        logger.start()
        event = BaseEvent(
            type=EventType.AGENT_START,
            metadata=EventMetadata(source="test"),
            data={"query": "hello", "config": {}},
        )
        event_bus.publish(event)
        logger.stop()
        with open(logger.file_path) as f:
            lines = f.readlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event_type"] == "agent_start"
        assert record["source"] == "test"
        assert record["data"]["query"] == "hello"

    def test_multiple_events(self, event_bus, logger):
        """Multiple events should all be recorded."""
        logger.start()
        for i in range(5):
            event = BaseEvent(
                type=EventType.AGENT_START if i % 2 == 0 else EventType.AGENT_END,
                metadata=EventMetadata(source=f"src{i}"),
                data={"index": i},
            )
            event_bus.publish(event)
        logger.stop()
        with open(logger.file_path) as f:
            lines = f.readlines()
        assert len(lines) == 5

    def test_logger_context_manager(self, event_bus):
        """Context manager should start and stop properly."""
        from agent.logging.event_logger import EventLogger
        EventLogger._instance = None
        tmpdir = tempfile.mkdtemp()
        with EventLogger(workspace_path=tmpdir, event_bus=event_bus) as log:
            assert log._subscribed
            event = BaseEvent(
                type=EventType.TOOL_CALL,
                metadata=EventMetadata(source="ctx"),
                data={"tool_name": "test", "arguments": {}},
            )
            event_bus.publish(event)
        assert not log._subscribed
        with open(log.file_path) as f:
            lines = f.readlines()
        assert len(lines) == 1
        os.remove(log.file_path)
        os.rmdir(tmpdir)

    def test_new_event_types_work(self, event_bus, logger):
        """The new WEBSOCKET_CONNECT, WEBSOCKET_DISCONNECT, CONFIG_LOADED event types should work."""
        logger.start()
        events = [
            BaseEvent(type=EventType.WEBSOCKET_CONNECT, metadata=EventMetadata(source="ws"), data={"connection_id": "1"}),
            BaseEvent(type=EventType.WEBSOCKET_DISCONNECT, metadata=EventMetadata(source="ws"), data={"connection_id": "1"}),
            BaseEvent(type=EventType.CONFIG_LOADED, metadata=EventMetadata(source="config"), data={"path": "/etc/config.yaml"}),
        ]
        for ev in events:
            event_bus.publish(ev)
        logger.stop()
        with open(logger.file_path) as f:
            lines = f.readlines()
        assert len(lines) == 3
        types = [json.loads(l)["event_type"] for l in lines]
        assert "websocket_connect" in types
        assert "websocket_disconnect" in types
        assert "config_loaded" in types

    def test_async_queue_drain(self, event_bus, logger):
        """Events should be drained asynchronously by the writer thread without calling stop()."""
        logger.start()
        # Fire 10 events rapidly
        for i in range(10):
            event = BaseEvent(
                type=EventType.TOOL_CALL if i % 2 == 0 else EventType.TOOL_RESULT,
                metadata=EventMetadata(source=f"async_test_{i}"),
                data={"index": i, "payload": f"value_{i}"},
            )
            event_bus.publish(event)

        # Wait briefly for the background writer thread to drain the queue
        import time
        time.sleep(2.0)

        # Read the log file WITHOUT stopping the logger first
        # This proves the async writer is working in real-time
        with open(logger.file_path) as f:
            lines = f.readlines()

        assert len(lines) == 10, f"Expected 10 events, got {len(lines)}. Writer thread did not drain asynchronously."

        # Verify content
        for i, line in enumerate(lines):
            record = json.loads(line)
            assert record["data"]["index"] == i
            assert record["source"] == f"async_test_{i}"

        # Clean up
        logger.stop()

    def test_create_event_with_new_types(self):
        """create_event() should work with the new event types."""
        from agent.events import create_event
        ev = create_event(EventType.WEBSOCKET_CONNECT, {"connection_id": "42"}, source="test")
        assert ev.type == EventType.WEBSOCKET_CONNECT
        assert ev.data["connection_id"] == "42"
        ev2 = create_event(EventType.CONFIG_LOADED, {"path": "/tmp/cfg"}, source="test")
        assert ev2.type == EventType.CONFIG_LOADED

    def test_stream_only_event_types_holds_the_deltas(self):
        """STREAM_ONLY_EVENT_TYPES must name exactly the per-token stream deltas."""
        assert STREAM_ONLY_EVENT_TYPES == {EventType.TEXT_DELTA, EventType.REASONING_DELTA}

    def test_normal_event_types_are_not_excluded(self):
        """Guard: real history types must never be widened into the exclusion."""
        for event_type in (
            EventType.TOOL_CALL,
            EventType.TOOL_RESULT,
            EventType.TURN,
            EventType.AGENT_START,
            EventType.AGENT_END,
            EventType.ASSISTANT_MESSAGE,
        ):
            assert event_type not in STREAM_ONLY_EVENT_TYPES

    def test_event_type_classification_is_total(self):
        """Every EventType must land in exactly one of the two classification sets.

        Persistence is opt-out, so without this guard a newly added per-token
        event type would silently reopen the session.log firehose (or, worse, a
        real history type could be dropped by a careless widening of
        STREAM_ONLY_EVENT_TYPES).
        """
        all_types = set(EventType)
        unclassified = all_types - PERSISTED_EVENT_TYPES - STREAM_ONLY_EVENT_TYPES
        assert not unclassified, (
            "unclassified EventType members: "
            f"{sorted(t.name for t in unclassified)} -- add them to "
            "PERSISTED_EVENT_TYPES in agent/events.py unless they are "
            "per-token stream transport"
        )
        assert not (PERSISTED_EVENT_TYPES & STREAM_ONLY_EVENT_TYPES), (
            "an event type cannot be both persisted and stream-only"
        )
        assert PERSISTED_EVENT_TYPES == all_types - STREAM_ONLY_EVENT_TYPES

    def test_is_stream_only_event_type_accepts_enum_and_string(self):
        """The shared classifier must answer for enum members and string values."""
        assert is_stream_only_event_type(EventType.TEXT_DELTA) is True
        assert is_stream_only_event_type(EventType.REASONING_DELTA) is True
        assert is_stream_only_event_type("text_delta") is True
        assert is_stream_only_event_type("reasoning_delta") is True
        assert is_stream_only_event_type(EventType.TURN) is False
        assert is_stream_only_event_type("tool_call") is False
        # Unknown/empty names fail open to "persist": history is never dropped
        # just because a name could not be classified.
        assert is_stream_only_event_type(None) is False
        assert is_stream_only_event_type("") is False
        assert is_stream_only_event_type("not_a_real_event") is False

    def test_stream_deltas_are_not_persisted(self, event_bus, logger):
        """Per-token deltas must not reach event_log.jsonl; normal events still do."""
        logger.start()
        event_bus.publish(BaseEvent(
            type=EventType.TEXT_DELTA,
            metadata=EventMetadata(source="stream"),
            data={"text": "hi", "turn": 1},
        ))
        event_bus.publish(BaseEvent(
            type=EventType.REASONING_DELTA,
            metadata=EventMetadata(source="stream"),
            data={"text": "thinking", "turn": 1},
        ))
        event_bus.publish(BaseEvent(
            type=EventType.TOOL_CALL,
            metadata=EventMetadata(source="tool"),
            data={"tool_name": "test", "arguments": {}},
        ))
        logger.stop()
        with open(logger.file_path) as f:
            lines = f.readlines()
        assert [json.loads(line)["event_type"] for line in lines] == ["tool_call"]


class TestControllerEmitEventPersistence:
    """Controller._emit_event keeps streaming deltas off session.log.

    The global event bus is what live UI streaming depends on, so deltas must
    still be published there; only the session.log sink skips them.
    """

    @pytest.fixture
    def bus(self, monkeypatch):
        from agent import events as events_module
        local_bus = EventBus()
        monkeypatch.setattr(events_module, "global_event_bus", local_bus)
        return local_bus

    @pytest.fixture
    def persisted(self, monkeypatch):
        from agent.logging import lifecycle
        records = []

        def _record(event_type, session_id="", data=None):
            records.append((event_type, session_id, data))

        monkeypatch.setattr(lifecycle, "log_session_event", _record)
        return records

    @pytest.fixture
    def controller(self):
        from agent.controller import AgentController
        ctrl = AgentController.__new__(AgentController)
        ctrl.current_session_id = "sess-test"
        ctrl.event_queue = queue.Queue()
        ctrl._event_callbacks = []
        return ctrl

    def test_stream_delta_reaches_bus_but_is_not_persisted(self, controller, bus, persisted):
        published = []
        bus.subscribe(None, published.append)
        controller._emit_event({"type": "text_delta", "text": "hi", "turn": 1})
        # UI streaming intact: the delta is still on the global event bus...
        assert [event.type for event in published] == [EventType.TEXT_DELTA]
        # ...but it never touches session.log.
        assert persisted == []

    def test_reasoning_delta_is_not_persisted(self, controller, bus, persisted):
        published = []
        bus.subscribe(None, published.append)
        controller._emit_event({"type": "reasoning_delta", "text": "thinking", "turn": 1})
        assert [event.type for event in published] == [EventType.REASONING_DELTA]
        assert persisted == []

    def test_normal_event_is_still_persisted(self, controller, bus, persisted):
        published = []
        bus.subscribe(None, published.append)
        controller._emit_event({"type": "turn", "turn": 2})
        assert [event.type for event in published] == [EventType.TURN]
        assert [record[0] for record in persisted] == ["turn"]
        assert persisted[0][1] == "sess-test"
