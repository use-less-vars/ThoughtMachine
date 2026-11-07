# Config Ownership Model

Status: **Phase 1 (Path A)** — `set_default_config` now persists into the
global-default layer only.

This document defines **who owns which configuration values** and **where they
are persisted**. It supersedes the implicit single-file model where the Web UI
wrote the whole config into `~/.thoughtmachine/agent_config.json`.

## The four layers

| Layer | Owned by | Path | JSON keys (flat dict) | Writer |
|---|---|---|---|---|
| **Session-local** | Web UI session (bridge) | in-memory `SessionConfig` + persisted session JSON (`~/.thoughtmachine/sessions/` or `~/.thoughtmachine/workspaces/<ws>/sessions/`) | full config incl. `mode`, `enabled_tools`, `session_permissions`, `api_key` (never persisted), … | `web_ui/backend/bridge.py` (apply-config path) |
| **Workspace default** | Workspace (`workspace_id`) | `~/.thoughtmachine/workspaces/<ws>/defaults.json` | flat config dict (any keys the workspace layer wants to override) | `agent.config.config_manager.save_config_defaults(..., global_scope=False)` |
| **Global default** | User (all workspaces) | `~/.thoughtmachine/user/defaults.json` | **only** `GLOBAL_DEFAULT_KEYS`: `provider_id`, `model`, `base_url`, `temperature`, `max_turns`, `system_prompt` | `server.save_global_defaults()` → `agent.config.config_manager.save_config_defaults(..., global_scope=True)` |
| **System-owned** | Installation (immutable) | `~/.thoughtmachine/system/factory_defaults.json` (+ `~/.thoughtmachine/resources/` prompt files) | `{ "version": …, "config": { max_turns, temperature, provider_id, model, system_prompt, … } }` | installer / factory seeding |

Precedence (lowest → highest): **system-owned → global default → workspace
default → session-local**.

## `GLOBAL_DEFAULT_KEYS`

Defined in `web_ui/backend/config_manager.py` (~line 90):

```python
GLOBAL_DEFAULT_KEYS = frozenset({
    "provider_id", "model", "base_url", "temperature",
    "max_turns", "system_prompt",
})
```

This is the **only** set of keys the Web UI may persist into
`~/.thoughtmachine/user/defaults.json`. Everything else in a frontend config
payload (`mode`, `enabled_tools`, `workspace_path`, `workspace_id`,
`session_permissions`, …) is session-local or workspace-scoped and must never
leak into the global defaults file.

## Path A: how `set_default_config` persists

The WebSocket handler `set_default_config` in `web_ui/backend/server.py`
(≈line 1179) delegates to `server.save_global_defaults(cfg_dict)` (≈line 164):

1. **Subset** — keep only keys in `GLOBAL_DEFAULT_KEYS`.
2. **Merge** — load the existing `~/.thoughtmachine/user/defaults.json`
   (preserving unrelated keys) and overlay the subset on top.
3. **Delegate** — call the canonical writer
   `agent.config.config_manager.save_config_defaults(merged, workspace_id,
   global_scope=True)`, which writes `~/.thoughtmachine/user/defaults.json`
   and returns the path.

### Why Path A (canonical writer + merge in server.py)

- There is exactly **one** writer for the global-default file
  (`agent.config.config_manager.save_config_defaults`), so `bridge.py`,
  `config_routes.py`, and the WebSocket handler cannot diverge in path
  resolution or serialization format.
- `save_config_defaults` **does not merge** — it overwrites. The merge is done
  by `save_global_defaults` in server.py *before* delegating, so unrelated keys
  already present in `user/defaults.json` survive a save.
- **Atomic writes (canonical writer):** `save_config_defaults` writes
  atomically: it serializes the JSON payload to a temp file **in the same
  directory** as the destination (`NamedTemporaryFile` with prefix
  `.{dst.name}.` and suffix `.tmp`), flushes and `os.fsync`s the temp file,
  `chmod`s it to `0o600` **before** the rename (the mode survives
  `os.replace`), then `os.replace`s it onto the destination — atomic on both
  POSIX and Windows. On failure the temp file is unlinked and the previous
  destination content is preserved; there is **no retry loop** (unlike
  `atomic_replace` in `web_ui/backend/config_manager.py`, which adds retries
  and a `shutil.move` fallback for Windows sharing violations). Readers (e.g.
  `resolve_config_defaults`) therefore never observe a partially-written
  defaults file, and the file never exists with a more permissive mode than
  `0o600`.

