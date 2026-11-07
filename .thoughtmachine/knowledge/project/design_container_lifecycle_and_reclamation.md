# Design Pass — Container Lifecycle & Reclamation

- **Brief**: `.thoughtmachine/working_docs/brief-container-lifecycle-design.md`
- **Brief sha256**: `a20009e035a458246e841ce318b7d3fab64b9fb6174222d7897adbb2a2e0e51f` (3066 B / 61 lines, verified)
- **Baseline**: branch `dev`, HEAD `92e12153d6b74952d5c9187172f54573ad700718`
- **Mode**: read-only design pass. No code change. This file is the only artifact.

## The question

> **Who owns a container's death?**

Every container this system runs is created by `create_hardened_container` and
eventually must stop, exit, be removed, or be reaped. This pass enumerates the
four lifecycle classes, the verbs that act on a container over its life, the
reapers that reclaim it after life, and the privilege/grant blast radius that
decides *who is even allowed to kill it*. The core fault line is **ownership of
death**: which actor (agent, operator, boot sweep, periodic sweep, session
teardown, idle reclaimer) is authoritative for each class, and where those
authorities disagrees.

**Out of scope (named only, not analysed)**: Bins, R13, R6, R9, friend-machine.

## Corrections carried into this pass

These correct earlier/looser statements and are load-bearing for the rulings:

- **(a)** `validate_path` (`thoughtmachine/security.py`) *is* realpath-aware, but
  the vault / workspace-boundary / deny-list **re-checks fire only when
  `canonical_abs != target_abs`** (i.e. conditionally). See decision 5.
- **(b)** Git "no host fallback" holds **only on the error path**:
  `_run_git_raw` raises on `unavailable` / `host_fallback`. Host git is a
  **config-selected first-class mode**, not an error fallback.
- **(c)** `host_user()` resolves via `defaults._host_ids()`, returning `None`
  **iff `os.name == "nt"`**; the `"0:0"` value is a Windows-only branch and is
  unreachable on POSIX.
- **(d)** All lifecycle families converge on a **single `.containers.run` call**
  inside `create_hardened_container` (`infra/container_create.py`).

## Class taxonomy (shared vocabulary)

`LIFECYCLE_CLASSES = (ephemeral, persistent, resource, service)` in
`thoughtmachine/container_record/models.py` (a test asserts `len == 4`).
`SCHEMA_VERSION_CURRENT = 5`. `LifecyclePolicy` in
`thoughtmachine/container_record/lifecycle_policy.py` carries
`(class_name, agent_reachable, own_lifecycle, workspace_gc_max_age_s,
drift_axes, restart_policy)`:

| class | agent_reachable | own_lifecycle | gc_max_age_s | drift axes | restart_policy |
|-------|-----------------|---------------|--------------|------------|----------------|
| persistent | True | False | 86400 | all | `unless-stopped` |
| ephemeral  | True | False | 86400 | image | `no` |
| resource   | False | True | None | image | `unless-stopped` |
| service    | False | True | None | image | `unless-stopped` |

`policy_for()` raises `UnknownLifecycleClass` (fail-closed).
`LIFECYCLE_SERVICE` exists **only** as a taxonomy/policy entry plus an `__init__`
export — no production create site was found (grep). It is a **declared but
unpopulated class**: it has a fail-closed predicate and a GC-skip, but no
producer and no dedicated reaper. `docker_restart_policy(cls)` yields
`{"Name": ..., "MaximumRetryCount": 0}` or `None`;
`normalise_restart_policy` maps `""`/`None` → `"no"`.

---

## D.1 Decision 1 — Privilege model per lifecycle class

**Question.** What privileges/identity does each class run under, and is that a
per-class decision or a single pipeline?

**Current behaviour (cited).**
- All four classes are created by `create_hardened_container`
  (`infra/container_create.py`): a single `.containers.run` call, single
  hardening path. Empirically the resulting identity is `uid=1000` (agent);
  `CapPrm`/`CapEff`/`CapBnd`/`CapAmb` are all `0`. (Correction (d): one create
  site, not four.)
