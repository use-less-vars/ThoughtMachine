@echo off
REM ---------------------------------------------------------------------------
REM install_thoughtmachine.bat
REM  Windows installer for ThoughtMachine.
REM  Checks prerequisites, then creates venv, installs deps, builds frontend.
REM
REM  Prerequisites (install manually if missing):
REM    - Python 3.11 or 3.12 from https://www.python.org/downloads/
REM    - Node.js LTS from https://nodejs.org/
REM ---------------------------------------------------------------------------

setlocal enabledelayedexpansion
cd /d "%~dp0"

set "SCRIPT_DIR=%~dp0"

echo ============================================
echo   ThoughtMachine - Install
echo ============================================
echo.

REM -- Parse flags -------------------------------------------------------------
set INSTALL_RAG=false

:parse_args
if "%~1"=="" goto :after_help
if /i "%~1"=="--with-rag" (
    set INSTALL_RAG=true
    echo   [i] RAG support enabled (codebase search embeddings)
    shift
    goto :parse_args
)
if /i "%~1"=="--help" goto :show_help
if /i "%~1"=="-h" goto :show_help

echo   [x] Unknown argument: %~1
echo.
echo   Usage: %~nx0 [--with-rag]
echo.
echo     --with-rag    Also install RAG dependencies
echo     --help, -h    Show this help
pause
exit /b 1

:show_help
echo   Usage: %~nx0 [--with-rag]
echo.
echo     --with-rag    Also install RAG dependencies (sentence-transformers,
echo                   ChromaDB, CPU-only PyTorch ~500 MB)
echo     --help, -h    Show this help
echo.
echo   Prerequisites (install manually if missing):
echo     - Python 3.11+  from https://www.python.org/downloads/
echo     - Node.js LTS   from https://nodejs.org/
echo.
pause
exit /b 0

:after_help

REM -- 1. Check prerequisites -------------------------------------------------
echo.
echo [1/5] Checking prerequisites...
echo.
echo   --- Python ---

set PYTHON_CMD=
set PYTHON_VER=
set PYTHON_OK=0
for %%c in (python3.12 python3.11 python3 python py) do (
    where %%c >nul 2>nul
    if not errorlevel 1 (
        for /f "tokens=2 delims= " %%v in ('%%c --version 2^>nul') do set PYTHON_VER=%%v
        if defined PYTHON_VER (
            for /f "tokens=1,2 delims=." %%a in ("!PYTHON_VER!") do (
                if %%a==3 if %%b GEQ 11 if %%b LEQ 13 (
                    set PYTHON_CMD=%%c
                    set PYTHON_OK=1
                )
            )
        )
    )
)

if !PYTHON_OK!==1 (
    echo   [+] %PYTHON_CMD% (Python %PYTHON_VER%)
) else (
    if defined PYTHON_VER (
        echo   [x] Python !PYTHON_VER! found but not supported.
        echo       Need Python 3.11, 3.12, or 3.13.
        echo       !PYTHON_VER! is too new (lacks package wheels).
    ) else (
        echo   [x] Python not found.
    )
)

echo.
echo   --- Node.js ---

set NODE_OK=0
where node >nul 2>nul
if not errorlevel 1 (
    for /f "tokens=1 delims=v" %%v in ('node --version') do set NODE_VER=%%v
    echo   [+] Node.js (v!NODE_VER!)
    set NODE_OK=1
) else (
    echo   [x] Node.js not found. Install Node.js LTS from:
    echo       https://nodejs.org/
    echo       Then close this window and open a NEW one.
)

echo.
echo   --- npm ---

set NPM_OK=0
if !NODE_OK!==1 (
    where npm >nul 2>nul
    if not errorlevel 1 (
        for /f %%v in ('npm --version') do set NPM_VER=%%v
        echo   [+] npm (v!NPM_VER!)
        set NPM_OK=1
    ) else (
        echo   [x] npm not found (should come with Node.js).
        echo       Reinstall Node.js and select "npm package manager".
    )
) else (
    echo   [ ] (skipped - Node.js missing)
)

echo.
echo   --- Summary ---

