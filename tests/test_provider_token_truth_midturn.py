"""D2: provider token truth lands in state *within* the same turn.

The stream's ``UsageUpdate`` overwrites the estimate and calls
``state.update_token_state()`` immediately -- before the tool batch runs. So a
crossing of the CRITICAL threshold in turn N makes ``restrictions_active`` True
in that same turn, and ``tool_executor.execute_tool_calls`` consults
``is_tool_allowed`` per call: the remaining calls of the *same* batch are
rejected. This test pins that current behaviour (documented here, not endorsed):
the flip is immediate, there is no grace turn.
"""

from __future__ import annotations

import json

from agent.config.models import AgentConfig
from agent.core.agent import Agent
from agent.core.llm_client import _to_canonical_tool_call
from agent.core.state import TokenState
from agent.core.stream_events import Done, TextDelta, ToolCallComplete, UsageUpdate


def _tool_call_event(name: str, arguments: dict, call_id: str) -> ToolCallComplete:
    wire = {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }
    canonical = _to_canonical_tool_call(wire)
    return ToolCallComplete(tool_call=canonical, parse_error=canonical.parse_error)


class _ScriptedLLM:
    def __init__(self, turns):
        self._turns = [list(turn) for turn in turns]
        self.calls = 0

    async def stream_completion(self, messages, tools=None, **kwargs):
        index = min(self.calls, len(self._turns) - 1)
        self.calls += 1
        for event in self._turns[index]:
            yield event


class TestProviderTokenTruthMidTurn:
    def test_critical_crossing_flips_restrictions_before_tools_run(self):
        config = AgentConfig(
            api_key="test-key",
            enable_logging=False,
            token_monitor_warning_threshold=1000,
            token_monitor_critical_threshold=2000,
        )
        agent = Agent(config=config, session_id="test-session")
        assert agent.state.restrictions_active is False
        assert agent.state.is_tool_allowed("ReadFile") is True

        llm = _ScriptedLLM([
            # Same turn: provider truth first, then TWO tool calls.
            [
                UsageUpdate(input_tokens=5000, output_tokens=10),
                _tool_call_event("ReadFile", {"file_path": "a.txt"}, "call_read_1"),
                _tool_call_event("ReadFile", {"file_path": "b.txt"}, "call_read_2"),
                Done(),
            ],
            [TextDelta(text="Context is critical; summarizing instead."), Done()],
        ])
        agent.llm_client.stream_completion = llm.stream_completion

        events = list(agent.process_query("Read two files"))
        types = [event["type"] for event in events]

        # Provider truth reached state: the estimate was replaced by 5000 tokens.
        assert agent.state.current_conversation_tokens >= 5000
        assert agent.state.token_state is TokenState.CRITICAL

        # A CRITICAL token_warning was emitted, before the tools were rejected.
        # (An earlier 'warning' event can exist: the turn-start estimate crosses
        # the WARNING threshold before the LLM is even called.)
        warnings = [event for event in events if event["type"] == "token_warning"]
        critical_warnings = [e for e in warnings if e.get("new_state") == "critical"]
        assert len(critical_warnings) == 1, warnings
        critical_idx = events.index(critical_warnings[0])
        first_result_idx = types.index("tool_result")
        assert critical_idx < first_result_idx, types

        # Restrictions flipped mid-turn: BOTH remaining calls of the same batch
        # were rejected by tool_executor.is_tool_allowed.
        results = [event for event in events if event["type"] == "tool_result"]
        assert len(results) == 2, types
        for result in results:
            # FIXED (bug/tool-refusal-reported-as-success):
            # the rejection in tool_executor._create_tool_rejection_message does
            # not match the result-classification prefixes in agent.py ("\u274c",
            # "TOOL CALL REJECTED", "Error executing tool"), so a refusal is
            # reported with success=True.  The assertion pinned here has now
            # flipped, as promised: tool_executor records the refusal
            # structurally ('success': False) and agent.py honours that verdict
            # before falling back to the legacy prose sniff.
            assert result["success"] is False
            assert "not allowed" in result["result"]
            assert "token" in result["result"]
        assert agent.state.restrictions_active is True
        assert agent.state.restriction_reason == "token"
        assert agent.state.is_tool_allowed("ReadFile") is False
        assert agent.state.get_allowed_tools() == ["Respond", "SummarizeTool"]

        # The turn still terminates normally.
        assert "agent_responded" in types
