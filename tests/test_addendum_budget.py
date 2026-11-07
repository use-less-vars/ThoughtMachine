"""Slice 5: the addendum budget is enforced at the one write boundary.

``session.addendum.write_addendum_entry`` is the ONLY addendum write path, so the
budget is enforced there and nowhere else. What is asserted here:

  * the measurement is an exact character count over the RESULTING entry set
    (existing + candidate), and ``max_entries`` is a separate count limit;
  * the verdict is REFUSE only -- the entry is never truncated and no existing
    entry is ever evicted to make room;
  * the refusal is ATOMIC: the persisted addendum is byte-identical to what it
    was before the attempt, nothing is enqueued for the drain except the
    ``runtime_notice`` that reports the refusal, and nothing raises;
  * the refusal is VISIBLE: the same ``runtime_notice`` channel the existing
    rejections use, ``level='warning'``, with the two new codes
    ``budget_exceeded`` / ``max_entries_exceeded`` carrying the real numbers;
  * there is no grandfathering: a session already over budget can never accept
    another write, because its resulting set is over budget too.

The harness is the one ``tests/test_addendum_drain.py`` already uses: a real
``Session`` plus a real ``Agent`` (which attaches the ``AddendumQueue`` the
notice travels on), then the queue is drained and inspected. No stubs.
"""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config.models import AgentConfig
from agent.core.agent import Agent
from session.addendum import write_addendum_entry
from session.models import (
    DEFAULT_ADDENDUM_BUDGET_CHARS,
    DEFAULT_ADDENDUM_MAX_ENTRIES,
    Session,
    normalize_addendum,
)


#: Ruling S-E: the caps are operator-owned. These tests read them and never
#: change them (and never change the session schema).
BUDGET = DEFAULT_ADDENDUM_BUDGET_CHARS     # 8000 chars
MAX_ENTRIES = DEFAULT_ADDENDUM_MAX_ENTRIES  # 64 entries

SESSION_ID = 'slice5-addendum-budget'

#: Same payload keys as every other rejection notice.
NOTICE_KEYS = {'type', 'session_id', 'level', 'code', 'message'}

TS = '2025-01-01T00:00:00Z'


# ---------------------------------------------------------------------------
# Real session + real Agent (queue attached), as in test_addendum_drain.py
# ---------------------------------------------------------------------------

def _entry(entry_id, text, source='operator', pinned=False):
    return {'id': entry_id, 'ts': TS, 'source': source, 'text': text, 'pinned': pinned}


def _make_session(entries=(), budget_chars=BUDGET, max_entries=MAX_ENTRIES):
    return Session(
        session_id=SESSION_ID,
        user_history=[
            {'role': 'user', 'content': 'earlier question'},
            {'role': 'assistant', 'content': 'earlier answer'},
        ],
        addendum=normalize_addendum({
            'entries': list(entries),
            'budget_chars': budget_chars,
            'max_entries': max_entries,
        }),
    )


def _make_agent(session):
    config = AgentConfig(api_key='test-key', enable_logging=False)
    return Agent(config=config, session=session)


def _stored_chars(session):
    return sum(len(entry['text']) for entry in session.addendum['entries'])


def _notices(payloads):
    return [payload for payload in payloads if payload.get('type') == 'runtime_notice']


# ---------------------------------------------------------------------------
# (1) a write landing EXACTLY on ``budget_chars`` is accepted
# ---------------------------------------------------------------------------

