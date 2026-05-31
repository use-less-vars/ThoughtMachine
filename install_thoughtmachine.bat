@echo off
REM ──────────────────────────────────────────────────────────────────────────────
REM install_thoughtmachine.bat
REM  Windows-native install script for ThoughtMachine.
REM  Creates venv, installs Python deps, installs npm deps & builds frontend.
REM ──────────────────────────────────────────────────────────────────────────────

setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
set "PROJECT_DIR=%SCRIPT_DIR%"

echo ============================================
echo   ThoughtMachine — Install (Windows)
echo ============================================
echo.

REM ── Parse flags ────────────────────────────────────────────────────────────
set INSTALL_RAG=false

:parse_args
if not "%~1"=="" (
    if /i "%~1"=="--with-rag" (
        set INSTALL_RAG=true
        echo   ^> RAG support requested (codebase search, embeddings)
    ) else if /i "%~1"=="--help" (
        goto :show_help
    ) else if /i "%~1"=="-h" (
        goto :show_help
    ) else (
        echo   ^? Unknown argument: %~1
        echo   Usage: %~nx0 [--with-rag]
        echo.
        echo     --with-rag    Also install RAG dependencies (sentence-transformers,
        echo                   ChromaDB, CPU-only PyTorch ~500 MB)
        echo     --help, -h    Show this help
        pause
        exit /b 1
    )
    shift
    goto :parse_args
)
goto :after_help

:show_help
echo   Usage: %~nx0 [--with-rag]
echo.
echo     --with-rag    Also install RAG dependencies (sentence-transformers,
echo                   ChromaDB, CPU-only PyTorch ~500 MB)
echo     --help, -h    Show this help
echo.
pause
exit /b 0

:after_help

REM ── 0. Enforce admin for winget auto-install (optional) ─────────────────────
REM    We don't require admin, but winget install needs it.
REM    If Python/Node are missing, the user may need to install manually.

REM ── 1. Check prerequisites ────────────────────────────────────────────────────
echo.
echo [1/5] Checking prerequisites...
echo.

REM Check Python
set PYTHON_CMD=
for %%c in (python3.12 python3.11 python3 python py) do (
    where %%c >nul 2>nul
    if not errorlevel 1 (
        for /f "tokens=2 delims= " %%v in ('%%c --version 2^>nul') do (
            set PYTHON_VER=%%v
        )
        if defined PYTHON_VER (
            for /f "tokens=1,2 delims=." %%a in ("!PYTHON_VER!") do (
                set PY_MAJOR=%%a
                set PY_MINOR=%%b
            )
            if !PY_MAJOR!==3 (
                if !PY_MINOR! GEQ 11 (
                    set PYTHON_CMD=%%c
                    echo   ^^! Found %%c (Python !PYTHON_VER!)
                    if !PY_MINOR! GEQ 14 (
                        echo   ^?  Python !PY_MAJOR!.!PY_MINOR! is very new — some packages may lack wheels.
                        echo      If pip install fails, try Python 3.11 or 3.12.
                    )
                    goto :python_found
                )
            )
        )
    )
)

REM Python not found — try winget
echo   ^> Python not found. Attempting auto-install via winget...
echo   (Pin: Python 3.12 — 3.14+ lacks package wheels)
echo.
echo   Note: winget auto-install may require administrator privileges.
echo   If it fails, install Python 3.12 manually from https://www.python.org/downloads/
echo   Then re-run this script.
echo.
winget install --silent --accept-package-agreements Python.Python.3.12 2>nul
if errorlevel 1 (
    winget install --silent --accept-package-agreements Python.Python.3.11 2>nul
)
REM Check again
for %%c in (python3.12 python3.11 python3 python py) do (
    where %%c >nul 2>nul
    if not errorlevel 1 (
        for /f "tokens=2 delims= " %%v in ('%%c --version 2^>nul') do (
            set PYTHON_VER=%%v
        )
        if defined PYTHON_VER (
            for /f "tokens=1,2 delims=." %%a in ("!PYTHON_VER!") do (
                set PY_MAJOR=%%a
                set PY_MINOR=%%b
            )
            if !PY_MAJOR!==3 if !PY_MINOR! GEQ 11 (
                set PYTHON_CMD=%%c
                echo   ^^! Python installed: !PYTHON_VER!
                goto :python_found
            )
        )
    )
)

echo   ^? Python 3.11+ not found and could not be auto-installed.
echo   Install Python 3.12 from https://www.python.org/downloads/
echo   Make sure to check "Add Python to PATH" during installation.
echo   Then re-run this script.
pause
exit /b 1

:python_found
echo.

