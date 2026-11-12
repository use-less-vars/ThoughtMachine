"""
test_state_timeout.py — Tests for timeout soft-restriction changes in AgentState.

The 'timeout' restriction is RESOURCE MANAGEMENT, not salvage: when the soft
budget is spent only Respond, SummarizeTool and the single write-only escape
hatch (``TIMEOUT_WRITE_ESCAPE_TOOLS``) may be called. Read tools are refused --
the point of the restriction is that the agent stops investigating and yields.

Covered here:
1. the 'timeout' allow-list and its agreement with ``is_tool_allowed()``;
2. rot protection for ``TIMEOUT_WRITE_ESCAPE_TOOLS`` (names resolve, no read /
   exec / container category, exactly one tool, wired into the gate);
3. ``describe_allowed_tools()`` -- the one place an allow-list becomes prose;
4. ``restriction_reason`` being set/cleared on every monitor axis;
5. the warning / critical message text.

Run with::

    pytest tests/test_state_timeout.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent.core.state import (
    AgentState,
    TimeState,
    TokenState,
    TurnState,
    describe_allowed_tools,
)
from thoughtmachine.timeout_constants import TIMEOUT_WRITE_ESCAPE_TOOLS


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

    def test_get_allowed_tools_timeout_returns_respond_and_write_escape(self, state: AgentState):
        """On 'timeout', Respond + SummarizeTool + the write-only hatch are allowed.

        The runtime budget being spent must stop new WORK *and* further
        investigation; the one escape hatch is a WRITE-only tool the agent can
        use to dump working memory before it yields and compacts.
        """
        state.restrictions_active = True
        state.restriction_reason = 'timeout'
        allowed = state.get_allowed_tools()
        expected = {'Respond', 'SummarizeTool'} | set(TIMEOUT_WRITE_ESCAPE_TOOLS)
        assert set(allowed) == expected, f"Expected {sorted(expected)}, got {allowed}"
        assert len(allowed) == len(set(allowed)), f'Duplicates in {allowed}'
        # No work tools leak in through the timeout branch.
        for write_tool in ('ApplyEdits', 'FileEditor', 'DockerCodeRunner',
                           'ContainerExecTool', 'Worker', 'ProgressReport'):
            assert write_tool not in allowed, (
                f'{write_tool} must not be allowed on the timeout axis: {allowed}'
            )
        # ...and no READ tool either: a read grant lets the agent keep
        # investigating instead of yielding, which is the whole point here.
        for read_tool in ('ReadFile', 'FileSearchTool', 'FileSummaryTool',
                          'FilePreviewTool', 'GlobTool', 'DirectoryTreeTool',
                          'CheckSystem', 'git_read'):
            assert read_tool not in allowed, (
                f'{read_tool} is read-scoped and must not survive the timeout '
                f'restriction: {allowed}'
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
            # Read-scoped tools must be REFUSED too, and the display must not
            # advertise them.
            'ReadFile', 'FileSearchTool', 'GlobTool', 'DirectoryTreeTool',
            'CheckSystem', 'git_read',
        }
        for name in sorted(probes):
            assert state.is_tool_allowed(name) == (name in allowed), (
                f'is_tool_allowed({name!r}) disagrees with get_allowed_tools()'
            )

    def test_get_allowed_tools_timeout_allows_write_escape_refuses_read(self, state: AgentState):
        """The timeout axis grants the write hatch and refuses every read tool."""
        state.restrictions_active = True
        state.restriction_reason = 'timeout'
        assert state.is_tool_allowed('WorkingDocument') is True
        assert state.is_tool_allowed('Respond') is True
        assert state.is_tool_allowed('SummarizeTool') is True
        assert state.is_tool_allowed('ReadFile') is False
        assert state.is_tool_allowed('FileSearchTool') is False
        assert state.is_tool_allowed('git_read') is False
        assert state.is_tool_allowed('ApplyEdits') is False
        assert state.is_tool_allowed('ContainerExecTool') is False

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


class TestTimeoutWriteEscapeAllowlistRot:
    """Rot protection for ``TIMEOUT_WRITE_ESCAPE_TOOLS``.

    The constant is an explicit, hand-maintained frozenset (it must not be
    derived from the tool registry at runtime -- see the comment in
    thoughtmachine/timeout_constants.py). These tests are the counterweight:
    they fail if a name stops resolving to a registered tool, if the set grows
    past the single documented escape hatch, if a member picks up a read /
    exec / container / arbitrary-write category, or if a member is declared but
    never wired into the gate.
    """

    # Every permission category that is read-scoped. A tool declaring one of
    # these lets the agent keep INVESTIGATING, which is exactly what the
    # timeout restriction exists to stop -- so none may appear on a member.
    READ_SCOPED_CATEGORIES = frozenset({
        'filesystem:read',
        'git:read',
        'ci:read',
    })

    # The ONLY category a member may declare: ``filesystem:write`` is what the
    # write-only escape hatch needs to dump verbatim working memory to disk.
    # This narrows what may be CALLED; it never grants the permission -- a
    # session without ``filesystem:write`` still cannot use the hatch.
    ALLOWED_CATEGORIES = frozenset({'filesystem:write'})

    # Tools that are excluded on purpose. The read tools are the whole point of
    # the inversion; the operation-level ones are here because a single name
    # spans read and write, so a name-only gate cannot narrow them.
    EXPLICITLY_EXCLUDED = frozenset({
        'ReadFile',
        'FileSearchTool',
        'FileSummaryTool',
        'FilePreviewTool',
        'GlobTool',
        'DirectoryTreeTool',
        'CheckSystem',
        'git_read',
        'FileEditor',         # read AND write/delete in one tool
        'KnowledgeBaseTool',  # read and write operations share one tool
        'Worker',             # required_categories == [] but spawns nested agents
    })

    @staticmethod
    def _registry() -> dict:
        """Map canonical tool name -> tool class, from the live registry."""
        import tools  # local import: keeps this module importable without tools
        return {cls.tool_name(): cls for cls in tools.TOOL_CLASSES}

    def test_every_name_resolves_to_a_registered_tool(self):
        registry = self._registry()
        assert registry, 'tools.TOOL_CLASSES is empty -- registry did not load'
        missing = sorted(n for n in TIMEOUT_WRITE_ESCAPE_TOOLS if n not in registry)
        assert not missing, (
            f'TIMEOUT_WRITE_ESCAPE_TOOLS names no registered tool: {missing}. '
            f'Registered names: {sorted(registry)}'
        )

    def test_exactly_one_escape_hatch(self):
        """The set is ONE tool: "dump working memory, then yield".

        Growing it needs a new rationale in timeout_constants.py, not just a
        new entry -- a second tool is a second way to keep working.
        """
        assert TIMEOUT_WRITE_ESCAPE_TOOLS == frozenset({'WorkingDocument'}), (
            'The timeout escape hatch is documented as the single write-only '
            f'tool WorkingDocument; got {sorted(TIMEOUT_WRITE_ESCAPE_TOOLS)}'
        )

    def test_no_member_declares_read_or_non_write_categories(self):
        registry = self._registry()
        offenders = []
        for name in sorted(TIMEOUT_WRITE_ESCAPE_TOOLS):
            cls = registry.get(name)
            if cls is None:
                continue  # covered by the resolution test
            # Static check with no params -- the shape the gate sees.
            cats = set(cls.get_required_categories({}) or [])
            read_ish = cats & self.READ_SCOPED_CATEGORIES
            if read_ish:
                offenders.append((name, 'read-scoped', sorted(read_ish)))
            unexpected = cats - self.ALLOWED_CATEGORIES
            if unexpected:
                offenders.append((name, 'outside the write hatch', sorted(unexpected)))
        assert not offenders, (
            'A member of TIMEOUT_WRITE_ESCAPE_TOOLS may declare only '
            f'{sorted(self.ALLOWED_CATEGORIES)}: {offenders}'
        )

    def test_explicitly_excluded_tools_are_absent(self):
        leaked = sorted(self.EXPLICITLY_EXCLUDED & set(TIMEOUT_WRITE_ESCAPE_TOOLS))
        assert not leaked, (
            f'These tools are excluded on purpose and must not be in '
            f'TIMEOUT_WRITE_ESCAPE_TOOLS: {leaked}'
        )

    def test_every_member_is_wired_into_the_gate(self):
        """Declaring a member is not enough -- the gate must actually allow it."""
        gate = AgentState(config=MagicMock(), logger=MagicMock())
        gate.reset()
        gate.restrictions_active = True
        gate.restriction_reason = 'timeout'
        assert gate.get_allowed_tools() == (
            ['Respond', 'SummarizeTool'] + sorted(TIMEOUT_WRITE_ESCAPE_TOOLS)
        ), f'unexpected timeout allow-list: {gate.get_allowed_tools()}'
        for name in sorted(TIMEOUT_WRITE_ESCAPE_TOOLS):
            assert gate.is_tool_allowed(name) is True, (
                f'{name} is declared in TIMEOUT_WRITE_ESCAPE_TOOLS but the '
                f'timeout gate refuses it'
            )

    def test_no_registered_read_tool_is_a_member(self):
        """Any registry tool that needs a read-scoped category is refused."""
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
            if cats & self.READ_SCOPED_CATEGORIES:
                if gate.is_tool_allowed(name):
                    leaked.append(name)
        # Respond/SummarizeTool are granted explicitly on every restricted axis
        # and declare no read category; they are not what this guards.
        leaked = [n for n in leaked if n not in ('Respond', 'SummarizeTool')]
        assert not leaked, (
            f'Read-scoped tools are allowed under timeout: {leaked}'
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

    def test_timeout_critical_message_derives_from_allowed_tools(self):
        """The time critical message must name exactly what the gate allows."""
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
        # Pinned verbatim: the sentence is rendered by describe_allowed_tools()
        # from get_allowed_tools(), so it cannot drift from the gate it
        # describes (the bug this replaced advertised read tools the gate then
        # refused).
        assert (
            'Only the following tools are available: '
            'Respond, SummarizeTool, WorkingDocument.'
        ) in msg, (
            f"Critical message must name the allow-list the gate enforces. Got: {msg}"
        )
        assert 'Please finish your work and respond immediately' in msg, (
            f"Critical message should ask to finish work. Got: {msg}"
        )


class TestDescribeAllowedTools:
    """The one place an allow-list becomes prose.

    Every restriction warning must go through this function and hand it
    ``get_allowed_tools()``; the shapes below are what the model reads.
    """

    def test_empty_allow_list(self):
        assert describe_allowed_tools([]) == 'No tools are available.'

    def test_single_tool(self):
        assert describe_allowed_tools(['Respond']) == 'Only the Respond tool is available.'

    def test_many_tools_are_listed_verbatim_and_in_order(self):
        assert describe_allowed_tools(['Respond', 'SummarizeTool', 'WorkingDocument']) == (
            'Only the following tools are available: '
            'Respond, SummarizeTool, WorkingDocument.'
        )

    def test_renders_each_restriction_reason_from_the_gate(self, state: AgentState):
        """Prose and gate are the same decision on every restricted axis."""
        for reason in ('turn', 'pause', 'token', 'timeout', 'something-unknown'):
            state.restrictions_active = True
            state.restriction_reason = reason
            allowed = state.get_allowed_tools()
            rendered = describe_allowed_tools(allowed)
            for name in allowed:
                assert name in rendered, (reason, name, rendered)
            for non_member in ('ApplyEdits', 'ReadFile', 'ContainerExecTool'):
                if non_member not in allowed:
                    assert non_member not in rendered, (reason, non_member, rendered)
