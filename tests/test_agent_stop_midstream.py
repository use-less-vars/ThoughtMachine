"""Hard-stop (halt) contract for the in-stream Stop button.

Contract PINNED AND SATISFIED by the current implementation:

  ``Agent.process_query`` polls ``Agent.should_halt()`` at the top of every
  stream iteration (agent/core/agent.py, the ``if self.should_halt():`` guard).
  A truthy halt breaks out of the stream, and the ``if halted:`` block after the
  loop commits the partial assistant text via ``TurnTransaction`` and yields a
  single terminal ``interrupted`` event carrying ``stop_reason='interrupted'``.

Two properties matter and are both pinned here:

  1. The partial text the user already watched stream in is KEPT - it is the
     committed assistant message, byte-for-byte the concatenation of the
     ``text_delta`` events that preceded the halt.
  2. A halt BEFORE any delta commits NOTHING (the empty-message regression):
     ``TurnTransaction.has_assistant_message()`` gates the commit, so no blank
     assistant bubble is pushed into history.

Determinism: the halt predicate is a pure call counter and the LLM is a
scripted async generator - no threads, sleeps or timeouts. ``_iter_stream_events``
guarantees ``agen.aclose()``, so the scripted generator's own ``finally`` must
run; that is asserted directly on the fake's ``closed`` flag.
"""

from __future__ import annotations

from agent.config.models import AgentConfig
from agent.core.agent import Agent
from agent.core.stream_events import Done, TextDelta
from agent.core.turn_transaction import TurnTransaction


class _ScriptedLLM:
    """Scripted async stream that records whether it was closed."""

    def __init__(self, turns):
        self._turns = [list(turn) for turn in turns]
        self.calls = 0
        self.closed = False
        self.deltas_yielded = 0

    async def stream_completion(self, messages, tools=None, **kwargs):
        index = min(self.calls, len(self._turns) - 1)
        self.calls += 1
        try:
            for event in self._turns[index]:
                yield event
                if isinstance(event, TextDelta):
                    self.deltas_yielded += 1
        finally:
            self.closed = True


def _make_agent(session_id="test-session"):
    config = AgentConfig(api_key="test-key", enable_logging=False)
    return Agent(config=config, session_id=session_id)


def _assistant_messages(agent):
    return [m for m in agent.conversation if m.get("role") == "assistant"]


def test_halt_after_first_delta_keeps_partial_text_and_closes_stream():
    """(a) One delta seen, then halt: 1 text_delta, 1 interrupted, partial kept."""
    agent = _make_agent()
    llm = _ScriptedLLM([[TextDelta(text="Par"), TextDelta(text="tial"), TextDelta(text=" text"), Done()]])
    agent.llm_client.stream_completion = llm.stream_completion

    polls = {"n": 0}

    def halt_check():
        # False on the first poll (delta #1 is allowed through), True on the
        # second - so the halt lands after exactly one delta.
        polls["n"] += 1
        return polls["n"] > 1

    agent.halt_check = halt_check
    history_before = len(agent.conversation)

    events = list(agent.process_query("tell me a long story"))
    types = [event["type"] for event in events]

    # Exactly one delta reached the consumer before the halt.
    deltas = [event for event in events if event["type"] == "text_delta"]
    assert len(deltas) == 1, types
    assert deltas[0]["text"] == "Par"

    # Exactly one interrupted terminal, carrying the partial content.
    interrupted = [event for event in events if event["type"] == "interrupted"]
    assert len(interrupted) == 1, types
    terminal = interrupted[0]
    assert terminal["stop_reason"] == "interrupted"
    assert terminal["content"] == "Par"
    assert terminal["turn"] == deltas[0]["turn"]

    # No normal turn terminal - the halt replaces it rather than doubling it.
    assert "turn" not in types, types

    # The committed assistant message equals the partial text EXACTLY.
    assistants = _assistant_messages(agent)
    assert len(assistants) == 1, assistants
    assert assistants[0]["content"] == "Par"
    assert len(agent.conversation) == history_before + 2, agent.conversation  # user + assistant

    # The abandoned stream was closed by _iter_stream_events' finally.
    assert llm.closed is True
    # Only one delta was ever pulled from the stream: the halt is checked BEFORE
    # the next __anext__(), so text the model had queued is never requested.
    assert llm.deltas_yielded == 1, llm.deltas_yielded


def test_halt_before_any_delta_commits_no_assistant_message():
    """(b) Empty-halt regression: nothing committed, terminal still emitted."""
    agent = _make_agent(session_id="test-session-empty")
    llm = _ScriptedLLM([[TextDelta(text="never"), TextDelta(text=" seen"), Done()]])
    agent.llm_client.stream_completion = llm.stream_completion

    agent.halt_check = lambda: True  # halted from the very first poll
    history_before = len(agent.conversation)

    events = list(agent.process_query("say something"))
    types = [event["type"] for event in events]

    # No text_delta was ever consumed.
    assert [event for event in events if event["type"] == "text_delta"] == [], types

    # The terminal is still emitted so the UI can clear its in-flight state.
    interrupted = [event for event in events if event["type"] == "interrupted"]
    assert len(interrupted) == 1, types
    assert interrupted[0]["stop_reason"] == "interrupted"
    assert interrupted[0]["content"] == ""

    # No blank assistant bubble in history. Only the user query was added.
    assert _assistant_messages(agent) == [], agent.conversation
    assert len(agent.conversation) == history_before + 1, agent.conversation


def test_has_assistant_message_is_content_based():
    """(c) has_assistant_message() ignores empty assistant messages."""
    # Nothing added at all.
    empty_tx = TurnTransaction(None, None, conversation=[])
    assert empty_tx.has_assistant_message() is False

    # Empty content and no tool_calls -> not a message.
    blank_tx = TurnTransaction(None, None, conversation=[])
    blank_tx.add_assistant_message({"role": "assistant", "content": ""})
    assert blank_tx.has_assistant_message() is False

    # Real content -> a message.
    text_tx = TurnTransaction(None, None, conversation=[])
    text_tx.add_assistant_message({"role": "assistant", "content": "hi"})
    assert text_tx.has_assistant_message() is True

    # Tool-calls only -> still a message.
    tool_tx = TurnTransaction(None, None, conversation=[])
    tool_tx.add_assistant_message({
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "1", "type": "function", "function": {"name": "x", "arguments": "{}"}}],
    })
    assert tool_tx.has_assistant_message() is True


def test_normal_turn_is_undisturbed_by_the_halt_path():
    """(d) Happy path: one committed message, no interrupted event."""
    agent = _make_agent(session_id="test-session-normal")
    llm = _ScriptedLLM([[TextDelta(text="Hel"), TextDelta(text="lo"), Done()]])
    agent.llm_client.stream_completion = llm.stream_completion
    # halt_check left unset -> must behave exactly as before the halt feature.
    history_before = len(agent.conversation)

    events = list(agent.process_query("Say hello"))
    types = [event["type"] for event in events]

    assert "interrupted" not in types, types
    assert len([event for event in events if event["type"] == "text_delta"]) == 2, types

    assistants = _assistant_messages(agent)
    assert len(assistants) == 1, assistants
    assert assistants[0]["content"] == "Hello"
    assert len(agent.conversation) == history_before + 2, agent.conversation
