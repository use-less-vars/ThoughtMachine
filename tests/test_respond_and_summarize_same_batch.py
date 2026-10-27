"""D1: a single tool batch carrying BOTH ``Respond`` and ``SummarizeTool``.

The summary branch ``continue``s the turn loop (correct for a summarize-only
batch), so a terminal that is owed in the *same* batch would be dropped. The
loop must instead fall through to the deferred terminal block:

    tool_call/tool_result  ->  token_update  ->  context_summarized  ->  agent_responded

with the terminal emitted exactly once, after (never before) the pruning
notification, and without spending an extra LLM round trip.
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
    def __init__(self, turns):
        self._turns = [list(turn) for turn in turns]
        self.calls = 0

    async def stream_completion(self, messages, tools=None, **kwargs):
        index = min(self.calls, len(self._turns) - 1)
        self.calls += 1
        for event in self._turns[index]:
            yield event


class TestRespondAndSummarizeSameBatch:
    def test_pruning_then_terminal_in_one_batch(self):
        config = AgentConfig(api_key="test-key", enable_logging=False)
        agent = Agent(config=config, session_id="test-session")

        llm = _ScriptedLLM([
            # ONE assistant turn, ONE tool batch: SummarizeTool + Respond.
            [
                TextDelta(text="Summarizing, then answering."),
                _tool_call_event(
                    "SummarizeTool",
                    {"summary": "We discussed the plan.", "keep_recent_turns": 1},
                    "call_summarize",
                ),
                _tool_call_event(
                    "Respond",
                    {"content": "Done from batch", "status": "final", "confidence": "high"},
                    "call_respond",
                ),
                Done(),
            ],
            # Must NOT be reached: the terminal is owed by the same batch.
            [TextDelta(text="WRONG: loop continued"), Done()],
        ])
        agent.llm_client.stream_completion = llm.stream_completion

        events = list(agent.process_query("Summarize and answer"))
        types = [event["type"] for event in events]

        # Pruning happened in this turn.
        assert types.count("context_summarized") == 1, types
        assert any(
            isinstance(message, dict) and message.get("summary")
            for message in agent.conversation
        ), f"no summary message in conversation: {agent.conversation}"

        # ... and the terminal is still owed and emitted exactly once.
        assert types.count("agent_responded") == 1, types
        summarized_idx = types.index("context_summarized")
        terminal_idx = types.index("agent_responded")
        assert summarized_idx < terminal_idx, types
        responded = [event for event in events if event["type"] == "agent_responded"]
        assert responded[0]["content"] == "Done from batch"
        assert responded[0]["response_type"] == "answer"

        # The terminal came from the same batch, not from a follow-up turn.
        assert llm.calls == 1, f"loop continued instead of emitting the terminal: {types}"
