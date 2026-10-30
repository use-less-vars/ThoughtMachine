"""
Slice 1 of the sysprompt addendum wave: addendum schema and persistence.

Obligations (brief §4.1 Path A, §13 slice 1):
1. round-trip through the real store (save_session / load_session),
2. a session file that predates the field loads as an empty default addendum,
3. forward tolerance -- unknown keys do not raise,
4. an entry with an illegal `source` is dropped on read rather than failing the
   whole load (R4: `tool` is not a legal source).
"""
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from session.models import (
    ADDENDUM_SOURCES,
    DEFAULT_ADDENDUM_BUDGET_CHARS,
    DEFAULT_ADDENDUM_MAX_ENTRIES,
    Session,
)
from session.store import FileSystemSessionStore


def _entry(entry_id, text, source='operator', pinned=False, ts='2024-01-01T00:00:00'):
    return {'id': entry_id, 'ts': ts, 'source': source, 'text': text, 'pinned': pinned}


def _normalised(entries):
    """JSON-normalise an entry list so round-trip equality is byte-level."""
    return json.loads(json.dumps(entries, sort_keys=True))


def _with_addendum(addendum):
    """A persistable dict for a fresh session, with the addendum replaced."""
    data = Session().to_persistable_dict()
    data['addendum'] = addendum
    return data


@pytest.fixture
def store(tmp_path):
    """A FileSystemSessionStore rooted in this test's tmp dir."""
    return FileSystemSessionStore(
        sessions_dir=str(tmp_path / 'sessions'),
        state_dir=str(tmp_path / 'state'),
        enable_session_history_pruning=False,
    )


# ── Defaults ────────────────────────────────────────────────────────────────

class TestDefaults:
    def test_fresh_session_has_empty_default_addendum(self):
        assert Session().addendum == {
            'entries': [],
            'budget_chars': DEFAULT_ADDENDUM_BUDGET_CHARS,
            'max_entries': DEFAULT_ADDENDUM_MAX_ENTRIES,
        }

    def test_constants_are_the_ruled_values(self):
        assert DEFAULT_ADDENDUM_BUDGET_CHARS == 8000        # R2: exact char count
        assert DEFAULT_ADDENDUM_MAX_ENTRIES == 64           # R3
        assert ADDENDUM_SOURCES == ('operator', 'mailbox')  # R4: no `tool`

    def test_two_sessions_do_not_share_the_default(self):
        first, second = Session(), Session()
        first.addendum['entries'].append(_entry('e1', 'hello'))
        assert second.addendum['entries'] == []

    def test_persistable_dict_carries_the_addendum(self):
        session = Session()
        assert session.to_persistable_dict()['addendum'] == session.addendum


# ── Persistence through the store ───────────────────────────────────────────

class TestPersistence:
    def test_round_trip_through_store_preserves_entries(self, store):
        session = Session()
        session.metadata['name'] = 'Addendum Round Trip'
        session.addendum = {
            'entries': [
                _entry('e1', 'plain entry'),
                _entry('e2', 'pinned entry', pinned=True),
                _entry('e3', 'multi-line\nwith Ünïcode — 日本語 and a\ttab'),
            ],
            'budget_chars': 250,
            'max_entries': 3,
        }

        store.save_session(session)
        loaded = store.load_session(session.session_id)

        assert loaded is not None
        assert _normalised(loaded.addendum['entries']) == _normalised(session.addendum['entries'])
        assert [e['id'] for e in loaded.addendum['entries']] == ['e1', 'e2', 'e3']
        assert [e['pinned'] for e in loaded.addendum['entries']] == [False, True, False]
        assert loaded.addendum['budget_chars'] == 250
        assert loaded.addendum['max_entries'] == 3

    def test_saved_file_contains_the_addendum_key(self, store):
        session = Session()
        session.metadata['name'] = 'On Disk'
        session.addendum['entries'].append(_entry('e1', 'on disk'))

        store.save_session(session)
        path = store._find_session_path(session.session_id)

        assert path is not None
        on_disk = json.loads(path.read_text())
        assert on_disk['addendum']['entries'][0]['text'] == 'on disk'
        assert on_disk['addendum']['budget_chars'] == DEFAULT_ADDENDUM_BUDGET_CHARS

    def test_session_file_predating_the_field_loads_as_empty_addendum(self, store):
        """Backward compatibility: no `addendum` key at all must not break load."""
        session = Session()
        session.metadata['name'] = 'Pre Field'
        session.addendum['entries'].append(_entry('e1', 'written by an older version'))

        store.save_session(session)
        path = store._find_session_path(session.session_id)
        data = json.loads(path.read_text())
        del data['addendum']
        path.write_text(json.dumps(data, indent=2, default=str))

        loaded = store.load_session(session.session_id)

        assert loaded is not None
        assert loaded.addendum['entries'] == []
        assert loaded.addendum['budget_chars'] == DEFAULT_ADDENDUM_BUDGET_CHARS
        assert loaded.addendum['max_entries'] == DEFAULT_ADDENDUM_MAX_ENTRIES

    def test_dict_predating_the_field_loads_as_empty_addendum(self):
        data = Session().to_persistable_dict()
        del data['addendum']

        session = Session.from_persistable_dict(data)

        assert session.addendum == {
            'entries': [],
            'budget_chars': DEFAULT_ADDENDUM_BUDGET_CHARS,
            'max_entries': DEFAULT_ADDENDUM_MAX_ENTRIES,
        }

    def test_update_from_persistable_dict_carries_the_addendum(self):
        session = Session()
        data = _with_addendum({
            'entries': [_entry('e9', 'in place')],
            'budget_chars': 42,
            'max_entries': 5,
        })

        session.update_from_persistable_dict(data)

        assert [e['id'] for e in session.addendum['entries']] == ['e9']
        assert session.addendum['budget_chars'] == 42
        assert session.addendum['max_entries'] == 5


