# ThoughtMachine

ThoughtMachine is a capability management platform. An operator grants an AI agent controlled access to computer capabilities. The security layer is the product - grants are enforced structurally, in code, not by prompt. The state layer is what makes the security layer legible - every container, every permission, every state change is visible and reconcilable. A machine that is safe but opaque is a machine you cannot use. A machine that is visible but unsafe is a machine you should not. ThoughtMachine is both.

V2 was the era where the security layer was the product. That layer is built and tested. This is the era where the state layer stops lying - every subsystem that holds state now reconciles, or reports that it cannot. The machine is safe; the machine is now also honest.

- **Isolated execution.** Agent-generated code runs in a Docker container that has
  no network and a read-only root filesystem by default; relax the policy when a
  task needs it.
- **No model lock-in.** OpenAI, Anthropic, DeepSeek, Ollama, OpenRouter, or any
  OpenAI-compatible endpoint — change the config, not the code.
- **Lean by design.** Summarisation plus a persistent Knowledge Base keep long
  agentic sessions inside a working budget.
- **Persistent workspaces.** Point it at any folder; it keeps architecture notes,
  bug logs, and task tracking that survive across sessions.
- **Agent & Engineer modes.** Engineer mode orchestrates worker sub-agents, each
  in its own thread/context, returning a structured status/confidence envelope.

ThoughtMachine runs for roughly **1 Euro per 1–2 hours of runtime** on an 80k-token working budget with DeepSeek 4.1. That budget is not a limitation of the model — it is the point. Where other systems solve the "long-range" problem by paying for ever-larger context windows, ThoughtMachine solves it by **layering**. The main agent delegates legwork to workers; workers read, summarise, and discard; only dense conclusions return. The pruning mechanism keeps the active window lean by summarising history into the Knowledge Base. Deep analysis without huge context windows — and without the cost that follows from re-sending a 500k window every round. Layering plus pruning avoids that by construction.

## Platform support

| Capability | Linux | macOS | Windows |
|---|---|---|---|
| Web UI (React + FastAPI + WebSocket) | ✅ | ✅ | ✅ |
| CLI / programmatic API | ✅ | ✅ | ✅ |
| Docker code sandbox | ✅ | ✅ | ✅ (degrades gracefully without Docker) |

The web UI is the supported frontend on every platform. Docker works on Windows
whenever a Docker engine is available; without one the containerized tools
degrade gracefully — a tool returns a structured error and the rest of the agent
keeps working; see
[docs/windows_stability_contract.md](docs/windows_stability_contract.md).

## Quick Start — Linux / macOS

```bash
# 1. Install prerequisites first: Python 3.11+, Node.js 18+ (with npm), and Docker.
#    Then run the installer: it creates the venv, installs Python deps, and
#    installs the Web UI dependencies itself (npm ci, or npm install).
./install.sh

# 2. Launch: production mode — one backend on :8000 serving the built frontend
./start_thoughtmachine.sh
```

Open **http://127.0.0.1:8000** (production mode: the backend serves the built
frontend directly). For development with hot-reload, where a Vite dev server on
:5173 proxies `/api` and `/ws` to the backend:

```bash
./start_thoughtmachine.sh --dev   # http://127.0.0.1:5173
```

What each script does (and does not do) is spelled out in
[docs/installation_guide.md](docs/installation_guide.md).

## Quick Start — Windows

Install these manually first:

| Dependency | Source |
|---|---|
| **Python 3.11+** (3.14 supported) | https://www.python.org/downloads/ — tick "Add Python to PATH" |
| **Node.js 18+** (LTS) | https://nodejs.org/ |

Then, from a `cmd` prompt:

```batch
install_thoughtmachine.bat        REM venv + Python deps + npm install + frontend build
start_thoughtmachine.bat          REM development mode — open http://127.0.0.1:5173
start_thoughtmachine.bat --prod   REM production mode — open http://127.0.0.1:8000
```

The installer refuses to continue if Python is older than 3.11 or Node.js older
than 18. Docker Desktop is optional on Windows — without it, the containerized
tools degrade gracefully and the rest of the agent keeps working.

## What's Included

| Component | Description | Access |
|---|---|---|
| Web UI | React + FastAPI + WebSocket browser interface | Launcher prints the URL: `.sh` defaults to :8000 (prod); `.bat` defaults to :5173 (dev) |
| CLI / API | Programmatic access to the agent | FastAPI server, port 8000 |
| Docker sandbox | Isolated code execution | Requires a Docker engine (optional — tools degrade gracefully without it) |
| Engineer mode | Orchestrator + worker sub-agents, structured protocol, WorkingDocument | Create an Engineer session in the Web UI |
| Standalone binary | PyInstaller bundle (`.exe` / ELF) | See [PACKAGING.md](PACKAGING.md) |

## Configuration

On first start ThoughtMachine creates `~/.thoughtmachine/agent_config.json`
(`%USERPROFILE%\.thoughtmachine\agent_config.json` on Windows). Set API keys in
the Web UI's **Model** panel, or edit the file:

```json
{ "provider_type": "openai", "model": "gpt-4o", "api_key": "sk-..." }
```

Keep keys out of the repository — see [SECURITY.md](SECURITY.md).

## Requirements

| Dependency | Minimum | Notes |
|---|---|---|
| Python | 3.11 | `requires-python = ">=3.11"`; tested on 3.14 |
| Node.js | 18 | Required for the Web UI frontend |
| Docker | a recent engine | Required for the primary execution path; on Windows without Docker, containerized tools degrade gracefully |

## Development & Testing

```bash
python -m pytest -m "not docker and not e2e"   # the selection CI runs
python -m pytest -m docker                     # requires a real Docker daemon
```

Markers: `slow`, `integration`, `docker`, `e2e` (Playwright, `--run-e2e`); tests
live under `tests/`. Push the branch before merging, and merge only after CI is
green.

## Project Structure

```
├── install.sh                     # Linux/macOS installer — canonical path (used by CI)
├── install_thoughtmachine.sh      # legacy all-in-one installer (also builds frontend, bootstraps vault)
├── start_thoughtmachine.sh        # Linux/macOS launcher (default: prod; flags: --dev / --check-only / --doctor)
├── install_thoughtmachine.bat     # Windows installer
├── start_thoughtmachine.bat       # Windows launcher
├── start_windows.py               # portable Windows launcher (absolute paths)
├── kill_thoughtmachine.sh/.bat    # force-stop :8000 and :5173-5177
├── build_thoughtmachine_exe.sh/.bat   # PyInstaller builds
├── web_ui/
│   ├── backend/                   # FastAPI + WebSocket server
│   └── frontend/                  # React + Vite
├── agent/                         # core agent framework
├── tools/                         # tool implementations
├── tests/                         # pytest suite
├── docs/                          # guides + architecture notes
└── PACKAGING.md                   # PyInstaller packaging guide
```

## License

ThoughtMachine is licensed under the **Business Source License 1.1**. Free for individual and non-production use; not permitted for competing offerings. On the fourth anniversary of each release, the license converts automatically to Apache 2.0. See [LICENSE](LICENSE) for the full terms.
