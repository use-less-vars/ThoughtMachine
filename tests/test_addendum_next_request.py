"""The addendum applies on the NEXT REQUEST, not on the next restart.

The defect this file pins: composition used to live in
``LLMClient.ensure_system_prompt``, which runs only when an agent is built (and
on pause/resume / config apply) and which wrote the block into the stored
conversation. Adding the third entry to a running session therefore changed
nothing the model ever saw, and once the block was baked into
``user_history[0]`` an edit or a removal could not take effect at all: the
frozen snapshot was replayed forever.

The fix moves composition out of the stored prompt and onto the outgoing
payload: ``compose_addendum_into_messages`` (``agent/core/llm_client.py``) is
called once per request from ``Agent.process_query``. This file asserts the
observable consequence, one request at a time, on a real ``Agent``, a real
``Session`` and the real addendum writers:

1. exactly one system message in the composed payload (ruling D-2);
2. a write, an edit and a removal each reach the VERY NEXT request, with no
   rebuild, no resume and no restart;
3. the request that was already built is unchanged by the write (P3);
4. the base prompt prefix stays byte-identical and the block is only a tail
   (P4 - the prompt prefix is cache-stable);
5. nothing is baked into ``session.user_history``: composing copies the system
   message, it never mutates the builder's output (which aliases history);
6. a persisted entry set is what the next request sends, after a real
   ``FileSystemSessionStore`` round-trip.
"""

import copy
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from agent.config.models import AgentConfig
from agent.core.agent import Agent
from agent.core.llm_client import ADDENDUM_HEADING, compose_addendum_into_messages
from session.addendum import (
    remove_addendum_entry,
    update_addendum_entry,
    write_addendum_entry,
)
from session.context_builder import ContextBuilder
from session.models import Session
from session.store import FileSystemSessionStore


SESSION_ID = 'next-request-addendum'

TEXT_ONE = 'Prefer terse answers while the migration is running.'
TEXT_TWO = 'The repository is frozen until Friday; do not open pull requests.'
TEXT_EDITED = 'Prefer terse answers; the migration is finished.'


# ---------------------------------------------------------------------------
# Real session / agent / request build
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    """A FileSystemSessionStore rooted in this test's tmp dir."""
    return FileSystemSessionStore(
        sessions_dir=str(tmp_path / 'sessions'),
        state_dir=str(tmp_path / 'state'),
        enable_session_history_pruning=False,
    )


def _turn(index):
    return [
        {'role': 'user', 'content': f'Question {index}: what is next?'},
        {'role': 'assistant', 'content': f'Answer {index}: step {index} is done.'},
    ]


def _make_session(turns=2, session_id=SESSION_ID):
    history = []
    for index in range(turns):
        history.extend(_turn(index))
    return Session(session_id=session_id, user_history=history)


def _make_agent(session):
    config = AgentConfig(api_key='test-key', enable_logging=False)
    return Agent(config=config, session=session)


def _build(agent):
    """The payload as the turn loop builds it, BEFORE the addendum is composed."""
    messages = agent.context_builder.build(agent.conversation)
    return ContextBuilder._cleanup_orphaned_tool_messages(messages)


def _next_request(agent):
    """What the next message actually sends: build, clean, then compose.

    Mirrors ``Agent.process_query``. The composition happens on the OUTGOING
    list, so it is safe to build a request as often as a test wants.
    """
    return compose_addendum_into_messages(_build(agent), agent.session)


def _system_texts(payload):
    return [str(m.get('content', '')) for m in payload if m.get('role') == 'system']


def _the_system_text(payload):
    texts = _system_texts(payload)
    assert len(texts) == 1, f'exactly one system message expected, got {len(texts)}'
    return texts[0]


def _write(session, text):
    result = write_addendum_entry(session, text)
    assert result['status'] == 'ok', result
    return result


# ---------------------------------------------------------------------------
# 1. Exactly one system message, and it is the one at position 0
# ---------------------------------------------------------------------------

def test_composed_request_carries_exactly_one_system_message():
    session = _make_session()
    agent = _make_agent(session)
    _write(session, TEXT_ONE)

    payload = _next_request(agent)

    assert len(_system_texts(payload)) == 1
    assert payload[0].get('role') == 'system'
    assert ADDENDUM_HEADING in payload[0]['content']


# ---------------------------------------------------------------------------
# 2. Write / edit / remove each reach the VERY NEXT request
# ---------------------------------------------------------------------------

def test_entry_written_mid_session_appears_in_the_very_next_request():
    session = _make_session()
    agent = _make_agent(session)

    # The request that went out before the write: no addendum, byte-identical
    # to a session that has no addendum at all.
    in_flight = _next_request(agent)
    sent = copy.deepcopy(in_flight)
    assert ADDENDUM_HEADING not in _the_system_text(sent)

    _write(session, TEXT_ONE)

    # P3: the payload already sent is untouched by the write.
    assert in_flight == sent

    # ...and the next request carries the entry. No rebuild, no resume.
    next_request = _next_request(agent)
    system_text = _the_system_text(next_request)
    assert ADDENDUM_HEADING in system_text
    assert TEXT_ONE in system_text
    # Only the system message changed.
    assert next_request[1:] == sent[1:]


