@echo off
:: ═══════════════════════════════════════════════════════════════════════════
:: build_thoughtmachine_exe.bat
::
:: Build a standalone ThoughtMachine executable using PyInstaller (Windows).
::
:: Prerequisites
:: ─────────────
::   1. Python 3.10+ with all project dependencies installed
::      (pip install -r requirements.txt)
::   2. Node.js 18+ and npm (to build the React frontend)
::   3. PyInstaller (pip install pyinstaller)
::   4. UPX (optional, for smaller binaries — download from upx.github.io)
::
:: Usage
:: ─────
::   build_thoughtmachine_exe.bat          :: one-folder mode (faster startup)
::   set ONE_FILE=1 & build_thoughtmachine_exe.bat  :: single .exe (slower)
::
:: Output
:: ──────
::   dist\ThoughtMachine\          (one-folder mode — whole directory)
::   dist\ThoughtMachine.exe       (one-file mode — single file)
:: ═══════════════════════════════════════════════════════════════════════════

setlocal enabledelayedexpansion

:: ── Project root (where this script lives) ───────────────────────────────
set "PROJECT_ROOT=%~dp0"
:: Remove trailing backslash
if "%PROJECT_ROOT:~-1%"=="\" set "PROJECT_ROOT=%PROJECT_ROOT:~0,-1%"
cd /d "%PROJECT_ROOT%"

:: ── Step 1: Build the React frontend ────────────────────────────────────
call :info "Step 1/5: Building React frontend..."

set "FRONTEND_DIR=%PROJECT_ROOT%\web_ui\frontend"
set "FRONTEND_DIST=%FRONTEND_DIR%\dist"

if exist "%FRONTEND_DIST%\index.html" (
    call :info "  Frontend already built — skipping build (delete %FRONTEND_DIST% to rebuild)"
) else (
    call :info "  Installing npm dependencies..."
    cd /d "%FRONTEND_DIR%"
    call npm install --no-audit --no-fund 2>&1
    if !ERRORLEVEL! NEQ 0 (
        call :err "npm install failed"
        exit /b 1
    )
    call :info "  Running vite build..."
    call npm run build 2>&1
    if !ERRORLEVEL! NEQ 0 (
        call :err "npm run build failed"
        exit /b 1
    )
    cd /d "%PROJECT_ROOT%"
    if not exist "%FRONTEND_DIST%\index.html" (
        call :err "Frontend build failed — index.html not found in %FRONTEND_DIST%"
        exit /b 1
    )
    call :ok "Frontend built successfully"
)

:: ── Step 2: Check Python environment ────────────────────────────────────
call :info "Step 2/5: Checking Python environment..."

:: Check if we're in an active venv
if "%VIRTUAL_ENV%"=="" (
    if exist ".venv\Scripts\activate.bat" (
        call :info "  Activating existing .venv..."
        call ".venv\Scripts\activate.bat"
    ) else if exist ".venv\Scripts\activate" (
        call :info "  Activating existing .venv..."
        call ".venv\Scripts\activate"
    )
)

:: Find Python
set "PYTHON="
for %%p in (python3.exe python.exe) do (
    where %%p >nul 2>&1 && set "PYTHON=%%p" && goto :found_python
)
:found_python
if "%PYTHON%"=="" (
    call :err "Python not found. Please install Python 3.10+ and ensure it is on your PATH."
    exit /b 1
)

call :info "  Using Python:"
%PYTHON% --version

:: ── Step 3: Install PyInstaller ─────────────────────────────────────────
call :info "Step 3/5: Installing PyInstaller..."
%PYTHON% -m pip install --quiet pyinstaller 2>&1
if !ERRORLEVEL! NEQ 0 (
    call :warn "pip install failed; trying with --user..."
    %PYTHON% -m pip install --quiet --user pyinstaller 2>&1
    if !ERRORLEVEL! NEQ 0 (
        call :err "PyInstaller installation failed. Please install manually: pip install pyinstaller"
        exit /b 1
    )
)

:: Verify PyInstaller is available
%PYTHON% -m PyInstaller --version >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
    call :err "PyInstaller not found after installation."
    exit /b 1
)

