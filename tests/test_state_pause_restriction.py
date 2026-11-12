"""test_state_pause_restriction.py -- restricted mode API for AgentState.

Covers the two mutators added to AgentState (restrict / clear_restriction)
and the fail-safe allow-list in get_allowed_tools(): an unattributable
restriction_reason must never be granted the broader token allow-list.

Run with::

    pytest tests/test_state_pause_restriction.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent.core.state import AgentState
from thoughtmachine.timeout_constants import TIMEOUT_READ_ONLY_TOOLS


def _state() -> AgentState:
    """Fresh AgentState (the dataclass requires `config`)."""
    return AgentState(config=MagicMock())


def _assert_unrestricted(state: AgentState) -> None:
    assert state.restrictions_active is False
    assert state.restrictions_pending is False
    assert state.restriction_reason is None


# a) entering restricted mode, and idempotence for the same reason
def test_restrict_pause_enters_restricted_mode():
    state = _state()
    assert state.restrict('pause') is True
    assert state.restrictions_active is True
    assert state.restrictions_pending is False
    assert state.restriction_reason == 'pause'

    # Same reason twice is a no-op -- state stays restricted.
    assert state.restrict('pause') is False
    assert state.restrictions_active is True
    assert state.restriction_reason == 'pause'


# b) blank / missing reason is rejected and leaves the state untouched
@pytest.mark.parametrize('bad_reason', ['', None])
def test_restrict_rejects_blank_reason(bad_reason):
    state = _state()
    with pytest.raises(ValueError):
        state.restrict(bad_reason)
    _assert_unrestricted(state)


# c) a pause must never clear a token restriction
def test_clear_restriction_with_foreign_reason_is_a_no_op():
    state = _state()
    state.restrict('pause')
    assert state.clear_restriction('token') is False
    assert state.restrictions_active is True
    assert state.restriction_reason == 'pause'


# d) the owning reason clears all three attributes
def test_clear_restriction_with_own_reason_clears_everything():
    state = _state()
    state.restrict('pause')
    assert state.clear_restriction('pause') is True
    _assert_unrestricted(state)


# e) owner-agnostic clear works, and reports False when there was nothing
def test_clear_restriction_without_reason_clears_token_restriction():
    state = _state()
    state.restrict('token')
    assert state.clear_restriction() is True
    _assert_unrestricted(state)


def test_clear_restriction_without_reason_on_idle_state_returns_false():
    state = _state()
    assert state.clear_restriction() is False
    _assert_unrestricted(state)


# f) allow-list per reason -- unknown reasons must fail safe to Respond-only
# ('timeout' is deliberately NOT here: it is the widened axis, pinned below.)
@pytest.mark.parametrize(
    'reason',
    ['turn', 'pause', 'something-unknown', None],
)
def test_allowed_tools_respond_only_for_non_token_reasons(reason):
    state = _state()
    state.restrictions_active = True
    state.restriction_reason = reason
    assert state.get_allowed_tools() == ['Respond']


# f2) 'timeout' stops new WORK, not reading: Respond + SummarizeTool + the
# explicit read-only set, taken from the single source of truth.
def test_allowed_tools_timeout_reason_allows_respond_summarize_and_read_only():
    """Display and gate must agree on the widened timeout allow-list."""
    state = _state()
    state.restrictions_active = True
    state.restriction_reason = 'timeout'

    expected = ['Respond', 'SummarizeTool'] + sorted(TIMEOUT_READ_ONLY_TOOLS)
    assert state.get_allowed_tools() == expected
    # Every advertised tool must actually pass the gate, and vice versa.
    for name in expected:
        assert state.is_tool_allowed(name) is True, name

    # Read-only members are allowed...
    assert state.is_tool_allowed('ReadFile') is True
    # ...write/execute tools are refused under a spent budget.
    assert state.is_tool_allowed('ApplyEdits') is False
    assert state.is_tool_allowed('ContainerExecTool') is False


def test_allowed_tools_token_reason_allows_summarize():
    state = _state()
    state.restrict('token')
    assert state.get_allowed_tools() == ['Respond', 'SummarizeTool']


def test_allowed_tools_empty_only_when_unrestricted():
    state = _state()
    assert state.get_allowed_tools() == []
    state.restrict('pause')
    assert state.get_allowed_tools() != []


# g) is_tool_allowed honours the paused allow-list
def test_is_tool_allowed_on_paused_state():
    state = _state()
    state.restrict('pause')
    assert state.is_tool_allowed('Respond') is True
    assert state.is_tool_allowed('ContainerExecTool') is False


# j) fail-closed: an object missing `restrictions_active` is NOT unrestricted
#    (docs/CONTROL_PLANE.md 6.3 Option B / 6.4)
def test_is_tool_allowed_fail_closed_when_restrictions_active_missing():
    # A plain object has no `restrictions_active` attribute at all -- exactly
    # the fail-open edge the pre-Option-B implementation walked through.
    stub = object()
    assert AgentState.is_tool_allowed(stub, 'Respond') is True
    assert AgentState.is_tool_allowed(stub, 'ContainerExecTool') is False


# h) last writer wins on the reason, which is what makes ownership meaningful
def test_last_writer_wins_on_reason():
    state = _state()
    assert state.restrict('pause') is True
    assert state.restrict('token') is True
    assert state.restriction_reason == 'token'
    assert state.clear_restriction('pause') is False
    assert state.restrictions_active is True
    assert state.restriction_reason == 'token'


# i) reset() drops an externally owned restriction too
def test_reset_clears_a_pause_restriction():
    state = _state()
    state.restrict('pause')
    state.reset()
    _assert_unrestricted(state)
