"""Slice 4: the addendum write boundary and its mid-turn drain.

The brief (slice 4) asks for two things that are easy to fake and therefore easy
to get wrong:

  * ONE write boundary -- ``session.addendum.write_addendum_entry`` -- used by
    both writers ruling D-4 allows (``operator``, ``mailbox``), which validates,
    appends exactly one entry, persists and enqueues, and **never raises**
    (slice 1's contract: one bad write must not cost the session).
  * a pending queue drained at the *per-stream-event* poll, immediately after
    the halt poll and before the event-type ladder, so a write made mid-turn
    reaches the consumer **between tokens** and not at the turn boundary
    (ruling D-5, design doc §7.2).

Every test below drives the REAL ``Agent.process_query`` with a scripted async
stream (the same technique ``tests/test_agent_stop_midstream.py`` uses): the
write is performed *inside* the scripted generator, i.e. between two
``__anext__`` calls, which is exactly the window a synchronous consumer has. No
threads, no sleeps, no stubs of the drain or of the boundary.

``addendum_updated`` / ``runtime_notice`` are runtime-channel dicts (design doc
§7.3), so nothing here imports them from ``agent.core.stream_events``: one test
asserts that module still knows nothing about the addendum.
"""

import copy
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent.core.stream_events as stream_events_module
from agent.config.models import AgentConfig
from agent.core.agent import Agent
from agent.core.llm_client import ADDENDUM_HEADING
from agent.core.stream_events import Done, TextDelta
from session.addendum import AddendumQueue, write_addendum_entry
from session.context_builder import ContextBuilder
from session.models import Session


#: Ruling D-6: self-describing and idempotent, so ``entries`` is the FULL
#: resulting set and every event can be applied without a diff to replay.
UPDATED_KEYS = {'type', 'session_id', 'entry_id', 'ts', 'source', 'entries'}

#: Tier 2 (design doc §7.3): a dropped write reports itself on the same channel.
NOTICE_KEYS = {'type', 'session_id', 'level', 'code', 'message'}

#: The slice-1 entry shape, unchanged by the write boundary.
ENTRY_KEYS = {'id', 'ts', 'source', 'text', 'pinned'}

SESSION_ID = 'slice4-addendum-drain'


# ---------------------------------------------------------------------------
# Scripted stream + real Agent
# ---------------------------------------------------------------------------

class _ScriptedLLM:
    """Scripted async stream; ``before_event`` runs before each ``yield``.

    Running the write hook *before* the yield is what puts it between two
    ``__anext__`` calls, so the test observes the drain where a real operator
    write would land: mid-stream, not before the turn or after it.
    """

    def __init__(self, turns, before_event=None):
        self._turns = [list(turn) for turn in turns]
        self._before_event = before_event
        self.calls = 0
        self.closed = False
        self.yielded = 0

    async def stream_completion(self, messages, tools=None, **kwargs):
        index = min(self.calls, len(self._turns) - 1)
        self.calls += 1
        try:
            for position, event in enumerate(self._turns[index]):
                if self._before_event is not None:
                    self._before_event(position)
                yield event
                self.yielded += 1
        finally:
            self.closed = True


def _make_session():
    return Session(
        session_id=SESSION_ID,
        user_history=[
            {'role': 'user', 'content': 'earlier question'},
            {'role': 'assistant', 'content': 'earlier answer'},
        ],
    )


def _make_agent(session):
    config = AgentConfig(api_key='test-key', enable_logging=False)
    return Agent(config=config, session=session)


def _build_request(agent):
    """agent/core/agent.py:1297-1298 -- what the next LLM request is built from."""
    messages = agent.context_builder.build(agent.conversation)
    return ContextBuilder._cleanup_orphaned_tool_messages(messages)


def _types(events):
    return [event.get('type') for event in events]


def _indices(types, wanted):
    return [index for index, event_type in enumerate(types) if event_type == wanted]


def _write_on(positions, session, text, source='operator', log=None):
    """A ``before_event`` hook that writes once, at the given stream positions."""
    def before_event(position):
        if position in positions:
            result = write_addendum_entry(session, text, source=source)
            if log is not None:
                log.append(result)
    return before_event


# ---------------------------------------------------------------------------
# (a) the drain runs per stream event, between tokens, not at the turn boundary
# ---------------------------------------------------------------------------

