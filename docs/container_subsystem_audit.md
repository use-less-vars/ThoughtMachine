# Container Subsystem Audit

**Status:** DRAFT for Main-engineer review — do not treat as final.
**Branch:** `docs/container-subsystem-audit` (base `dev@c6c11e1`).
**Scope:** Design/audit only. This document proposes no code, tests, or config changes; build-out is a separate, later dispatch. Every substantive claim cites a workspace-relative `file:line`, or is explicitly flagged *unverified — needs runtime check*. The static claims in this draft were verified against live behaviour by probe **v2** (run_id `20260912-030511-0a15`; host `jojo-Swift-SFG16-71`; docker `29.1.3`; cgroup v2; image `tm-workspace-runtime:latest`); where a prior "unverified — needs runtime check" is now settled it is marked **CONFIRMED LIVE**. Probe artefacts (scripts + result JSON) live in `working_docs/`, which is gitignored (`.gitignore:160` → `/working_docs/`), so they cannot be committed and this document is the evidence of record. Probe script hashes: v1 `f898502408d281e52e9e08866c18338de062e611d943479423723cffe1788df1`; v2 `b0e203bfa2221d1ad5aa7b73fb509ef5f79984ee6b195fc2379165b224aff542`.

A container is a WORKSPACE MACHINE — the runtime for computational work the user would otherwise do on host: long test suites, model training, simulations, batch jobs. Containers are not just agent tooling. They are how ThoughtMachine does real work inside a workspace.

This audit reads the shipped container code against that intent and records where the two diverge. The headline is that the subsystem works, but it is **fragmented**: four independent create paths, two competing configuration sources-of-truth, and a described "contract" whose numbers and guarantees do not match the source in several load-bearing places.

**Companion document.** A separate **Target Architecture** document sets out where the container subsystem is going; this audit is *where we are*, that document is *where we're going*.

---

## 1. Container taxonomy

There are **four create paths**, not one. Three of them provision the same "free-use / user" class; the fourth provisions "resource" (git) containers. Agent-reachability is enforced largely by **name/label heuristics**, not by a single tool filter.