- Per-class *differentiation* is data-driven, not code-branched: the class flows
  in as `spec.lifecycle_class` and selects `LifecyclePolicy`. The visible
  per-class privilege levers are:
  - `restart_policy` via `docker_restart_policy(spec.lifecycle_class)` applied at
    the single create point (`infra/container_create.py`, decision 3).
  - `agent_reachable` gating agent verbs (`ContainerManager.stop`/`remove`
    call `_agent_access_denial`; `infra/container_manager.py`).
  - `workspace_mode` / `network_mode` carried in the record's
    `intent_snapshot`.
- The **resource** class is the outlier: it mounts `/workspace` **rw** (real
  `.git`), sets `tmpfs` to `/tmp` + `/home/agent` only (no `.git` shadow), and
  additionally bind-mounts the linked-worktree **main** repo rw at its original
  host path via `_resolve_worktree_main_repo`
  (`infra/resource_container_manager.py`), which returns `None` — declining the
  extra mount — for the workspace itself (or any path within it), filesystem
  roots, and paths within the vault root. Executors mount rw, or ro iff
  `workspace_mode != "rw"`.

**Failure family (evidence).** *Privilege divergence by accident* — because the
only real per-class privilege differentiators are `agent_reachable`,
`restart_policy`, and the resource-class mount set, any *other* privilege
difference (caps, seccomp, user) that a future author assumes is "per class" is
in fact **uniform**. A change intended for one class silently applies to all, or
a change intended for all silently misses the resource rw-mount exception.

**Candidate shapes.**
1. Keep single-pipeline + data-driven policy (status quo).
2. Fork the create path per class.
3. Single pipeline, but make the *entire* privilege envelope a field of
   `LifecyclePolicy` (so no hidden uniform defaults).

**Ruling.** **1 as the invariant, with 3 as the correctness guard.**
Per-class privilege must be expressed *only* through `LifecyclePolicy` and the
resource mount set; the create pipeline stays single (correction (d)). Any
future per-class privilege claim must be backed by a policy field, otherwise it
is a bug. The resource class is a **named exception**, not a template.

**Consequences.** The privilege model is auditable in one table. Divergence
between "what a class name implies" and "what actually differs" is a policy
authoring error, catchable by inspecting `POLICY_BY_CLASS` plus the resource
mount branch. No code change required to adopt; this is a documentation/boundary
ruling.

---

## D.1 Decision 2 — Lifecycle verbs: create / stop / restart / remove / reclaim-on-idle / reclaim-on-session-end

**Question.** What verbs exist, who may call them, and how is each of the six
realised?

**Current behaviour (cited).**
- **create** — `ContainerManager.start(...lifecycle_class, allow_fresh,
  heal_missing)` (`infra/container_manager.py`) → `create_hardened_container`.
  Ephemeral create is driven by `DockerCodeRunner`
  (`tools/docker_code_runner.py`) via
  `start(lifecycle_class=LIFECYCLE_EPHEMERAL, heal_missing=True)`.
- **stop** — `ContainerManager.stop(id)`: idempotent, never raises (reload →
  `stop(timeout=5)` → `kill` if still running → `_drop_container`). Gated by
  `_agent_access_denial(self.class_of(container))`, so the agent cannot stop
  resource/service. `ResourceContainerManager.stop()` is no-arg/workspace-scoped.
- **restart** — **There is no `restart()` method and no `RestartTool`** anywhere
  (grep for `def restart`, `RestartTool`: none). Restart is expressed **only**
  as (i) the docker `restart_policy` set at create, and (ii) drift-heal /
  `allow_fresh` recreate. Tools in `tools/container_control.py`:
  `ContainerStartTool`, `ContainerExecTool`, `ContainerStopTool`,
  `ContainerStatusTool`, `ContainerListTool`, `ContainerBuildTool`,
  `ContainerLogsTool`, `ContainerRemoveTool` — **no restart tool**.
