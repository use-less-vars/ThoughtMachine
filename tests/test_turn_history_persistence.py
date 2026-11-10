"""Pin the invariant: an answer given on turn 1 must still be visible to the
model when the turn-2 request is built.

Everything here runs through PRODUCTION code -- nothing is reimplemented:

  * ``agent.core.turn_transaction.TurnTransaction``
        ``add_assistant_message(message)``        l.44
        ``add_tool_result(tool_result)``          l.64
        ``has_assistant_message()``               l.172
        ``commit_assistant_only()``               l.72
        ``commit()``                              l.102
  * ``session.context_builder.SummaryBuilder``
        ``build(user_history)``                   l.215
  * ``session.context_builder.ContextBuilder``
        ``_cleanup_orphaned_tool_messages(messages)``  static, l.110

The last two are the exact request-time pipeline of ``agent/core/agent.py``:
``context_builder.build(conversation)`` (l.1694) followed by
``ContextBuilder._cleanup_orphaned_tool_messages(...)`` (l.1704).

Message shapes mirror production:
  * assistant with tool calls -> agent/core/agent.py:2040-2046
        {'role': 'assistant', 'content': content, 'tool_calls': tool_calls}
    where each wire call is {'id', 'type': 'function', 'function': {...}}
    (agent/core/agent.py:1800).
  * tool result -> agent/core/tool_executor.py:391
        {'role': 'tool', 'tool_call_id': ..., 'content': tool_result}
    ``Respond.execute()`` returns ``self.content`` (tools/respond.py) so the
    tool result *is* the answer text, and the answer is also carried in the
    call arguments (``{"content": ...}``).

Import order matters: ``agent.core.message_utils`` must be imported before
``session.context_builder`` to break the circular import chain
(session.context_builder -> agent.core.message -> agent.__init__ ->
agent.core.agent -> session.context_builder). Same dance as
tests/test_emergency_mode.py.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

# Break the circular import chain (see module docstring).
from agent.core.message_utils import group_messages_into_turns as _  # noqa: F401
from agent.core.turn_transaction import TurnTransaction
from session.context_builder import ContextBuilder, SummaryBuilder
from session.models import ObservableList


ANSWER = 'The answer to your first question is FORTY-TWO.'
CALL_ID = 'call_respond_1'


# ── Helpers ──────────────────────────────────────────────────────────────────

class _FakeSession:
    """The only surface ``TurnTransaction`` touches.

    ``turn_transaction.py`` needs ``session.user_history`` (extended) and
    ``session.updated_at`` (assigned) -- see l.99-100 and l.128-129.
    """

    def __init__(self) -> None:
        self.user_history = ObservableList()
        self.updated_at = None


def _respond_tool_calls(content: str = ANSWER, call_id: str = CALL_ID) -> List[Dict[str, Any]]:
    """Wire-format tool_calls for a ``Respond`` call (agent/core/agent.py:1800)."""
    return [{
        'id': call_id,
        'type': 'function',
        'function': {
            'name': 'Respond',
            'arguments': json.dumps({'content': content, 'response_type': 'answer'}),
        },
    }]


def _respond_tool_result(content: str = ANSWER, call_id: str = CALL_ID) -> Dict[str, Any]:
    """A committed ``Respond`` tool result (agent/core/tool_executor.py:391)."""
    return {
        'role': 'tool',
        'tool_call_id': call_id,
        'content': content,
        'response_type': 'answer',
    }


def _assistant_tool_call_msg(
    content: str = '', call_id: str = CALL_ID, answer: str = ANSWER
) -> Dict[str, Any]:
    """Assistant message with tool_calls, as built at agent/core/agent.py:2040-2046."""
    return {
        'role': 'assistant',
        'content': content,
        'reasoning_content': '',
        'tool_calls': _respond_tool_calls(answer, call_id),
    }


def _build_request(history: List[Dict[str, Any]], builder: SummaryBuilder) -> List[Dict[str, Any]]:
    """What the next LLM request is built from (agent/core/agent.py:1694 + 1704)."""
    messages = builder.build(history)
    return ContextBuilder._cleanup_orphaned_tool_messages(messages)


def _flatten(messages: List[Dict[str, Any]]) -> str:
    """Serialise a request so text anywhere in it (content, tool args) is searchable."""
    return json.dumps(messages, ensure_ascii=False, default=str)


# ── 1. Baseline: the normal two-turn Respond round-trip ──────────────────────

def test_two_turn_respond_answer_survives():
    """Turn 1 answers via ``Respond``; turn 2's request must still contain it.

    History written through the real ``TurnTransaction`` (the normal commit
    path: assistant committed, tool result committed, agent/core/agent.py
    2052-2117), then ``SummaryBuilder.build`` + the request-time cleanup.
    """
    session = _FakeSession()
    session.user_history.append({'role': 'user', 'content': 'What is the answer?'})

    tx = TurnTransaction(session)
    tx.add_assistant_message(_assistant_tool_call_msg())
    tx.add_tool_result(_respond_tool_result())
    tx.commit()

    session.user_history.append({'role': 'user', 'content': 'Say that again?'})
    assert len(session.user_history) == 4

    request = _build_request(session.user_history, SummaryBuilder())

    # The answer is present, and it is present as a *tool result attached to a
    # tool_calls assistant* -- i.e. the sequence survived intact rather than
    # being flattened.
    assert ANSWER in _flatten(request)
    assistants = [
        m for m in request
        if m.get('role') == 'assistant' and m.get('tool_calls')
    ]
    assert len(assistants) == 1, f'expected the tool_calls assistant to survive, got {request}'
    assert [tc['id'] for tc in assistants[0]['tool_calls']] == [CALL_ID]
    tool_results = [m for m in request if m.get('role') == 'tool']
    assert len(tool_results) == 1
    assert tool_results[0]['tool_call_id'] == CALL_ID
    assert tool_results[0]['content'] == ANSWER


# ── 2. Orphaned Respond tool call (commit_assistant_only / halt) ─────────────

@pytest.mark.xfail(
    strict=True,
    reason=(
        'ContextBuilder._cleanup_orphaned_tool_messages (session/context_builder.py:157-163) '
        "pops 'tool_calls' off an assistant whose tool results never landed, and that assistant's "
        "'content' is empty -- the Respond payload (the answer) lived only in the tool call "
        "arguments, so turn 1's answer is silently dropped from turn 2's request."
    ),
)
def test_respond_answer_survives_orphaned_tool_call():
    """No tool result ever lands; the answer must not silently vanish.

    Reachable in production two ways, both of which call
    ``TurnTransaction.commit_assistant_only()``:
      * normal path commits the assistant before tool execution
        (agent/core/agent.py:2039-2053) and the turn is then interrupted /
        paused / stopped before the tool results are committed;
      * the halt path commits a partial assistant
        (agent/core/agent.py:1847-1853).
    """
    session = _FakeSession()
    session.user_history.append({'role': 'user', 'content': 'What is the answer?'})

    tx = TurnTransaction(session)
    tx.add_assistant_message(_assistant_tool_call_msg())
    assert tx.has_assistant_message() is True
    tx.commit_assistant_only()
    # No tx.add_tool_result(...): the tool result was never committed.

    session.user_history.append({'role': 'user', 'content': 'Say that again?'})

    request = _build_request(session.user_history, SummaryBuilder())

    assert ANSWER in _flatten(request), (
        'turn-1 answer is not visible to the model on turn 2; request was: '
        f'{json.dumps(request, default=str)}'
    )


# ── 3. pruning_keep_recent_turns must actually prune ─────────────────────────

def test_summary_pruning_honours_keep_recent_turns():
    """The turns a summary says it kept must still reach the model.

    ``SummaryBuilder``'s own docstring (session/context_builder.py:196-208)
    promises "First N turns after summary (where N =
    pruning_keep_recent_turns from summary metadata). These are the turns that
    were kept from BEFORE the summarization event." The summary message is
    appended to the append-only history (agent/core/agent.py:2331-2332), i.e.
    it sits AFTER the turns it says it kept -- exactly the position where
    ``pruning_keep_recent_turns`` has to do the work.
    """
    history: List[Dict[str, Any]] = [
        {'role': 'system', 'content': 'You are a helpful assistant.'},
    ]
    for i in range(1, 6):
        history.append({'role': 'user', 'content': f'question-{i}'})
        history.append({'role': 'assistant', 'content': f'answer-{i}'})

    history.append({
        'role': 'system',
        'content': 'Summary of previous conversation: user asked question-1 .. question-3.',
        'summary': True,
        'pruning_keep_recent_turns': 2,
        'pruning_insertion_idx': len(history),
    })

    request = _build_request(history, SummaryBuilder())
    text = _flatten(request)

    # The last 2 turns are the ones the summary claims to have kept.
    assert 'question-4' in text and 'answer-4' in text, (
        f'kept turn 4 missing from the request: {json.dumps(request, default=str)}'
    )
    assert 'question-5' in text and 'answer-5' in text, (
        f'kept turn 5 missing from the request: {json.dumps(request, default=str)}'
    )


# ── 4. The emergency 20% trim must not slice a turn in half ─────────────────

def _assert_tool_sequences_intact(messages: List[Dict[str, Any]]) -> None:
    """Every tool result has its assistant tool call, and vice versa."""
    pending: Dict[str, bool] = {}
    for index, msg in enumerate(messages):
        role = msg.get('role')
        if role == 'assistant' and msg.get('tool_calls'):
            assert not pending, (
                f'assistant at {index} starts while tool calls {sorted(pending)} are unresolved'
            )
            for tc in msg['tool_calls']:
                pending[tc.get('id')] = True
        elif role == 'tool':
            tool_call_id = msg.get('tool_call_id')
            assert tool_call_id in pending, (
                f'orphaned tool result {tool_call_id!r} at index {index} '
                f'(no preceding assistant tool call for it)'
            )
            pending.pop(tool_call_id)
        else:
            assert not pending, (
                f'message at {index} follows unresolved tool calls {sorted(pending)}'
            )
    assert not pending, f'tool calls {sorted(pending)} never got a result'


def test_emergency_trim_does_not_decapitate_a_turn():
    """Emergency mode cuts the oldest 20% of messages; it must cut on a turn boundary.

    ``SummaryBuilder.build`` (session/context_builder.py:264-267) slices
    ``non_system`` by raw message count, which can land inside a turn. Whatever
    it leaves behind must still be structurally valid for the provider.
    """
    history: List[Dict[str, Any]] = [
        {'role': 'system', 'content': 'You are a helpful assistant.'},
    ]
    # Irregular turn sizes so a 20% cut cannot reliably land on a boundary.
    # Turn 1 is also a 3-message turn, so the 20 turns hold 47 non-system
    # messages and the emergency cut removes int(47 * 0.2) == 9 of them.
    # Turn boundaries fall after message 3/5/8/10, so message 9 sits INSIDE
    # turn 4: the cut takes turn 4's user message and leaves its assistant.
    for i in range(1, 21):
        history.append({'role': 'user', 'content': f'question-{i}'})
        if i % 3 == 0 or i == 1:
            # A turn that used a tool: assistant + tool result.
            history.append(_assistant_tool_call_msg(answer=f'answer-{i}'))
            history.append(_respond_tool_result(content=f'answer-{i}'))
        else:
            history.append({'role': 'assistant', 'content': f'answer-{i}'})

    builder = SummaryBuilder(default_keep_turns=5)
    builder.emergency_mode = True
    request = _build_request(history, builder)

    # The emergency path really ran (it dropped messages).
    assert len(request) < len(history), (
        'emergency trim did not remove anything; this test would be vacuous'
    )
    _assert_tool_sequences_intact(request)
