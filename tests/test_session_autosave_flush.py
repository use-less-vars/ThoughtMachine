"""Tests for the periodic session autosave (C89).

Nothing in this backend writes a session after a turn: the bridge's turn loop
ends by emitting ``session_stop``, not by saving, so the session file lags the
live conversation until a switch / create / close / shutdown.  ``server`` now
runs a background flush loop that calls the SAME ``bridge.save_session()`` the
shutdown hook calls, on an interval, so a crash loses at most one interval.

These tests drive the real ``FileSystemSessionStore`` in ``tmp_path`` and a
stand-in bridge exposing only the attributes the flusher touches, so no
websocket, no docker daemon and no agent is involved.
"""
import asyncio
import json
import os
import threading
import uuid

import pytest

import session.store as session_store_mod
from session.models import Session
from session.store import FileSystemSessionStore


def _load_server():
    from web_ui.backend import server
    return server


def _store(tmp_path):
    return FileSystemSessionStore(
        sessions_dir=str(tmp_path / 'sessions'),
        state_dir=str(tmp_path / 'state'),
        # Pruning rewrites old summarisation cycles on save; off, so a test
        # message can never be pruned out from under an assertion.
        enable_session_history_pruning=False,
    )


class _FakeBridge:
    """Minimal stand-in for ``WebAgentBridge`` (only the flusher's surface).

    ``save_session`` deliberately mirrors the real one: it falls back to
    ``_loaded_session`` when ``_session`` is None (bridge.py:2630-2640).  That
    fallback is what makes the flusher's ``_session is None`` guard mandatory --
    without it this fake happily overwrites a newer on-disk copy with a stale
    in-memory one, exactly as the real bridge would.
    """

    def __init__(self, store, session=None, loaded=None, cleanly_closed=False,
                 raise_on_save=False):
        self._session = session
        self._loaded_session = loaded if loaded is not None else session
        self._cleanly_closed = cleanly_closed
        self._session_state_lock = threading.RLock()
        self._store = store
        self._raise_on_save = raise_on_save
        self.save_calls = 0

    def save_session(self, name=None):
        self.save_calls += 1
        if self._raise_on_save:
            raise RuntimeError('save exploded')
        target = self._session if self._session is not None else self._loaded_session
        self._store.save_session(target)


@pytest.fixture
def server_module(monkeypatch):
    """``server`` with an empty bridge cache and a fresh version-marker map."""
    server = _load_server()
    monkeypatch.setattr(server, '_session_bridges', {})
    monkeypatch.setattr(server, '_session_flush_versions', {})
    return server


def _persisted_text(store, session_id):
    """Everything the store holds for *session_id*, as one searchable string."""
    loaded = store.load_session(session_id)
    if loaded is None:
        return ''
    return json.dumps(loaded.user_history, default=str)


def _live_session(store, name):
    """A registered bridge around a fresh live session; returns (sid, session, bridge)."""
    session_id = str(uuid.uuid4())
    session = Session(session_id=session_id, metadata={'name': name})
    return session_id, session, _FakeBridge(store, session=session)


def test_flush_writes_a_turn_that_nothing_else_persisted(tmp_path, server_module):
    """C89 core: a completed turn reaches disk on the next tick, without a save."""
    server = server_module
    store = _store(tmp_path)
    session_id, session, bridge = _live_session(store, 'c89 flush')
    server._session_bridges[session_id] = bridge

    session.add_message('user', 'HELLO-UNSAVED-TURN')

    # The defect, before the flusher runs: the live session has the turn, the
    # on-disk copy does not.
    assert 'HELLO-UNSAVED-TURN' not in _persisted_text(store, session_id)

    flushed = asyncio.run(server._flush_sessions_once())

    # The substantive claim FIRST: the turn is now ON DISK.  This is the
    # assertion that must fail when the flush is disabled, before any of the
    # bookkeeping assertions below can mask it.
    assert 'HELLO-UNSAVED-TURN' in _persisted_text(store, session_id)
    assert flushed == [session_id]
    assert bridge.save_calls == 1


def test_failed_write_keeps_the_previous_complete_copy(
        tmp_path, server_module, monkeypatch):
    """A save that dies mid-replace must leave the last complete file intact."""
    server = server_module
    store = _store(tmp_path)
    session_id, session, bridge = _live_session(store, 'c89 torn write')
    server._session_bridges[session_id] = bridge

    session.add_message('user', 'FIRST-TURN')
    assert asyncio.run(server._flush_sessions_once()) == [session_id]
    assert 'FIRST-TURN' in _persisted_text(store, session_id)

    class _NoReplaceOS:
        """``session.store``'s view of ``os``, with a failing ``os.replace``."""

        def __getattr__(self, name):
            return getattr(os, name)

        def replace(self, *args, **kwargs):
            raise OSError('simulated disk full')

    monkeypatch.setattr(session_store_mod, 'os', _NoReplaceOS())

    session.add_message('assistant', 'SECOND-TURN-LOST')

    # The tick must not raise, and must not claim a write it did not make.
    assert asyncio.run(server._flush_sessions_once()) == []
    text = _persisted_text(store, session_id)
    assert 'FIRST-TURN' in text
    assert 'SECOND-TURN-LOST' not in text


