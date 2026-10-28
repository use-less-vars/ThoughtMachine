"""Regression test — the agent loop must surface streamed text BEFORE the turn ends.

Contract PINNED AND SATISFIED by the current implementation:

  ``Agent.process_query`` consumes ``llm_client.stream_completion`` and
  accumulates ``TextDelta`` chunks into ``content`` (agent/core/agent.py:1250),
  yielding a ``text_delta`` event per chunk at that point
  (agent/core/agent.py:1252).  The terminal ``turn`` event is built at
  (agent/core/agent.py:1468) and yielded at (agent/core/agent.py:1474).

Each ``text_delta`` event carries at least ``text`` and ``turn``, is ordered
before the terminal ``turn`` event, and concatenates back to exactly the
``turn`` event's content (no duplication, no loss).

Written RED-first: without the ``text_delta`` yield the failure mode is that
no ``text_delta`` events at all are emitted — every text-bearing event is the
terminal ``turn`` — so this test is the regression guard that fails if that
yield is removed.
"""

from __future__ import annotations

from agent.config.models import AgentConfig
from agent.core.agent import Agent
from agent.core.stream_events import Done, TextDelta


class _ScriptedLLM:
    def __init__(self, turns):
        self._turns = [list(turn) for turn in turns]
        self.calls = 0

    async def stream_completion(self, messages, tools=None, **kwargs):
        index = min(self.calls, len(self._turns) - 1)
        self.calls += 1
        for event in self._turns[index]:
            yield event


def test_process_query_yields_text_delta_before_terminal_turn():
    config = AgentConfig(api_key="test-key", enable_logging=False)
    agent = Agent(config=config, session_id="test-session")

    llm = _ScriptedLLM([[TextDelta(text="Hel"), TextDelta(text="lo"), Done()]])
    agent.llm_client.stream_completion = llm.stream_completion

    events = list(agent.process_query("Say hello"))
    types = [event["type"] for event in events]

    # 1. Every streamed chunk reaches the consumer as its own event, in order.
    deltas = [event for event in events if event["type"] == "text_delta"]
    assert [delta.get("text") for delta in deltas] == ["Hel", "lo"], types

    # 2. Deltas precede the terminal turn event (no text arrives only at the end).
    turn_indices = [i for i, t in enumerate(types) if t == "turn"]
    assert turn_indices, types
    first_turn_idx = turn_indices[0]
    delta_indices = [i for i, t in enumerate(types) if t == "text_delta"]
    assert delta_indices and max(delta_indices) < first_turn_idx, types

    # 3. The deltas concatenate back to the terminal turn's content, exactly.
    turn_event = events[first_turn_idx]
    assert "".join(delta.get("text", "") for delta in deltas) == turn_event["content"]
    assert turn_event["content"] == "Hello"

    # 4. Deltas are attributable to the same display turn as their terminal event.
    assert all(delta.get("turn") == turn_event.get("turn") for delta in deltas), deltas
