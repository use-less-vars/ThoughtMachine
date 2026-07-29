"""Integration tests for save_config_defaults."""

import json

import pytest

from agent.config.config_manager import save_config_defaults


class TestSaveDefaults:
    """Verify save-as-default writes to correct vault locations."""

    @pytest.fixture
    def hermetic_vault(self, tmp_path, monkeypatch):
        vault_root = tmp_path / ".thoughtmachine"
        (vault_root / "user").mkdir(parents=True)
        (vault_root / "workspaces" / "ws-1").mkdir(parents=True)
        monkeypatch.setattr(
            "agent.config.config_manager._vault_root",
            lambda: vault_root,
        )
        yield vault_root

    def test_save_workspace_scope(self, hermetic_vault):
        path = save_config_defaults(
            {"temperature": 0.5}, "ws-1", global_scope=False
        )
        expected = hermetic_vault / "workspaces" / "ws-1" / "defaults.json"
        assert path == expected
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["temperature"] == 0.5

    def test_save_global_scope(self, hermetic_vault):
        path = save_config_defaults(
            {"max_turns": 100}, "ws-1", global_scope=True
        )
        expected = hermetic_vault / "user" / "defaults.json"
        assert path == expected
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["max_turns"] == 100

    def test_workspace_root_no_file_created(self, hermetic_vault):
        """Verify the workspace root is clean."""
        save_config_defaults({"temperature": 0.3}, "ws-1", global_scope=False)
        # No file should appear in the project root
        assert not (hermetic_vault.parent / "defaults.json").exists()
