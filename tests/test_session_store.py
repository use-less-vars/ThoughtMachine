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

    def test_directories_created_lazily_on_first_write(self, tmp_path):
        """Verify directories are created on first write, not on init."""
        sessions = tmp_path / "new_sessions"
        state = tmp_path / "new_state"
        assert not sessions.exists()
        assert not state.exists()
        store = FileSystemSessionStore(sessions_dir=str(sessions), state_dir=str(state),
                                        enable_session_history_pruning=False)
        # Construction performs no filesystem I/O
        assert not sessions.exists()
        assert not state.exists()
        # The first write creates the directories
        from session.models import Session
        store.save_session(Session())
        assert sessions.exists()
        assert state.exists()


# --- Open Sessions Path Tests ---

class TestOpenSessionsPath:
    def test_path_in_state_dir(self, store):
        """Verify open_sessions.json is returned inside state_dir."""
        path = store.get_open_sessions_path()
        assert path == store.state_dir / 'open_sessions.json'

    def test_path_parent_created_on_write(self, store):
        """Verify the state directory is created lazily on first write."""
        path = store.get_open_sessions_path()
        assert not path.parent.exists()
        store.save_open_sessions([])
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

    def test_delete_scoped_to_workspace_leaves_other_workspace_intact(
        self, store, sample_session, tmp_path, monkeypatch
    ):
        """Verify a workspace-scoped delete cannot reach another workspace.

        Workspace-scoped session files live under vault_root()/workspaces, so
        the vault root is redirected into tmp_path for isolation.
        """
        monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path / "vault"))
        session_id = sample_session.session_id
        store.save_session(sample_session, workspace_id="ws-b")
        assert store.load_session(session_id, workspace_id="ws-b") is not None

        # Deleting under a different workspace must not touch the session.
        assert store.delete_session(session_id, workspace_id="ws-a") is False
        assert store.load_session(session_id, workspace_id="ws-b") is not None

        # The owning workspace can still delete it.
        assert store.delete_session(session_id, workspace_id="ws-b") is True
        assert store.load_session(session_id, workspace_id="ws-b") is None


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
    """Test read-only fallback to legacy .current_session in sessions_dir."""

    def test_falls_back_to_old_location_read_only(self, store):
        """Verify the legacy marker is read without migrating.

        The legacy fallback is read-only: the old marker stays in place and no
        new marker is written to state_dir.  set_current_session_id still writes
        the new-location marker.
        """
        # sessions_dir is created lazily on first write; create it manually
        store.sessions_dir.mkdir(parents=True, exist_ok=True)
        # Write .current_session in old location (sessions_dir)
        old_marker = store.sessions_dir / ".current_session"
        old_marker.write_text("migrated-session-id")
        assert old_marker.exists()
        assert not (store.state_dir / ".current_session").exists()

        # Read falls back to the old location without migrating
        session_id = store.get_current_session_id()
        assert session_id == "migrated-session-id"

        # Old marker should be left in place (read-only fallback)
        assert old_marker.exists(), "Old marker should NOT be deleted by a read"

        # No new marker should be created by the read
        new_marker = store.state_dir / ".current_session"
        assert not new_marker.exists(), "Read should not write a new marker"

        # A subsequent set still writes the new-location marker
        store.set_current_session_id("migrated-session-id")
        assert new_marker.exists()
        assert new_marker.read_text().strip() == "migrated-session-id"

    def test_prefers_new_location_over_old(self, store):
        """Verify the new location takes precedence when both exist."""
        # Write in new location
        store.state_dir.mkdir(parents=True, exist_ok=True)
        new_marker = store.state_dir / ".current_session"
        new_marker.write_text("new-session-id")

        # Write in old location (should be ignored)
        store.sessions_dir.mkdir(parents=True, exist_ok=True)
        old_marker = store.sessions_dir / ".current_session"
        old_marker.write_text("old-session-id")

        # Read should return from new location
        session_id = store.get_current_session_id()
        assert session_id == "new-session-id"

        # Old marker should remain (since new existed, no migration needed)
        assert old_marker.exists()

    def test_empty_old_marker_does_not_migrate(self, store):
        """Verify an empty old marker does not create a new marker."""
        store.sessions_dir.mkdir(parents=True, exist_ok=True)
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
        store.state_dir.mkdir(parents=True, exist_ok=True)
        new_path = store.state_dir / 'open_sessions.json'
        new_path.write_text('["session-1"]')

        # Write in old location (should be ignored)
        store.sessions_dir.mkdir(parents=True, exist_ok=True)
        old_path = store.sessions_dir / 'open_sessions.json'
        old_path.write_text('["session-2"]')

        # The store's get_open_sessions_path returns new location
        assert store.get_open_sessions_path() == new_path

    def test_fallback_to_old_location(self, store):
        """Verify old location is a fallback concept in main_window (tested via store path)."""
        # Old location exists, new does not
        store.sessions_dir.mkdir(parents=True, exist_ok=True)
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