def test_write_landing_exactly_on_budget_is_accepted():
    session = _make_session()
    agent = _make_agent(session)
    assert session.addendum['budget_chars'] == 8000, 'ruling S-E: 8000 stays the default'

    first = write_addendum_entry(session, 'a' * (BUDGET - 1))
    assert first['status'] == 'ok', first
    assert _stored_chars(session) == BUDGET - 1

    # The decisive write: the RESULTING set is exactly at the budget, not under
    # it and not over it, so it must land (the check is on the resulting set,
    # not on the size of the candidate alone).
    second = write_addendum_entry(session, 'b')
    assert second['status'] == 'ok', second
    assert second['reason'] is None
    assert _stored_chars(session) == BUDGET == 8000
    assert [entry['id'] for entry in session.addendum['entries']] == [
        first['entry']['id'], second['entry']['id'],
    ]

    # Nothing was refused, so the queue carries only the two updates.
    payloads = list(agent._drain_addendum_queue())
    assert [payload['type'] for payload in payloads] == ['addendum_updated'] * 2, payloads


# ---------------------------------------------------------------------------
# (2) one character over is REFUSED, with both numbers in the notice
# ---------------------------------------------------------------------------

def test_write_one_char_over_budget_is_refused_with_both_numbers():
    session = _make_session()
    agent = _make_agent(session)
    assert session.addendum['entries'] == []

    result = write_addendum_entry(session, 'a' * (BUDGET + 1))

    assert result['status'] == 'dropped'
    assert result['reason'] == 'budget_exceeded'
    assert result['entry'] is None
    assert result['entries'] == []
    assert session.addendum['entries'] == [], 'the refused entry was persisted anyway'

    payloads = list(agent._drain_addendum_queue())
    assert len(payloads) == 1, payloads
    notice = payloads[0]
    assert notice['type'] == 'runtime_notice'
    assert set(notice) == NOTICE_KEYS, sorted(notice)
    assert notice['level'] == 'warning'
    assert notice['code'] == 'budget_exceeded'
    assert notice['session_id'] == SESSION_ID
    # The message names the real numbers: what was measured and what the cap is.
    assert str(BUDGET + 1) in notice['message'], notice['message']
    assert str(BUDGET) in notice['message'], notice['message']
    assert notice['message'] == f'addendum budget exceeded: {BUDGET + 1} > {BUDGET} chars'


# ---------------------------------------------------------------------------
# (3) exactly ``max_entries`` is accepted; one more is REFUSED
# ---------------------------------------------------------------------------

def test_exactly_max_entries_accepted_and_one_more_refused():
    session = _make_session()
    agent = _make_agent(session)
    assert session.addendum['max_entries'] == 64, 'ruling S-E: 64 stays the default'

    results = [write_addendum_entry(session, 'n') for _ in range(MAX_ENTRIES)]
    assert [result['status'] for result in results] == ['ok'] * MAX_ENTRIES
    assert len(session.addendum['entries']) == MAX_ENTRIES == 64
    # The character budget is nowhere near its cap, so the count is the only
    # limit under test here.
    assert _stored_chars(session) == MAX_ENTRIES < BUDGET

    over = write_addendum_entry(session, 'one too many')

    assert over['status'] == 'dropped'
    assert over['reason'] == 'max_entries_exceeded'
    assert over['entry'] is None
    assert len(session.addendum['entries']) == MAX_ENTRIES, 'the 65th entry was appended anyway'

    payloads = list(agent._drain_addendum_queue())
    assert len(payloads) == MAX_ENTRIES + 1, [payload['type'] for payload in payloads]
    notice = payloads[-1]
    assert set(notice) == NOTICE_KEYS, sorted(notice)
    assert notice['level'] == 'warning'
    assert notice['code'] == 'max_entries_exceeded'
    assert str(MAX_ENTRIES + 1) in notice['message'], notice['message']
    assert str(MAX_ENTRIES) in notice['message'], notice['message']


# ---------------------------------------------------------------------------
# (4) the refusal is atomic: no entry set change, nothing enqueued, no raise
# ---------------------------------------------------------------------------

