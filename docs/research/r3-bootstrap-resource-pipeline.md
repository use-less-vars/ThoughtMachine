# R3: Bootstrap & Resource Deployment Pipeline

## Overview

Documents how ThoughtMachine initialises the user's `~/.thoughtmachine/` directory on first run, deploys default resources from the bundled `resources/` directory, and manages version-driven synchronisation of the global knowledge base.

---

## 1. Entry Point: `bootstrap.py`

**File:** `thoughtmachine/bootstrap.py`

### `ensure_user_defaults(overwrite_existing=False)` — the main workflow

```
ensure_user_defaults()
  │
  ├── 1. mkdir ~/.thoughtmachine/ (parents=True, exist_ok=True)
  │
  ├── 2. mkdir ~/.thoughtmachine/sessions/
  ├── 3. mkdir ~/.thoughtmachine/state/
  ├── 4. mkdir ~/.thoughtmachine/knowledge/
  ├── 5. mkdir ~/.thoughtmachine/worker_templates/
  │
  ├── 6. Load resources/MANIFEST.json
  │
  ├── 7. Deploy individual files from manifest["files"]
  │     │  For each entry:
  │     │    - Skip if entry["internal"] == true (e.g., .version)
  │     │    - Skip if dest exists AND overwrite_existing == False
  │     │    - Otherwise: shutil.copy2(source, dest)
  │     │    - Append dest path to touched list
  │
  ├── 8. Deploy directories from manifest["directories"]
  │     │  For each entry:
  │     │    - If condition == "dest_empty":
  │     │        Skip if destination directory has any contents
  │     │    - mkdir destination
  │     │    - shutil.copy2 each file from source to destination
  │     │    - Append each copied file to touched list
  │
  └── 9. Call ensure_global_kb()
            (handles the global knowledge base separately)
```

### Public API

| Function | Returns | Description |
|----------|---------|-------------|
| `get_version()` | `str` | Reads `resources/.version` (ThoughtMachine version) |
| `get_manifest()` | `dict` | Returns full manifest JSON (for introspection by agent/UI) |
| `ensure_user_defaults(overwrite_existing)` | `list[str]` | Paths created/overwritten |
| `load_user_config()` | `dict` | Load `~/.thoughtmachine/agent_config.json`, fallback to `resources/default_config.json` |

### Resource loading utilities

| Function | Purpose |
|----------|---------|
| `_resources_dir()` | Locates the `resources/` directory using `importlib.resources.files("thoughtmachine") → parent / "resources"` |
| `_load_manifest()` | Reads and parses `resources/MANIFEST.json` |
| `_read_default(name)` | Reads bundled resource file as text |
| `_read_default_json(name)` | Reads bundled JSON resource |

---

## 2. Manifest: `resources/MANIFEST.json`

**Version:** `"1"`

### Deployed files

| Source (`resources/`) | Dest (`~/.thoughtmachine/`) | Notes |
|----------------------|----------------------------|-------|
| `default_system_prompt.txt` | `default_system_prompt.txt` | Default agent-mode system prompt |
| `default_config.json` | `agent_config.json` | User config overlay (diff from factory) |
| `default_providers.json` | `providers.json` | LLM provider profile definitions |
| `.version` | `.version` | **internal** — not deployed to user dir |
| `engineer_system_prompt.txt` | `engineer_system_prompt.txt` | Engineer/architect role system prompt |

### Deployed directories

| Source | Dest | Condition | Notes |
|--------|------|-----------|-------|
| `worker_templates/` | `worker_templates/` | `dest_empty` | Only deployed if destination is empty (no overwrites) |

### Worker template files

**Source:** `resources/worker_templates/`

| File | Role |
|------|------|
| `coder.json` | Code writing/modification specialist (temp 0.2, write filesystem + docker execution) |
| `reviewer.json` | Code review specialist (temp 0.1, read-only filesystem) |
| `researcher.json` | Codebase research specialist (temp 0.3, read-only + KB access) |
| `default.json` | General-purpose worker (no restrictions) |

### Manifest notes (from the JSON)

> "Global knowledge base (resources/global_kb/*.md → ~/.thoughtmachine/knowledge/system/) is handled separately by `agent/knowledge/global_kb.py::ensure_global_kb()`"
>
> "User subdirectory structure (~/.thoughtmachine/sessions/, state/, knowledge/) is created by `bootstrap.py::ensure_user_defaults()`"

---

## 3. Global Knowledge Base: `agent/knowledge/global_kb.py`

### File structure

```
~/.thoughtmachine/knowledge/
├── .version              # Currently deployed version marker (content like "1.2.0")
├── system/               # Read-only system files synced from package data
│   ├── architecture.md
│   ├── capabilities.md
│   ├── capabilities_reference.md
│   ├── configuration.md
│   ├── faq.md
│   ├── handbook.md
│   ├── onboarding_guide.md
│   ├── overview.md
│   ├── security_architecture.md
│   └── troubleshooting.md
└── user/                 # Writable user area
    └── my_notes.md       # Created if missing
```

### `ensure_global_kb(version_file=None)` workflow

