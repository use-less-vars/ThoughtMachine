@echo off
REM ---------------------------------------------------------------------------
REM start_thoughtmachine.bat
REM  Windows launcher for ThoughtMachine Web UI.
REM
REM Usage:
REM   start_thoughtmachine.bat          Development mode (hot-reload via Vite)
REM   start_thoughtmachine.bat --prod   Production mode (serves from dist/)
REM ---------------------------------------------------------------------------

setlocal enabledelayedexpansion

REM -- Parse flags -------------------------------------------------------------
set PROD_MODE=false
if /i "%~1"=="--prod" set PROD_MODE=true
if /i "%~1"=="-p" set PROD_MODE=true
if /i "%~1"=="--help" goto :show_help
if /i "%~1"=="-h" goto :show_help

set "SCRIPT_DIR=%~dp0"
set "VENV_DIR=%SCRIPT_DIR%.venv"

REM Check venv exists
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo [x] Virtual environment not found at %VENV_DIR%
    echo(
    echo   Run the install script first:
    echo     install_thoughtmachine.bat
    pause
    exit /b 1
)

REM Activate venv
call "%VENV_DIR%\Scripts\activate.bat"

echo ============================================
echo   ThoughtMachine - Starting Web UI
echo ============================================
echo(

if "%PROD_MODE%"=="true" (
    REM -- Production mode (serve from dist/) ---------------------------------
    echo   Mode:    PRODUCTION (serving pre-built dist/ files)
    echo   Server:  http://127.0.0.1:8000
    echo   Stop:    Ctrl+C
    echo   Note:    Rebuild frontend after changes:
    echo            cd web_ui\frontend ^&^& npm run build
    echo(
    python -m web_ui.backend.server --serve-frontend
) else (
    REM -- Development mode (hot-reload via Vite) -----------------------------
    echo   Mode:    DEVELOPMENT (hot-reload enabled)
    echo(
    echo   Frontend: http://127.0.0.1:5173
    echo   Backend:  http://127.0.0.1:8000
    echo   Stop:    Ctrl+C
    echo(

    set "FRONTEND_DIR=%SCRIPT_DIR%web_ui\frontend"

    REM Start Vite dev server in a new window
    echo   Starting Vite dev server ^(port 5173^)...
    pushd "!FRONTEND_DIR!"
    start "ThoughtMachine Vite" cmd /c "npm run dev"
    popd

    REM Small pause to let Vite start before backend starts
    timeout /t 2 /nobreak >nul

    REM Start backend (CORS already allows Vite dev server on any port)
    python -m web_ui.backend.server

    REM When backend stops, also stop Vite
    echo(
    echo   Shutting down Vite dev server...
    taskkill /f /fi "WINDOWTITLE eq ThoughtMachine Vite" >nul 2>&1
)

goto :eof

:show_help
echo Usage: %~nx0 [--prod]
echo(
echo   --prod    Production mode (serves pre-built dist/ files)
echo            Default is development mode with hot-reload.
echo   --help    Show this help
pause
exit /b 0
