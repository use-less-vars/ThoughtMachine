# Ticket: bug/ws-set-project-bypasses-vault-confinement

**DO NOT FIX IN THIS WAVE.** Filed as a pointer only.

**Filed:** 2026-10-21, noticed while verifying the WebSocket command surface claims at `9d5ed869`.
**Severity:** HIGH — the one documented "TRUST ANCHOR" invariant in the server is enforced on the REST path and skipped on the WebSocket path, and the WebSocket path is unauthenticated.
**Status:** open; verified by read. `set_project` itself is UI-dead — the live entry point is `apply_config`, which has the same defect and *less* validation.

## Finding

`_confine_to_home` states the invariant in its own docstring (`web_ui/backend/server.py:3075-3106`): "TRUST ANCHOR: `~/.thoughtmachine` holds the workspace registry, credentials, config and session state, so user-reachable endpoints (browse, create, container lifecycle) must never resolve INTO it." The REST routes obey it — `workspace_routes.py:145` and `:1725` both do `confined = _confine_to_home(body.path)` and only then call `registry.register_by_root(confined)` (`:157`, `:1738`).

The WebSocket connection does not. `set_project` (`web_ui/backend/server.py:2706`) validates the incoming path with exactly one check, at `:2723`: `if not os.path.isdir(new_project):` — no vault check, no confinement call — then assigns `_project_path = new_project` (`:2739`) and, when the path has no workspace yet, `WorkspaceRegistry.get_default().register_by_root(str(_project_path))` (`:2766`) followed by `ensure_workspace_dirs(entry.id)` (`:2769`). `register_by_root` does no confinement of its own (`thoughtmachine/workspace_registry.py:330-365`): it resolves, generates an id, derives a label, and writes the entry. So `{"command":"set_project","project":"/home/<user>/.thoughtmachine"}` is accepted in full: the vault is registered as a workspace, and its root becomes the session's workspace root.

Two corrections to the obvious version of this claim, both of which make the ticket *broader* rather than weaker. First, `set_project` is not what the UI sends — grepping `web_ui/frontend/src` for `set_project` returns nothing, and the code says so itself at `:1391-1393` ("the frontend no longer sends a separate set_project command; apply_config handles the full project switch internally"). The live equivalent is the `apply_config` workspace-switch branch: `raw_workspace_path = config.get("workspace_path")` at `:1404`, `os.path.normpath` at `:1405`, straight into `_project_path = new_workspace_path` at `:1455` and `register_by_root` at `:1482` — with no `isdir` check at all, let alone confinement. Third entry point: the connect-time `?project=` query parameter is taken verbatim at `:1131` with zero validation. Second, the vault does **not** fall open to file tools: `thoughtmachine/security.py:419-443` blocks any host path under `~/.thoughtmachine` regardless of workspace root, and re-checks after symlink resolution (`:539-553`). What actually breaks is everything downstream of "the workspace root is X": the registry write lands in the vault, `ensure_workspace_dirs` creates directories in it, and — because the workspace root is what gets mounted into the sandbox (`infra/containers/executor.py:659` sets `self.workspace_path`, mounts declared at `:1049`, destination `/workspace` per the integrity check at `:539`) — the vault becomes the container's `/workspace`. Nothing under `infra/containers/` mentions the vault or confines a mount source, so the host-path textual guard in `security.py` never sees in-container paths like `/workspace/credentials/...`. Confirming that leg needs one read of the mount assembly before it is quoted as a proven escape; it is filed here because the confinement that is supposed to make the question moot is simply absent.

## Evidence

- `web_ui/backend/server.py:3075-3106` — `_confine_to_home`, "TRUST ANCHOR", vault holds registry/credentials/config/session state.
- `web_ui/backend/server.py:2706`, `:2714`, `:2723`, `:2739`, `:2766`, `:2769` — `set_project`: `isdir` only, then `_project_path`, `register_by_root`, `ensure_workspace_dirs`. No `_confine_to_home` call anywhere in the block.
- `web_ui/backend/server.py:1404-1405`, `:1414-1416`, `:1455`, `:1482`, `:1486` — `apply_config` workspace switch: `normpath` only, then `_project_path`, `register_by_root`, `ensure_workspace_dirs`. No `isdir`, no confinement.
- `web_ui/backend/server.py:1131` — `_project_path = project or _project_root`: the `?project=` handshake parameter, unvalidated.
- `thoughtmachine/workspace_registry.py:330-365` — `register_by_root` performs no confinement; `:299-328` `register_workspace` persists via `self._save(raw)`.
- `web_ui/backend/workspace_routes.py:145,157,1725,1738` — the REST path that does it correctly, i.e. the precedent for the fix.
- `thoughtmachine/security.py:419-443` (and `:539-553` for symlinks) — the mitigating layer: any path under `~/.thoughtmachine` is blocked for file tools.
- `infra/containers/executor.py:539`, `:659`, `:1049` — workspace root is the mount source, destination `/workspace`; `infra/containers/` contains no vault reference.

## What this is not

- Not a claim that file tools can read vault credentials as a consequence. `validate_path` blocks that independently of workspace root. The exposure is registry/workspace-root state, directory creation inside the vault, and the container mount.
- Not a duplicate of `ticket-security-workspace-vault-isolation.md` (that one asks whether workspace A can read workspace B through `checkSystem`) nor of `ticket-bug-unauthenticated-routes-beyond-health.md` (that one is about the absence of auth; this is about a confinement check that exists, is documented, and is skipped on one entry point).
- Not a claim that `set_project` is reachable from the shipped frontend — it is not. `apply_config`'s `workspace_path` is, which is why this is filed as a class defect rather than a single dead branch.
- Not a claim that the container leg is proven end-to-end. It is filed as the reason the missing check matters, with the one remaining read named above.

## Disposition

Decide whether one confinement helper is the rule for *all* client-supplied workspace roots, or only for REST bodies. If it is the rule, the fix is a single `_confine_to_home` call at each of the three assignment sites (`:1131`, `:1455`, `:2739`) — with the `?project=` and `apply_config` sites needing the answer even more urgently than `set_project`, since neither checks `isdir`. If it is not the rule, then `_confine_to_home`'s docstring is the thing that is wrong, and the container mount source needs its own guard.

Cross-reference: F30, `ticket-bug-unauthenticated-routes-beyond-health.md`, `ticket-bug-ws-commands-unauthenticated.md`, `ticket-security-workspace-vault-isolation.md`.