def test_drain_fires_between_token_events_not_at_the_turn_boundary():
    """A write made between two deltas is delivered between those two deltas."""
    session = _make_session()
    agent = _make_agent(session)
    writes = []

    llm = _ScriptedLLM(
        [[TextDelta(text='A'), TextDelta(text='B'), TextDelta(text='C'), Done()]],
        before_event=_write_on({1}, session, 'First mid-turn note.', log=writes),
    )
    agent.llm_client.stream_completion = llm.stream_completion

    events = list(agent.process_query('hello there'))
    types = _types(events)

    assert len(writes) == 1 and writes[0]['status'] == 'ok', writes

    updated = _indices(types, 'addendum_updated')
    assert len(updated) == 1, f'expected exactly one addendum_updated, got {types}'
    at = updated[0]

    # It rides BETWEEN the token that preceded the write and the next one: the
    # drain is per stream event, not a single flush at the turn boundary.
    assert events[at - 1]['type'] == 'text_delta' and events[at - 1]['text'] == 'A', (
        f'addendum_updated did not land directly after delta A: {types}'
    )
    assert events[at + 1]['type'] == 'text_delta' and events[at + 1]['text'] == 'B', (
        f'addendum_updated did not land directly before delta B: {types}'
    )

    # Two more deltas (and the turn terminal) were still to come, so this is
    # genuinely mid-turn delivery rather than an end-of-turn report.
    assert _types(events[at:]).count('text_delta') == 2, types
    assert at < types.index('turn'), types

    # Runtime-channel dict, never a StreamEvent, and stream_events.py is untouched.
    assert isinstance(events[at], dict) and not hasattr(events[at], 'reason')
    assert set(events[at]) == UPDATED_KEYS, sorted(events[at])
    assert 'addendum' not in inspect.getsource(stream_events_module).lower(), (
        'the addendum leaked into agent/core/stream_events.py'
    )

    # The stream itself was consumed to its Done terminal and closed normally.
    # (``yielded`` counts post-yield, so the terminal that breaks the loop is
    # consumed but not counted: 3 deltas, then Done.)
    assert llm.closed is True, 'the stream was not closed'
    assert llm.yielded == 3, (llm.yielded, types)


def test_addendum_updated_is_delivered_on_the_done_terminal():
    """The drain fires on ``Done`` too: the last event also drains the queue."""
    session = _make_session()
    agent = _make_agent(session)
    writes = []

    # The write happens after the ONLY delta has already been yielded, so the
    # single remaining poll is the one attached to the ``Done`` terminal.
    llm = _ScriptedLLM(
        [[TextDelta(text='only'), Done()]],
        before_event=_write_on({1}, session, 'Written on the terminal.'),
    )
    agent.llm_client.stream_completion = llm.stream_completion

    events = list(agent.process_query('say one word'))
    types = _types(events)

    updated = _indices(types, 'addendum_updated')
    assert len(updated) == 1, types
    at = updated[0]
    deltas = _indices(types, 'text_delta')
    assert deltas == [at - 1], f'the drain did not fire on the terminal event: {types}'
    assert events[at - 1]['text'] == 'only'
    assert _types(events[at:]).count('text_delta') == 0, types
    assert at < types.index('turn'), types


def test_drain_sits_after_the_halt_poll_and_before_the_type_ladder():
    """Source order: halt poll, then drain, then the isinstance ladder.

    Pinned structurally rather than behaviourally because the ``Interrupted``
    rung cannot be driven end to end: it re-raises an ``LLMError`` into the
    retry/backoff handlers below (agent/core/agent.py:1458-1471), so a scripted
    stream cannot reach the ladder rung for it. The ordering is what makes the
    drain fire for every event including both terminals.
    """
    source = inspect.getsource(Agent.process_query)

    drain_at = source.index('self._drain_addendum_queue()')
    halt_at = source.rindex('if self.should_halt():', 0, drain_at)
    ladder_at = source.index('if isinstance(stream_event, TextDelta):', drain_at)
    done_at = source.index('isinstance(stream_event, Done)', ladder_at)
    interrupted_at = source.index('isinstance(stream_event, Interrupted)', ladder_at)

    assert halt_at < drain_at, 'the drain runs before the halt poll'
    assert drain_at < ladder_at, 'the drain runs after the event-type ladder opens'
    assert ladder_at < done_at < interrupted_at, (
        f'the ladder no longer runs TextDelta -> Done -> Interrupted: '
        f'{ladder_at}, {done_at}, {interrupted_at}'
    )

    # Exactly one drain call site in the loop: the poll is not duplicated.
    assert source.count('self._drain_addendum_queue()') == 1


