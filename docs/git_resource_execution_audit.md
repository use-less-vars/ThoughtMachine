# Git Resource Execution Audit

- **Date:** 2026-08-15
- **Branch:** `feat/workspace-panel`
- **HEAD:** `b23c26b` — "docs: add operational analysis + unknowns reports (Option 1 — .gitignore negation)"
- **Method:** static inspection of the working tree via file reads; branch/HEAD via host-side git; sandbox has no git binary. `docs/resource_execution_contract.md` is the normative companion.
- **Environment note:** `/workspace/.git` is an 84-byte gitfile pointing to a gitdir outside the sandbox mount (`/home/jojo/PycharmProjects/ThoughtMachine/.git/worktrees/ThoughtMachine-dev`), so the shared `.git/hooks` directory is **not readable from this environment** (item (d)).

Verdicts: **PASS** (implemented as specified), **PARTIAL** (implemented with gaps),
**NOT IMPLEMENTED** (no code), **NOT FOUND** (no matches).

---

## (a) Containerized git execution path — PASS

`tools/git_info_tool.py`:
- Dispatch: `_run_git_raw` L382-395 (container if `_use_container_mode` else host).
- Container runner: `_exec_container_raw` L451-506 — manager from `_ensure_resource_container` L461; git read/write gate via `check_atomic_operation` L470-484 (skipped when effective `git == 'ask'` to defer outward); **no `--no-verify`** — repo-local `.git/hooks` allowed inside the container, comment L486-488; `agent_config` git-environment dict merged L493-498; exec L500-505.
- Mode selection: `_git_execution_mode` L528-540 (agent_config → workspace metadata → default `'container'`); `_use_container_mode` L567-580 (mode ≠ `'host'` AND registry-derived workspace id + path).
- Resource container: `_ensure_resource_container` L582-610 — network_mode from `get_expected_container_config` L591-599; `ResourceContainerManager(workspace_id, workspace_path, network_mode)` L601-609.
- Path mapping: `_to_container_path` L612-623 / `_from_container_path` L625-633 (ValueError if outside workspace).
- Repo-root resolution inside container: `_git_repo_root` L508-526 (`.git` dir fast path L515-517, else `rev-parse --show-toplevel` L519-521, reverse-map L524-525).

## (b) Host fallback git path — PASS (with note)

`tools/git_info_tool.py`:
- Host runner: `_exec_host_raw` L397-449.
- Hardened args L406-415: `core.hooksPath=/dev/null`, `core.attributesFile=/dev/null`, `diff.external=`, `core.fsmonitor=`, `filter.clean=`, `filter.smudge=`, `diff.textconv=`, `credential.helper=`; commits add `--no-verify` L416-419.
- Hermetic env L444-447: `GIT_PAGER=cat`, `GIT_CONFIG_SYSTEM=/dev/null`.
- Permission gate: `required_category` L434-437 (`None` when `session_permissions is None` or `git == 'ask'` → defers outward); runs inside `SandboxedExecution` L421-448.
- Deprecated `workspace_path` fallback stays on the **host** path: comments L264-265, fallback L277-282.
- **Note (verified behavior, not a gap):** there is **no automatic try/except fallback** around container exec — container errors propagate. "Host fallback" = the mode-selected/direct-caller host path, which works without Docker and disables workspace hooks (item (c)).

## (c) Workspace hook execution: container allows, host disables — PASS (by design)

- Container path allows repo-local hooks: `tools/git_info_tool.py` L486-488 comment; `thoughtmachine/security.py` design note L70-76 (".git/hooks is deliberately NOT blocked: the resource container is the security boundary … the host fallback still neutralizes them via core.hooksPath=/dev/null").
- Host path disables them: `tools/git_info_tool.py` L406-415 (`core.hooksPath=/dev/null`) + L416-419 (`--no-verify` on commit).
- Commit flow: stage L896-898 → vault pre-commit hook L904-907 → `git commit` L909-910; repo-local hooks never consulted on the host path.

## (d) Shared `.git/hooks/pre-commit` — NOT READABLE from this environment (gitfile indirection); repo `.githooks/pre-commit` inspected

