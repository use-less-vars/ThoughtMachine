# Parameter Ownership Map

Status: **Phase 1+2** — canonical map of *which configuration layer owns which
parameter*, including the worker/session boundary and the git grain split.

This document is the single source of truth for "who owns this config key".
It complements `docs/architecture/config_ownership.md` (persistence) and
`docs/research/r6-worker-config.md` (worker config history). The rules here are
enforced by the layer merge chain in `web_ui/backend/config_manager.py`
(`CONFIG_LAYER_ORDER` / `CONFIG_LAYER_OWNERSHIP`) and by the field-level
validation in `agent/config/models.py` (AgentConfig), `agent/config/session_config.py`
(SessionConfig) and `agent/models/worker_definition.py` (WorkerDefinition).

## Layer precedence (lowest → highest)

`CONFIG_LAYER_ORDER` in `web_ui/backend/config_manager.py`:

```
fallback < factory < global_defaults < agent_config < provider_profile
        < workspace_config < session_config < worker_overrides
```

| Layer | Owns | Never owns |
|---|---|---|
| `fallback` | frontend-shape base (`FALLBACK_FRONTEND_CONFIG`) | — |
| `factory` | installation base overrides (`factory_defaults.json`) | — |
| `global_defaults` | **only** `GLOBAL_DEFAULT_KEYS`: `provider_id`, `model`, `base_url`, `temperature`, `max_turns`, `system_prompt` | every other key |
| `agent_config` | legacy `AgentConfig` keys (read-compat only) | — |
| `provider_profile` | `provider_type`, `api_key`, `base_url`, `provider_config {timeout, max_retries}`, `default_model → model` | `temperature`, `max_turns`, `system_prompt` (global defaults) |
| `workspace_config` | any flat config keys (`workspaces/<id>/defaults.json`) | `provider_config` (provider-owned) |
| `session_config` | `mode`, `enabled_tools`, `session_permissions`, `workspace_path`, `provider_id`, `model`, `api_key`, `base_url`, `temperature`, `max_turns`, `timeout_seconds`, feature flags, `git_read`/`git_write` grains, `worker_timeout_seconds`, `worker_max_retries` | `provider_config {timeout, max_retries}` (provider-owned — session layer CANNOT override the provider profile) |
| `worker_overrides` | per-worker spawn overrides | — |

Because the session layer sits **after** the provider-profile layer, the
provider profile's `provider_config {timeout, max_retries}` survives
resolution: the session layer may *select* a provider via `provider_id`, but it
never overwrites the profile's timeout/retry policy.

## Parameter → owner table

| Parameter | Owner layer / model | Notes |
|---|---|---|
| `provider_type` | `provider_profile` | resolved by `ProviderManager().resolve_config` |
| `api_key` | `provider_profile` | never persisted to session JSON |
| `base_url` | `provider_profile` (or `GLOBAL_DEFAULT_KEYS` fallback) | |
| `provider_config.timeout` | `provider_profile` | session layer cannot override |
| `provider_config.max_retries` | `provider_profile` | session layer cannot override |
| `default_model` | `provider_profile` | folded to `model` when no explicit model |
| `model` | `global_defaults` / `session_config` (explicit) | `model_override` wins in `resolve_from_profile` |
| `temperature` | `global_defaults` / `session_config` | worker-scoped copy on `WorkerDefinition` |
| `max_turns` | `global_defaults` / `session_config` | |
| `system_prompt` | `global_defaults` / mode resource | mode-locked behaviour |
| `mode` | `session_config` | |
| `enabled_tools` | `session_config` | |
| `session_permissions` | `session_config` | contains `git_read`/`git_write` grains |
| `git_read` / `git_write` | `session_config` (session-permission grains) | folded into `session_permissions` by `SessionConfig.to_agent_config` |
| `workspace_path` | `session_config` | |
| `timeout_seconds` | `session_config` (agent soft budget) | `map_agent_soft_budget_seconds` pre-validator |
| `worker_timeout_seconds` | `session_config` | session-owned worker spawn budget; default `WORKER_TIMEOUT_SECONDS=600` |
| `worker_max_retries` | `session_config` | session-owned worker retry budget; default `WORKER_MAX_RETRIES=3` |
| `max_workers_per_session` | `session_config` | |
| `git_allow_worktree_commits` | **removed** (Phase 2) | migrated into `session_permissions['git_write']='write'` |

## Git permission grains

The single `git` permission is split into two grains:

- `git_read` — `Literal["banned", "ask", "read", "write"]`, default `read`.
- `git_write` — `Literal["banned", "ask", "read", "write"]`, default `banned`.

`SessionPermissions` carries both grains (`Optional`; `None` means "derive from
`git`"). `security_gate.get_effective_permissions` resolves them: an explicit
non-`None` session grain takes precedence, is merged with the workspace
`git_available` capability via `_min_permission`, and otherwise falls back to
`split_git_permission(git)`.

`GitWriteTool` gates every write on the *effective* `git_write` permission
(`write`/`full`), with `ask` deferred to the outer security gate and a
fallback to the raw `session_permissions` dict for direct callers.

## Worker parameters

Worker spawn parameters are owned by the **session** (the spawning agent), not
by the `WorkerDefinition`:

- `WorkerDefinition.timeout_seconds` may be `None` — meaning "inherit from the
  spawning session at spawn time".
- `WorkerThread` resolves the effective worker timeout with the chain:
  constructor `timeout_seconds` → `definition.timeout_seconds` →
  `agent_config['worker_timeout_seconds']` → nested
  `session_config.worker_timeout_seconds` → `WORKER_TIMEOUT_SECONDS`.
- `WorkerManager.request_worker` uses the thread's resolved
  `_timeout_seconds` when no explicit `timeout` argument is supplied, so the
  session-owned value propagates to `deliver_query_and_block`.
- There is **no silent fallback** for a missing session timeout beyond the
  explicit `WORKER_TIMEOUT_SECONDS` constant — a missing value is a
  configuration error surfaced by validation, not a guess.

## Frontend ↔ backend

`backend_to_frontend_config` converts backend keys to the frontend shape
(provider reverse map, `enabled_tools` → legacy `tools` names, session
permissions via `model_dump` or dict). The frontend never writes
`provider_config` into the session layer; provider timeout/retry values reach
the LLM client only through the provider-profile-owned `provider_config`.
