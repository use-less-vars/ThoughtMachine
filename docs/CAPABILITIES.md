# Capabilities Reference

This document is the single reference for **what ThoughtMachine can do**, **where it
runs**, and **what it does not yet do**. It complements — and deliberately does not
duplicate — the platform-specific contract in
[`docs/windows_stability_contract.md`](windows_stability_contract.md) and the install
steps in [`docs/installation_guide.md`](installation_guide.md).

Scope note: this is a *capabilities* document. It contains no roadmap, no vision
statements, and no forward-looking commitments.

---

## 1. Platform & feature matrix

The web UI is the supported frontend on **every** platform; the CLI and programmatic
API are the supported automation surfaces everywhere.

| Capability | Linux | macOS | Windows |
|---|---|---|---|
| Web UI (`web_ui/`) | ✅ | ✅ | ✅ |
| CLI / programmatic API (`agent/`) | ✅ | ✅ | ✅ |
| Agent core, config load/save, sessions, logging | ✅ | ✅ | ✅ |
| Docker code sandbox (`DockerExecutor`) | ✅ | ✅ | ❌ fails gracefully |
| Standalone binary (PyInstaller) | ✅ | ✅ | ✅ (`.exe`) |
| Optional RAG (`--with-rag`) | ✅ | ✅ | ✅ |

- On Windows the Docker code sandbox is **not available natively**. `DockerCodeRunner`
  detects the missing engine and degrades rather than crashing. See
  [`docs/windows_stability_contract.md`](windows_stability_contract.md) §9 (Non-Goals)
  and the `Docker executor` row of its scope table — that file is authoritative for
  the Windows boundary; the detail is not repeated here.
- Windows targets **native `cmd.exe` / PowerShell only**. Cygwin, MSYS2 and WSL are out
  of scope, and Windows < 10 / ARM64 are untested (x86-64 only). Again, see the Windows
  contract rather than re-stating it.
- macOS uses Docker Desktop. `install.sh` detects Darwin and adjusts the toolchain
  setup accordingly (skips `apt`/`docker` group manipulation, adds the Docker Desktop
  CLI to `PATH`).

Requirements (see [`pyproject.toml`](../pyproject.toml) and the requirements table in
`README.md`): **Python ≥ 3.11** (`requires-python = ">=3.11"`, classifiers cover
3.11–3.14), **Node ≥ 18**, and a recent **Docker engine** (required for the sandbox on
Linux/macOS, optional on Windows).

---

## 2. Container sandbox architecture & security model

The sandbox is the isolation boundary for all untrusted code execution. Its
configuration is resolved by a **single source of truth**, then enforced at container
creation.

**Paths that matter**

| Concern | Path |
|---|---|
| Sandbox executor / lifecycle | `tools/docker_executor.py` (`DockerExecutor`) |
| Container registry (durable records) | `infra/container_registry.py` |
| Container manager (live lifecycle) | `infra/container_manager.py` |
| Security SSOT | `security/security_gate.py` |
| Admission gate | `security/admission_gate.py` |
| Fail-closed tool entry | `agent/core/tool_executor.py` |
| Host-user identity | `agent/config/defaults.py` (`host_user()`) |
| Resource catalog | `agent/config/resource_catalog.py` |

**Configuration resolution (fail-closed).** `DockerExecutor` does not trust ambient
defaults. `_resolve_container_config_via_gate` (`tools/docker_executor.py:176`)
enters with fail-closed defaults — `network_mode="none"`, `workspace_mode="ro"`
(`:188`–`:189`) — and delegates to `security.security_gate.resolve_container_config`
(`:200`–`:206`), the SSOT. `_compute_container_config` (`:504`) produces the effective
`(network_mode ∈ {bridge, none}, workspace_mode ∈ {rw, ro})`.

**Enforced hardening at create time.** Containers are launched with:

- `network="none"` by default (`tools/docker_executor.py:474`),
- `mem_limit="1g"` (`:475`) and `cpu_quota=100000` (`:476`),
- `cap_drop=["ALL"]`, `security_opt=["no-new-privileges:true"]`,
- a **read-only root filesystem**,
- a **non-root `user`** equal to the host user (`host_user()`),
- a keep-alive command (`["tail", "-f", "/dev/null"]`).

