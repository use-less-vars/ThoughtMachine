"""Addendum cap self-heal + the two entry mutations (update / remove).

The reported bug: one workspace "cannot add an addendum no matter what". The
root cause class is a PERSISTED session whose ``budget_chars`` / ``max_entries``
make every write refuse forever, with nothing able to repair it --
``normalize_addendum`` type-checked those fields but never range-checked them,
and "clear all" preserves them.

What is asserted here:

  (a) an out-of-range cap is reset to its DEFAULT on load, with one WARNING, and
      never raises; a record that held ``budget_chars: 0`` can take a write
      afterwards (before the range check that write always came back
      ``budget_exceeded``);
  (b) ``update_addendum_entry`` swaps the text in place, keeps the entry's
      identity (id / ts / pinned / source), re-measures the RESULTING set, is
      all-or-nothing on refusal, and refuses an unknown id with
      ``entry_not_found``;
  (c) ``remove_addendum_entry`` removes exactly one entry, refuses an unknown id,
      and is the RECOVERY path: removing entries brings an over-budget set back
      under its cap so the next write lands;
  (d) every mutation survives a persistence round trip (entries AND caps);
  (e) the guard: ``normalize_addendum`` is still fail-soft for a non-dict
      addendum, a non-list ``entries`` and an invalid entry object.

No stubs: real ``Session`` objects, real boundary functions, real result values.
"""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from session.addendum import (
    remove_addendum_entry,
    update_addendum_entry,
    write_addendum_entry,
)
from session.models import (
    DEFAULT_ADDENDUM_BUDGET_CHARS,
    DEFAULT_ADDENDUM_MAX_ENTRIES,
    MAX_ADDENDUM_BUDGET_CHARS,
    MAX_ADDENDUM_MAX_ENTRIES,
    MIN_ADDENDUM_BUDGET_CHARS,
    MIN_ADDENDUM_MAX_ENTRIES,
    Session,
    normalize_addendum,
)

TS = '2025-01-01T00:00:00Z'


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def _entry(entry_id, text, ts=TS, source='operator', pinned=False):
    return {'id': entry_id, 'ts': ts, 'source': source, 'text': text, 'pinned': pinned}


def _make_session(entries=(), budget_chars=DEFAULT_ADDENDUM_BUDGET_CHARS,
                  max_entries=DEFAULT_ADDENDUM_MAX_ENTRIES):
    return Session(
        session_id='mutations-addendum',
        addendum=normalize_addendum({
            'entries': list(entries),
            'budget_chars': budget_chars,
            'max_entries': max_entries,
        }),
    )


def _persistable(addendum):
    """A persistable dict for a fresh session, with the addendum replaced."""
    data = Session().to_persistable_dict()
    data['addendum'] = addendum
    return data


def _reload(session):
    """Round trip through the persistence representation."""
    return Session.from_persistable_dict(copy.deepcopy(session.to_persistable_dict()))


@pytest.fixture
def log_records(monkeypatch):
    """Capture everything ``session.models`` logs (level, channel, message)."""
    captured = []

    def fake_log(level, channel, message, **kwargs):
        captured.append((level, channel, message))

    monkeypatch.setattr('session.models.log', fake_log)
    return captured


def _cap_warnings(log_records, field):
    return [record for record in log_records
            if record[0] == 'WARNING' and f'{field}=' in record[2]]


# ---------------------------------------------------------------------------
# (a) the caps are range-checked and SELF-HEALED on load
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('bad_budget', [0, -5, 10 ** 9, '8000'])
def test_out_of_range_budget_chars_is_reset_to_the_default(bad_budget, log_records):
    data = _persistable({'entries': [], 'budget_chars': bad_budget,
                         'max_entries': DEFAULT_ADDENDUM_MAX_ENTRIES})

    session = Session.from_persistable_dict(data)  # must never raise

    assert session.addendum['budget_chars'] == DEFAULT_ADDENDUM_BUDGET_CHARS
    # The other cap is untouched: one bad field does not cost the good one.
    assert session.addendum['max_entries'] == DEFAULT_ADDENDUM_MAX_ENTRIES

    budget_warnings = _cap_warnings(log_records, 'budget_chars')
    assert len(budget_warnings) == 1, budget_warnings
    level, channel, message = budget_warnings[0]
    assert (level, channel) == ('WARNING', 'core.session')
    # The warning names the bad value and the value used instead.
    assert repr(bad_budget) in message, message
    assert str(DEFAULT_ADDENDUM_BUDGET_CHARS) in message, message
    if isinstance(bad_budget, int):
        # ... and, for a wrong-but-int value, the accepted range too.
        assert f'[{MIN_ADDENDUM_BUDGET_CHARS}, {MAX_ADDENDUM_BUDGET_CHARS}]' in message, message