- **remove** — `ContainerManager.remove(id)`: idempotent, never raises (calls
  `stop()` first, then `remove(force=True)`), gated by `_agent_access_denial`.
  `ResourceContainerManager.remove()` no-arg/workspace-scoped.
- **reclaim-on-idle** — `DockerExecutor` (`docker_executor.py`, persistent
  pooling): if a container exists and `time.time() - last_used > idle_timeout`
  (default from `timeout_constants.IDLE_TIMEOUT_SECONDS = 600`) → close.
  Ephemeral uses `idle_timeout = 0` (no pooling). Note `IDLE_TIMEOUT_SECONDS` is
  an **idle** constant, not a TTL.
- **reclaim-on-session-end** — `web_ui/backend/bridge.py::close_session()`;
  `tools/workspace/worker_thread.py::_cleanup_worker_containers()` +
  `shutdown_workers(timeout)`;
  `worker_container.cleanup_worker_containers(cm, owner_identity)` stops+removes
  only worker-owned containers (label exact match AND ephemeral, resolved from
  record via `RECORD_LABEL_KEY`, fail-closed to persistent);
  `worker_manager.cleanup_dead(session_id)`.
- **ephemeral create→stop** — `DockerCodeRunner` runs `exec` then, in
  `finally`, `manager.stop(info["id"])`. It **stops only, never removes**, so an
  **exited** container is deliberately left for GC/sweep. On drift-deny it
  `remove`s the drifted container and starts fresh.

**Failure family (evidence).** *Missing verb / silent-orphan family.*
1. The absence of a `restart` verb means restart is a *create-time contract*
   plus *recreate on drift*. Anyone expecting a first-class "restart" semantics
   (in-place, preserving container identity) finds none; the effective restart
   changes container identity (recreate) or is delegated to the docker daemon
   policy.
2. Ephemeral stop-only leaves **exited** containers as the steady state —
   reclamation is entirely delegated to the sweep (decision 3). If the sweep is
   not triggered, exited ephemerals accumulate.

**Candidate shapes.**
1. Add a first-class `restart()` verb/tool.
2. Keep restart as policy + recreate; document that container identity is not
   preserved across restart.
3. Make ephemeral `remove` instead of `stop` on the `finally` path.

**Ruling.** **2.** No restart verb is added. Restart remains: (a) docker
`restart_policy` (per class), and (b) recreate-on-drift/`allow_fresh`. Document
explicitly that "restart" here **does not preserve container identity**.
Ephemeral stop-only stays as-is (stop, not remove) — it is the correct
counterpart to the sweep-based reclaimer (decision 3) and keeps the reclamation
authority in **one** place.

**Consequences.** Restart semantics are daemon-delegated or identity-changing;
callers must not assume state continuity. Ephemeral exited-container accumulation
is *owned by the sweep*, so sweep liveness is load-bearing for ephemeral hygiene
(link to decision 3 trigger coverage). No code change required to adopt the
ruling.

---

## D.1 Decision 3 — Reaper ownership per class (reaper + trigger + skip)

**Question.** For each lifecycle class, which reaper is authoritative, what
triggers it, and what does it skip?

**Current behaviour (cited).** Reapers live largely in
`infra/container_manager.py`, with resource-specific ones in
`resource_container_manager.py`.

