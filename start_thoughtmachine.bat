@echo off
REM==============================================================================
REM start_thoughtmachine.bat
REM
REM  ⚠ SYNCED with start_thoughtmachine.sh — keep in agreement.
REM  ⚠ If you edit this file, mirror the same change in the shell script.
REM==============================================================================
REM Single-window launcher for ThoughtMachine Web UI  (like the Linux script).
REM
REM  Pre-requisite: run install_thoughtmachine.bat first.
REM
REM  Usage:
REM    start_thoughtmachine.bat          Development mode (hot-reload via Vite)
REM    start_thoughtmachine.bat --prod   Production mode (serves from dist/)
REM==============================================================================

setlocal enabledelayedexpansion

REM Kill leftover processes before starting
call "%~dp0kill_thoughtmachine.bat" 2>nul

set "SCRIPT_DIR=%~dp0"
set "VENV_DIR=%SCRIPT_DIR%.venv"
set "TM_PYTHON=%VENV_DIR%\Scripts\python.exe"

REM -- Parse flags -------------------------------------------------------------
set PROD_MODE=false
if /i "%~1"=="--prod" set PROD_MODE=true
if /i "%~1"=="-p" set PROD_MODE=true
if /i "%~1"=="--help" goto :show_help
if /i "%~1"=="-h" goto :show_help

REM -- Check venv ---------------------------------------------------------------
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo [x] Virtual environment not found at %VENV_DIR%
    echo(
    echo   Run the install script first:
    echo     install_thoughtmachine.bat
    pause
    exit /b 1
)

REM Activate venv — also sets VIRTUAL_ENV, PROMPT etc.
call "%VENV_DIR%\Scripts\activate.bat"