- `/workspace/.git` is an 84-byte gitfile → gitdir `/home/jojo/PycharmProjects/ThoughtMachine/.git/worktrees/ThoughtMachine-dev` (outside the sandbox mount) → `.git/hooks` cannot be listed/read here. Direct inspection verdict: **NOT READABLE** (infrastructure constraint, not a code defect).
- Repo-owned hook **`.githooks/pre-commit`** (tracked in workspace, readable): executable, 11,677 B, 279 lines. Content = "fast gate": (1) import `web_ui.backend.server`; (2) live health check on `127.0.0.1:<random-high-port>` via urllib, asserting `{"status":"ok"}` and `revision == git rev-parse HEAD`; (3) hermetic websocket smoke (starlette `TestClient`, temp HOME, patched `Path.home`, purged modules, `new_session(custom)` → `session_loaded`); (4) conditional pytest run of `tests/integration/test_session_creation_contract.py` only if steps 1-3 finish < 3 s, with `--ignore` for the RED test (exit 5 treated as SKIP). Python resolution `$PYTHON` → `.venv/bin/python` → `python3`; EXIT trap cleanup; `mktemp -d ${TMPDIR:-/tmp}/tm_precommit.XXXXXX`; `setsid` process-group kill. No `mcp_servers.json` creation anywhere.
- `.pre-commit-config.yaml` is **absent** from the repo (verified via glob). Whether `.githooks` actually fires on commit depends on the operator's host `core.hooksPath` wiring — unverifiable from the sandbox.

## (e) Vault hooks (`~/.thoughtmachine/hooks/<workspace_id>/`) — PASS but LATENT

- Implemented: `tools/git_info_tool.py` `_run_vault_hooks` L675-731 — docstring L676-688 (vault hooks are "the ONLY sanctioned extension point for policy injection"; repo-local `.git/hooks` never executed on the host); skip when no workspace_id L689-695; hook path `Path.home()/".thoughtmachine"/"hooks"/<workspace_id>/<hook_name>` L697-699; silent return if not a file L700-703; `SandboxedExecution` with `required_category="git:write"` L705-726 (L716-721) when `session_permissions is not None and git != 'ask'`; `RuntimeError` on non-zero exit L727-731. Only `pre-commit` is honored, in `_git_commit` L904-907.
- **LATENT:** nothing creates `~/.thoughtmachine/hooks/`. `thoughtmachine/vault.py` `ensure_vault_structure` L51-72 creates only `VAULT_SUBDIRS` L40-48 (`system,user,credentials,workspaces,global,state,logs`); `vault.py` contains zero occurrences of `hooks`. The hook directory is never created and no sample hook is installed → the mechanism is dormant until an operator creates the directory + hooks.

## (f) Linked-worktree / common git-dir resolution — PASS (empirical + code)

- Empirical: `/workspace/.git` is a gitfile (84 B) → `gitdir: /home/jojo/PycharmProjects/ThoughtMachine/.git/worktrees/ThoughtMachine-dev`; the main repo gitdir lives outside the sandbox mount.
- Code: `infra/resource_container_manager.py` `_resolve_worktree_main_repo` L258-~330 — reads the `.git` gitfile pointer L261-266 (else "Not a git repository"); resolves relative pointers against the gitfile's directory L307-310; `realpath` L310; requires `basename(dirname(gitdir)) == "worktrees"` L312-318; `main_root` = 3 parents up L320-321; main `.git` must be a dir with `HEAD/objects/refs` L322-325; refuses pointers into the vault, filesystem roots, or the workspace itself (comment L526-527).
- The main repo is bind-mounted **read-write at its original host path** inside the resource container L528-539, so git resolves the gitdir pointer; a `"common git dir"` is mentioned in comment L524-525. No `common_dir` string/flag exists anywhere (0 matches) — resolution is purely via the mounted main repo. Extra mounts documented in module docstring L41-52.

## (g) ResourceContainerManager mount/hardening — PASS