- **`cleanup_workspace(workspace_id, docker_client)`** — stops (`timeout=5`) +
  removes (`force=True`) **every** container labelled
  `thoughtmachine.workspace_id`, and **does not apply `_gc_should_skip`**, so it
  removes resource containers too. This is the **workspace-decommission** path;
  the `resource_container_manager` header states this explicitly ("the git
  sandbox must never outlive its workspace"). **Reaper-of-record for teardown,
  all classes.**
- **`cleanup_stale_worker_containers(docker_client, owner_identity)`** — matches
  label `thoughtmachine.worker == owner_identity` AND state in
  `{created, exited, dead}`; calls `_gc_should_skip`; never touches
  running/paused/resource. Caller `ContainerManager._fresh_start`
  (`infra/container_manager.py`).
- **`_container_lifecycle_class(container)`** — resolves the class: resource via
  a live probe, else via record lookup (`RECORD_LABEL_KEY`,
  `find_by_docker_label`); **fails closed to `LIFECYCLE_PERSISTENT`**. Docstring
  warns a divergent vault silently misses this, losing the fail-closed denial
  for service/unknown.
- **`_gc_should_skip(container)`** — returns `True` when
  `policy_for(cls).own_lifecycle` OR `UnknownLifecycleClass` ⇒ **skipped = {
  resource, service, unknown }**.
- **`sweep_exited_workspace_containers(registered_workspace_ids=None,
  max_age_s=86400, dry_run=False)`** — predicate `status == 'exited'` AND idle
  age ≥ `max_age_s`; branches registered (idle/TTL) = `removed_registered`,
  unregistered (orphan) = `removed_orphan`; **always skips
  resource/own_lifecycle**; skips missing/unparseable/future `FinishedAt`
  (clock-skew); `registered=None` ⇒ TTL-only (orphan disabled); `registered=[]`
  ⇒ conservative **no-op**; `dry_run` counts but does not remove; never raises.
- **`sweep_orphan_container_records(*, registered_workspace_ids=None, ...)`** —
  reaps orphan **records** (container gone AND past age/retention).
- **`reap_container_record(workspace_id, record_id, *, apply=False, ...)`** —
  the single-record reap primitive (docstring: "Primitive behind
  `scripts/reap_container_record.py`"), bypassing the **age gate only** while
  every other gate (own-lifecycle, liveness, docker) stays in force. The
  **"gating SSOT"** framing — that this is the single gate every path funnels
  through — is **our judgement, not line-verified**: it is not proven that
  `sweep_orphan_container_records` delegates to it.
- **`scripts/reap_container_record.py`** — operator CLI escape hatch over
  `reap_container_record`: retains every gate **except the age gate**; refuses
  live-bound / own-lifecycle / unknown; dry-run without `--apply`; exit codes
  `0/2/3`; reason vocab `ok / dry_run / record_not_found / container_live /
  lifecycle_own / unknown_class / docker_unavailable / delete_failed`.
- **`resource_container_manager.py`** — `cleanup_workspace_resources(workspace_id)`,
  `sweep_stale_resource_containers(registered_workspace_ids)`.
- **Age precedence** (per record, never exempt): `retention_days` →
  `policy.workspace_gc_max_age_s` → global `default_max_age_s` (`None` falls
  through; a `None` policy age is **not** an exemption — the exemption is the
  `own_lifecycle` class gate).
- **Triggers** (`web_ui/backend/server.py`): boot sweep at startup inside
  `lifespan` (`_sweep_orphan_resource_containers`,
  `_sweep_exited_workspace_containers`, `_sweep_orphan_container_records`)
  plus periodic
  `_periodic_container_sweep_loop(interval_s)` → `_run_container_sweeps`. Sweep
  list: `('exited-workspace', None, ...)`, `('orphan-resource',
  _periodic_registry_has_workspaces, ...)`, `('orphan-records', None, ...)`;
  `_periodic_registry_has_workspaces` guards (empty registry ⇒ skip).

**Failure family (evidence).** *Divergent ownership of death.*
1. **Two authorities on the same container.** `cleanup_workspace` removes
   resource/service (no `_gc_should_skip`), while every *sweep* deliberately
   skips them (`_gc_should_skip`). This is intentional (teardown vs. GC) but it
   means "own_lifecycle ⇒ exempt from GC" is **not** "exempt from death" —
   teardown still kills it. A reader who equates the two mis-reads ownership.
2. **Fail-closed class resolution is also a divergence sensor.**
   `_container_lifecycle_class` failing closed to `persistent` means a vault
   divergence makes a resource/service container *appear* GC-eligible — it would
   lose its skip and become reapable. (The docstring flags this.)
3. **Trigger coverage gap.** `sweep_exited_workspace_containers` is triggered
   with `registered_workspace_ids=None` (orphan branch disabled) on the periodic
   list, and `orphan-resource` is guarded by `_periodic_registry_has_workspaces`
   (skips when the registry is empty). So in a long-lived process with an empty
   registry, orphan reclamation does not run — orphans persist until boot.
4. **`registered=[]` no-op** is conservative but means "no registered
   workspaces" is indistinguishable from "registry unavailable" in behaviour
   (both no-op).

**Candidate shapes.**
1. Give each class an explicit, single owning reaper and forbid overlap.
2. Keep teardown (`cleanup_workspace`) as the *terminal* authority for all
   classes and sweeps as *GC-only* for `own_lifecycle=False` (status quo).
3. Make `_gc_should_skip` consult classification via a single SSOT that never
   fails to `persistent` implicitly.

**Ruling.** **2, with 3 as the required hardening direction.** Ownership of
death is **layered**:
- **GC authority** (sweeps, idle reclaim, session-end worker cleanup) applies
  **only** to `own_lifecycle = False` (persistent, ephemeral). `resource` and
  `service` are GC-immortal by design.
- **Teardown authority** (`cleanup_workspace` /
  `cleanup_workspace_resources`) is the **terminal** owner for *all* classes on
  workspace decommission — the resource sandbox must not outlive its workspace.
- `reap_container_record` + `scripts/reap_container_record.py` is the
  **operator override**, retaining every gate except age.
Class resolution must be the single SSOT; the fail-closed-to-`persistent`
default is acceptable only as long as divergence is *sensed*, not silently
mis-reaped (hardening track 3).

**Consequences.** "Who owns death" has a clean layering, but the exempt-from-GC
vs. exempt-from-death distinction must be documented (it is a common misread).
The periodic trigger's orphan-branch guards (points 3–4) are a **known
reclamation-liveness gap** when the registry is empty; orphans persist to the
next boot. No code change is made in this pass — this is a ruling/boundary, and
track 3 is flagged for a future implementation pass.

**Reaper conformance (Wave 2).** The shared GC-tier predicate is
`_gc_should_skip(container)` (`infra/container_manager.py`): skip iff
`policy_for(lifecycle_class).own_lifecycle` is true (equivalently, skip
`resource`/`service`; fail-closed `True` on `UnknownLifecycleClass`). It is
**the** single source of truth for the **GC tier**, whose conformance set is:
`sweep_exited_workspace_containers` (literal `_gc_should_skip`),
`sweep_orphan_container_records` (the same predicate applied to *records* via
`policy_for(record.lifecycle_class).own_lifecycle`, with a
`!= LIFECYCLE_RESOURCE` carve-out), and `reap_container_record` /
`scripts/reap_container_record.py` (refuses `own_lifecycle`). The **teardown
tier** is deliberately *not* class-gated and must never consult
`_gc_should_skip`: `cleanup_workspace` / `cleanup_workspace_resources`
(terminal, all classes on decommission),
`resource_container_manager.sweep_stale_resource_containers` (kills orphan
resource sandboxes — teardown authority for orphans; only an age gate may
spare one), and `vault_gc._gc_orphan_resource_containers` (reaps resource/user
containers whose owning **workspace is gone**; skips those whose workspace is
still registered). The two tiers are separated by *ownership of death*:
GC-tier containers are own-lifecycle-exempt while their owner lives, and
orphan-teardown is the fallback authority once the owning workspace no longer
exists.

---

## D.1 Decision 4 — Grant blast radius, including the tmpfs `.git` shadow

**Question.** What is the blast radius of the grants (mounts/tmpfs/identity)
each class receives, and is the `.git` shadow a bug, a design choice, or
load-bearing?

**Current behaviour (cited).**
- **`.git` shadow**: `container_manager.py` sets `tmpfs["/workspace/.git"] = ""`
  **iff** `os.path.isdir(workspace_path/.git)`; `docker_executor.py` does the
  same. An empty tmpfs mount over `/workspace/.git` yields an **empty, writable-
  but-ephemeral** (RAM) `.git` inside the container, hiding the host repo's git
  internals from the agent.
- **Resource container**: **no shadow** — `tmpfs` is `/tmp` + `/home/agent`
  only, and `/workspace` is bind-mounted **rw** (real `.git`). Additionally the
  linked-worktree **main** repo is bind-mounted **rw at its original host path**
  (`_resolve_worktree_main_repo` in `infra/resource_container_manager.py` returns
  `None` for the workspace itself/within it, filesystem roots, and vault-root
  paths).
- **Mount mode**: resource mounts rw; executor mounts ro iff
  `workspace_mode != "rw"`.
- **Identity**: `uid=1000` (agent); caps all `0` (empirical).

**Failure family (evidence).** *Shadow is conditional and mount-mode-dependent.*
1. The shadow is applied **only when `.git` is a directory on the host at create
   time**. A worktree (`.git` is a *file*), a bare-adjacent layout, or a `.git`
   created *after* container start ⇒ **no shadow** ⇒ the agent sees the real
   `.git` (write path to host git internals via the rw workspace bind).
2. The resource class deliberately has **no** shadow and an **rw** workspace —
   so for resource containers the agent *can* touch the real `.git` (by design:
   the resource container is the git sandbox). The blast radius differs sharply
   between classes.
3. The `_resolve_worktree_main_repo` rw main-repo mount widens the blast radius
   beyond `/workspace`: a resource container can write the **original host main
   repo** (the helper declines only the workspace itself/within it, filesystem
   roots, and vault-root paths).

**Candidate shapes.**
1. Treat the `.git` shadow as a **bug** to be made unconditional.
2. Treat it as a **design choice** conditioned on intent (executor = hide git;
   resource = expose git).
3. Treat it as **load-bearing**: the empty tmpfs is what lets git-needing tools
   believe `.git` exists while writes are discarded.

**Ruling.** **2, and it is genuinely load-bearing for the executor class — with
a known hole.** The shadow is a deliberate, class-scoped design choice:
- For **executor** (persistent/ephemeral) containers the shadow is
  **load-bearing**: it (a) prevents host `.git` mutation and (b) gives a
  scratch git that evaporates, keeping the container stateless w.r.t. VCS.
- For the **resource** class the absence of a shadow is equally deliberate: the
  resource container *is* the git sandbox and must see the real `.git`.
The hole: because the shadow is gated on `os.path.isdir(workspace_path/.git)`,
the guarantee is **conditional on host layout and create-time state** — a
worktree (`.git` is a file) or a post-start `.git` defeats it. This is a **bug
in the guard, not in the intent**: the intent (executor must not reach host git)
is load-bearing; the `isdir` conditionality is the defect.

**Consequences.** Blast radius:
- executor: host `.git` hidden **iff** create-time `.git` is a dir — otherwise
  the real `.git` is reachable through the workspace bind (bug).
- resource: real `.git` + rw workspace + rw main-repo at host path (wide, by
  design) — the widest grant in the system.
The fix direction is to make the executor shadow unconditional (shadow
`/workspace/.git` regardless of file/dir), which is an implementation track, not
this pass. No code change here.

---

## D.1 Decision 5 — Symlink safety conditionality in `validate_path`

**Question.** Is the symlink/realpath safety in `validate_path` guaranteed, or
conditional, and is that acceptable?

**Current behaviour (cited).** `validate_path` in `thoughtmachine/security.py`:
1. reject null bytes;
2. compute `target_abs` via **`abspath`** (does **not** follow symlinks);
3. block vault compartments:
   `requested_abs.startswith(vault_root_resolved / VAULT_ROOT ×
   VAULT_BLOCKED_SUBDIRS)`;
4. if no `workspace_path`, return `realpath`;
5. `workspace_abs = realpath(workspace_path)`; `target_rel = relpath(...)`;
   `".."`/absolute ⇒ `PathOutsideWorkspaceError`;
6. `_match_blocked_workspace_path(target_rel)` deny-list;
7. **then** `canonical_abs = realpath(target_abs)`, and **all** re-checks
   (vault-via-symlink, workspace-boundary `canonical_rel startswith ".."`,
   deny-list) execute **only inside `if canonical_abs != target_abs:`**.

**Failure family (evidence).** *Conditional safety by construction.* (Correction
(a).)
1. If the pre-resolution path already resolves (`canonical_abs == target_abs`,
   i.e. no symlink component was traversed), the canonical re-checks are
   **skipped**. The first pass used non-resolving `abspath`, so this is
   *usually* safe: a symlink component makes `canonical != target` and the
   re-checks fire. But the guarantee is **conditional on the inequality**, not
   on an explicit "no symlink" test.
2. **TOCTOU**: between the `abspath` check and the eventual use, a path
   component can be swapped for a symlink; the re-checks guard the *resolved*
   value at check time, not at use time.
3. **Non-realpath callers**: `docker_code_runner`'s working-dir guard uses
   `normpath`, **not** `realpath` — a caller that bypasses `validate_path`'s
   canonical step gets weaker (non-resolving) protection.

**Candidate shapes.**
1. Always run the canonical re-checks (drop the `!=` short-circuit).
2. Keep the short-circuit; document it as a performance-gated conditional and
   require `realpath` at every call site.
3. Make all callers route through `validate_path` (single gate).

**Ruling.** **2 as the observed contract; 1 + 3 as the hardening direction.**
The short-circuit is a deliberate fast path that is *safe enough* only because
step 2 uses non-resolving `abspath`: any symlink in the chain forces
`canonical != target` and re-runs the checks. This makes the safety
**conditional** — it depends on the inequality, hence on there being a symlink
to catch, and it is defeated by (i) TOCTOU between check and use and (ii)
non-`realpath` callers. The conditional is **not** a correctness bug for the
single-component case (the resolved-vs-unresolved comparison is exactly the
symlink detector), but the guarantee must be stated as
**conditional-on-inequality, realpath-at-call-site** rather than unconditional.

**Consequences.** `validate_path` is realpath-aware and its re-checks are
correct *for paths it fully resolves*; the residual risks are TOCTOU and
non-realpath callers (e.g. `docker_code_runner`'s `normpath` guard). Hardening
track: always run the canonical re-checks and funnel all callers through
`validate_path`. No code change in this pass.

---

## Not covered / boundary

- **Excluded by task (named only)**: Bins, R13, R6, R9, friend-machine.
- **Not covered here**: the `service` class **implementation** (declared and
  GC-immortal, but no producer/reaper — out of scope to build); the exact
  hardening implementations of decisions 3 (SSOT class resolution), 4
  (unconditional `.git` shadow), and 5 (always-on canonical re-checks) — these
  are **implementation tracks**, flagged, not executed in this read-only pass.
- **Boundary**: this pass rules on *ownership semantics* and *stated contracts*;
  it does not modify code, records, or git. Empirical evidence is limited to the
  observed identity/caps (`uid=1000`, caps `0`) and the containerized git
  trailer (`execution_mode: containerized`, `failure_reason: none`).

- **Revision note (this pass)**: an **accuracy/changelog pass** on this file
  only — tool names de-shorthanded (`ExecTool`→`ContainerExecTool` and the
  other six), the two source line refs replaced by symbol citations
  (`ContainerManager._fresh_start`; `lifespan`), and two characterisations
  resolved (the "gating SSOT" framing marked as judgement, not line-verified;
  the `_resolve_worktree_main_repo` refusal restated symbol-precisely). **No
  ruling changed.**
