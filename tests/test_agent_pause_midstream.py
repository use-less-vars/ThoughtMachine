"""Mid-stream PAUSE contract: a pause cuts generation without committing a final answer.

The pause that lands WHILE a stream is in flight must stop the stream at the
delta boundary and keep what the user already watched arrive as a DRAFT -- not
as a committed assistant answer:

  * exactly one ``stream_paused`` event, carrying the pre-pause partial text;
  * NO ``interrupted`` event (``interrupted`` is terminal vocabulary; a pause is
    resumable) and no ``role: 'assistant'`` message holding that text;
  * exactly one ``role: 'paused_draft'`` history entry carrying it;
  * the pause edge is NOT consumed by the stream loop (``_pause_requested``
    stays True -- the reading-mode checkpoints own that consume), and the
    'pause' level restriction is still in force afterwards.

The mechanism is a per-query capture of a monotonically increasing pause
EPOCH, compared as a level in the stream loop.  That is what keeps a pause
that was already in force BEFORE the turn began from cutting a stream it
predates (test 2), and what keeps two pauses in one turn both visible.

Determinism: the halt predicate is a pure call counter and the LLM is a
scripted async generator; the pause is raised from inside that generator,
between two deltas.  No threads, sleeps or timeouts.  ``_iter_stream_events``
guarantees ``agen.aclose()``, so the scripted generator's own ``finally`` must
run; that is asserted directly on the fake's ``closed`` flag.
"""

from __future__ import annotations

from agent.config.models import AgentConfig
from agent.core.agent import Agent
from agent.core.stream_events import Done, TextDelta
from session.models import Session


class _ScriptedLLM:
    """Scripted async stream; ``after_delta`` fires between two deltas."""

    def __init__(self, turns, after_delta=None):
        self._turns = [list(turn) for turn in turns]
        self.calls = 0
        self.closed = False
        self.deltas_yielded = 0
        self._after_delta = after_delta

    async def stream_completion(self, messages, tools=None, **kwargs):
        index = min(self.calls, len(self._turns) - 1)
        self.calls += 1
        try:
            for event in self._turns[index]:
                yield event
                if isinstance(event, TextDelta):
                    self.deltas_yielded += 1
                    if self._after_delta is not None:
                        self._after_delta()
        finally:
            self.closed = True


def _make_agent(session_id="test-session"):
    config = AgentConfig(api_key="test-key", enable_logging=False)
    return Agent(config=config, session_id=session_id)


def _roles(agent):
    return [m.get("role") for m in agent.conversation]


def _messages_with_role(agent, role):
    return [m for m in agent.conversation if m.get("role") == role]


def _events_of(events, kind):
    return [event for event in events if event["type"] == kind]


def test_pause_midstream_keeps_partial_as_draft_not_final():
    """The pause cuts the stream at the delta boundary; the partial is a draft."""
    agent = _make_agent("test-session-pause-midstream")

    def pause_between_deltas():
        agent.enter_reading_mode("pause")

    llm = _ScriptedLLM(
        [[TextDelta(text="Par"), TextDelta(text="tial"), Done()]],
        after_delta=pause_between_deltas,
    )
    agent.llm_client.stream_completion = llm.stream_completion

    events = list(agent.process_query("tell me a long story"))
    types = [event["type"] for event in events]

    # Exactly one delta reached the consumer: the pause is checked BEFORE the
    # next __anext__(), so text the model had queued was never requested.
    deltas = _events_of(events, "text_delta")
    assert len(deltas) == 1, types
    assert deltas[0]["text"] == "Par"

    # Exactly one stream_paused, carrying the pre-pause partial text.
    paused = _events_of(events, "stream_paused")
    assert len(paused) == 1, types
    assert paused[0]["content"] == "Par"

    # Not terminal: an 'interrupted' event is stop vocabulary and must not fire.
    assert "interrupted" not in types, types
    assert _events_of(events, "interrupted") == [], types

    # Not committed as a final assistant answer.
    assert _messages_with_role(agent, "assistant") == [], agent.conversation
    # ... but the partial text is not lost: it is a DISTINCT draft entry.
    drafts = _messages_with_role(agent, "paused_draft")
    assert len(drafts) == 1, agent.conversation
    assert "Par" in str(drafts[0].get("content", ""))

    # THE STALE-PAUSE PIN: the stream loop must not consume the pause edge --
    # the reading-mode checkpoints (and their pinned tests) own that consume.
    assert agent._pause_requested is True
    # The durable level is untouched too: the session is still paused.
    assert agent.state.restriction_reason == "pause"

    # The abandoned stream was closed by _iter_stream_events' finally.
    assert llm.closed is True


def test_pause_before_the_turn_starts_does_not_cut_generation():
    """A pause in force BEFORE the turn began must not cut a stream it predates."""
    agent = _make_agent("test-session-pause-before")
    llm = _ScriptedLLM([[TextDelta(text="Ful"), TextDelta(text="l answer"), Done()]])
    agent.llm_client.stream_completion = llm.stream_completion

    agent.enter_reading_mode("pause")  # the durable level is set before turn start

    events = list(agent.process_query("say something"))
    types = [event["type"] for event in events]

    assert "stream_paused" not in types, types
    assert len(_events_of(events, "text_delta")) == 2, types

    assistants = _messages_with_role(agent, "assistant")
    assert len(assistants) == 1, agent.conversation
    assert assistants[0]["content"] == "Full answer"
    assert _messages_with_role(agent, "paused_draft") == [], agent.conversation


def test_stop_outranks_pause_midstream():
    """Both armed mid-stream: the stop wins, exactly as it did before the pause."""
    agent = _make_agent("test-session-stop-over-pause")

    def pause_between_deltas():
        agent.enter_reading_mode("pause")

    llm = _ScriptedLLM(
        [[TextDelta(text="Par"), TextDelta(text="tial"), Done()]],
        after_delta=pause_between_deltas,
    )
    agent.llm_client.stream_completion = llm.stream_completion

    polls = {"n": 0}

    def halt_check():
        # False on the first poll (delta #1 is allowed through), True on the
        # second -- so at that iteration BOTH a stop and a pause are live.
        polls["n"] += 1
        return polls["n"] > 1

    agent.halt_check = halt_check

    events = list(agent.process_query("tell me a long story"))
    types = [event["type"] for event in events]

    assert "stream_paused" not in types, types
    interrupted = _events_of(events, "interrupted")
    assert len(interrupted) == 1, types
    assert interrupted[0]["stop_reason"] == "interrupted"
    assert interrupted[0]["content"] == "Par"
    assert _messages_with_role(agent, "paused_draft") == [], agent.conversation
    assert [m.get("content") for m in _messages_with_role(agent, "assistant")] == ["Par"]


def test_paused_draft_survives_session_roundtrip():
    """The distinct draft role must survive persistence with its role intact."""
    draft_text = "Par\n[Paused by the user - partial draft, resumable]"
    session = Session()
    session.add_message("user", "tell me a long story")
    session.add_message("paused_draft", draft_text)

    restored = Session.from_persistable_dict(session.to_persistable_dict())

    drafts = [m for m in restored.user_history if m.get("role") == "paused_draft"]
    assert len(drafts) == 1, [m.get("role") for m in restored.user_history]
    assert drafts[0]["content"] == draft_text