| type | provisioner (`file:line`) | who owns | agent can touch? | network default | name scheme |
|---|---|---|---|---|---|
| **free-use / user** (worker + ad-hoc) — **HOT path** | `infra/container_manager.py:209` (create `:764`) | the workspace/session that spawned it | yes — generic agent tools route here | derived from the `network` permission via `resolve_network_mode` (bridge iff value ∈ `{True,"write","outbound"}`, else none) | `agent-exec-<sha256(ws)[:12]>-<session_tag>` (`container_manager.py:471-473`) |
| **free-use / user** (legacy) | `docker_executor.py:400` (create `:658`) | workspace | indirectly (tools don't call it) | same derivation | `agent-exec-<sha256(abspath(ws))[:12]>` (`docker_executor.py:511-512`) |
| **free-use** (registry facade) | `infra/container_registry.py:126-144` (create `:144`) | session | yes — only when `use_container_registry` flag is on (default **False**) | `resolve_network_mode` delegate (`:628-635`) | `tm-{type}-{ws_id[:12]}-{uuid4hex[:8]}` (`container_registry.py:332-334`) |
| **resource / git** | `infra/resource_container_manager.py:837` (create `:1116`) | the resource manager (system-hosted) | **no** — blocked by name/label heuristics | `"none"` unless the caller passes an explicit grant (`:845`, applied `:1102`,`:1121`); registry resource path also `"none"` fail-closed (`container_registry.py:380`) | `tm-res-<sha256(ws_path)[:12]>-git` (`:938-947`) |
| **worker** | free-use containers stamped `thoughtmachine.worker=<session_id>:<worker_name>` (`container_manager.py:694-700`) | the worker's parent session | yes (permission-gated only; **not** hard-blocked, `tools/workspace/worker_thread.py:597-603`) | inherited from the parent spawn | reuse of the free-use scheme |
| **ephemeral** (`DockerCodeRunner`) | via `ContainerManager` (`tools/docker_code_runner.py:52-54, 389-425`) | the tool call | yes | inherited | free-use scheme |
| **mcp** | RESERVED — no provisioner | n/a | n/a | n/a | n/a |
| **proxy** | RESERVED — no provisioner | n/a | n/a | n/a | n/a |

Supporting facts:

- `CONTAINER_TYPES=("user","resource","mcp","proxy")` (`agent/config/defaults.py:136-138`), but only `user` (free-use) and `resource` are provisioned. `mcp`/`proxy` exist only in the validation tuple and the policy vocabulary (`mcp` is `"banned"` in `defaults.py:190`; `resource_catalog.py:74-80,106`), and are never passed to `register()`/`request_container()` — the request type defaults to `"user"` (`container_registry.py:281`). ⇒ **mcp/proxy are reserved/dead.**
- "Agent cannot touch resource containers" is enforced by **name/label heuristics**: `start` refuses the `tm-res-` prefix (`container_manager.py:478-479`); `exec` raises `PermissionError` via `_is_resource_container` (`:806-807`); `stop`/`remove` refuse type resource (`:905-907,926-928,963-965`); detector `:1460-1496`; the registry create refuses resource image/type/name-hint (`container_registry.py:288-296`); `list_containers` hides the resource label (`:1247-1251`); lifecycle guards also at `workspace_lifecycle_manager.py:156-163`.
- Ownership / stop-authority: free-use containers are stop/removable by their workspace; resource containers are stopped/removed only by the resource manager (`resource_container_manager.py:1450,1470`).
- **All four paths ship the same hardening recipe** (verbatim): `cap_drop=["ALL"]`, `security_opt=["no-new-privileges:true"]`, `read_only=True`, `user="1000:1000"`, `detach=True`, `tty=True`, `stdin_open=True`, `command=["tail","-f","/dev/null"]`, tmpfs `/tmp=rw,noexec,nosuid,size=64m` and `/home/agent=rw,exec,size=256M,uid=1000,gid=1000`, workspace mount `read_only=(workspace_mode=="ro")`, package-cache volume `tm-packages-<ws_id|default>` → `/home/agent/.local` (`docker_executor.py:618-651,658-681`; mirrored `container_manager.py:764-788`; `container_registry.py:126-144`). The worker/executor create paths add a `/workspace/.git` tmpfs shadow **only when `<ws>/.git` is a directory** (`docker_executor.py:618-624`, applied `:662`; mirror `container_manager.py:668-673`, applied `:769`); the resource (git) container does **not** shadow `.git` — it mounts the workspace RW with the real `.git` (`resource_container_manager.py:1044-1055,1076-1081`, applied `:1120`), plus a linked-worktree main-repo bind (`:1057-1075`).

---

## 2. Lifecycle

A container's life, end to end (with the host-level events the contract asks about):

**Fresh install → no image is built.** `install.sh:1-120` runs only prerequisite doctor checks (Python / Docker daemon / docker-group / venv / Node) and performs **no `docker build`**. `thoughtmachine/bootstrap.py:176-187` `ensure_user_defaults()` seeds only the build *sources* (runtime Dockerfile, git-overlay Dockerfile, pinned requirements; `thoughtmachine/vault.py:265`). Therefore **no image is eagerly built at install**; images are built **lazily on first use** — see §11(e).

**Runtime image.** The executor image `agent-executor-<hash>` is built lazily by `DockerExecutor._ensure_image` (`docker_executor.py:774-810`), which rebuilds when the image is missing or the build-hash label drifts. `ContainerManager.build_image` (`container_manager.py:1286`) is the explicit, vault-gated build. Note `ContainerManager.start`'s create path (`:764-788`) does **not** auto-build — a missing image simply fails the create.

**Resource images.** `tm-workspace-runtime:latest` is the base; `tm-resource-git` is the git overlay (two-stage, built `--build-arg BASE_IMAGE`, `resource_container_manager.py:522-690`). They are built lazily, single-flight, success-cached, and drift-strict by `_ensure_resource_image` (`:522-548`); the registry delegates via `_ensure_resource_image_or_raise` (`container_registry.py:457-490`). First spawn of a resource container triggers the build.

**First spawn (hot path).** An agent tool calls `ContainerManager.start(...)` (`tools/container_control.py:99-134` → `infra/container_manager.py:444`). The manager recomputes the desired config (`_compute_config` `:1706-1721`) and either **reuses** an existing matching container (`:489`) or **creates** (`:562, :764`).

**Naming.** Four schemes (see §1). The legacy `agent-exec-*` and the manager's `agent-exec-*-<session_tag>` share a prefix; the registry uses `tm-user-*`; resource uses `tm-res-*-git`.

**Stopped / removed / GC'd.** `stop` (`container_manager.py:897`) only stops — the container lands in `exited` state. Only the EXITED sweeper removes them, after an idle threshold (§5). `remove` (`:949`) is explicit. `cleanup_workspace` (`:1736-1762`) stops+removes all labelled containers for a workspace.

**Host reboot.** **No container carries a restart policy** — see §5 / §11(f). After a reboot or Docker daemon restart all containers (workspace and resource) remain stopped and are re-created on demand.

**Finding:** The lifecycle performs no preflight host free-disk check, so a full host disk is an unhandled failure mode.

**Host out of disk.** **There is no host free-disk check anywhere in `infra/`** — confirmed by both source and live probe v2. Source: **0** host-free-disk-check patterns (`disk_usage`/`statvfs`/free-space) in `infra/`+`tools/`; the only disk introspection is an in-container `du` (`container_manager.py:1083-1089,1196-1203`), and `disk_quota_mb` governs only the `/home/agent/.local` package-cache volume (the 6 generic `disk_usage` hits measure container-volume usage only). Live: host `/` is **368 G with 20.99 GiB free (94 % used)**. A full host disk is therefore an unhandled failure mode — now a **live-confirmed** gap, not merely a source-level fact.

**Disposition:** CHANGED — DECISION: the precondition moves into the admission gate (move 4); no longer a parking-lot item. Today 94% used / ~21 GiB free.

---

## 3. Limits

**Finding:** The shipped per-workspace container limit is 4 (`DEFAULT_MAX_CONTAINERS`), and the limit axes disagree across paths.

**The contract says "one persistent-container count per workspace, default 6." The source says 4.**

**FINDING (D1).** `DEFAULT_MAX_CONTAINERS=4` (`agent/config/defaults.py:133-143`), mirrored at `infra/container_manager.py:255,270` and `infra/container_registry.py:611-625`, and in the frontend `web_ui/frontend/src/workspaceUtils.jsx:19` (`CONTAINER_LIMIT = 4`, with a "matches the backend" comment). **The value 4 is the only one found; 6 appears nowhere.** Either the brief intends 6 as a *new* default, or the brief is stale. Resolve before any implementation.

**Scope (FINDING D4).** The legacy/manager limit is **per-workspace** (`container_manager.py:547-559`); the registry limit is **per-session** (`container_registry.py:298-303`); the worker default is `WORKER_DEFAULT_MAX_CONTAINERS=4`. These are not the same axis, yet they back the same product claim.

**Enforcement is racy (FINDING D5).** The registry does **check-then-create under a lock, not atomically** (`container_registry.py:298-303`: `current = len(_session_map.get(session_id, ()))` then create). Two concurrent spawns can both observe a free slot.

**Client surface.** `GET /api/workspace/{id}/containers` (`web_ui/backend/server.py:2995`) returns `{containers, containers_in_use, containers_available}`. The value is computed with **two clamps**: `cap = max(1, int(raw_cap))` (`server.py:3020`; `except (TypeError, ValueError)` → `cap = 4`, `:3021-3022`) and `"containers_available": max(0, cap - containers_in_use)` (`server.py:3026`). **Consequence:** `containers_available` can **never be negative** — the outer `max(0, ...)` floors it at 0, so any premise or test that expects a negative "available" is unachievable by construction. *(The prior draft folded the inner `max(1, ...)` default-4 clamp and the outer `max(0, ...)` floor into a single expression, which was wrong.)* Probe v2 could only print the truncated fragment `max(0` (no `--base-url` was supplied, so it emitted expression-level evidence, not live JSON) — this is the one client-surface line the probe did **not** independently verify end-to-end.

**Ephemeral containers.** `DockerCodeRunner` containers are created through the same manager and are **not counted against a separate ephemeral quota** — they are ordinary free-use containers left EXITED (§5, §11(c)). A dedicated "tracked separately / observable" ephemeral counter is not implemented.

**At the limit.** The manager refuses the create and returns an error to the caller (tool surface). The user-facing counter exists only in the ORPHANED frontend tab (§7), so the live UI does not display remaining capacity. *(the exact refusal error text is tool-surface dependent — unverified — needs runtime check.)*

**Disposition:** CHANGED — DECISION: the limit becomes 6, per-workspace, user-configurable; DEFAULT_MAX_CONTAINERS is the single source of truth across all sites, and the worker default unifies with the workspace budget.

---

## 4. Permissions and drift

**The contract says every container "carries a snapshot of the permissions it was created with," drift is computed from that snapshot and surfaced where the user can act on it, and the user decides keep-vs-recreate — "NO SILENT KILLS, EVER." The source does not match this.**

**Finding:** Containers do NOT carry a permission snapshot; drift is recomputed against live Docker state, and on drift the manager silently recreates (contradicting "NO SILENT KILLS, EVER").

**No per-container permission snapshot was found** (*unverified — needs runtime check*). Instead, drift is computed by comparing **live Docker state** (actual `NetworkMode` + `/workspace` mount mode) against a **freshly recomputed** desired config: `_config_matches` (`infra/container_manager.py:1678-1704`) and the integrity recompute (`docker_executor.py:508-557`).

**Recreate is silent.** On drift the manager does stop+remove+recreate with only an audit event and a `WARNING` — **no user confirmation and no "keep" option** (`container_manager.py:519-529,617-624,640-648`; registry `on_permission_changed` `container_registry.py:541-607`). **No UI offers a keep-vs-recreate decision on permission drift** — the only integrity surface is the read-only `GET /api/container/integrity` (`web_ui/backend/server.py:2815-2819`), rendered as the integrity dot in `web_ui/frontend/src/components/ContainerPanel.jsx` (display only); *vault schema* drift is separate (`server.py:508-524,2734-2771`; `VaultHealthPanel.jsx`).

**FINDING (D3):** the shipped behaviour **contradicts "NO SILENT KILLS, EVER."**

**Split-brain (§11(a)).** Two independent config sources-of-truth govern the same network + workspace axes:

- **SSOT#1** — `security/security_gate.py:945-1011` `get_expected_container_config(perms, caps=None)`. With `caps=None` it substitutes a **permissive** `WorkspaceCapabilities()` (`:982-983`); `net = resolve_network_mode(eff["network"])` (`:1002`); `workspace_mode = "rw"` iff fs ∈ (write, full) else `"ro"` (`:1006`); canonical mapper `security/gate_helpers.py:90`.
- **SSOT#2** — `docker_executor.py:176-246` `_compute_container_config_from_permissions`, which recomputes the same axes **fail-closed** (`("none","ro")` on exception `:214-228`; `FALLBACK_NETWORK_RESTRICTION` audit `:229-242`).

`ContainerManager._compute_config` (`:1706-1721`) calls SSOT#1 with `caps=None` (permissive); `verify_container_integrity` calls SSOT#2 (fail-closed, `docker_executor.py:290`). With no capabilities file and `network=outbound`, the tool path yields `bridge` while the session-load integrity check yields `none` — they **disagree**, so the split-brain class persists on **network and workspace_mode**. The registry's `resolve_network_mode` (`container_registry.py:628-635`) and the legacy `DockerExecutor._compute_container_config` (`docker_executor.py:435-451`) both delegate, so there are exactly **two** decision helpers.

**Resolved live (probe v2).** With canonical permissions `{'network':'outbound','filesystem':'write'}`, no per-workspace `capabilities.json`, and a **resolving** workspace_id (`resolve_workspace_id → 'probe-ws'`, `err=None`, **no** "falling back" warning), the two SSOTs were exercised side by side: SSOT#1 `security.security_gate.get_expected_container_config(..., caps=None)` → `(bridge, rw)` while SSOT#2 `docker_executor._compute_container_config_from_permissions(...)` → `(none, ro)`, `diverge=True`. A live container started through `ContainerManager.start()` (no custom name) came up as `agent-exec-9939d92abc3f-anon` with `HostConfig.NetworkMode='bridge'` and `/workspace Mounts[].RW=True` (mode `rw`) ⇒ **the live container matches SSOT#1, the PERMISSIVE source of truth.** Precondition for the divergence to bite: a **permissive `network` permission** AND an **absent per-workspace `capabilities.json`** (`caps=None` ⇒ permissive `WorkspaceCapabilities()` default, `security_gate.py:982-983`). Impact: the creator path uses the permissive SSOT while `verify_container_integrity`'s `desired` is SSOT#2's fail-closed `{'network':'none','mode':'ro'}` (`docker_executor.py:290`) ⇒ **creator and drift checker disagree by construction** ⇒ any container the integrity check sees in a permissive-network workspace is a **guaranteed false drift**.

**Integrity-check blindness — CONFIRMED LIVE (probe v2).** While the live container above existed, `verify_container_integrity(...)` returned `container_exists=False`, `matches_config=None`, `action_taken=none`, `desired={'network':'none','mode':'ro'}`, `actual=None`. Cause: the lookup derived the name `agent-exec-9939d92abc3f` (no `-<session_tag>` suffix) while the live container was `agent-exec-9939d92abc3f-anon`, `equal=False` — the name scheme (§1) is `agent-exec-<sha256(abspath(ws))[:12]>-<session_tag>`. Consequence: the session-load drift check **cannot find hot-path containers**, and even when it finds one its `desired` is the **opposite** SSOT (above).

**mem/cpu axis.** It is not permission-derived and has no recompute helper, but it shows the same class of divergence as **static per-path defaults** (§8); `_config_matches` does not compare mem/cpu, so mem/cpu drift is silently reused.

**Disposition:** SUPERSEDED — TARGET ARCHITECTURE (move 2: one fail-closed config function replaces both SSOTs; move 6: drift becomes discrete recorded events the user decides on, so no silent kills). The interim branch fix/container-permission-snapshot is superseded by feat/drift-events.

---

## 5. GC policy

**Finding:** The EXITED-container sweeper's default max age is 1 hour (`max_age_s=3600`), not the contract's 24 hours.

**The contract says "only stopped containers, after N hours (default 24h)." The source says 1 hour.**

**FINDING (D2).** `sweep_exited_workspace_containers(..., max_age_s=3600)` (`container_manager.py:1846-1985`); env override `THOUGHTMACHINE_EXITED_CONTAINER_MAX_AGE_S`, default `'3600'` (`server.py:374-376`). **24h appears nowhere found.**

**Disposition:** CHANGED — DECISION: 24 h (max_age_s=86400, env default 86400).

**Finding:** The sweeper removes only EXITED containers and always skips resource (and running) containers.

Mechanics: the sweeper removes **only `status=="exited"`** containers past the age (`:1936,1949`). It always **skips resource containers** (`:1921-1925`) and running containers. It skips unparseable/future `FinishedAt` for clock-skew safety (`:1940-1948`). Removal is `force=True` (`:1965`). It is **label-based** (`thoughtmachine.workspace_id`, `:1829,1896`), so it covers BOTH the legacy `agent-exec-*` (which stamps the workspace label, `docker_executor.py:675-680`) and the manager's `tm-user-*` (`container_manager.py:689-693`). **Running containers are never GC'd.** Workspace files persist on disk regardless — the sweeper does not delete host workspace files (`container_manager.py:678`). `registered_workspace_ids==[]` → no-op; `None` → TTL-only. Resource side: `sweep_stale_resource_containers` (`resource_container_manager.py:1896-1954`, only unregistered workspace ids), `prune_unreferenced_resource_images` (`:2020-2098`), `_sweep_orphan_resource_containers` (`server.py:331-366`).

**Disposition:** ADOPTED AS-IS (resource containers are skipped by the workspace sweeper — correct).

Wiring: `_sweep_exited_workspace_containers` (`server.py:379-428`), `_run_container_sweeps` (`:440-453`), `_periodic_container_sweep_loop` (`:456-472`); interval env `THOUGHTMACHINE_CONTAINER_SWEEP_INTERVAL_S` default 300 (`:435-437`); wired in the lifespan (`:583,:590`, task `:595-597`).

**Host-reboot behaviour (see §11(f)).** **No `restart_policy`/`restart=` argument exists in any `containers.run(...)`** (`docker_executor.py:658`, `container_manager.py:764`, `container_registry.py:144`, `resource_container_manager.py:1116`), and there are no compose files. Docker's default is `restart=no`, so after a reboot containers stay stopped and do not auto-restart (code-level fact).

---

## 6. Identity and communication

Containers have names (§1) and, by design, **sticky notes that are NOT Docker labels** — Docker has no label-update API, so notes live on a per-workspace vault "bulletin board" `<vault_root>/workspaces/<workspace_id>/container_notes.json` (`infra/container_manager.py:48-56`; `vault_root` = kwarg → `THOUGHTMACHINE_VAULT_ROOT` → `~/.thoughtmachine`). Load `:257-259`; path/read `:364-401`.

**Save (`_save_container_notes`, `:403-417`) DOES create its own parent directory.** L404 docstring "Atomically persist the bulletin board; NEVER raises"; L405 `notes_path = self._notes_path()`; L406 `try:`; **L407 `notes_path.parent.mkdir(parents=True, exist_ok=True)`**; L408 `tmp_path = notes_path.with_suffix(".json.tmp")`; L409 `with open(tmp_path, "w", encoding="utf-8") as f:`; L410 `json.dump(...)`; L411 `os.replace(tmp_path, notes_path)`; L412 `except (OSError, ValueError)` → WARNING L413-414 (blame origin `3067fe39`). **Any claim that a save fails to create its parent is WRONG** — L407's `mkdir(parents=True, exist_ok=True)` self-heals a missing parent (live-confirmed below). Written on fresh create (`:535-537`) and on reuse (`:603-608`); read by `list_containers` (`:1281-1282`). The legacy path stamps an **always-empty** `thoughtmachine.note` label (`docker_executor.py:675-680`).

**Finding:** The notes file's temp name is a fixed, shared path opened without `O_EXCL`, and the read-modify-write is unlocked — concurrent saves lose updates.

**REAL notes defect — CONFIRMED LIVE (probe v2).** The "atomic via tmp + `os.replace`" framing hides **two distinct races**: (i) the temp filename is a **fixed, shared** per-workspace name (`tmp_path = notes_path.with_suffix(".json.tmp")` → `container_notes.json.tmp`, identical for every writer) opened `"w"` with **no exclusive create** (no `O_EXCL`), and (ii) the read-modify-write cycle around it is **unlocked**. Live **phase A** (parent dir EXISTS; 3 forked processes × 8 non-dry writes; 0.064 s): expected 24 notes, **5 surviving, LOST_UPDATES=19**; `os.replace` was called 24 times with **8 FAILED** (source tmp already renamed away by another writer), all `ENOENT` — first message `FileNotFoundError: [Errno 2] No such file or directory: '…/container_notes.json.tmp' -> '…/container_notes.json'`; per-process replace_fails **3/4/1**. A **second, independent** mechanism is visible in the `_diag_` traces: a **stale in-memory snapshot clobber** — the surviving key count moved 8 → 1/2 mid-run (proc0 iter6 nkeys=8, then proc1 iter4 nkeys=1) because each writer dumps back its whole stale dict. Live **phase B** (single writer, parent MISSING) shows the mkdir self-heal: `parent existed_before=False → after=True`, **8/8 surviving, SILENT_WRITE_FAILURES=0**. The two headline numbers must **not** be conflated: (a) `lost_updates(parent EXISTS)=19` versus (b) `silent_write_failures(parent MISSING)=0`. Additional live corruption evidence: one `Failed to read container notes … Expecting value: line 1 column 1 (char 0)` plus 8 real `Failed to write container notes` WARNINGs ⇒ a non-exclusive truncating open can publish a partial document under interleaving.

**Agent surface.** Notes are readable/writable **only** through `ContainerStartTool.note` (`tools/container_control.py:192-195,:222,:236`) and readable via `ContainerListTool` (`:456`). `ContainerManager.set_note` exists but is **not** exposed as an agent tool. There are **no rename / claim / handoff / release tools.** The only "claim" concept is worker teardown reclaiming containers by exact `thoughtmachine.worker` label (`tools/workspace/worker_container.py:22-30`; `tools/docker_code_runner.py:188-195`; `ContainerStartTool.worker_name` `:206-213`). ⇒ the contract's "hand off containers between sessions, mark containers free" tool surface is **not implemented**.

**Disposition:** SUPERSEDED — TARGET ARCHITECTURE (move 1: notes become a field in the Container Record; move 7: fix/notes-as-record-field). Ownership: free-use containers are workspace-owned, resource containers are system-owned; ContainerManager.set_note is exposed as an agent tool; rename/claim/handoff/release are Phase 3.

---

## 7. UI surfaces

Three surfaces exist. The full lifecycle REST API (§9) is present on the backend, but the **live UI uses none of it except one button.**

- **Live workspace page (read-only).** `web_ui/frontend/src/components/workspace/WorkspaceDetailPage.jsx` (route `App.jsx:46,1317`); its embedded `ContainersTab` (`:297-347`) shows a Dockerfile card plus active containers (name, type badge, status, id, workspace_id) — **no actions, no note, no mem/cpu, no limit counter** (`containerCount :554`; header chip `:665`; overview stat `:749-750`; `<ContainersTab summary>` `:766`). Data from `workspace_routes.py:1646-1768` (`active_containers :1729,1765`) via `_containers_for_workspace()` `:1608-1644` → `ContainerManager.list_containers()` `:1626`.
- **Live session "Container Sandbox" panel.** `web_ui/frontend/src/components/ContainerPanel.jsx`; polls `/api/container/status` + `/api/container/integrity` every 5s; renders a status dot (running/stopped/building/error/unavailable), an integrity dot, the image tag, a `"Capabilities: Coming soon…"` placeholder, the build log, and **one** `"Rebuild Container"` button → WS `rebuild_container` (`:47-52,64-125,133-175`). No list, no note, no limits.
- **Global read-only.** `web_ui/frontend/src/components/GlobalContainers.jsx:2` (name, free_use/resource badge, workspace_id, status) via `global_routes.py:237`.
- **ORPHANED.** `web_ui/frontend/src/components/workspace/tabs/ContainersTab.jsx:1` carries an "ORPHANED — … do not import in new code" banner. It is the only place with Start/Stop/Remove/Logs (`:50-63`), a Note column (`:37,:49`), and a limit counter (`:24`), and it filters names by `'tm-resource-'` (`:13`) whereas the real prefix is `tm-res-` (`resource_container_manager.py:938-947`) — a dead-code mismatch. `workspaceUtils.jsx:19` (`CONTAINER_LIMIT=4`) is consumed only by this orphaned tab. `modals/ContainerLogsModal.jsx:8-10` is a placeholder.

**FINDING (UI gap).** Everything the contract asks for (notes visible, controls, limit counter, actions) lives in orphaned/placeholder components; the live UI is read-only plus a single rebuild button. Whether this is intentional is unresolved.

---

## 8. Resource allocation

**Finding:** Memory/CPU/OOM defaults disagree by create path, and there is no user-facing mem/cpu configuration surface.

Axes: **memory (`mem_limit`), CPU (`cpu_quota`), and OOM adjustment** only. There is **no `pids_limit` and no `nano_cpus` anywhere** in `infra/` (grep).

Defaults disagree by path:

- `ContainerManager`: `mem_limit="1g"`, `cpu_quota=100000` (`container_manager.py:219-220`, stored `:234-235`); create sets `oom_score_adj=1000` (`:773`).
- `ContainerProfile`: also 1g/100000 (`container_registry.py:91-110`, `__post_init__ :114-123`, `create_hardened_container :126-163`).
- Resource variant: hardcodes **512m / 50000 / oom 500** (`:419-425`).
- Agent tool path: defaults **512m / 50000** (`tools/container_control.py:196-199` + `_make_manager :130-131`).
- REST start: passes no mem/cpu (`server.py:3089`), so it gets 1g/100000.

**FINDING (D-mem/cpu):** the agent tool default (512m/50000) and the REST default (1g/100000) disagree for the same container class.

**Configuration.** The contract says user-configurable; the concrete per-workspace knobs found are `DEFAULT_MAX_CONTAINERS` and the worker/registry constants (`agent/config/defaults.py:133-143`). A user-facing mem/cpu configuration surface was not found (*unverified — needs runtime check*).

**Enforcement — CONFIRMED LIVE (probe v2).** A container started with the limits carries them in `HostConfig.Memory=268435456` and `HostConfig.CpuQuota=50000` (inspect), and the same limits appear **inside** the container's cgroup v2 filesystem: `memory.max:268435456`, `cpu.max:50000 100000`, the v1 files (`memory.limit_in_bytes`, `cpu.cfs_quota_us`) empty, `CGROUP_VER=v2` ⇒ **real cgroup enforcement confirmed on this host.** *(Probe caveat: probe 5's own verdict line wrongly says it "could not statically confirm the ignored-field set", yet its printed excerpt **is** the confirmation — a live `HostConfig` with `Memory=134217728`, `CpuQuota=25000`, and `OomKillDisable=null` present but never compared. Quote the numbers, not the verdict line.)*

