"""
Tests for session size bounding (``session/size_bounding.py``).

Covers:
- Summary-anchored bounding: the history is cut at the second-last summary
  INCLUSIVE; everything from there on survives byte-identical, while the
  older region is filtered down to user queries, plain assistant messages
  and Respond-family tool results (non-Respond tool outputs are DROPPED,
  never truncated).
- Older-region filtering: Respond/Final/FinalReport/RequestUserInteraction
  results are resolved via ``tool_call_id`` -> assistant ``tool_calls``
  mapping; orphan tool results, assistant tool-call carriers and system
  notifications are dropped.
- Oldest-first dropping: the drop loop pops the oldest messages from the
  filtered older region, then from the kept region, re-measuring until the
  payload fits under the cap.
- Overrun flag: ``history_over_capacity`` is set only when a single message
  alone serializes over the cap and is never cleared.
- Main-agent gate: payloads without ``metadata['agent_type'] == 'main'`` or
  under the cap are never mutated; fewer than two summaries leaves the
  payload untouched (the store's re-measure guard enforces the cap).
- Store integration: ``session_size_bytes`` is written for main-agent
  sessions only, stored files never exceed the hard cap, and save/load
  round-trip stays lossless.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from agent.core.message import Message
from session.models import Session
from session.size_bounding import (
    SESSION_SIZE_CAP_BYTES,
    TRUNCATED_SUFFIX,
    _filter_older_region,
    _serialize,
    _strip_message,
    _tool_name_map,
    apply_session_size_bounding,
    payload_size_bytes,
)
from session.store import FileSystemSessionStore

# ── Helpers ──────────────────────────────────────────────────────────────────

_SEQ = iter(range(1, 10000))


def _reset_seq() -> None:
    global _SEQ
    _SEQ = iter(range(1, 10000))


def _msg(role: str, content: str, **extra) -> dict:
    seq = next(_SEQ)
    base = {
        'role': role,
        'content': content,
        'seq': seq,
        'created_at': '2026-01-01T00:00:00Z',
    }
    base.update(extra)
    return base


def _user(content: str, **kw) -> dict:
    return _msg('user', content, **kw)


def _asst(content: str, tool_calls=None, **kw) -> dict:
    m = _msg('assistant', content, **kw)
    if tool_calls is not None:
        m['tool_calls'] = tool_calls
    return m


def _tool(content: str, tool_call_id: str, **kw) -> dict:
    return _msg('tool', content, tool_call_id=tool_call_id, **kw)


def _tool_call(name: str, call_id: str, args: str = '{}') -> dict:
    return {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': args}}


def _summary(content: str, **kw) -> dict:
    return _msg('system', content, summary=True, **kw)


def _cycle(i: int, tool_size: int) -> list:
    """One user->tool cycle: query, tool-call carrier, big tool output,
    Respond terminal."""
    g = _tool_call('GlobTool', f'g{i}')
    r = _tool_call('Respond', f'r{i}')
    return [
        _user(f'Query {i}'),
        _asst('thinking', tool_calls=[g, r]),
        _tool('T' * tool_size, f'g{i}'),
        _tool(f'Answer {i}', f'r{i}', response_type='answer'),
    ]


def _payload(history, metadata=None) -> dict:
    return {
        'session_id': 'test-session',
        'metadata': metadata if metadata is not None else {'agent_type': 'main'},
        'user_history': list(history),
    }


def _make_store(tmp_path) -> FileSystemSessionStore:
    return FileSystemSessionStore(
        sessions_dir=str(tmp_path / 'sessions'),
        state_dir=str(tmp_path / 'state'),
        enable_session_history_pruning=False,
    )


# ── Cap enforcement order (summary-anchored) ────────────────────────────────

class TestCapEnforcementOrder:
    def test_second_last_summary_anchor_keeps_latest_byte_identical(self):
        """3 summaries + 3 cycles of 600k tool outputs: the history is cut at
        the second-last summary INCLUSIVE; the kept region (second-last
        summary and everything after) survives byte-identical, and the older
        region is filtered to [user query, Respond terminal] only."""
        _reset_seq()
        history = []
        for i in range(3):
            history.append(_summary(f'summary {i}'))
            history.extend(_cycle(i, 700_000))

        data = _payload(history)
        assert payload_size_bytes(data) > SESSION_SIZE_CAP_BYTES
        assert apply_session_size_bounding(data) is True

        out = data['user_history']
        # older region filtered: s0 (system) dropped, asst carrier + big
        # GlobTool output dropped, only user0 and the Respond terminal kept
        assert out[0] == history[1]
        assert out[1] == history[4]
        # kept region starts AT the second-last summary (inclusive) and is
        # byte-identical to the original
        assert out[2] == history[5]
        assert out[2:] == history[5:]
        assert _serialize(out[2:]) == _serialize(history[5:])
        assert payload_size_bytes(data) <= SESSION_SIZE_CAP_BYTES
        assert 'history_over_capacity' not in data['metadata']


# ── Older-region filtering (Respond-family resolution) ──────────────────────

class TestTerminalAnswerIdentification:
    def test_filter_keeps_respond_results_drops_intermediate_outputs(self):
        """Respond-family tool results survive the older-region filter;
        non-Respond tool outputs and assistant tool-call carriers are
        dropped."""
        _reset_seq()
        older = [
            _user('Query 0'),
            _asst('thinking', tool_calls=[_tool_call('GlobTool', 'g0'),
                                          _tool_call('Respond', 'r0')]),
            _tool('T' * 950_000, 'g0'),
            _tool('final answer', 'r0', response_type='answer'),
        ]
        name_map = _tool_name_map(older)
        assert name_map == {'g0': 'GlobTool', 'r0': 'Respond'}
        filtered = _filter_older_region(older, name_map)
        assert [m['role'] for m in filtered] == ['user', 'tool']
        assert filtered[0] == older[0]
        assert filtered[1] == older[3]
        assert 'T' * 950_000 not in json.dumps(filtered)

    def test_filter_keeps_legacy_respond_family_via_call_id_map(self):
        """Legacy Final/FinalReport/RequestUserInteraction results resolve
        through the call-id map and are kept by the older-region filter."""
        _reset_seq()
        older = [
            _user('Query 0'),
            _asst('thinking', tool_calls=[
                _tool_call('Final', 'f0'),
                _tool_call('FinalReport', 'fr0'),
                _tool_call('RequestUserInteraction', 'ru0'),
            ]),
            _tool('Final result', 'f0'),
            _tool('Report result', 'fr0'),
            _tool('Clarify?', 'ru0'),
        ]
        name_map = _tool_name_map(older)
        assert name_map == {
            'f0': 'Final',
            'fr0': 'FinalReport',
            'ru0': 'RequestUserInteraction',
        }
        filtered = _filter_older_region(older, name_map)
        assert [m['role'] for m in filtered] == ['user', 'tool', 'tool', 'tool']
        assert [m['tool_call_id'] for m in filtered if m['role'] == 'tool'] == \
            ['f0', 'fr0', 'ru0']

    def test_filter_drops_orphan_tool_results_and_system_notifications(self):
        """Orphan tool results (unmapped call id), system notifications and
        assistant tool-call carriers are dropped; plain assistant messages
        survive."""
        _reset_seq()
        older = [
            _user('Query 0'),
            _msg('system', 'context refresh', is_system_notification=True),
            _asst('thinking', tool_calls=[_tool_call('Respond', 'r0'),
                                          _tool_call('GlobTool', 'g0')]),
            _tool('orphan result', 'unknown-call-id'),
            _tool('T' * 500, 'g0'),
            _tool('kept answer', 'r0', response_type='answer'),
            _asst('plain assistant message'),
        ]
        name_map = _tool_name_map(older)
        filtered = _filter_older_region(older, name_map)
        assert [m['role'] for m in filtered] == ['user', 'tool', 'assistant']
        assert filtered[1]['tool_call_id'] == 'r0'
        assert filtered[1]['content'] == 'kept answer'
        assert filtered[2]['content'] == 'plain assistant message'

    def test_strip_message_removes_reasoning_and_tool_calls(self):
        """Unit check: _strip_message drops only the two private keys."""
        _reset_seq()
        m = _asst('hello', tool_calls=[_tool_call('GlobTool', 'x')],
                  reasoning_content='secret', extra_key='keep')
        stripped = _strip_message(m)
        assert 'reasoning_content' not in stripped
        assert 'tool_calls' not in stripped
        assert stripped['extra_key'] == 'keep'
        assert stripped['content'] == 'hello'
        assert stripped['role'] == 'assistant'


# ── Oldest-first dropping ───────────────────────────────────────────────────

class TestOldestDroppedFirst:
    def test_drops_oldest_first_from_filtered_older_then_kept(self):
        """3 summaries + 3 cycles of 1.05m tool outputs: the kept region
        alone is over the cap, so the drop loop pops the filtered older
        region first, then walks the kept region oldest-first, re-measuring
        until the payload fits; the newest cycle survives byte-identical."""
        _reset_seq()
        history = []
        for i in range(3):
            history.append(_summary(f'summary {i}'))
            history.extend(_cycle(i, 1_050_000))

        data = _payload(history)
        assert payload_size_bytes(data) > SESSION_SIZE_CAP_BYTES
        assert apply_session_size_bounding(data) is True

        kept = history[5:]
        out = data['user_history']
        assert payload_size_bytes(data) <= SESSION_SIZE_CAP_BYTES
        # every surviving message comes from the kept region, oldest-first:
        # out is a trailing slice of the kept region (filtered older fully
        # drained first)
        assert out == kept[len(kept) - len(out):]
        # newest cycle byte-identical
        assert out[-4:] == kept[-4:]
        all_content = json.dumps(out)
        assert 'Query 0' not in all_content
        assert 'Query 1' not in all_content
        assert 'history_over_capacity' not in data['metadata']


# ── No truncation: dropping is the only mechanism ───────────────────────────

class TestTruncation:
    def test_no_truncation_tool_outputs_kept_byte_for_byte(self):
        """Tool outputs are NEVER truncated: kept messages stay byte-for-byte
        and dropping is the only size-reduction mechanism. The 1.5m tool
        output of the latest cycle survives intact; the older one is dropped
        by filtering."""
        _reset_seq()
        history = [_summary('summary 0')]
        history.extend(_cycle(0, 1_500_000))
        history.append(_summary('summary 1'))
        history.extend(_cycle(1, 1_500_000))
        history.append(_summary('summary 2'))

        data = _payload(history)
        assert payload_size_bytes(data) > SESSION_SIZE_CAP_BYTES
        assert apply_session_size_bounding(data) is True

        out = data['user_history']
        # [user0, Respond terminal0] + kept region from second-last summary
        assert out == [history[1], history[4]] + history[5:]
        # the big tool output of the latest cycle is byte-for-byte identical
        assert out[5]['content'] == 'T' * 1_500_000
        assert out[5]['tool_call_id'] == 'g1'
        assert out[5] == history[8]
        # the older big output was dropped, never truncated
        assert TRUNCATED_SUFFIX not in json.dumps(out)
        assert out.count(history[8]) == 1
        assert payload_size_bytes(data) <= SESSION_SIZE_CAP_BYTES
        assert 'history_over_capacity' not in data['metadata']


# ── Overrun: single message over the cap ────────────────────────────────────

class TestOverrun:
    def test_overrun_marks_history_over_capacity_and_stays_under_cap(self):
        """A single message that alone serializes over the cap sets
        history_over_capacity (never cleared); the drop loop still brings
        the final payload under the cap, even if that empties the history."""
        _reset_seq()
        history = [
            _summary('summary 0'),
            _summary('summary 1'),
            _user('u' * 2_100_000),
            _asst('small'),
        ]
        data = _payload(history)
        assert apply_session_size_bounding(data) is True
        assert data['metadata']['history_over_capacity'] is True
        assert payload_size_bytes(data) <= SESSION_SIZE_CAP_BYTES
        # the over-cap message itself is dropped
        assert 'u' * 2_100_000 not in json.dumps(data['user_history'])

        # flag is never cleared on a subsequent (no-op) call
        before = copy.deepcopy(data)
        assert apply_session_size_bounding(data) is False
        assert data == before
        assert data['metadata']['history_over_capacity'] is True


# ── Main-agent gate ─────────────────────────────────────────────────────────

class TestMainAgentGate:
    def test_non_main_agent_payload_untouched(self):
        _reset_seq()
        history = [_user('u' * 1_100_000), _asst('a' * 1_100_000)]
        data = _payload(history, metadata={'agent_type': 'worker'})
        before = copy.deepcopy(data)
        assert payload_size_bytes(data) > SESSION_SIZE_CAP_BYTES
        assert apply_session_size_bounding(data) is False
        assert data == before

    def test_absent_metadata_untouched(self):
        _reset_seq()
        history = [_user('u' * 1_100_000), _asst('a' * 1_100_000)]
        data = _payload(history, metadata={})
        before = copy.deepcopy(data)
        assert payload_size_bytes(data) > SESSION_SIZE_CAP_BYTES
        assert apply_session_size_bounding(data) is False
        assert data == before

    def test_under_cap_main_untouched(self):
        _reset_seq()
        history = [_user('small'), _asst('small answer')]
        data = _payload(history)
        before = copy.deepcopy(data)
        assert payload_size_bytes(data) <= SESSION_SIZE_CAP_BYTES
        assert apply_session_size_bounding(data) is False
        assert data == before

    def test_fewer_than_two_summaries_untouched(self):
        """With fewer than two summaries the bounder leaves the payload
        unchanged (the store's re-measure guard enforces the cap)."""
        _reset_seq()
        history = [_summary('summary 0'), _user('u' * 2_100_000)]
        data = _payload(history)
        before = copy.deepcopy(data)
        assert payload_size_bytes(data) > SESSION_SIZE_CAP_BYTES
        assert apply_session_size_bounding(data) is False
        assert data == before


# ── Hard-cap guarantee (bounder + store re-measure guard) ───────────────────

class TestHardCapGuarantees:
    def test_hard_cap_keeps_serialized_session_under_2mb_large_payload(self, tmp_path):
        """3 summaries + 3 cycles of 900k tool outputs: after bounding the
        payload is under 2 MB, the latest two-cycle region is intact, and
        the stored file re-serializes under 2,000,000 bytes."""
        _reset_seq()
        history = []
        for i in range(3):
            history.append(_summary(f'summary {i}'))
            history.extend(_cycle(i, 900_000))

        data = _payload(history)
        assert payload_size_bytes(data) > SESSION_SIZE_CAP_BYTES
        assert apply_session_size_bounding(data) is True
        assert payload_size_bytes(data) <= SESSION_SIZE_CAP_BYTES

        out = data['user_history']
        # filtered older = [user0, Respond terminal0]; kept region from the
        # second-last summary inclusive — latest two cycles intact
        assert out == [history[1], history[4]] + history[5:]
        assert out[-8:] == history[7:15]

        # exercise the full save path and read the file back
        store = _make_store(tmp_path)
        session = Session.from_persistable_dict({
            'session_id': 'hard-cap-session',
            'metadata': {'agent_type': 'main', 'name': 'Hard Cap'},
            'user_history': out,
        })
        sid = session.session_id
        store.save_session(session)

        path = store._find_session_path(sid)
        assert path is not None and path.exists()
        assert os.path.getsize(path) <= 2_000_000
        with open(path, 'r', encoding='utf-8') as f:
            stored = json.load(f)
        assert len(json.dumps(stored, indent=2, default=str).encode('utf-8')) <= 2_000_000

    def test_remeasure_guard_triggers_when_still_over_cap(self, tmp_path):
        """The store's re-measure guard is the last line of defense: even
        when the bounder leaves the payload over the cap (fewer than two
        summaries), save_session still guarantees a stored file <= 2 MB."""
        store = _make_store(tmp_path)

        # Scenario A: <2 summaries + a single >2MB message — bounder no-ops
        # (unchanged), the store guard drops messages until the file fits.
        _reset_seq()
        history_a = [_summary('summary 0'), _user('u' * 2_100_000)]
        data_a = _payload(history_a)
        before = copy.deepcopy(data_a)
        assert apply_session_size_bounding(data_a) is False
        assert data_a == before

        session_a = Session.from_persistable_dict({
            'session_id': 'guard-a',
            'metadata': {'agent_type': 'main', 'name': 'Guard A'},
            'user_history': data_a['user_history'],
        })
        store.save_session(session_a)
        path_a = store._find_session_path(session_a.session_id)
        assert path_a is not None and path_a.exists()
        assert os.path.getsize(path_a) <= 2_000_000

        # Scenario B: >=2 summaries + a single >2MB message — the bounder
        # sets the flag and drops the over-cap message; the flag survives a
        # later call and the stored file is still <= 2 MB.
        _reset_seq()
        history_b = [
            _summary('summary 0'),
            _summary('summary 1'),
            _user('u' * 2_100_000),
        ]
        data_b = _payload(history_b)
        assert apply_session_size_bounding(data_b) is True
        assert data_b['metadata']['history_over_capacity'] is True
        assert payload_size_bytes(data_b) <= SESSION_SIZE_CAP_BYTES
        assert apply_session_size_bounding(data_b) is False
        assert data_b['metadata']['history_over_capacity'] is True

        session_b = Session.from_persistable_dict({
            'session_id': 'guard-b',
            'metadata': dict(data_b['metadata'], name='Guard B'),
            'user_history': data_b['user_history'],
        })
        store.save_session(session_b)
        path_b = store._find_session_path(session_b.session_id)
        assert path_b is not None and path_b.exists()
        assert os.path.getsize(path_b) <= 2_000_000


# ── Store integration: session_size_bytes ───────────────────────────────────

class TestStoreIntegration:
    def test_session_size_bytes_written_for_main_session(self, tmp_path):
        store = _make_store(tmp_path)
        session = Session()
        session.metadata['agent_type'] = 'main'
        session.metadata['name'] = 'Big Session'
        session.add_message('user', 'u' * 1_100_000)
        session.add_message('assistant', 'a' * 1_100_000)
        sid = session.session_id

        store.save_session(session)

        path = store._find_session_path(sid)
        assert path is not None and path.exists()
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        size = data.get('session_size_bytes')
        assert isinstance(size, int) and size > 0
        # stored size equals the serialized byte length and the real file size
        assert len(json.dumps(data, indent=2, default=str).encode('utf-8')) == size
        assert os.path.getsize(path) == size
        # metadata loaders expose the same value
        meta = store.load_session_metadata(sid)
        assert meta is not None
        assert meta['session_size_bytes'] == size
        batch = store.load_sessions_metadata_batch([sid])
        assert batch[sid]['session_size_bytes'] == size
        assert store.get_session_size_bytes(sid) == size

    def test_no_size_field_for_non_main_session(self, tmp_path):
        store = _make_store(tmp_path)
        session = Session()
        session.metadata['name'] = 'Worker Session'
        session.add_message('user', 'u' * 1_100_000)
        session.add_message('assistant', 'a' * 1_100_000)
        sid = session.session_id

        store.save_session(session)

        path = store._find_session_path(sid)
        assert path is not None and path.exists()
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        assert 'session_size_bytes' not in data
        assert store.get_session_size_bytes(sid) is None
        meta = store.load_session_metadata(sid)
        assert meta is not None
        assert meta.get('session_size_bytes') is None


# ── Round-trip safety ───────────────────────────────────────────────────────

class TestRoundTrip:
    def test_save_load_round_trip_preserves_messages(self, tmp_path):
        store = _make_store(tmp_path)
        session = Session()
        session.metadata['agent_type'] = 'main'
        session.add_message('user', 'Hello')
        session.add_message('assistant', 'Hi there')
        sid = session.session_id

        store.save_session(session)
        loaded = store.load_session(sid)

        assert loaded is not None
        assert len(loaded.user_history) == 2
        for msg in loaded.user_history:
            assert isinstance(msg, Message)
            assert isinstance(msg, dict)
        assert loaded.user_history[0]['content'] == 'Hello'
        assert loaded.user_history[1]['content'] == 'Hi there'

    def test_from_persistable_dict_ignores_session_size_bytes(self):
        s = Session.from_persistable_dict({
            'session_id': 'x',
            'user_history': [],
            'metadata': {},
            'session_size_bytes': 42,
        })
        assert s.session_id == 'x'
        assert len(s.user_history) == 0
