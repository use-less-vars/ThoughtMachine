# Realm 2 Investigation — Supplementary Section

> **Note:** This is Section 10 (Supplementary Investigation) of the full Realm 2 Investigation report. Sections 1–9 were delivered in the original conversation and can be re-extracted from the conversation history.

---

## 10. Supplementary Investigation

This section covers five additional gaps identified during the investigation that extend beyond the nine previously documented sections.

### 10.1 Workspace Folder Naming: Deterministic vs. Random

**Finding: Two competing workspace ID generation schemes exist.**

| Scheme | Algorithm | Used By | Characteristic |
|--------|-----------|---------|----------------|
| **Deterministic hash** | `sha256(path).hexdigest()[:16]` | `setup_workspace.py`, `thoughtmachine.workspace_capabilities` | Same path → same ID |
| **Random UUID** | `uuid.uuid4().hex` (32 chars) | `server.py` auto-registration fallback | Every call → new ID |

**The `setup_workspace.py` flow:**
- Computes `ws_id = hashlib.sha256(PROJECT_ROOT.encode()).hexdigest()[:16]`
- Creates `~/.thoughtmachine/workspaces/<ws_id>/config.json` with `{"root": PROJECT_ROOT, "capabilities": {}}`
- Delegates to `ensure_workspace_dirs(ws_id)` to create the 5 standard workspace files

**The `server.py` fallback flow (`_resolve_workspace_id` returns None):**
- Generates `new_ws_id = uuid.uuid4().hex` (random, 32 chars)
- Creates `~/.thoughtmachine/workspaces/<uuid>/config.json` with `{"root": str(_project_path)}`
- Calls `ensure_workspace_dirs(new_ws_id)`

**No registry file exists.** The mapping from path to ID is derived entirely from scanning `~/.thoughtmachine/workspaces/<id>/config.json` entries at runtime. The primary resolver (`_resolve_workspace_id` in `bridge.py`) normalises paths before comparison, so repeated calls for the same project path should return the same ID — but only if `setup_workspace.py` ran first (creating the deterministic ID). If only the fallback ran, each new session could theoretically create a new UUID-based workspace for the same path.

**Impact:** On a fresh install where `setup_workspace.py` hasn't run, the web UI's auto-registration creates a random-UUID workspace. If the user later runs `setup_workspace.py`, a *different* deterministic-ID workspace is created. Two workspaces now point to the same project root with different IDs, potentially causing session confusion.

---

### 10.2 Worker vs. Main Agent Configuration

**Finding: Workers are NOT `AgentConfig` subclasses — they use separate model classes with runtime override inheritance.**

The project does **not** have an `agent/worker.py` file. Instead:

**Agent Configuration (`AgentConfig`):**
- Defined in `agent/config/models.py` (Pydantic model)
- Has `worker_mode: bool` field (default `False`)
- `FIELD_CATEGORIES` classify fields as `RESTART_REQUIRED`, `HOT_SWAPPABLE`, or `GLOBAL_STATIC`
- System prompt loading: `custom_system_prompt.txt` → explicit field → `resources/default_system_prompt.txt`
- Single model used by both main agent and workers

**Worker Configuration (`WorkerDefinition`):**
- Defined in `agent/models/worker_definition.py` (Pydantic model)
- Represents a reusable **sub-agent** template: `system_prompt`, `enabled_tools`, `blocklist_tools`, `permissions`, `timeout_seconds`, `max_turns`, `allowed_domains`
- NOT a config — it is a *definition* that gets applied at spawn time

**Worker spawn flow (at runtime):**
1. `AgentConfig` is cloned from parent (inherits provider, model, API key, base URL)
2. WorkerDefinition fields override the clone: system_prompt (defaults to `DEFAULT_WORKER_SYSTEM_PROMPT`), tools (intersection of enabled_list minus blocklist), permissions (restrictive merge)
3. Hard-forced overrides: `time_monitor_enabled=True`, `time_warning_threshold=80% of timeout`, `worker_mode=True`

**Key distinction:** Workers share the same `AgentConfig` class as the main agent but with overrides applied programmatically at spawn time. There is no separate config class or config file for workers — their configuration lives in `workers.json` (WorkerDefinition entries) combined with runtime overrides.

---

### 10.3 "Save as Default" vs. Mode Toggle: System Prompt File Contention

**Finding: A critical overwrite condition exists between two features that both write to `~/.thoughtmachine/custom_system_prompt.txt`.**

**Path A: "Save as Default" (`save_config()` in `state_bridge.py`)**
- Extracts `system_prompt` from config dict via `.pop('system_prompt', None)`
- If prompt matches factory default → **deletes** `custom_system_prompt.txt`
- If prompt is non-empty & non-default → **writes** to `custom_system_prompt.txt`
- If prompt is empty/None → **deletes** `custom_system_prompt.txt`