@pytest.mark.parametrize('bad_max_entries', [0, -1, 10 ** 9, '64'])
def test_out_of_range_max_entries_is_reset_to_the_default(bad_max_entries, log_records):
    data = _persistable({'entries': [], 'budget_chars': DEFAULT_ADDENDUM_BUDGET_CHARS,
                         'max_entries': bad_max_entries})

    session = Session.from_persistable_dict(data)  # must never raise

    assert session.addendum['max_entries'] == DEFAULT_ADDENDUM_MAX_ENTRIES
    assert session.addendum['budget_chars'] == DEFAULT_ADDENDUM_BUDGET_CHARS

    entry_warnings = _cap_warnings(log_records, 'max_entries')
    assert len(entry_warnings) == 1, entry_warnings
    level, channel, message = entry_warnings[0]
    assert (level, channel) == ('WARNING', 'core.session')
    assert repr(bad_max_entries) in message, message
    assert str(DEFAULT_ADDENDUM_MAX_ENTRIES) in message, message
    if isinstance(bad_max_entries, int):
        assert f'[{MIN_ADDENDUM_MAX_ENTRIES}, {MAX_ADDENDUM_MAX_ENTRIES}]' in message, message


def test_a_legal_small_cap_survives_the_load_unchanged(log_records):
    """The heal is not a bulldozer: a small-but-legal cap is a real setting."""
    session = Session.from_persistable_dict(_persistable({
        'entries': [], 'budget_chars': 250, 'max_entries': 3,
    }))

    assert session.addendum['budget_chars'] == 250
    assert session.addendum['max_entries'] == 3
    assert _cap_warnings(log_records, 'budget_chars') == []
    assert _cap_warnings(log_records, 'max_entries') == []


def test_record_that_held_budget_zero_can_now_take_a_write():
    """THE bug: ``budget_chars: 0`` used to survive the load, so every write was
    refused ``budget_exceeded`` -- forever, with no way to repair the record."""
    data = _persistable({'entries': [], 'budget_chars': 0, 'max_entries': 0})

    session = Session.from_persistable_dict(data)
    assert session.addendum['budget_chars'] == DEFAULT_ADDENDUM_BUDGET_CHARS
    assert session.addendum['max_entries'] == DEFAULT_ADDENDUM_MAX_ENTRIES

    # 200 characters: far past the 0-char cap the record used to carry.
    result = write_addendum_entry(session, 'a usable note ' * 13 + 'and then some')

    assert result['status'] == 'ok', result
    assert result['reason'] is None
    assert len(session.addendum['entries']) == 1
    assert result['attempted_chars'] == len(session.addendum['entries'][0]['text'])
    assert result['attempted_chars'] > 0
    # And the repair is PERSISTED with the write, not just used once.
    assert session.addendum['budget_chars'] == DEFAULT_ADDENDUM_BUDGET_CHARS
    assert session.addendum['max_entries'] == DEFAULT_ADDENDUM_MAX_ENTRIES


# ---------------------------------------------------------------------------
# (b) update_addendum_entry
# ---------------------------------------------------------------------------

def _two_entry_session(**caps):
    return _make_session([
        _entry('e-1', 'first text', ts='2025-02-03T04:05:06+00:00',
               source='operator', pinned=True),
        _entry('e-2', 'second text', ts='2025-02-04T05:06:07+00:00', source='mailbox'),
    ], **caps)


def test_update_replaces_text_and_keeps_the_entry_identity():
    session = _two_entry_session()
    untouched = copy.deepcopy(session.addendum['entries'][1])

    result = update_addendum_entry(session, 'e-1', 'revised text')

    assert result['status'] == 'ok', result
    assert result['reason'] is None

    stored = session.addendum['entries'][0]
    assert stored['text'] == 'revised text'
    # The identity is preserved exactly -- nothing else about the entry changes.
    assert stored['id'] == 'e-1'
    assert stored['ts'] == '2025-02-03T04:05:06+00:00'
    assert stored['pinned'] is True
    assert stored['source'] == 'operator'
    # No new field: the entry normaliser would drop it on the next read anyway.
    assert set(stored) == {'id', 'ts', 'source', 'text', 'pinned'}, sorted(stored)
    # The neighbouring entry is not touched, and the order is unchanged.
    assert session.addendum['entries'][1] == untouched
    assert [entry['id'] for entry in session.addendum['entries']] == ['e-1', 'e-2']

    # Totals are the RESULTING set: the old text is swapped out, not added to.
    expected_total = len('revised text') + len('second text')
    assert result['attempted_chars'] == expected_total
    assert result['total_chars'] == expected_total
    assert result['attempted_entries'] == 2
    assert result['budget_chars'] == DEFAULT_ADDENDUM_BUDGET_CHARS
    assert result['max_entries'] == DEFAULT_ADDENDUM_MAX_ENTRIES
    assert [entry['text'] for entry in result['entries']] == ['revised text', 'second text']
    assert result['entry'] == stored


