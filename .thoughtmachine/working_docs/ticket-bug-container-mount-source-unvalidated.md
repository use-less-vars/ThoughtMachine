# Ticket: bug/container-mount-source-unvalidated

**DO NOT FIX IN THIS WAVE.** Filed as a pointer only.

**Filed:** 2026-10-21, while verifying the container-mount leg of `ticket-bug-ws-set-project-bypasses-vault-confinement.md` on the working tree of `/home/jojo/PycharmProjects/ThoughtMachine-dev` (branch `feat/workspace-panel`, HEAD `b23c26b`).
**Severity:** HIGH — the confinement helper that exists *specifically* to stop "container lifecycle endpoints" from mounting "arbitrary host paths (e.g. /etc, another user's home, the vault, another workspace)" is reached only when the client supplies an explicit `workspace_path`; the resolution path the endpoints actually use returns a registry `root_path` unvalidated, and nothing below the server rechecks before `Mount(source=...)`.
**Status:** open; verified by read. No container was started and no daemon was touched — this is a static read of four hops.

## Finding

`web_ui/backend/server.py:3499-3547` defines `_validate_workspace_path`, and its docstring (`:3500-3513`) states the purpose without ambiguity: the check exists so that container lifecycle endpoints cannot mount arbitrary host paths — `/etc`, another user's home, the vault, another workspace. The implementation matches the stated intent: `_confine_to_home(...)` at `:3518`, `os.path.isdir` at `:3521`, and containment inside the *registered* root at `:3540-3541` (`_path_is_within(resolved, root)` → `WorkspacePathForbiddenError`).

The function is unreachable on the path that matters. `_resolve_workspace_path` (`:3550-3569`) branches once: **if the caller supplied a `workspace_path`, validate it** (`:3558-3559`); **otherwise look the workspace up in the registry and return `entry.root_path` verbatim** (`:3561-3569`). `_make_container_manager` (`:3572-3590`) then calls `_resolve_workspace_path` (`:3578`) and hands the result straight to `ContainerManager(workspace_path=path, ...)` (`:3583-3584`) with no further inspection. So the normal, frontend-driven, no-query-param request takes the *unvalidated* branch: the server's own registry is trusted as an input source, and the vault/arbitrary-host-path question is never asked of a value the client did not literally type.

Below that, nothing else asks either. `infra/containers/executor.py:659` normalises and stores — `self.workspace_path = os.path.abspath(workspace_path).rstrip('/')` — no policy; `:946-950` then binds it as the `/workspace` mount with `source=host_workspace`. The same shape repeats at `infra/container_manager.py:2182-2185` and `infra/resource_container_manager.py:1057-1061`: `Mount(target="/workspace", source=self.workspace_path, ...)`.

The net claim, stated narrowly: **the only confinement of a container mount source is a check that runs on client input, not on the value that reaches the mount.** A workspace registered with a root inside `~/.thoughtmachine` (or at `/etc`, or in another workspace) produces a container whose `/workspace` *is* that directory. The `workspace_path` query parameter is the one input that gets checked; the registry is the input that does not.

This is the consumption half of `ticket-bug-ws-set-project-bypasses-vault-confinement.md`, filed separately because it holds independently of how a bad root got registered — an operator can register one through the REST path, a registry file can be edited, and any future registration entry point inherits the same hole. Fixing the three WebSocket assignment sites closes the door this ticket watches; it does not close this one.

## Evidence

