"""
Tests for worker_state_sync event emitted by WorkerBusAdapter.emit_state_sync().

Verifies that:
- Event type is 'worker_state_sync'
- context_length is an integer
- token_state is one of 'LOW', 'WARNING', 'CRITICAL'
- worker_name matches the adapter's worker_name
- warning_message and critical_threshold are correctly propagated
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from tools.workspace.worker import WorkerBusAdapter


class TestWorkerStateSync:
    """Test suite for WorkerBusAdapter.emit_state_sync()."""

    @pytest.fixture
    def mock_event_bus(self):
        """Create a mock EventBus that records published events."""
        bus = MagicMock()
        bus.publish = MagicMock()
        return bus

    @pytest.fixture
    def adapter(self, mock_event_bus):
        """Create a WorkerBusAdapter with a mock EventBus."""
        return WorkerBusAdapter(event_bus=mock_event_bus, worker_name="test_worker")

    def _get_published_event(self, mock_event_bus):
        """Extract the most recently published event from the mock bus."""
        assert mock_event_bus.publish.called, "No event was published"
        return mock_event_bus.publish.call_args[0][0]

    # ── Basic field assertions ─────────────────────────────────────

    def test_emit_state_sync_type(self, adapter, mock_event_bus):
        """The published event type must be 'worker_state_sync'."""
        adapter.emit_state_sync(context_length=100)
        event = self._get_published_event(mock_event_bus)
        assert event.type.value == "worker_state_sync"

    def test_emit_state_sync_context_length_is_number(self, adapter, mock_event_bus):
        """context_length must be a plain integer (not total_input)."""
        adapter.emit_state_sync(context_length=15000)
        event = self._get_published_event(mock_event_bus)
        assert isinstance(event.data["context_length"], int)
        assert event.data["context_length"] == 15000

    def test_emit_state_sync_context_length_zero_by_default(self, adapter, mock_event_bus):
        """Default context_length should be 0."""
        adapter.emit_state_sync()
        event = self._get_published_event(mock_event_bus)
        assert event.data["context_length"] == 0

    def test_emit_state_sync_token_state_valid(self, adapter, mock_event_bus):
        """token_state must be one of 'LOW', 'WARNING', 'CRITICAL'."""
        valid_states = ["LOW", "WARNING", "CRITICAL"]
        for state in valid_states:
            mock_event_bus.reset_mock()
            adapter.emit_state_sync(context_length=100, token_state=state)
            event = self._get_published_event(mock_event_bus)
            assert event.data["token_state"] in valid_states, \
                f"token_state={event.data['token_state']!r} not in {valid_states}"

    def test_emit_state_sync_default_token_state_low(self, adapter, mock_event_bus):
        """Default token_state should be 'LOW'."""
        adapter.emit_state_sync()
        event = self._get_published_event(mock_event_bus)
        assert event.data["token_state"] == "LOW"

    def test_emit_state_sync_worker_name(self, adapter, mock_event_bus):
        """worker_name must match the adapter's worker_name."""
        adapter.emit_state_sync()
        event = self._get_published_event(mock_event_bus)
        assert event.data["worker_name"] == "test_worker"

    # ── Warning message propagation ────────────────────────────────

    def test_emit_state_sync_warning_message(self, adapter, mock_event_bus):
        """warning_message must be propagated correctly."""
        adapter.emit_state_sync(
            context_length=120000,
            token_state="WARNING",
            warning_message="Token count 120000 approaching limit",
            critical_threshold=100000,
        )
        event = self._get_published_event(mock_event_bus)
        assert event.data["warning_message"] == "Token count 120000 approaching limit"
        assert event.data["critical_threshold"] == 100000

    def test_emit_state_sync_warning_message_empty_by_default(self, adapter, mock_event_bus):
        """Default warning_message should be empty string."""
        adapter.emit_state_sync()
        event = self._get_published_event(mock_event_bus)
        assert event.data["warning_message"] == ""

    # ── Full payload structure ─────────────────────────────────────

    def test_emit_state_sync_all_fields(self, adapter, mock_event_bus):
        """All expected fields must be present in the published data."""
        adapter.emit_state_sync(
            context_length=50000,
            token_state="WARNING",
            warning_message="Approaching context limit",
            critical_threshold=80000,
        )
        event = self._get_published_event(mock_event_bus)
        data = event.data
        assert "context_length" in data
        assert "token_state" in data
        assert "warning_message" in data
        assert "critical_threshold" in data
        assert data["context_length"] == 50000
        assert data["token_state"] == "WARNING"
        assert data["warning_message"] == "Approaching context limit"
        assert data["critical_threshold"] == 80000

    # ── CRITICAL state ─────────────────────────────────────────────

    def test_emit_state_sync_critical_state(self, adapter, mock_event_bus):
        """CRITICAL token_state must be settable."""
        adapter.emit_state_sync(
            context_length=200000,
            token_state="CRITICAL",
            warning_message="Context limit critical!",
            critical_threshold=150000,
        )
        event = self._get_published_event(mock_event_bus)
        assert event.data["token_state"] == "CRITICAL"
        assert event.data["warning_message"] == "Context limit critical!"
        assert event.data["critical_threshold"] == 150000

    # ── Serialization (JSON round-trip) ────────────────────────────

    def test_emit_state_sync_json_serializable(self, adapter, mock_event_bus):
        """Event data must be JSON-serializable."""
        adapter.emit_state_sync(
            context_length=50000,
            token_state="WARNING",
            warning_message="Test warning",
            critical_threshold=80000,
        )
        event = self._get_published_event(mock_event_bus)
        # Should not raise TypeError
        json_str = json.dumps(event.data, default=str)
        parsed = json.loads(json_str)
        assert parsed["context_length"] == 50000
        assert parsed["token_state"] == "WARNING"
        assert parsed["warning_message"] == "Test warning"
        assert parsed["critical_threshold"] == 80000
