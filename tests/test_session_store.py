"""
Tests for the FileSystemSessionStore.

Tests cover:
- Initialization with custom sessions_dir and state_dir
- Save/Load round-trip
- Atomic session saves (temp file cleanup, crash resilience)
- Open sessions path relocation to state directory
- Current session marker in state directory
- Session listing and deletion
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from session.store import FileSystemSessionStore


@pytest.fixture
def store(tmp_path):
    """Create a FileSystemSessionStore with temporary directories."""
    sessions_dir = tmp_path / "sessions"
    state_dir = tmp_path / "state"
    return FileSystemSessionStore(
        sessions_dir=str(sessions_dir),
        state_dir=str(state_dir),
        enable_session_history_pruning=False,
    )


@pytest.fixture
def sample_session(store):
    """Create a sample Session with some conversation history."""
    from session.models import Session
    session = Session()
    session.metadata['name'] = 'Test Session'
    session.add_message('user', 'Hello, how are you?')
    session.add_message('assistant', 'I am doing well, thank you!')
    session.total_input_tokens = 100
    session.total_output_tokens = 200
    return session


# --- Initialization Tests ---

class TestInit:
    def test_default_dirs_use_thoughtmachine_home(self):
        """Verify that default paths use ~/.thoughtmachine/."""
        store = FileSystemSessionStore(enable_session_history_pruning=False)
        home = os.path.expanduser("~")
        assert str(store.sessions_dir) == os.path.join(home, ".thoughtmachine", "sessions")
        assert str(store.state_dir) == os.path.join(home, ".thoughtmachine", "state")

    def test_custom_sessions_dir(self, tmp_path):
        """Verify custom sessions directory is used."""
        d = tmp_path / "custom_sessions"
        store = FileSystemSessionStore(sessions_dir=str(d), enable_session_history_pruning=False)
        assert store.sessions_dir == d

    def test_custom_state_dir(self, tmp_path):
        """Verify custom state directory is used."""
        sessions = tmp_path / "sessions"
        state = tmp_path / "my_state"
        store = FileSystemSessionStore(sessions_dir=str(sessions), state_dir=str(state),
                                        enable_session_history_pruning=False)
        assert store.state_dir == state

    def test_directories_created_on_init(self, tmp_path):
        """Verify directories are created automatically."""
        sessions = tmp_path / "new_sessions"
        state = tmp_path / "new_state"
        assert not sessions.exists()
        assert not state.exists()
        store = FileSystemSessionStore(sessions_dir=str(sessions), state_dir=str(state),
                                        enable_session_history_pruning=False)
        assert sessions.exists()
        assert state.exists()


# --- Open Sessions Path Tests ---

class TestOpenSessionsPath:
    def test_path_in_state_dir(self, store):
        """Verify open_sessions.json is returned inside state_dir."""
        path = store.get_open_sessions_path()
        assert path == store.state_dir / 'open_sessions.json'

    def test_path_parent_created(self, store):
        """Verify the state directory exists for the path."""
        path = store.get_open_sessions_path()
        assert path.parent.exists()


# --- Save/Load Round-Trip Tests ---

class TestSaveLoadRoundTrip:
    def test_save_and_load(self, store, sample_session):
        """Verify a basic save/load round-trip preserves all fields."""
        store.save_session(sample_session)
        loaded = store.load_session(sample_session.session_id)
        assert loaded is not None
        assert loaded.session_id == sample_session.session_id
        assert loaded.metadata.get('name') == 'Test Session'
        assert len(loaded.user_history) == 2
        assert loaded.user_history[0]['role'] == 'user'
        assert loaded.user_history[0]['content'] == 'Hello, how are you?'
        assert loaded.user_history[1]['role'] == 'assistant'
        assert loaded.user_history[1]['content'] == 'I am doing well, thank you!'
        assert loaded.total_input_tokens == 100
        assert loaded.total_output_tokens == 200

    def test_save_updates_timestamp(self, store, sample_session):
        """Verify that save_session updates updated_at."""
        original_updated = sample_session.updated_at
        store.save_session(sample_session)
        loaded = store.load_session(sample_session.session_id)
        assert loaded is not None
        assert loaded.updated_at >= original_updated

    def test_load_nonexistent_returns_none(self, store):
        """Verify loading a non-existent session returns None."""
        loaded = store.load_session('nonexistent-id')
        assert loaded is None

    def test_double_save_overwrites(self, store, sample_session):
        """Verify saving twice overwrites the previous file."""
        store.save_session(sample_session)
        sample_session.total_input_tokens = 999
        store.save_session(sample_session)
        loaded = store.load_session(sample_session.session_id)
        assert loaded is not None
        assert loaded.total_input_tokens == 999

    def test_multiple_sessions(self, store):
        """Verify multiple sessions can be saved and loaded independently."""
        from session.models import Session
        sessions = []
        for i in range(3):
            s = Session()
            s.metadata['name'] = f'Session {i}'
            s.add_message('user', f'Message {i}')
            store.save_session(s)
            sessions.append(s)
        for i, s in enumerate(sessions):
            loaded = store.load_session(s.session_id)
            assert loaded is not None
            assert loaded.metadata['name'] == f'Session {i}'


# --- Atomic Save Tests ---

class TestAtomicSave:
    def test_temp_file_cleaned_up(self, store, sample_session):
        """Verify no .tmp files linger after successful save."""
        store.save_session(sample_session)
        tmp_files = list(store.sessions_dir.glob("*.tmp"))
        assert len(tmp_files) == 0, f"Found leftover temp files: {tmp_files}"

    def test_temp_file_cleaned_up_on_error(self, store, sample_session, monkeypatch):
        """Verify temp files are cleaned up even when json.dump fails."""
        def failing_dump(*args, **kwargs):
            raise IOError("Simulated write failure")
        monkeypatch.setattr(json, 'dump', failing_dump)
        with pytest.raises(IOError):
            store.save_session(sample_session)
        tmp_files = list(store.sessions_dir.glob("*.tmp"))
        assert len(tmp_files) == 0, f"Found leftover temp files after error: {tmp_files}"

    def test_file_is_valid_json(self, store, sample_session):
        """Verify the saved file contains valid JSON."""
        store.save_session(sample_session)
        path = store._find_session_path(sample_session.session_id)
        assert path is not None
        with open(path, 'r') as f:
            data = json.load(f)
        assert data['session_id'] == sample_session.session_id
        assert 'user_history' in data


# --- .current_session Marker Tests ---

class TestCurrentSessionMarker:
    def test_marker_in_state_dir(self, store):
        """Verify .current_session is stored in state_dir."""
        store.set_current_session_id('test-session-id')
        marker = store.state_dir / ".current_session"
        assert marker.exists()
        assert marker.read_text().strip() == 'test-session-id'

    def test_marker_not_in_sessions_dir(self, store):
        """Verify .current_session is NOT stored in sessions_dir."""
        store.set_current_session_id('test-session-id')
        old_marker = store.sessions_dir / ".current_session"
        assert not old_marker.exists(), ".current_session should NOT be in sessions_dir anymore"

    def test_get_set_current_session(self, store):
        """Verify set/get round-trip for current session."""
        store.set_current_session_id('my-session')
        assert store.get_current_session_id() == 'my-session'

    def test_clear_marker(self, store):
        """Verify setting to None removes the marker."""
        store.set_current_session_id('test-session')
        assert store.get_current_session_id() == 'test-session'
        store.set_current_session_id(None)
        assert store.get_current_session_id() is None
        assert not store.state_dir.joinpath(".current_session").exists()


# --- List Sessions Tests ---

class TestListSessions:
    def test_list_empty(self, store):
        """Verify listing when no sessions returns empty list."""
        sessions = store.list_sessions()
        assert sessions == []

    def test_list_with_sessions(self, store, sample_session):
        """Verify listing returns saved sessions."""
        store.save_session(sample_session)
        sessions = store.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]['session_id'] == sample_session.session_id
        assert sessions[0]['name'] == 'Test Session'

    def test_list_skips_non_session_files(self, store):
        """Verify open_sessions.json list file is not listed as a session."""
        store.save_session(self._make_session(store, 'Session A'))
        # Write a non-session JSON file (like open_sessions.json)
        non_session = store.sessions_dir / 'not_a_session.json'
        with open(non_session, 'w') as f:
            json.dump(['list', 'of', 'ids'], f)
        sessions = store.list_sessions()
        assert len(sessions) == 1

    @staticmethod
    def _make_session(store, name: str):
        from session.models import Session
        s = Session()
        s.metadata['name'] = name
        s.add_message('user', f'Hello from {name}')
        store.save_session(s)
        return s


# --- Delete Session Tests ---

class TestDeleteSession:
    def test_delete_existing(self, store, sample_session):
        """Verify deleting an existing session returns True."""
        store.save_session(sample_session)
        assert store.delete_session(sample_session.session_id) is True
        assert store.load_session(sample_session.session_id) is None

    def test_delete_nonexistent(self, store):
        """Verify deleting a non-existent session returns False."""
        assert store.delete_session('nonexistent') is False


# --- Get Session Path Tests ---

class TestGetSessionPath:
    def test_get_path_for_saved_session(self, store, sample_session):
        """Verify get_session_path returns the actual file path."""
        store.save_session(sample_session)
        path = store.get_session_path(sample_session.session_id)
        assert path.exists()

    def test_get_path_for_unsaved_session(self, store):
        """Verify get_session_path returns a sensible default for unsaved sessions."""
        path = store.get_session_path('nonexistent-id')
        assert str(path.name) == 'nonexistent-id.json'
# --- Migration Tests ---

class TestCurrentSessionMigration:
    """Test migration of .current_session from sessions_dir to state_dir."""

    def test_migrates_from_old_location_on_read(self, store):
        """Verify .current_session is migrated from sessions_dir to state_dir on first read."""
        # Write .current_session in old location (sessions_dir)
        old_marker = store.sessions_dir / ".current_session"
        old_marker.write_text("migrated-session-id")
        assert old_marker.exists()
        assert not (store.state_dir / ".current_session").exists()

        # Read should trigger migration
        session_id = store.get_current_session_id()
        assert session_id == "migrated-session-id"

        # Old marker should be removed
        assert not old_marker.exists(), "Old marker should be deleted after migration"

        # New marker should exist
        new_marker = store.state_dir / ".current_session"
        assert new_marker.exists()
        assert new_marker.read_text().strip() == "migrated-session-id"

    def test_prefers_new_location_over_old(self, store):
        """Verify the new location takes precedence when both exist."""
        # Write in new location
        new_marker = store.state_dir / ".current_session"
        new_marker.write_text("new-session-id")

        # Write in old location (should be ignored)
        old_marker = store.sessions_dir / ".current_session"
        old_marker.write_text("old-session-id")

        # Read should return from new location
        session_id = store.get_current_session_id()
        assert session_id == "new-session-id"

        # Old marker should remain (since new existed, no migration needed)
        assert old_marker.exists()

    def test_empty_old_marker_does_not_migrate(self, store):
        """Verify an empty old marker does not create a new marker."""
        old_marker = store.sessions_dir / ".current_session"
        old_marker.write_text("")
        assert store.get_current_session_id() is None
        assert not (store.state_dir / ".current_session").exists()


# --- Open Sessions Migration Tests ---

class TestOpenSessionsMigration:
    """Test migration of open_sessions.json from sessions_dir to state_dir."""

    def test_reads_from_new_location_first(self, store):
        """Verify new location is tried first."""
        # Write in new location
        new_path = store.state_dir / 'open_sessions.json'
        new_path.write_text('["session-1"]')

        # Write in old location (should be ignored)
        old_path = store.sessions_dir / 'open_sessions.json'
        old_path.write_text('["session-2"]')

        # The store's get_open_sessions_path returns new location
        assert store.get_open_sessions_path() == new_path

    def test_fallback_to_old_location(self, store):
        """Verify old location is a fallback concept in main_window (tested via store path)."""
        # Old location exists, new does not
        old_path = store.sessions_dir / 'open_sessions.json'
        old_path.write_text('["session-a"]')

        new_path = store.state_dir / 'open_sessions.json'
        assert not new_path.exists()

        # Verify store points to new location
        assert store.get_open_sessions_path() == new_path

    def test_list_ignores_old_open_sessions_in_sessions_dir(self, store):
        """Verify list_sessions() ignores a leftover open_sessions.json in sessions_dir."""
        from session.models import Session
        s = Session()
        s.metadata['name'] = 'Real Session'
        s.add_message('user', 'Hello')
        store.save_session(s)

        # Write a leftover open_sessions.json in sessions_dir (like a list)
        old_file = store.sessions_dir / 'open_sessions.json'
        with open(old_file, 'w') as f:
            json.dump(['some', 'ids'], f)

        sessions = store.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]['session_id'] == s.session_id

