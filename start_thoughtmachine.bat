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

REM Check npm
where npm >nul 2>&1
if errorlevel 1 (
    echo [ERROR] npm not found.  Install Node.js from https://nodejs.org/
    pause
    exit /b 1
)
for /f "tokens=*" %%p in ('where npm') do set "TM_NPM_CMD=%%p"

REM ── Start backend in background (same window) ──────────────────────────────
echo   ^> Starting backend server ^(port 8000^)...
cd /d "%SCRIPT_DIR%"
start /b "" "%TM_PYTHON%" -m web_ui.backend.server

REM ── Wait for port 8000 (up to 15 s) ────────────────────────────────────────
echo   ^> Waiting for backend to be ready...
set BACKEND_READY=
for /l %%i in (1,1,15) do (
    timeout /t 1 /nobreak >nul
    netstat -an 2^>nul | findstr ":8000 " >nul 2>&1
    if not errorlevel 1 (
        set BACKEND_READY=1
        goto :dev_backend_ready
    )
)
:dev_backend_ready
if not defined BACKEND_READY (
    echo(
    echo  [WARNING] Backend may not have started.
    echo(
) else (
    echo   ^> Backend is ready on http://127.0.0.1:8000
)

REM ── Start Vite in a separate window ────────────────────────────────────────
echo   ^> Starting Vite dev server ^(port 5173^)...
start "Vite Dev Server" /d "%FRONTEND_DIR%" npm run dev

REM ── Cleanup ────────────────────────────────────────────────────────────────
echo(
echo   ^> Shutting down backend server...
taskkill /f /fi "IMAGENAME eq python.exe" 2>nul
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

where npm >nul 2>&1
if not errorlevel 1 (
    for /f "tokens=*" %%p in ('where npm') do set "TM_NPM_CMD=%%p"
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