**Disk is out of scope.** There is no host-disk enforcement; `disk_quota_mb` governs only the `/home/agent/.local` package-cache volume; the workspace is a bind mount and `/tmp`+`/home/agent` are tmpfs (64M / 256M). Any disk story is deferred.

**Registry feature flag — CONFIRMED LIVE (probe v2).** Registry activation is derived from `session_config["use_container_registry"]` and reported consistently by two flag helpers: `is_registry_active` (`infra/registry_wiring.py:55`) and `is_container_registry_enabled` (`infra/container_registry.py:663-667`, `:667 return bool((session_config or {}).get("use_container_registry", False))`) both return **True / False / False** for configs **True / False / `{}`**. **Latent trap:** `get_active_registry` (`infra/registry_wiring.py:29`) returns a real `ContainerRegistry` object for **all three** configs — including `False`, where it builds a disabled, docker-free registry — so callers must gate on `is_registry_active` / `is_container_registry_enabled`, **never on the truthiness of the registry object**.

**Disposition:** ADOPTED AS-IS (live-confirmed); mem/cpu/oom drift handling is covered by the §4 disposition. *(Probe-v2 pin note: the probe listed `registry_wiring.py:27`/`:53` and `container_registry.py:661`/`:668`; the read-verified lines are `registry_wiring.py:29`/`:55` and `container_registry.py:663-667` — cited here after re-reading.)*

