"""Tests for the EventLogger."""
from __future__ import annotations
import json
import os
import tempfile
import pytest
from agent.events import EventBus, BaseEvent, EventType, EventMetadata


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