- `web_ui/backend/server.py:3499` — `def _validate_workspace_path(workspace_path: str, workspace_id: str) -> str:`
- `web_ui/backend/server.py:3500-3513` — docstring: the check exists so container lifecycle endpoints cannot "mount arbitrary host paths (e.g. /etc, another user's home, the vault, another workspace)".
- `web_ui/backend/server.py:3515-3516` — `if not workspace_path:` → `raise WorkspacePathError("workspace_path is required")`.
- `web_ui/backend/server.py:3518` — `resolved = _confine_to_home(workspace_path)`.
- `web_ui/backend/server.py:3521` — `if not os.path.isdir(resolved):`
- `web_ui/backend/server.py:3540-3542` — `root = os.path.realpath(entry.root_path)`; `if not _path_is_within(resolved, root):` → `raise WorkspacePathForbiddenError(`.
- `web_ui/backend/server.py:3547` — `return resolved`.
- `web_ui/backend/server.py:3550` — `def _resolve_workspace_path(workspace_id: str, workspace_path: str = ""):`
- `web_ui/backend/server.py:3558-3559` — `if workspace_path:` → `return _validate_workspace_path(workspace_path, workspace_id)`: the **only** caller of the validator.
- `web_ui/backend/server.py:3561` — `entry = WorkspaceRegistry.get_default().get_workspace(workspace_id)`.
- `web_ui/backend/server.py:3569` — `return entry.root_path if entry is not None else None`: unvalidated registry value.
- `web_ui/backend/server.py:3572` — `def _make_container_manager(workspace_id: str, workspace_path: str = ""):`
- `web_ui/backend/server.py:3578` — `path = _resolve_workspace_path(workspace_id, workspace_path)`
- `web_ui/backend/server.py:3583-3584` — `return ContainerManager(` / `workspace_path=path,`
- `infra/containers/executor.py:659` — `self.workspace_path = os.path.abspath(workspace_path).rstrip('/')`: an `abspath` and a `rstrip`, no policy.
- `infra/containers/executor.py:946-950` — `host_workspace = self.workspace_path` → `Mount(target="/workspace", source=host_workspace, type="bind", read_only=read_only)`.
- `infra/container_manager.py:2182-2185` — second bind site, `Mount(target="/workspace", source=self.workspace_path, ...)`, unvalidated.
- `infra/resource_container_manager.py:1057-1061` — third bind site, same shape, unvalidated.

## What this is not

- Not a claim that the confinement helper is wrong. It is correct and documented; the defect is that it is optional, because it sits on the parameter branch and not on the return value.
- Not a duplicate of `ticket-bug-ws-set-project-bypasses-vault-confinement.md`. That ticket is about *registration* accepting a vault path over the WebSocket; this one is about the *mount* accepting whatever the registry holds. The fixes are independent and the recommendation there ("both") is why this was split out.
- Not a claim that file tools can read the vault as a consequence. `thoughtmachine/security.py:419-443` blocks host paths under `~/.thoughtmachine` independently of workspace root. The exposure is a container whose `/workspace` is the vault, where in-container paths like `/workspace/credentials/...` never pass the host-path check.
- Not an authenticated-context claim and not a claim about the WebSocket surface — the container lifecycle endpoints here are REST (`server.py` `_make_container_manager` consumers); no container-management WebSocket command was found in this pass.
- Not an end-to-end demonstration. No container was created; the chain is read hop by read hop.

## Disposition

One check, placed where the value is *consumed* rather than where it is *accepted*: refuse a `root_path` that resolves inside `~/.thoughtmachine` at `server.py:3569` (the resolution step), so that no registration path — present or future — can produce a vault-backed `/workspace`. Optional defence in depth at `infra/containers/executor.py:659` and at the two `Mount` sites, which is the cheaper and more durable placement because it does not depend on the server being the only caller.

Deliberately not decided here: whether `/etc` and other non-home roots deserve a check too, or whether "inside the vault" is the only invariant the project wants to enforce. That is a policy question for the owner of `_confine_to_home`'s docstring.

## MUST NOT TOUCH

- `web_ui/backend/server.py:3499-3547` — the validator itself is correct; do not "simplify" it into a prefix string comparison (it already does `realpath`-based containment).
- `thoughtmachine/security.py:419-443` and `:539-553` — the independent file-tool vault block, including the post-symlink recheck. It is the reason this is a mount/tooling issue and not a credential-read issue.
- The `/workspace` destination and the bind `type` at all three `Mount` sites — the destination is load-bearing for every in-container path the project builds.
