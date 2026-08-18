"""
Tests for session size bounding (``session/size_bounding.py``).

Covers:
- Cap enforcement order: 2 newest cycles kept full, older cycles compacted to
  ``[query, terminal]``, oldest compacted cycles dropped first, non-terminal
  tool outputs truncated to a byte budget, and the ``history_over_capacity``
  overrun flag.
- Terminal-answer identification: Respond tool results with ``response_type``,
  legacy Final/FinalReport/RequestUserInteraction results mapped via
  ``tool_call_id``, standalone assistant messages (reasoning stripped), and
  the query-only fallback.
- Main-agent gate: payloads without ``metadata['agent_type'] == 'main'`` or
  under the cap are never mutated.
- Store integration: ``session_size_bytes`` is written for main-agent sessions
  only and exposed through ``load_session_metadata``,
  ``load_sessions_metadata_batch`` and ``get_session_size_bytes``; save/load
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
    TOOL_CONTENT_BUDGET_BYTES,
    TRUNCATED_SUFFIX,
    _serialize,
    _strip_message,
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


# ── Cap enforcement order (newest 2 full, older compacted) ──────────────────

class TestCapEnforcementOrder:
    def test_two_newest_cycles_full_older_compacted(self):
        """4 over-cap cycles: oldest 2 compact to [query, terminal],
        newest 2 stay byte-identical."""
        _reset_seq()
        history = []
        for i in range(4):
            g = _tool_call('GlobTool', f'g{i}')
            r = _tool_call('Respond', f'r{i}')
            history.append(_user(f'Query {i}'))
            history.append(_asst('thinking', tool_calls=[g, r], reasoning_content=f'private {i}'))
            history.append(_tool('T' * 600_000, f'g{i}'))
            history.append(_tool(f'Answer text {i}', f'r{i}', response_type='answer'))

        data = _payload(history)
        assert payload_size_bytes(data) > SESSION_SIZE_CAP_BYTES
        assert apply_session_size_bounding(data) is True

        out = data['user_history']
        assert len(out) == 12
        # oldest 2 cycles compacted to exactly [query, terminal]
        assert out[0] == history[0]
        assert out[1] == history[3]
        assert out[2] == history[4]
        assert out[3] == history[7]
        # queries and terminals are byte-identical to the originals
        assert _serialize(out[0]) == _serialize(history[0])
        assert _serialize(out[1]) == _serialize(history[3])
        assert _serialize(out[2]) == _serialize(history[4])
        assert _serialize(out[3]) == _serialize(history[7])
        # 2 newest cycles kept full and byte-identical
        assert out[4:8] == history[8:12]
        assert out[8:12] == history[12:16]
        assert payload_size_bytes(data) <= SESSION_SIZE_CAP_BYTES
        assert 'history_over_capacity' not in data['metadata']


# ── Terminal-answer identification + compaction ─────────────────────────────

class TestTerminalAnswerIdentification:
    def test_standalone_assistant_terminal_with_reasoning_stripped(self):
        """Rule (c): standalone assistant message is the terminal; its
        reasoning_content is stripped."""
        _reset_seq()
        history = []
        c1 = _tool_call('GlobTool', 'c1')
        history.append(_user('Q'))
        history.append(_asst('thinking', tool_calls=[c1], reasoning_content='private'))
        history.append(_tool('T' * 950_000, 'c1'))
        history.append(_asst('Final answer', reasoning_content='hidden'))
        for i in range(2):
            history.append(_user(f'fill user {i}'))
            history.append(_asst('F' * 600_000))

        data = _payload(history)
        assert apply_session_size_bounding(data) is True

        out = data['user_history']
        assert len(out) == 6
        assert out[0] == history[0]
        assert out[1]['content'] == 'Final answer'
        assert 'reasoning_content' not in out[1]
        # no reasoning_content/tool_calls anywhere in the output
        for m in out:
            assert 'reasoning_content' not in m
            assert 'tool_calls' not in m
        assert payload_size_bytes(data) <= SESSION_SIZE_CAP_BYTES

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

    def test_respond_terminal_kept_intermediate_tool_output_dropped(self):
        """Rule (a): the Respond result (response_type) is the terminal;
        the big intermediate GlobTool output is dropped."""
        _reset_seq()
        history = []
        g0 = _tool_call('GlobTool', 'g0')
        r0 = _tool_call('Respond', 'r0')
        history.append(_user('Query 0'))
        history.append(_asst('thinking', tool_calls=[g0, r0]))
        history.append(_tool('T' * 950_000, 'g0'))
        history.append(_tool('final answer', 'r0', response_type='answer'))
        for i in range(2):
            history.append(_user(f'fill user {i}'))
            history.append(_asst('F' * 600_000))

        data = _payload(history)
        assert apply_session_size_bounding(data) is True

        out = data['user_history']
        assert len(out) == 6
        assert out[0] == history[0]
        assert out[1]['content'] == 'final answer'
        assert out[1]['tool_call_id'] == 'r0'
        assert 'T' * 950_000 not in json.dumps(out)
        assert payload_size_bytes(data) <= SESSION_SIZE_CAP_BYTES

    def test_legacy_final_result_dropped_respond_terminal_kept(self):
        """A non-terminal legacy Final result is dropped when a real Respond
        terminal exists later in the cycle."""
        _reset_seq()
        history = []
        f0 = _tool_call('Final', 'f0')
        rs0 = _tool_call('Respond', 'rs0')
        history.append(_user('Query 0'))
        history.append(_asst('thinking', tool_calls=[f0, rs0]))
        history.append(_tool('Final result content', 'f0'))
        history.append(_tool('X' * 950_000, 'gx'))
        history.append(_tool('Respond answer', 'rs0', response_type='answer'))
        for i in range(2):
            history.append(_user(f'fill user {i}'))
            history.append(_asst('F' * 600_000))

        data = _payload(history)
        assert apply_session_size_bounding(data) is True

        out = data['user_history']
        assert len(out) == 6
        assert out[1]['content'] == 'Respond answer'
        assert 'Final result content' not in json.dumps(out)
        assert 'X' * 950_000 not in json.dumps(out)

    def test_legacy_final_result_is_terminal_via_call_id_mapping(self):
        """Rule (b): a Final tool result without response_type is still the
        terminal because its tool_call_id maps to a Final call."""
        _reset_seq()
        history = []
        f0 = _tool_call('Final', 'f0')
        history.append(_user('Query 0'))
        history.append(_asst('thinking', tool_calls=[f0]))
        history.append(_tool('Final result content', 'f0'))
        history.append(_tool('X' * 950_000, 'gx'))
        for i in range(2):
            history.append(_user(f'fill user {i}'))
            history.append(_asst('F' * 600_000))

        data = _payload(history)
        assert apply_session_size_bounding(data) is True

        out = data['user_history']
        assert out[1]['content'] == 'Final result content'
        assert out[1]['tool_call_id'] == 'f0'
        assert 'X' * 950_000 not in json.dumps(out)

    def test_legacy_request_user_interaction_result_is_terminal(self):
        """Rule (b) also covers RequestUserInteraction results."""
        _reset_seq()
        history = []
        ru0 = _tool_call('RequestUserInteraction', 'ru0')
        history.append(_user('Query 0'))
        history.append(_asst('thinking', tool_calls=[ru0]))
        history.append(_tool('Please clarify: which file?', 'ru0'))
        history.append(_tool('X' * 950_000, 'gx'))
        for i in range(2):
            history.append(_user(f'fill user {i}'))
            history.append(_asst('F' * 600_000))

        data = _payload(history)
        assert apply_session_size_bounding(data) is True

        out = data['user_history']
        assert out[1]['content'] == 'Please clarify: which file?'
        assert out[1]['tool_call_id'] == 'ru0'


# ── Oldest-first dropping ────────────────────────────────────────────────────

class TestOldestDroppedFirst:
    def test_drops_oldest_compacted_cycles_first(self):
        """6 cycles, middle terminals 550k each: exactly the 2 oldest are
        dropped (2.76M -> drop -> 2.21M -> drop -> 1.66M under cap)."""
        _reset_seq()
        history = []
        for i in range(6):
            g = _tool_call('GlobTool', f'g{i}')
            r = _tool_call('Respond', f'r{i}')
            history.append(_user(f'Query {i}'))
            history.append(_asst('thinking', tool_calls=[g, r]))
            history.append(_tool('mid', f'g{i}'))
            terminal = 'A' * 550_000 if i < 4 else 'F' * 280_000
            history.append(_tool(terminal, f'r{i}', response_type='answer'))

        data = _payload(history)
        assert payload_size_bytes(data) > SESSION_SIZE_CAP_BYTES
        assert apply_session_size_bounding(data) is True

        out = data['user_history']
        # exactly 2 oldest cycles dropped; remaining = c2,c3 compacted + c4,c5 full
        assert len(out) == 12
        assert out[0]['content'] == 'Query 2'
        assert out[1]['content'] == 'A' * 550_000
        assert out[2]['content'] == 'Query 3'
        assert out[3]['content'] == 'A' * 550_000
        assert out[4:8] == history[16:20]
        assert out[8:12] == history[20:24]
        all_content = json.dumps(out)
        assert 'Query 0' not in all_content
        assert 'Query 1' not in all_content
        assert payload_size_bytes(data) <= SESSION_SIZE_CAP_BYTES


# ── Step-4 truncation of non-terminal tool outputs ──────────────────────────

class TestTruncation:
    def test_tool_outputs_truncated_to_budget_queries_terminals_kept(self):
        """2 cycles with 1.5MB tool outputs: no compaction possible, the big
        non-terminal tool outputs are truncated; queries/terminals untouched."""
        _reset_seq()
        history = []
        for i in range(2):
            d = _tool_call('DockerCodeRunner', f'd{i}')
            rd = _tool_call('Respond', f'rd{i}')
            history.append(_user(f'Query {i}'))
            history.append(_asst('thinking', tool_calls=[d, rd]))
            history.append(_tool('T' * 1_500_000, f'd{i}'))
            history.append(_tool(f'answer{i}', f'rd{i}', response_type='answer'))

        data = _payload(history)
        assert payload_size_bytes(data) > SESSION_SIZE_CAP_BYTES
        assert apply_session_size_bounding(data) is True

        out = data['user_history']
        assert len(out) == 8
        # queries byte-identical
        assert out[0] == history[0]
        assert out[4] == history[4]
        # terminals byte-identical
        assert out[3] == history[3]
        assert out[7] == history[7]
        # big tool outputs truncated to budget + suffix
        prefix_budget = TOOL_CONTENT_BUDGET_BYTES - len(TRUNCATED_SUFFIX)
        expected = ('T' * 1_500_000)[:prefix_budget] + TRUNCATED_SUFFIX
        assert out[2]['content'] == expected
        assert out[6]['content'] == expected
        assert out[2]['tool_call_id'] == 'd0'
        assert out[2]['seq'] == history[2]['seq']
        assert 'history_over_capacity' not in data['metadata']
        assert payload_size_bytes(data) <= SESSION_SIZE_CAP_BYTES


# ── Overrun: still over the cap after all steps ─────────────────────────────

class TestOverrun:
    def test_overrun_marks_history_over_capacity_and_keeps_messages(self):
        """Terminal-only big content cannot be compacted or truncated: the
        payload stays over the cap and the flag is set (never cleared)."""
        _reset_seq()
        history = []
        for i in range(2):
            rd = _tool_call('Respond', f'rd{i}')
            history.append(_user(f'Query {i}'))
            history.append(_asst('thinking', tool_calls=[rd]))
            history.append(_tool('A' * 1_500_000, f'rd{i}', response_type='answer'))

        data = _payload(history)
        assert apply_session_size_bounding(data) is True

        out = data['user_history']
        assert out == history
        assert data['metadata']['history_over_capacity'] is True
        assert payload_size_bytes(data) > SESSION_SIZE_CAP_BYTES


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
