@echo off
REM==============================================================================
REM start_thoughtmachine.bat
REM
REM  ⚠ SYNCED with start_thoughtmachine.sh — keep in agreement.
REM  ⚠ If you edit the batch file, mirror the same change in the shell script.
REM==============================================================================
REM  Windows launcher for ThoughtMachine Web UI.
REM
REM  Usage:
REM    start_thoughtmachine.bat          Development mode (hot-reload via Vite)
REM    start_thoughtmachine.bat --prod   Production mode (serves from dist/)
REM==============================================================================
REM
REM  Kill leftover processes before starting
call "%~dp0kill_thoughtmachine.bat" 2>nul

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
    REM Check npm and capture its path so Python can use it
    where npm >nul 2>&1
    if not errorlevel 1 (
        for /f "tokens=*" %%p in ('where npm') do set "TM_NPM_CMD=%%p"
    )
    start "ThoughtMachine Backend" /wait python -m web_ui.backend.server --serve-frontend
    exit /b %ERRORLEVEL%
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
    REM Capture npm full path so Python can find it too
    for /f "tokens=*" %%p in ('where npm') do set "TM_NPM_CMD=%%p"

    REM Start backend FIRST so Vite's proxy never hits ECONNREFUSED
    echo   Starting backend server ^(port 8000^)...
    start "ThoughtMachine Backend" python -m web_ui.backend.server

    REM Wait for backend to start listening on port 8000 (up to 15 seconds)
    echo   Waiting for backend to be ready...
    set "BACKEND_READY="
    for /l %%i in (1,1,15) do (
        timeout /t 1 /nobreak >nul
        netstat -an 2^>nul | findstr ":8000 " >nul 2>&1
        if not errorlevel 1 (
            set "BACKEND_READY=1"
            goto :backend_ready
        )
    )
    :backend_ready
    if not defined BACKEND_READY (
        echo(
        echo [WARNING] Backend server may not have started.
        echo           Check the "ThoughtMachine Backend" window for errors.
        echo(
    ) else (
        echo   Backend is ready on http://127.0.0.1:8000
    )

    REM Start Vite dev server in foreground (blocks until Vite exits)
    pushd "!FRONTEND_DIR!"
    start "ThoughtMachine Vite" /wait cmd /c "npm run dev"
    popd

    REM When Vite stops (Ctrl+C), also stop backend
    echo(
    echo   Shutting down backend server...
    taskkill /f /fi "WINDOWTITLE eq ThoughtMachine Backend*" >nul 2>&1
)
exit /b %ERRORLEVEL%

goto :eof

:show_help
echo Usage: %~nx0 [--prod]
echo(
echo   --prod    Production mode (fresh build from source, then serve)
echo            Default is development mode with hot-reload.
echo   --help    Show this help
pause
exit /b 0