for /f "delims=" %%v in ('%PYTHON% -m PyInstaller --version 2^>^&1') do set "PYINSTALLER_VER=%%v"
call :info "  PyInstaller version: !PYINSTALLER_VER!"

:: ── Step 4: Ensure all project dependencies are installed ───────────────
call :info "Step 4/5: Ensuring project dependencies..."
%PYTHON% -m pip install --quiet -r requirements.txt 2>&1

:: ── Step 5: Run PyInstaller ─────────────────────────────────────────────
call :info "Step 5/5: Running PyInstaller..."

:: Clean previous builds
if exist "build" rmdir /s /q "build" 2>nul
if exist "dist\*.spec" del /f /q "dist\*.spec" 2>nul

if "%ONE_FILE%"=="1" (
    call :info "  Build mode: ONE-FILE (single .exe)"
    %PYTHON% -m PyInstaller ^
        --clean ^
        --noconfirm ^
        --onefile ^
        --name "ThoughtMachine" ^
        --add-data "%FRONTEND_DIST%;frontend_dist" ^
        --add-data "%PROJECT_ROOT%\resources;resources" ^
        --add-data "%PROJECT_ROOT%\docker\executor.Dockerfile;docker" ^
        --add-data "%PROJECT_ROOT%\docker\requirements-docker.txt;docker" ^
        --hidden-import "tools.base" ^
        --hidden-import "tools.file_editor" ^
        --hidden-import "tools.file_preview_tool" ^
        --hidden-import "tools.directory_tree_tool" ^
        --hidden-import "tools.glob_tool" ^
        --hidden-import "tools.file_search_tool" ^
        --hidden-import "tools.apply_edits" ^
        --hidden-import "tools.code_modifier" ^
        --hidden-import "tools.code_modifier_utils" ^
        --hidden-import "tools.refactor_tool" ^
        --hidden-import "tools.search_codebase" ^
        --hidden-import "tools.datetime_tool" ^
        --hidden-import "tools.directory_creator" ^
        --hidden-import "tools.docker_code_runner" ^
        --hidden-import "tools.field_viewer" ^
        --hidden-import "tools.file_mover" ^
        --hidden-import "tools.file_summary_tool" ^
        --hidden-import "tools.git_info_tool" ^
        --hidden-import "tools.knowledge_base" ^
        --hidden-import "tools.mcp_validator" ^
        --hidden-import "tools.paginate_tool" ^
        --hidden-import "tools.progress_report" ^
        --hidden-import "tools.respond" ^
        --hidden-import "tools.summarize_tool" ^
        --hidden-import "tools.thought" ^
        --hidden-import "tools.utils" ^
        --hidden-import "tools.mcp_client" ^
        --hidden-import "tools.mcp_client_new" ^
        --hidden-import "tools.mcp_manager" ^
        --hidden-import "agent.controller" ^
        --hidden-import "agent.config" ^
        --hidden-import "agent.config.loader" ^
        --hidden-import "agent.config.models" ^
        --hidden-import "agent.config.preset" ^
        --hidden-import "agent.config.provider_profile" ^
        --hidden-import "agent.config.service" ^
        --hidden-import "agent.core.agent" ^
        --hidden-import "agent.core.conversation_manager" ^
        --hidden-import "agent.core.debug_context" ^
        --hidden-import "agent.core.llm_client" ^
        --hidden-import "agent.core.message" ^
        --hidden-import "agent.core.message_utils" ^
        --hidden-import "agent.core.state" ^
        --hidden-import "agent.core.token_counter" ^
        --hidden-import "agent.core.tool_executor" ^
        --hidden-import "agent.core.turn_transaction" ^
        --hidden-import "agent.cli" ^
        --hidden-import "agent.cli.main" ^
        --hidden-import "agent.cli.rag_commands" ^
        --hidden-import "agent.knowledge.base" ^
        --hidden-import "agent.knowledge.codebase_indexer" ^
        --hidden-import "agent.knowledge.codebase_kb" ^
        --hidden-import "agent.knowledge.dependencies" ^
        --hidden-import "agent.knowledge.global_kb" ^
        --hidden-import "agent.logging" ^
        --hidden-import "agent.logging.unified" ^
        --hidden-import "agent.logging.debug_log_adapter" ^
        --hidden-import "agent.presenter" ^
        --hidden-import "agent.presenter.agent_presenter" ^
        --hidden-import "agent.presenter.event_processor" ^
        --hidden-import "agent.presenter.gui_integration" ^
        --hidden-import "agent.presenter.session_lifecycle" ^
        --hidden-import "agent.presenter.state_bridge" ^
        --hidden-import "session.models" ^
        --hidden-import "session.store" ^
        --hidden-import "session.context_builder" ^
        --hidden-import "session.event_schema" ^
        --hidden-import "session.history_provider" ^
        --hidden-import "session.history_pruner" ^
        --hidden-import "session.utils" ^
        --hidden-import "llm_providers.base" ^
        --hidden-import "llm_providers.factory" ^
        --hidden-import "llm_providers.openai_compatible" ^
        --hidden-import "llm_providers.anthropic_provider" ^
        --hidden-import "llm_providers.exceptions" ^
        --hidden-import "llm_providers.tool_converter" ^
        --hidden-import "web_ui.backend.bridge" ^
        --hidden-import "web_ui.backend.server" ^
        --hidden-import "thoughtmachine.bootstrap" ^
        --hidden-import "thoughtmachine.security" ^
        --hidden-import "thoughtmachine.security_config" ^
        --hidden-import "uvicorn.logging" ^
        --hidden-import "uvicorn.loops.auto" ^
        --hidden-import "uvicorn.loops.asyncio" ^
        --hidden-import "uvicorn.protocols.http.auto" ^
        --hidden-import "uvicorn.protocols.http.h11_impl" ^
        --hidden-import "uvicorn.protocols.websockets.auto" ^
        --hidden-import "uvicorn.protocols.websockets.websockets_impl" ^
        --hidden-import "uvicorn.middleware.debug" ^
        --hidden-import "uvicorn.middleware.proxy_headers" ^
        --hidden-import "starlette.routing" ^
        --hidden-import "starlette.middleware" ^
        --hidden-import "pydantic" ^
        --hidden-import "pydantic.dataclasses" ^
        --hidden-import "tiktoken_ext.openai_public" ^
        --hidden-import "tiktoken_ext" ^
        --exclude-module "PyQt6" ^
        --exclude-module "qt_gui" ^
        --exclude-module "tkinter" ^
        --exclude-module "test" ^
        --exclude-module "unittest" ^
        --exclude-module "distutils" ^
        --exclude-module "ensurepip" ^
        --exclude-module "lib2to3" ^
        --exclude-module "numpy" ^
        --exclude-module "matplotlib" ^
        --exclude-module "pandas" ^
        --exclude-module "IPython" ^
        --exclude-module "notebook" ^
        --exclude-module "cryptography" ^
        thoughtmachine_entry.py
) else (
    call :info "  Build mode: ONE-FOLDER (fast startup, whole directory)"
    call :info "  Using spec file: thoughtmachine.spec"
    %PYTHON% -m PyInstaller ^
        --clean ^
        --noconfirm ^
        thoughtmachine.spec
)

:: ── Done ─────────────────────────────────────────────────────────────────
echo(
if "%ONE_FILE%"=="1" (
    if exist "dist\ThoughtMachine.exe" (
        call :ok "Build complete! Single executable created:"
        for %%f in ("dist\ThoughtMachine.exe") do echo     %%~zf bytes
    ) else (
        call :warn "One-file executable not found — check dist\ for output."
    )
) else (
    if exist "dist\ThoughtMachine" (
        call :ok "Build complete! One-folder bundle created at:"
        echo     dist\ThoughtMachine\
echo(
        call :info "To run:"
        call :info "  dist\ThoughtMachine\ThoughtMachine.exe"
    ) else (
        call :warn "Output directory not found — check dist\ for output."
    )
)

goto :eof

:: ═══════════════════════════════════════════════════════════════════════════
:: Helper functions
:: ═══════════════════════════════════════════════════════════════════════════

:info
    echo [INFO]  %*
    goto :eof

:ok
    echo [OK]    %*
    goto :eof

:warn
    echo [WARN]  %*
    goto :eof

:err
    echo [ERROR] %*
    goto :eof
