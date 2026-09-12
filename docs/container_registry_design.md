> **STATUS: SUPERSEDED (2026-09-12).**
> This document predates and is superseded by
> `docs/container_subsystem_target_architecture.md`, which is canonical.
> Read with the target architecture open.
>
> Two semantics in this document contradict the target architecture and
> must not be implemented as written (body line numbers as of pre-header
> revision):
>
>   - L471-472, L515-543 — auto-recreate of running containers on permission
>     change. The target architecture forbids silent mutation of running
>     containers. Drift is an event; recreate is a user action.
>   - L598-600 — auto-destroy of oldest idle containers on limit drop. Same
>     class of silent destruction; forbidden by the target architecture.
>
> The profile/factory detail and the `resolve_container_config()` SSOT
> proposal remain useful reference material. Historical interest only; not
> a contract.

# Container Registry — Design Document

**Status:** Design draft (not implemented)
**Scope:** A central container registry + single hardened container-creation path, with
live permission-change sync, for the ThoughtMachine container stack.
**Related docs:** `docs/audit-worker-core.md`, `docs/audit/sprint_audit.md`,
`docs/backend-audit.md`, `docs/event_pipeline_audit.md`,
`docs/infrastructure/docker-pipeline-trace.md`, `docs/docker_usage.md`,
`docs/security_layer.md`.
**Line references:** all `file:line` citations below were verified by direct read of the
current tree (this session). They are *current-truth anchors* — the design must keep them
in sync as it lands.

---

**Table of Contents**

