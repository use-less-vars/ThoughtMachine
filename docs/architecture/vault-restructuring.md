# Vault Restructuring — Design Document

## Overview

Restructure `~/.thoughtmachine/` into a secure vault with compartmentalized
subdirectories, an allowlist-enforced CheckSystem tool, and a factory-defaults
bootstrap mechanism.

## Phases

### Phase 1 — Vault Structure & validate_path() Hardening ✅ (Completed)

**Changes:**
- Created `thoughtmachine/vault.py` with `ensure_vault_structure()`, `vault_root()`, `ensure_vault_defaults()`
- Added `VAULT_SUBDIRS`: `credentials`, `knowledge`, `sessions`, `state`, `system`, `worker_templates`
- Hardened `validate_path()` in `security.py` to block `~/.thoughtmachine/credentials/`
- Created `resources/factory_defaults.json`
- Updated `bootstrap.py` to create vault compartments and deploy factory defaults
- Updated `resources/MANIFEST.json`

### Phase 2 — CheckSystem Allowlist & Enforcement 🔜 (Current)

**Changes:**
- Introduced `system/checksystem_allowlist.json` in vault — a JSON array of explicit
  filenames that CheckSystem is allowed to report or access
- Added `verify_allowlist_integrity()` and `get_checksystem_allowlist()` to `vault.py`
- Added SHA-256 integrity check for the allowlist file
- Added `allowlist` field to CheckSystem tool; any path not in the list is denied
- `agent_config.json` is NOT in the allowlist (Amendment 1)
- Wildcards are forbidden — explicit filenames only (Amendment 3)
- `worker_templates` is NOT in the allowlist (Amendment 4)

### Phase 3 — Startup Integrity Check (Planned)

Validate vault structure integrity at server startup.

### Phase 4 — Secret Scanning (Planned)

Scan for accidentally committed secrets.

## Amendments from Main Engineer Review

| # | Change | Status |
|---|--------|--------|
| 1 | agent_config.json NOT in allowlist | ✅ Applied Phase 2 |
| 2 | workspace_id from bridge, not agent query | ✅ Applied Phase 2 |
| 3 | No wildcards — explicit file list only | ✅ Applied Phase 2 |
| 4 | Remove worker_templates from allowlist | ✅ Applied Phase 2 |
| 5 | (Reserved) | — |

## Vault Directory Layout

```
~/.thoughtmachine/
├── credentials/       # API keys, secrets (blocked by validate_path)
├── knowledge/         # Knowledge base entries (global KB)
├── sessions/          # Session persistence storage
├── state/             # Runtime state
├── system/            # System-managed configs
│   ├── factory_defaults.json
│   └── checksystem_allowlist.json
└── worker_templates/  # Legacy worker template storage
```

## CheckSystem Allowlist

The allowlist lives at `~/.thoughtmachine/system/checksystem_allowlist.json`.
It is a JSON array of strings, each being an explicit filename (no wildcards).
Integrity is verified via SHA-256 hash stored within the file.

```json
{
  "version": 1,
  "allowlist": ["filename1.json", "filename2.json"],
  "sha256": "abc123..."
}
```

Any path that does not appear in the `allowlist` array is denied by CheckSystem.
