# ThoughtMachine Packaging Guide

This directory contains everything needed to build a standalone,
self-contained executable of ThoughtMachine using **PyInstaller**.

## Quick Start (Linux/macOS)

```bash
# 1. Build the React frontend
cd web_ui/frontend && npm install && npm run build && cd ../..

# 2. Run the build script
./build_thoughtmachine_exe.sh

# 3. Run the result
./dist/ThoughtMachine/ThoughtMachine
# → Opens http://localhost:8000 in your browser
```

## Quick Start (Windows)

```batch
REM 1. Build the React frontend
cd web_ui\frontend
call npm install
call npm run build
cd ..\..

REM 2. Run the build script
build_thoughtmachine_exe.bat

REM 3. Run the result
dist\ThoughtMachine\ThoughtMachine.exe
REM → Double-click the .exe or run from terminal
```

## Build Modes

### One-folder mode (default)
- `./build_thoughtmachine_exe.sh` — creates `dist/ThoughtMachine/` directory
- ✅ Fast startup (no extraction)
- ✅ Easier to debug
- ✅ Smaller total size on first run (no compression overhead)
- 📦 All dependencies in `dist/ThoughtMachine/_internal/`

### One-file mode
- `ONE_FILE=1 ./build_thoughtmachine_exe.sh` — creates a single executable
- ✅ Single file to distribute
- ❌ Slower startup (self-extracts to temp dir on each run)
- ❌ Larger compressed size
- 📦 Everything packed into one binary

## What's Included

| Component | Where it goes |
|-----------|--------------|
| Python application code | `_internal/` (all packages) |
| React frontend (built) | `frontend_dist/` (mounted at `/`) |
| Default resources | `resources/` (config, prompts, etc.) |
| Docker executor | `docker/` (Dockerfile for agent sandbox) |
| Third-party dependencies | All bundled in `_internal/` |

## What's Excluded

- **Qt GUI** (`qt_gui/`, `PyQt6`) — not needed for web UI
- **tkinter**, **idlelib** — bundled with CPython, never used
- **numpy**, **matplotlib**, **pandas** — not project dependencies
- **test** / **unittest** / **distutils** — development-only

## System Requirements of the Built Executable

- **OS**: Windows 10+, macOS 12+, or Linux (glibc 2.28+)
- **No Python required** — the executable is fully standalone
- **No Node.js required** — the frontend is pre-built
- **Disk space**: ~500 MB for one-folder mode, ~250 MB for one-file mode
- **RAM**: 2 GB minimum, 4 GB+ recommended (for running LLM agents)

## First Run

On first launch, ThoughtMachine automatically:
1. Creates `~/.thoughtmachine/` directory
2. Copies default config, system prompt, security policy, and provider list
3. Creates `~/.thoughtmachine/sessions/` for session persistence
4. Logs details to console

Point your browser to **http://localhost:8000** and configure your API key
in the Settings panel.

## Troubleshooting

### "Frontend not built" page on /
The frontend wasn't bundled. Rebuild with:
```bash
cd web_ui/frontend && npm install && npm run build
```
Then run PyInstaller again.

### Missing module errors at startup
Add the missing module to the `hiddenimports` list in `thoughtmachine.spec`
(or `--hidden-import` flag in the build script).

### Executable is very large
- Exclude unused packages (already done in the spec)
- Run with `upx=True` (requires UPX installed)
- Use one-file mode for smaller distribution size

## File Reference

| File | Purpose |
|------|---------|
| `thoughtmachine.spec` | PyInstaller spec file (main config) |
| `thoughtmachine_entry.py` | Entry point wrapper (auto-enables frontend) |
| `build_thoughtmachine_exe.sh` | Linux/macOS build script |
| `build_thoughtmachine_exe.bat` | Windows build script |