def test_refusal_is_atomic():
    existing = [
        _entry('e-001', 'Prefer terse answers.'),
        _entry('e-002', 'The repository is frozen until Friday.', source='mailbox', pinned=True),
    ]
    # A small cap so the arithmetic is readable; the entry limit is set higher
    # than the writes below so the CHARACTER budget is the binding limit here.
    session = _make_session(existing, budget_chars=100, max_entries=4)
    agent = _make_agent(session)

    before_addendum = copy.deepcopy(session.addendum)
    before_entries = copy.deepcopy(session.addendum['entries'])
    assert _stored_chars(session) == sum(len(entry['text']) for entry in existing)

    # Over the 100-char cap by exactly one character: the write must not raise,
    # must not be persisted and must not touch the two existing entries (no
    # eviction, and nothing truncated to make room).
    result = write_addendum_entry(session, 'x' * (100 - _stored_chars(session) + 1))

    assert result['status'] == 'dropped'
    assert result['reason'] == 'budget_exceeded'
    assert result['entry'] is None
    assert result['entries'] == []

    # Byte-identical: same entries, same ids, same order, same caps.
    assert session.addendum == before_addendum
    assert session.addendum['entries'] == before_entries
    assert [entry['id'] for entry in session.addendum['entries']] == ['e-001', 'e-002']
    assert session.addendum['budget_chars'] == 100
    assert session.addendum['max_entries'] == 4

    # Nothing was enqueued for the drain except the refusal notice itself: no
    # ``addendum_updated`` means no consumer can believe the write landed.
    payloads = list(agent._drain_addendum_queue())
    assert [payload['type'] for payload in payloads] == ['runtime_notice'], payloads
    assert payloads[0]['code'] == 'budget_exceeded'

    # And the boundary is still usable afterwards: the refusal cost nothing.
    room = write_addendum_entry(session, 'y' * (100 - _stored_chars(session)))
    assert room['status'] == 'ok', room
    assert _stored_chars(session) == 100


# ---------------------------------------------------------------------------
# (5) an ordinary under-budget write still emits ``addendum_updated``, no notice
# ---------------------------------------------------------------------------

def test_under_budget_write_emits_updated_and_no_notice():
    session = _make_session()
    agent = _make_agent(session)

    result = write_addendum_entry(session, 'Prefer terse answers while the migration runs.')

    assert result['status'] == 'ok'
    assert result['reason'] is None
    assert len(session.addendum['entries']) == 1

    payloads = list(agent._drain_addendum_queue())
    assert len(payloads) == 1, payloads
    updated = payloads[0]
    assert updated['type'] == 'addendum_updated'
    assert updated['entry_id'] == result['entry']['id']
    assert [entry['text'] for entry in updated['entries']] == [
        'Prefer terse answers while the migration runs.'
    ]
    assert _notices(payloads) == [], 'a successful write must not emit a notice'


# ---------------------------------------------------------------------------
# (6) no grandfathering: an already over-budget session cannot write (S-D)
# ---------------------------------------------------------------------------

def test_already_over_budget_session_cannot_write():
    # A session whose persisted state predates enforcement: one 9000-char entry
    # against the 8000-char cap. There is no grace policy and no eviction, so
    # every further write is over budget by construction.
    oversized = _entry('e-legacy', 'z' * (BUDGET + 1000))
    session = _make_session([oversized])
    agent = _make_agent(session)
    assert _stored_chars(session) == BUDGET + 1000 > BUDGET

    before_addendum = copy.deepcopy(session.addendum)

    result = write_addendum_entry(session, 'a one-character note')

    assert result['status'] == 'dropped'
    assert result['reason'] == 'budget_exceeded'
    assert result['entry'] is None
    assert session.addendum == before_addendum
    assert [entry['id'] for entry in session.addendum['entries']] == ['e-legacy']

    payloads = list(agent._drain_addendum_queue())
    assert [payload['type'] for payload in payloads] == ['runtime_notice'], payloads
    notice = payloads[0]
    assert notice['code'] == 'budget_exceeded'
    assert str(BUDGET + 1000 + len('a one-character note')) in notice['message']
    assert str(BUDGET) in notice['message']