---

## 9. Network model

**Finding:** Worker/runtime containers honour the workspace `network` permission (`bridge` iff the value ∈ {True,"write","outbound"}, else `none`); resource (git) containers default to `"none"`.

**Worker/runtime containers honour the workspace `network` permission.** The desired `network_mode` is derived once by `security/gate_helpers.py:90` `resolve_network_mode` → `"bridge"` **iff** the value is in `{True,"write","outbound"}`, else `"none"`, reached end-to-end via `security_gate.get_expected_container_config:945-1011` (net `:1002`) → `docker_executor.py:176-246` / `ContainerManager._compute_config:1718-1721` → create (`container_manager.py:764-788`; `docker_executor.py:658-681`; registry `container_registry.py:144-165`/`:628-635`).

**Resource (git) containers use a different path** — system-hosted, not free-use. The caller `tools/git_info_tool.py:843-859` computes net via `get_expected_container_config(session_permissions, None)` and passes it in; `ResourceContainerManager` defaults to `"none"` (`resource_container_manager.py:845`, applied `:1102,:1121`); the registry resource path also defaults `"none"` fail-closed (`container_registry.py:380`). The agent cannot create/exec/stop them (guards §1).

**LOAD-BEARING NEGATIVE — there is no comm gate.** **No comm-gate / egress-allowlist / message-bus module exists.** Dir-scoped greps (`comm[_-]?gate|egress|message[_ ]?bus|allowlist|proxy`, case-insensitive, `*.py`) return **0 matches in `infra/` (7 files)** and only the incidental word "regression" in `security/` (`security_gate.py:832`). Critically, **`domain_allowlist` appears nowhere in `infra/` (0/7) or `security/` (0/5)** — the per-workspace domain allowlist is stored/CRUD'd/surfaced (`web_ui/backend/workspace_routes.py:388-411`, model `:260`; `tools/check_system.py:14,486,499,538`) but **never enforced** in any container-run or egress path. Other `allowlist`/`egress` hits are unrelated: git-protocol allowlist (`tools/git_write_tool.py:14,69,177,352,393,664`); a check-system query allowlist + a manually-triggered egress diagnostic (`tools/check_system.py:98-103,190-193,235-252,608,645-664`); `network:outbound` gating for MCP (`tools/mcp_manager.py:223-227` / `mcp_validator.py:33-38`).

