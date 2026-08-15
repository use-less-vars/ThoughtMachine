# Resource Execution Contract

**Status:** Normative contract for the agent workspace resource-execution model.
**Scope:** branch `feat/workspace-panel`, HEAD `b23c26b` (2026-08-15).
**Companion:** `docs/git_resource_execution_audit.md` (line-level evidence for every item below).

This document defines *what must be true* about how agent git/resource execution
works. Sections marked **ASPIRATIONAL — NOT IMPLEMENTED** are contract targets that
do not exist in the codebase yet; their current status is noted briefly. All other
sections are implemented and verified against the tree at `b23c26b`.

## 1. Workspace ownership

- The workspace belongs to the agent. The agent may read and modify everything
  inside the workspace: code, docs, configuration, and hooks.
- In the resource container the workspace is mounted at `/workspace` read-write
  (`infra/resource_container_manager.py` L513-519).
- File-tool path validation confines the agent to the workspace (`tools/base.py`
  `_validate_path` L323-350, delegating to `security_validate_path`).

## 2. Workspace hooks are agent-editable

- The agent may create/edit workspace (repo-local) hooks, e.g. `.git/hooks/pre-commit`.
- `.git/hooks` is deliberately **not** in the blocked-path list. Only
  `.git/config`, `.git/HEAD`, `.ssh`, `.npmrc`, `.env` are blocked
  (`thoughtmachine/security.py` `WORKSPACE_BLOCKED_PATH_PREFIXES` L77-83).
- The design note (`thoughtmachine/security.py` L70-76) and the `validate_path`
  docstring example (L291-292, `.git/hooks/pre-commit` as an unblocked path) make
  this explicit.

## 3. Workspace hooks run ONLY inside the resource container

- When git runs in the container, repo-local `.git/hooks` scripts **are allowed to
  run**: no `--no-verify`, no `core.hooksPath` override — "the resource container
  IS the security boundary" (`tools/git_info_tool.py` `_exec_container_raw` L451-506,
  comment L486-488).
- The host fallback **disables** workspace hooks entirely (see §4).
- Scope note: this guarantee covers git execution mediated by ThoughtMachine's git
  tooling. Git invoked directly on the host by a human operator is outside the
  tool's control.
- Host-side extension point (operator-owned, not agent-owned): the vault hook
  `~/.thoughtmachine/hooks/<workspace_id>/pre-commit` runs on the host before
  commits (`tools/git_info_tool.py` `_run_vault_hooks` L675-731, `_git_commit`
  L904-907). Only `pre-commit` is honored.

## 4. Host fallback git

- Host fallback runs git on the host **without Docker** and works even when the
  resource container is unavailable.
- It disables workspace hooks:
  - hardened `-c` args incl. `core.hooksPath=/dev/null`, `core.attributesFile=/dev/null`,
    `diff.external=`, `core.fsmonitor=`, `filter.clean=`, `filter.smudge=`,
    `diff.textconv=`, `credential.helper=` (`tools/git_info_tool.py` L406-415);
  - commits add `--no-verify` (L416-419);
  - `GIT_CONFIG_SYSTEM=/dev/null` + `GIT_PAGER=cat` (L444-447).
- Current status: the host path is what non-registry/direct callers use, and what
  `git_execution_mode='host'` selects. There is **no automatic fallback on
  container failure** — container errors propagate (`_run_git_raw` L382-395 has no
  try/except around the container branch). This is deliberate and matches the
  contract: fallback is mode-selected, not error-triggered.

## 5. Host preflight script — ASPIRATIONAL — NOT IMPLEMENTED

- A fixed preflight script, located **outside the workspace**, checks the host
  environment (e.g. git availability, test environment) before host-fallback
  operations.
- The preflight script is **NOT agent-editable** — it is operator-owned host code.
- The agent may not create, edit, or replace it through any tool.
- **Current status:** no preflight mechanism exists anywhere in the codebase
  (`'preflight'` → 0 matches in `tools/`, `infra/`, `agent/`, `thoughtmachine/`).
  Nothing currently gates host-fallback git beyond the permission layer (§9).

## 6. pytest-list file — ASPIRATIONAL — NOT IMPLEMENTED

- When implemented, the host preflight executes a **fixed script** that runs only
  the tests listed in a **validated pytest-list file**.
- Validation rules (contract target): the file must parse, every path must resolve
  inside the allowed test directories, and no shell metacharacters or command
  injection forms may pass.
- The agent **MAY edit the pytest-list file** (choosing which tests run) but **NOT**
  the preflight script (how they run).
- **Current status:** no `pytest-list` / `test-list` mechanism exists (0 matches).
  Today's pre-commit gating is the repo-owned `.githooks/pre-commit` (operator-wired,
  runs via `core.hooksPath` host-side) and the vault pre-commit hook — neither is
  agent-configurable beyond the repo's own hook contents.

## 7. Resource execution modes

- Every workspace has a resource execution mode:
  - `containerized` — git and resource commands run inside the per-workspace
    resource container (`infra/resource_container_manager.py`; `tools/git_info_tool.py`
    `_exec_container_raw` L451-506).
  - `host_fallback` — git runs on the host with workspace hooks disabled and no
    Docker dependency (§4).
  - `unavailable` — neither path is usable; the mode is reported rather than
    silently degrading.
- Mode resolution today: `_git_execution_mode` = `agent_config['git_execution_mode']`
  → workspace metadata → default `'container'` (`tools/git_info_tool.py` L528-540);
  `_use_container_mode` additionally requires a registry-derived workspace id+path
  (L567-580).
- **Current status:** the labels `containerized` / `host_fallback` / `unavailable`
  are **NOT IMPLEMENTED** as a mode enum or UI labels. `'unavailable'` exists only
  as an ad-hoc container-status string in CheckSystem
  (`tools/workspace/check_system.py` L301, L310, L324, L329, L334). These labels are
  the contract target.

## 8. UI and CheckSystem must show the mode

- The UI and `CheckSystem` **MUST** surface the workspace resource execution mode so
  the operator/agent can see which path is in effect.
- **Current status:** CheckSystem reports container status (including
  `"status": "unavailable"` strings) but exposes **no unified resource-mode label**.
  Phase 7 per-workspace container lifecycle API endpoints exist
  (`web_ui/backend/server.py` L2447, L2561-2562, L2585-2586) and are the natural
  place to carry the mode. Mode display is a contract target.

## 9. Agent-editable vs operator-editable (summary)

- **Agent-editable:** everything inside the workspace, including workspace hooks (§2);
  the pytest-list file once implemented (§6).
- **Operator-editable only (never the agent):** vault hooks
  (`~/.thoughtmachine/hooks/<workspace_id>/`), the host preflight script (§5),
  resource-container configuration, and host git hardening parameters.
- Host-side execution is hermetic (`SandboxedExecution`), so the agent cannot reach
  host code even indirectly.