# ── Forward / partial tolerance ─────────────────────────────────────────────

class TestTolerance:
    def test_unknown_addendum_keys_do_not_raise(self):
        data = _with_addendum({
            'entries': [dict(_entry('e1', 'kept'), future_entry_field='x')],
            'budget_chars': 1000,
            'max_entries': 4,
            'future_top_level_field': 'y',
        })

        session = Session.from_persistable_dict(data)

        assert [e['id'] for e in session.addendum['entries']] == ['e1']
        assert session.addendum['budget_chars'] == 1000
        assert session.addendum['max_entries'] == 4

    def test_partial_addendum_dict_is_defaulted_not_rejected(self):
        data = _with_addendum({'entries': [_entry('e1', 'only entries')]})

        session = Session.from_persistable_dict(data)

        assert len(session.addendum['entries']) == 1
        assert session.addendum['budget_chars'] == DEFAULT_ADDENDUM_BUDGET_CHARS
        assert session.addendum['max_entries'] == DEFAULT_ADDENDUM_MAX_ENTRIES

    def test_non_dict_addendum_is_ignored(self):
        session = Session.from_persistable_dict(_with_addendum('nonsense'))
        assert session.addendum['entries'] == []

    def test_non_int_caps_fall_back_to_defaults(self):
        session = Session.from_persistable_dict(_with_addendum({
            'entries': [],
            'budget_chars': '1000',
            'max_entries': None,
        }))

        assert session.addendum['budget_chars'] == DEFAULT_ADDENDUM_BUDGET_CHARS
        assert session.addendum['max_entries'] == DEFAULT_ADDENDUM_MAX_ENTRIES


# ── Invalid entries are dropped, never raised ──────────────────────────────

class TestInvalidEntriesAreDropped:
    def test_illegal_source_entry_is_dropped_and_load_still_succeeds(self, store, monkeypatch):
        warnings = []

        def fake_log(level, channel, message, **kwargs):
            warnings.append((level, channel, message))

        monkeypatch.setattr('session.models.log', fake_log)

        session = Session()
        session.metadata['name'] = 'Bad Source'
        session.addendum['entries'] = [
            _entry('e1', 'legal operator entry'),
            _entry('e2', 'illegal source', source='tool'),  # R4: `tool` is not legal
            _entry('e3', 'legal mailbox entry', source='mailbox'),
        ]

        store.save_session(session)
        loaded = store.load_session(session.session_id)

        assert loaded is not None, "one malformed entry must not cost the whole session"
        assert [e['id'] for e in loaded.addendum['entries']] == ['e1', 'e3']
        assert any(
            level == 'WARNING' and 'dropping invalid entry' in message
            for level, _channel, message in warnings
        ), f"expected a WARNING for the dropped entry, got {warnings}"

    def test_malformed_entries_are_dropped(self):
        session = Session.from_persistable_dict(_with_addendum({
            'entries': [
                _entry('e1', 'kept'),
                {'id': 'e2', 'source': 'operator'},            # no text
                {'source': 'operator', 'text': 'no id'},       # no id
                {'id': '', 'source': 'operator', 'text': 'x'},  # empty id
                'not a dict',
                {'id': 'e4', 'source': 'operator', 'text': 'kept too'},
            ],
        }))

        assert [e['id'] for e in session.addendum['entries']] == ['e1', 'e4']