```
ensure_global_kb()
  │
  ├── 1. Read current version from resources/global_kb/.version
  │       → "1.2.0" (the version of the KB content, NOT ThoughtMachine version)
  │
  ├── 2. Read stored version from ~/.thoughtmachine/knowledge/.version
  │       → None if file missing
  │
  ├── 3. If stored_version != current_version OR system/ missing:
  │     │
  │     ├── a. mkdir system/ (parents=True, exist_ok=True)
  │     │
  │     ├── b. Copy all *.md from resources/global_kb/ → system/
  │     │     (logs each copy, warns on failure)
  │     │
  │     ├── c. REMOVE stale .md files in system/ that no longer exist in resources
  │     │     (logs each removal, warns on failure)
  │     │
  │     └── d. Write current version to .version marker
  │           (logs on failure)
  │
  └── 4. Ensure user/ subdirectory exists
        │
        └── If my_notes.md missing:
              Create template file with header and placeholder
```

### Two separate version strings

| File | Version | Scope |
|------|---------|-------|
| `resources/.version` | `0.1.0` | ThoughtMachine overall version |
| `resources/global_kb/.version` | `1.2.0` | Global KB content version |

The global KB has its **own versioning** because its content changes independently of the main application (new handbook sections, updated architecture docs, etc.).

---

## 4. Migration / Version-Check Logic

### No explicit migration framework

There is **no general migration system** in ThoughtMachine. The bootstrap simply checks:

1. **Does the file exist?** → If yes, skip (unless `overwrite_existing=True`)
2. **Is the directory empty?** → If yes, deploy (only for `dest_empty` condition)
3. **Has the KB version changed?** → If yes, full resync with stale file removal

### What this means for upgrades

| Scenario | Behaviour |
|----------|-----------|
| Fresh install | All files deployed, KB synced |
| Upgrade (files unchanged) | User files untouched (existing check), KB resyncs if version changed |
| Upgrade (file changed) | User's copy NOT overwritten (no version check on individual files) |
| Factory reset | Use `ensure_user_defaults(overwrite_existing=True)` |

### KB version-driven resync advantages

- Adding a new .md file to the KB: all users get it on next startup (deployed fresh)
- Removing a .md file: stale file cleaned up (only on version bump)
- Changing content: deploy only when KB version changes (avoids unnecessary I/O)

### Potential gap

Individual resource files (like `default_config.json`) don't get updated on version changes — the user's overlay file is never overwritten. This means:
- If a new config key is added to `default_config.json`, existing users won't get it unless they delete their `agent_config.json`
- There's no "merge new defaults into existing user config" logic

---

## 5. Complete Bootstrap Call Graph

```
Application startup
  │
  └── bootstrap.ensure_user_defaults()
        │
        ├── Create ~/.thoughtmachine/ + subdirs
        │
        ├── Load MANIFEST.json
        │
        ├── For each file in manifest["files"]:
        │     Skip if internal or dest exists
        │     Otherwise: copy2(source, dest)
        │
        ├── For each dir in manifest["directories"]:
        │     Skip if condition=="dest_empty" and dest has files
        │     Otherwise: copy2 each file
        │
        └── global_kb.ensure_global_kb()
              │
              ├── Read resources/global_kb/.version → "1.2.0"
              ├── Read ~/.thoughtmachine/knowledge/.version → stored
              │
              ├── If version mismatch or system/ missing:
              │     Copy all *.md files (add new, remove stale)
              │     Write .version marker
              │
              └── Ensure user/ subdirectory
                    Create my_notes.md if missing
```

---

## 6. Key Files Reference

| File | Role |
|------|------|
| `thoughtmachine/bootstrap.py` | Entry point, manifest loading, file/directory deployment |
| `resources/MANIFEST.json` | Single source of truth for what gets deployed |
| `resources/.version` | ThoughtMachine package version (`0.1.0`) |
| `resources/default_config.json` | Factory-default agent configuration |
| `resources/default_providers.json` | LLM provider definitions |
| `resources/default_system_prompt.txt` | Agent-mode system prompt |
| `resources/engineer_system_prompt.txt` | Engineer-mode system prompt |
| `resources/worker_templates/*.json` | Worker role definitions (coder, reviewer, researcher, default) |
| `agent/knowledge/global_kb.py` | Global KB syncing with version-driven resync |
| `resources/global_kb/.version` | KB content version (`1.2.0`) |
| `resources/global_kb/*.md` | Bundled KB system files |

---

## 7. Observations & Potential Improvements

1. **No per-file versioning**: Individual resource files (config, prompts) don't have version metadata. If a resource changes in a future release, existing users won't get the update unless they delete their user copy or pass `overwrite_existing=True`.

2. **Config merge is one-way**: `load_user_config()` returns the user's overlay on top of factory defaults, but there's no mechanism to auto-merge new default keys into existing user configs.

3. **KB resync is version-gated**: The KB version check is the only version-driven update mechanism in the system. This works well because it's self-contained (all .md files in one directory).

4. **Worker templates deploy only on empty dir**: After initial deployment, new worker templates won't be deployed even if they're added to `resources/worker_templates/` in a future release. A user would need to manually empty `~/.thoughtmachine/worker_templates/` to trigger re-deployment.
