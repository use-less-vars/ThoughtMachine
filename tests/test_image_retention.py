"""Tests for the tombstone retention pass (``session/image_retention.py``).

The pass releases an image by emptying ``message['images']``.  What these tests
pin, in order of importance:

* the message itself is untouched -- ``content`` round-trips byte-identically
  through a real ``save_session`` / ``load_session``;
* no byte is ever deleted here (reclaiming the file stays with
  ``agent.core.image_files``);
* the input history is never mutated;
* the count policy keeps the newest N image-bearing messages and the byte
  policy drops oldest-first until the session fits its ceiling;
* a second pass is a no-op, and an unknown workspace / garbage input is a
  no-op that never raises.
"""
from __future__ import annotations

import copy
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from agent.core import image_files, image_gc
from session.image_retention import (
    TOMBSTONE_COUNT_KEY,
    TOMBSTONE_MAX_IMAGE_BYTES,
    apply_image_retention,
)
from session.models import Session
from session.store import FileSystemSessionStore

WS = 'ws-tombstone'


@pytest.fixture(autouse=True)
def _hermetic_image_vault(tmp_path, monkeypatch):
    """Point ``vault_root()`` at the test's tmp dir (images + session files)."""
    vault = tmp_path / '.thoughtmachine'
    monkeypatch.setenv('THOUGHTMACHINE_VAULT_ROOT', str(vault))
    return vault


def _store(tmp_path) -> FileSystemSessionStore:
    return FileSystemSessionStore(
        sessions_dir=str(tmp_path / 'sessions'),
        state_dir=str(tmp_path / 'state'),
        enable_session_history_pruning=False,
    )


def _msg(content: str, images=(), role: str = 'user') -> dict:
    message = {'role': role, 'content': content}
    if images:
        message['images'] = list(images)
    return message


def _image(seed: str) -> str:
    """A well-formed (32 hex) image id that names no file on disk."""
    return (seed * 32)[:32]


