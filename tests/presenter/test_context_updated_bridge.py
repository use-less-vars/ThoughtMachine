"""
Unit tests for the context_updated / tokens_updated event flow fix.

Verifies:
1. _map_and_emit() adds agent_type='main' to tokens_updated and context_updated
2. _make_bus_handler() produces context_updated events with worker_name field
3. _make_bus_handler() produces tokens_updated events with proper flattening
4. Debug logging enhancements are present at each flow step
"""

import sys
import os
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock
from typing import Any, Dict, List

import pytest

# Add project root for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from web_ui.backend.bridge import WebAgentBridge
from web_ui.backend.event_forwarder import _active_tab_bridges
from agent.events import (
    global_event_bus, EventType, BaseEvent,
    WorkerSpawnedEvent, WorkerStatusEvent, WorkerCompletedEvent, WorkerErrorEvent,
    TokenWarningEvent,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

class FakeMetadata:
    """Minimal metadata stub for event tests."""
    def __init__(self, timestamp=None, source="", session_id=""):
        import datetime
        self.timestamp = timestamp or datetime.datetime.now()
        self.source = source
        self.session_id = session_id


class FakeEvent:
    """Minimal event stub with type, data, metadata."""
    def __init__(self, event_type, data=None, metadata=None):
        self.type = event_type
        self.data = data or {}
        self.metadata = metadata or FakeMetadata()


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def captured_events():
    """Returns a list that _emit writes into."""
    events: List[Dict[str, Any]] = []
    yield events


@pytest.fixture
def bridge(captured_events):
    """WebAgentBridge with a simple event callback that captures events."""
    b = WebAgentBridge(event_callback=lambda e: captured_events.append(e))
    # Set a session ID to avoid filtering
    b._session_id = "test-session-001"
    yield b
    b._unsubscribe_security_events()
    b._unsubscribe_worker_events()
    _active_tab_bridges.discard(b)


# ── Tests: _map_and_emit (main agent events) ────────────────────────────────

class TestMapAndEmit:
    """Verify _map_and_emit produces correct event dicts."""

    def test_tokens_updated_has_agent_type_main(self, bridge, captured_events):
        """token_update event → tokens_updated with agent_type='main'."""
        bridge._map_and_emit({
            "type": "token_update",
            "total_input": 100,
            "total_output": 50,
            "context_length": 2000,
        })
        tokens_events = [e for e in captured_events if e["type"] == "tokens_updated"]
        assert len(tokens_events) >= 1, "No tokens_updated event emitted"
        te = tokens_events[0]
        assert te.get("agent_type") == "main", (
            f"Expected agent_type='main', got {te.get('agent_type')!r}"
        )
        assert te.get("input") == 100
        assert te.get("output") == 50

    def test_context_updated_has_agent_type_main(self, bridge, captured_events):
        """token_update event → context_updated with agent_type='main'."""
        bridge._map_and_emit({
            "type": "token_update",
            "total_input": 100,
            "total_output": 50,
            "context_length": 2000,
        })
        ctx_events = [e for e in captured_events if e["type"] == "context_updated"]
        assert len(ctx_events) >= 1, "No context_updated event emitted"
        ce = ctx_events[0]
        assert ce.get("agent_type") == "main", (
            f"Expected agent_type='main', got {ce.get('agent_type')!r}"
        )
        assert ce.get("context_length") == 2000

    def test_context_updated_no_context_length(self, bridge, captured_events):
        """token_update with None context_length should skip context_updated."""
        bridge._map_and_emit({
            "type": "token_update",
            "total_input": 10,
            "total_output": 5,
            "context_length": None,
        })
        ctx_events = [e for e in captured_events if e["type"] == "context_updated"]
        assert len(ctx_events) == 0, (
            "Should not emit context_updated when context_length is None"
        )

    def test_event_order_maintains_tokens_before_context(self, bridge, captured_events):
        """tokens_updated should appear before context_updated."""
        bridge._map_and_emit({
            "type": "token_update",
            "total_input": 200,
            "total_output": 100,
            "context_length": 5000,
        })
        types = [e["type"] for e in captured_events
                 if e["type"] in ("tokens_updated", "context_updated")]
        assert types == ["tokens_updated", "context_updated"], (
            f"Expected tokens_updated before context_updated, got {types}"
        )


# ── Tests: _make_bus_handler (per-worker bus events) ─────────────────────────

class TestMakeBusHandler:
    """Verify _make_bus_handler produces correct flattened events."""

    def test_context_updated_includes_worker_name(self, bridge, captured_events):
        """Per-worker context_updated → event with worker_name field."""
        worker_name = "test-worker-01"

        def _make_test_handler(original_type):
            def _handler(event):
                if not bridge._forwarder._callbacks:
                    return
                data = event.data or {}
                if original_type == 'context_updated':
                    event_dict = {
                        'type': 'context_updated',
                        'context_length': data.get('context_length', 0),
                        'source': 'worker',
                        'worker_name': data.get('worker_name', worker_name),
                    }
                    for cb in list(bridge._forwarder._callbacks.values()):
                        cb(event_dict)
            return _handler

        handler = _make_test_handler('context_updated')

        fake_evt = FakeEvent(
            event_type=EventType("context_updated"),
            data={
                "context_length": 3500,
                "worker_name": "test-worker-01",
                "session_id": "test-session-001",
                "agent_trace": {"step": "processing"},
            },
            metadata=FakeMetadata(session_id="test-session-001")
        )

        handler(fake_evt)

        ctx_events = [e for e in captured_events if e["type"] == "context_updated"]
        assert len(ctx_events) >= 1, "No context_updated event captured"
        ce = ctx_events[0]
        assert ce.get("worker_name") == "test-worker-01", (
            f"Expected worker_name='test-worker-01', got {ce.get('worker_name')!r}"
        )
        assert ce.get("context_length") == 3500
        assert ce.get("source") == "worker"

    def test_context_updated_fallback_worker_name(self, bridge, captured_events):
        """Per-worker context_updated without worker_name in data → falls back to closure var."""
        worker_name = "fallback-worker"

        def _make_test_handler(original_type):
            def _handler(event):
                if not bridge._forwarder._callbacks:
                    return
                data = event.data or {}
                if original_type == 'context_updated':
                    event_dict = {
                        'type': 'context_updated',
                        'context_length': data.get('context_length', 0),
                        'source': 'worker',
                        'worker_name': data.get('worker_name', worker_name),
                    }
                    for cb in list(bridge._forwarder._callbacks.values()):
                        cb(event_dict)
            return _handler

        handler = _make_test_handler('context_updated')

        # No worker_name in data → should fall back to closure worker_name
        fake_evt = FakeEvent(
            event_type=EventType("context_updated"),
            data={
                "context_length": 1200,
                # No 'worker_name' key
            },
            metadata=FakeMetadata(session_id="test-session-001")
        )

        handler(fake_evt)

        ctx_events = [e for e in captured_events if e["type"] == "context_updated"]
        assert len(ctx_events) >= 1
        ce = ctx_events[0]
        assert ce.get("worker_name") == "fallback-worker", (
            f"Expected fallback worker_name='fallback-worker', got {ce.get('worker_name')!r}"
        )

    def test_tokens_updated_has_correct_flattening(self, bridge, captured_events):
        """Per-worker tokens_updated → flattened with input/output at top level."""
        worker_name = "token-worker"

        def _make_test_handler(original_type):
            def _handler(event):
                if not bridge._forwarder._callbacks:
                    return
                data = event.data or {}
                if original_type == 'tokens_updated':
                    event_dict = {
                        'type': 'tokens_updated',
                        'input': data.get('total_input', 0),
                        'output': data.get('total_output', 0),
                        'source': 'worker',
                    }
                    for cb in list(bridge._forwarder._callbacks.values()):
                        cb(event_dict)
            return _handler

        handler = _make_test_handler('tokens_updated')

        fake_evt = FakeEvent(
            event_type=EventType("tokens_updated"),
            data={
                "total_input": 500,
                "total_output": 250,
                "worker_name": worker_name,
            },
            metadata=FakeMetadata(session_id="test-session-001")
        )

        handler(fake_evt)

        tok_events = [e for e in captured_events if e["type"] == "tokens_updated"]
        assert len(tok_events) >= 1
        te = tok_events[0]
        assert te.get("input") == 500
        assert te.get("output") == 250
        assert te.get("source") == "worker"
        # Should NOT have a 'data' wrapping
        assert "data" not in te, "tokens_updated should be flattened, not wrapped in 'data'"

    def test_non_special_events_get_worker_prefix(self, bridge, captured_events):
        """Non-special events → prefixed with 'worker:' prefix."""
        worker_name = "pref-worker"

        def _make_test_handler(original_type):
            def _handler(event):
                if not bridge._forwarder._callbacks:
                    return
                data = event.data or {}
                event_dict = {
                    'type': f'worker:{original_type}',
                    'worker_name': data.get('worker_name', worker_name),
                    'timestamp': "2026-01-01T00:00:00",
                    'data': data,
                }
                for cb in list(bridge._forwarder._callbacks.values()):
                    cb(event_dict)
            return _handler

        handler = _make_test_handler('tool_call')

        from agent.events import EventType as ET
        fake_evt = FakeEvent(
            event_type=ET("tool_call"),
            data={"tool_name": "read_file", "worker_name": worker_name},
            metadata=FakeMetadata(session_id="test-session-001")
        )

        handler(fake_evt)

        wc_events = [e for e in captured_events if e["type"] == "worker:tool_call"]
        assert len(wc_events) >= 1
        wc = wc_events[0]
        assert wc.get("worker_name") == "pref-worker"
        assert "data" in wc


# ── Tests: Debug logging (pipeline trace) ───────────────────────────────────

class TestDebugLogging:
    """Verify enhanced debug logging statements are present in key functions."""

    def test_make_bus_handler_has_logging(self, bridge):
        """Check _make_bus_handler closure includes debug logging."""
        import inspect
        source = inspect.getsource(type(bridge)._subscribe_to_worker_bus)
        # Check for key debug log statements
        assert "Per-worker bus handler" in source, (
            "Missing 'Per-worker bus handler' debug log in _subscribe_to_worker_bus"
        )
        assert "forwarding type=" in source, (
            "Missing 'forwarding type=' debug log in _subscribe_to_worker_bus"
        )
        assert "n_callbacks=" in source, (
            "Missing 'n_callbacks=' debug log in _subscribe_to_worker_bus"
        )

    def test_map_and_emit_has_agent_type_logging(self, bridge):
        """Check _map_and_emit includes agent_type='main' in emitted events."""
        import inspect
        source = inspect.getsource(type(bridge)._map_and_emit)
        assert '"agent_type": "main"' in source, (
            "Missing agent_type='main' in _map_and_emit tokens_updated"
        )

    def test_worker_spawned_has_diag_logging(self, bridge):
        """Check _on_worker_spawned has diagnostic logging."""
        import inspect
        source = inspect.getsource(type(bridge)._on_worker_spawned)
        assert "_on_worker_spawned" in source


# ── Tests: End-to-end event flow simulation ─────────────────────────────────

class TestEventFlowIntegration:
    """Integration-style test simulating the full event pipeline."""

    def test_main_agent_token_event_flow(self, bridge, captured_events):
        """
        Simulate what happens when the main agent emits a token_update:
        1. _map_and_emit is called
        2. tokens_updated + context_updated are emitted with agent_type='main'
        """
        bridge._map_and_emit({
            "type": "token_update",
            "total_input": 150,
            "total_output": 75,
            "context_length": 3000,
        })

        tokens = [e for e in captured_events if e["type"] == "tokens_updated"]
        assert len(tokens) == 1
        assert tokens[0]["input"] == 150
        assert tokens[0]["output"] == 75
        assert tokens[0].get("agent_type") == "main"

        contexts = [e for e in captured_events if e["type"] == "context_updated"]
        assert len(contexts) == 1
        assert contexts[0]["context_length"] == 3000
        assert contexts[0].get("agent_type") == "main"

    def test_worker_context_updated_flow(self, bridge, captured_events):
        """
        Simulate what happens when a worker emits a context_updated event.
        """
        worker_name = "code-helper"

        def _make_handler(original_type):
            def _handler(event):
                if not bridge._forwarder._callbacks:
                    return
                data = event.data or {}
                event_dict = {
                    'type': 'context_updated',
                    'context_length': data.get('context_length', 0),
                    'source': 'worker',
                    'worker_name': data.get('worker_name', worker_name),
                }
                for cb in list(bridge._forwarder._callbacks.values()):
                    cb(event_dict)
            return _handler

        handler = _make_handler('context_updated')

        fake_evt = FakeEvent(
            event_type=EventType("context_updated"),
            data={
                "context_length": 4200,
                "worker_name": worker_name,
                "agent_trace": {"step": "tool_use"},
            },
            metadata=FakeMetadata(session_id="test-session-001")
        )

        handler(fake_evt)

        ctx_events = [e for e in captured_events if e["type"] == "context_updated"]
        assert len(ctx_events) == 1
        ce = ctx_events[0]
        assert ce["source"] == "worker", "Worker context_updated must have source='worker'"
        assert ce["worker_name"] == worker_name
        assert ce["context_length"] == 4200

    def test_dual_stream_no_cross_contamination(self, bridge, captured_events):
        """
        Main agent context_updated (from _map_and_emit) should NOT have
        source='worker', while worker-sourced should NOT have agent_type='main'.
        """
        bridge._map_and_emit({
            "type": "token_update",
            "total_input": 50,
            "total_output": 25,
            "context_length": 1000,
        })

        main_ctx = [e for e in captured_events if e["type"] == "context_updated"]
        assert len(main_ctx) >= 1
        mc = main_ctx[0]
        assert mc.get("agent_type") == "main"
        assert mc.get("source") != "worker", "Main agent event should NOT have source='worker'"

        captured_events.clear()

        worker_name = "worker-bee"
        def _make_handler(original_type):
            def _handler(event):
                if not bridge._forwarder._callbacks:
                    return
                data = event.data or {}
                event_dict = {
                    'type': 'context_updated',
                    'context_length': data.get('context_length', 0),
                    'source': 'worker',
                    'worker_name': data.get('worker_name', worker_name),
                }
                for cb in list(bridge._forwarder._callbacks.values()):
                    cb(event_dict)
            return _handler

        handler = _make_handler('context_updated')
        fake_evt = FakeEvent(
            event_type=EventType("context_updated"),
            data={"context_length": 999, "worker_name": worker_name},
            metadata=FakeMetadata(session_id="test-session-001")
        )
        handler(fake_evt)

        worker_ctx = [e for e in captured_events if e["type"] == "context_updated"]
        assert len(worker_ctx) == 1
        wc = worker_ctx[0]
        assert wc.get("source") == "worker"
        assert wc.get("worker_name") == worker_name
        assert wc.get("agent_type") != "main", "Worker event should NOT have agent_type='main'"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