def test_edit_reaches_the_next_request_and_the_old_text_is_gone():
    session = _make_session()
    agent = _make_agent(session)
    entry = _write(session, TEXT_ONE)['entry']

    before = _the_system_text(_next_request(agent))
    assert TEXT_ONE in before

    result = update_addendum_entry(session, entry['id'], TEXT_EDITED)
    assert result['status'] == 'ok', result

    after = _the_system_text(_next_request(agent))
    assert TEXT_EDITED in after
    assert TEXT_ONE not in after, 'the edited entry still sends its OLD text'


def test_removal_disappears_from_the_next_request_but_the_base_prompt_stays():
    session = _make_session()
    agent = _make_agent(session)
    first = _write(session, TEXT_ONE)['entry']
    _write(session, TEXT_TWO)

    before = _the_system_text(_next_request(agent))
    assert TEXT_ONE in before and TEXT_TWO in before

    result = remove_addendum_entry(session, first['id'])
    assert result['status'] == 'ok', result

    after = _the_system_text(_next_request(agent))
    assert TEXT_ONE not in after, 'a removed entry is still being sent'
    assert TEXT_TWO in after
    assert after.startswith(agent.config.system_prompt)
    assert after.count(ADDENDUM_HEADING) == 1


# ---------------------------------------------------------------------------
# 3./4. Byte-stable prefix, tail-only block
# ---------------------------------------------------------------------------

def test_base_prompt_prefix_is_byte_stable_while_the_block_changes():
    session = _make_session()
    agent = _make_agent(session)
    base_prompt = agent.config.system_prompt

    _write(session, TEXT_ONE)
    first = _the_system_text(_next_request(agent))
    _write(session, TEXT_TWO)
    second = _the_system_text(_next_request(agent))

    assert first.startswith(base_prompt)
    assert second.startswith(base_prompt)
    assert first != second, 'the second entry never reached the prompt'
    # The block is a pure tail: everything up to the heading is the base prompt.
    for text in (first, second):
        assert text[:len(base_prompt)] == base_prompt
        assert text[len(base_prompt):].startswith(f'\n\n{ADDENDUM_HEADING}')


def test_two_compositions_with_a_changed_addendum_differ():
    session = _make_session()
    agent = _make_agent(session)

    _write(session, TEXT_ONE)
    one = _the_system_text(_next_request(agent))

    session.addendum['entries'][0]['text'] = TEXT_TWO
    two = _the_system_text(_next_request(agent))

    assert one != two
    assert TEXT_ONE in one and TEXT_ONE not in two
    assert TEXT_TWO in two and TEXT_TWO not in one
    assert one.count(ADDENDUM_HEADING) == 1
    assert two.count(ADDENDUM_HEADING) == 1


# ---------------------------------------------------------------------------
# 5. Anti-baking: composing never writes into the stored conversation
# ---------------------------------------------------------------------------

def test_composition_copies_the_payload_and_leaves_history_untouched():
    session = _make_session()
    agent = _make_agent(session)
    _write(session, TEXT_ONE)

    built = _build(agent)
    # ``user_history`` is a callback-carrying list subclass (not deep-copyable),
    # so the snapshot is per-message: one level is all this test mutates.
    stored_before = [dict(message) for message in session.user_history]

    composed = compose_addendum_into_messages(built, session)

    assert composed is not built, 'the composer must return a new list'
    assert composed[0] is not built[0], 'the system message must be a copy'
    assert ADDENDUM_HEADING in composed[0]['content']
    # The builder's output aliases session.user_history: it must not have been
    # rewritten in place...
    assert built[0]['content'] == stored_before[0]['content']
    # ...and the durable record holds no block at all.
    assert [dict(message) for message in session.user_history] == stored_before
    for message in session.user_history:
        assert ADDENDUM_HEADING not in str(message.get('content', ''))


def test_empty_addendum_returns_the_payload_unchanged():
    session = _make_session()
    agent = _make_agent(session)

    built = _build(agent)

    assert compose_addendum_into_messages(built, session) is built


# ---------------------------------------------------------------------------
# 6. Persistence: what was saved is what the next request sends
# ---------------------------------------------------------------------------

def test_persisted_entry_set_is_what_the_next_request_sends(store):
    session = _make_session()
    _write(session, TEXT_ONE)
    _write(session, TEXT_TWO)
    store.save_session(session)

    reloaded = store.load_session(SESSION_ID)
    assert reloaded is not None
    assert [entry['text'] for entry in reloaded.addendum['entries']] == [TEXT_ONE, TEXT_TWO]

    # A freshly started agent on the reloaded session sends the stored set.
    agent = _make_agent(reloaded)
    system_text = _the_system_text(_next_request(agent))

    assert TEXT_ONE in system_text and TEXT_TWO in system_text
    assert system_text.index(TEXT_ONE) < system_text.index(TEXT_TWO)
    # Stored order survives the round-trip; nothing sorts or rewrites it.
    assert [entry['text'] for entry in reloaded.addendum['entries']] == [TEXT_ONE, TEXT_TWO]