def test_unchanged_session_is_not_rewritten(tmp_path, server_module):
    """Two ticks with no conversation change cost exactly one write.

    Pinned against a REAL ``session.models.Session`` -- the object that ships and
    that ``bridge.save_session`` actually persists -- so the no-churn guarantee is
    measured on the model's own change counter, not on a stub that could define
    an accessor production does not have.
    """
    server = server_module
    store = _store(tmp_path)
    session_id, session, bridge = _live_session(store, 'c89 idle')
    server._session_bridges[session_id] = bridge

    assert isinstance(session, Session), 'test must run against the real model'

    session.add_message('user', 'ONLY-TURN')
    version_before = session.conversation_version
    # The counter the flusher observes is a real int that has actually moved off
    # the construction default; otherwise the skip guard could never fire.
    assert isinstance(version_before, int), version_before
    assert version_before > 0, version_before

    assert asyncio.run(server._flush_sessions_once()) == [session_id]
    assert bridge.save_calls == 1
    # The flusher stored exactly the version it observed.
    assert server._session_flush_versions[session_id] == version_before

    # No conversation change between ticks: the observed version is unchanged.
    assert session.conversation_version == version_before
    assert asyncio.run(server._flush_sessions_once()) == []
    assert bridge.save_calls == 1

    session.add_message('assistant', 'NEXT-TURN')
    version_after = session.conversation_version
    assert version_after > version_before, (version_before, version_after)

    assert asyncio.run(server._flush_sessions_once()) == [session_id]
    assert bridge.save_calls == 2
    assert server._session_flush_versions[session_id] == version_after


def test_bridge_without_a_live_session_is_never_saved(tmp_path, server_module):
    """No ``_session`` -> never call save_session (it would write stale history)."""
    server = server_module
    store = _store(tmp_path)
    session_id = str(uuid.uuid4())

    newer = Session(session_id=session_id, metadata={'name': 'c89 guard'})
    newer.add_message('user', 'NEWER-ON-DISK')
    store.save_session(newer)

    stale = Session(session_id=session_id, metadata={'name': 'c89 guard'})
    stale.add_message('user', 'STALE-IN-MEMORY')
    bridge = _FakeBridge(store, session=None, loaded=stale)
    server._session_bridges[session_id] = bridge

    assert asyncio.run(server._flush_sessions_once()) == []
    assert bridge.save_calls == 0
    text = _persisted_text(store, session_id)
    assert 'NEWER-ON-DISK' in text
    assert 'STALE-IN-MEMORY' not in text


def test_cleanly_closed_bridge_is_never_saved(tmp_path, server_module):
    """close_session already saved; a late tick must not write again."""
    server = server_module
    store = _store(tmp_path)
    session_id = str(uuid.uuid4())
    session = Session(session_id=session_id, metadata={'name': 'c89 closed'})
    bridge = _FakeBridge(store, session=session, cleanly_closed=True)
    server._session_bridges[session_id] = bridge

    session.add_message('user', 'AFTER-CLOSE')
    assert asyncio.run(server._flush_sessions_once()) == []
    assert bridge.save_calls == 0


def test_one_failing_session_does_not_stop_the_tick(tmp_path, server_module):
    """A raising save is isolated; its sibling in the same tick still flushes."""
    server = server_module
    store = _store(tmp_path)

    bad_id = str(uuid.uuid4())
    bad = _FakeBridge(store, session=Session(session_id=bad_id), raise_on_save=True)
    good_id, good_session, good = _live_session(store, 'c89 sibling')
    good_session.add_message('user', 'SIBLING-SURVIVED')

    server._session_bridges[bad_id] = bad      # inserted first: tick hits it first
    server._session_bridges[good_id] = good

    flushed = asyncio.run(server._flush_sessions_once())

    assert flushed == [good_id]
    assert bad.save_calls == 1
    assert 'SIBLING-SURVIVED' in _persisted_text(store, good_id)


def test_markers_for_gone_sessions_are_pruned(server_module):
    """close_session removes the bridge; its stale marker must not leak."""
    server_module._session_flush_versions['gone-session'] = 7

    asyncio.run(server_module._flush_sessions_once())

    assert 'gone-session' not in server_module._session_flush_versions


def test_flush_loop_keeps_ticking_and_survives_failures(tmp_path, server_module):
    """The loop iterates repeatedly, honours cancellation, and never dies."""
    server = server_module
    store = _store(tmp_path)

    bad_id = str(uuid.uuid4())
    server._session_bridges[bad_id] = _FakeBridge(
        store, session=Session(session_id=bad_id), raise_on_save=True)
    session_id, session, bridge = _live_session(store, 'c89 loop')
    server._session_bridges[session_id] = bridge

    async def scenario():
        task = asyncio.create_task(
            server._periodic_session_flush_loop(interval_s=0.02))
        await asyncio.sleep(0.05)
        session.add_message('user', 'TURN-ONE')
        await asyncio.sleep(0.2)
        assert not task.done(), 'flush loop should still be running'
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return task

    task = asyncio.run(scenario())

    assert task.cancelled()
    assert bridge.save_calls >= 1
    assert 'TURN-ONE' in _persisted_text(store, session_id)


def test_lifespan_creates_and_cancels_flush_task(monkeypatch, server_module):
    """Startup creates app.state.session_flush_task; shutdown cancels it."""
    server = server_module

    # Neutralise the real sweeps so startup does no docker/filesystem work.
    monkeypatch.setattr(
        server, '_sweep_exited_workspace_containers', lambda: None)
    monkeypatch.setattr(
        server, '_sweep_orphan_resource_containers', lambda: None)
    monkeypatch.setattr(
        server, '_sweep_orphan_container_records', lambda: None)

    captured = {}

    async def scenario():
        async with server.lifespan(server.app):
            task = getattr(server.app.state, 'session_flush_task', None)
            assert task is not None, 'lifespan did not create the flush task'
            assert not task.done(), 'flush task should be pending at startup'
            captured['task'] = task
        return captured['task']

    task = asyncio.run(scenario())

    assert task.done(), 'flush task not finished after shutdown'
    assert task.cancelled() or task.exception() is None