# ---------------------------------------------------------------------------
# (b) exactly once / idempotent across repeated drains
# ---------------------------------------------------------------------------

def test_drain_is_exactly_once_across_repeated_drains():
    session = _make_session()
    agent = _make_agent(session)

    first = write_addendum_entry(session, 'note one')
    second = write_addendum_entry(session, 'note two')
    assert first['status'] == 'ok' and second['status'] == 'ok'

    drained = list(agent._drain_addendum_queue())
    assert [payload['entry_id'] for payload in drained] == [
        first['entry']['id'], second['entry']['id'],
    ], 'the queue did not preserve write order'

    # Nothing survives a drain: a second drain in the SAME turn re-delivers none.
    assert list(agent._drain_addendum_queue()) == []


def test_queue_drain_pops_and_is_empty_afterwards():
    """``drain`` pops (deque.popleft), which is what makes delivery one-shot."""
    queue = AddendumQueue()
    queue.enqueue({'type': 'addendum_updated', 'n': 1})
    queue.enqueue({'type': 'addendum_updated', 'n': 2})

    assert len(queue) == 2
    assert [payload['n'] for payload in queue.drain()] == [1, 2]
    assert len(queue) == 0
    assert queue.drain() == []


# ---------------------------------------------------------------------------
# (c) exact payload keys, and ``entries`` is the FULL resulting set (D-6)
# ---------------------------------------------------------------------------

def test_addendum_updated_payload_has_exact_keys_and_full_entry_set():
    session = _make_session()
    agent = _make_agent(session)
    assert session.addendum['entries'] == []

    operator = write_addendum_entry(session, 'Operator note.', source='operator')
    mailbox = write_addendum_entry(session, 'Mailbox note.', source='mailbox')
    assert operator['status'] == 'ok' and mailbox['status'] == 'ok'

    for result in (operator, mailbox):
        assert set(result['entry']) == ENTRY_KEYS, sorted(result['entry'])
        assert result['entry']['pinned'] is False
        assert isinstance(result['entry']['ts'], str) and result['entry']['ts']

    payloads = list(agent._drain_addendum_queue())
    assert len(payloads) == 2, payloads
    head, tail = payloads

    for payload in payloads:
        assert isinstance(payload, dict), type(payload)
        assert set(payload) == UPDATED_KEYS, sorted(payload)
        assert payload['type'] == 'addendum_updated'
        assert payload['session_id'] == SESSION_ID
        assert [entry['id'] for entry in payload['entries']] == [
            entry['id'] for entry in session.addendum['entries'][:len(payload['entries'])]
        ]

    # Head reports the state after the first write; tail carries the FULL set,
    # not the delta -- that is what makes D-6 idempotent and out-of-order safe.
    assert head['entry_id'] == operator['entry']['id']
    assert head['source'] == 'operator'
    assert head['ts'] == operator['entry']['ts']
    assert [entry['id'] for entry in head['entries']] == [operator['entry']['id']]

    assert tail['entry_id'] == mailbox['entry']['id']
    assert tail['source'] == 'mailbox'
    assert [entry['id'] for entry in tail['entries']] == [
        operator['entry']['id'], mailbox['entry']['id'],
    ]
    assert [entry['text'] for entry in tail['entries']] == [
        'Operator note.', 'Mailbox note.',
    ]
    for entry in tail['entries']:
        assert set(entry) == ENTRY_KEYS, sorted(entry)

    # ``entries`` is a copy: a consumer cannot corrupt the session through it.
    head['entries'].append({'id': 'e-injected', 'ts': '', 'source': 'operator',
                            'text': 'injected', 'pinned': False})
    head['entries'][0]['text'] = 'mutated by the consumer'
    assert [entry['id'] for entry in session.addendum['entries']] == [
        operator['entry']['id'], mailbox['entry']['id'],
    ]
    assert session.addendum['entries'][0]['text'] == 'Operator note.'


def test_entry_ids_are_unique_across_writes():
    session = _make_session()
    results = [write_addendum_entry(session, f'note {index}') for index in range(5)]

    ids = [result['entry']['id'] for result in results]
    assert all(isinstance(entry_id, str) and entry_id for entry_id in ids), ids
    assert len(set(ids)) == len(ids), f'duplicate entry ids: {ids}'

    # The stored order is the write order -- nothing sorts or rewrites history.
    assert [entry['id'] for entry in session.addendum['entries']] == ids
    assert [entry['text'] for entry in session.addendum['entries']] == [
        f'note {index}' for index in range(5)
    ]


