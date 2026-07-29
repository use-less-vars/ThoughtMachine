#!/usr/bin/env python3
"""Integration tests for scripts/migrate_vault.py — vault migration to version 2 layout."""

import json
import os
import shutil
from pathlib import Path

import pytest

from scripts.migrate_vault import migrate_vault


# ── Helpers ────────────────────────────────────────────────────────────────

def _create_old_vault(vault: Path, workspaces=None, with_secrets=False, with_knowledge=True, with_templates=True):
    """Helper to set up a mock old-style vault for testing."""
    if workspaces is None:
        workspaces = ["ws1"]
    
    vault.mkdir(parents=True, exist_ok=True)
    
    # agent_config.json (no secrets unless requested)
    config_content = {"model": "gpt-4", "temperature": 0.7}
    if with_secrets:
        config_content["api_key"] = "sk-1234"
    (vault / "agent_config.json").write_text(json.dumps(config_content))
    
    # workspace_registry.json
    (vault / "workspace_registry.json").write_text(json.dumps({wid: {"name": wid} for wid in workspaces}))
    
    # Workspace dirs
    ws_dir = vault / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    for wid in workspaces:
        wd = ws_dir / wid
        wd.mkdir(parents=True, exist_ok=True)
        (wd / "Dockerfile").write_text(f"FROM ubuntu:22.04\n# {wid}")
        (wd / "config.json").write_text(json.dumps({"name": wid, "setting": "old"}))
    
    # Worker templates
    if with_templates:
        tmpl_dir = vault / "worker_templates"
        tmpl_dir.mkdir(parents=True, exist_ok=True)
        (tmpl_dir / "default.yaml").write_text("name: default")
    
    # Knowledge
    if with_knowledge:
        kb_dir = vault / "knowledge"
        kb_dir.mkdir(parents=True, exist_ok=True)
        (kb_dir / "notes.md").write_text("# KB notes")
    
    # Sessions and state (should be left alone)
    (vault / "sessions").mkdir(parents=True, exist_ok=True)
    (vault / "state").mkdir(parents=True, exist_ok=True)
    (vault / "sessions" / "session_1.json").write_text("{}")

    return vault


def _assert_migrated_structure(vault, workspaces=None):
    """Assert the vault has the expected version 2 layout."""
    if workspaces is None:
        workspaces = ["ws1"]
    
    # Version marker
    version_file = vault / "system" / ".vault_version"
    assert version_file.exists(), "Version marker missing"
    assert version_file.read_text().strip() == "2", "Version should be 2"
    
    # Allowlist
    assert (vault / "system" / "checksystem_allowlist.json").exists()
    assert (vault / "system" / "checksystem_allowlist.sha256").exists()
    
    # Verify allowlist hash
    import hashlib
    allowlist_data = (vault / "system" / "checksystem_allowlist.json").read_bytes()
    expected_hash = hashlib.sha256(allowlist_data).hexdigest()
    actual_hash = (vault / "system" / "checksystem_allowlist.sha256").read_text().strip()
    assert actual_hash == expected_hash, "SHA-256 mismatch"
    
    # Directory structure
    for d in ("system", "user", "credentials", "workspaces", "global"):
        assert (vault / d).is_dir(), f"{d}/ should be a directory"
    
    # Workspaces
    for wid in workspaces:
        ws_dir = vault / "workspaces" / wid
        assert ws_dir.is_dir(), f"workspaces/{wid}/ should exist"
        assert (ws_dir / "Dockerfile").exists(), f"workspaces/{wid}/Dockerfile should exist"
        assert (ws_dir / "defaults.json").exists(), f"workspaces/{wid}/defaults.json should exist"
        # config.json should NOT exist anymore
        assert not (ws_dir / "config.json").exists(), f"workspaces/{wid}/config.json should have been renamed"
    
    # Knowledge migrated
    assert (vault / "global" / "knowledge").is_dir(), "global/knowledge/ should exist"
    
    # Legacy worker templates
    assert (vault / "_legacy_global_worker_templates").is_dir(), "_legacy_global_worker_templates/ should exist"
    
    # Sessions/state preserved
    assert (vault / "sessions").is_dir(), "sessions/ should be preserved"
    assert (vault / "state").is_dir(), "state/ should be preserved"
    
    # agent_config.json preserved at root
    assert (vault / "agent_config.json").exists(), "agent_config.json should be preserved"