`infra/container_registry.py` uses the same hardened kwargs (`cap_drop=HARDENED`,
`security_opt`, `read_only=HARDENED_READ_ONLY`, `user=(host_user() or "0:0")`) and **requires** a
`workspace_id` (fail-closed). `infra/container_manager.py` additionally pins
`oom_score_adj=1000`, observes the *live* container user, and with `strict=True`
refuses to proceed if the observed process would exit `126` (i.e. a broken user
mapping).

**Workspace mount & volumes.** The workspace is a bind mount of type `bind` targeting
`/workspace` (`tools/docker_executor.py:708`–`:710`). It is mounted **read-only iff**
`workspace_mode == "ro"`. A named volume `tm-packages-{workspace_id or 'default'}`
(`:714`) caches installed packages across runs. Containers are named
`agent-exec-{sha256(workspace_path)[:12]}` (`:583`–`:584`).

**Admission control.** Before every `create`, the admission gate is consulted with the
proposed `mem_limit` / `cpu_quota` / `network_mode` and `read_only=True`
(`:753`–`:770`). A **Deny** raises `AdmissionDenied`; a **Transform** may override the
`network_mode` (`:772`). `security/admission_gate.py` defines `AdmissionDenied`,
`admit()` (fail-closed), `narrow()`, `ADMISSION_IMAGE_ALLOWLIST`, `CONTAINER_TYPES`
and `DEFAULT_MAX_CONTAINERS`.

**Root refusal.** The host **root** (uid 0) is refused at creation with the
`root_host_unsupported` reason (`:774`–`:777`): the sandbox will not run as the host
superuser.

**Host-user ownership.** `agent/config/defaults.py::host_user()` returns the host
user's `"uid:gid"` (or `None` on Windows, `os.name == "nt"`). On **native Linux** the
container runs as the **host** user, so bind-mounted files retain correct ownership and
Git ownership checks pass without needing `safe.directory`. On **Windows** the resolver
returns `None` and the create sites (`infra/container_registry.py`,
`infra/resource_container_manager.py`) supply `user="0:0"` instead — matching Docker
Desktop, which presents bind mounts as `root:root`, so the container user matches the
mount owner.

For the Windows boundary and the banned-pattern rules (`shell=True`/`os.system`
prohibited, `pathlib`/`os.path` required, explicit UTF-8), see
[`docs/windows_stability_contract.md`](windows_stability_contract.md).

---

## 3. Resource grains

Permissions are modelled as **six canonical grains**
(`agent/config/session_config.py`):

```
git | filesystem | container | network | mcp | host_bash
```

- `git` is a single grain. The legacy split grains `git_read` / `git_write` have been
  **removed**; a prior `git_write == "write"` now maps to
  `session_permissions["git"] = "write"`, and `git_read` is dropped.
- `host_bash` gates all host shell execution. The old `allow_host_resources` session
  flag has been **removed**; host execution is now controlled solely by the `host_bash`
  grain. Setting it to false means *nothing runs on the host*.
- `container` is **boolean-only** in the resource catalog
  (`agent/config/resource_catalog.py:245`–`:255`).
- `SessionConfig` uses `extra="forbid"`, so unknown/legacy keys are rejected rather than
  silently ignored.

Effective permissions are computed **monotonically** via
`security/security_gate.py::apply_workspace_ceiling` — `effective = min(ceiling,
session)`. The gate is applied fail-closed at tool entry
(`agent/core/tool_executor.py:67`–`:77`).

One optional session feature flag exists and it **defaults to `False`**:
`use_workspace_lifecycle_manager`.

> Note: `kill_thoughtmachine.sh` / `kill_thoughtmachine.bat` are **process-kill**
> scripts (they force-stop the services on ports 8000/5173). They are *not* a security
> kill switch. The security kill switch is the `host_bash` / `allow-on-host` policy
> expressed through the grains above.

---

## 4. Container subsystem & DEGRADED MODE (no Docker)

The container subsystem has two layers:

1. **`DockerExecutor`** (`tools/docker_executor.py`) — per-invocation code execution, the
   `DockerCodeRunner` path.
2. **Lifecycle layer** — `infra/container_registry.py` (durable, `workspace_id`-keyed
   records) and `infra/container_manager.py`
   (live container management).

### DEGRADED MODE