def test_update_of_unknown_id_is_entry_not_found_and_changes_nothing():
    session = _two_entry_session()
    before = copy.deepcopy(session.addendum)

    result = update_addendum_entry(session, 'nope', 'irrelevant')

    assert result['status'] == 'dropped'
    assert result['reason'] == 'entry_not_found'
    assert result['message'] == "no addendum entry with id 'nope'"
    assert result['entry'] is None
    assert result['entries'] == []
    assert session.addendum == before


def test_update_that_would_exceed_the_budget_is_refused_and_the_old_text_survives():
    session = _make_session([_entry('e-1', 'a' * 100), _entry('e-2', 'b' * 100)],
                            budget_chars=250)
    before = copy.deepcopy(session.addendum)

    # 100 (e-2) + 151 = 251 > 250. Measured on the RESULTING set, so this is the
    # tightest refusal: one character over.
    refused = update_addendum_entry(session, 'e-1', 'c' * 151)

    assert refused['status'] == 'dropped'
    assert refused['reason'] == 'budget_exceeded'
    assert refused['attempted_chars'] == 251
    assert refused['budget_chars'] == 250
    # On a refusal the reported total is the UNCHANGED set.
    assert refused['total_chars'] == 200
    # All-or-nothing: the stored entry text is exactly what it was.
    assert session.addendum == before
    assert session.addendum['entries'][0]['text'] == 'a' * 100

    # The swap, not the sum, is what is measured: 150 replaces the 100, so the
    # set lands exactly on the 250-character cap (a naive "add" would be 350).
    accepted = update_addendum_entry(session, 'e-1', 'c' * 150)

    assert accepted['status'] == 'ok', accepted
    assert accepted['attempted_chars'] == 250
    assert accepted['budget_chars'] == 250
    assert accepted['total_chars'] == 250
    assert session.addendum['entries'][0]['text'] == 'c' * 150
    assert session.addendum['entries'][1]['text'] == 'b' * 100


def test_update_to_blank_text_is_empty_text_and_changes_nothing():
    session = _two_entry_session()
    before = copy.deepcopy(session.addendum)

    for blank in ('', '   ', ' \n\t '):
        result = update_addendum_entry(session, 'e-1', blank)

        assert result['status'] == 'dropped', result
        assert result['reason'] == 'empty_text'
        assert result['entry'] is None

    assert session.addendum == before
    assert session.addendum['entries'][0]['text'] == 'first text'


def test_update_rejects_a_non_string_text_and_an_illegal_source():
    session = _two_entry_session()
    before = copy.deepcopy(session.addendum)

    assert update_addendum_entry(session, 'e-1', 42)['reason'] == 'invalid_text'
    assert update_addendum_entry(session, 'e-1', 'x', source='tool')['reason'] == 'invalid_source'
    assert update_addendum_entry(None, 'e-1', 'x')['reason'] == 'no_session'

    assert session.addendum == before


# ---------------------------------------------------------------------------
# (c) remove_addendum_entry
# ---------------------------------------------------------------------------

def test_remove_deletes_exactly_one_entry_and_reports_the_totals():
    session = _two_entry_session()

    result = remove_addendum_entry(session, 'e-1')

    assert result['status'] == 'ok', result
    assert result['reason'] is None
    assert result['removed_id'] == 'e-1'
    assert result['entry']['id'] == 'e-1'
    assert [entry['id'] for entry in session.addendum['entries']] == ['e-2']
    assert session.addendum['entries'][0]['text'] == 'second text'
    assert result['attempted_entries'] == 1
    assert result['attempted_chars'] == len('second text')
    assert result['total_chars'] == len('second text')
    assert [entry['id'] for entry in result['entries']] == ['e-2']


def test_remove_of_unknown_id_is_entry_not_found_and_changes_nothing():
    session = _two_entry_session()
    before = copy.deepcopy(session.addendum)

    result = remove_addendum_entry(session, 'ghost')

    assert result['status'] == 'dropped'
    assert result['reason'] == 'entry_not_found'
    assert result['message'] == "no addendum entry with id 'ghost'"
    assert result['entry'] is None
    assert session.addendum == before
    assert remove_addendum_entry(None, 'ghost')['reason'] == 'no_session'


