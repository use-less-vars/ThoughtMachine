"""Summarization is orthogonal to (the now sole) streaming path.

A turn whose only tool call is ``SummarizeTool`` must prune the conversation and
*continue* the loop: the summary frees context, so the agent keeps working. It
must NOT emit an ``agent_responded`` terminal for that turn -- the terminal is
owed only once the agent actually answers.

This is the orthogonality proof after the ``streaming`` flag was deleted: there
is exactly one turn loop (the streaming one), and pruning behaves there exactly
as it did on the old sync path. No ``streaming=True/False`` is passed anywhere.
"""

from __future__ import annotations

import json

from agent.config.models import AgentConfig
from agent.core.agent import Agent
from agent.core.llm_client import _to_canonical_tool_call
from agent.core.stream_events import Done, TextDelta, ToolCallComplete


def _tool_call_event(name: str, arguments: dict, call_id: str) -> ToolCallComplete:
    wire = {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }
    canonical = _to_canonical_tool_call(wire)
    return ToolCallComplete(tool_call=canonical, parse_error=canonical.parse_error)


class _ScriptedLLM:
    """Yield pre-built stream events, one script per LLM call."""

    def __init__(self, turns):
        self._turns = [list(turn) for turn in turns]
        self.calls = 0

    async def stream_completion(self, messages, tools=None, **kwargs):
        index = min(self.calls, len(self._turns) - 1)
        self.calls += 1
        for event in self._turns[index]:
            yield event


class TestPruningOrthogonalToStreaming:
    def test_summarize_only_turn_prunes_and_loop_continues(self):
        config = AgentConfig(api_key="test-key", enable_logging=False)
        agent = Agent(config=config, session_id="test-session")

        llm = _ScriptedLLM([
            # Turn 1: the ONLY tool call is SummarizeTool.
            [
                _tool_call_event(
                    "SummarizeTool",
                    {"summary": "We discussed the plan.", "keep_recent_turns": 1},
                    "call_summarize",
                ),
                Done(),
            ],
            # Turn 2: the loop must come back here after pruning.
            [TextDelta(text="Continuing after summary."), Done()],
        ])
        agent.llm_client.stream_completion = llm.stream_completion

        events = list(agent.process_query("Please summarize and continue"))
        types = [event["type"] for event in events]

        # Pruning happened: the summary marker is in the conversation.
        assert types.count("context_summarized") == 1
        assert any(
            isinstance(message, dict) and message.get("summary")
            for message in agent.conversation
        ), f"no summary message in conversation: {agent.conversation}"

        # No terminal for the summarize-only turn: the loop continued instead.
        assert "agent_responded" in types
        terminal_idx = types.index("agent_responded")
        assert terminal_idx > types.index("context_summarized"), types
        assert "agent_responded" not in types[: types.index("context_summarized")], types

        # The loop really continued (a second LLM call) and answered there.
        assert llm.calls == 2
        responded = [
            event for event in events if event["type"] == "agent_responded"
        ]
        assert len(responded) == 1
        assert responded[0]["content"] == "Continuing after summary."