set ALL_OK=1
if !PYTHON_OK!==0 (
    set ALL_OK=0
    echo   [FAIL] Python 3.11-3.13 required - install or upgrade
)
if !NODE_OK!==0 (
    set ALL_OK=0
    echo   [FAIL] Node.js LTS required - install from https://nodejs.org/
)
if !NPM_OK!==0 (
    set ALL_OK=0
    echo   [FAIL] npm required - reinstall Node.js with npm
)

if !ALL_OK!==1 (
    echo   [PASS] All prerequisites met.
    echo.
    echo   Starting install...
    echo.
) else (
    echo.
    echo   Fix the issues above, then run this script again.
    echo   Note: You may need to close this window and open a
    echo   NEW Command Prompt after installing.
    pause
    exit /b 1
)

REM -- 2. Create venv ---------------------------------------------------------
echo [2/5] Creating Python virtual environment...

set "VENV_DIR=%SCRIPT_DIR%.venv"

if exist "%VENV_DIR%\Scripts\activate.bat" (
    echo   [i] Virtual environment already exists at %VENV_DIR%
) else (
    echo   Running: !PYTHON_CMD! -m venv .venv
    call !PYTHON_CMD! -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo.
        echo   [x] Failed to create virtual environment.
        echo       Try reinstalling Python with "pip" included.
        pause
        exit /b 1
    )
    echo   [+] Created virtual environment
)

REM Activate
call "%VENV_DIR%\Scripts\activate.bat"
echo   [+] Virtual environment activated
echo.

REM -- 3. Install Python dependencies -----------------------------------------
echo [3/5] Installing Python dependencies...

echo   Upgrading pip...
python -m pip install --upgrade pip >nul 2>&1
if errorlevel 1 (
    echo   [x] pip upgrade failed
    pause
    exit /b 1
)
echo   [+] pip upgraded

echo   Installing core packages (this may take a minute)...
pip install -r "%SCRIPT_DIR%requirements.txt"
if errorlevel 1 (
    echo.
    echo   [x] pip install failed. Try:
    echo       pip install --pre -r requirements.txt
    pause
    exit /b 1
)
echo   [+] Core Python packages installed

if /i "!INSTALL_RAG!"=="true" (
    echo.
    echo   Installing RAG dependencies...
    if exist "%SCRIPT_DIR%requirements-rag.txt" (
        pip install -r "%SCRIPT_DIR%requirements-rag.txt"
        if errorlevel 1 (
            echo   [!] RAG install had issues. You can retry later:
            echo       pip install -r requirements-rag.txt
        ) else (
            echo   [+] RAG dependencies installed
        )
    ) else (
        echo   [!] requirements-rag.txt not found, skipping
    )
)
echo.

REM -- 4. Install npm dependencies --------------------------------------------
echo [4/5] Installing npm dependencies...

set "FRONTEND_DIR=%SCRIPT_DIR%web_ui\frontend"
if exist "%FRONTEND_DIR%\package.json" (
    pushd "%FRONTEND_DIR%"
    echo   Installing npm packages (this may take a while)...
    call npm install
    if errorlevel 1 (
        echo.
        echo   [x] npm install failed
        popd
        pause
        exit /b 1
    )
    echo   [+] npm packages installed
    popd
) else (
    echo   [!] Frontend directory not found, skipping
)
echo.

REM -- 5. Build frontend ------------------------------------------------------
echo [5/5] Building frontend...

if exist "%FRONTEND_DIR%\package.json" (
    pushd "%FRONTEND_DIR%"
    echo   Running npm run build...
    call npm run build
    if errorlevel 1 (
        echo.
        echo   [x] Frontend build failed
        popd
        pause
        exit /b 1
    )
    echo   [+] Frontend built successfully
    popd
) else (
    echo   [!] Skipping frontend build
)
echo.

REM -- Done -------------------------------------------------------------------
echo ============================================
echo   [+] Install complete!
echo.
echo   Next steps:
echo.
echo     1. Double-click: start_thoughtmachine.bat
echo.
echo     2. Open http://127.0.0.1:8000 in your browser
echo.
echo   Config is created automatically on first run at:
echo     %%USERPROFILE%%\.thoughtmachine\agent_config.json
echo ============================================
echo.

pause
