"""
Test: token_warning event emission in AgentState.

Verifies that update_token_state() emits events correctly:
- token_warning on LOW→WARNING (unless already fired)
- token_warning on WARNING→CRITICAL (independent of WARNING flag)
- token_recovery on CRITICAL/WARNING→LOW (resets warning flag)
- After recovery, a fresh LOW→WARNING can fire again

Sequence: 50k → 68k → 72k → 85k → 64k → 68k
Thresholds: warning=65k, critical=80k

Expected:
  50k → LOW              (no event, below warning)
  68k → WARNING          (event 1: LOW→WARNING fires)
  72k → WARNING          (no event: no state change)
  85k → CRITICAL         (event 2: WARNING→CRITICAL fires independently)
  64k → LOW              (event 3: CRITICAL→LOW fires token_recovery)
  68k → WARNING          (event 4: LOW→WARNING fires again after recovery reset)
Total: exactly 4 events.
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
    """AgentState must emit token_warning and token_recovery events correctly."""

    def test_single_warning_across_oscillating_sequence(self, agent_state):
        """
        Given a sequence of token counts that oscillate above and below thresholds,
        the state machine emits events correctly: one warning when entering warning
        territory, one critical when entering critical territory, one recovery when
        dropping back to LOW, and another warning when re-entering after recovery.

        Sequence: 50k (LOW) → 68k (WARNING) → 72k (WARNING) → 85k (CRITICAL)
                  → 64k (LOW) → 68k (WARNING)

        Expected events:
          1. token_warning   at 68k (LOW→WARNING)
          2. token_warning   at 85k (WARNING→CRITICAL)
          3. token_recovery  at 64k (CRITICAL→LOW)
          4. token_warning   at 68k (LOW→WARNING, after recovery)
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

        # ── Assert exactly 4 events total ──
        assert len(all_events) == 4, (
            f"Expected exactly 4 events across the entire sequence, "
            f"got {len(all_events)}"
        )

        # ── Event 1: token_warning at 68k (LOW→WARNING) ──
        evt1 = all_events[0]
        assert evt1["type"] == "token_warning"
        assert evt1["old_state"] == "low"
        assert evt1["new_state"] == "warning"
        assert evt1["token_count"] == 68_000
        assert "warning_message" in evt1
        assert evt1["state"] == "warning"

        # ── Event 2: token_warning at 85k (WARNING→CRITICAL) ──
        evt2 = all_events[1]
        assert evt2["type"] == "token_warning"
        assert evt2["old_state"] == "warning"
        assert evt2["new_state"] == "critical"
        assert evt2["token_count"] == 85_000
        assert "warning_message" in evt2
        assert evt2["state"] == "critical"

        # ── Event 3: token_recovery at 64k (CRITICAL→LOW) ──
        evt3 = all_events[2]
        assert evt3["type"] == "token_recovery"
        assert evt3["old_state"] == "critical"
        assert evt3["new_state"] == "low"
        assert evt3["token_count"] == 64_000
        assert "recovery_message" in evt3

        # ── Event 4: token_warning at 68k (LOW→WARNING, after recovery reset) ──
        evt4 = all_events[3]
        assert evt4["type"] == "token_warning"
        assert evt4["old_state"] == "low"
        assert evt4["new_state"] == "warning"
        assert evt4["token_count"] == 68_000
        assert "warning_message" in evt4
        assert evt4["state"] == "warning"

    def test_no_warning_when_below_threshold(self, agent_state):
        """Token counts entirely below the warning threshold produce no events."""
        for tokens in [10_000, 30_000, 50_000, 64_999]:
            events = agent_state.update_token_state(tokens)
            assert len(events) == 0, (
                f"Expected 0 events for {tokens} tokens (below 65k threshold), "
                f"got {len(events)}"
            )

    def test_warning_fires_at_first_crossing_from_clean_state(self, agent_state):
        """A clean state fires token_warning on both LOW→WARNING and WARNING→CRITICAL."""
        events = agent_state.update_token_state(68_000)
        assert len(events) == 1
        assert events[0]["type"] == "token_warning"
        assert events[0]["new_state"] == "warning"
        assert events[0]["token_count"] == 68_000

        # WARNING→CRITICAL fires independently (CRITICAL always fires
        # regardless of previous WARNING, as long as last_token_warning_state
        # is not already CRITICAL)
        events2 = agent_state.update_token_state(90_000)  # WARNING→CRITICAL
        assert len(events2) == 1, (
            "CRITICAL fires independently of WARNING when "
            "last_token_warning_state != CRITICAL"
        )
        assert events2[0]["type"] == "token_warning"
        assert events2[0]["new_state"] == "critical"
        assert events2[0]["token_count"] == 90_000

    def test_rapid_re_entry_after_drop_does_not_fire_again(self, agent_state):
        """
        After firing, dropping below warning fires a recovery event and resets
        the flag, so re-entering can fire again.
        """
        # First crossing
        agent_state.update_token_state(70_000)  # fires token_warning
        assert agent_state._token_warning_has_fired is True

        # Drop below warning — fires token_recovery and resets the flag
        events_drop = agent_state.update_token_state(50_000)
        assert len(events_drop) == 1, (
            "Dropping from WARNING→LOW with _token_warning_has_fired=True "
            "should fire a token_recovery event"
        )
        assert events_drop[0]["type"] == "token_recovery"
        assert events_drop[0]["old_state"] == "warning"
        assert events_drop[0]["new_state"] == "low"
        assert events_drop[0]["token_count"] == 50_000
        assert agent_state.token_state == TokenState.LOW
        assert agent_state._token_warning_has_fired is False, (
            "Recovery should reset _token_warning_has_fired to False"
        )

        # Re-enter warning — fires again because flag was reset by recovery
        events = agent_state.update_token_state(70_000)
        assert len(events) == 1, (
            "Re-entering warning after recovery should fire again "
            "because _token_warning_has_fired was reset"
        )
        assert events[0]["type"] == "token_warning"
        assert events[0]["new_state"] == "warning"

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
        Crossing directly to CRITICAL fires a token_warning. Dropping to LOW
        fires a token_recovery (resetting the flag), so a fresh LOW→WARNING
        can fire again.
        """
        import warnings as _w

        with _w.catch_warnings(record=True) as w:
            _w.simplefilter("always")

            events = agent_state.update_token_state(85_000)  # LOW→CRITICAL
            assert len(events) == 1
            assert events[0]["new_state"] == "critical"
            assert agent_state._token_warning_has_fired is True

            # Drop to LOW — fires token_recovery
            events2 = agent_state.update_token_state(50_000)
            assert len(events2) == 1, (
                "Dropping from CRITICAL→LOW with _token_warning_has_fired=True "
                "should fire a token_recovery event"
            )
            assert events2[0]["type"] == "token_recovery"
            assert events2[0]["old_state"] == "critical"
            assert events2[0]["new_state"] == "low"
            assert events2[0]["token_count"] == 50_000
            assert agent_state.token_state == TokenState.LOW
            assert agent_state._token_warning_has_fired is False, (
                "Recovery should reset _token_warning_has_fired to False"
            )

            # Re-enter WARNING → fires again because flag was reset
            events3 = agent_state.update_token_state(70_000)
            assert len(events3) == 1, (
                "After CRITICAL→LOW recovery, re-entering WARNING should fire "
                "again because _token_warning_has_fired was reset"
            )
            assert events3[0]["type"] == "token_warning"
            assert events3[0]["new_state"] == "warning"
