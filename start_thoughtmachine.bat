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
    REM -- Production mode (auto-build fresh, then serve) --------------------
    echo   Mode:    PRODUCTION (fresh build from source)
    echo   Server:  http://127.0.0.1:8000
    echo   Stop:    Ctrl+C
    echo(
    python -m web_ui.backend.server --serve-frontend
) else (
    REM -- Development mode (hot-reload via Vite) -----------------------------
    echo   Mode:    DEVELOPMENT (hot-reload enabled)
    echo(
    echo   Frontend: http://127.0.0.1:5173  ^<-^< USE THIS URL
    echo   Backend:  http://127.0.0.1:8000   ^(API only, not the app^)
    echo   Stop:    Ctrl+C
    echo(

    set "FRONTEND_DIR=%SCRIPT_DIR%web_ui\frontend"

    REM Verify npm is available before starting Vite
    where npm >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] npm not found. Make sure Node.js is installed and on your PATH.
        echo        Download from: https://nodejs.org/
        pause
        exit /b 1
    )

    REM Start Vite dev server in a new window
    echo   Starting Vite dev server ^(port 5173^)...
    pushd "!FRONTEND_DIR!"
    start "ThoughtMachine Vite" cmd /c "npm run dev"
    popd

    REM Wait for Vite to start listening on port 5173 (up to 15 seconds)
    echo   Waiting for Vite to be ready...
    set "VITE_READY="
    for /l %%i in (1,1,15) do (
        timeout /t 1 /nobreak >nul
        netstat -an 2^>nul | findstr ":5173 " >nul 2>&1
        if not errorlevel 1 (
            set "VITE_READY=1"
            goto :vite_ready
        )
    )
    :vite_ready
    if not defined VITE_READY (
        echo(
        echo [WARNING] Vite dev server may not have started.
        echo           Check the "ThoughtMachine Vite" window for errors.
        echo           Try browsing to http://127.0.0.1:5173 manually.
        echo(
    ) else (
        echo   Vite is ready on http://127.0.0.1:5173
    )

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
echo   --prod    Production mode (fresh build from source, then serve)
echo            Default is development mode with hot-reload.
echo   --help    Show this help
pause
exit /b 0
