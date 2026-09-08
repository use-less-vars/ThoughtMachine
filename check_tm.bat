@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ================================================
echo ThoughtMachine Diagnostic
echo Date: %date% %time%
echo ================================================
echo.

:: 1. Python check
echo [1] Checking Python...
python --version 2>&1 || python3 --version 2>&1 || py --version 2>&1
echo.

:: 2. Node check
echo [2] Checking Node.js and npm...
node --version 2>&1
npm --version 2>&1
echo.

:: 3. Repo structure
echo [3] Current directory: %cd%
echo Script directory: %~dp0
echo Checking for .venv...
if exist ".venv\Scripts\python.exe" (echo .venv found) else (echo .venv MISSING)
echo Checking for requirements.txt...
if exist "requirements.txt" (echo requirements.txt found) else (echo requirements.txt MISSING)
echo Checking for web_ui\frontend\package.json...
if exist "web_ui\frontend\package.json" (echo package.json found) else (echo package.json MISSING)
echo Checking for web_ui\frontend\dist\index.html...
if exist "web_ui\frontend\dist\index.html" (echo dist/ found) else (echo dist/ MISSING - frontend not built)
echo.

:: 4. Venv health
if exist ".venv\Scripts\python.exe" (
    echo [4] Venv Python health...
    .venv\Scripts\python.exe -c "import fastapi; print('fastapi version:', fastapi.__version__)" 2>&1
    .venv\Scripts\python.exe -c "import uvicorn; print('uvicorn version:', uvicorn.__version__)" 2>&1
) else (
    echo [4] Skipping venv checks - .venv not found
)
echo.

:: 5. If dist missing, offer to build
if not exist "web_ui\frontend\dist\index.html" (
    echo [5] Frontend not built. Attempting build...
    if exist "web_ui\frontend\node_modules" (
        cd web_ui\frontend
        call npm run build 2>&1
        cd ..\..
        if exist "web_ui\frontend\dist\index.html" (echo Build successful) else (echo Build FAILED)
    ) else (
        echo node_modules missing - run install_thoughtmachine.bat first
    )
) else (
    echo [5] Frontend dist/ already present.
)
echo.

:: 6. Try production mode start
echo [6] Starting ThoughtMachine in PRODUCTION mode...
echo Command: .venv\Scripts\python.exe -m web_ui.backend.server --serve-frontend
echo Output will appear below. Press Ctrl+C when done.
echo.
.venv\Scripts\python.exe -m web_ui.backend.server --serve-frontend 2>&1

pause