def test_removing_entries_is_how_an_over_budget_set_recovers():
    """The other half of the bug report: getting a stuck session writable again."""
    session = _make_session([_entry('e-1', 'x' * 150), _entry('e-2', 'y' * 150)],
                            budget_chars=250)
    assert sum(len(entry['text']) for entry in session.addendum['entries']) == 300

    refused = write_addendum_entry(session, 'new note')
    assert refused['status'] == 'dropped'
    assert refused['reason'] == 'budget_exceeded'

    removed = remove_addendum_entry(session, 'e-1')
    assert removed['status'] == 'ok'
    assert removed['attempted_chars'] == 150

    # 150 + 100 lands exactly on the cap, so the session is usable again.
    accepted = write_addendum_entry(session, 'z' * 100)
    assert accepted['status'] == 'ok', accepted
    assert accepted['attempted_chars'] == 250
    assert [entry['text'] for entry in session.addendum['entries']] == [
        'y' * 150, 'z' * 100,
    ]


# ---------------------------------------------------------------------------
# (d) every mutation survives a persistence round trip
# ---------------------------------------------------------------------------

def test_each_mutation_survives_a_persistence_round_trip():
    session = _make_session([_entry('e-1', 'one'), _entry('e-2', 'two')],
                            budget_chars=300, max_entries=7)

    written = write_addendum_entry(session, 'three')
    assert written['status'] == 'ok'

    reloaded = _reload(session)
    assert [entry['text'] for entry in reloaded.addendum['entries']] == ['one', 'two', 'three']
    assert reloaded.addendum['budget_chars'] == 300
    assert reloaded.addendum['max_entries'] == 7

    updated = update_addendum_entry(reloaded, 'e-2', 'two revised')
    assert updated['status'] == 'ok'

    reloaded = _reload(reloaded)
    assert [(entry['id'], entry['text']) for entry in reloaded.addendum['entries']] == [
        ('e-1', 'one'), ('e-2', 'two revised'), (written['entry']['id'], 'three'),
    ]
    assert reloaded.addendum['budget_chars'] == 300
    assert reloaded.addendum['max_entries'] == 7

    removed = remove_addendum_entry(reloaded, 'e-1')
    assert removed['status'] == 'ok'

    reloaded = _reload(reloaded)
    assert [(entry['id'], entry['text']) for entry in reloaded.addendum['entries']] == [
        ('e-2', 'two revised'), (written['entry']['id'], 'three'),
    ]
    assert reloaded.addendum['budget_chars'] == 300
    assert reloaded.addendum['max_entries'] == 7


# ---------------------------------------------------------------------------
# (e) guard: normalize_addendum stays fail-soft
# ---------------------------------------------------------------------------

def test_normalize_addendum_is_still_fail_soft_for_bad_shapes():
    default = {
        'entries': [],
        'budget_chars': DEFAULT_ADDENDUM_BUDGET_CHARS,
        'max_entries': DEFAULT_ADDENDUM_MAX_ENTRIES,
    }

    # Not a dict at all.
    assert normalize_addendum('nonsense') == default
    assert normalize_addendum([1, 2, 3]) == default
    assert normalize_addendum(None) == default

    # ``entries`` is not a list: ignored, the (legal) caps are kept.
    non_list = normalize_addendum({'entries': {'id': 'e-1'},
                                   'budget_chars': 1000, 'max_entries': 4})
    assert non_list['entries'] == []
    assert non_list['budget_chars'] == 1000
    assert non_list['max_entries'] == 4

    # Individual invalid entry objects are dropped, legal ones are kept.
    mixed = normalize_addendum({
        'entries': [
            _entry('e-1', 'kept'),
            {'id': 'e-2', 'source': 'tool', 'text': 'illegal source'},  # R4
            'not a dict',
            {'id': '', 'source': 'operator', 'text': 'empty id'},
            {'source': 'operator', 'text': 'no id'},
            {'id': 'e-3', 'source': 'operator'},
            _entry('e-4', 'kept too'),
        ],
        'budget_chars': 1000,
        'max_entries': 4,
    })
    assert [entry['id'] for entry in mixed['entries']] == ['e-1', 'e-4']

    # And none of it raises through the load path either.
    session = Session.from_persistable_dict(_persistable({'entries': 'nonsense',
                                                         'budget_chars': None,
                                                         'max_entries': []}))
    assert session.addendum == default

    # An invalid entry object is also a clean refusal, not an exception, at the
    # mutation boundary: an entry id that is not present is simply not found.
    mutations_session = _make_session([_entry('e-1', 'kept')])
    assert update_addendum_entry(mutations_session, {'not': 'an id'}, 'x')['reason'] == 'entry_not_found'
    assert remove_addendum_entry(mutations_session, ['not', 'an', 'id'])['reason'] == 'entry_not_found'
    assert mutations_session.addendum['entries'][0]['text'] == 'kept'
