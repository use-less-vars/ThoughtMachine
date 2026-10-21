# Ticket: bug/ws-set-project-bypasses-vault-confinement

**DO NOT FIX IN THIS WAVE.** Filed as a pointer only.

**Filed:** 2026-10-21, noticed while verifying the WebSocket command surface claims at `9d5ed869`.
**Severity:** HIGH — the one documented "TRUST ANCHOR" invariant in the server is enforced on the REST path and skipped on the WebSocket path, and the WebSocket path is unauthenticated.
**Status:** open; verified by read. `set_project` itself is UI-dead — the live entry point is `apply_config`, which has the same defect and *less* validation.

**Refined 2026-10-21 (same pass, `9d5ed869`): the container-mount leg is now PROVEN, not merely suspected — and with one correction to the reasoning below.** The mount assembly validates nothing (`infra/containers/executor.py:659`, `:946-950`), and the server's confinement helper runs only on an *explicit client-supplied* path, never on a registry entry's `root_path` (`web_ui/backend/server.py:3558-3569`, detail in `## The container-mount leg, now proven`).

## Finding

`_confine_to_home` states the invariant in its own docstring (`web_ui/backend/server.py:3075-3106`): "TRUST ANCHOR: `~/.thoughtmachine` holds the workspace registry, credentials, config and session state, so user-reachable endpoints (browse, create, container lifecycle) must never resolve INTO it." The REST routes obey it — `workspace_routes.py:145` and `:1725` both do `confined = _confine_to_home(body.path)` and only then call `registry.register_by_root(confined)` (`:157`, `:1738`).

The WebSocket connection does not. `set_project` (`web_ui/backend/server.py:2706`) validates the incoming path with exactly one check, at `:2723`: `if not os.path.isdir(new_project):` — no vault check, no confinement call — then assigns `_project_path = new_project` (`:2739`) and, when the path has no workspace yet, `WorkspaceRegistry.get_default().register_by_root(str(_project_path))` (`:2766`) followed by `ensure_workspace_dirs(entry.id)` (`:2769`). `register_by_root` does no confinement of its own (`thoughtmachine/workspace_registry.py:330-365`): it resolves, generates an id, derives a label, and writes the entry. So `{"command":"set_project","project":"/home/<user>/.thoughtmachine"}` is accepted in full: the vault is registered as a workspace, and its root becomes the session's workspace root.

Two corrections to the obvious version of this claim, both of which make the ticket *broader* rather than weaker. First, `set_project` is not what the UI sends — grepping `web_ui/frontend/src` for `set_project` returns nothing, and the code says so itself at `:1391-1393` ("the frontend no longer sends a separate set_project command; apply_config handles the full project switch internally"). The live equivalent is the `apply_config` workspace-switch branch: `raw_workspace_path = config.get("workspace_path")` at `:1404`, `os.path.normpath` at `:1405`, straight into `_project_path = new_workspace_path` at `:1455` and `register_by_root` at `:1482` — with no `isdir` check at all, let alone confinement. Third entry point: the connect-time `?project=` query parameter is taken verbatim at `:1131` with zero validation. Second, the vault does **not** fall open to file tools: `thoughtmachine/security.py:419-443` blocks any host path under `~/.thoughtmachine` regardless of workspace root, and re-checks after symlink resolution (`:539-553`). What actually breaks is everything downstream of "the workspace root is X": the registry write lands in the vault, `ensure_workspace_dirs` creates directories in it, and — because the workspace root is what gets mounted into the sandbox (`infra/containers/executor.py:659` sets `self.workspace_path`, mounts declared at `:1049`, destination `/workspace` per the integrity check at `:539`) — the vault becomes the container's `/workspace`. Nothing on the container path confines a mount source, so the host-path textual guard in `security.py` never sees in-container paths like `/workspace/credentials/...`. That leg was upgraded from "suspected" to **proven** in the refinement pass — see `## The container-mount leg, now proven` below; it is filed here because the confinement that is supposed to make the question moot is simply absent.

## Evidence

- `web_ui/backend/server.py:3075-3106` — `_confine_to_home`, "TRUST ANCHOR", vault holds registry/credentials/config/session state.
- `web_ui/backend/server.py:2706`, `:2714`, `:2723`, `:2739`, `:2766`, `:2769` — `set_project`: `isdir` only, then `_project_path`, `register_by_root`, `ensure_workspace_dirs`. No `_confine_to_home` call anywhere in the block.
- `web_ui/backend/server.py:1404-1405`, `:1414-1416`, `:1455`, `:1482`, `:1486` — `apply_config` workspace switch: `normpath` only, then `_project_path`, `register_by_root`, `ensure_workspace_dirs`. No `isdir`, no confinement.
- `web_ui/backend/server.py:1131` — `_project_path = project or _project_root`: the `?project=` handshake parameter, unvalidated.
- `thoughtmachine/workspace_registry.py:330-365` — `register_by_root` performs no confinement; `:299-328` `register_workspace` persists via `self._save(raw)`.
- `web_ui/backend/workspace_routes.py:145,157,1725,1738` — the REST path that does it correctly, i.e. the precedent for the fix.
- `thoughtmachine/security.py:419-443` (and `:539-553` for symlinks) — the mitigating layer: any path under `~/.thoughtmachine` is blocked for file tools.
- `infra/containers/executor.py:539`, `:659`, `:946-950`, `:1049` — workspace root is the mount source, destination `/workspace`. (Correction to the original wording: `infra/containers/executor.py:1291-1297` *does* reference the vault, but only as the **build input** `~/.thoughtmachine/workspaces/<id>/Dockerfile` — never as a mount-source guard.)
- `infra/container_manager.py:2182-2185` and `infra/resource_container_manager.py:1057-1061` — the two other `/workspace` bind sites, both `source=self.workspace_path`, both unvalidated.

