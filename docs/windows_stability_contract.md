# Windows Stability Contract

> **Last updated:** 2026-06-02
> **Status:** Active

This document defines the explicit stability guarantees that ThoughtMachine makes
when running on **Microsoft Windows** (10 22H2+, 11 23H2+).

---

## 1. Scope

The contract covers:

| Area | Covered |
|---|---|
| CLI (`agent.cli.main`) | ✅ Full |
| Web UI backend (`web_ui.backend.*`) | ✅ Full |
| Agent core (`agent.core.*`) | ✅ Full |
| Configuration loading / saving | ✅ Full |
| Session persistence | ✅ Full |
| Logging subsystem | ✅ Full |
| Build packaging (PyInstaller) | ✅ Full |
| PyQt6 GUI (`qt_gui/`) | ❌ Excluded from build |
| Docker executor | ❌ Not available on Windows |

---

## 2. Path Handling

**Rule:** All paths **must** use `pathlib.Path` or `os.path.join()`.
Raw string concatenation with `'/'` or `os.sep` is prohibited in production code.

| Issue | Guarantee | Mechanism |
|---|---|---|
| Drive letters (`C:\`) | Correct | `Path.resolve()`, `os.path.abspath()` |
| Backslashes in JSON config | Safe | Paths are stored as strings; `Path()` normalises automatically |
| Long paths (>260 chars) | Supported | Prefix when possible; Python 3.11+ enables long paths by default |
| User home dir (`~`) | Correct | `Path.home()` / `os.path.expanduser()` |
| Temp dir isolation | Correct | `tempfile.mkdtemp()` with prefix |

**Reference:**
- `session/store.py` → `Path(session_path)`
- `agent/config/loader.py` → `Path.home()`, `os.path.join()`
- All log paths → `os.path.join(log_dir, ...)`

---

## 3. Encoding

**Rule:** All file I/O uses explicit UTF-8 encoding.

| Context | Encoding | Enforced at |
|---|---|---|
| Config files | `utf-8` | `loader.py:125` |
| Session store | `utf-8` | `store.py` |
| Log files | `utf-8` | `logging/*.py` |
| System prompt | `utf-8` | `models.py:110` |

**Windows-specific note:** The default system encoding on Windows is often
`cp1252`. All file `open()` calls **must** pass `encoding='utf-8'` explicitly.
The CI smoke test (`scripts/windows_smoke_test.ps1`) checks that config
loading succeeds regardless of system encoding.

---

## 4. Subprocess Handling

**Rule:** No shell scripts are invoked on Windows unless cross-platform
guaranteed.

| Subprocess call | Windows behaviour | Status |
|---|---|---|
| `subprocess.run(['python', ...])` | Works (creates a new process) | ✅ |
| `subprocess.run('python ...', shell=True)` | **Do not use** — fragile quoting | ❌ Banned |
| `os.system()` | **Do not use** | ❌ Banned |
| Docker SDK | Unavailable — caught gracefully | ✅ |
| `shutil.which('executable')` | Works (appends `.exe` automatically) | ✅ |

---

## 5. Serialisation Safety

**Rule:** No non-serialisable values (callables, threads, locks) may leak into
JSON output.

| Safeguard | Location | Description |
|---|---|---|
| `@model_serializer` on `AgentConfig` | `models.py:143` | Strips `stop_check` from model dumps |
| `_sanitize_config_for_serialization()` | `loader.py:47` | Recursively removes callables before `json.dump()` |
| Atomic write (tmp + rename) | `loader.py:215-219` | Prevents partial/corrupt files |
| `_backup_config()` before overwrite | `loader.py:26` | Timestamped `.bak` files in `.config_backups/` |
| `_warn_stray_keys()` | `loader.py:158` | Warns about keys not in `AgentConfig` schema |

---

## 6. Error Recovery

**Rule:** Startup must degrade gracefully under adverse Windows conditions.

| Failure mode | Behaviour | Mechanism |
|---|---|---|
| Corrupt config JSON | Fall back to defaults | `loader.py:146-149` |
| Empty config file | Fall back to defaults | `loader.py:127-129` |
| Missing config file | Fall back to defaults | `loader.py:121-123` |
| Missing log directory | Created automatically | `service.py:100` |
| Missing workspace directory | Created automatically | `bootstrap.py` |
| API key not set | Logged warning; Agent defers error to LLM call | `bridge.py:216-218` |

---

## 7. Build & Packaging

**Rule:** The PyInstaller build must include the Windows-specific metadata.

| Requirement | Status | Location |
|---|---|---|
| PyInstaller 6.x+ | ✅ | `build_thoughtmachine_exe.sh` |
| `--hidden-import` for all lazy modules | ✅ | `thoughtmachine.spec` |
| `agent.startup_health_check` in bundle | ✅ | `thoughtmachine.spec:82` |
| `.exe` extension for Windows | ✅ | Automatic via PyInstaller |
| Console window (CLI mode) | ✅ | `thoughtmachine.spec:299` |
| UPX compression (optional) | ✅ | `thoughtmachine.spec:296` |
| Frontend bundle included | ✅ | `thoughtmachine.spec:190-197` |

---

## 8. Testing (Windows Gate)

Before every release, the following **must** pass on a Windows 10/11 machine:

1. `.\scripts\windows_smoke_test.ps1` — 8 smoke tests (Python version, imports,
   config, health check, agent instantiation, logging, session I/O)
2. `python -m agent.startup_health_check` — structural checks
3. `python -c "from agent.config import AgentConfig; print(AgentConfig())"` —
   config model instantiation
4. `python -m PyInstaller thoughtmachine.spec` — build produces
   `dist/ThoughtMachine/ThoughtMachine.exe`

---

## 9. Non-Goals (Explicitly Out of Scope)

- **Docker on Windows**: Docker Desktop is not required. The `DockerCodeRunner`
  tool will fail gracefully with a message if Docker is unavailable.
- **PyQt6 GUI**: Not bundled. The web UI is the supported frontend on Windows.
- **Windows < 10**: Windows 7/8 are not tested and not supported.
- **ARM64 Windows**: Not tested (x86-64 only).
- **Cygwin / MSYS2 / WSL**: Native `cmd.exe` or PowerShell only.

---

## 10. Maintenance

This contract is reviewed when:
- A new Windows-specific issue is resolved
- A cross-platform path/encoding/subprocess regression is fixed
- A new module is added that interacts with the file system or subprocesses

Any change that affects Windows compatibility **must** update this document.