### `workspace_id` passthrough

`save_global_defaults` extracts `workspace_id` from the payload purely to
satisfy the canonical writer's signature (it is required positionally); with
`global_scope=True` the workspace id is **not** used in the destination path
and the key is **not** persisted into `user/defaults.json`.

## Live callers of `save_config_defaults` / `resolve_config_defaults` (audit)

Both functions are **live** production code (not dead code):

- `agent/presenter/state_bridge.py:228-229` — imports
  `resolve_config_defaults` and calls it with the resolved workspace id to
  layer vault defaults into new session configs.
- Test coverage: `tests/integration/test_defaults_resolution.py`,
  `tests/integration/test_vault_hardening_end_to_end.py`,
  `tests/integration/test_first_query_fresh_vault.py`, and
  `tests/integration/test_save_defaults.py` (including the atomic-write
  tests for the canonical writer).

## `agent_config.json` — deprecated, read-compat only

`~/.thoughtmachine/agent_config.json` is **no longer written by the Web UI
server**. It remains readable for backwards compatibility:

| Reader | Location | Notes |
|---|---|---|
| `web_ui/backend/bridge.py` | `_build_global_agent_config` ≈ lines 962–968 | builds `AgentConfig` from `agent_config.json` via `ConfigService`, mirroring the PyQt GUI path |
| `web_ui/backend/server.py` | `start_session` comment ≈ line 680 | comment text still says “Global config from agent_config.json provides defaults” — left as read-compat note |
| `web_ui/backend/config_routes.py` | `_CONFIG_PATH` ≈ line 24 | `/api/config` reset/read paths still target `agent_config.json` |
| `agent/presenter/state_bridge.py` | `save_config` ≈ lines 83–162 | legacy PyQt-side writer: writes a minimal diff overlay to `agent_config.json` and extracts `system_prompt` to `custom_system_prompt.txt` |

Phase 1 deliberately **does not** migrate these readers; they are documented
here so a later phase can retire the file.

## Default-drift reconciliation (documented, not changed)

Several sources of “defaults” exist and are intentionally **not** reconciled in
Phase 1:

- `agent/config/default_config.json` (repo resource used by `AgentConfig`)
- `~/.thoughtmachine/system/factory_defaults.json` (vault factory layer)
- `web_ui/backend/config_manager.py::FALLBACK_FRONTEND_CONFIG` (Web UI fallback)
- `agent/config/session_config.py::SessionConfig` field defaults
  (`temperature=0.7`, `max_turns=None` → constructor fallback `100`)

Unifying these is a separate task; Phase 1 only fixes *where the Web UI
persists* a saved “global default”.

## `system_prompt` semantics

A `system_prompt` saved in `user/defaults.json` is applied to new sessions via
`SessionManager.create_session` **only when the session mode is `custom`**.
For `agent`/`engineer` modes the mode resource prompt
(`~/.thoughtmachine/resources/…` / `custom_system_prompt.txt` precedence in
`agent/config/models.py::load_default_system_prompt` + `_apply_mode_system_prompt`)
wins — mode-locked behaviour is by design and out of scope here.

## Session application of global defaults

`SessionManager.create_session` (web_ui/backend/session_manager.py ≈ line 64):

1. Builds a minimal `SessionConfig` for the requested mode
   (`max_turns=100`, empty `provider_id`/`model`/`base_url`).
2. Loads `~/.thoughtmachine/user/defaults.json` and applies **only**
   `GLOBAL_DEFAULT_KEYS` (truthy values via `setattr`).
3. Persists `metadata["session_config"]` via
   `model_dump(exclude={"api_key"}, exclude_none=True)` — so `system_prompt`
   is absent from metadata when no default is set.