When Docker is **not** present, ThoughtMachine runs in **degraded mode**: the platform
starts and the web UI / CLI / agent core all work, but any capability that requires the
sandbox (container code execution) is disabled and reported as unavailable.

- The launcher (`start_thoughtmachine.sh`) prints *"continuing without Docker (degraded
  mode)"* when the `docker` binary is simply absent.
- If Docker is present but the **daemon is down** or a **required library is missing**
  (`daemon_down` / `lib_missing`), the launcher **exits 1** in `dev` / `prod` mode.
- The preflight-only modes degrade those same conditions to a **WARNING and continue**:
  - `--check-only` (also `TM_CHECK_ONLY=1`) — validates and exits
    (`(--check-only: preflight done, nothing was started)`).
  - `--doctor` — check-only plus starts the backend and verifies `/api/health`, then
    prints `BACKEND-HEALTHY`.
- `DockerCodeRunner` **fails gracefully** everywhere it is unavailable — most visibly on
  Windows, where the sandbox is not offered at all (see §1 and the Windows contract).

So: **Docker is effectively required for sandboxed code execution.** Without it, the
product is fully usable for reasoning/UI/CLI work but cannot run code in isolation.

Related launcher flags: `--prod` serves the built frontend at
`http://127.0.0.1:8000`; dev mode runs the backend at `http://localhost:8000` plus the
Vite dev server at `:5173`.

---

## 5. Optional RAG (codebase semantic search)

Retrieval-augmented code search is an **optional** add-on, not part of the base install.

- Dependency set: `requirements-rag.txt`, installed via the `--with-rag` install flag.
- Key packages: `torch` (CPU index), `sentence-transformers>=3.0.0`, `chromadb>=0.5.0`,
  `langchain>=0.3.0`, `langchain-community>=0.3.0`.
- Weight: these pull roughly **~500 MB** of dependencies (models/index tooling), which is
  why the feature is opt-in and not installed by default.
- Documentation: [`docs/rag_for_code.md`](rag_for_code.md) covers codebase semantic
  search.

---

## 6. CURRENT LIMITATIONS

These are factual, present-tense limitations — not roadmap items.

1. **Windows has no native container path.** The Docker code sandbox is unavailable on
   Windows and `DockerCodeRunner` fails gracefully. There is **no** WSL / Cygwin / MSYS2
   fallback. Authoritative statement:
   [`docs/windows_stability_contract.md:25`](windows_stability_contract.md) and its §9
   Non-Goals.
2. **macOS Docker Desktop `--user uid:gid` bind-mount host-ownership semantics are
   UNVERIFIED.** The container runs as the host `uid:gid` (`host_user()`), which yields
   correct bind-mount ownership on **native Linux**. On **macOS with Docker Desktop**,
   file ownership is mediated by the Docker Desktop VM's uid mapping (e.g. virtiofs/gRPC-FUSE),
   which differs from a native Linux kernel; the resulting host-ownership behaviour has
   **not been tested or verified**. Treat macOS bind-mount ownership as unconfirmed.
3. **Docker is effectively required for the sandbox.** Without Docker the product runs in
   **degraded mode** and containerized code execution is disabled (see §4).
4. **RAG is optional and heavy.** It is not installed by default and adds ~500 MB of
   dependencies (§5).
5. **Packaging metadata uses a placeholder homepage.** `pyproject.toml`
   `[project.urls] Homepage` points at `https://github.com/your-org/thoughtmachine` — a
   placeholder, not a real project URL.
6. **No Qt desktop GUI ships in this repository.** There is **no** `qt_gui/` package, no
   `run_gui.py`, and no `.ui` files anywhere in the tree. The only Qt-related code is an
   *optional* PyQt6 signalling shim, `agent/presenter/gui_integration.py`, which imports
   PyQt6 behind `try/except` and falls back to no-op dummy signal objects when PyQt6 is
   absent (graceful degradation). The web UI is the supported frontend on every platform.
   Stale references to a Qt GUI remain in
   [`docs/windows_stability_contract.md:24`](windows_stability_contract.md) (PyQt6 GUI
   "excluded from build") and `PACKAGING.md`; the PyInstaller spec
   (`thoughtmachine.spec`) explicitly excludes `PyQt6*` and `qt_gui`.