echo ============================================
echo   ThoughtMachine — Starting Web UI
echo ============================================
echo(

REM ── Diagnostic: verify Python & fastapi ────────────────────────────
echo   Python:    "%TM_PYTHON%"
"%TM_PYTHON%" -c "import fastapi; print('  fastapi OK: v' + fastapi.__version__)" 2>&1
if errorlevel 1 (
    echo   [!!] fastapi NOT found in this venv!
    echo   Run:  "%TM_PYTHON%" -m pip install -r "%SCRIPT_DIR%requirements.txt"
    pause
    exit /b 1
)
echo(

if "%PROD_MODE%"=="true" goto :prod_mode

REM ═══════════════════════════════════════════════════════════════════════════
REM  Development mode (hot-reload via Vite)
REM ═══════════════════════════════════════════════════════════════════════════

echo   Mode:    DEVELOPMENT (hot-reload enabled)
echo(
echo   Frontend: http://127.0.0.1:5173  ^<-^< USE THIS URL
echo   Backend:  http://127.0.0.1:8000   ^(API only, not the app^)
echo   Stop:    Ctrl+C
echo(

set "FRONTEND_DIR=%SCRIPT_DIR%web_ui\frontend"

REM ── Pre-flight checks for Vite ────────────────────────────────────────
if not exist "%FRONTEND_DIR%\" (
    echo [ERROR] Frontend directory not found at:
    echo   %FRONTEND_DIR%
    echo(
    echo   Make sure you ran install_thoughtmachine.bat first.
    echo   If the directory exists elsewhere, edit FRONTEND_DIR in this script.
    pause
    exit /b 1
)
if not exist "%FRONTEND_DIR%\package.json" (
    echo [ERROR] package.json not found in frontend directory:
    echo   %FRONTEND_DIR%
    echo(
    echo   The installation appears incomplete. Run install_thoughtmachine.bat again.
    pause
    exit /b 1
)
if not exist "%FRONTEND_DIR%\node_modules\" (
    echo [WARNING] node_modules not found — frontend dependencies not installed.
    echo   Running npm install for you...
    echo(
    pushd "%FRONTEND_DIR%"
    call npm install
    if errorlevel 1 (
        popd
        echo(
        echo [FAIL] npm install failed. Try running manually:
        echo   cd /d "%FRONTEND_DIR%"
        echo   npm install
        pause
        exit /b 1
    )
    popd
    echo   [+] npm packages installed
    echo(
)

REM ── Verify Vite binary exists ──────────────────────────────────────────
if not exist "%FRONTEND_DIR%\node_modules\.bin\vite.cmd" (
    if not exist "%FRONTEND_DIR%\node_modules\vite\bin\vite.js" (
        echo [ERROR] Vite binary not found in node_modules.
        echo   The npm install may have failed or was interrupted.
        echo   Try:  cd /d "%FRONTEND_DIR%" ^&^& npm install
        pause
        exit /b 1
    )
)

REM ── Start Vite FIRST in its own window ─────────────────────────────────────
echo   ^> Starting Vite dev server ^(port 5173^) in its own window...
start "Vite Dev Server" /d "%FRONTEND_DIR%" cmd /k ""%FRONTEND_DIR%\node_modules\.bin\vite.cmd" --host 127.0.0.1"

REM ── Wait for port 5173 (up to 15 s) ────────────────────────────────────────
echo   ^> Waiting for Vite to start...
set VITE_READY=
for /l %%i in (1,1,15) do (
    REM Use PowerShell to check TCP connection state (works on Win8+)
    powershell -Command "Get-NetTCPConnection -LocalPort 5173 -ErrorAction SilentlyContinue | Where-Object { $_.State -eq 'Listen' }" >nul 2>&1
    if not errorlevel 1 (
        set VITE_READY=1
        goto :vite_ready
    )
    REM 1-second delay if not ready yet
    ping -n 2 127.0.0.1 >nul 2>&1
)
:vite_ready
if defined VITE_READY (
    echo   ^> Vite is ready on http://127.0.0.1:5173
) else (
    echo(
    echo  [WARNING] Vite may not have started in time.
    echo   Check the "Vite Dev Server" window for errors.
    echo   If it closed, try manually:
    echo     cd /d "%FRONTEND_DIR%"
    echo     node_modules\.bin\vite.cmd
    echo(
)

REM ── Start backend in foreground (blocks until Ctrl+C) ─────────────────────
echo   ^> Starting backend server ^(port 8000^)...
echo   ^> Press Ctrl+C to stop all servers.
echo(
cd /d "%SCRIPT_DIR%"
"%TM_PYTHON%" -m web_ui.backend.server

REM ── Cleanup runs after user presses Ctrl+C ────────────────────────────────
echo(
echo   ^> Shutting down all servers...
call "%~dp0kill_thoughtmachine.bat" 2>nul
pause
exit /b %ERRORLEVEL%

REM ═══════════════════════════════════════════════════════════════════════════
REM  Production mode (auto-build fresh, then serve)
REM ═══════════════════════════════════════════════════════════════════════════
:prod_mode

echo   Mode:    PRODUCTION (fresh build from source)
echo   Server:  http://127.0.0.1:8000
echo   Stop:    Ctrl+C
echo(

REM ── Find npm (needed for frontend build) ────────────────────────────────
set "FRONTEND_DIR=%SCRIPT_DIR%web_ui\frontend"
where npm >nul 2>&1
if not errorlevel 1 (
    for /f "tokens=*" %%p in ('where npm') do set "TM_NPM_CMD=%%p"
    echo   npm:    !TM_NPM_CMD!
) else (
    echo   [x] npm not found in PATH
    echo(
    echo   Production mode requires npm to build the frontend.
    echo   Make sure Node.js is installed, then restart.
    echo   If you just installed Node.js, start a new terminal.
    echo(
    pause
    exit /b 1
)

REM ── Verify frontend build prerequisites ─────────────────────────────────
if not exist "%FRONTEND_DIR%\package.json" (
    echo   [x] Frontend source not found at %FRONTEND_DIR%
    echo   Run install_thoughtmachine.bat first.
    pause
    exit /b 1
)
if not exist "%FRONTEND_DIR%\node_modules\" (
    echo   [WARNING] node_modules not found — running npm install...
    pushd "%FRONTEND_DIR%"
    call npm install
    if errorlevel 1 (
        popd
        echo   [FAIL] npm install failed.
        pause
        exit /b 1
    )
    popd
    echo   [+] npm packages installed
    echo(
)

cd /d "%SCRIPT_DIR%"
"%TM_PYTHON%" -m web_ui.backend.server --serve-frontend
pause
exit /b %ERRORLEVEL%

goto :eof

REM ═══════════════════════════════════════════════════════════════════════════
REM  Help
REM ═══════════════════════════════════════════════════════════════════════════
:show_help
echo Usage: %~nx0 [--prod]
echo(
echo   --prod    Production mode (fresh build from source, then serve)
echo            Default is development mode with hot-reload.
echo   --help    Show this help
pause
exit /b 0