1. [Motivation](#1-motivation)
2. [Core Design](#2-core-design)
3. [Live Permission Sync](#3-live-permission-sync)
4. [Integration with Existing Managers](#4-integration-with-existing-managers)
5. [Integration with Workspace Lifecycle Manager](#5-integration-with-workspace-lifecycle-manager)
6. [Resource Container Specifics](#6-resource-container-specifics)
7. [Future Extensions](#7-future-extensions)
8. [Security Considerations](#8-security-considerations)

---

## 1. Motivation

### 1.1 Three duplicated container-creation stacks

Today the codebase builds hardened containers in **three independent places**. Each one
re-derives the same hardening recipe (capabilities, security options, tmpfs, user,
read-only root, idle command) from scratch:

| Aspect | `infra/container_manager.py` | `infra/resource_container_manager.py` | `tools/docker_executor.py` |
|---|---|---|---|
| Fresh-create block | L486–534 | L528–546 (`ensure_container`) | L572–635 (`_ensure_container`) |
| `cap_drop=["ALL"]` | ✓ | ✓ | ✓ |
| `security_opt=["no-new-privileges:true"]` | ✓ | ✓ | ✓ |
| `read_only=True` | ✓ | ✓ | ✓ |
| `user="1000:1000"` | ✓ | ✓ | ✓ |
| tmpfs `/tmp` `rw,noexec,nosuid,size=64m` | ✓ | ✓ | ✓ |
| tmpfs `/home/agent` `rw,exec,size=256M,uid=1000,gid=1000` | ✓ | ✓ | ✓ |
| tmpfs `/workspace/.git` (when present) | ✓ | — | — |
| `network=network_mode` | ✓ | ✓ | ✓ |
| `mem_limit` | ✓ (default `"1g"`) | ✓ (fixed `"512m"`) | ✓ (default `"1g"`) |
| `cpu_quota` | ✓ (default 100000) | ✓ (fixed 50000) | ✓ (default 100000) |
| `command=["tail","-f","/dev/null"]` | ✓ | ✓ | ✓ |
| `oom_score_adj` | ✓ **1000** (L523) | ✓ **500** (L536) | ✗ **missing** |
| `/workspace` bind | ro/rw by workspace mode | always rw | ro/rw by workspace mode |
| `tm-packages-<ws_id>` volume → `/home/agent/.local` | ✓ | ✗ | ✓ |
| `PYTHONUSERBASE=/home/agent/.local` env | ✓ | ✗ | ✓ |
| worktree main-repo mount + `.venv` (ro) | ✗ | ✓ | ✗ |
| Labels | `thoughtmachine.container_name`, `thoughtmachine.workspace_id` | + `thoughtmachine.resource=git` | `thoughtmachine.workspace_id`, `thoughtmachine.note` |

**Consequences:**

- **Drift already happened.** `oom_score_adj` exists in exactly two of the three sites
  (container_manager L523 = 1000 with comment *"user containers are the first OOM-kill
  victims"*; resource_container_manager L536 = 500 with comment *"resource (git)
  containers get a moderate OOM score"*; grep confirms `docker_executor.py` has none).
  Any future hardening knob (e.g. seccomp profile, pids limit, ulimits) will silently
  land in one or two of the three sites.
- A security fix must be applied three times, and there is no test that asserts
  equivalence, so the fix can never be proven complete.
- The duplication is the root cause behind the "expected config" concept in
  `security_gate.get_expected_container_config` (L159–223) — an *audit-time* comparison
  exists precisely because the *build-time* recipe is not centralized.

### 1.2 Permission resolution is fragmented

Container network/workspace mode is derived in **two separate resolution sites** that
must stay in lockstep:

1. `tools/docker_executor.py::_compute_container_config_from_permissions`
   (L128–201): `network_mode = "bridge"` when network write is granted, else `"none"`;
   `workspace_mode = "rw"` when filesystem is in (`write`, `full`), else `"ro"`.
   Audits `NETWORK_DECISION`.
2. `infra/container_manager.py::_compute_config` (borrowed from the above,
   container_manager L411–413) — a *copy* of the same logic.
3. `security/security_gate.py::get_expected_container_config` (L159–223) returns
   `{"network_mode": "bridge"|"none", "workspace_mode": "rw"|"ro"}` from effective
   permissions (`get_effective_permissions` L109, the min of session vs workspace
   capabilities) — a **third, separate** site, used for verification.

Three copies of "what should the container look like" is three chances to disagree.
The registry makes the *factory* the single site that consumes resolved permissions and
the single site that produces container config.

### 1.3 No live permission sync

Docker network mode and bind-mount read-only-ness are **create-time** properties: they
are fixed when `docker create` runs and cannot be changed while the container lives
(there is no live network-mode update API on a stock engine, and bind-mount ro/rw is
equally immutable without recreate).

Today, when a session's permissions change (filesystem write granted, network write
granted/revoked via the web UI or a config apply), running containers keep their stale
configuration:

- A container created under `network_mode="none"` stays offline after the user grants
  network write.
- A container created with a `ro` workspace stays read-only after `write` is granted.
- **Worse (security):** a container created with `network_mode="bridge"` stays online
  after network write is revoked.

There is no `config_changed` consumer that reconciles running containers. The
`global_event_bus` (`agent/events.py`, `global_event_bus = EventBus()` L571) already
carries `EventType.CONFIG_CHANGED = 'config_changed'` (L80) and is consumed in many
places (agent, tool_executor, event_logger, worker, check_system), but no container
manager subscribes.

### 1.4 No central container lifecycle

Containers are created ad-hoc by three managers and tracked only inside each manager
instance:

- `container_manager` counts against `max_containers` (enforced at
  `container_manager.py:405` before create) but has no session-wide view of *other*
  managers' containers.
- `resource_container_manager` ensures at most one git resource container per workspace
  via label lookup (reuse by labels else create, L424+).
- `docker_executor` creates on demand inside tool execution; nothing registers the
  container anywhere.
- `container_manager.list_containers` (L934–937) *skips* `thoughtmachine.resource`
  labeled containers as defense-in-depth — i.e. the two populations are deliberately
  opaque to each other.

There is no single place that can answer "which containers exist for session X, what
profile were they created with, and are they still compliant with the session's current
permissions?". The audit docs keep bumping into container-adjacent failure modes
(`docs/audit-worker-core.md` R1–R8: stale-reply misattribution at worker.py:705-735,
sticky `_timeout_triggered`, timeout asymmetry caller-300s vs worker.py:2515,
completed-but-abandoned attempts never pruned, deterministic 1000-char panel
truncation, CheckSystem wrong-workspace/permission fallbacks, paused-worker query
abandonment + dual resume, F2 generation-guard TOCTOU); several of those risks are
aggravated by containers that outlive the permission context that created them.

### 1.5 Dockerfile trust anchor already vault-gated (resource image)

The resource git image is built from `~/.thoughtmachine/docker/resource/Dockerfile`
(`vault.py::_RESOURCE_DOCKERFILE_RELPATH` L193), seeded once from
`resources/resource_dockerfile.txt` by `ensure_resource_dockerfile` (L196–227) during
bootstrap step 2b (`thoughtmachine/bootstrap.py` L125–130) with `overwrite_existing=False`
and `never_overwrite` semantics (L138–141): the vault copy is the trust anchor and is
never replaced. Any centralization work must preserve this property (see §6).

### 1.6 Goals

1. **One hardened creation path** — a single factory (`create_hardened_container`) that
   owns the full hardening recipe; the three managers become thin facades over it.
2. **One permission-resolution site** — the factory consumes effective permissions and
   derives `network_mode` / `workspace_mode`; `get_expected_container_config` becomes a
   pure read of the factory's resolution logic instead of a parallel implementation.
3. **Live permission sync** — a `ContainerRegistry` subscribes to
   `config_changed` events and reconciles running containers (recreate when network or
   workspace mode changed).
4. **Registry visibility** — a session→containers map so any component can enumerate,
   destroy, or re-profile containers; resource containers remain first-class but
   visible.
5. **No behavior change when disabled** — a session-config flag `use_container_registry`
   (default false) gates delegation; legacy paths stay byte-for-byte when off.

### 1.7 Non-goals

- Changing Docker engine semantics (no live network-mode API; recreate is the mechanism).
- Rewriting `container_manager`/`docker_executor` internals beyond the facade boundary.
- Container image building (vault-gated builds stay as-is).
- Web-UI work (the UI only gains *correct* behavior via events; no new UI surfaces).

---

## 2. Core Design

### 2.1 Components

```
┌────────────────────────────────────────────────────────────────┐
│                      ContainerRegistry                         │
│  • session → {containers} map                                  │
│  • request_container / destroy_container / list                │
│  • on_permission_changed (config_changed subscriber)           │
│  • container limits (max_containers per session;              │
│    ≤1 resource container per workspace)                       │
└───────────────┬───────────────────────────────┬────────────────┘
                │ creates via                   │ reconciles via
                ▼                               ▼
┌──────────────────────────────┐   ┌──────────────────────────────┐
│   create_hardened_container  │   │  permission diff + recreate  │
│   (THE single create path)   │   │  (stop → remove → create,    │
│   profile × resolved perms   │   │   same handle)               │
└───────────────┬──────────────┘   └──────────────────────────────┘
                │ consumed by (thin facades, flag-gated)
                ▼
┌──────────────┬───────────────┬───────────────┐
│ ContainerMgr │ ResourceCont  │ DockerExec    │
│ (user cm)    │ Mgr (git)     │ (tool exec)   │
└──────────────┴───────────────┴───────────────┘
```

The registry is the *orchestrator*; the factory is the *builder*. Neither touches the
Docker daemon directly except through the factory (registry) and the existing
docker-client wrappers (facades' exec/stop/remove passthrough).

### 2.2 ContainerProfile

The factory is driven by an immutable `ContainerProfile` — the *union* of everything
observed in the three create blocks today:

```python
@dataclass(frozen=True)
class ContainerProfile:
    # identity / routing
    container_type: str            # "user" | "git" | (future) "mcp", ...
    image: str                     # e.g. "agent-executor", RESOURCE_IMAGE_TAG
    name_prefix: str               # "agent-exec-" | "tm-res-"   (see note)
    labels: dict                   # thoughtmachine.* labels (workspace_id,
                                   #   container_name, resource kind, note, ...)
    # resource envelope
    mem_limit: str                 # "1g" (user) | "512m" (git)
    cpu_quota: int                 # 100000 (user) | 50000 (git)
    oom_score_adj: int             # 1000 (user) | 500 (git) — mandatory, no default
    # mounts
    workspace_mode: str            # "rw" | "ro"  (resolved perms override, see 2.4)
    extra_mounts: tuple            # e.g. git worktree main repo (rw),
                                   #   .venv (ro), tm-packages volume (rw)
    package_volume: bool           # tm-packages-<ws_id> → /home/agent/.local
    package_volume_env: bool       # PYTHONUSERBASE=/home/agent/.local
    tmpfs: dict                    # /tmp + /home/agent (+ /workspace/.git opt-in)
    # network
    network_mode: str              # "none" | "bridge" (resolved perms override, 2.4)
    # process
    user: str = "1000:1000"
    read_only: bool = True
    cap_drop: tuple = ("ALL",)
    security_opt: tuple = ("no-new-privileges:true",)
    command: tuple = ("tail", "-f", "/dev/null")
    detach: bool = True
    tty: bool = True
    stdin_open: bool = True
```

**Name-prefix note.** The current naming conventions must be preserved exactly, because
several components pattern-match on them:
- user containers: `agent-exec-<sha256(workspace_path)[:12]>-<safe_session_tag>`
  (`container_manager.py` L380–382);
- resource containers: `tm-res-<sha256(workspace_path)[:12]>-git`
  (`resource_container_manager.py` L404–413, deliberately avoiding the `agent-exec-`
  prefix; mirrored in `workspace_lifecycle_manager.py` `_RESOURCE_NAME_PREFIX="tm-res-"`
  L53 / `_RESOURCE_NAME_SUFFIX="-git"` L54).

The profile carries the *prefix*; the factory appends the hash/tag. The
`_is_resource_container_request` guard (`workspace_lifecycle_manager.py` L575–594:
`value == RESOURCE_IMAGE_TAG` or `startswith("tm-res-") and endswith("-git")`) keeps
working unchanged.

### 2.3 The factory: `create_hardened_container`

Single creation path. Pseudocode (all field names mirror the observed create calls):

```python
def create_hardened_container(profile, *, workspace_path, workspace_id,
                              session_id, resolved_network_mode, resolved_workspace_mode,
                              vault_root, name=None, note=None):
    """THE only place that runs docker_client.containers.create."""

    # 1. resolve the final config: explicit perms beat profile defaults
    network_mode  = resolved_network_mode or profile.network_mode   # "none" | "bridge"
    workspace_mode = resolved_workspace_mode or profile.workspace_mode  # "ro" | "rw"

    # 2. identity
    if name is None:
        name = f"{profile.name_prefix}{sha256(workspace_path)[:12]}-{safe_tag(session_id)}"

    # 3. mounts
    mounts = [
        Mount("/workspace", workspace_path, read_only=(workspace_mode != "rw")),
    ]
    if profile.package_volume:
        volume = get_or_create_volume(f"tm-packages-{workspace_id}")   # docker_executor L597-604
        mounts.append(Mount("/home/agent/.local", volume, read_only=False))
    mounts.extend(profile.extra_mounts)   # e.g. git worktree repo (rw), .venv (ro)

    # 4. tmpfs  (container_manager L486-491 / resource L523-526 / docker_executor L613-620)
    tmpfs = {"/tmp": "rw,noexec,nosuid,size=64m",
             "/home/agent": "rw,exec,size=256M,uid=1000,gid=1000"}
    if profile.tmpfs.get("/workspace/.git") and (workspace_path / ".git").is_dir():
        tmpfs["/workspace/.git"] = ""

    # 5. environment
    environment = {"PYTHONUSERBASE": "/home/agent/.local"} if profile.package_volume_env else {}

    # 6. create — every hardening knob is a profile field; no optional hardening
    return docker_client.containers.create(
        image=profile.image,
        name=name,
        mounts=mounts,
        tmpfs=tmpfs,
        network=network_mode,
        mem_limit=profile.mem_limit,
        cpu_quota=profile.cpu_quota,
        oom_score_adj=profile.oom_score_adj,          # MANDATORY (fixes docker_executor gap)
        cap_drop=list(profile.cap_drop),
        security_opt=list(profile.security_opt),
        read_only=profile.read_only,
        user=profile.user,
        detach=profile.detach,
        tty=profile.tty,
        stdin_open=profile.stdin_open,
        command=list(profile.command),
        environment=environment or None,
        labels={**profile.labels,
                "thoughtmachine.workspace_id": workspace_id,
                "thoughtmachine.container_name": name,
                "thoughtmachine.session_id": session_id},   # new: registry key
    )
```

Properties the factory guarantees by construction:

- **No drift**: every created container carries the full hardening set; `oom_score_adj`
  is mandatory (the `docker_executor` gap from §1.1 closes automatically).
- **Registry keyed**: the `thoughtmachine.session_id` label is added by the factory so
  the registry (and any future tool) can enumerate by session without an in-memory map
  being the sole source of truth.
- **Deterministic**: same profile + same permissions ⇒ identical create parameters;
  `security_gate.get_expected_container_config` becomes a thin call into the same
  resolution function the factory uses (§2.4).

### 2.4 Single permission-resolution function

Move the resolution logic from `docker_executor._compute_container_config_from_permissions`
(L128–201) into a shared, registry-owned function that both the factory and the security
gate call:

```python
def resolve_container_config(workspace_id, session_permissions, workspace_permissions):
    """Single source of truth for network_mode + workspace_mode.
    Replaces: docker_executor._compute_container_config_from_permissions (L128-201),
              container_manager._compute_config (L411-413),
              security_gate.get_expected_container_config (L159-223)."""
    effective = min_permissions(session_permissions, workspace_permissions)  # gate L109
    network_mode = "bridge" if (effective.network is True or effective.network == "write") else "none"
    workspace_mode = "rw" if effective.filesystem in ("write", "full") else "ro"
    return {"network_mode": network_mode, "workspace_mode": workspace_mode}
```

`security_gate.get_expected_container_config` keeps its signature but returns
`resolve_container_config(...)` — the audit-time comparison and the build-time
resolution can no longer disagree.

### 2.5 ContainerRegistry interface

```python
class ContainerRegistry:
    def __init__(self, vault_root, *, event_bus=global_event_bus):
        self._containers: dict[str, RegisteredContainer] = {}   # key: container_id
        self._sessions: dict[str, set[str]] = {}                # session_id -> container_ids
        self._session_configs: dict[str, dict] = {}             # session_id -> config (limits)
        event_bus.subscribe(EventType.CONFIG_CHANGED, self.on_permission_changed)

    # -- lifecycle -------------------------------------------------------
    def register(self, session_id, workspace_id, handle, profile) -> None:
        """Adopt a container (fresh create or discovered at startup)."""
        self._sessions.setdefault(session_id, set()).add(handle.container_id)
        self._containers[handle.container_id] = RegisteredContainer(
            handle=handle, session_id=session_id, workspace_id=workspace_id,
            profile=profile, created_permissions=resolve_container_config(...))

    def unregister(self, container_id) -> None: ...

    def request_container(self, session_id, workspace_id, permissions,
                          *, profile="user", name=None, note=None) -> Handle:
        """Factory create + register, after enforcing limits (2.6) and the
        resource guard (2.7). Mirrors WorkerSupervisor.request_container
        (workspace_lifecycle_manager.py L532-563)."""
        self._enforce_limits(session_id, workspace_id, profile)
        cfg = resolve_container_config(workspace_id, permissions, workspace_permissions(workspace_id))
        container = create_hardened_container(profile=PROFILES[profile],
            workspace_path=..., workspace_id=..., session_id=session_id,
            resolved_network_mode=cfg["network_mode"], resolved_workspace_mode=cfg["workspace_mode"])
        self.register(session_id, workspace_id, Handle(container), PROFILES[profile])
        return handle

    def destroy_container(self, container_id, *, force=False) -> None:
        """stop (5s grace, kill if needed) → remove(force=True) → unregister.
        Mirrors container_manager.stop L633 / remove L660 idempotent semantics."""

    def get_containers_for_session(self, session_id) -> list[RegisteredContainer]: ...

    def list_containers(self, *, include_resource=False) -> list[RegisteredContainer]:
        """Registry view. NOTE: container_manager.list_containers (L934-937) excludes
        resource-labeled containers; the registry keeps both populations visible and
        lets callers filter — no more hidden containers."""

    # -- live permission sync (section 3) -------------------------------
    def on_permission_changed(self, event) -> None:
        """Diff created_permissions vs resolved permissions; recreate on change."""

    # -- limits ----------------------------------------------------------
    def _enforce_limits(self, session_id, workspace_id, profile) -> None:
        """max_containers (session config container_limits.max_containers →
        workspace config.json default 4) and resource cap (≤1 per workspace)."""
```

### 2.6 Container limits

Preserve the existing two-level limit and make the registry the enforcer:

1. **Per-session user containers**: precedence session config
   `container_limits.max_containers` (`container_manager.py` L276/283) → workspace
   `config.json` `max_containers` (default 4, L228/243); clamped to ≥1
   (`_get_max_containers` L273–291). Enforced before create
   (`container_manager.py` L405–408).
2. **Resource containers**: **at most 1 per workspace** (label-based reuse in
   `resource_container_manager.ensure_container` already guarantees this; the registry
   enforces it structurally via the `thoughtmachine.resource=git` label + name hash).

The registry enforces both centrally, so `container_manager` and
`resource_container_manager` no longer each carry their own counter.

### 2.7 Resource-container guard

The registry's `request_container` applies the same guard as
`WorkerSupervisor._is_resource_container_request` (`workspace_lifecycle_manager.py`
L575–594) before delegating to the factory:

```python
def _is_resource_container_request(profile_or_image) -> bool:
    return (value == RESOURCE_IMAGE_TAG            # "tm-resource-git"
            or (isinstance(value, str)
                and value.startswith("tm-res-") and value.endswith("-git")))
```

Worker sub-agents cannot request resource containers — `PermissionError("Resource
containers (git, tm-res-*) are reserved for the main agent...")` stays the behavior,
now enforced at the registry boundary rather than only at the supervisor.

### 2.8 Feature flag: `use_container_registry`

No `use_container_registry` key exists anywhere in the tree today (grep: zero matches).
The flag mirrors the established `use_workspace_lifecycle_manager` pattern
(`workspace_lifecycle_manager.py` L97–101 reads session config; checked at
`tools/workspace/worker.py` L781–783):

```python
def is_registry_enabled(session_config) -> bool:
    """Session config key; default False."""
    return bool((session_config or {}).get("use_container_registry", False))
```

- **Off (default):** all three managers keep their current code paths untouched;
  `global_event_bus` subscription is never installed.
- **On:** managers delegate creation/reconciliation to the registry (see §4); the
  `config_changed` subscription is installed and §3 reconciliation is active.

The flag composes with `use_workspace_lifecycle_manager` — the WLM can be on without
the registry (today's behavior) and the registry can be on without the WLM; the
interesting configuration is both on (§5).

---

## 3. Live Permission Sync

### 3.1 The problem restated

`network_mode` and bind-mount `ro/rw` are fixed at `docker create` time. Docker has no
live network-mode update API on a stock engine, and bind mounts cannot be remounted
read-write in place. The **only** way to change either property is
**stop → remove → recreate**.

Recreate is safe *because* containers are ephemeral, stateless executors:
- all state lives on the `/workspace` bind and the `tm-packages-<ws_id>` volume
  (both persist across recreate);
- tmpfs (`/tmp`, `/home/agent`, optional `/workspace/.git`) is explicitly *not*
  expected to survive — it is scratch by design;
- the handle (name) is preserved so pattern-matching components
  (`tm-res-*`, `agent-exec-*`, `_is_resource_container_request`) see no change.

### 3.2 The event

The registry subscribes to `EventType.CONFIG_CHANGED` on `global_event_bus`
(`agent/events.py` L80, L571).

**Canonical payload** the registry consumes:

```json
{
  "type": "config_changed",
  "session_id": "session-xyz",
  "config": { "...": "full merged session config (incl. container_limits)" },
  "permissions": { "filesystem": "write", "network": "write", "container": "...", "..." }
}
```

**Current-emitter gap (must be closed as part of this design):**

| Emitter | Location | Payload today | Missing |
|---|---|---|---|
| Worker bus adapter | `tools/workspace/worker.py` L182–186 | `("config_changed", {"config": config})` | `session_id`, `permissions` |
| Web backend `get_config` | `web_ui/backend/server.py` L732–736 | `{"type","config","settings","permissions","merged_config"}` | `session_id` (bridge knows it) |
| Web backend config applied | `web_ui/backend/server.py` L1000–1003 | `{"type":"config_changed", **result}` | `session_id` |

The emitters know their session context (worker bus adapter runs inside the session;
the web bridge owns `session_id`) — they must add `session_id` to the payload. The
registry **rejects** `config_changed` events without a `session_id` it recognizes
(see §8.7).

### 3.3 Subscription + reconciliation pseudocode

```python
class ContainerRegistry:
    def on_permission_changed(self, event):
        # 1. validate (8.7): session_id present and known
        session_id = event.get("session_id")
        if not session_id or session_id not in self._sessions:
            return  # not our session, or malformed — ignore

        # 2. resolve the *new* expected config from the SAME function the factory uses
        new_cfg = resolve_container_config(
            workspace_id, event["permissions"], workspace_permissions(workspace_id))

        # 3. diff per registered container of this session
        for cid in list(self._sessions[session_id]):
            reg = self._containers[cid]
            old = reg.created_permissions          # captured at create time
            if (old["network_mode"] == new_cfg["network_mode"]
                    and old["workspace_mode"] == new_cfg["workspace_mode"]):
                continue                            # still compliant — nothing to do
            self._recreate(reg, new_cfg)            # 3.4

    def _recreate(self, reg, new_cfg):
        """stop → remove → create from factory, same handle, same profile."""
        handle = reg.handle
        try:
            self._stop_idempotent(handle.container_id)     # stop(5s), kill if running
            self._remove_force(handle.container_id)        # remove(force=True)
        except Exception as exc:
            log("ERROR", "registry.reconcile", f"teardown failed: {exc}")
            # Do NOT proceed: a half-removed container must not be recreated blind.
            # Mark quarantined (8.6) and surface to the session; retry next event.
            reg.quarantine(exc)
            return
        fresh = create_hardened_container(
            profile=reg.profile,
            workspace_path=reg.workspace_path, workspace_id=reg.workspace_id,
            session_id=reg.session_id,
            resolved_network_mode=new_cfg["network_mode"],
            resolved_workspace_mode=new_cfg["workspace_mode"],
            name=handle.name)                     # SAME name → same handle
        reg.handle = Handle(fresh)
        reg.created_permissions = new_cfg
        self._containers[fresh.id] = reg          # re-key (old id is gone)
        log("INFO", "registry.reconcile",
            f"recreated {handle.name} for {new_cfg['network_mode']}/{new_cfg['workspace_mode']}")
```

### 3.4 Why recreate, not restart

| Property | Restart (`docker restart`) | Recreate (this design) |
|---|---|---|
| network mode change | ✗ impossible | ✓ new create params |
| workspace ro/rw change | ✗ impossible | ✓ new mount flags |
| hardening recipe change | ✗ keeps old params | ✓ picks up new profile |
| `/workspace` + package volume state | preserved | preserved (bind/volume) |
| tmpfs scratch | preserved (stale) | reset (desired) |
| container id | same | **new** (callers must not pin ids) |
| name / labels | same | same (name preserved) |

**Contract for callers:** hold the **handle (name)**, never the container id. All
existing managers already resolve by name/label for exec (`container_manager.exec`
L546, `resource_container_manager.exec` L577), so this contract is already the norm.

### 3.5 Failure semantics

- **Teardown failure** (stop/remove raises): container is marked **quarantined**
  (removed from active service; still listed so it can be cleaned); the registry logs
  and re-attempts on the next `config_changed` event. A container that cannot be
  stopped is **never** silently recreated next to its stale twin (that would defeat
  the security purpose of the sync).
- **Create failure** after successful teardown: the session is left without a running
  container for that slot; the error is surfaced via the event bus (existing
  `error_occurred` channel) and the next `config_changed`/`request_container` retries.
  This mirrors the idempotent, never-raises conventions of `container_manager.stop`
  (L633) / `resource_container_manager.remove` (L667).
- **One failure never blocks the rest**: reconciliation iterates containers
  independently (same principle as `ExecutionTracker.terminate_all`,
  `workspace_lifecycle_manager.py` L131–176).
- **Event storms**: `config_changed` may fire in bursts (web UI apply + worker echo).
  Reconciliation is **debounced** (e.g. coalesce within 1s) and **idempotent**
  (diffing makes the second pass a no-op). No event ordering is assumed beyond
  last-write-wins on the resolved permissions.

### 3.6 Interaction with `container_limits` changes

A `config_changed` may also lower `container_limits.max_containers`. Reconciliation
handles this: if the session now exceeds its limit, the registry destroys the
**oldest, idle** user containers (lowest activity, then oldest create time) until back
under the limit — mirroring the limit check at `container_manager.py` L405 but now with
a global view. Resource containers are exempt (separate cap, §2.6).

---

## 4. Integration with Existing Managers

### 4.1 Facade strategy

When `use_container_registry` is **on**, the three managers keep their **public API**
and become thin facades over the registry + factory. When **off**, their current code
runs unchanged. This gives a rollback-safe, flag-gated migration with no caller changes.

| Public API (unchanged) | Legacy impl (flag off) | Facade impl (flag on) |
|---|---|---|
| `container_manager.start(image=None, name=None, note=None)` L363 | current L363+ | `registry.request_container(...)` (profile `user`); limit check via registry |
| `container_manager.stop(container_id)` L633 | current | `registry.destroy_container` (idempotent) |
| `container_manager.remove(container_id)` L660 | current | `registry.destroy_container(force=True)` |
| `container_manager.exec(...)` L546 | current | passthrough to docker client (unchanged) |
| `container_manager.status()` L684 | current | registry-derived |
| `container_manager.list_containers()` L934 | current (excludes resource) | `registry.list_containers(include_resource=False)` — same visible result |
| `resource_container_manager.ensure_container()` L424 | current | `registry.request_container(profile="git", network_mode=...)` with ≤1-per-workspace cap |
| `resource_container_manager.exec/stop/remove` L577/L647/L667 | current | passthrough |
| `docker_executor._ensure_container` L572 | current | `registry.request_container(profile="user")`; `verify_container_integrity` L204 now compares against factory-derived expected config |

### 4.2 What the facades no longer own

- create parameters (the factory owns them);
- limit counters (the registry owns them);
- permission → config mapping (`resolve_container_config` owns it);
- resource-image availability checks stay in `resource_container_manager`
  (`_ensure_resource_image` L109–208, `is_resource_image_available` L211–225) — the
  registry calls *in*, it does not duplicate image logic (see §6).

### 4.3 Migration / adoption

At startup with the flag on, the registry **adopts** existing labeled containers
instead of orphaning them:

```python
def adopt_existing(self, workspace_id):
    for c in docker_client.containers.list(all=True,
                filters={"label": f"thoughtmachine.workspace_id={workspace_id}"}):
        if c.labels.get("thoughtmachine.session_id") in self._sessions:
            self._containers[c.id] = RegisteredContainer(
                handle=Handle(c), session_id=..., workspace_id=...,
                profile=guess_profile_from_labels(c),   # "git" if thoughtmachine.resource else "user"
                created_permissions=infer_from_create_params(c))
```

Inferred `created_permissions` is then validated on the first `config_changed` event —
any adopted container that is already non-compliant gets recreated by the normal
reconciliation path (§3.3). No manual cleanup step needed.

### 4.4 Decommissioning the registry (flag off again)

Turning the flag off after it was on is safe: containers created by the factory carry
the same labels/names the legacy managers already understand, so the legacy paths can
pick them up (resource reuse-by-label, `agent-exec-*` naming, `list_containers`
filters). The registry unsubscribes and stops reconciling; nothing is destroyed on
decommission.

---

## 5. Integration with Workspace Lifecycle Manager

### 5.1 Call flow (flags on)

```
WorkerSupervisor.request_container(permissions)          workspace_lifecycle_manager.py L532
  │  1. guard: _is_resource_container_request → PermissionError (unchanged, L575-594)
  │  2. registry enabled?  ──no──► container_manager.start(image=..., name=..., note=...)  (legacy L557-563)
  ▼  yes
ContainerRegistry.request_container(session_id, workspace_id, permissions, profile="user")
  │  3. _enforce_limits (session max_containers; resource cap)
  │  4. resolve_container_config(permissions)      ← single resolution site (§2.4)
  ▼
create_hardened_container(profile=user, resolved perms, ...)   ← single create site (§2.3)
  │
  ▼
handle returned to WorkerSupervisor → ExecutionTracker.add(worker_id, handle)
```

### 5.2 Per-hop responsibilities

| Hop | Responsibility | Current anchor |
|---|---|---|
| `WorkerSupervisor.request_container` | resource guard; feature-flag check (`feature_flag_check` param, L254–271); delegate to registry when on | L532–563 |
| `Registry.request_container` | limits; resolution; factory create; register | new |
| `create_hardened_container` | the full hardening recipe | new |
| `ExecutionTracker.add` | lifecycle tracking for `terminate_all` | L104+ |
| `WorkerSupervisor.release_container` | `container_manager.stop` → becomes `registry.destroy_container` | L565–573 |

### 5.3 ExecutionTracker cross-reference

The WLM's `ExecutionTracker` (L104+) and `terminate_all` (L131–176) already handle
three execution kinds (docker_exec, scoped_container, subprocess) with one-failure-
never-blocks semantics. The registry **does not replace** the tracker — the tracker
owns *work* lifecycle; the registry owns *container* lifecycle. The seam:

- `ExecutionTracker.add(worker_id, handle)` — worker work items carry a container
  handle (name), not an id (consistent with §3.4's handle contract).
- `ExecutionTracker.terminate_all` calls `container_manager.stop(container_id)` today;
  with the flag on it calls `registry.destroy_container(container_id)` (which is
  stop-then-remove, strictly stronger — matches the `remove(container_id)` fallback the
  tracker already uses on stop failure).
- `WorkerSupervisor._terminate_all` (L608–614) and the timeout transition
  (L339–349) are unchanged — they delegate to the tracker, which delegates to the
  registry facade.

### 5.4 `is_wlm_enabled` vs `is_registry_enabled`

```python
is_wlm_enabled(cm)        # workspace_lifecycle_manager.py L97-101
    → session config "use_workspace_lifecycle_manager" (default False)

is_registry_enabled(session_config)   # new
    → session config "use_container_registry" (default False)
```

Both read the same `_get_session_config(cm)` pattern (L86–94: `session_config` param
or private `_session_config` attr). They are independent flags:

- WLM on, registry off → today's behavior (supervisor manages containers directly).
- Registry on, WLM off → managers are facades; supervisor still calls
  `container_manager.start` which delegates to the registry internally.
- Both on → §5.1 flow; recommended target state.

### 5.5 Guard consistency

The resource guard exists in **two** places today (`WorkerSupervisor._is_resource_container_request`
L575–594, plus name/image constants at L47–54). With the registry, the guard moves into
`registry.request_container` (§2.7) and the supervisor's copy becomes a pass-through
call to it — one implementation, two enforcement points (defense in depth, not
duplication).

---

## 6. Resource Container Specifics

### 6.1 Image trust anchor (vault-gated Dockerfile)

The git resource image `tm-resource-git` is built from
`~/.thoughtmachine/docker/resource/Dockerfile`:

- `vault.py::_RESOURCE_DOCKERFILE_RELPATH = "docker/resource/Dockerfile"` (L193);
- `ensure_resource_dockerfile(resources_dir, overwrite_existing=False)` (L196–227)
  copies `resources/resource_dockerfile.txt` → vault path on **first** bootstrap only;
- bootstrap step 2b (`thoughtmachine/bootstrap.py` L125–130) calls it with
  `overwrite_existing=False`; `never_overwrite` entries (L138–141) mean the vault copy
  is the trust anchor and is **never replaced** by repo content afterwards;
- build command per `resource_container_manager.py` L98–100:
  `docker build -t tm-resource-git <vault_root>/docker/resource/`.

**Registry interaction:** the registry never builds or copies this Dockerfile. It calls
`resource_container_manager._ensure_resource_image()` (L109–208) — single-flight,
double-checked lock, **success cached** in the module-level `_RESOURCE_IMAGE_READY`,
**failures never cached**, **never raises** (availability surfaced via
`is_resource_image_available` L211–225). The factory's `profile.image` for `"git"` is
`RESOURCE_IMAGE_TAG` (L96).

### 6.2 Resource profile (as registered in the factory)

| Field | Value | Anchor |
|---|---|---|
| `container_type` | `"git"` | `RESOURCE_KIND` L375 |
| `image` | `"tm-resource-git"` | `RESOURCE_IMAGE_TAG` L96 |
| `name_prefix` | `"tm-res-"` + hash + `"-git"` | L404–413 |
| labels | + `thoughtmachine.resource=git` | `RESOURCE_LABEL` L374, `_labels` L415–421 |
| `mem_limit` / `cpu_quota` | `"512m"` / `50000` | L398–399 |
| `oom_score_adj` | `500` ("moderate OOM score") | L536 |
| workspace bind | always `rw` | L473–480 |
| extra mounts | linked-worktree main repo `rw` (L488–499), `.venv` `ro` (L505–514) | |
| package volume / `PYTHONUSERBASE` | **absent** by design (no python packages in the sandbox image) | |
| network | `"none"` default via constructor param | `__init__` L379–400 |

The image itself is a minimal git sandbox (`resources/resource_dockerfile.txt`, 57
lines): **no docker CLI, no docker socket, no python packages, no vault access**;
`CMD ["tail","-f","/dev/null"]` (L57).

### 6.3 Network enforcement for resource containers

`ResourceContainerManager.__init__(..., network_mode="none", ...)` takes the network
mode as a constructor parameter. Under the registry this becomes a *profile* field, not
a constructor constant:

- Default: `"none"` — git operations that need the network fail closed (same as today).
- When the session's effective permissions grant network write, reconciliation
  (§3.3) recreates the resource container with `network_mode="bridge"` so `git fetch`
  works; revoking network write recreates it back to `"none"`.
- The ≤1-per-workspace invariant is preserved across recreates (same name ⇒ same slot).

This is a deliberate behavior *improvement* over today (where the resource container's
network mode is fixed for the lifetime of the manager instance).

### 6.4 Resource containers in the registry's list view

Today `container_manager.list_containers` (L934–937) skips
`thoughtmachine.resource`-labeled containers as defense-in-depth. The registry keeps
both populations visible (§2.5) with a filter parameter, because the registry *is* the
defense-in-depth boundary — hiding resource containers from one manager no longer
serves a purpose when all containers flow through one factory with one guard.

---

## 7. Future Extensions

### 7.1 `container_type`-driven profiles

The factory's `profile` parameter already implies a profile table:

```python
PROFILES = {
    "user": ContainerProfile(container_type="user", image="agent-executor",
                             name_prefix="agent-exec-", mem_limit="1g",
                             cpu_quota=100000, oom_score_adj=1000,
                             package_volume=True, package_volume_env=True, ...),
    "git":  ContainerProfile(container_type="git", image=RESOURCE_IMAGE_TAG,
                             name_prefix="tm-res-", mem_limit="512m",
                             cpu_quota=50000, oom_score_adj=500,
                             package_volume=False, ..., extra_mounts=(worktree, .venv)),
}
```

New container types become new table entries — no new create paths, no new hardening
decisions.

### 7.2 MCP container type + `localhost` network profile

A future `"mcp"` container_type (MCP servers running next to the agent) would add:

- a `localhost`-style network profile — today only `"none"`/`"bridge"` exist; the
  resolution function (§2.4) can grow a third mode (e.g. bridge with
  `--add-host`/port mapping to a loopback-only network);
- its own image/Dockerfile trust anchor in the vault (same `ensure_*` + `never_overwrite`
  pattern as the resource image, §6.1);
- registry enforcement that MCP containers are session-scoped and destroyed on session
  teardown (the registry already keys by `session_id`).

### 7.3 Egress proxy profile

A future egress-proxy profile (route only whitelisted destinations) fits the same
shape: a profile field for network config (proxy env vars, extra mounts for proxy
CA/certs), resolved at create time and reconciled on permission change. The recreate
mechanism in §3.4 is the enabling primitive — no new sync machinery needed.

### 7.4 Future profile table (sketch)

| container_type | image | net default | ws mode | oom | purpose |
|---|---|---|---|---|---|
| `user` | agent-executor | none | ro (rw w/ perm) | 1000 | agent/tool execution |
| `git` | tm-resource-git | none | rw | 500 | git worktree sandbox |
| `mcp` (future) | (vault-gated) | localhost | ro | 500 | MCP servers |
| `egress` (future) | (vault-gated) | proxy | ro | 500 | filtered egress |

---

## 8. Security Considerations

### 8.1 Mandatory hardened factory

The single most important property: **no code path creates a container outside
`create_hardened_container`**. The facade layer makes this enforceable — the three
managers' create blocks are deleted (flag on) or become unreachable (flag off is the
legacy snapshot, which already carries the hardening). Any future creator must go
through the factory, so `cap_drop`, `no-new-privileges`, `read_only`, `user=1000:1000`,
`oom_score_adj`, tmpfs, and network mode are guaranteed present — closing the
`docker_executor` oom_score_adj gap (§1.1) by construction.

### 8.2 Resource limits

- **Per-session** `max_containers` (session config → workspace config.json default 4,
  clamped ≥1) enforced centrally (§2.6) — prevents one session from exhausting the
  daemon.
- **Per-container** `mem_limit` / `cpu_quota` are profile fields with no unbounded
  default; resource containers are capped at 512m/50000.
- `oom_score_adj` ordering is preserved: user containers 1000 (first OOM victims),
  resource containers 500 (moderate) — the kernel-side priority that protects the host
  under memory pressure.

### 8.3 Network isolation

- Default is `network_mode="none"` for both user and resource profiles; `"bridge"`
  only when effective permissions grant network write (§2.4, same rule as today's
  `docker_executor` L128–201).
- The security-critical direction is **revocation**: network write revoked ⇒ container
  recreated to `"none"` via §3.3. The recreate window (stop→remove→create, seconds) is
  the only exposure, and it is strictly better than today's indefinite exposure.
- No `--privileged`, no host network, no docker socket mounts anywhere in the factory.

### 8.4 Resource (git) container access

The resource image is minimal by construction (§6.2): no docker CLI/socket, no python
packages, no vault. Its only host-visible surfaces are the `/workspace` bind (rw) and
the linked-worktree main repo (rw) — both already scoped to the workspace. The registry
adds no new mounts.

### 8.5 Image integrity

- Vault-gated Dockerfiles (`docker/resource/Dockerfile`) with `never_overwrite`
  semantics (§6.1) — repo content cannot silently replace the trust anchor.
- Image availability is single-flight and success-cached; failures never cached and
  never raised into the container path (`_ensure_resource_image` L109–208) — a missing
  image fails closed (no container) with a visible availability flag.

### 8.6 Quarantine on failed reconciliation

If teardown fails during permission-change reconciliation (§3.5), the container is
quarantined: it is excluded from active use but kept listed and retried, and it is
**never** recreated alongside. Rationale: a stale container with superseded permissions
is a security liability; a blind recreate next to a half-removed twin is a worse one.
Quarantined containers are reaped by the normal session-teardown path.

### 8.7 Event payload trust

`config_changed` events are cross-process (worker bus, web bridge). The registry must
not let an arbitrary event re-profile containers:

1. **`session_id` required** — events without it (all current emitters, §3.2) are
   ignored until emitters are updated; the registry logs a warning naming the emitter
   gap.
2. **Known-session check** — `session_id` must match a registered session; unknown
   sessions are ignored.
3. **Permission values pass through the same min-with-workspace reduction**
   (`security_gate.get_effective_permissions` L109) — a session cannot escalate beyond
   workspace capabilities by emitting a favorable event, because the workspace-side
   cap is re-applied at resolution time.
4. The registry never accepts container *configuration* from events — only
   permissions; the profile comes from the registry's own table.

### 8.8 Defense in depth preserved

The label-based exclusions and guards stay in place even though the registry now
centralizes them:

- `thoughtmachine.resource` label exclusion in `container_manager.list_containers`
  (L934–937) remains (harmless, and protects the legacy path when the flag is off);
- `_is_resource_container_request` remains enforced at the supervisor *and* the
  registry (§5.5);
- `verify_container_integrity` (`docker_executor.py` L204) now compares against the
  factory-derived expected config — audit and build can no longer drift apart.

---

*End of design. All line references verified against the current tree; the
`use_container_registry` flag, the factory, and the registry do not exist yet and are
the subject of this proposal.*