**Nearest surrogates, all weaker than a real gate:** (1) the binary `network_mode` switch; (2) the unenforced `domain_allowlist.json`; (3) the agent-triggered egress probe (`check_system.py:608,645-664`, diagnostic only). ⇒ **the Comm Gate is a design DEPENDENCY absent from code.**

**Live confirmation (probe v2).** Under `network_mode='none'` an in-container HTTP call is blocked (`HTTP_NET_BLOCKED: URLError`) while the **same** call under a bridge control container succeeds (`HTTP_NET_OK`) ⇒ the binary switch is real at runtime, not merely in config. `resolve_git_execution_mode`'s **five-input table is unchanged** (`containerized` / `host_fallback` / `host_fallback` / `host_fallback` / `unavailable` across the probed inputs). Both images are present; in-container **local** git succeeds from the resource image (`LOCAL_GIT_OK`, commit `0a1a596`); a network-requiring git op fails in-container (`NET_GIT_EXIT=128`, `fatal: … Could not resolve host: github.com`) with **no silent host fallback**.

**Bug-4 implication.** Local git works inside a `'none'` container; network-requiring git is **blocked** in the sandbox (non-zero exit, **no silent host fallback**); host fallback happens only on container-INFRA outage, is loudly surfaced, and is gated fail-closed (see §11(b)).

