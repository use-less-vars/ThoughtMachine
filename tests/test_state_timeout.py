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
from thoughtmachine.timeout_constants import TIMEOUT_READ_ONLY_TOOLS


# ════════════════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def state() -> AgentState:
    """Fresh AgentState with a mock logger."""
    mock_config = MagicMock()
    mock_config.turn_monitor_enabled = True
    mock_config.max_turns = 100
    mock_config.token_monitor_warning_threshold = 500
    mock_config.token_monitor_critical_threshold = 1000
    mock_config.time_monitor_enabled = True
    mock_config.timeout_seconds = 300
    mock_config.time_warning_threshold = 240
    s = AgentState(config=mock_config, logger=MagicMock())
    s.reset()
    return s


# ════════════════════════════════════════════════════════════════════════════
# Tests
# ════════════════════════════════════════════════════════════════════════════

class TestGetAllowedTools:
    """Verify get_allowed_tools() returns the correct tool lists."""

    def test_get_allowed_tools_timeout_returns_respond_and_read_only(self, state: AgentState):
        """On 'timeout', Respond + SummarizeTool + the read-only set are allowed.

        The runtime budget being spent must stop new WORK, not blind the agent
        while it writes up its (mandatory) response.
        """
        state.restrictions_active = True
        state.restriction_reason = 'timeout'
        allowed = state.get_allowed_tools()
        expected = {'Respond', 'SummarizeTool'} | set(TIMEOUT_READ_ONLY_TOOLS)
        assert set(allowed) == expected, f"Expected {sorted(expected)}, got {allowed}"
        assert len(allowed) == len(set(allowed)), f'Duplicates in {allowed}'
        # No work tools leak in through the timeout branch.
        for write_tool in ('ApplyEdits', 'FileEditor', 'DockerCodeRunner',
                           'ContainerExecTool', 'Worker', 'ProgressReport'):
            assert write_tool not in allowed, (
                f'{write_tool} must not be allowed on the timeout axis: {allowed}'
            )

    def test_get_allowed_tools_timeout_matches_is_tool_allowed(self, state: AgentState):
        """get_allowed_tools() is display-only but must agree with the gate.

        The tool-rejection prose renders ``Available tools:`` from
        get_allowed_tools(), so a disagreement would advertise tools the gate
        then refuses (or hide tools that are permitted).
        """
        state.restrictions_active = True
        state.restriction_reason = 'timeout'
        allowed = set(state.get_allowed_tools())
        probes = set(allowed) | {
            'ApplyEdits', 'FileEditor', 'DockerCodeRunner', 'ContainerExecTool',
            'CodeModifier', 'NonexistentToolOnPurpose',
        }
        for name in sorted(probes):
            assert state.is_tool_allowed(name) == (name in allowed), (
                f'is_tool_allowed({name!r}) disagrees with get_allowed_tools()'
            )

    def test_get_allowed_tools_timeout_allows_read_refuses_write(self, state: AgentState):
        """Read-only tools survive the timeout; write tools do not."""
        state.restrictions_active = True
        state.restriction_reason = 'timeout'
        assert state.is_tool_allowed('ReadFile') is True
        assert state.is_tool_allowed('FileSearchTool') is True
        assert state.is_tool_allowed('git_read') is True
        assert state.is_tool_allowed('ApplyEdits') is False
        assert state.is_tool_allowed('ContainerExecTool') is False
        assert state.is_tool_allowed('Respond') is True

    def test_get_allowed_tools_token_critical_returns_respond_and_summarize(self, state: AgentState):
        """When restriction_reason is 'token', both Respond and SummarizeTool should be allowed."""
        state.restrictions_active = True
        state.restriction_reason = 'token'
        allowed = state.get_allowed_tools()
        assert allowed == ['Respond', 'SummarizeTool'], f"Expected ['Respond', 'SummarizeTool'], got {allowed}"

    def test_get_allowed_tools_turn_critical_returns_only_respond(self, state: AgentState):
        """When restriction_reason is 'turn', only Respond should be allowed."""
        state.restrictions_active = True
        state.restriction_reason = 'turn'
        allowed = state.get_allowed_tools()
        assert allowed == ['Respond'], f"Expected ['Respond'], got {allowed}"

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
        events = state.update_token_state(state.current_conversation_tokens)
        assert state.restriction_reason == 'token', (
            f"Expected 'token', got {state.restriction_reason}"
        )
        assert state.restrictions_active is True

    def test_restriction_reason_set_on_turn_critical(self, state: AgentState):
        """Turn CRITICAL state should set restriction_reason to 'turn'."""
        # product reads max_turns from config (agent/core/state.py:265)
        state.current_turn = 19
        state.config.max_turns = 20
        events = state.update_turn_state(state.current_turn)
        assert state.restriction_reason == 'turn', (
            f"Expected 'turn', got {state.restriction_reason}"
        )
        assert state.restrictions_active is True

    def test_restriction_reason_cleared_when_time_returns_to_low(self, state: AgentState):
        """When time state goes back to LOW, this monitor clears its own
        restrictions: restriction_reason is reset AND restrictions_active /
        restrictions_pending are turned off — the timeout is NOT a hard stop
        for the rest of the turn (mirrors the turn/token LOW branches).

        Product semantics (agent/core/state.py): the update_time_state() LOW
        transition clears the restrictions it set when
        restriction_reason == 'timeout'.
        """
        # First trigger CRITICAL
        state.time_start = 0.0
        state.timeout_seconds = 1
        state.time_warning_threshold = 0
        state.update_time_state(999.0)
        assert state.restriction_reason == 'timeout'

        # Now simulate time returning to LOW (elapsed back under the timeout)
        state.time_start = None
        state.update_time_state(0.0)
        assert state.restriction_reason is None, (
            f"Expected None after time returns to LOW, got {state.restriction_reason}"
        )
        # Restrictions are lifted: work tools are available again
        assert state.restrictions_active is False, (
            "Expected restrictions_active to be False after time returns to LOW "
            "(timeout is not a hard stop for the turn)"
        )
        assert state.restrictions_pending is False
        assert state.get_allowed_tools() == [], (
            f"Expected all tools restored, got {state.get_allowed_tools()}"
        )

    def test_restriction_reason_cleared_when_token_returns_to_low(self, state: AgentState):
        """When token state goes back to LOW, restriction_reason should be cleared."""
        # First trigger CRITICAL
        state.current_conversation_tokens = 1000000
        state.max_conversation_tokens = 1000
        state.token_warning_threshold = 500
        state.update_token_state(state.current_conversation_tokens)
        assert state.restriction_reason == 'token'

        # Now reduce tokens below threshold
        state.current_conversation_tokens = 100
        state.update_token_state(state.current_conversation_tokens)
        assert state.restriction_reason is None, (
            f"Expected None after tokens return to LOW, got {state.restriction_reason}"
        )

    def test_restriction_reason_cleared_when_turn_returns_to_low(self, state: AgentState):
        """When turn state goes back to LOW, restriction_reason should be cleared."""
        # First trigger CRITICAL: product critical_turn = max(max_turns-5, 1) = 95 for max_turns=100
        state.current_turn = 99
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


