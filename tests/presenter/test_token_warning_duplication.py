"""
Test: token_warning duplication suppression in AgentState.

Verifies that update_token_state() emits exactly ONE token_warning event
across a realistic sequence of token counts that cross thresholds multiple
times, ensuring the _token_warning_has_fired flag works correctly.

Sequence: 50k → 68k → 72k → 85k → 64k → 68k
Thresholds: warning=65k, critical=80k

Expected:
  50k → LOW              (no event, below warning)
  68k → WARNING          (1st event: LOW→WARNING fires)
  72k → WARNING          (no event: no state change)
  85k → CRITICAL         (no event: _token_warning_has_fired is True)
  64k → LOW              (no event: state_order decreases)
  68k → WARNING          (no event: _token_warning_has_fired is True)
Total: exactly 1 warning event.
"""

import pytest
from unittest.mock import MagicMock

from agent.core.state import AgentState, TokenState, TurnState, TimeState, ExecutionState, SessionState
from agent.config.models import AgentConfig


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def agent_config():
    """Return an AgentConfig with default token thresholds (65k warning, 80k critical)."""
    return AgentConfig(
        api_key="test-key",
        provider_type="openai_compatible",
        model="gpt-4",
        base_url="https://api.openai.com/v1",
        token_monitor_warning_threshold=65000,
        token_monitor_critical_threshold=80000,
        max_turns=100,
        turn_monitor_enabled=False,   # irrelevant to this test
        time_monitor_enabled=False,   # irrelevant to this test
        workspace_path="/tmp",
    )


@pytest.fixture
def agent_state(agent_config):
    """Return a fresh AgentState with all warning flags reset."""
    return AgentState(
        config=agent_config,
        logger=None,
        token_state=TokenState.LOW,
        turn_state=TurnState.LOW,
        time_state=TimeState.LOW,
        execution_state=ExecutionState.READY,
        session_state=SessionState.NEW,
        _token_warning_has_fired=False,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestTokenWarningDuplication:
    """AgentState must emit exactly one token_warning across multiple threshold crossings."""

    def test_single_warning_across_oscillating_sequence(self, agent_state):
        """
        Given a sequence of token counts that oscillate above and below thresholds,
        exactly one token_warning event is emitted — the first time the state
        transitions upward into warning or critical territory.

        Sequence: 50k (LOW) → 68k (WARNING) → 72k (WARNING) → 85k (CRITICAL)
                  → 64k (LOW) → 68k (WARNING)
        """
        sequence = [50_000, 68_000, 72_000, 85_000, 64_000, 68_000]

        all_events = []

        for idx, tokens in enumerate(sequence):
            events = agent_state.update_token_state(tokens)
            all_events.extend(events)

            if events:
                assert len(events) == 1, (
                    f"Expected exactly 0 or 1 event per update_token_state() call; "
                    f"got {len(events)} at index {idx} (tokens={tokens})"
                )
                evt = events[0]
                assert evt["type"] in ("token_warning", "token_recovery"), (
                    f"Expected event type 'token_warning' or 'token_recovery', got '{evt.get('type')}' "
                    f"at index {idx} (tokens={tokens})"
                )

        # ── Assert exactly 1 event total ──
        assert len(all_events) == 1, (
            f"Expected exactly 1 token_warning event across the entire sequence, "
            f"got {len(all_events)}"
        )

        # ── Assert the single event fired at the correct index ──
        warning_tokens = all_events[0]["token_count"]
        assert warning_tokens == 68_000, (
            f"The single warning should fire at 68k (first LOW→WARNING), "
            f"but token_count is {warning_tokens}"
        )

        # ── Assert specific event fields are present ──
        evt = all_events[0]
        assert evt["old_state"] == "low"
        assert evt["new_state"] == "warning"
        assert "warning_message" in evt
        assert evt["state"] == "warning"

    def test_no_warning_when_below_threshold(self, agent_state):
        """Token counts entirely below the warning threshold produce no events."""
        for tokens in [10_000, 30_000, 50_000, 64_999]:
            events = agent_state.update_token_state(tokens)
            assert len(events) == 0, (
                f"Expected 0 events for {tokens} tokens (below 65k threshold), "
                f"got {len(events)}"
            )

    def test_warning_fires_at_first_crossing_from_clean_state(self, agent_state):
        """A clean state fires exactly once when crossing from LOW→WARNING."""
        events = agent_state.update_token_state(68_000)
        assert len(events) == 1
        assert events[0]["type"] == "token_warning"
        assert events[0]["new_state"] == "warning"
        assert events[0]["token_count"] == 68_000

        # Second crossing (regardless of direction) should NOT fire
        events2 = agent_state.update_token_state(90_000)  # WARNING→CRITICAL
        assert len(events2) == 0, "_token_warning_has_fired should suppress this"

    def test_rapid_re_entry_after_drop_does_not_fire_again(self, agent_state):
        """After firing, dropping below warning and re-entering is suppressed."""
        # First crossing
        agent_state.update_token_state(70_000)  # fires
        assert agent_state._token_warning_has_fired is True

        # Drop below warning
        agent_state.update_token_state(50_000)  # no event
        assert agent_state.token_state == TokenState.LOW

        # Re-enter warning — should NOT fire again
        events = agent_state.update_token_state(70_000)
        assert len(events) == 0, (
            "Re-entering warning after drop should NOT fire a second warning "
            "because _token_warning_has_fired is True"
        )

    def test_internal_warning_flag_not_reset_by_reset_method(self, agent_state):
        """
        The _token_warning_has_fired flag is True after a warning fires.
        reset() clears it back to False so a new session starts clean.
        """
        agent_state.update_token_state(70_000)
        assert agent_state._token_warning_has_fired is True

        # reset() should clear the flag
        agent_state.reset()
        assert agent_state._token_warning_has_fired is False
        assert agent_state.token_state == TokenState.LOW

        # Now a fresh warning should fire again
        events = agent_state.update_token_state(70_000)
        assert len(events) == 1, "After reset(), a fresh warning should fire"

    def test_warning_survives_critical_to_low_transition(self, agent_state):
        """
        If the first crossing is directly to CRITICAL (bypassing WARNING),
        the flag is still set and subsequent crossings are suppressed.
        """
        import warnings as _w

        with _w.catch_warnings(record=True) as w:
            _w.simplefilter("always")

            events = agent_state.update_token_state(85_000)  # LOW→CRITICAL
            assert len(events) == 1
            assert events[0]["new_state"] == "critical"
            assert agent_state._token_warning_has_fired is True

            # Drop to LOW
            events2 = agent_state.update_token_state(50_000)
            assert len(events2) == 0
            assert agent_state.token_state == TokenState.LOW

            # Re-enter WARNING → suppressed
            events3 = agent_state.update_token_state(70_000)
            assert len(events3) == 0, (
                "Even after CRITICAL→LOW→WARNING, "
                "_token_warning_has_fired prevents a second warning"
            )
