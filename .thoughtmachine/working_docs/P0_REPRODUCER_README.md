# P0 Reproducer - Operator Checklist

HOST-RUN reproducer for four P0 container-lifecycle defects in
`ThoughtMachine-dev`. It drives the **real** `ContainerManager` and the **real**
`GitReadTool` against your **real Docker daemon** - no mocks, no repository
changes. It only ever touches containers named `tm-p0-*`.

> **Cannot run in the worker container.** There is no Docker daemon / docker
> socket inside the worker sandbox, so this script must be run **on the HOST**.

---

## 1. Pre-flight (do these first)

1. **Docker daemon running** - `docker version` shows a Server section.
2. **A local image present** - pull one for the container sections:
   ```
   docker pull alpine:latest      # or: docker pull alpine:3.8
   ```
   Override with `P0_IMAGE` if you prefer a different tag (`P0_IMAGE=alpine:3.8`).
   BUG1, BUG2 and BUG3 need it; BUG4 needs no image.
3. **`.venv` exists** with `docker-py >= 7`
   (`./.venv/bin/python -c "import docker; print(docker.__version__)"`).
   BUG4 and the preflight need only the repo importable.

---

## 2. Exact run command

From the repo root, on the HOST:

```
bash .thoughtmachine/working_docs/reproduce_p0.sh 2>&1 | tee /tmp/p0_evidence.txt
```

Optional env overrides:

| Var        | Default              | Meaning                          |
|------------|----------------------|----------------------------------|
| `P0_VENV`  | `./.venv`            | venv holding docker-py           |
| `P0_IMAGE` | `alpine:latest`      | local image for BUG1/BUG2/BUG3   |
| `P0_VAULT` | `/tmp/tm-p0-vault`   | throwaway vault (deleted on exit)|

The wrapper runs 6 numbered stages (`[1/6] PREFLIGHT` … `[6/6] CLEANUP`) and the
driver prints its own SUMMARY table.

---

## 2b. Two modes - synthetic vault vs real workspace

The driver runs in one of two modes:

- **Synthetic mode (default).** Fully self-contained. It builds a throwaway vault
  under `P0_VAULT` (`/tmp/tm-p0-vault` by default) that intentionally has **no
  `capabilities.json`**. With no capabilities the security gate **fails CLOSED**,
  so a `network=outbound` request is decoded to `network_mode=none`. That is a
  *fail-closed artifact of the throwaway vault*, **not** proof of Bug 2. Use this
  mode to exercise the plumbing; do not cite its network result as Bug-2 evidence.
- **Real-workspace mode.** Point the driver at an actual workspace that **has**
  `capabilities.json` (i.e. one the framework already provisioned):

  ```bash
  P0_WS=<your real workspace id> bash .thoughtmachine/working_docs/reproduce_p0.sh 2>&1 | tee /tmp/p0_evidence.txt
  ```

  Optionally set `P0_REAL_VAULT_ROOT` to override the vault root; it defaults to
  the framework's `thoughtmachine.vault.vault_root()`. Setting `P0_WS` enables two
  extra sections that run against *that* workspace:

  - **SECTION 0d** is **strictly read-only**: it takes a before/after fingerprint
    (`snapshot_tree`) of `<vault_root>/workspaces/<P0_WS>` and asserts nothing was
    written (`TREE UNCHANGED: True`). It compares the security GATE's
    `get_expected_container_config(sp, None)` against the MANAGER's
    `_compute_config(repo_root, ws, sp)` on the same workspace.
  - **SECTION 0e** runs the real-workspace **Bug-1 census** (`list_containers()`
    vs `_get_max_containers()`) and, when a daemon is present, creates a single
    **Bug-2 probe container** and inspects its `HostConfig.NetworkMode` from
    inside (plus `_compute_config`), then execs `/proc/net/*` and a socket probe.

  Both sections are **read-only with respect to the vault** and only ever
  **create/remove containers named `tm-p0-*`** - the same prefix the CLEANUP stage
  removes; nothing outside that prefix is ever touched.

**Only the real-workspace run is authoritative Bug-2 evidence.** For that
workspace, SECTION 0d compares the GATE's `get_expected_container_config(sp, None)`
against the MANAGER's `_compute_config(repo_root, ws, sp)` on the *same*
workspace and reports **AGREE** vs **DIVERGENCE** - a DIVERGENCE on `network_mode`
is Bug 2's mechanism.

---

## 3. What each bug must demonstrate

| # | Bug | Must demonstrate (buggy evidence) |
|---|-----|-----------------------------------|
| 1 | **limit counts stopped containers** | With `max_containers=2` and `tm-p0-1`+`tm-p0-2` **stopped but not removed**, `start tm-p0-3` returns `{"error": "Workspace container limit (2) reached. Stop an unused container first."}` **while 0 containers run**. |
| 2 | **network ignored** | `session_permissions={"network":"outbound"}` on a fail-closed workspace still yields a container whose `HostConfig.NetworkMode != "bridge"` (i.e. `none`); the request is only WARN-logged. Sub-test: a no-network container is returned `"reused"` when `outbound` is later requested. |
| 3 | **image mismatch silently reused** | Starting the **same name** with a DIFFERENT image tag returns `{"status":"reused"}` and `Config.Image` is still the FIRST image -> requested image silently ignored. |
| 4 | **git execution fallback** | **INVESTIGATION ONLY - no container is started.** Prints `resolve_git_execution_mode(...)` for several combos, `_git_execution_mode()`, `_use_container_mode()` and a demo `_with_mode` trailer. No pass/fail is asserted. |

Section 0 also prints interpreter/platform, docker SDK version, a best-effort
daemon ping, and `inspect.signature(...)` for the real tool classes.

---

## 4. What to paste back

**Everything.** The full contents of `/tmp/p0_evidence.txt` - every stage, every
traceback, and the final SUMMARY table. Do not summarise or trim.

---

## 5. Cleanup

Stage `[6/6]` runs `docker ps -a --filter name=tm-p0-` before and after, then
`docker rm -f` on names matching `tm-p0-*` only, and `rm -rf "$P0_VAULT"`.
Manual fallback if interrupted:

```
docker ps -a --filter name=tm-p0- --format '{{.Names}}'
docker rm -f $(docker ps -a --filter name=tm-p0- --format '{{.Names}}' | grep '^tm-p0-')
rm -rf /tmp/tm-p0-vault
```

---

## 6. Safety

- **Only** `tm-p0-*` containers are created/stopped/removed (guarded by a
  `case tm-p0-*)` check and asserted in the driver).
- All state lives in the throwaway vault `${P0_VAULT:-/tmp/tm-p0-vault}`; your
  real `~/.thoughtmachine` is never written.
- **No repository files are modified.**

---

## 7. SUMMARY verdict vocabulary

- `REPRODUCED` - buggy behaviour observed (defect is real).
- `NOT-REPRODUCED` - behaved correctly for that scenario.
- `INVESTIGATION` - BUG4 only; informational.
- `INCONCLUSIVE` - ran, but result was neither buggy nor clean.
- `BLOCKED` - could not run (no daemon / missing local image).
- `ERROR` - exception raised (traceback printed above).

A `BLOCKED` on BUG1/BUG3 usually means the local image is missing - run
`docker pull $P0_IMAGE` and re-run.