def _assert_old_structure(vault, workspaces=None):
    """Assert the vault still has its old structure (pre-migration)."""
    if workspaces is None:
        workspaces = ["ws1"]
    
    # Old structure markers
    assert (vault / "agent_config.json").exists()
    assert (vault / "workspace_registry.json").exists()
    for wid in workspaces:
        assert (vault / "workspaces" / wid / "config.json").exists()
        assert not (vault / "workspaces" / wid / "defaults.json").exists()
    # Version 2 markers should NOT exist
    assert not (vault / "system" / ".vault_version").exists()


# ── Fixture ────────────────────────────────────────────────────────────────

@pytest.fixture
def old_vault(tmp_path, monkeypatch):
    """Create an isolated old-style vault and patch Path.home()."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    vault = tmp_path / ".thoughtmachine"
    _create_old_vault(vault, workspaces=["ws1", "ws2"])
    return vault


# ── Tests ──────────────────────────────────────────────────────────────────

class TestVaultMigration:
    """Tests for vault migration from version 1 to version 2 layout."""

    def test_successful_migration(self, old_vault):
        """Full migration from old-style to version 2 layout."""
        result = migrate_vault(dry_run=False)
        assert result == 0, f"Migration failed with code {result}"
        _assert_migrated_structure(old_vault, workspaces=["ws1", "ws2"])
        
        # Check backup archive was created
        backups = list(old_vault.parent.glob(".thoughtmachine.backup.*.tar.gz"))
        assert len(backups) == 1, f"Expected 1 backup, found {len(backups)}"

    def test_idempotency(self, old_vault):
        """Running migration twice is safe and second run is no-op."""
        result1 = migrate_vault(dry_run=False)
        assert result1 == 0
        _assert_migrated_structure(old_vault, workspaces=["ws1", "ws2"])
        
        # Second run
        result2 = migrate_vault(dry_run=False)
        assert result2 == 0  # Should exit cleanly
        _assert_migrated_structure(old_vault, workspaces=["ws1", "ws2"])

    def test_secret_abort(self, tmp_path, monkeypatch):
        """Migration aborts if agent_config.json contains secrets."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        vault = tmp_path / ".thoughtmachine"
        _create_old_vault(vault, with_secrets=True)
        
        result = migrate_vault(dry_run=False)
        assert result == 1, f"Expected abort (code 1), got {result}"
        
        # Vault should remain in old structure
        _assert_old_structure(vault)

    def test_rollback_on_failure(self, old_vault):
        """If migration fails after backup, vault is restored from backup."""
        # Create a file where system/ should be — this will cause Step 4's mkdir
        # to fail AFTER the backup has been created, triggering rollback.
        (old_vault / "system").write_text("i am a file, not a dir")
        
        result = migrate_vault(dry_run=False)
        assert result == 1, f"Expected rollback (code 1), got {result}"
        
        # After rollback, vault should be restored from backup
        assert old_vault.exists(), "Vault should exist after rollback"
        # Key old-structure items should exist
        _assert_old_structure(old_vault, workspaces=["ws1", "ws2"])

    def test_noop_on_current_vault(self, old_vault):
        """Running on an already-migrated vault does nothing."""
        # First migration
        result1 = migrate_vault(dry_run=False)
        assert result1 == 0
        _assert_migrated_structure(old_vault, workspaces=["ws1", "ws2"])
        
        # Second migration should be no-op (detected via version marker)
        result2 = migrate_vault(dry_run=False)
        assert result2 == 0
        _assert_migrated_structure(old_vault, workspaces=["ws1", "ws2"])
