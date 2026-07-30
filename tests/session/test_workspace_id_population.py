"""
Tests for Session.workspace_id population at creation, immutability guard,
and backward compat migration on load.
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from session.models import Session


# ──────────────────────────────────────────────────────────────────────────────
# Test 1: Session creation populates workspace_id
# ──────────────────────────────────────────────────────────────────────────────

class TestSessionCreationPopulatesWorkspaceId:
    """Verify that Session.workspace_id is populated at creation time."""

    def test_session_creation_with_workspace_id(self):
        """Direct construction with workspace_id should set it."""
        session = Session(workspace_id="ws-abc-123")
        assert session.workspace_id == "ws-abc-123"

    def test_session_creation_without_workspace_id(self):
        """Leaving workspace_id as default should leave it None."""
        session = Session()
        assert session.workspace_id is None

    @patch("thoughtmachine.workspace_capabilities.resolve_workspace_id")
    def test_resolve_workspace_id_called_from_lifecycle(self, mock_resolve):
        """Simulate what session_lifecycle does: resolve ws_path → workspace_id and pass to Session."""
        mock_resolve.return_value = "ws-resolved-456"
        ws_path = "/home/user/project"

        from thoughtmachine.workspace_capabilities import resolve_workspace_id
        workspace_id = resolve_workspace_id(ws_path)
        session = Session(workspace_id=workspace_id)

        mock_resolve.assert_called_once_with(ws_path)
        assert session.workspace_id == "ws-resolved-456"

    @patch("thoughtmachine.workspace_capabilities.resolve_workspace_id")
    def test_resolve_failure_leaves_workspace_id_none(self, mock_resolve):
        """If resolve fails, workspace_id should be None (not crash)."""
        mock_resolve.side_effect = Exception("Vault not found")

        workspace_id = None
        try:
            from thoughtmachine.workspace_capabilities import resolve_workspace_id
            workspace_id = resolve_workspace_id("/nonexistent/path")
        except Exception:
            workspace_id = None

        session = Session(workspace_id=workspace_id)
        assert session.workspace_id is None


# ──────────────────────────────────────────────────────────────────────────────
# Test 2: workspace_id immutability guard
# ──────────────────────────────────────────────────────────────────────────────

class TestWorkspaceIdImmutability:
    """Verify that workspace_id cannot be changed once set."""

    def test_immutable_after_set(self):
        """Once workspace_id is set, changing it should raise AttributeError."""
        session = Session(workspace_id="ws-fixed")
        with pytest.raises(AttributeError, match="workspace_id is immutable"):
            session.workspace_id = "ws-different"

    def test_settable_from_none(self):
        """Setting workspace_id from None to a value should succeed."""
        session = Session()  # workspace_id defaults to None
        session.workspace_id = "ws-first"
        assert session.workspace_id == "ws-first"

    def test_setting_same_value_is_ok(self):
        """Setting workspace_id to its current value should not raise."""
        session = Session(workspace_id="ws-same")
        # Should not raise
        session.workspace_id = "ws-same"
        assert session.workspace_id == "ws-same"

    def test_none_to_none_is_ok(self):
        """Re-setting workspace_id to None when it's already None should not raise."""
        session = Session()  # workspace_id defaults to None
        session.workspace_id = None  # should not raise
        assert session.workspace_id is None


# ──────────────────────────────────────────────────────────────────────────────
# Test 3: Backward compat on load from persistable dict
# ──────────────────────────────────────────────────────────────────────────────