**Disposition:** ADOPTED AS-IS

---

## 10. Repair tools

**What exists today (agent-reachable).** `tools/container_control.py`; base `_ContainerControlBase.required_categories=["container:true"]` (`:94-97`); `ContainerExecTool` overrides `["container:true","filesystem:write"]` (`:277`); all others `["container:true"]`.

| Tool class | declaration | `execute()` |
|---|---|---|
| `ContainerStartTool` | :143 | :215 (image / name / note / mem_limit=512m / cpu_quota=50000 / worker_name :182-213) |
| `ContainerExecTool` | :249 | :303 |
| `ContainerStopTool` | :348 | :373 |
| `ContainerStatusTool` | :396 | :424 |
| `ContainerListTool` | :436 | :466 |
| `ContainerBuildTool` | :487 | :515 (vault-gated, Dockerfile-only build, `tag`; **no `--no-cache`**) |
| `ContainerLogsTool` | :535 | :572 |
| `ContainerRemoveTool` | :598 | :624 |

`_make_manager:99-136` builds `ContainerManager(..., mem_limit="512m", cpu_quota=50000)` and refuses a cwd fallback (`:118-123`). All 8 are registered in `tools/__init__.py:150-170`. For workers they are **permission-gated only, not hard-blocked** (`tools/workspace/worker_thread.py:597-603`).

Also present but **NOT agent-exposed**: the REST lifecycle (`server.py` — see §9 list: GET containers/status/logs, POST start/stop, DELETE) and internal/startup functions — `docker_executor.verify_container_integrity:249` (called at server startup `web_ui/backend/server.py:553-576`; on config-change re-sync `web_ui/backend/bridge.py:1433`; on session load `agent/presenter/session_lifecycle.py:329` and `:385`; and read-only via REST `GET /api/container/integrity` `web_ui/backend/server.py:2815-2819`), `docker_executor.rebuild_container:984` (background `--no-cache`, `_background_build_results :979-982`), `ContainerManager.build_image:1286`, `stop:897`, `remove:949`, `cleanup_workspace:1736`, `sweep_exited_workspace_containers:1846`, `ResourceContainerManager.stop:1450`, `remove:1470`, `cleanup_workspace_resources:1788`, `sweep_stale_resource_containers:1896`, `prune_unreferenced_resource_images:2020`, and the server sweeps (§5). The WS command `rebuild_container` (`server.py:2456-2466`) is UI-only.

**Missing-list assessment.**

| Candidate capability | Verdict | Evidence |
|---|---|---|
| (i) Rebuild image | **PARTIAL** | `ContainerBuildTool` (vault Dockerfile-only, no `--no-cache`) + WS `rebuild_container` (UI-only, background `--no-cache`) + `DockerExecutor._ensure_image` (`:774`, hash-label drift, internal). No agent surface for a forced/`--no-cache` rebuild or an image-staleness report. |
| (ii) Reset / recreate container | **GENUINELY MISSING** | No reset tool; the agent must remove+start manually. The reuse path `_config_matches` checks ONLY network + workspace-RW (`:1678-1704`), so mem/cpu/oom drift does NOT force a recreate. |
| (iii) Check container health/integrity | **EXISTS, NOT AGENT-EXPOSED** | `verify_container_integrity` (`docker_executor.py:249`) runs at server startup (`web_ui/backend/server.py:565`), on config-change re-sync (`web_ui/backend/bridge.py:1433`), on session load (`agent/presenter/session_lifecycle.py:329` and `:385`), and read-only via REST `GET /api/container/integrity` (`web_ui/backend/server.py:2815-2819`), but is **never exposed as an agent-callable repair tool** — the agent gets only `Status`/`List`. **Live-confirmed blind (probe v2):** its lookup drops the `-<session_tag>` suffix, so against a live `agent-exec-9939d92abc3f-anon` it looked up `agent-exec-9939d92abc3f`, saw `container_exists=False`/`actual=None`, and returned `desired={'network':'none','mode':'ro'}` — it is both **blind to hot-path containers** and, if it did find one, computes the **opposite** desired config from the creator (§4). |
| (iv) Reclaim orphaned ephemerals | **EXISTS, NOT AGENT-EXPOSED** | `sweep_exited_workspace_containers` (`container_manager.py:1846`), `sweep_stale_resource_containers` (`resource_container_manager.py:1896`), `prune_unreferenced_resource_images` (`:2020`), `cleanup_workspace` (`:1736`) — invoked only from the server lifespan/periodic loop. |
| (v) Inspect config-vs-desired drift | **MISSING (agent) AND INCOMPLETE** | Only `_config_matches` (network+workspace-RW only, `:1678-1704`) + `verify_container_integrity`; no drift-report tool. Net is derived from `network` only, not from `container`. |

---

## 11. What we don't know

Each item states the code-level verdict and, separately, what still needs a runtime check.

**(a) Network SSOT split-brain class — CONFIRMED BY READING (code) AND LIVE.** Two helpers decide the same network + workspace axes: `get_expected_container_config` (`security/security_gate.py:945-1011`, permissive when `caps=None` `:982-983`) and `_compute_container_config_from_permissions` (`docker_executor.py:176-246`, fail-closed). `ContainerManager._compute_config` uses the former (`container_manager.py:1706-1721`) while `verify_container_integrity` uses the latter (`docker_executor.py:290`), so they can disagree. The class also exists on mem/cpu as static per-path defaults (§8). **The live winner is now settled (probe v2): the creator path — SSOT#1, the permissive source — wins.** A live `ContainerManager.start()` container came up `bridge`/`rw` matching SSOT#1 while `verify_container_integrity` computed the opposite `{'network':'none','mode':'ro'}` (§4).

