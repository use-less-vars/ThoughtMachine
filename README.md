# ThoughtMachine

An AI agent framework that executes the code it writes inside isolated Docker
containers. Runs natively on **Linux**, **macOS**, and **Windows** — no WSL,
Git Bash, or Cygwin required.

- **Isolated execution.** Agent-generated code runs in a Docker container that has
  no network and a read-only root filesystem by default; relax the policy when a
  task needs it.
- **No model lock-in.** OpenAI, Anthropic, DeepSeek, Ollama, OpenRouter, or any
  OpenAI-compatible endpoint — change the config, not the code.
- **Lean by design.** Summarisation plus a persistent Knowledge Base keep long
  agentic sessions inside a ~75k-token working budget.
- **Persistent workspaces.** Point it at any folder; it keeps architecture notes,
  bug logs, and task tracking that survive across sessions.
- **Agent & Engineer modes.** Engineer mode orchestrates worker sub-agents, each
  in its own thread/context, returning a structured status/confidence envelope.

## Platform support

| Capability | Linux | macOS | Windows |
|---|---|---|---|
| Web UI (React + FastAPI + WebSocket) | ✅ | ✅ | ✅ |
| CLI / programmatic API | ✅ | ✅ | ✅ |
| Docker code sandbox | ✅ | ✅ | ❌ fails gracefully |

The web UI is the supported frontend on every platform. On Windows the Docker
sandbox is unavailable by design — the tool reports this and the rest of the
agent keeps working; see
[docs/windows_stability_contract.md](docs/windows_stability_contract.md).

## Quick Start — Linux / macOS

```bash
# 1. Prerequisite checks + venv + Python deps (Python >= 3.11, Docker, Node >= 18)
./install.sh

# 2. Web UI dependencies (install.sh does NOT run npm install)
cd web_ui/frontend && npm install && cd ../..

# 3. Launch: backend on :8000 + Vite dev server on :5173
./start_thoughtmachine.sh
```

Open **http://127.0.0.1:5173** (Vite proxies `/api` and `/ws` to the backend on
:8000). For a single-process production run, where the backend serves the built
frontend directly:

```bash
./start_thoughtmachine.sh --prod   # http://127.0.0.1:8000
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
install_thoughtmachine.bat    REM venv + Python deps + npm install + frontend build
start_thoughtmachine.bat      REM open http://127.0.0.1:8000
```

The installer refuses to continue if Python is older than 3.11 or Node.js older
than 18. Docker Desktop is optional on Windows — the sandbox is unavailable
there, so those tools fail gracefully.

## What's Included

| Component | Description | Access |
|---|---|---|
| Web UI | React + FastAPI + WebSocket browser interface | `start_thoughtmachine.{sh,bat}` → :8000 (:5173 in dev) |
| CLI / API | Programmatic access to the agent | FastAPI server, port 8000 |
| Docker sandbox | Isolated code execution | Auto-configured (Linux/macOS) |
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
| Docker | a recent engine | Required for the sandbox; optional on Windows |

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
├── start_thoughtmachine.sh        # Linux/macOS launcher (dev / --prod / --check-only / --doctor)
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

MIT
