"""
test_state_timeout.py — Tests for timeout soft-restriction changes in AgentState.

Tests:
1. test_get_allowed_tools_timeout_returns_only_respond
2. test_restriction_reason_set_on_timeout
3. test_restriction_reason_set_on_token_critical
4. test_restriction_reason_set_on_turn_warning
5. test_restriction_reason_cleared_when_state_returns_to_low
6. test_timeout_warning_message_updated
7. test_get_allowed_tokens_returns_respond_and_summarize_for_non_timeout
8. test_restriction_reason_cleared_on_reset

Run with::

    pytest tests/test_state_timeout.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent.core.state import AgentState, TokenState, TurnState, TimeState


# ════════════════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def state() -> AgentState:
    """Fresh AgentState with a mock logger."""
    s = AgentState(logger=MagicMock())
    s.reset()
    return s


# ════════════════════════════════════════════════════════════════════════════
# Tests
# ════════════════════════════════════════════════════════════════════════════

class TestGetAllowedTools:
    """Verify get_allowed_tools() returns the correct tool lists."""

    def test_get_allowed_tools_timeout_returns_only_respond(self, state: AgentState):
        """When restriction_reason is 'timeout', only 'Respond' should be allowed."""
        state.restrictions_active = True
        state.restriction_reason = 'timeout'
        allowed = state.get_allowed_tools()
        assert allowed == ['Respond'], f"Expected ['Respond'], got {allowed}"

    def test_get_allowed_tools_token_critical_returns_respond_and_summarize(self, state: AgentState):
        """When restriction_reason is 'token', both Respond and SummarizeTool should be allowed."""
        state.restrictions_active = True
        state.restriction_reason = 'token'
        allowed = state.get_allowed_tools()
        assert allowed == ['Respond', 'SummarizeTool'], f"Expected ['Respond', 'SummarizeTool'], got {allowed}"

    def test_get_allowed_tools_turn_warning_returns_respond_and_summarize(self, state: AgentState):
        """When restriction_reason is 'turn', both Respond and SummarizeTool should be allowed."""
        state.restrictions_active = True
        state.restriction_reason = 'turn'
        allowed = state.get_allowed_tools()
        assert allowed == ['Respond', 'SummarizeTool'], f"Expected ['Respond', 'SummarizeTool'], got {allowed}"

    def test_get_allowed_tools_no_restrictions_returns_empty(self, state: AgentState):
        """When restrictions are not active, get_allowed_tools() should return []."""
        state.restrictions_active = False
        state.restriction_reason = None
        allowed = state.get_allowed_tools()
        assert allowed == [], f"Expected [], got {allowed}"


class TestRestrictionReason:
    """Verify restriction_reason is set and cleared correctly."""

    def test_restriction_reason_set_on_timeout_critical(self, state: AgentState):
        """Simulate timeout CRITICAL — restriction_reason should be 'timeout'."""
        state.time_start = 0.0
        state.timeout_seconds = 1
        state.time_warning_threshold = 0
        events = state.update_time_state(999.0)  # far beyond timeout
        assert state.restriction_reason == 'timeout', (
            f"Expected 'timeout', got {state.restriction_reason}"
        )
        assert state.restrictions_active is True
        # Should have generated a time_warning event
        time_warnings = [e for e in events if e['type'] == 'time_warning']
        assert len(time_warnings) > 0

    def test_restriction_reason_set_on_token_critical(self, state: AgentState):
        """Token CRITICAL state should set restriction_reason to 'token'."""
        state.current_conversation_tokens = 1000000
        state.max_conversation_tokens = 1000
        state.token_warning_threshold = 500
        events = state.update_token_state()
        assert state.restriction_reason == 'token', (
            f"Expected 'token', got {state.restriction_reason}"
        )
        assert state.restrictions_active is True

    def test_restriction_reason_set_on_turn_warning(self, state: AgentState):
        """Turn WARNING state should set restriction_reason to 'turn'."""
        state.current_turn = 18
        state.max_turns = 20
        state.turn_warning_threshold = 15
        events = state.update_turn_state(state.current_turn)
        assert state.restriction_reason == 'turn', (
            f"Expected 'turn', got {state.restriction_reason}"
        )
        assert state.restrictions_active is True

    def test_restriction_reason_cleared_when_time_returns_to_low(self, state: AgentState):
        """When time state goes back to LOW, restriction_reason should be cleared."""
        # First trigger CRITICAL
        state.time_start = 0.0
        state.timeout_seconds = 1
        state.time_warning_threshold = 0
        state.update_time_state(999.0)
        assert state.restriction_reason == 'timeout'

        # Now simulate time returning to LOW (reset)
        state.time_start = None
        state.update_time_state(0.0)
        assert state.restriction_reason is None, (
            f"Expected None after time returns to LOW, got {state.restriction_reason}"
        )
        assert state.restrictions_active is False

    def test_restriction_reason_cleared_when_token_returns_to_low(self, state: AgentState):
        """When token state goes back to LOW, restriction_reason should be cleared."""
        # First trigger CRITICAL
        state.current_conversation_tokens = 1000000
        state.max_conversation_tokens = 1000
        state.token_warning_threshold = 500
        state.update_token_state()
        assert state.restriction_reason == 'token'

        # Now reduce tokens below threshold
        state.current_conversation_tokens = 100
        state.update_token_state()
        assert state.restriction_reason is None, (
            f"Expected None after tokens return to LOW, got {state.restriction_reason}"
        )

    def test_restriction_reason_cleared_when_turn_returns_to_low(self, state: AgentState):
        """When turn state goes back to LOW, restriction_reason should be cleared."""
        # First trigger WARNING
        state.current_turn = 18
        state.max_turns = 20
        state.turn_warning_threshold = 15
        state.update_turn_state(state.current_turn)
        assert state.restriction_reason == 'turn'

        # Now reset turn count
        state.current_turn = 1
        state.update_turn_state(state.current_turn)
        assert state.restriction_reason is None, (
            f"Expected None after turns return to LOW, got {state.restriction_reason}"
        )

    def test_restriction_reason_cleared_on_reset(self, state: AgentState):
        """reset() should clear restriction_reason."""
        state.restrictions_active = True
        state.restriction_reason = 'timeout'
        state.reset()
        assert state.restriction_reason is None
        assert state.restrictions_active is False


class TestTimeoutWarningMessage:
    """Verify the timeout warning messages contain the expected text."""

    def test_timeout_warning_message_contains_restriction_hint(self):
        """The time warning message should mention tool restrictions."""
        state = AgentState(logger=MagicMock())
        state.reset()
        state.time_start = 0.0
        state.timeout_seconds = 300
        state.time_warning_threshold = 240
        state.last_time_warning_state = None

        # Trigger WARNING state (elapsed < timeout but > threshold)
        events = state.update_time_state(250.0)
        warning_events = [e for e in events if e['type'] == 'time_warning']
        assert len(warning_events) > 0
        msg = warning_events[0].get('message', '')
        assert 'Tool restrictions will be applied' in msg, (
            f"Warning message should mention restrictions. Got: {msg}"
        )

    def test_timeout_critical_message_contains_only_respond_hint(self):
        """The time critical message should mention only Respond is available."""
        state = AgentState(logger=MagicMock())
        state.reset()
        state.time_start = 0.0
        state.timeout_seconds = 300
        state.time_warning_threshold = 240
        state.last_time_warning_state = None

        # Trigger CRITICAL state (elapsed > timeout)
        events = state.update_time_state(301.0)
        warning_events = [e for e in events if e['type'] == 'time_warning']
        assert len(warning_events) > 0
        msg = warning_events[0].get('message', '')
        assert 'Only the Respond tool is available' in msg, (
            f"Critical message should mention only Respond. Got: {msg}"
        )
        assert 'Please finish your work and respond immediately' in msg, (
            f"Critical message should ask to finish work. Got: {msg}"
        )