class _FakePath:
    """Stand-in for ``Path`` whose ``stat()`` reports a chosen byte size."""

    def __init__(self, size: int) -> None:
        self._size = size

    def stat(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(st_size=self._size)


# ── Round trip through the real store ───────────────────────────────────────

def test_tombstoned_message_round_trips_content_byte_identical(tmp_path, monkeypatch):
    """The stored message keeps its content exactly; only ``images`` changes."""
    import session.image_retention as retention

    image_id = image_files.save_image(WS, 'image/png', b'\x89PNG\r\n\x1a\n' + b'x' * 16)
    original = 'why does this fail?'

    real = retention.apply_image_retention
    monkeypatch.setattr(
        retention,
        'apply_image_retention',
        lambda workspace_id, user_history: real(
            workspace_id, user_history, max_image_messages=0),
    )

    session = Session(
        session_id='sess-tombstone-roundtrip',
        metadata={'name': 'Tombstone round trip', 'agent_type': 'main'},
        user_history=[
            {'role': 'user', 'content': original, 'images': [image_id],
             'seq': 1, 'created_at': '2026-01-01T00:00:00'},
            {'role': 'assistant', 'content': 'it fails because the input is None',
             'seq': 2, 'created_at': '2026-01-01T00:00:01'},
        ],
    )
    store = _store(tmp_path)
    store.save_session(session, workspace_id=WS)

    path = store._find_session_path(session.session_id, workspace_id=WS)
    assert path is not None and path.exists()
    raw = json.loads(path.read_text())
    stored_user = [m for m in raw['user_history'] if m.get('role') == 'user'][0]
    assert stored_user['content'] == original
    assert stored_user['images'] == []
    assert stored_user[TOMBSTONE_COUNT_KEY] == 1

    loaded = store.load_session(session.session_id, workspace_id=WS)
    assert loaded is not None
    loaded_user = [m for m in loaded.user_history if m.get('role') == 'user'][0]
    assert loaded_user['content'] == original
    assert loaded_user['images'] == []
    assert loaded_user[TOMBSTONE_COUNT_KEY] == 1


# ── No bytes are deleted here ───────────────────────────────────────────────

def test_tombstone_never_deletes_bytes(monkeypatch):
    """Retention only unlinks ids from the transcript -- never the file."""
    image_id = image_files.save_image(WS, 'image/jpeg', b'jpeg-bytes')
    path = image_files.image_path(WS, image_id)
    assert path is not None and path.exists()

    def _boom(*args, **kwargs):
        raise AssertionError('image retention must never delete a stored image')

    monkeypatch.setattr(image_files, 'delete_image', _boom)

    out = apply_image_retention(
        WS, [_msg('what is this?', [image_id])], max_image_messages=0)

    assert out[0]['images'] == []
    assert out[0][TOMBSTONE_COUNT_KEY] == 1
    still_there = image_files.image_path(WS, image_id)
    assert still_there is not None and still_there.exists()


def test_id_is_absent_from_the_rewritten_message():
    """The released id must be gone from the persisted message bytes."""
    image_id = _image('ab')
    out = apply_image_retention(
        WS, [_msg('look at this', [image_id])], max_image_messages=0)

    serialised = json.dumps(out[0])
    assert image_id not in serialised
    assert json.loads(serialised)['content'] == 'look at this'


# ── Purity ──────────────────────────────────────────────────────────────────

def test_input_history_is_not_mutated():
    history = [_msg('first', [_image('a1')]), _msg('second', [_image('b2')])]
    snapshot = copy.deepcopy(history)

    out = apply_image_retention(WS, history, max_image_messages=1)

    assert out is not history
    assert history == snapshot
    assert out[0]['images'] == []
    assert out[1]['images'] == snapshot[1]['images']
    assert TOMBSTONE_COUNT_KEY not in history[0]


# ── Count policy ────────────────────────────────────────────────────────────

def test_count_policy_tombstones_only_the_oldest_over_n():
    history = [_msg(f'query {i}', [_image(f'{i:02d}')]) for i in range(41)]

    out = apply_image_retention(WS, history, max_image_messages=40)

    assert out is not history
    assert out[0]['images'] == []
    assert out[0][TOMBSTONE_COUNT_KEY] == 1
    for index in range(1, 41):
        assert out[index]['images'] == [_image(f'{index:02d}')]
        assert TOMBSTONE_COUNT_KEY not in out[index]


# ── Byte policy ─────────────────────────────────────────────────────────────

def test_byte_policy_tombstones_oldest_first_until_under_ceiling(monkeypatch):
    ten_mb = 10 * 1024 * 1024
    sizes = {_image(f'{i:02d}'): ten_mb for i in range(5)}
    history = [_msg(f'query {i}', [_image(f'{i:02d}')]) for i in range(5)]

    def _fake_image_path(workspace_id, image_id):
        size = sizes.get(image_id)
        return None if size is None else _FakePath(size)

    monkeypatch.setattr(image_files, 'image_path', _fake_image_path)

    # 50 MiB live, ceiling 25 MiB: 3 messages must go (0, 1, 2 -> 20 MiB left).
    out = apply_image_retention(
        WS, history, max_image_messages=100, max_bytes=25 * 1024 * 1024)

    assert [TOMBSTONE_COUNT_KEY in message for message in out] == [
        True, True, True, False, False,
    ]
    assert out[0]['images'] == []
    assert out[3]['images'] == [_image('03')]


# ── Idempotence and reach ───────────────────────────────────────────────────

def test_second_pass_is_a_no_op():
    history = [_msg('only', [_image('cd')])]
    first = apply_image_retention(WS, history, max_image_messages=0)
    assert first[0]['images'] == []

    second = apply_image_retention(WS, first, max_image_messages=0)

    assert second is first
    assert second == first


def test_tombstoning_one_message_leaves_other_messages_ids_live():
    """Only the message being tombstoned loses ids; its neighbours keep theirs.

    Candidate selection stays role-agnostic: a non-user message carrying images
    is a valid carrier too, and the bytes are only bounded if every carrier is
    a candidate.
    """
    user_id, assistant_id, tool_id = _image('e1'), _image('f2'), _image('a3')
    history = [
        _msg('the screenshot', [user_id], role='user'),
        _msg('I see it', [assistant_id], role='assistant'),
        {'role': 'tool', 'content': 'ok', 'images': [tool_id],
         'tool_call_id': 'call-1'},
    ]

    out = apply_image_retention(WS, history, max_image_messages=2)

    assert out[0]['images'] == []
    assert out[1]['images'] == [assistant_id]
    assert out[2]['images'] == [tool_id]
    assert TOMBSTONE_COUNT_KEY not in out[1]
    assert TOMBSTONE_COUNT_KEY not in out[2]

    # The released id is the only one the collector stops seeing: the ids the
    # later messages carry are still live, so their bytes must not be swept.
    live = image_gc.live_image_ids(out)
    assert user_id not in live
    assert live == {assistant_id, tool_id}


def test_missing_workspace_id_is_a_no_op():
    history = [_msg('no workspace', [_image('11')])]

    for workspace_id in (None, ''):
        out = apply_image_retention(workspace_id, history, max_image_messages=0)
        assert out is history
        assert out[0]['images'] == [_image('11')]
        assert TOMBSTONE_COUNT_KEY not in out[0]


def test_garbage_input_never_raises():
    history = [_msg('real', [_image('22')])]

    assert apply_image_retention(WS, None, max_image_messages=0) is None
    assert apply_image_retention(WS, [], max_image_messages=0) == []
    assert apply_image_retention(WS, 'not a history', max_image_messages=0) == 'not a history'
    assert apply_image_retention(WS, {'images': ['x']}, max_image_messages=0) == {'images': ['x']}

    mixed = [None, 'x', 42, {'images': 'nope'}, {'images': []}, {'images': [1, 2]}, history[0]]
    out = apply_image_retention(WS, mixed, max_image_messages=0)
    assert out is not None

    # A policy parameter the pass cannot use is swallowed, not raised.
    assert apply_image_retention(WS, history, max_image_messages=None) is history


def test_session_ceiling_is_one_eighth_of_workspace_budget():
    assert TOMBSTONE_MAX_IMAGE_BYTES * 8 == image_files.MAX_WORKSPACE_IMAGE_BYTES