7. **Documentation and coverage gaps (observed in this tree):**
   - `docs/docker_usage.md` states container defaults that are **stale**: it lists
     `512m` memory / `50000` cpu_quota and says the sandbox agent runs as **UID 1000**,
     whereas the code uses `1g` / `100000` and runs as the **host `uid:gid`**
     (`tools/docker_executor.py:475`–`:476`, `agent/config/defaults.py::host_user`).
   - `docs/installation_guide.md` contains **duplicate `## Updating` headings** and a
     truncated sentence.
   - `tests/test_installation_guide.py` asserts the guide declares macOS
     **"Not supported"**, which is **out of sync** with the current guide (macOS is now
     listed as supported). This is a doc/test inconsistency, left as-is.
   - `SECURITY.md` covers only secret-handling; `docs/security_layer.md` is obsolete.
   - Per `docs/testing/test_inventory.md`, some areas are not covered by tests (e.g. the
     Respond tool, ToolPreset, and Vault).
8. **macOS support is declared but not gated in CI.** macOS is documented as supported
   (`README.md`, `docs/installation_guide.md`) and `install.sh` handles Darwin, but the
   macOS CI jobs are **advisory** (`continue-on-error`), so macOS is not a gating
   target.

---

## 7. Packaging

Standalone binary packaging is documented separately in
[`PACKAGING.md`](../PACKAGING.md) — see that file for the full procedure. In brief: the
build uses **PyInstaller 6.x** driven by `thoughtmachine.spec` with entry point
`thoughtmachine_entry.py` (which auto-enables the bundled frontend), via
`build_thoughtmachine_exe.sh` (Linux/macOS) or `build_thoughtmachine_exe.bat` (Windows).
A one-folder build is the default (`ONE_FILE=1` for single-file). The resulting
`dist/ThoughtMachine/ThoughtMachine[.exe]` needs neither Python nor Node installed, and
excludes PyQt6/tkinter/numpy/matplotlib/pandas/test artifacts.

---

## 8. Development & CI

**Configuration.** `pyproject.toml` (`requires-python = ">=3.11"`) plus
`requirements.txt` (runtime) and `requirements-dev.txt` (adds `playwright>=1.40`,
`pytest-playwright>=0.4`). Console script: `tm-logs = agent.cli.logs:main`.

**Tests.** Tests live under `tests/`. Markers (`pyproject.toml
[tool.pytest.ini_options]`): `slow`, `integration`, `docker` (require a real Docker
daemon), `e2e` (Playwright, run with `--run-e2e`). The selection CI runs the default
suite:

```bash
python -m pytest -m "not docker and not e2e"
```

Real-daemon container tests are run explicitly:

```bash
python -m pytest -m docker
```

**Workflows** (`.github/workflows/`, triggered on both `push` and `pull_request`):

| Workflow | Purpose |
|---|---|
| `cross-platform-smoke.yml` | Linux install smoke; Windows smoke (validates the `.bat` Python section parses under `cmd.exe`, Python 3.14, imports `web_ui.backend.server`, and runs `scripts/smoke_windows.ps1` polling `/api/health`). |
| `python-version-matrix.yml` | Unit tests across Python 3.11–3.14; installer-contract check (floor-only) and a CRLF guard over the `.bat` files. |
| `first_start_ci.yml` | First-start flow on `debian:13`; `--check-only` requires a Docker daemon; `daemon_down` / `lib_missing` are WARNING+continue in `--check-only`/`--doctor` but FATAL in `dev`/`prod`. |
| `platform-matrix.yml` | Platform matrix: full suite on Ubuntu asserting the expected number of collected tests via `scripts/ci_assert_collected.py`; Windows import smoke; a thin Windows pytest selection (`test_container_registry.py`, `test_container_user_and_git_ownership.py`, `test_global_defaults.py`); macOS suite / install smoke / docker probe are **advisory** (`continue-on-error`). |

**Endpoints.** Backend health is `GET /api/health` (distinct from the legacy `/health`,
which no longer exists). The web UI is served at `http://127.0.0.1:8000` (dev uses Vite
on `:5173`); the server module is `web_ui.backend.server`.

**Branch process.** [`docs/testing/ci_process.md`](testing/ci_process.md) is the
authority: **push the branch before merging, and merge to `dev` only after CI is green
on the pushed branch.**
