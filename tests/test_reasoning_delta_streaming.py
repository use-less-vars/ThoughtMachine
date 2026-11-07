"""Regression test — the agent loop must surface streamed REASONING before the turn ends.

Contract PINNED by this test:

  ``Agent.process_query`` consumes ``llm_client.stream_completion`` and, for
  every ``ReasoningDelta`` chunk, accumulates it into ``reasoning`` AND yields
  a ``reasoning_delta`` event to its consumer (agent/core/agent.py, the
  ``for stream_event in _iter_stream_events(...)`` loop).  Before this wave the
  reasoning was accumulated silently — the UI only ever saw it at turn end, via
  the terminal ``turn`` event's ``reasoning`` key, so the "thinking" phase
  appeared frozen for its whole duration.

Each ``reasoning_delta`` event carries at least ``text`` and ``turn``, is
ordered before the terminal ``turn`` event, and concatenates back to exactly
the ``turn`` event's ``reasoning`` key (no duplication, no loss).  Reasoning
must NOT leak into the assistant's ``content``.

Written RED-first: without the ``reasoning_delta`` yield no such events are
emitted at all, so this test is the regression guard that fails if that yield
is removed.

Sibling: ``tests/test_text_delta_streaming.py`` pins the same contract for
``TextDelta`` -> ``text_delta``.
"""

from __future__ import annotations

from agent.config.models import AgentConfig
from agent.core.agent import Agent
from agent.core.stream_events import Done, ReasoningDelta, TextDelta


class _ScriptedLLM:
    def __init__(self, turns):
        self._turns = [list(turn) for turn in turns]
        self.calls = 0

    async def stream_completion(self, messages, tools=None, **kwargs):
        index = min(self.calls, len(self._turns) - 1)
        self.calls += 1
        for event in self._turns[index]:
            yield event


def test_process_query_yields_reasoning_delta_before_terminal_turn():
    config = AgentConfig(api_key="test-key", enable_logging=False)
    agent = Agent(config=config, session_id="test-session")

    llm = _ScriptedLLM([[
        ReasoningDelta(text="hmm"),
        ReasoningDelta(text=", let me think"),
        TextDelta(text="Hel"),
        TextDelta(text="lo"),
        Done(),
    ]])
    agent.llm_client.stream_completion = llm.stream_completion

    events = list(agent.process_query("Say hello"))
    types = [event["type"] for event in events]

    # 1. Every streamed reasoning chunk reaches the consumer as its own event.
    reasoning_deltas = [event for event in events if event["type"] == "reasoning_delta"]
    assert [delta.get("text") for delta in reasoning_deltas] == ["hmm", ", let me think"], types

    # 2. Reasoning deltas precede the terminal turn event (no thinking arrives
    #    only at the end).
    turn_indices = [i for i, t in enumerate(types) if t == "turn"]
    assert turn_indices, types
    first_turn_idx = turn_indices[0]
    reasoning_indices = [i for i, t in enumerate(types) if t == "reasoning_delta"]
    assert reasoning_indices and max(reasoning_indices) < first_turn_idx, types

    # 3. Reasoning deltas precede the assistant text deltas (thinking first).
    text_indices = [i for i, t in enumerate(types) if t == "text_delta"]
    assert text_indices, types
    assert max(reasoning_indices) < min(text_indices), types

    # 4. The deltas concatenate back to the terminal turn's reasoning, exactly.
    turn_event = events[first_turn_idx]
    assert "".join(delta.get("text", "") for delta in reasoning_deltas) == turn_event.get("reasoning")
    assert turn_event.get("reasoning") == "hmm, let me think"

    # 5. Reasoning never pollutes the assistant content.
    assert turn_event["content"] == "Hello"

    # 6. Deltas are attributable to the same display turn as their terminal event.
    assert all(delta.get("turn") == turn_event.get("turn") for delta in reasoning_deltas), reasoning_deltas
