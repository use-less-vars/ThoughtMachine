


# ThoughtMachine

An AI agent framework that executes code securely inside Docker containers.
Runs everywhere — **Linux**, **macOS**, and **Windows** (no WSL required).

---

## Why ThoughtMachine?

This project started from a simple frustration: most AI agent frameworks lock you
into specific models, cost a fortune at scale, or compromise on security.

ThoughtMachine is built different.

**Secure by design.** Every piece of code the agent writes runs inside an isolated
Docker container. No sandbox escapes, no accidental host modification. The
container has no network and a read-only filesystem by default. You can relax the
rules when you need to, but the safe defaults keep you out of trouble.

**No lock-in.** Use OpenAI, Anthropic, DeepSeek, Ollama, OpenRouter, or any
OpenAI-compatible endpoint. Swap models with a config change. Your workspace,
your choice.

**Efficient by default.** Larger context windows mean more tokens processed on
every call, and those add up fast over many agentic rounds — even with cached
tokens. ThoughtMachine is designed to do real work inside a **75k token** budget.
Smart summarization and a persistent Knowledge Base handle project memory, so you
don't need a million-token window to keep context. Run it on DeepSeek-v4-flash and
a full day of coding costs under $0.50. Or use a heavy model — either way, you
keep your token burn lean.

**Workspaces remember.** Point ThoughtMachine at any folder and it becomes a
workspace with a persistent Knowledge Base — architecture notes, bug logs,
lessons learned, task tracking, all surviving across sessions. The agent builds
real understanding of your project over time.

---

### What's new in V2.0 (coming soon)

Workspaces will host their own sub-agents — workers that live in the workspace
and work alongside you. Each worker gets a custom Docker container (the
"workspace office machine"), correctly privileged per workspace, with
session-level coarse security as hard limits. Default configs get you started
fast.

---

### Getting started

You're about three commands away from running your first session.
Download the repo, run the install script, set up Docker, drop your API keys in
one place, and you're riding ThoughtMachine.

Let us know how it goes. We'd love to see it tested against Claude Code, Cursor,
and the rest. Kick the tires.

---

## Quick Start — Windows

**You need to install these manually first:**

| Dependency | Where to get it |
|-----------|-----------------|
| **Python 3.12** | https://www.python.org/downloads/release/python-3125/ — click the big **Download Python 3.12.5** button. During install, check **"Add Python to PATH"**. |
| **Node.js** | https://nodejs.org/ — click the **LTS** button (left side, says e.g. "22 LTS"), not "Current". |

Then double-click (or run from cmd):

```batch
install_thoughtmachine.bat
```

This will:
1. Detect that Python and Node.js are installed
2. Create a virtual environment (`.venv`) and install Python dependencies
3. Install npm packages and build the React frontend

(A config file is created automatically on the first server start.)

When it finishes:

```batch
start_thoughtmachine.bat
```

Point your browser to **http://127.0.0.1:8000**.

> **Optional RAG support:** `install_thoughtmachine.bat --with-rag`

---

## Quick Start — Linux / macOS

```bash
# 1. Install dependencies
./install_thoughtmachine.sh

# 2. Start the Web UI
./start_thoughtmachine.sh
```

Prerequisites: **Python 3.11–3.13**, **Node.js 18+**, **Docker** (for sandboxed execution).

---

## What's Included

| Component | Description | Access |
|-----------|-------------|--------|
| **Web UI** | Full-featured browser interface (React + FastAPI + WebSocket) | `start_thoughtmachine.bat` / `.sh` → http://127.0.0.1:8000 |
| **Qt GUI** | Native desktop interface (PyQt6) | `python run_gui.py` |
| **CLI / API** | Programmatic access via FastAPI server | Server at port 8000 |
| **Docker sandbox** | Secure code execution in isolated containers | Auto-configured |
| **PyInstaller bundle** | Standalone `.exe` / binary (no Python needed) | See [PACKAGING.md](PACKAGING.md) |

---

## Packaging — Standalone Executable

Build a self-contained binary with PyInstaller:

**Linux / macOS:**
```bash
./build_thoughtmachine_exe.sh
```

**Windows:**
```batch
build_thoughtmachine_exe.bat
```

See [PACKAGING.md](PACKAGING.md) for detailed instructions, spec-file
reference, and troubleshooting.

---

## Configuration

On first run, ThoughtMachine creates a config file at:

- **Linux/macOS:** `~/.thoughtmachine/agent_config.json`
- **Windows:** `%USERPROFILE%\.thoughtmachine\agent_config.json`

Set your API key(s) in the Web UI's **Model** panel, or edit the file
directly:

```json
{
  "provider_type": "openai",
  "model": "gpt-4o",
  "api_key": "sk-..."
}
```

Supported providers: OpenAI, Anthropic, and any OpenAI-compatible endpoint
(e.g. OpenRouter, DeepSeek, Ollama).

---

## Requirements

| Dependency | Minimum | Notes |
|-----------|---------|-------|
| Python | 3.11 | 3.12 and 3.13 also supported |
| Node.js | Any recent version | Required for Web UI frontend build |
| Docker | 24+ | Required for sandboxed execution (optional without) |
| RAM | 4 GB | 8 GB recommended for local LLMs |

> **Windows users:** Install Python and Node.js manually first (links above).
No WSL, no Git Bash, no Cygwin needed — native `.bat` scripts.

---

## Project Structure

```
.
├── install_thoughtmachine.bat   # Windows installer (double-click)
├── install_thoughtmachine.sh    # Linux/macOS installer
├── start_thoughtmachine.bat     # Windows launcher
├── start_thoughtmachine.sh      # Linux/macOS launcher
├── build_thoughtmachine_exe.bat # Windows PyInstaller build
├── build_thoughtmachine_exe.sh  # Linux/macOS PyInstaller build
├── web_ui/
│   ├── backend/                 # FastAPI + WebSocket server
│   └── frontend/                # React + Vite
├── agent/                       # Core agent framework
├── tools/                       # Tool implementations
├── qt_gui/                      # Native PyQt6 desktop GUI
├── docker/                      # Docker executor config
└── PACKAGING.md                 # PyInstaller packaging guide
```

---

## License

MIT
