# ThoughtMachine Installation Guide

This guide covers installing and running ThoughtMachine from a source checkout on
Linux and Windows. It is written for engineers: it tells you what the scripts
actually do, what they check, and what they will not do for you.

## Scope

- **Supported:** Linux (Debian/Ubuntu, x86_64) and Windows (x64).
- **Not supported:** macOS. The Linux installer refuses to run on macOS; the
  recommended path is a Linux VM/container or manual setup of the components
  (Python, Node, Docker Desktop) followed by `./start_thoughtmachine.sh`.
- **Platform gaps are explicit.** The Windows launcher does **not** support the
  Docker executor (see `docs/windows_stability_contract.md`), and the Linux
  installer does **not** cover macOS or non-Debian distros.

| Platform | Installer | Launcher | Status |
| --- | --- | --- | --- |
| Linux x86_64 (Debian/Ubuntu) | `./install.sh` | `./start_thoughtmachine.sh` | Supported |
| Windows x64 | `install_thoughtmachine.bat` | `python start_windows.py` | Supported |
| macOS | — | `./start_thoughtmachine.sh` (manual setup) | **Not supported** |

## Prerequisites

| Requirement | Linux | Windows | Notes |
| --- | --- | --- | --- |
| Python | >= 3.11 (`install.sh` check `[1/5]`, critical) | 3.11 – 3.13 (checked via `py` launcher, then `python`, then `python3`) | The Windows installer refuses Python outside 3.11–3.13. |
| Node.js | >= 18 (`install.sh` check `[5/5]`, critical) | >= 18 | **Not vendored.** Must be installed manually on both platforms. |
| Docker | Recommended (daemon must be running; group membership optional) | Docker Desktop recommended; warning-only | Docker is optional for `--check-only`/`--doctor` and for the Windows dev launcher; many features are disabled without it. |
| curl | Required | — | Used by `install.sh`. |
| Network | Required | Required | `pip install -r requirements.txt` and `npm install` download dependencies. |

> On Windows, installing Node.js LTS from nodejs.org is a manual step — the
> installer does not vendor Node.

## Linux installation

The one-command installer is `install.sh`:

```bash
chmod +x install.sh
./install.sh
```

It is **idempotent** and does **not** use `set -e`: every check runs, failures are
reported, and a summary is printed at the end. It refuses to run on the wrong
platform before doing anything:

```
ERROR: macOS is not supported by this installer.
       Install Docker Desktop, Python >= 3.11 and Node.js >= 18,
       then run ./start_thoughtmachine.sh directly.
```

```
ERROR: this is the Linux installer; on Windows use install_thoughtmachine.bat.
```

It also gates on architecture (`ERROR: unsupported architecture: $UNAME_M
(expected x86_64/amd64).`) and distribution (`ERROR: unsupported distribution:
${DISTRO_ID:-unknown} (expected debian or ubuntu).`).

The five checks, in order:

1. **[1/5] Python >= 3.11** — critical. Installs `python3-venv` (needs `sudo`)
   if missing.
2. **[2/5] Docker daemon** — critical. May offer to install `docker.io` via
   `sudo apt` and/or start it with `sudo systemctl enable --now docker`.
3. **[3/5] Docker group** — non-critical. If your user is not in the `docker`
   group, it prints: `Re-login or run: newgrp docker`.
4. **[4/5] venv** — critical. Runs `doctor --ensure-venv`, which creates
   `.venv` and `pip install -r requirements.txt`.
5. **[5/5] Node.js >= 18** — critical.

On success the summary prints `[ok]`/`[--]` lines and ends with:

```
Next step: ./start_thoughtmachine.sh
```

> `sudo` is needed during install for `python3-venv` and for adding your user to
> the `docker` group.

### Running on Linux

```bash
./start_thoughtmachine.sh            # dev mode: backend + Vite frontend
./start_thoughtmachine.sh --prod     # backend serves the built frontend
./start_thoughtmachine.sh --check-only
./start_thoughtmachine.sh --doctor
```

- `--check-only` (also honored via `TM_CHECK_ONLY=1`) runs the preflight checks
  and exits 0 — nothing is started. In this mode Docker problems degrade to a
  warning:

  ```
  WARNING: Docker is not usable (reason: ...) - continuing in --check-only mode.
  ```

  and it finishes with:

  ```
    (--check-only: preflight done, nothing was started)
  ```

- `--doctor` is check-only plus starting the backend and verifying
  `/api/health`; it prints `BACKEND-HEALTHY` and keeps running. Use it when
  reporting problems.
- The 8 preflight checks: required tools, venv (critical), Docker (critical
  outside check-only/doctor), stale containers (reported, not removed), ports
  8000/5173 free (fatal if busy), Node >= 18, `~/.thoughtmachine` writable
  (`Vault not writable. Fix with: sudo chown -R $USER ~/.thoughtmachine`), and
  locale (`LANG set to C.UTF-8`).

In dev mode the backend listens on http://localhost:8000 and the Vite frontend
on http://localhost:5173. Backend startup logs are mirrored to
`logs/backend_startup.log`.

## Windows installation

Run the batch installer from a **Command Prompt** (not PowerShell):

```
install_thoughtmachine.bat [--with-rag]
```

- `--with-rag` additionally installs `requirements-rag.txt`
  (sentence-transformers / ChromaDB / CPU PyTorch, roughly 500 MB).
- Checks Python 3.11–3.13 (via `py` launcher, then `python`, then `python3`),
  Node.js 18+, npm, and Docker Desktop. Docker Desktop missing is only a
  warning: `Docker Desktop is required for full functionality. Some features
  will be disabled.`
