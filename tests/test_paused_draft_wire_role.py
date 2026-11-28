"""A paused draft is model-facing history -- and it must be SENDABLE.

A mid-stream pause leaves the partial answer in the transcript under its own
``paused_draft`` role (see ``_finalize_paused_stream``).  That entry is what the
next turn's request carries, so two things have to hold:

  * it reaches the model-facing payload at all (the draft text and the resumable
    marker are in the exact ``messages`` handed to the provider);
  * the role never reaches a provider's WIRE format.  The OpenAI chat API accepts
    only system/user/assistant/tool and rejects the whole request on anything
    else, while the Anthropic adapter's catch-all coercion would silently turn
    any unknown role into an assistant turn.  Both adapters therefore map the
    internal role onto ``assistant`` and agree about it.

Measured before the fix: ``build_wire_messages`` passed the role through
verbatim, so the outgoing OpenAI request carried ``role: 'paused_draft'``.
"""

from __future__ import annotations

from agent.config.models import AgentConfig
from agent.core.agent import Agent
from agent.core.stream_events import Done, TextDelta
from llm_providers.anthropic_provider import AnthropicProvider
from llm_providers.base import ProviderConfig
from llm_providers.openai_compatible import build_wire_messages
from session.context_builder import ContextBuilder, SummaryBuilder

DRAFT_TEXT = "Par"
MARKER = "[Paused by the user - partial draft, resumable]"
WIRE_ROLES = {"system", "user", "assistant", "tool"}


class _ScriptedLLM:
    """Scripted async stream; ``after_delta`` fires between two deltas."""

    def __init__(self, turns, after_delta=None, record=None):
        self._turns = [list(turn) for turn in turns]
        self.calls = 0
        self.closed = False
        self._after_delta = after_delta
        self._record = record

    async def stream_completion(self, messages, tools=None, **kwargs):
        if self._record is not None:
            self._record.append(messages)
        index = min(self.calls, len(self._turns) - 1)
        self.calls += 1
        try:
            for event in self._turns[index]:
                yield event
                if isinstance(event, TextDelta) and self._after_delta is not None:
                    self._after_delta()
        finally:
            self.closed = True


def _make_agent(session_id):
    return Agent(config=AgentConfig(api_key="test-key", enable_logging=False), session_id=session_id)


def _paused_agent(session_id, record=None):
    """Run one turn, paused mid-stream, leaving a ``paused_draft`` behind."""
    agent = _make_agent(session_id)
    llm = _ScriptedLLM(
        [[TextDelta(text=DRAFT_TEXT), TextDelta(text="tial"), Done()]],
        after_delta=lambda: agent.enter_reading_mode("pause"),
        record=record,
    )
    agent.llm_client.stream_completion = llm.stream_completion
    events = list(agent.process_query("tell me a long story"))
    assert [e["type"] for e in events].count("stream_paused") == 1, events
    assert [m for m in agent.conversation if m.get("role") == "paused_draft"], agent.conversation
    return agent


def _anthropic_wire(messages):
    provider = AnthropicProvider(ProviderConfig(api_key="test-key", model="claude-3-5-sonnet"))
    system_prompt, wire = provider._serialize_messages(messages)
    return system_prompt, wire


# --------------------------------------------------------------------------
# GAP 2: the draft IS in the model-facing payload.
# --------------------------------------------------------------------------
def test_paused_draft_reaches_the_model_facing_payload_on_the_next_turn():
    """The partial answer is sent to the model on the next turn, marker included."""
    recorded = []
    agent = _paused_agent("wire-role-next-turn", record=recorded)

    # Turn 2 runs unpaused; ``recorded`` captures the exact payload it built.
    agent.leave_reading_mode("pause")
    agent._pause_requested = False
    quiet = _ScriptedLLM([[TextDelta(text="ok"), Done()]], record=recorded)
    agent.llm_client.stream_completion = quiet.stream_completion
    list(agent.process_query("continue"))

    payload = recorded[-1]
    draft_entries = [m for m in payload if DRAFT_TEXT in str(m.get("content", ""))]
    assert draft_entries, payload
    assert any(MARKER in str(m.get("content", "")) for m in draft_entries), payload
    assert [m.get("role") for m in draft_entries] == ["paused_draft"], draft_entries


def test_paused_draft_survives_summary_build_and_orphan_cleanup():
    """Neither context building nor the orphaned-tool cleanup drops the draft."""
    agent = _paused_agent("wire-role-build")

    built = SummaryBuilder().build(agent.conversation)
    kept = [m for m in built if m.get("role") == "paused_draft"]
    assert len(kept) == 1, built
    assert DRAFT_TEXT in kept[0]["content"] and MARKER in kept[0]["content"]

    cleaned = ContextBuilder._cleanup_orphaned_tool_messages(list(built))
    assert len([m for m in cleaned if m.get("role") == "paused_draft"]) == 1, cleaned


# --------------------------------------------------------------------------
# GAP 1: the role never reaches either provider's wire format.
# --------------------------------------------------------------------------
def test_openai_wire_maps_paused_draft_to_assistant():
    """OpenAI accepts only its own roles; the draft travels as assistant."""
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "tell me a long story"},
        {"role": "paused_draft", "content": DRAFT_TEXT + "\n" + MARKER},
    ]
    wire = build_wire_messages(history, model="gpt-4o-mini")

    assert [m["role"] for m in wire] == ["system", "user", "assistant"], wire
    assert wire[2]["content"] == DRAFT_TEXT + "\n" + MARKER
    # The canonical transcript is untouched: the draft role is a storage detail.
    assert history[2]["role"] == "paused_draft"


def test_anthropic_wire_maps_paused_draft_to_assistant():
    """The same role, the same destination role, in the Anthropic adapter."""
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "tell me a long story"},
        {"role": "paused_draft", "content": DRAFT_TEXT + "\n" + MARKER},
    ]
    system_prompt, wire = _anthropic_wire(history)

    assert system_prompt == "sys"
    assert [m["role"] for m in wire] == ["user", "assistant"], wire
    assert wire[1]["content"] == DRAFT_TEXT + "\n" + MARKER


def test_no_internal_role_reaches_either_wire_after_a_real_pause():
    """End to end: a paused transcript is sendable by both adapters, verbatim text."""
    agent = _paused_agent("wire-role-e2e")
    built = SummaryBuilder().build(agent.conversation)
    messages = ContextBuilder._cleanup_orphaned_tool_messages(list(built))

    openai_wire = build_wire_messages(messages, model="gpt-4o-mini")
    assert {m["role"] for m in openai_wire} <= WIRE_ROLES, openai_wire
    assert any(DRAFT_TEXT in str(m.get("content")) for m in openai_wire), openai_wire

    _system_prompt, anthropic_wire = _anthropic_wire(messages)
    assert {m["role"] for m in anthropic_wire} <= WIRE_ROLES, anthropic_wire
    assert any(DRAFT_TEXT in str(m.get("content")) for m in anthropic_wire), anthropic_wire

    # Both adapters place the draft on the same side of the conversation.
    assert [m["role"] for m in openai_wire][-1] == "assistant", openai_wire
    assert anthropic_wire[-1]["role"] == "assistant", anthropic_wire