class TestTimeoutReadOnlyAllowlistRot:
    """Rot protection for ``TIMEOUT_READ_ONLY_TOOLS``.

    The constant is an explicit, hand-maintained frozenset (it must not be
    derived from the tool registry at runtime -- see the comment in
    thoughtmachine/timeout_constants.py). These tests are the counterweight:
    they fail if a name stops resolving to a registered tool, or if a tool
    with a write-ish ``required_categories`` entry ever lands in the set.
    """

    # Every permission category that is unambiguously read-scoped. Derived by
    # grepping ``required_categories`` / ``get_required_categories`` across
    # ``tools/`` (tools/base.py:90 is the declaring ClassVar). Any category
    # NOT listed here (filesystem:write, container:true, mcp:connect,
    # host_bash:allow, visual:view, network:outbound, git:write, ci:read is
    # read-scoped ...) is treated as write-ish by default -- fail closed, so a
    # newly invented category cannot silently join the read-only set.
    READ_SCOPED_CATEGORIES = frozenset({
        'filesystem:read',
        'git:read',
        'ci:read',
    })

    # Tools that are excluded on purpose even though a naive category check
    # would let them through. Documented in timeout_constants.py too.
    EXPLICITLY_EXCLUDED = frozenset({
        'KnowledgeBaseTool',  # read and write operations share one tool
        'Worker',             # required_categories == [] but spawns nested agents
        'FileEditor',         # read AND write/delete in one tool
        'WorkingDocument',    # task-list/memory mutator
        'ProgressReport',     # always writes a file
    })

    @staticmethod
    def _registry() -> dict:
        """Map canonical tool name -> tool class, from the live registry."""
        import tools  # local import: keeps this module importable without tools
        return {cls.tool_name(): cls for cls in tools.TOOL_CLASSES}

    def test_every_name_resolves_to_a_registered_tool(self):
        registry = self._registry()
        assert registry, 'tools.TOOL_CLASSES is empty -- registry did not load'
        missing = sorted(n for n in TIMEOUT_READ_ONLY_TOOLS if n not in registry)
        assert not missing, (
            f'TIMEOUT_READ_ONLY_TOOLS names no registered tool: {missing}. '
            f'Registered names: {sorted(registry)}'
        )

    def test_no_member_declares_write_ish_categories(self):
        registry = self._registry()
        offenders = []
        for name in sorted(TIMEOUT_READ_ONLY_TOOLS):
            cls = registry.get(name)
            if cls is None:
                continue  # covered by the resolution test
            # Static check with no params: tools with operation-level
            # granularity (FileEditor, KnowledgeBaseTool, Worker) report their
            # most permissive categories here, which is what we want.
            cats = set(cls.get_required_categories({}) or [])
            write_ish = cats - self.READ_SCOPED_CATEGORIES
            if write_ish:
                offenders.append((name, sorted(write_ish)))
        assert not offenders, (
            'Tools with write-ish required_categories must NOT stay available '
            f'on the timeout axis: {offenders}'
        )

    def test_explicitly_excluded_tools_are_absent(self):
        leaked = sorted(self.EXPLICITLY_EXCLUDED & set(TIMEOUT_READ_ONLY_TOOLS))
        assert not leaked, (
            f'These tools are excluded on purpose and must not be in '
            f'TIMEOUT_READ_ONLY_TOOLS: {leaked}'
        )

    def test_no_registered_write_tool_is_a_member(self):
        """Any registry tool that needs a write-ish category is refused."""
        registry = self._registry()
        gate = AgentState(config=MagicMock(), logger=MagicMock())
        gate.reset()
        gate.restrictions_active = True
        gate.restriction_reason = 'timeout'
        leaked = []
        for name, cls in sorted(registry.items()):
            try:
                cats = set(cls.get_required_categories({}) or [])
            except Exception:  # pragma: no cover - registry anomaly
                continue
            if cats - self.READ_SCOPED_CATEGORIES:
                if gate.is_tool_allowed(name):
                    leaked.append(name)
        # Respond/SummarizeTool are granted explicitly on every restricted axis
        # and are not read tools; the timeout branch grants them deliberately.
        leaked = [n for n in leaked if n not in ('Respond', 'SummarizeTool')]
        assert not leaked, (
            f'Write-ish tools are allowed under timeout: {leaked}'
        )