- Creates `.venv`, runs `pip install -r requirements.txt`, then
  `npm install && npm run build` inside `web_ui\frontend`.
- Writes a log to `install.log`.

Next steps (printed by the installer):

```
python start_windows.py             # dev: backend + Vite frontend
python start_windows.py --prod      # serves the built frontend
```

Open http://127.0.0.1:8000 (backend) or http://127.0.0.1:5173 (dev frontend).
User config lives at `%USERPROFILE%\.thoughtmachine\agent_config.json`.

`start_windows.py` (the Phase 1 rewrite) requires the venv created by the
installer, verifies the venv Python is >= 3.11, warns (does not fail) if Docker
Desktop is missing, frees ports 8000/5173 by killing stale listeners, starts
the backend (`.venv\Scripts\python.exe -m web_ui.backend.server`), waits for
health, then starts Vite. `Ctrl+C` shuts everything down cleanly.

To stop everything forcefully:

```
kill_thoughtmachine.bat
```

This kills Vite on ports 5173–5177, the Python backend on port 8000, and any
windows titled `ThoughtMachine Backend*` / `Vite Dev Server*`. It is safe to
run when nothing is running.

There is also a PowerShell smoke test, `scripts/smoke_windows.ps1`, for
verifying an installation end-to-end. For the full Windows history and the
guarantees the project commits to, see
[docs/windows_installation_saga.md](windows_installation_saga.md) and
[docs/windows_stability_contract.md](windows_stability_contract.md).

## First run: the onboarding wizard

On first launch, the web UI shows the onboarding wizard (4 screens) whenever
`GET /api/onboarding/status` reports `onboarding_complete: false`. The wizard
never blocks you: **Skip** on any screen calls `POST /api/onboarding/complete`
and moves on — skipping counts as done, and even a failed skip request is
ignored so you are never trapped in the wizard.

1. **Welcome** — three layer cards (Landing / Workspace / Session).
2. **Provider** — provider type (`openai`, `openai_compatible` with a required
   base URL, or `anthropic`), base URL, masked API key (with show/hide toggle),
   and model. "Test connection" calls `POST /api/onboarding/test-connection`;
   the backend never echoes the API key back.
3. **Workspace** — name is turned into a slug (`~/workspaces/<slug>`), the
   folder is created via `POST /api/browse/create` and registered via
   `POST /api/workspace/resolve`.
4. **Summary** — provider + workspace recap; **Finish** calls
   `POST /api/onboarding/complete`.

Onboarding is also considered complete if you already have a provider profile
(`~/.thoughtmachine/providers.json`) or a registered workspace, so existing
setups skip the wizard.

## Verifying the installation

With the backend running, the canonical health endpoint is:

```bash
curl -i http://127.0.0.1:8000/api/health
```

Expect `200 OK` with a JSON body like
`{"status": "ok", "service": "thoughtmachine-web-ui", "revision": ...}`.
The legacy `/health` endpoint still works as a back-compat mirror. To check
whether the Docker daemon is reachable from the backend:

```bash
curl http://127.0.0.1:8000/api/health/containers
```

If the backend starts but Docker features are missing, the health checks will
report it — do not assume Docker is working just because the backend is up.

## Updating

There is no separate update script. To update from a source checkout:

```bash
git pull                        # Windows: git pull on the checkout, then re-run the installer
./install.sh                    # Linux: re-runs all 5 checks; creates .venv if missing
pip install -r requirements.txt # Windows: re-run install_thoughtmachine.bat to refresh deps + frontend build
```

`install.sh` is idempotent, so re-running it after a `git pull` is safe. On
## Updating

There is no separate update script. To update from a source checkout:

```bash
# Linux
git pull
./install.sh     # re-runs all 5 checks; creates .venv if missing
```

```bat
:: Windows
git pull
install_thoughtmachine.bat   :: refresh deps + frontend build (add --with-rag if used)
```

`install.sh` is idempotent, so re-running it after a `git pull` is safe. On
Windows, re-run `install_thoughtmachine.bat` (optionally with `--with-rag`).


## Uninstalling

There is no uninstaller. Removal is manual:

- Linux: delete the checkout directory (`.venv` is inside it) and remove
  `~/.thoughtmachine` if you want to wipe user data.
- Windows: delete the checkout directory, run `kill_thoughtmachine.bat` first
  to stop any running processes, and remove `%USERPROFILE%\.thoughtmachine` to
  wipe user data.

User data (config, workspaces, session state) lives in `~/.thoughtmachine` /
`%USERPROFILE%\.thoughtmachine`, never in the checkout.

## Troubleshooting

- Run `./start_thoughtmachine.sh --doctor` (Linux) and paste its full output
  into any bug report — the doctor output is the first thing maintainers ask
  for. On Windows, include the contents of `install.log` and the output of
  `python start_windows.py`.
- Ports 8000/5173 busy → the launcher refuses to start. Kill the stale
  processes (`kill_thoughtmachine.bat` on Windows) and retry.
- Vault not writable → `sudo chown -R $USER ~/.thoughtmachine` (Linux).
- Docker not usable in normal mode → the launcher fails with
  `Start it with:  sudo systemctl enable --now docker` (Linux); on Windows,
  start Docker Desktop and retry.
- Locale warnings → set `LANG` to a UTF-8 locale such as `C.UTF-8`.

For the Windows-specific debugging history, see
[docs/windows_installation_saga.md](windows_installation_saga.md). For what is
guaranteed to keep working on Windows, see
[docs/windows_stability_contract.md](windows_stability_contract.md). To run the tests yourself, use the pytest suite under `tests/`; the CI smoke workflow (`.github/workflows/cross-platform-smoke.yml`) exercises the Windows installer and launcher end-to-end.