# --- Workspace-scoped read path tests ---

class TestWorkspaceScopedReadPaths:
    """Reads that name a workspace must never resolve into another workspace.

    Workspace-scoped session files live under vault_root()/workspaces, so the
    vault root is redirected into tmp_path for isolation (same pattern as
    ``TestDeleteSession.test_delete_scoped_to_workspace_leaves_other_workspace_intact``).
    """

    def test_load_session_scoped_to_workspace(
        self, store, sample_session, tmp_path, monkeypatch
    ):
        """D: a scoped load may not read another workspace's session."""
        monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path / "vault"))
        session_id = sample_session.session_id
        store.save_session(sample_session, workspace_id="ws-b")

        # A different workspace sees nothing.
        assert store.load_session(session_id, workspace_id="ws-a") is None

        # The owning workspace still loads it.
        loaded = store.load_session(session_id, workspace_id="ws-b")
        assert loaded is not None
        assert loaded.session_id == session_id

    def test_path_cache_is_not_shared_between_workspaces(
        self, store, sample_session, tmp_path, monkeypatch
    ):
        """E: a path cached in one workspace is never served to another."""
        monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path / "vault"))
        session_id = sample_session.session_id
        store.save_session(sample_session, workspace_id="ws-b")

        # Resolve in the owning workspace first, populating the path cache.
        assert store.load_session(session_id, workspace_id="ws-b") is not None
        assert any(
            key[1] == session_id for key in store._cached_paths
        ), "the ws-b load should have populated the path cache"

        # Asked within the same TTL window, a different workspace must not be
        # served the cached ws-b path.
        assert store.load_session(session_id, workspace_id="ws-a") is None

        # The unscoped lookup keeps its historical cross-workspace scan.
        assert store.load_session(session_id) is not None

    def test_size_bytes_and_path_are_workspace_scoped(
        self, store, sample_session, tmp_path, monkeypatch
    ):
        """F: get_session_size_bytes / get_session_path honour workspace_id."""
        monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path / "vault"))
        session_id = sample_session.session_id
        store.save_session(sample_session, workspace_id="ws-b")
        ws_b_path = store._find_session_path(session_id, workspace_id="ws-b")
        assert ws_b_path is not None

        # Stamp a size into the owner's file so the lookup has something to find.
        data = json.loads(ws_b_path.read_text())
        data["session_size_bytes"] = 4242
        ws_b_path.write_text(json.dumps(data))

        assert store.get_session_size_bytes(session_id, workspace_id="ws-b") == 4242
        assert store.get_session_size_bytes(session_id, workspace_id="ws-a") is None

        assert store.get_session_path(session_id, workspace_id="ws-b") == ws_b_path
        ws_a_path = store.get_session_path(session_id, workspace_id="ws-a")
        assert "ws-b" not in str(ws_a_path)

    def test_save_into_other_workspace_does_not_move_file(
        self, store, sample_session, tmp_path, monkeypatch
    ):
        """D3: saving into another workspace must not MOVE the owner's file."""
        monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path / "vault"))
        session_id = sample_session.session_id
        store.save_session(sample_session, workspace_id="ws-b")
        ws_b_path = store._find_session_path(session_id, workspace_id="ws-b")
        assert ws_b_path is not None and ws_b_path.exists()

        # Re-save the same session under a DIFFERENT workspace.
        store.save_session(sample_session, workspace_id="ws-a")

        # ws-b's copy must survive: it was never this writer's to move.
        assert ws_b_path.exists(), "saving into ws-a moved ws-b's session file"
        assert store.load_session(session_id, workspace_id="ws-b") is not None
        # And the ws-a write landed in ws-a.
        assert store._find_session_path(session_id, workspace_id="ws-a") is not None

    def test_re_save_into_other_workspace_keeps_single_copy(
        self, store, sample_session, tmp_path, monkeypatch
    ):
        """D4: a re-save into another workspace never duplicates the record.

        A persisted session's ``workspace_id`` is immutable, so the second save
        cannot honour the new workspace.  It must keep updating the owner's own
        file: writing a second copy into the requested workspace would both
        duplicate the session and mislabel it, because the payload would still
        carry the old workspace_id.  (The unowned/legacy case — no recorded
        workspace_id at all — is D3 above.)
        """
        monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path / "vault"))
        sample_session.workspace_id = "ws-a"
        session_id = sample_session.session_id
        store.save_session(sample_session, workspace_id="ws-a")
        ws_a_path = store._find_session_path(session_id, workspace_id="ws-a")
        assert ws_a_path is not None, "the first save did not land in ws-a"

        # Re-save the same session into a DIFFERENT workspace.
        store.save_session(sample_session, workspace_id="ws-b")

        # Exactly ONE copy of the session exists across every workspace dir.
        copies = []
        for ws_sessions in (tmp_path / "vault" / "workspaces").glob("*/sessions"):
            for path in ws_sessions.glob("*.json"):
                if path.name.startswith("_meta_"):
                    continue
                if json.loads(path.read_text()).get("session_id") == session_id:
                    copies.append((ws_sessions.parent.name, path))
        assert len(copies) == 1, (
            f"expected exactly one copy of the session, found {copies}"
        )

        # ... it lives in the owning workspace, and its payload agrees with it.
        owner, path = copies[0]
        assert owner == "ws-a", f"the single copy landed in {owner!r}, not the owner's ws-a"
        payload_workspace_id = json.loads(path.read_text()).get("workspace_id")
        assert payload_workspace_id == owner, (
            f"payload workspace_id {payload_workspace_id!r} disagrees with its "
            f"directory {owner!r}"
        )
        assert store._find_session_path(session_id, workspace_id="ws-a") == path

    def test_legacy_resident_session_readable_but_never_leaks_another_workspace(
        self, store, sample_session, tmp_path, monkeypatch
    ):
        """D (compat): the legacy dir is workspace-agnostic, not a leak channel.

        Records written with no workspace_id land in the legacy sessions_dir
        (that is what ``SessionManager.create_session`` produces).  They belong
        to no workspace at all, so a workspace-scoped read must still resolve
        them -- otherwise every REST-created session becomes unreadable through
        its own workspace.

        That widening must NOT reopen the cross-workspace leak: a ws-b-owned
        record stays invisible under ws-a even while the legacy dir is
        non-empty.  This half is the regression guard for the widened
        candidate list in ``_find_session_path``.
        """
        from session.models import Session

        monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path / "vault"))

        # (i) A legacy-resident record (no workspace_id) is readable through a
        # scoped call, in ANY workspace, because it belongs to none.
        legacy_session = Session()
        legacy_session.metadata['name'] = 'Legacy Session'
        legacy_session.add_message('user', 'Hello')
        store.save_session(legacy_session)  # no workspace_id -> legacy sessions_dir
        legacy_id = legacy_session.session_id
        legacy_path = store._find_session_path(legacy_id)
        assert legacy_path is not None
        assert legacy_path.parent == store.sessions_dir

        for ws in ("ws-a", "ws-b"):
            loaded = store.load_session(legacy_id, workspace_id=ws)
            assert loaded is not None, (
                f"legacy-resident session must remain readable under {ws}"
            )
            assert loaded.session_id == legacy_id

        # (ii) A ws-b-owned record is still invisible to ws-a, even though the
        # legacy dir now holds a real session file.
        store.save_session(sample_session, workspace_id="ws-b")
        owned_id = sample_session.session_id
        ws_b_path = store._find_session_path(owned_id, workspace_id="ws-b")
        assert ws_b_path is not None
        assert "ws-b" in str(ws_b_path)

        assert store.load_session(owned_id, workspace_id="ws-a") is None, (
            "widening the candidate list reopened the cross-workspace leak"
        )
        assert store.load_session(owned_id, workspace_id="ws-b") is not None