class TestTimeoutWarningMessage:
    """Verify the timeout warning messages contain the expected text."""

    def test_timeout_warning_message_contains_restriction_hint(self):
        """The time warning message should mention tool restrictions."""
        mock_config = MagicMock()
        mock_config.time_monitor_enabled = True
        mock_config.timeout_seconds = 300
        mock_config.time_warning_threshold = 240
        state = AgentState(config=mock_config, logger=MagicMock())
        state.reset()
        state.time_start = 0.0
        state.timeout_seconds = 300
        state.time_warning_threshold = 240
        state.last_time_warning_state = None

        # Trigger WARNING state (elapsed < timeout but > threshold)
        events = state.update_time_state(250.0)
        warning_events = [e for e in events if e['type'] == 'time_warning']
        assert len(warning_events) > 0
        msg = warning_events[0].get('warning_message', '')
        assert 'Tool restrictions will be applied' in msg, (
            f"Warning message should mention restrictions. Got: {msg}"
        )

    def test_timeout_critical_message_contains_only_respond_hint(self):
        """The time critical message should mention only Respond is available."""
        mock_config = MagicMock()
        mock_config.time_monitor_enabled = True
        mock_config.timeout_seconds = 300
        mock_config.time_warning_threshold = 240
        state = AgentState(config=mock_config, logger=MagicMock())
        state.reset()
        state.time_start = 0.0
        state.timeout_seconds = 300
        state.time_warning_threshold = 240
        state.last_time_warning_state = None

        # Trigger CRITICAL state (elapsed > timeout)
        events = state.update_time_state(301.0)
        warning_events = [e for e in events if e['type'] == 'time_warning']
        assert len(warning_events) > 0
        msg = warning_events[0].get('warning_message', '')
        assert 'Only Respond, SummarizeTool and the read-only tools are available' in msg, (
            f"Critical message should mention Respond + SummarizeTool + read-only. Got: {msg}"
        )
        assert 'Please finish your work and respond immediately' in msg, (
            f"Critical message should ask to finish work. Got: {msg}"
        )