class TestLegacySessionBackwardCompat:
    """Verify that sessions loaded without workspace_id get it from metadata."""

    def test_legacy_session_gets_workspace_id_from_metadata(self):
        """When loading a session dict that has workspace_path in metadata but no
        workspace_id, the backward-compat logic should populate it."""
        from thoughtmachine.workspace_capabilities import resolve_workspace_id

        # Create a persistable dict like a legacy session would have
        data = {
            'session_id': 'legacy-session-1',
            'created_at': '2025-01-01T00:00:00',
            'updated_at': '2025-01-01T00:00:00',
            'user_history': [],
            'containers': [],
            'metadata': {
                'workspace_path': '/home/user/legacy-project',
            },
            'version': 1,
            'mode': 'agent',
            'last_active': '',
        }

        session = Session.from_persistable_dict(data)

        # workspace_id should be None because from_persistable_dict doesn't call
        # resolve_workspace_id — that's the store's job.
        # The store handles the backward-compat migration.
        assert session.workspace_id is None
        assert session.metadata.get('workspace_path') == '/home/user/legacy-project'

    def test_store_migration_populates_workspace_id(self, tmp_path):
        """Simulate what store.load_session does: after deserialization, resolve
        workspace_id from metadata and save back."""
        from session.store import FileSystemSessionStore

        store = FileSystemSessionStore(
            sessions_dir=str(tmp_path / "sessions"),
            state_dir=str(tmp_path / "state"),
            enable_session_history_pruning=False,
        )

        # Create a legacy session (workspace_id=None but workspace_path in metadata)
        session = Session(
            workspace_id=None,
            metadata={'workspace_path': '/tmp/some-project'},
        )
        session.metadata['name'] = 'Legacy Session'

        # Save it (so we can load it back)
        store.save_session(session)

        # Patch resolve_workspace_id to return a known value
        with patch("thoughtmachine.workspace_capabilities.resolve_workspace_id") as mock_resolve:
            mock_resolve.return_value = "ws-migrated-789"
            loaded = store.load_session(session.session_id)

            assert loaded is not None
            # Backward-compat should have populated workspace_id
            mock_resolve.assert_called_once_with('/tmp/some-project')
            assert loaded.workspace_id == "ws-migrated-789"

    def test_missing_workspace_path_leaves_id_none(self, tmp_path):
        """When metadata has no workspace_path, workspace_id should remain None."""
        from session.store import FileSystemSessionStore

        store = FileSystemSessionStore(
            sessions_dir=str(tmp_path / "sessions"),
            state_dir=str(tmp_path / "state"),
            enable_session_history_pruning=False,
        )

        # Session with no workspace_path at all
        session = Session(metadata={'name': 'No Path Session'})
        store.save_session(session)

        loaded = store.load_session(session.session_id)
        assert loaded is not None
        assert loaded.workspace_id is None
        assert loaded.metadata.get('workspace_path') is None

    def test_empty_workspace_path_leaves_id_none(self, tmp_path):
        """When workspace_path is empty string, workspace_id should remain None."""
        from session.store import FileSystemSessionStore

        store = FileSystemSessionStore(
            sessions_dir=str(tmp_path / "sessions"),
            state_dir=str(tmp_path / "state"),
            enable_session_history_pruning=False,
        )

        session = Session(
            workspace_id=None,
            metadata={'workspace_path': ''},
        )
        session.metadata['name'] = 'Empty Path Session'
        store.save_session(session)

        loaded = store.load_session(session.session_id)
        assert loaded is not None
        assert loaded.workspace_id is None


# ──────────────────────────────────────────────────────────────────────────────
# Test 4: Serialization round-trip preserves workspace_id
# ──────────────────────────────────────────────────────────────────────────────

class TestWorkspaceIdSerialization:
    """Verify workspace_id survives to_persistable_dict / from_persistable_dict round-trip."""

    def test_round_trip_preserves_workspace_id(self):
        """to_persistable_dict should include workspace_id, and from_persistable_dict should restore it."""
        original = Session(workspace_id="ws-roundtrip-999")
        original.metadata['name'] = 'Roundtrip Test'

        data = original.to_persistable_dict()
        assert data.get('workspace_id') == "ws-roundtrip-999"

        restored = Session.from_persistable_dict(data)
        assert restored.workspace_id == "ws-roundtrip-999"

    def test_round_trip_preserves_none_workspace_id(self):
        """When workspace_id is None, it should survive round-trip as None."""
        original = Session()  # workspace_id defaults to None
        original.metadata['name'] = 'None WorkspaceID Test'

        data = original.to_persistable_dict()
        assert data.get('workspace_id') is None

        restored = Session.from_persistable_dict(data)
        assert restored.workspace_id is None

    def test_update_from_persistable_dict_preserves_workspace_id(self):
        """update_from_persistable_dict should set workspace_id from the data."""
        session = Session(workspace_id="ws-original")
        data = {'workspace_id': 'ws-updated'}
        # Add minimal required fields
        data['created_at'] = '2025-01-01T00:00:00'
        data['updated_at'] = '2025-01-01T00:00:00'
        data['user_history'] = []
        data['containers'] = []

        # This should call __setattr__('workspace_id', 'ws-updated')
        # Since current is 'ws-original' and new is 'ws-updated', it should raise
        with pytest.raises(AttributeError, match="workspace_id is immutable"):
            session.update_from_persistable_dict(data)
