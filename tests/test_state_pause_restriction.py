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

from agent.core.state import RESTRICTION_RULES, WRITE_ESCAPE_TOOLS, AgentState
from thoughtmachine.timeout_constants import TIMEOUT_WRITE_ESCAPE_TOOLS


def _state() -> AgentState:
    """Fresh AgentState (the dataclass requires `config`)."""
    return AgentState(config=MagicMock())


def _assert_unrestricted(state: AgentState) -> None:
    assert state.restrictions_active is False
    assert state.restrictions_pending is False
    assert state.restriction_reason is None


#: A fixed probe list of REAL tool names (plus one that must never be granted
#: and the empty string) for round-tripping the display allow-list through the
#: gate in both directions. Names, never a hard-coded expectation: widening the
#: escape set cannot make these tests pass silently.
_GATE_PROBES = (
    'Respond', 'SummarizeTool', 'WorkingDocument',
    'ReadFile', 'ReadFileTool', 'FileSearchTool', 'FilePreviewTool',
    'FileSummaryTool', 'GlobTool', 'DirectoryTreeTool', 'CheckSystem',
    'git_read', 'git_write', 'ApplyEdits', 'FileEditor', 'FileMover',
    'CodeModifier', 'DockerCodeRunner', 'ContainerExecTool',
    'ContainerStartTool', 'ProgressReport', 'Worker',
    'NonexistentToolOnPurpose', '',
)


def _state_restricted_on(reason) -> AgentState:
    """A state whose restriction_reason is ``reason`` -- including the ones
    ``restrict()`` refuses (unknown, '', None): those are exactly the values
    both readers must fail safe on, so they are written straight onto the
    attributes."""
    state = _state()
    if isinstance(reason, str) and reason.strip():
        state.restrict(reason)
    else:
        state.restrictions_active = True
        state.restriction_reason = reason
    return state


def _permitted_by_gate(state: AgentState) -> set:
    """The probe names ``is_tool_allowed()`` actually permits."""
    return {name for name in _GATE_PROBES if state.is_tool_allowed(name)}


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
    ['turn', 'pause', 'something-unknown', '', None],
)
def test_allowed_tools_respond_only_for_non_token_reasons(reason):
    state = _state()
    state.restrictions_active = True
    state.restriction_reason = reason
    assert state.get_allowed_tools() == ['Respond']


# f2) 'timeout' stops new WORK *and* further reading: Respond + SummarizeTool
# + the single write-only escape hatch, from the single source of truth.
def test_allowed_tools_timeout_reason_allows_respond_summarize_and_write_escape():
    """Display and gate must agree on the timeout allow-list."""
    state = _state()
    state.restrictions_active = True
    state.restriction_reason = 'timeout'

    expected = ['Respond', 'SummarizeTool'] + sorted(TIMEOUT_WRITE_ESCAPE_TOOLS)
    assert state.get_allowed_tools() == expected
    # Every advertised tool must actually pass the gate, and vice versa.
    for name in expected:
        assert state.is_tool_allowed(name) is True, name

    # The write-only hatch is the ONE thing this axis adds: it can dump working
    # memory to disk, then the agent yields and compacts.
    assert state.is_tool_allowed('WorkingDocument') is True
    # Nothing that would let the agent keep INVESTIGATING is allowed -- the
    # restriction is resource management, not salvage.
    for refused in ('ReadFile', 'FileSearchTool', 'git_read',
                    'ApplyEdits', 'ContainerExecTool'):
        assert state.is_tool_allowed(refused) is False, refused


def test_allowed_tools_token_reason_allows_summarize():
    """'token' allows Respond, SummarizeTool and the WRITE escape hatch.

    Same escape set as 'timeout': the ceiling protects the context window, not
    the ability to land an already-prepared write. Reads/searches stay out.
    """
    state = _state()
    state.restrict('token')
    # Derived from the token axis' own escape set (not a hard-coded list, not the
    # timeout constant) so a future widening cannot pass here silently: reads and
    # searches stay refused, writes are allowed, exactly as on 'timeout'.
    expected = ['Respond', 'SummarizeTool'] + list(WRITE_ESCAPE_TOOLS)
    assert state.get_allowed_tools() == expected
    for name in expected:
        assert state.is_tool_allowed(name) is True, name
    for refused in ('ReadFile', 'FileSearchTool', 'git_read', 'DockerCodeRunner'):
        assert state.is_tool_allowed(refused) is False, refused


# f3) D4: ONE restriction table -- the display allow-list and the gate are the
# same decision for EVERY declared reason, not just the two widened ones.
@pytest.mark.parametrize('reason', sorted(RESTRICTION_RULES))
def test_display_and_gate_agree_for_every_declared_reason(reason):
    """get_allowed_tools() == exactly what is_tool_allowed() permits.

    Both directions over a fixed probe list: the display must not advertise a
    tool the gate refuses, and must not hide one it permits. Parametrized off
    ``RESTRICTION_RULES`` so a new row is covered the day it is declared.
    """
    state = _state_restricted_on(reason)
    allowed = state.get_allowed_tools()
    permitted = _permitted_by_gate(state)
    assert permitted == set(allowed), (
        f'[{reason}] gate and display disagree: only-in-gate '
        f'{sorted(permitted - set(allowed))}, only-in-display '
        f'{sorted(set(allowed) - permitted)}'
    )
    # Every advertised name really passes the gate (not just set equality).
    for name in allowed:
        assert state.is_tool_allowed(name) is True, (reason, name)
    # The probe list must be able to SEE the whole allow-list, otherwise the
    # bidirectional check above is vacuous for the tools it is missing.
    assert set(allowed) <= set(_GATE_PROBES), (
        f'[{reason}] allow-list names a tool _GATE_PROBES does not probe: '
        f'{sorted(set(allowed) - set(_GATE_PROBES))}'
    )
    # One name, one line: a duplicate would be advertised twice.
    assert len(allowed) == len(set(allowed)), allowed


@pytest.mark.parametrize(
    'reason',
    ['something-unknown', '', None, 42],
    ids=['unknown-string', 'empty-string', 'none', 'non-string'],
)
def test_unattributable_reason_permits_exactly_respond(reason):
    """A reason no rule declares fails safe to Respond and NOTHING else.

    'Some misspelled or future reason' must never inherit the token allow-list
    -- that would silently re-enable tool access.
    """
    state = _state_restricted_on(reason)
    assert state.get_allowed_tools() == ['Respond'], state.get_allowed_tools()
    assert _permitted_by_gate(state) == {'Respond'}
    assert state.is_tool_allowed('SummarizeTool') is False
    assert state.is_tool_allowed('WorkingDocument') is False
    assert state.is_tool_allowed('ReadFile') is False
    assert state.is_tool_allowed('ContainerExecTool') is False


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