**Disposition:** SUPERSEDED — TARGET ARCHITECTURE (move 2: the two SSOTs collapse into one fail-closed config function; SSOT #2 wins, caps=None never permissive).

**(b) Bug 4 — git containerized fallback — CONFIRMED BY READING (code) AND LIVE.** `resolve_git_execution_mode` (`tools/git_info_tool.py:16-43`) and `_resolve_resource_execution` (`:799-859`) degrade to host only when the mode is `host` / no workspace id; `ResourceContainerManager.ensure_resource` returns `host_fallback` only on container-INFRA outage (image/build/Docker/start failure, `~:1222-1350`) — **no branch falls back for missing network**. Under `network_mode='none'`, a network-requiring git op exits non-zero inside the container; any host fallback is loudly surfaced and gated (`_host_execution_denied_reason()`, fail-closed). **CONFIRMED LIVE (probe v2):** under `network_mode='none'` the network op fails in-container (`NET_GIT_EXIT=128`, `Could not resolve host: github.com`) with **no silent host fallback**, while the bridge control reaches the network (`HTTP_NET_OK`). `docs/container_p0_bug4_git_fallback.md` was inferred-static; its conclusion is now live-confirmed.

**Disposition:** ADOPTED AS-IS

**(c) `DockerCodeRunner` ephemerals — CONFIRMED BY READING (code).** `tools/docker_code_runner.py:389-425` does start→exec→`finally: manager.stop(...)`; `stop` (`container_manager.py:897`) only stops, so **each call leaves an EXITED container that accumulates**. They carry the `thoughtmachine.workspace_id` label (`:689-693`), so the label-based sweeper DOES reach them — they are the **same leak vector as workspace containers, not a separate one**. **No `tm-p0-*` prefix logic exists anywhere** in the current tree; the sweeper is label-based. (The spec's "tm-p0-* prefix logic" is a historical/renamed detail; confirming via git history is out of scope here.)

**Disposition:** ADOPTED AS-IS

**(d) Resource container network today — CONFIRMED BY READING (code) AND LIVE.** Git resource containers run on the network mode supplied by the caller, **default `"none"`** (`resource_container_manager.py:845`, applied `:1102,:1121`; `container_registry.py:380` fail-closed). So "git-in-container is non-functional / everything host-fallbacks" is **false**: local git works in-container; network-requiring git is blocked in-container (non-zero, no host fallback). **CONFIRMED LIVE (probe v2):** from the resource image under `network_mode='none'`, local git succeeds (`LOCAL_GIT_OK`, commit `0a1a596`) while a network git op fails (`NET_GIT_EXIT=128`); both images are present.

**Disposition:** ADOPTED AS-IS

**(e) Fresh-install image build — CONFIRMED BY READING (code): NO eager build; ALL LAZY.** `install.sh:1-120` runs only prerequisite checks (no `docker build`); `bootstrap.py:176-187` seeds only build *sources*. `agent-executor-<hash>` is built lazily by `docker_executor._ensure_image:774-810`; `tm-workspace-runtime`/`tm-resource-git` are built lazily on first git use (`resource_container_manager.py:522-548`). Whether lazy auto-build runs **reliably** on a truly fresh machine (registry egress during first use, base-image availability, build-context perms) is *unverified — needs runtime check*. The launcher work is confirmed for vault creation (bootstrap seeds build files) but **not** for image warming (no evidence in `install.sh`). *(Probe v2 added: `install.sh` has **0** `docker build` occurrences, and `tm-workspace-runtime:latest` is present in `docker images`. Caveat: probe 7 printed `image tm-resource-git listed=False` while `docker images` shows `tm-resource-git:latest` — a tag/substring wart; its dry-run `present=False` lines merely reflect the dry run skipping the image listing.)*

**Disposition:** DEFERRED — PARKING LOT (the code-level fact is adopted — no eager build, everything lazy — but the fresh-machine reliability check on a true fresh install remains unverified and unscheduled).

**(f) Host reboot / restart policy — CONFIRMED BY READING (code): NO restart policy exists anywhere.** No `restart_policy`/`restart=` at `docker_executor.py:658`, `container_manager.py:764`, `container_registry.py:144`, `resource_container_manager.py:1116`; no compose files. Docker default `restart=no` → after a reboot all containers (workspace and resource) stay stopped and do not auto-restart; they are re-created on demand. Whether they *should* auto-restart is a design decision for §5.

**Disposition:** DEFERRED — DESIGN (target architecture §6; unless-stopped for persistent, no for ephemeral, drift blocks auto-restart)

**Resolved by the v2 live probe:**

- **(C) Split-brain winner** — the live creator path (`ContainerManager.start()`) matches SSOT#1 (permissive, `bridge`/`rw`); `verify_container_integrity` computes the opposite fail-closed `{'network':'none','mode':'ro'}` (§4).
- **(D) Integrity-check blindness** — `verify_container_integrity` returned `container_exists=False`/`actual=None` against a live container because it looked up `agent-exec-9939d92abc3f` while the container was `agent-exec-9939d92abc3f-anon` (`equal=False`); it is blind to hot-path containers and computes the opposite `desired` (§4, §10-iii).
- **(E) `container_notes.json` races** — `_save_container_notes` DOES mkdir its parent (L407), so parent-missing writes succeed (SILENT_WRITE_FAILURES=0); the real defect is a shared, non-exclusive temp file plus an unlocked read-modify-write, yielding `LOST_UPDATES=19` of 24 under 3 processes and a partial-read/write corruption (§6).
- **(F) Host disk** — host `/` is 368 G with 20.99 GiB free (94 % used) and there is no host-free-disk precondition check (`infra/`+`tools/`: 0 matches), so host-full remains an unhandled failure mode (§2).
- **(G) mem/cpu enforcement** — real cgroup v2 enforcement confirmed (`memory.max:268435456`, `cpu.max:50000 100000`; `HostConfig.Memory=268435456`, `CpuQuota=50000`) (§8).
- **(H) Network + git** — HTTP blocked under `none` (`HTTP_NET_BLOCKED: URLError`) and OK under bridge (`HTTP_NET_OK`); local git succeeds in-container (`LOCAL_GIT_OK`, commit `0a1a596`), network git fails (`NET_GIT_EXIT=128`) with no silent host fallback (§9).
- **(Earlier, by reading) Sweeper invocation site** — startup wiring `web_ui/backend/server.py:583-598` (`_sweep_orphan_resource_containers()` `:583`, `_sweep_exited_workspace_containers()` `:590`, asyncio task `:595-598`), periodic loop `:456-472` interval default 300 s (`:435-437`). No session-load entry point exists.

**Disposition:** ADOPTED AS-IS — the probe-settled items above are adopted as the record; no further runtime check is required.

**Additional open items:**

- **mem/cpu/oom drift does not force a recreate** — `_config_matches` compares only network + workspace-RW (`container_manager.py:1678-1704`) *confirmed by reading*; the runtime consequence (a container silently keeping stale limits) is *unverified — probe 5's ignored-field check was inconclusive* (its verdict line contradicts its own printed excerpt; §8).

**Disposition:** ADOPTED AS-IS (mem/cpu/oom drift handling is covered by the §4 disposition).
- **`container_notes.json` concurrent write safety — now CONFIRMED (was open, §6).** No longer "unverified": 24 concurrent unlocked RMW saves produced 19 lost updates (5/24 surviving), 8 `os.replace` failures (all `ENOENT`, source tmp already renamed away), a stale-snapshot clobber (key count 8 → 1/2 mid-run), and a partial-read/write corruption. The "atomic via tmp + `os.replace`" wording is thin: the temp name is fixed/shared and non-exclusive, and the RMW is unlocked.

**Disposition:** SUPERSEDED — TARGET ARCHITECTURE (move 1: notes become a field in the Container Record; move 7: fix/notes-as-record-field); see §6.
- **Registry `get_active_registry` truthiness trap (new, §8).** `get_active_registry` returns a `ContainerRegistry` object even when `use_container_registry` is `False`, so callers must gate on `is_registry_active`/`is_container_registry_enabled`. Whether any shipped caller tests the object's truthiness directly is *unverified — needs read/runtime check*.

**Disposition:** DEFERRED — PARKING LOT (unverified whether any shipped caller gates on the object's truthiness)
- **`ContainerManager` construction hard-requires a live Docker daemon (operability fact).** `infra/container_manager.py:259 self.container_notes = self._load_container_notes()` runs first and L261 `self.client = docker.from_env()` follows; constructing daemonless aborts with `DockerException: Error while fetching server API version … FileNotFoundError`. Only the socket connect is attempted — nothing is started or removed.

**Disposition:** DEFERRED — PARKING LOT for now; it belongs in the admission-gate design (move 4)

- **Comm Gate enforcement.** No comm-gate / egress-allowlist / message-bus module exists; `domain_allowlist.json` is stored but never enforced (§9).

**Disposition:** DEFERRED — PARKING LOT (domain_allowlist.json UI gets a "not yet enforced" label)

- **Vault-path sweep.**

**Disposition:** DEFERRED — PARKING LOT (into the chore/config-reader-ssot family)

**Evidence provenance and probe caveats (v2).** run_id `20260912-030511-0a15`; host `jojo-Swift-SFG16-71`; docker `29.1.3`; cgroup v2; image `tm-workspace-runtime:latest`; probe cleanup reported `Containers cleaned: 5`. Probe script hashes: v1 `f898502408d281e52e9e08866c18338de062e611d943479423723cffe1788df1`; v2 `b0e203bfa2221d1ad5aa7b73fb509ef5f79984ee6b195fc2379165b224aff542`. Artefacts (scripts + result JSON) live under `working_docs/`, which is gitignored (`.gitignore:160`), so they cannot be committed — this document is the evidence of record. The **v1 results JSON was overwritten by a v2 dry run** (the probe writes `--json-out` by default), so v1's surviving evidence is its stdout in the session record only. Known probe warts, flagged rather than silently trusted:

- **Probe 6** printed only the truncated fragment `max(0` for the `containers_available` expression (no `--base-url` supplied); the two-clamp source read (§3) is authoritative.
- **Probe 8** counter (ii) "save exceptions by errno" recorded `{}` despite 8 real `Failed to write container notes` WARNINGs; counter (iii) — 8 `ENOENT` renames — is the authoritative count.
- **Probe 7** printed `image tm-resource-git listed=False` while `docker images` shows `tm-resource-git:latest` (tag/substring wart); its dry-run `present=False` lines merely reflect the dry run skipping the image listing.

---

## Decisions Recorded

One line per decision; each cross-references the finding section that carries it.

1. Container limit is **6**, per-workspace and user-configurable; `DEFAULT_MAX_CONTAINERS` is the single source of truth across all sites → §3.
2. GC age is **24 h** (`max_age_s=86400`, env default `86400`) → §5.
3. No-silent-kills becomes discrete drift events the **user** decides on → §4.
4. The two SSOTs collapse to one fail-closed config function — SSOT #2 wins, `caps=None` never permissive → §4.
5. Restart policy `unless-stopped` for persistent / `no` for ephemeral → DEFERRED — DESIGN (§11).
6. Notes-race fix direction: unique per-writer temp + `O_EXCL` + a real lock → §6.
7. Integrity-check blindness — CONFIRMED live by probe v2 → §4.
8. The split-brain (two competing config sources-of-truth) → §4.
9. Ownership: free-use containers workspace-owned / resource containers system-owned; `set_note` exposed as an agent tool → §6.
10. Comm Gate parking lot + "not yet enforced" UI label on `domain_allowlist.json` → §11.
11. Host disk moves into the admission gate (move 4) → §2.
12. `ContainerManager.__init__` daemon precondition — parking lot for now / admission gate (move 4) → §11.
13. Vault-path sweep deferred into the `chore/config-reader-ssot` family → §11.

---

## Revision log

Permanent corrections folded into this revision (from the v2 live probe and re-verification against source):

1. **`_save_container_notes` DOES create its parent directory** (`infra/container_manager.py:407` → `notes_path.parent.mkdir(parents=True, exist_ok=True)`). Any "no parent dir" / silent-write-failure claim is **disproven**: with the parent missing, a single writer self-heals it and 8/8 notes survive (SILENT_WRITE_FAILURES=0). The genuine defect is elsewhere (shared fixed temp name, no `O_EXCL`, unlocked RMW).
2. **§3 availability is double-clamped** — `cap = max(1, int(raw_cap))` (`web_ui/backend/server.py:3020`) and `"containers_available": max(0, cap - containers_in_use)` (`:3026`), so `containers_available ≥ 0` by construction; the prior single-expression formula was wrong.
3. **Registry pins verified** — `is_registry_active` (`infra/registry_wiring.py:55`), `get_active_registry` (`infra/registry_wiring.py:29`), `is_container_registry_enabled` (`infra/container_registry.py:663-667`). `get_active_registry` returns a `ContainerRegistry` object **even when the flag is `False`**, so callers must never gate on the object's truthiness. *(Probe-v2 pins 27/53/661/668 were 2 lines off; these are the read-verified lines.)*
4. **Integrity-check blindness CONFIRMED live** — the lookup name omits the `-<session_tag>` suffix (existing `agent-exec-9939d92abc3f-anon` vs looked-up `agent-exec-9939d92abc3f`, `equal=False`); it is blind to hot-path containers and computes the opposite `desired` from the creator.
5. **Host `/` is 94 % used** (368 G / 20.99 GiB free) with **zero** host-disk precondition checks in `infra/`+`tools/`.

6. **Revision 2026-09-12 — findings/dispositions restructure.** Every numbered finding section (§2, §3, §4, §5, §6, §8, §9, §11) now carries a `**Finding:**` label (the existing statement of code behaviour, left intact) and one `**Disposition:**` line per distinct finding, drawn from a fixed vocabulary: `ADOPTED AS-IS`, `CHANGED — DECISION:`, `FIX — PHASE 4 BRANCH:`, `DEFERRED — PARKING LOT`, `DEFERRED — DESIGN`, `SUPERSEDED — TARGET ARCHITECTURE`. The stale tail that deferred the open decisions was replaced by a flat `## Decisions Recorded` index (13 one-line decisions, each cross-referenced to its finding section). A top-of-doc cross-reference to the companion **Target Architecture** document was added. The `SUPERSEDED — TARGET ARCHITECTURE` disposition value is introduced here. No prior evidence, `file:line` pin, number, or quoted string was altered or duplicated, and sections were not renumbered.