# ---------------------------------------------------------------------------
# (d) a dropped write reports a runtime_notice and never raises
# ---------------------------------------------------------------------------

BAD_TEXTS = ('', '   \n\t ', None, 42, ['not a string'])
BAD_TEXT_REASONS = [
    'empty_text', 'empty_text', 'invalid_text', 'invalid_text', 'invalid_text',
]


def test_dropped_write_emits_runtime_notice_and_never_raises():
    session = _make_session()
    agent = _make_agent(session)

    results = [write_addendum_entry(session, text) for text in BAD_TEXTS]
    assert [result['status'] for result in results] == ['dropped'] * len(BAD_TEXTS)
    assert [result['reason'] for result in results] == BAD_TEXT_REASONS
    for result in results:
        assert result['entry'] is None
        assert result['entries'] == []

    # Ruling D-4: only the operator and the mailbox may write (C2 -- the model
    # must never write its own memory).
    bad_source = write_addendum_entry(session, 'a perfectly good note', source='tool')
    assert bad_source['status'] == 'dropped'
    assert bad_source['reason'] == 'invalid_source'
    assert bad_source['entry'] is None

    # Nothing was persisted by any of the six rejected writes.
    assert session.addendum['entries'] == []

    payloads = list(agent._drain_addendum_queue())
    assert [payload['type'] for payload in payloads] == ['runtime_notice'] * 6, payloads
    assert [payload['code'] for payload in payloads] == BAD_TEXT_REASONS + ['invalid_source']
    for payload in payloads:
        assert set(payload) == NOTICE_KEYS, sorted(payload)
        assert payload['level'] == 'warning'
        assert payload['session_id'] == SESSION_ID
        assert isinstance(payload['message'], str) and payload['message']


def test_write_with_no_session_returns_a_drop_instead_of_raising():
    result = write_addendum_entry(None, 'nowhere to put this')

    assert result['status'] == 'dropped'
    assert result['reason'] == 'no_session'
    assert result['entry'] is None
    assert result['entries'] == []


def test_write_without_an_attached_queue_still_persists():
    """A session with no Agent attached simply does not notify; the write stands."""
    session = _make_session()
    assert not hasattr(session, 'addendum_queue'), (
        'a plain Session should not carry a queue -- the Agent attaches one'
    )

    result = write_addendum_entry(session, 'persisted with nobody listening')

    assert result['status'] == 'ok'
    assert len(session.addendum['entries']) == 1
    assert session.addendum['entries'][0]['text'] == 'persisted with nobody listening'


# ---------------------------------------------------------------------------
# (e) P3: the in-flight request is unchanged; the NEXT one differs only in system
# ---------------------------------------------------------------------------

def test_p3_in_flight_request_unchanged_and_next_request_differs_only_in_system():
    session = _make_session()
    agent = _make_agent(session)

    # The request this turn already sent (agent/core/agent.py:1297-1298).
    snapshot = copy.deepcopy(_build_request(agent))
    assert [message.get('role') for message in snapshot] == ['system', 'user', 'assistant']
    assert ADDENDUM_HEADING not in snapshot[0]['content'], 'the addendum predates the write'

    result = write_addendum_entry(session, 'Prefer terse answers while the migration runs.')
    assert result['status'] == 'ok'

    # P3: a Tier-1 write must not retroactively alter the request in flight.
    assert _build_request(agent) == snapshot, (
        'the write changed the in-flight request; it must only affect the next turn'
    )

    # The refresh is the once-per-turn call at agent/core/agent.py:1181.
    agent.conversation = agent.llm_client.ensure_system_prompt(agent.conversation)

    next_request = _build_request(agent)
    assert next_request[0]['role'] == 'system'
    assert next_request[0]['content'] != snapshot[0]['content'], (
        'the next request still carries the pre-write system prompt'
    )
    assert next_request[0]['content'].startswith(agent.config.system_prompt), (
        'the base system prompt prefix was lost'
    )
    assert ADDENDUM_HEADING in next_request[0]['content']
    assert 'Prefer terse answers while the migration runs.' in next_request[0]['content']

    # Only the system message changed: the addendum rides in the single composed
    # system string (ruling D-2) and touches no other message.
    assert next_request[1:] == snapshot[1:]
    assert len(next_request) == len(snapshot)
