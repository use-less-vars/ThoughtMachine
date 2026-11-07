# Security Audit: Resource Container Isolation (Worker Access)

- **Date:** 2026-08-13
- **Branch / commit:** `feat/workspace-panel` @ `0b43e45` (working tree, git clean); historical deltas verified via `git show 663eb62` (`feat/sec-rce-upgrade`)
- **Scope:** Can a worker agent reach the git **resource container** (`ResourceContainerManager` / `ContainerRegistry.create_resource_container`)? Covers the worker tool blocklist, git tool routing, the resource-container guard, lifecycle/reuse, sandbox boundary, and real-Docker E2E coverage.
- **Method:** Read-only code audit of the working tree @ `0b43e45`; every file:line verified by reading the code. No containers were created. Report is gitignored.
- **Test suite:** `python3 -m pytest -q` -> **1414 passed, 38 skipped, 20 warnings** (skips = `@pytest.mark.docker` / daemon-dependent modules).
- **Threat model:** A worker (or a malicious repo whose hooks a worker's git operations trigger) attempts to create/inspect the resource container, escape it, exfiltrate vault secrets, reach the Docker daemon, mutate the host repo, or exhaust host resources.

Legend: **PASS** = control present and verified; **GAP** = control absent/weaker than ideal (severity + evidence); **UNKNOWN** = not determinable from repo code.

---

## 1. Worker tool blocklist

| # | Question | Finding | Evidence (file:line) | Verdict |
|---|---|---|---|---|
| 1.1 | Which container-control tools exist? | `DockerCodeRunner` + `ContainerStartTool`/`ExecTool`/`StopTool`/`StatusTool`/`ListTool`/`BuildTool`/`LogsTool` (per-session container lifecycle; `ContainerExecTool` adds `filesystem:write`) | `tools/docker_code_runner.py`; `tools/container_control.py:1-30,131-220,245-260`; registered `tools/__init__.py:67-70,81-97` | PASS |
| 1.2 | Which are blocked for workers? | **None.** `_WORKER_BLOCKLIST = {Worker, EditDockerfile, MCPValidator, CheckSystem, KnowledgeBaseTool}` — no container tool is on it; all remain permission-gated only | `tools/workspace/worker.py:396-401` | **GAP — LOW/MEDIUM (G12)** |
| 1.3 | Where is the blocklist enforced? | Two choke points: (a) `_build_agent_config` filters worker tools by the blocklist then intersects with parent `enabled_tools`; (b) spawn-time footprint validation marks blocklisted tools `missing_tools` and re-runs `check_required_categories(..., is_worker_context=True)` | (a) `tools/workspace/worker.py:953-964` (`:961-963`); (b) `:2477-2508` (`:2482-2483`,`:2503`) | PASS |
| 1.4 | Can a worker call container start/stop/create/remove tools? | Yes, **iff** effective perms satisfy `container:true` (exec additionally `filesystem:write`). Worker asks are hard-denied: `if event_bus is None or is_worker_context or isinstance(event_bus, NullEventBus): return False` — no interactive user exists for approval. WLM worker feature is feature-flagged OFF by default | `security/security_gate.py:329-425` (`:339`,`:399-407`,`:417-418`,`:420-425`); `tools/container_control.py:85,250`; `tools/docker_code_runner.py:47`; `tools/workspace/worker.py:758-767` | PASS (permission-gated; see G12) |
| 1.5 | Blocklist effective for all paths incl. aliases/legacy names? | Both enforcement points filter the worker tool list itself (class-name based; no alias mechanism found in worker tool loading), so aliases would still resolve to a blocklisted class and be stripped; the parent-enabled intersection is a second filter | `tools/workspace/worker.py:953-964,2477-2508` | PASS |
| 1.6 | Is the parent/default `enabled_tools` configuration inspected? | Yes — worker tools are intersected with the parent's `enabled_tools` (`[t for t in enabled_tools if t in parent_enabled_tools]`), and defaults flow through the same build path | `tools/workspace/worker.py:961-963` | PASS |

**Section verdict: PASS with 1 LOW/MEDIUM gap (G12).** The five highest-privilege tools are hard-blocked at config-build AND spawn time; container tools are available only under `container:true` grants, and any worker-level ask is denied outright.

---

## 2. Git tools for workers

| # | Question | Finding | Evidence (file:line) | Verdict |
|---|---|---|---|---|
| 2.1 | Where do worker git operations execute? | `GitInfoTool` dispatches per call: `_run_git_raw` -> `_exec_container_raw` (resource container) when `_use_container_mode()`, else `_exec_host_raw` (host `SandboxedExecution` subprocess) | `tools/git_info_tool.py:382-395` | PASS |
| 2.2 | Do they route through the resource container? | Yes when a registry workspace is resolvable: `_use_container_mode()` = mode != `host` AND resolved workspace path AND workspace id; `_ensure_resource_container()` builds `ResourceContainerManager(workspace_id, workspace_path, network_mode=<expected, fail-closed "none">)` and calls `ensure_container()`; git runs via `manager.exec(["git"] + args, ...)` | `tools/git_info_tool.py:451-506,572-580,582-608` | PASS |
| 2.3 | Is there any host fallback path? | **Yes.** `_exec_host_raw` runs git as a subprocess **on the host** through `SandboxedExecution` whenever `_use_container_mode()` is False (mode `host`, or workspace path/id unresolved). Hooks are neutralized there (`core.hooksPath=/dev/null`, `diff.external=`/`fsmonitor=`/filters/`credential.helper=` emptied, commit gets `--no-verify`) | `tools/git_info_tool.py:397-449` (`:406-419`) | **GAP — MEDIUM (host fallback executes outside the container boundary)** |
| 2.4 | How is `git_execution_mode` resolved? | `_git_execution_mode()` precedence: `agent_config['git_execution_mode']` -> workspace metadata -> default `'container'`; only values `'host'`/`'container'` accepted | `tools/git_info_tool.py:528-540` | PASS |
| 2.5 | Are pre-commit hooks allowed only inside the container? | Container mode: **yes, hooks run** — "no --no-verify here. The resource container IS the security boundary" (repo-local `.git/hooks` execute; no `core.hooksPath` override anywhere in `infra/resource_container_manager.py`). Host mode: **no** — `core.hooksPath=/dev/null` + `--no-verify` on commit | `tools/git_info_tool.py:486-488` vs `:406-419`; grep `hooks` over `infra/resource_container_manager.py` (0 matches) | PASS (by design) |
| 2.6 | Is `.venv` mounted read-only for hooks? | Yes — `Mount(..., read_only=True)` resolved by walking up parent dirs; skipped with a warning when absent (a stale reused container may lack it — see 4.4/G13) | `infra/resource_container_manager.py:540-554` | PASS (see G13) |
| 2.7 | Is the Docker socket exposed to the resource container? | No — `/var/run/docker.sock` appears only in docstrings, never as a mount in any create path; E2E test asserts `test -S /var/run/docker.sock` fails inside the container | `infra/resource_container_manager.py:31-32`; grep `infra/`; `tests/security/test_git_container_sandbox.py:176-180` | PASS |
| 2.8 | Is the vault mounted inside the resource container? | No — explicit design invariant; `_resolve_worktree_main_repo` refuses vault pointers (and fs roots, workspace self, submodule gitdirs); E2E test proves `/vault` exfil yields empty output | `infra/resource_container_manager.py:26-30,278-279,335-339`; `tests/security/test_git_container_sandbox.py:148-156` | PASS |
| 2.9 | Is the resource container visible to worker container-listing tools? | No — `ContainerManager.list_containers()` filters by `thoughtmachine.workspace_id` then **excludes** containers labeled `thoughtmachine.resource`; `ContainerListTool` inherits; web_ui startup scan and `docker_executor` integrity target only `agent-exec-` names (never `tm-res-*`) | `infra/container_manager.py:1047-1061`; `web_ui/backend/server.py:332-333`; `docker_executor.py:239,467` | PASS |

**Section verdict: GAP (MEDIUM x1, 2.3).** Container routing is the default and hooks are confined there; the host fallback is the weakest path (git subprocess on the host, hooks neutralized but boundary = host OS).

---

## 3. Resource-container guard

| # | Question | Finding | Evidence (file:line) | Verdict |
|---|---|---|---|---|
| 3.1 | Where is the resource-container request guard implemented? | Two layers: (a) registry `request_container` rejects `image == RESOURCE_IMAGE_TAG ("tm-resource-git")` OR `container_type == "resource"` OR `name_hint.startswith("tm-res-")` -> `PermissionError("Resource container access denied")`; (b) `WorkerSupervisor.request_container` raises `PermissionError("Resource containers (git, tm-res-*) are reserved for the main agent...")` via `_is_resource_container_request` (image/name checks for `tm-resource-git`/`tm-res-` prefix) | (a) `infra/container_registry.py:344-352` (constants `:60,:69-72`); (b) `infra/workspace_lifecycle_manager.py:693-735` (`:761+`) | PASS |
| 3.2 | Can any worker path request the resource container? | No sanctioned path: WorkerSupervisor refuses (3.1b) and the registry refuses (3.1a). The only unguarded route is the **legacy** `ContainerManager.start(image=...)` when the registry is inactive — a `container:true` worker could pass `image="tm-resource-git"`; the container still gets full user-container hardening and none of the resource-only mounts | `infra/workspace_lifecycle_manager.py:735`; `infra/container_manager.py:455-470,566-584` | **GAP — LOW (G14)** |
| 3.3 | Can the main agent request the resource container through normal container tools? | No — `ContainerStartTool` -> `manager.start(image,name,note)`; the registry guard (3.1a) blocks `tm-resource-git` there too. The main agent creates resource containers only via the sanctioned factory `registry.create_resource_container` (RCM delegates when registry active); legacy RCM create is the fallback | `tools/container_control.py:193-219`; `infra/container_registry.py:430-500`; `infra/resource_container_manager.py:568-590,591-609` | PASS |
| 3.4 | What labels identify resource containers? | `thoughtmachine.resource="git"` (`RESOURCE_LABEL`/`RESOURCE_KIND`), `thoughtmachine.workspace_id=<ws>`, `thoughtmachine.container_name=<tm-res-...-git>`; plus the `tm-res-` name prefix | `infra/container_registry.py:69-72`; `infra/resource_container_manager.py:404-413,608` | PASS |
| 3.5 | Are those labels filtered from every container-listing path? | `list_containers()`: filtered (`if (container.labels or {}).get("thoughtmachine.resource"): continue`, L1056-1061); `ContainerListTool`: inherits; web_ui startup scan: matches `agent-exec-` names only; `docker_executor.verify_container_integrity`/`_remove_container_by_workspace`: `agent-exec-` names only -> resource never surfaced | `infra/container_manager.py:1056-1061`; `web_ui/backend/server.py:332-333`; `docker_executor.py:239,467,884` | PASS |
| 3.6 | Any listing path that does NOT filter resource containers? | Yes, intentional ones: (a) `cleanup_workspace()` sweeps ALL `workspace_id`-labeled containers **including** the resource container (documented); (b) raw daemon label queries — RCM `ensure_container` reuse candidate scan and `ContainerManager.list_containers`' initial daemon fetch see it before the exclusion; (c) direct `docker` CLI/SDK usage by an operator. No agent-facing API path surfaces it | `infra/container_manager.py:1364-1372`; `infra/resource_container_manager.py:420-520`; `infra/container_manager.py:1047-1050` | PASS (intentional visibility only) |

**Section verdict: PASS with 1 LOW gap (G14).** Double guard (registry + WLM) blocks every sanctioned worker path; invisibility is enforced on the agent-facing listing path with only deliberate exceptions (cleanup sweep, raw daemon queries).

---

## 4. Resource-container lifecycle

| # | Question | Finding | Evidence (file:line) | Verdict |
|---|---|---|---|---|
| 4.1 | How named? | `tm-res-<sha256(workspace_path)[:12]>-git` — deliberately avoids the `agent-exec-` prefix so startup/integrity scans (matched on `agent-exec-`) never touch it | `infra/resource_container_manager.py:404-413` | PASS |
| 4.2 | How created? | `ensure_container()` ensures the image first (`_ensure_resource_image`: single-flight, success-cached, never raises), then either registry `create_resource_container` (labels, `/workspace` rw always, `network_mode or "none"`, 512m/50000/oom 500) or legacy create: `/workspace` bind rw (L513-519), linked-worktree main repo bind rw at original host path (L521-539), `.venv` bind ro (L540-554), tmpfs `/tmp`+`/home/agent` (L560-566), `network_mode` (L596), `cap_drop ALL` (L597), `no-new-privileges` (L598), `oom_score_adj=500` (L599), `read_only=True` (L600), `user="1000:1000"` (L601), `tail -f /dev/null` (L605), mem 512m (L606), cpu 50000 (L607), labels (L608) | `infra/resource_container_manager.py:142,420-520,568-590,591-609`; `infra/container_registry.py:430-500` | PASS |
| 4.3 | How reused? | Candidates = containers with `thoughtmachine.workspace_id` label; reuse iff `container_name` label OR docker name == ours AND labels `thoughtmachine.resource == "git"`; started if not running. **No re-verification of mounts or config** | `infra/resource_container_manager.py:420-520` | PASS (see G13) |
| 4.4 | Can a stale container without the expected `.venv` mount be reused? | **Yes.** Reuse is label/name-based only; a container created before `.venv` existed (or whose `.venv` was removed) is reused without the ro `.venv` bind. Impact: hooks fall back to system git/python tooling — not a sandbox escape | `infra/resource_container_manager.py:420-520` vs `:540-554` | **GAP — LOW/MEDIUM (G13)** |
| 4.5 | Wrong configuration (network/mem/cpu drift) reused? | **Yes.** Reuse does not compare existing network/mem/cpu against the current expected config — unlike `ContainerManager._config_matches`, which re-creates on drift. If the expected network later tightens to `none`, a previously bridge-mode resource container keeps running | `infra/resource_container_manager.py:420-520` vs `infra/container_manager.py:500-530` | **GAP — MEDIUM (G13)** |
| 4.6 | Is creation atomic? | Single `containers.run()` call (create+start). On failure a partially created container is not cleaned up by the create path (minor availability note; image-build failure never raises) | `infra/resource_container_manager.py:591-609` | PASS (minor note) |
| 4.7 | Included in global container limits? | No — the resource container is **not** counted against the user-container limit (registry `_get_max_containers` default 4 / legacy `_get_max_containers` default 4; legacy count uses `list_containers()`, which excludes resource containers). No global cap applies to the resource container itself | `infra/container_registry.py` (`_get_max_containers`); `infra/container_manager.py` (`_get_max_containers`, `list_containers` L1047-1061) | PASS (separate concern) |
| 4.8 | How destroyed? | `stop()`/`remove()` exist; E2E fixture teardown always calls `manager.remove()`; `cleanup_workspace()` sweeps all workspace-labeled containers including the resource container (documented intentional) | `infra/resource_container_manager.py` (stop/remove); `infra/container_manager.py:1364-1372`; `tests/security/test_git_container_sandbox.py:62-66` | PASS |
| 4.9 | `oom_score_adj` value — VERIFIED | **resource = 500, user = 1000.** Current tree: RCM legacy create `oom_score_adj=500` (L599); user containers 1000 (registry delegation / legacy run). Historical verification via `git show 663eb62` (`feat/sec-rce-upgrade`): "resource (git) containers get a moderate OOM score" (500), "user containers are the first OOM-kill victims" (1000). Matches expectation exactly | `infra/resource_container_manager.py:599`; `infra/container_manager.py:604-614`; `git show 663eb62 -- infra/resource_container_manager.py infra/container_manager.py` | PASS |
| 4.10 | Non-root? | Yes — image `USER agent` (uid 1000) + create `user="1000:1000"`; tmpfs `/home/agent` uid/gid 1000 | `resources/resource_dockerfile.txt`; `infra/resource_container_manager.py:560-566,601` | PASS |

**Section verdict: PASS with 1 MEDIUM gap (G13).** Lifecycle controls are strong except reuse never re-verifies config; oom_score_adj verified 500/1000 (matches `663eb62`).

---

## 5. Sandbox boundary

| # | Question | Finding | Evidence (file:line) | Verdict |
|---|---|---|---|---|
| 5.1 | Capabilities dropped? | `cap_drop=["ALL"]` on resource create (legacy + registry hardened factory); no `cap_add` anywhere in `infra/` | `infra/resource_container_manager.py:597`; `infra/container_registry.py:169`; grep `infra/` | PASS |
| 5.2 | no-new-privileges? | `security_opt=["no-new-privileges:true"]` on both resource create paths | `infra/resource_container_manager.py:598`; `infra/container_registry.py:170` | PASS |
| 5.3 | Rootfs read-only? | `read_only=True` (legacy create and registry `create_hardened_container`); writable only via binds/tmpfs | `infra/resource_container_manager.py:600`; `infra/container_registry.py:162-183` | PASS |
| 5.4 | `/tmp` tmpfs? | tmpfs `/tmp` + `/home/agent` (registry profile adds `noexec,nosuid`); no `/workspace/.git` shadow on the resource path (user containers have it) | `infra/resource_container_manager.py:560-566`; `infra/container_registry.py:97-98`; vs `infra/container_manager.py:530-535` | PASS |
| 5.5 | User namespace / uid 1000? | uid 1000 non-root everywhere; **userns remap NOT configured** in repo (daemon-level `userns-remap`) | `infra/resource_container_manager.py:601`; (absent) | **GAP — LOW (G11)** |
| 5.6 | Network modes allowed? | `none` default (`self.network_mode = network_mode or "none"`); registry `resolve_network_mode`: bridge iff perms `network` True/"write" else none; `get_expected_container_config` fail-closed none/ro | `infra/resource_container_manager.py:399,411,596`; `infra/container_registry.py:621-626`; `security/security_gate.py:202-215` | PASS |
| 5.7 | Host networking possible? | No — no `network_mode="host"` anywhere in `infra/` (grep); no `ports=`/`publish` either | grep `infra/` | PASS |
| 5.8 | Host home mounted? | No — no host `/home/<user>` bind; the container's `/home/agent` is tmpfs | `infra/resource_container_manager.py:560-566`; grep `infra/` | PASS |
| 5.9 | Workspace mounted rw? | **Yes — always rw** for the resource container (git must write real `.git`/index). Malicious hook can mutate the real repo; bounded by no network/socket/vault + no-new-privileges | `infra/resource_container_manager.py:513-519`; `infra/container_registry.py:417-421` | **GAP — HIGH, by design (G1)** |
| 5.10 | Main git repo mounted correctly for worktrees? | Yes — linked-worktree main repo bound rw at its original host path so `gitdir:` pointers resolve; `_resolve_worktree_main_repo` validates: refuses vault pointers, fs roots, workspace self, submodule gitdirs, non-worktrees; unit regression tests cover it | `infra/resource_container_manager.py:521-539` (`:258-339`); `tests/docker/test_resource_container_worktree.py` | **GAP — HIGH, by design (G2)**; resolution logic PASS |
| 5.11 | Any path bind-mounts that could escape workspace/repo? | The worktree main-repo bind (5.10) is the only extra host path and is validated; `.venv` bind is ro and parent-walk-resolved; no other host paths, devices, or volumes are mounted | `infra/resource_container_manager.py:513-554`; grep `infra/` | PASS (bounded by G2) |

**Section verdict: GAP (HIGH x2, by design — G1/G2).** Hardening (cap-drop, no-new-privileges, ro rootfs, tmpfs, none-network) is uniform; the deliberate rw workspace/worktree mounts are the accepted tradeoff, proven bounded by the E2E malicious-hook test.

---

## 6. Real Docker E2E coverage

| # | Question | Finding | Evidence (file:line) | Verdict |
|---|---|---|---|---|
| 6.1 | What real-Docker tests exist for the resource container? | `tests/security/test_git_container_sandbox.py` — module `@pytest.mark.docker` with `require_docker` fixture (skips whole module without a daemon); tests the REAL manager against a REAL daemon; image must be built first (`docker build -t tm-resource-git ~/.thoughtmachine/docker/resource/`). Plus `tests/docker_integration/` (container_integrity, startup_check, error_boundaries) | `tests/security/test_git_container_sandbox.py:1-86`; `pyproject.toml:45-49` (marker registered) | PASS (collected; skipped w/o daemon) |
| 6.2 | Malicious git hooks? | Yes — `test_git_commit_hook_isolated`: malicious `post-commit` hook written from the host into real `.git/hooks`; asserts the hook **runs** (`/tmp/hook_ran` on tmpfs) but cannot write the container rootfs (`/host_proof` absent), cannot exfil `/vault` (not mounted — output empty), no docker socket, no network egress (curl with python-socket fallback) | `tests/security/test_git_container_sandbox.py:88-186` | PASS |
| 6.3 | Worktree resolution? | Unit regression tests (not real-Docker): `tests/docker/test_resource_container_worktree.py` covers the `gitdir:` pointer fix — main repo refused for non-worktrees/self/vault/fs-roots | `tests/docker/test_resource_container_worktree.py:1-34` | PASS (unit-level) |
| 6.4 | Permission propagation? | Contract/unit coverage: `tests/security/test_reg_contract_matrix.py` + `tests/test_git_hardening.py` (git:read/git:write gate via `_ensure_resource_container`), `tests/test_permission_routing_fix.py`, `tests/test_workspace_lifecycle_manager.py` (`FakeResourceContainerManager`), `tests/tools/test_container_control.py` (43 KB), `tests/test_container_registry.py` (28.5 KB) | files listed | PASS |
| 6.5 | Resource-container invisibility? | Yes — `test_resource_container_hidden_from_agent_listing` asserts the resource container is absent from `ContainerManager.list_containers()` (contract: same `workspace_id` label so cleanup sweeps it, `thoughtmachine.resource` label hides it). NOTE: the test docstring predates the exclusion diff and says it "FAILS against the current (un-patched) code"; the working tree @ `0b43e45` HAS the exclusion (`infra/container_manager.py:1056-1061`), so the contract now holds | `tests/security/test_git_container_sandbox.py:195-230` | PASS (docstring stale; code current) |
| 6.6 | What is missing? | (a) Real-Docker tests were **collected but not run** in this audit env (no daemon; 38 suite skips include this module); (b) no real-Docker test for reuse/config-drift (G13); (c) no test for the legacy `start()` resource guard (G14); (d) no `pids_limit`/memswap tests (G3/G4); (e) daemon-level seccomp/apparmor/userns untested; (f) no adversarial test of the host-fallback sandbox (2.3); (g) no E2E test for linked-worktree main-repo binding against a real daemon | (see GAP Register) | GAP — test coverage holes (documented) |

**Section verdict: PASS with documented coverage holes.** The two hardest properties (hook confinement, invisibility) have real-Docker tests that skip cleanly without a daemon; everything else is unit/contract-tested.

---

## GAP Register

| # | Severity | Domain | Gap | Evidence (file:line) | Recommended Mitigation |
|---|---|---|---|---|---|
| G1 | HIGH | Filesystem | Resource container `/workspace` bind is READ-WRITE; malicious git hook can mutate the real repo | `infra/resource_container_manager.py:513-519`; `infra/container_registry.py:417-421` | By design (documented `:41-45`). Bound further: sanitize `core.hooksPath`, snapshot-restore `.git` after run |
| G2 | HIGH | Filesystem | Linked-worktree MAIN repo bind-mounted READ-WRITE at original host path | `infra/resource_container_manager.py:521-539` | By design. Same mitigations as G1; consider ro bind + overlay for git objects |
| G3 | MEDIUM | Resource limits | No `pids_limit` anywhere — fork bomb in hook/script bounded only by mem/cpu | (absent from all `containers.run` calls; profile `infra/container_registry.py:118-129`) | Add `pids_limit` to profile + all create paths |
| G4 | LOW | Resource limits | No memswap_limit | (absent) | Set `memswap_limit=2x mem_limit` |
| G5 | LOW/MEDIUM | Resource limits | Disk quota is soft post-hoc `du` in `ContainerManager.exec` only; not enforced on the resource exec path | `infra/container_manager.py:236-238,254` | Add pre/post `du` guard in resource exec or storage-opt size |
| G6 | LOW | Privilege | Seccomp/AppArmor not explicit (docker defaults; daemon may run unconfined) | (absent) | Pass explicit seccomp profile; verify daemon apparmor |
| G7 | LOW | Daemon API | `docker.from_env()` x5 without TLS/version pin/timeout — honors host `DOCKER_HOST`/`DOCKER_TLS_VERIFY` | `infra/container_registry.py:211`; `infra/resource_container_manager.py:142,231,420`; `docker_executor.py:252,378`; `infra/container_manager.py:245` | Pin `version=`+`timeout=`; refuse plain-TCP `DOCKER_HOST` |
| G8 | LOW | Daemon API | RCM `__init__` `docker.from_env()` unwrapped (L420) — constructor raises when daemon down | `infra/resource_container_manager.py:420` | Wrap like `_ensure_resource_image` |
| G9 | LOW | Daemon API | `ContainerRegistry.__init__` eagerly probes daemon when enabled | `infra/container_registry.py:193-216` | Defer probe to first create |
| G10 | LOW | Gate | `get_expected_container_config` derives network from `network` category only; `container` category not consulted | `security/security_gate.py:135` vs `:215` | AND in `eff["container"]` as defense-in-depth |
| G11 | LOW | Namespace | No userns remap / explicit shm_size / sysctls (daemon-level) | (absent) | Configure daemon `userns-remap`; set `shm_size` |
| G12 | LOW/MEDIUM | Worker surface | Container tools (DockerCodeRunner, ContainerStart/Exec/...) NOT in `_WORKER_BLOCKLIST` — gated only by `container:true`/`filesystem:write`; worker asks hard-denied, so exposure = workers granted `container:true` | `tools/workspace/worker.py:396-401`; `tools/container_control.py:85,250`; `tools/docker_code_runner.py:47` | Keep permission-gated (current) or add `container:true` to worker deny-by-default; document that `container:true` workers control their own sandboxes |
| G13 | MEDIUM | Lifecycle | Resource-container reuse verifies only labels/name — stale (`.venv` missing) or config-drifted (network/mem/cpu) containers reused without re-check | `infra/resource_container_manager.py:420-520` vs `infra/container_manager.py:500-530` | Add config re-verification on reuse (mirror `_config_matches`); rebuild on drift |
| G14 | LOW | Guard | Legacy `ContainerManager.start` has no resource-image/name guard when registry inactive — `image="tm-resource-git"` could be requested; container still fully hardened, no resource-only mounts | `infra/container_manager.py:455-470,566-584`; `infra/workspace_lifecycle_manager.py:735` | Reject `RESOURCE_IMAGE_TAG`/`tm-res-` names in `ContainerManager.start` regardless of registry |

---

## UNKNOWN Items

1. **Daemon-level configuration** — effective seccomp/apparmor profile, `userns-remap`, `shm_size`, storage-driver disk quotas: outside the repo, depend on how the host daemon was started.
2. **Real-Docker E2E execution** — `tests/security/test_git_container_sandbox.py` and other `@pytest.mark.docker` tests were COLLECTED but NOT RUN in this audit environment (no daemon; included in the 38 suite skips).
3. **Host-git exposure depth** — the host fallback (2.3) runs git on the host via `SandboxedExecution`; full neutralization of repo-induced code paths (e.g. `GIT_EDITOR`, submodule hooks, pager config beyond `GIT_PAGER=cat`) was not exhaustively enumerated.
4. **Worker `container:true` prevalence** — whether any shipped worker template actually grants `container:true` (and thus reaches G12/G14) was not audited in this pass.

---

## Summary

The resource-container isolation posture **holds against the worker-threat model**: workers cannot request or create resource containers through any sanctioned path (registry guard 3.1 + WLM guard 3.2, single factory 3.3), cannot see them in listings (2.9, 3.5), and the git tool routes to the hardened container with fail-closed permissions (2.2, 2.4, 2.5). The two by-design HIGH gaps remain the read-write workspace / worktree mounts (G1/G2), bounded by no-network/no-vault/no-socket + cap-drop ALL + no-new-privileges + non-root and proven by a real-Docker malicious-hook E2E test (6.2). Most actionable new items: **G13 (reuse never re-verifies config — MEDIUM)**, **G12 (worker container-tool exposure permission-gated only — LOW/MEDIUM)**, **G14 (legacy `start()` lacks resource guard — LOW)**. `oom_score_adj` verified resource=500 / user=1000 (matches `663eb62`). Test suite: **1414 passed, 38 skipped, 0 failed**.
