# Self-Description Reconciliation

This document reconciles six self-descriptions emitted by the working session's own
prompt and tooling against the committed source at the current HEAD. Each claim is
quoted from its source, traced to the code path that implements (or contradicts) it,
and assigned a verdict: **confirmed** / **falsified** / **partially-true** /
**unverifiable**. Method: static reads of the committed tree, plus empirical checks
run in the sandbox where noted. No production code was modified.

Scope note: the "claim source" for several items is the single-line worker system
prompt (`resources/worker_templates/default.json:4`), which is stored as one JSON
string; all quoted phrases live on that line.

## Verdict summary

| # | claim (short) | verdict | primary evidence |
|---|---|---|---|
| i | workers cannot commit; git is engineer-only | partially-true | `tools/git_write_tool.py:79-85` (commit in schema) vs `:91-120` (grain gate) |
| ii | prompt names tools absent from worker schema | confirmed | `resources/worker_templates/default.json:4` vs `session/tool_presets.py:72` |
| iii | sandbox network blocked | partially-true | sandbox: `pypi.org` 200; `tools/workspace/check_system.py:644,656-664` |
| iv | container_status says "stopped" while exec works | confirmed | `docker_executor.py:1223-1224,1252-1253` vs `tools/docker_code_runner.py:391-414` |
| v | /workspace is the host repo bind-mounted RW | confirmed | `docker_executor.py:713-717`; sandbox wrote `/workspace/tmp/...` |
| vi | DockerCodeRunner writes a script into the workspace | confirmed | `tools/docker_code_runner.py:249-250,257-261`; sandbox artefacts |

Counts: **confirmed 4, partially-true 2, falsified 0, unverifiable 0.**

---

## Claim (i) — "Workers cannot commit to Git; you may only read history."

