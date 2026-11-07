#!/usr/bin/env python3
"""Vault migration script — migrates ~/.thoughtmachine to version 2 layout."""

import hashlib
import json
import os
import shutil
import sys
import tarfile
from datetime import datetime
from pathlib import Path

# Try to import from thoughtmachine.vault, fallback to hardcoded constants
try:
    from thoughtmachine.vault import vault_root, VAULT_SUBDIRS
except ImportError:
    def vault_root():
        return Path.home() / ".thoughtmachine"
    VAULT_SUBDIRS = ("credentials", "knowledge", "sessions", "state", "system", "user", "worker_templates", "workspaces")


def migrate_vault(dry_run=False):
    """Migrate the vault to version 2 layout. Returns 0 on success, 1 on error."""
    vault = vault_root()
    
    # Step 1: Idempotency check
    version_file = vault / "system" / ".vault_version"
    if version_file.exists():
        current_version = version_file.read_text().strip()
        if current_version == "2":
            print("Vault already at version 2.")
            return 0
    
    # Check for partial migration
    new_dirs = [vault / d for d in ("system", "user", "global")]
    existing_new = [d for d in new_dirs if d.exists()]
    if existing_new and not version_file.exists():
        print("ERROR: Partial migration detected — some version 2 directories exist but version marker is missing.")
        print("Manual recovery from backup is advised.")
        return 1
    
    # Step 2: Backup
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    backup_name = f".thoughtmachine.backup.{timestamp}"
    backup_path = vault.parent / backup_name
    
    print(f"Creating backup at {backup_path}.tar.gz...")
    try:
        archive_path = shutil.make_archive(
            str(backup_path),
            "gztar",
            root_dir=vault.parent,
            base_dir=vault.name
        )
        print(f"Backup created: {archive_path}")
    except Exception as e:
        print(f"ERROR: Backup failed: {e}")
        return 1
    
    # Step 3: Secret scan
    config_file = vault / "agent_config.json"
    if config_file.exists():
        try:
            config_data = json.loads(config_file.read_text())
            secret_keys = [k for k in config_data.keys() 
                          if any(sub in k.lower() for sub in ("api_key", "token", "secret", "password"))]
            if secret_keys:
                print("ERROR: agent_config.json contains sensitive keys that must be moved to credentials/:")
                for k in secret_keys:
                    print(f"  - {k}")
                print("Move these secrets to the credential manager before migrating.")
                return 1
        except (json.JSONDecodeError, OSError) as e:
            print(f"ERROR: Could not read agent_config.json: {e}")
            return 1
    
    if dry_run:
        print("Dry-run: no changes made.")
        return 0
    
    try:
        # Step 4: Create directory structure
        dirs_to_create = ["system", "user", "credentials", "workspaces", "global"]
        for d in dirs_to_create:
            (vault / d).mkdir(mode=0o700, parents=True, exist_ok=True)
        
        # Step 5: Migrate files
        
        # --- Workspace config handling ---
        workspaces_dir = vault / "workspaces"
        workspace_ids = []
        if workspaces_dir.exists():
            workspace_ids = [p.name for p in workspaces_dir.iterdir() if p.is_dir()]
        
        # Also check workspace_registry.json
        registry_file = vault / "workspace_registry.json"
        if registry_file.exists():
            try:
                registry = json.loads(registry_file.read_text())
                if isinstance(registry, dict):
                    for wid in registry:
                        if wid not in workspace_ids:
                            workspace_ids.append(wid)
            except (json.JSONDecodeError, OSError):
                pass
        
        for wid in workspace_ids:
            ws_dir = workspaces_dir / wid
            ws_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            
            # Rename config.json -> defaults.json
            old_config = ws_dir / "config.json"
            new_defaults = ws_dir / "defaults.json"
            if old_config.exists():
                if new_defaults.exists():
                    # Merge: existing defaults.json overrides
                    try:
                        old_data = json.loads(old_config.read_text())
                        new_data = json.loads(new_defaults.read_text())
                        merged = {**old_data, **new_data}
                        new_defaults.write_text(json.dumps(merged, indent=2))
                        print(f"WARNING: Merged {wid}/config.json into {wid}/defaults.json (existing values win)")
                    except (json.JSONDecodeError, OSError):
                        print(f"WARNING: Could not merge {wid}/config.json into defaults.json, skipping")
                    old_config.unlink()
                else:
                    old_config.rename(new_defaults)
                    print(f"Migrated {wid}/config.json -> {wid}/defaults.json")
            
            # Ensure Dockerfile exists
            ws_dockerfile = ws_dir / "Dockerfile"
            root_dockerfile = vault / "Dockerfile"
            if not ws_dockerfile.exists() and root_dockerfile.exists():
                shutil.copy2(root_dockerfile, ws_dockerfile)
                print(f"Copied Dockerfile to {wid}/")
        
        # --- Worker templates ---
        global_templates = vault / "worker_templates"
        if global_templates.exists() and global_templates.is_dir():
            for wid in workspace_ids:
                ws_templates = workspaces_dir / wid / "worker_templates"
                if not ws_templates.exists():
                    shutil.copytree(str(global_templates), str(ws_templates))
                    print(f"Copied worker_templates to {wid}/")
            
            # Move originals to _legacy
            legacy_templates = vault / "_legacy_global_worker_templates"
            if legacy_templates.exists():
                shutil.rmtree(str(legacy_templates))
            global_templates.rename(legacy_templates)
            print(f"Moved global worker_templates -> _legacy_global_worker_templates")
        
        # --- Knowledge ---
        old_knowledge = vault / "knowledge"
        new_knowledge = vault / "global" / "knowledge"
        if old_knowledge.exists() and old_knowledge.is_dir():
            if new_knowledge.exists():
                # Merge contents
                for item in old_knowledge.iterdir():
                    dest = new_knowledge / item.name
                    if item.is_dir():
                        if not dest.exists():
                            shutil.copytree(str(item), str(dest))
                    else:
                        shutil.copy2(str(item), str(dest))
                shutil.rmtree(str(old_knowledge))
            else:
                old_knowledge.rename(new_knowledge)
            print(f"Migrated knowledge/ -> global/knowledge/")
        
        # --- Unrecognized files at vault root ---
        expected_names = {
            "system", "user", "credentials", "workspaces", "global",
            "knowledge", "sessions", "state", "worker_templates",
            "_legacy_global_worker_templates", "_migrated_legacy",
            "agent_config.json", "workspace_registry.json",
        }
        legacy_dir = vault / "_migrated_legacy"
        for item in vault.iterdir():
            if item.name.startswith(".thoughtmachine.backup"):
                continue  # Skip backup files
            if item.name.startswith("_"):
                continue  # Skip already-underscored dirs
            if item.name in expected_names:
                continue
            # Move unrecognized items
            legacy_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            dest = legacy_dir / item.name
            shutil.move(str(item), str(dest))
            print(f"WARNING: Moved unrecognized '{item.name}' -> _migrated_legacy/")
        
        # Step 6: Generate system files
        allowlist_data = {
            "version": 1,
            "allowlist": [
                "capabilities", "container_status", "dockerfile",
                "effective_permissions", "event_bus_status", "event_log",
                "mcp_servers", "my_config", "network_diagnostics",
                "running_workers", "workers", "workspace_info"
            ]
        }
        system_dir = vault / "system"
        system_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        
        allowlist_path = system_dir / "checksystem_allowlist.json"
        allowlist_path.write_text(json.dumps(allowlist_data, indent=2))
        allowlist_path.chmod(0o644)
        
        # Compute SHA-256
        sha256_hash = hashlib.sha256(allowlist_path.read_bytes()).hexdigest()
        sha256_path = system_dir / "checksystem_allowlist.sha256"
        sha256_path.write_text(sha256_hash + "\n")
        sha256_path.chmod(0o644)
        
        # Write version marker
        version_path = system_dir / ".vault_version"
        version_path.write_text("2\n")
        version_path.chmod(0o644)
        
        # Step 7: Validation
        issues = []
        
        if not (system_dir / "factory_defaults.json").exists():
            issues.append("system/factory_defaults.json is missing — run ensure_vault_defaults()")
        if not allowlist_path.exists():
            issues.append("system/checksystem_allowlist.json is missing")
        if not sha256_path.exists():
            issues.append("system/checksystem_allowlist.sha256 is missing")
        
        for wid in workspace_ids:
            ws_dir = workspaces_dir / wid
            if not (ws_dir / "Dockerfile").exists():
                issues.append(f"{wid}/Dockerfile is missing")
        
        # Check for unexpected files at vault root
        for item in vault.iterdir():
            if item.name in expected_names or item.name.startswith("."):
                continue
            issues.append(f"Unexpected item at vault root: {item.name}")
        
        if issues:
            print("\nValidation issues:")
            for issue in issues:
                print(f"  - {issue}")
        else:
            print("\nValidation passed.")
        
        print(f"\nMigration to version 2 complete.")
        return 0
        
    except Exception as e:
        print(f"\nERROR: Migration failed: {e}")
        print("Rolling back from backup...")
        try:
            if vault.exists():
                shutil.rmtree(str(vault))
            archive_to_restore = str(backup_path) + ".tar.gz"
            if os.path.exists(archive_to_restore):
                with tarfile.open(archive_to_restore, "r:gz") as tar:
                    tar.extractall(path=str(vault.parent))
                print("Rollback complete: vault restored from backup.")
        except Exception as restore_error:
            print(f"CRITICAL: Rollback failed: {restore_error}")
            print(f"Manual restore required from: {backup_path}.tar.gz")
        return 1


def main():
    """CLI entry point."""
    dry_run = "--dry-run" in sys.argv
    if "--help" in sys.argv or "-h" in sys.argv:
        print("Usage: python migrate_vault.py [--dry-run]")
        print("Migrate ~/.thoughtmachine vault to version 2 layout.")
        return 0
    return migrate_vault(dry_run=dry_run)


if __name__ == "__main__":
    sys.exit(main())