REM Check Node.js
where node >nul 2>nul
if errorlevel 1 (
    echo   ^> Node.js not found. Attempting auto-install via winget...
    echo   Note: winget auto-install may require administrator privileges.
    echo   If it fails, install Node.js LTS manually from https://nodejs.org/
    echo.
    winget install --silent --accept-package-agreements OpenJS.NodeJS.LTS 2>nul
    where node >nul 2>nul
    if errorlevel 1 (
        echo   ^? Node.js not found and could not be auto-installed.
        echo   Install Node.js LTS from https://nodejs.org/
        echo   Then re-run this script.
        pause
        exit /b 1
    )
)

for /f "tokens=1 delims=v" %%v in ('node --version 2^>nul') do set NODE_VER=%%v
if not defined NODE_VER set NODE_VER=unknown
echo   ^^! Node.js (!NODE_VER!)
echo   ^^! npm (!NODE_VER!)

REM ── 2. Create venv ────────────────────────────────────────────────────────────
echo.
echo [2/5] Creating Python virtual environment...
echo   ^> Using: !PYTHON_CMD!

set "VENV_DIR=%PROJECT_DIR%.venv"

if exist "%VENV_DIR%\Scripts\activate.bat" (
    echo   ^> Venv already exists at %VENV_DIR%
) else (
    echo   ^> Running: !PYTHON_CMD! -m venv .venv
    call !PYTHON_CMD! -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo   ^? Failed to create venv.
        echo   Try: !PYTHON_CMD! -m ensurepip --upgrade
        pause
        exit /b 1
    )
    echo   ^^! Created venv at %VENV_DIR%
)

REM Activate
if exist "%VENV_DIR%\Scripts\activate.bat" (
    call "%VENV_DIR%\Scripts\activate.bat"
) else (
    echo   ^? Cannot find venv activate script at %VENV_DIR%\Scripts\activate.bat
    pause
    exit /b 1
)
echo   ^> Activated: %VENV_DIR%

REM ── 3. Install Python dependencies ────────────────────────────────────────────
echo.
echo [3/5] Installing Python dependencies...

echo   ^> Upgrading pip...
python -m pip install --upgrade pip
if errorlevel 1 (
    echo   ^? pip upgrade failed
    pause
    exit /b 1
)

echo   ^> Installing core Python packages from requirements.txt...
pip install -r "%PROJECT_DIR%requirements.txt"
if errorlevel 1 (
    echo   ^? pip install failed — see output above
    echo   Try: pip install --pre -r requirements.txt
    pause
    exit /b 1
)
echo   ^^! Core Python deps installed

if /i "!INSTALL_RAG!"=="true" (
    echo.
    echo   ^> Installing RAG dependencies (CPU-only PyTorch, ChromaDB, etc.)...
    if exist "%PROJECT_DIR%requirements-rag.txt" (
        pip install -r "%PROJECT_DIR%requirements-rag.txt"
        if errorlevel 1 (
            echo   ^?  RAG installation had issues (exit code !errorlevel!)
            echo      You can install manually later: pip install -r requirements-rag.txt
        ) else (
            echo   ^^! RAG dependencies installed
        )
    ) else (
        echo   ! requirements-rag.txt not found, skipping RAG install
    )
)

REM ── 4. Install npm dependencies ───────────────────────────────────────────────
echo.
echo [4/5] Installing npm dependencies...

set "FRONTEND_DIR=%PROJECT_DIR%web_ui\frontend"
if exist "%FRONTEND_DIR%\package.json" (
    pushd "%FRONTEND_DIR%"
    echo   ^> Installing npm packages (this may take a while)...
    call npm install
    if errorlevel 1 (
        echo   ^? npm install failed — see output above
        popd
        pause
        exit /b 1
    )
    echo   ^^! npm deps installed
    popd
) else (
    echo   ! Frontend directory not found at %FRONTEND_DIR%, skipping.
)

REM ── 5. Build frontend ─────────────────────────────────────────────────────────
echo.
echo [5/5] Building frontend...

if exist "%FRONTEND_DIR%\package.json" (
    pushd "%FRONTEND_DIR%"
    echo   ^> Building frontend bundle...
    call npm run build
    if errorlevel 1 (
        echo   ^? Frontend build failed — see output above
        popd
        pause
        exit /b 1
    )
    echo   ^^! Frontend built
    popd
) else (
    echo   ! Skipping frontend build.
)

echo.
echo ============================================
echo   ^^! Install complete!
echo.
echo   Next steps:
echo     1. Double-click: start_thoughtmachine.bat
echo.
echo     2. Open http://127.0.0.1:8000 in your browser
echo.
echo   Your config file will be created automatically
echo   at %%USERPROFILE%%\.thoughtmachine\agent_config.json on first run.
echo ============================================
echo.

pause