- **Claim source:** `resources/worker_templates/default.json:4` — literal text
  "You cannot commit to Git; you may only read history." Restated for the engineer at
  `resources/engineer_system_prompt.txt:9` ("You are responsible for all commits;
  workers cannot commit.") and `:65` ("Only you can commit to Git; workers cannot.").
- **Code path:** `tools/git_write_tool.py:19-21` defines `GitWriteTool` with operations
  `commit, init, clone, branch_create, checkout, stage, unstage`; `:57` sets
  `name="git_write"`; `:66-75` `get_required_categories` returns `["git:write"]`;
  `:79-85` the `operation` Literal includes `"commit"`; `:274-275` dispatches
  `if self.operation == "commit": return self._git_commit(...)`; `:91-120`
  `_git_write_allowed` fails closed unless the git grain is
  `write`/`full`/`write_on_feature_branch`/`ask`.
- **Finding:** `git_write` — including `commit` — IS present in the worker runtime tool
  schema (this session's own tool list exposes it). The prohibition is enforced only at
  runtime, by the `git:write` permission grain, not by the tool's absence.
- **Verdict: partially-true** — accurate as a fail-closed runtime guarantee, inaccurate
  as a capability/schema statement.

## Claim (ii) — prompt names CheckSystem and SearchCodebaseTool; the worker schema exposes neither.

- **Claim source:** `resources/worker_templates/default.json:4` names `CheckSystem`
  ("CheckSystem lets you inspect your environment, permissions, and the vault") and
  `SearchCodebaseTool` ("SearchCodebaseTool (semantic)").
- **Code path:** `session/tool_presets.py:21-65` `_ALL_TOOLS` contains `"CheckSystem"`
  (`:24`) and `"SearchCodebaseTool"` (`:39`). The `AGENT_TOOLS` list (`:70-101`)
  includes `"CheckSystem"` (`:72`) but **omits** `"SearchCodebaseTool"`. The worker's
  own runtime schema (this session) exposes **neither** `CheckSystem` nor
  `SearchCodebaseTool`.
- **Verdict: confirmed** — the prompt references tools the worker schema does not expose.

## Claim (iii) — "the sandbox network is blocked."

- **Empirical (this sandbox, DockerCodeRunner / python3 urllib):** `OK https://pypi.org/simple/ 200`
  and `OK https://example.com 200` — egress works.
- **Code path:** `tools/workspace/check_system.py:576-673` `_query_network_diagnostics`
  probes `https://example.com` (`:644`) from a `python:3.11-slim` probe container; it
  runs only when that image is present locally (`:619-632`, otherwise the message
  "image not present locally; probe skipped"); on success it sets `result["egress"]=True`
  and "egress OK (bridge network)" (`:656-664`); the probe is always stopped in a
  `finally` (`:668-672`).
- **Finding:** the sandbox reaches the network, and `network_diagnostics` likewise
  reports egress OK whenever its probe image exists and the probe container runs on a
  bridge network. The "blocked" branch is reachable only when the probe's
  `network_mode` is `none` or the `3.11-slim` image is absent. Note the probe targets
  `example.com`, **not** `pypi.org`.
- **Verdict: partially-true** — the "blocked" reading is falsified by measurement; the
  divergence surfaces only in the probe's degraded cases.

## Claim (iv) — container_status reports "stopped" while execution actually works.

- **Claim / code path:** `tools/workspace/check_system.py:412-425`
  `_query_container_status` delegates to `docker_executor.get_container_status(workspace_path=ws_path)`
  (`:420`). `docker_executor.py:1194-1289` computes
  `container_name = f"agent-exec-{sha256(abspath(ws))[:12]}"` (`:1223-1224`) and returns
  `status="stopped"` on `docker.errors.NotFound` (`:1252-1253`).
- **Divergence:** the live execution path does not use that legacy name.
  `tools/docker_code_runner.py:391-414` routes through a per-session `ContainerManager`:
  `manager.start(..., lifecycle_class=LIFECYCLE_EPHEMERAL)` (`:409-414`) → `manager.exec`
  (`:448`) → `manager.stop` (`:456`). `ContainerManager` names its containers
  `agent-exec-<sha256(ws)[:12]>-<session_tag>` (`docs/container_subsystem_audit.md:21`;
  create site `infra/container_manager.py:1463`) — a session-tagged name the status
  probe never looks up — and the ephemeral container is stopped after each run.
- **Finding:** the status probe reports the by-name legacy container absent ⇒ "stopped",
  while a fresh `exec` succeeds on the manager's session-tagged container.
- **Verdict: confirmed.**

## Claim (v) — /workspace is the live host repo, bind-mounted read-write.

- **Code path:** `docker_executor.py:713-717` — `read_only = workspace_mode == "ro"`;
  `docker.types.Mount(target="/workspace", source=self.workspace_path, type="bind", read_only=read_only)`.
  `workspace_mode` derives from the security-gate `resolve_container_config`
  (`:504-523`, `:176-212`; default `("none","ro")`). Container name scheme
  `agent-exec-<sha256[:12]>` (`:583-584`). A `/workspace/.git` tmpfs shadow is added
  when `<ws>/.git` is a directory (~`:702`).
- **Empirical:** this sandbox successfully created `/workspace/tmp/script_d93507dd.sh`,
  so the bind mount is read-write under this session's permission footprint.
- **Verdict: confirmed** — a bind mount of the host repo, RW when the permission grants.

## Claim (vi) — DockerCodeRunner writes a script file into the workspace working dir.

- **Code path:** `tools/docker_code_runner.py:249` `script_dir="/workspace/tmp"`;
  `:250` `script_path=f"{script_dir}/script_{uuid.uuid4().hex[:8]}.sh"`; `:257-261`
  command = `mkdir -p` + here-doc `cat >` + `chmod +x` + run; `_prepare_script_command`
  is defined at `:232` and called at `:359`.
- **Empirical:** the sandbox run literally created `/workspace/tmp/script_d93507dd.sh`.
- **Verdict: confirmed.**

---

## Divergence: which create sites are actually executed

`docs/container_subsystem_audit.md` (`:9`, `:17`) documents **four** independent
terminal `containers.run(...)` create sites:

1. `infra/container_manager.py:1463` — free-use / "user" HOT path (also the
   DockerCodeRunner path);
2. `docker_executor.py:790` — legacy executor;
3. `infra/container_registry.py:239` — `create_hardened_container`, the advertised
   "single hardened create path";
4. `infra/resource_container_manager.py:1213` — resource / git.

In **this** running session the registry site (3) is **not** executed.
`use_container_registry` defaults to `False` (`agent/config/models.py:141-144`;
`agent/config/session_config.py:190-193`) and is set true in **no** `.yaml`
(repo-wide search: no matches), so `is_container_registry_enabled` returns `False`
(`infra/container_registry.py:972-976`) and `get_container_registry` hands back a
docker-less registry whose feature-flag check is `lambda: False`
(`infra/container_registry.py:986-987`) — `create_hardened_container` is therefore
never reached. `use_workspace_lifecycle_manager` likewise defaults to `False`
(`models.py:137-140`; `session_config.py:186-189`; `is_wlm_enabled`
`infra/workspace_lifecycle_manager.py:194-198`; `registry_wiring.py:9-12`).

**Conclusion:** the live container-create path in this session is the
`ContainerManager` site (1), reached via DockerCodeRunner; the legacy executor site (2)
and the registry site (3) are *not* the runtime path here, so code that only executes
through them (e.g. `create_hardened_container`, and the registry's `user=(host_user() or "0:0")`
line) is effectively dormant even though it is present in the tree.

---

## Known dev-state fact (retired user-default contract)

The Windows user-fallback contract is stale between test and code at HEAD:

- `tests/test_container_user_and_git_ownership.py:501` defines
  `test_container_registry_omits_user_when_host_user_is_none`, and `:517` asserts
  `assert kwargs.get("user") is None` — i.e. that `create_hardened_container` omits
  `--user` when the host has no uid.
- `infra/container_registry.py:251` now passes `user=(host_user() or "0:0")`, with the
  comment at `:248-250` ("Windows: Docker Desktop presents bind mounts as root:root;
  match the mount owner. Retired when fix/uid-probe-universal lands."). Under the test's
  Windows-shaped monkeypatch (`host_user()` → `None`) this yields `"0:0"`, not `None`,
  so the assertion is stale against HEAD.
- History (per `git_read`): the None-omission expectation entered with `8916fb2`
  "fix(config): guard host uid/gid resolution on Windows"; the file tip is `4649a7e`
  "route all create-stack uid/gid sites through _host_ids guard". The fallback itself
  was added on the current branch by `06a597e`/`0700c86` ("fall back to \"0:0\" when
  host_user() is unavailable") and HEAD is `f534d48` "ci(windows): run the host_user()
  fallback contract in the thin slice" on branch `fix/host-user-fallback-all-sites`
  (a `fix/windows-user-fallback-test-contract` branch also exists).

_Unverified: exact commit dates (the `git_read` format override was ignored in this
environment); the ephemeral container's precise runtime name in the `(iv)` mechanism is
inferred from the manager naming scheme, not captured live._
