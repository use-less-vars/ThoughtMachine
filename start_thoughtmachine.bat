@echo off
REM ---------------------------------------------------------------------------
REM start_thoughtmachine.bat
REM  Windows launcher for ThoughtMachine Web UI.
REM ---------------------------------------------------------------------------

echo ============================================
echo   ThoughtMachine - Starting Web UI
echo ============================================
echo.
echo   Server:  http://127.0.0.1:8000
echo   Stop:    Ctrl+C
echo.

set "SCRIPT_DIR=%~dp0"
set "VENV_DIR=%SCRIPT_DIR%.venv"

REM Check venv exists
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo [x] Virtual environment not found at %VENV_DIR%
    echo.
    echo   Run the install script first:
    echo     install_thoughtmachine.bat
    pause
    exit /b 1
)

REM Activate venv and start server
call "%VENV_DIR%\Scripts\activate.bat"
python -m web_ui.backend.server --serve-frontend

if errorlevel 1 (
    echo.
    echo [x] Server exited with an error.
    pause
)
