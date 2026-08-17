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


class TestSaveDefaultsAtomicity:
    """Verify save_config_defaults writes atomically (temp + os.replace)."""

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

    @staticmethod
    def _leftovers(directory):
        """Files in *directory* other than the destination defaults.json."""
        return [p.name for p in directory.iterdir() if p.name != "defaults.json"]

    def test_atomic_write_clean_no_temp_litter_mode_600(self, hermetic_vault):
        payload = {"temperature": 0.5, "max_turns": 50}
        path = save_config_defaults(payload, "ws-1", global_scope=True)
        assert path == hermetic_vault / "user" / "defaults.json"
        # No temp files left behind in the destination directory
        assert self._leftovers(path.parent) == []
        # Temp file was chmod 0o600 before the rename; mode survives
        assert (path.stat().st_mode & 0o777) == 0o600
        assert json.loads(path.read_text(encoding="utf-8")) == payload

    def test_replace_failure_preserves_original_and_no_temp_litter(
        self, hermetic_vault, monkeypatch
    ):
        target = hermetic_vault / "user" / "defaults.json"
        original = {"temperature": 0.9, "note": "original"}
        target.write_text(json.dumps(original), encoding="utf-8")

        def boom(src, dst):
            raise OSError("simulated replace failure")

        monkeypatch.setattr("agent.config.config_manager.os.replace", boom)
        with pytest.raises(OSError):
            save_config_defaults(
                {"temperature": 0.1}, "ws-1", global_scope=True
            )
        # Original content preserved and no temp litter on failure
        assert json.loads(target.read_text(encoding="utf-8")) == original
        assert self._leftovers(target.parent) == []

    def test_both_scopes_atomic_mode_600(self, hermetic_vault):
        ws_payload = {"temperature": 0.3}
        global_payload = {"max_turns": 100}
        ws_path = save_config_defaults(
            ws_payload, "ws-1", global_scope=False
        )
        global_path = save_config_defaults(
            global_payload, "ws-1", global_scope=True
        )
        assert (ws_path.stat().st_mode & 0o777) == 0o600
        assert (global_path.stat().st_mode & 0o777) == 0o600
        assert json.loads(ws_path.read_text(encoding="utf-8")) == ws_payload
        assert json.loads(global_path.read_text(encoding="utf-8")) == global_payload
        assert self._leftovers(ws_path.parent) == []
        assert self._leftovers(global_path.parent) == []