## What this is not

- Not a claim that file tools can read vault credentials as a consequence. `validate_path` blocks that independently of workspace root. The exposure is registry/workspace-root state, directory creation inside the vault, and the container mount.
- Not a duplicate of `ticket-security-workspace-vault-isolation.md` (that one asks whether workspace A can read workspace B through `checkSystem`) nor of `ticket-bug-unauthenticated-routes-beyond-health.md` (that one is about the absence of auth; this is about a confinement check that exists, is documented, and is skipped on one entry point).
- Not a claim that `set_project` is reachable from the shipped frontend — it is not. `apply_config`'s `workspace_path` is, which is why this is filed as a class defect rather than a single dead branch.
- ~~Not a claim that the container leg is proven end-to-end.~~ **Superseded:** the leg is proven at the mount assembly and at the server's resolution of a registry entry; see below. It is still *not* a claim that anyone has executed the escape against a live daemon (no container was started in this pass).

## The container-mount leg, now proven

Four hops, each read at `9d5ed869`. Nothing between the WebSocket handler and the dockerd call compares a workspace root against `~/.thoughtmachine`.

1. **The registry can hold a vault path.** `register_by_root` does no confinement (`thoughtmachine/workspace_registry.py:330-365`), and neither WS entry point confines first (`:2739`, `:1455`; the `?project=` handshake at `:1131` validates nothing).
2. **The executor takes the path verbatim.** `infra/containers/executor.py:659`: `self.workspace_path = os.path.abspath(workspace_path).rstrip('/')` — an `abspath` and a `rstrip`, no policy. `:946-950` then binds it:

```python
host_workspace = self.workspace_path
workspace_mount = docker.types.Mount(
    target="/workspace", source=host_workspace, type="bind", read_only=read_only
)
```

   The same shape at `infra/container_manager.py:2182-2185` (`Mount(target="/workspace", source=self.workspace_path, …)`) and `infra/resource_container_manager.py:1057-1061`. `/workspace` is the only destination; the source is always `self.workspace_path`.
3. **No check under `infra/containers/` compares a source to the vault.** The only `~/.thoughtmachine` reference in `infra/containers/executor.py` is the *build input* at `:1291-1297` (`~/.thoughtmachine/workspaces/{ws}/Dockerfile` copied into a temp build context). `infra/container_manager.py`'s many `vault_root` references are record-store / notes / Dockerfile paths (`:818-819`, `:838`, `:955-956`, `:4204-4226`) — state I/O, not mount policy. `thoughtmachine/security.py:658-724` (`setup_docker_sandbox`) forwards `workspace_path` to `DockerExecutor(...)` at `:697-705` with no validation at all.
4. **The server's trust anchor does not cover the registry path — this is the refinement that turns suspicion into proof.** `web_ui/backend/server.py:3499-3547` (`_validate_workspace_path`) does exactly the right thing: `_confine_to_home` at `:3518`, containment inside the registered root at `:3540-3546`. But it is only reached from `_resolve_workspace_path` at `:3558-3559`, i.e. **only when the caller supplied an explicit `workspace_path` query param**. With no such param the function returns `entry.root_path` directly (`:3569`) — unvalidated — and `_make_container_manager` (`:3572-3590`) hands that straight to `ContainerManager(workspace_path=path, …)` (`:3583-3584`). So a workspace that was *registered* with a vault root gets a container whose `/workspace` **is** the vault, and the confinement check never runs, because from the server's point of view the path came from its own registry.

Net: the guard is on *client input*, not on *the value that reaches the mount*. The fix therefore cannot be only "add `_confine_to_home` at the three WS assignment sites" — that closes the registration door, but the resolution step at `:3569` should refuse a registry entry whose `root_path` is inside the vault regardless of how it got there. Two independent checks, one ticket.

Deliberately not claimed: `_path_is_within` / realpath comparisons are not re-verified here for symlink games, and no daemon was touched.

## Disposition

Decide whether one confinement helper is the rule for *all* client-supplied workspace roots, or only for REST bodies. If it is the rule, the fix is a single `_confine_to_home` call at each of the three assignment sites (`:1131`, `:1455`, `:2739`) — with the `?project=` and `apply_config` sites needing the answer even more urgently than `set_project`, since neither checks `isdir`. If it is not the rule, then `_confine_to_home`'s docstring is the thing that is wrong, and the container mount source needs its own guard.

Given the refinement, the recommendation is now: **both**, and the second one is cheaper and more durable — add a vault check where a workspace root is *consumed* (`server.py:3569`, and defensively in `infra/containers/executor.py:659` / `infra/container_manager.py` before the `Mount`), so that no registration path — present or future — can produce a vault-backed `/workspace`.

Cross-reference: F30, `ticket-bug-unauthenticated-routes-beyond-health.md`, `ticket-bug-ws-commands-unauthenticated.md`, `ticket-security-workspace-vault-isolation.md`.
