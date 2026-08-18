"""
Tests for ``session_size_bytes`` exposure in the session REST routes
(GET /api/session/{id} and GET /api/session/list).

The endpoints are invoked directly as coroutines (``asyncio.run``) with
``session_routes._get_store`` monkeypatched to a temporary
``FileSystemSessionStore``, so the real singleton (and the real
``~/.thoughtmachine``) is never touched.  For the list endpoint the module's
``SessionRegistry`` binding is replaced with a stub whose ``get_all()``
returns the registry entries we control.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

import pytest

from session.models import Session
from session.store import FileSystemSessionStore

from web_ui.backend import session_routes


class _StubRegistry:
    """Minimal stand-in for the object returned by SessionRegistry.get_default()."""

    def __init__(self, entries):
        self._entries = entries

    def get_all(self):
        return dict(self._entries)

    def rebuild_from_disk(self):
        pass


class _RegistryType:
    """Class-level stand-in for the SessionRegistry class itself."""

    _entries = {}

    @classmethod
    def get_default(cls):
        return _StubRegistry(dict(cls._entries))


def _make_store(tmp_path) -> FileSystemSessionStore:
    return FileSystemSessionStore(
        sessions_dir=str(tmp_path / 'sessions'),
        state_dir=str(tmp_path / 'state'),
        enable_session_history_pruning=False,
    )


# ── GET /api/session/{session_id} ───────────────────────────────────────────

class TestGetSessionSize:
    def test_detail_includes_session_size_bytes(self, tmp_path, monkeypatch):
        store = _make_store(tmp_path)
        monkeypatch.setattr(session_routes, '_get_store', lambda: store)

        session = Session()
        session.metadata['agent_type'] = 'main'
        session.metadata['name'] = 'Main Session'
        session.add_message('user', 'Hello')
        session.add_message('assistant', 'Hi')
        sid = session.session_id
        store.save_session(session)

        detail = asyncio.run(session_routes.get_session(sid))
        assert detail['session_id'] == sid
        assert isinstance(detail['session_size_bytes'], int)
        assert detail['session_size_bytes'] > 0
        assert detail['session_size_bytes'] == store.get_session_size_bytes(sid)

    def test_detail_size_none_for_non_main_session(self, tmp_path, monkeypatch):
        store = _make_store(tmp_path)
        monkeypatch.setattr(session_routes, '_get_store', lambda: store)

        session = Session()
        session.metadata['name'] = 'Worker Session'
        session.add_message('user', 'Hello')
        session.add_message('assistant', 'Hi')
        sid = session.session_id
        store.save_session(session)

        detail = asyncio.run(session_routes.get_session(sid))
        assert detail['session_id'] == sid
        assert detail['session_size_bytes'] is None


# ── GET /api/session/list ───────────────────────────────────────────────────

class TestListSessionsSize:
    def test_list_includes_session_size_bytes(self, tmp_path, monkeypatch):
        store = _make_store(tmp_path)
        monkeypatch.setattr(session_routes, '_get_store', lambda: store)

        session = Session()
        session.metadata['agent_type'] = 'main'
        session.metadata['name'] = 'Main Session'
        session.add_message('user', 'Hello')
        session.add_message('assistant', 'Hi')
        sid = session.session_id
        store.save_session(session)

        _RegistryType._entries = {
            sid: {
                'session_id': sid,
                'name': 'Main Session',
                'mode': 'agent',
                'workspace_id': '',
                'created_at': None,
                'updated_at': None,
            }
        }
        monkeypatch.setattr(session_routes, 'SessionRegistry', _RegistryType)

        items = asyncio.run(session_routes.list_sessions(workspace_id=None))
        assert len(items) == 1
        assert items[0]['session_id'] == sid
        assert isinstance(items[0]['session_size_bytes'], int)
        assert items[0]['session_size_bytes'] > 0
        assert items[0]['session_size_bytes'] == store.get_session_size_bytes(sid)

    def test_list_size_none_for_non_main_session(self, tmp_path, monkeypatch):
        store = _make_store(tmp_path)
        monkeypatch.setattr(session_routes, '_get_store', lambda: store)

        session = Session()
        session.metadata['name'] = 'Worker Session'
        session.add_message('user', 'Hello')
        session.add_message('assistant', 'Hi')
        sid = session.session_id
        store.save_session(session)

        _RegistryType._entries = {
            sid: {
                'session_id': sid,
                'name': 'Worker Session',
                'mode': 'agent',
                'workspace_id': '',
                'created_at': None,
                'updated_at': None,
            }
        }
        monkeypatch.setattr(session_routes, 'SessionRegistry', _RegistryType)
        items = asyncio.run(session_routes.list_sessions(workspace_id=None))
        assert len(items) == 1
        assert items[0]['session_size_bytes'] is None