**Path B: Mode Toggle (`POST /api/config/mode` in `config_routes.py`)**
- `"engineer"` mode: Copies `engineer_system_prompt.txt` → `custom_system_prompt.txt`
- `"full"` mode: Deletes `custom_system_prompt.txt` (so validator falls through to factory default)

**The Conflict Scenario:**

1. User is in **engineer mode** → `custom_system_prompt.txt` contains the engineer prompt
2. User clicks **"Save as Default"** in Config Panel → `save_config()` extracts the *current config's* system prompt and writes it to `custom_system_prompt.txt`
3. If the current config's prompt differs from the engineer prompt, the engineer prompt is **silently overwritten**
4. Mode detection (`_is_engineer_mode()`) still returns True (file exists with content), but the content is now the user's saved prompt, not the engineer prompt

**Reverse scenario:**
1. User saves defaults with a custom system prompt → `custom_system_prompt.txt` written
2. User toggles to **engineer mode** → engineer prompt overwrites the saved prompt
3. User toggles to **full mode** → `custom_system_prompt.txt` deleted; the saved prompt is lost

**Root cause:** Both features use `custom_system_prompt.txt` as their persistence target without coordination. The config save assumes it "owns" the file; the mode toggle also assumes it "owns" the file. There is no mutual exclusion, no lock, and no metadata flag indicating which source is authoritative.

---

### 10.4 Workspace Functionality Checklist: Minimum Requirements

**Finding: The WorkspacePanel is highly resilient — it renders in 4 independent sections, each with graceful degradation.**

**The 4 workspace API calls that must respond 200 for full render:**

| # | Endpoint | Component | Failure Mode |
|---|----------|-----------|-------------|
| 1 | `GET /api/workspace/{id}/dockerfile` | DockerfileEditor | Shows error + retry button |
| 2 | `GET /api/workspace/{id}/domain_allowlist` | DomainAllowlistEditor | Shows error + retry button |
| 3 | `GET /api/workspace/{id}/workers` | WorkerManagementPanel | Empty worker list (silent) |
| 4 | `GET /api/workspace/{id}/effective_permissions?session_id=...` | EffectivePermissionsSection | Shows "Failed to load" (red text) |

**Required files on disk (`~/.thoughtmachine/workspaces/{id}/`):**

| File | Purpose | Auto-created? |
|------|---------|---------------|
| `config.json` | Contains `{"root": "/path/to/project"}` | Yes (by `ensure_workspace_dirs()`) |
| `capabilities.json` | Workspace capabilities definition | Yes (fully permissive default) |
| `Dockerfile` | Container build instructions | Yes (copied from default template) |
| `domain_allowlist.json` | Allowed external domains | Yes (empty `[]` default) |
| `workers.json` | Worker definitions | Yes (template workers auto-created) |
| `mcp_servers.json` | MCP server configurations | Yes (empty `[]` default) |
| `workers/` (directory) | Runtime worker state files | Yes (created by runtime) |

**Key observations:**
- All 5 config files are auto-created by `ensure_workspace_dirs()` — no setup script needed
- The only hard preconditions: FastAPI backend running, workspace directory accessible, session store responding
- Worker runtime status files (`status.json`, `command.json`, `context.json`) are created asynchronously — not required for render
- The `effective_permissions` endpoint has a 3-tier fallback: (1) security_gate module merge, (2) manual merge if module unavailable, (3) restrictive defaults if no session found

---

### 10.5 Migration Impact on "Save as Default"

**Finding: The "Save as Default" flow is entirely local — it persists the current session's config as the user-level default. No migration path exists.**

**Current behavior:**
- `save_config()` computes a diff vs. factory defaults and writes only the overlay to `~/.thoughtmachine/agent_config.json`
- The system prompt is handled separately (see §10.3)
- The overlay is loaded on every `AgentConfig` construction, merged with factory defaults
- There is no versioning, no migration mechanism, and no conflict resolution for stale overlays

**Migration gaps:**
- If a future update changes factory defaults, the overlay diff may reference keys that no longer exist or have different semantics
- If the project path changes (e.g., repository moved), the deterministic workspace ID changes (see §10.1), creating an orphaned workspace
- If `engineer_system_prompt.txt` is updated (new version), users in "engineer mode" keep the old prompt until they toggle mode again (because the file was copied at toggle time, not symlinked)

**Recommendations:**

1. **Unify workspace ID schemes** — Use deterministic hashing everywhere (eliminate the UUID fallback) to ensure path-to-ID consistency
2. **Decouple prompt saving from mode** — `save_config()` should skip writing `custom_system_prompt.txt` when engineer mode is active (defer to the mode toggle), OR track the authoritative source via a metadata flag
3. **Add config versioning** — Include a `_config_version` field in the overlay JSON for migration support
4. **Symlink engineer prompt** — Instead of copying `engineer_system_prompt.txt` to `custom_system_prompt.txt`, use a symlink so updates to the engineer prompt are live
5. **Add factory reset scoping** — Allow resetting config without affecting mode, and vice versa

---