`infra/resource_container_manager.py` (note: the file is `infra/resource_container_manager.py`, 758 lines — `tools/resource_container_manager.py` does not exist):
- Workspace bind **read-write** L513-519 (deliberate divergence from the executor's tmpfs-shadowing of `/workspace/.git`, docstring L41-52).
- Linked-worktree main repo bind rw L528-539; project `.venv` bind **read-only** L545-554 (skip + warning L555-560) so hooks can run pytest with exact deps.
- tmpfs L561-566: `/tmp` rw,noexec,nosuid,size=64m; `/home/agent` rw,exec,size=256M,uid=1000,gid=1000. **No** `/workspace/.git` shadow — the real gitdir is required (gitfile path).
- Registry delegation L567-590 (extras passed as `mounts[1:]` with ro/rw modes); legacy create L591-616: `cap_drop=["ALL"]`, `security_opt no-new-privileges`, `oom_score_adj=500`, `read_only=True`, user `1000:1000`, mem/cpu quotas, `tail -f /dev/null`; image-missing wrapped with build instructions L610-615.
- Exec L640+: default timeout=30 s, raw argv (`exec_run(cmd=list)`, no `/bin/sh -c` wrapper), on timeout kill+remove+`TimeoutError`.
- Related (legacy executor manager `infra/container_manager.py`): container-limit error dict L444-446; network/workspace-mode compute L448-451; workspace bind ro/rw + tm-packages volume L537-549; `/workspace/.git` tmpfs **only when `.git` is a real dir** L534-535 (gitfile → no tmpfs).

## (h) Permission propagation into git execution — PASS

- Tool executor: `agent/core/tool_executor.py` L241-280 — `session_perms_obj` L242-244; gate path: `resolve_workspace_id` L249 → `get_workspace_capabilities` L250 → `get_effective_permissions` L251 → `check_required_categories` L253-263 (deny → error L264-265); injects `session_permissions` L269-272 and `effective_permissions` + `workspace_id` L274-279.
- Security gate: `security/security_gate.py` `get_effective_permissions` L109-151 (filesystem write→read downgrade L127-128; `container = session.container AND workspace.allow_docker` L135; `git = min(session.git, workspace.git_available)` L138); `check_atomic_operation` L286-321 (ASK → DENIED L315-320); `check_required_categories` L329+; `get_expected_container_config` L159+.
- GitInfoTool consumption: host `required_category` L434-437; container `check_atomic_operation` L470-484; vault hooks `git:write` L716-721.
- Capabilities storage: `thoughtmachine/workspace_capabilities.py` — defaults fully permissive L92-104; path `~/.thoughtmachine/workspaces/<id>/capabilities.json` L159-161; `load_workspace_capabilities` L164+.

## (i) Agent can edit workspace hooks — PASS (by design)

- `thoughtmachine/security.py` L70-76 (design note) and `WORKSPACE_BLOCKED_PATH_PREFIXES` L77-83 (`.git/config`, `.git/HEAD`, `.ssh`, `.npmrc`, `.env` — no `.git/hooks` entry); `validate_path` docstring L291-292 lists `.git/hooks/pre-commit` as an unblocked example.
- File tools enforce only workspace confinement + blocked prefixes: `tools/base.py` `_validate_path` L323-350 (registry workspace resolution L329-330, delegate to `security_validate_path` L333-349, containment fallback L350+).
- Vault remains blocked: `VAULT_BLOCKED_SUBDIRS` L50-61 (`credentials, system, global, user, state, sessions, workspaces, logs, worker_templates`); vault blocking in `validate_path` L361-380+; null-byte rejection L333-346.

## (j) Agent cannot affect host preflight or arbitrary host code — PASS / NOT IMPLEMENTED

- No preflight exists: `'preflight'` → 0 matches in `tools/`, `infra/`, `agent/`, `thoughtmachine/` (NOT IMPLEMENTED — aspirational, see contract §5).
- No pytest-list mechanism: `'pytest-list'` / `'test-list'` → 0 matches (NOT IMPLEMENTED — contract §6).
- Vault hooks are the only sanctioned host-side extension point and are operator-owned: `tools/git_info_tool.py` L676-688 docstring.
- Host execution is hermetic: git runs inside `SandboxedExecution` (`_exec_host_raw` L421-448), so the agent cannot reach host code or the host filesystem outside the sandbox; `required_category` gates every host invocation (L434-437).

---

## Summary table

| Item | Verdict |
|---|---|
| (a) Containerized git execution | PASS |
| (b) Host fallback git (hooks disabled, no Docker) | PASS |
| (c) Hook execution boundary (container allows / host disables) | PASS (by design) |
| (d) Shared `.git/hooks/pre-commit` | NOT READABLE from sandbox (gitfile); `.githooks/pre-commit` inspected (279 lines); `.pre-commit-config.yaml` absent |
| (e) Vault hooks | PASS but LATENT (no creator of `~/.thoughtmachine/hooks/`) |
| (f) Linked-worktree / common git-dir resolution | PASS |
| (g) ResourceContainerManager mounts/hardening | PASS |
| (h) Permission propagation | PASS |
| (i) Agent can edit workspace hooks | PASS (by design) |
| (j) Agent cannot affect host preflight / host code | PASS (no preflight exists → NOT IMPLEMENTED; vault hooks only sanctioned extension point; host runs hermetic) |
