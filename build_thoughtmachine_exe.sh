#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# build_thoughtmachine_exe.sh
#
# Build a standalone ThoughtMachine executable using PyInstaller.
#
# Prerequisites
# ─────────────
#   1. Python 3.10+ with all project dependencies installed
#      (pip install -r requirements.txt)
#   2. Node.js 18+ and npm (to build the React frontend)
#   3. PyInstaller (pip install pyinstaller)
#   4. UPX (optional, for smaller binaries — install via package manager)
#
# Usage
# ─────
#   ./build_thoughtmachine_exe.sh          # one-folder mode (faster startup)
#   ONE_FILE=1 ./build_thoughtmachine_exe.sh  # single .exe (slower startup)
#
# Output
# ──────
#   dist/ThoughtMachine/          (one-folder mode — whole directory)
#   dist/ThoughtMachine.exe       (one-file mode — single file)
# ═══════════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Colour helpers ───────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

# ── Project root (where this script lives) ───────────────────────────────
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

# ── Step 1: Build the React frontend ────────────────────────────────────
info "Step 1/5: Building React frontend..."

FRONTEND_DIR="$PROJECT_ROOT/web_ui/frontend"
FRONTEND_DIST="$FRONTEND_DIR/dist"

if [ -d "$FRONTEND_DIST" ] && [ -f "$FRONTEND_DIST/index.html" ]; then
    info "  Frontend already built — skipping build (delete $FRONTEND_DIST to rebuild)"
else
    info "  Installing npm dependencies..."
    cd "$FRONTEND_DIR"
    npm install --no-audit --no-fund 2>&1 | tail -5
    info "  Running vite build..."
    npm run build 2>&1 | tail -10
    cd "$PROJECT_ROOT"
    if [ ! -f "$FRONTEND_DIST/index.html" ]; then
        err "Frontend build failed — index.html not found in $FRONTEND_DIST"
        exit 1
    fi
    ok "Frontend built successfully"
fi

# ── Step 2: Create a Python venv with all dependencies ─────────────────
info "Step 2/5: Checking Python environment..."

# Use the currently active venv if it exists, otherwise offer to create one
if [ -z "${VIRTUAL_ENV:-}" ]; then
    if [ -d ".venv" ]; then
        info "  Activating existing .venv..."
        source .venv/bin/activate 2>/dev/null || source .venv/Scripts/activate 2>/dev/null || true
    fi
fi

PYTHON="$(command -v python3 || command -v python || echo '')"
if [ -z "$PYTHON" ]; then
    err "Python not found. Please install Python 3.10+."
    exit 1
fi

info "  Using Python: $($PYTHON --version 2>&1)"

# ── Step 3: Install PyInstaller ─────────────────────────────────────────
info "Step 3/5: Installing PyInstaller..."
$PYTHON -m pip install --quiet pyinstaller 2>&1 | tail -3 || {
    warn "pip install failed; trying with --user..."
    $PYTHON -m pip install --quiet --user pyinstaller 2>&1 | tail -3
}

# Verify PyInstaller is available
if ! command -v pyinstaller &>/dev/null; then
    $PYTHON -m PyInstaller --version &>/dev/null || {
        err "PyInstaller not found after installation."
        exit 1
    }
    # Use module invocation
    PYINSTALLER="$PYTHON -m PyInstaller"
else
    PYINSTALLER="pyinstaller"
fi

info "  PyInstaller version: $($PYTHON -m PyInstaller --version 2>&1)"

# ── Step 4: Ensure all project dependencies are installed ───────────────
info "Step 4/5: Ensuring project dependencies..."
$PYTHON -m pip install --quiet -r requirements.txt 2>&1 | tail -3

# ── Step 5: Run PyInstaller ─────────────────────────────────────────────
info "Step 5/5: Running PyInstaller..."

# Clean previous builds
rm -rf build dist/*.spec

BUILD_MODE="${ONE_FILE:-0}"
if [ "$BUILD_MODE" = "1" ]; then
    info "  Build mode: ONE-FILE (single .exe/.bin)"
    $PYTHON -m PyInstaller \
        --clean \
        --noconfirm \
        --onefile \
        --name "ThoughtMachine" \
        --add-data "$FRONTEND_DIST:frontend_dist" \
        --add-data "$PROJECT_ROOT/resources:resources" \
        --add-data "$PROJECT_ROOT/docker/executor.Dockerfile:docker" \
        --add-data "$PROJECT_ROOT/docker/requirements-docker.txt:docker" \
        --hidden-import "tools.base" \
        --hidden-import "tools.file_editor" \
        --hidden-import "tools.file_preview_tool" \
        --hidden-import "tools.directory_tree_tool" \
        --hidden-import "tools.glob_tool" \
        --hidden-import "tools.file_search_tool" \
        --hidden-import "tools.apply_edits" \
        --hidden-import "tools.code_modifier" \
        --hidden-import "tools.code_modifier_utils" \
        --hidden-import "tools.refactor_tool" \
        --hidden-import "tools.search_codebase" \
        --hidden-import "tools.datetime_tool" \
        --hidden-import "tools.directory_creator" \
        --hidden-import "tools.docker_code_runner" \
        --hidden-import "tools.field_viewer" \
        --hidden-import "tools.file_mover" \
        --hidden-import "tools.file_summary_tool" \
        --hidden-import "tools.git_info_tool" \
        --hidden-import "tools.knowledge_base" \
        --hidden-import "tools.mcp_validator" \
        --hidden-import "tools.paginate_tool" \
        --hidden-import "tools.progress_report" \
        --hidden-import "tools.respond" \
        --hidden-import "tools.summarize_tool" \
        --hidden-import "tools.thought" \
        --hidden-import "tools.utils" \
        --hidden-import "tools.mcp_client" \
        --hidden-import "tools.mcp_client_new" \
        --hidden-import "tools.mcp_manager" \
        --hidden-import "agent.controller" \
        --hidden-import "agent.config" \
        --hidden-import "agent.config.loader" \
        --hidden-import "agent.config.models" \
        --hidden-import "agent.config.preset" \
        --hidden-import "agent.config.provider_profile" \
        --hidden-import "agent.config.service" \
        --hidden-import "agent.core.agent" \
        --hidden-import "agent.core.conversation_manager" \
        --hidden-import "agent.core.debug_context" \
        --hidden-import "agent.core.llm_client" \
        --hidden-import "agent.core.message" \
        --hidden-import "agent.core.message_utils" \
        --hidden-import "agent.core.state" \
        --hidden-import "agent.core.token_counter" \
        --hidden-import "agent.core.tool_executor" \
        --hidden-import "agent.core.turn_transaction" \
        --hidden-import "agent.cli" \
        --hidden-import "agent.cli.main" \
        --hidden-import "agent.cli.rag_commands" \
        --hidden-import "agent.knowledge.base" \
        --hidden-import "agent.knowledge.codebase_indexer" \
        --hidden-import "agent.knowledge.codebase_kb" \
        --hidden-import "agent.knowledge.dependencies" \
        --hidden-import "agent.knowledge.global_kb" \
        --hidden-import "agent.logging" \
        --hidden-import "agent.logging.unified" \
        --hidden-import "agent.logging.debug_log_adapter" \
        --hidden-import "agent.presenter" \
        --hidden-import "agent.presenter.agent_presenter" \
        --hidden-import "agent.presenter.event_processor" \
        --hidden-import "agent.presenter.gui_integration" \
        --hidden-import "agent.presenter.session_lifecycle" \
        --hidden-import "agent.presenter.state_bridge" \
        --hidden-import "session.models" \
        --hidden-import "session.store" \
        --hidden-import "session.context_builder" \
        --hidden-import "session.event_schema" \
        --hidden-import "session.history_provider" \
        --hidden-import "session.history_pruner" \
        --hidden-import "session.utils" \
        --hidden-import "llm_providers.base" \
        --hidden-import "llm_providers.factory" \
        --hidden-import "llm_providers.openai_compatible" \
        --hidden-import "llm_providers.anthropic_provider" \
        --hidden-import "llm_providers.exceptions" \
        --hidden-import "llm_providers.tool_converter" \
        --hidden-import "web_ui.backend.bridge" \
        --hidden-import "web_ui.backend.server" \
        --hidden-import "thoughtmachine.bootstrap" \
        --hidden-import "thoughtmachine.security" \
        --hidden-import "thoughtmachine.security_config" \
        --hidden-import "uvicorn.logging" \
        --hidden-import "uvicorn.loops.auto" \
        --hidden-import "uvicorn.loops.asyncio" \
        --hidden-import "uvicorn.protocols.http.auto" \
        --hidden-import "uvicorn.protocols.http.h11_impl" \
        --hidden-import "uvicorn.protocols.websockets.auto" \
        --hidden-import "uvicorn.protocols.websockets.websockets_impl" \
        --hidden-import "uvicorn.middleware.debug" \
        --hidden-import "uvicorn.middleware.proxy_headers" \
        --hidden-import "starlette.routing" \
        --hidden-import "starlette.middleware" \
        --hidden-import "pydantic" \
        --hidden-import "pydantic.dataclasses" \
        --hidden-import "tiktoken_ext.openai_public" \
        --hidden-import "tiktoken_ext" \
        --exclude-module "PyQt6" \
        --exclude-module "qt_gui" \
        --exclude-module "tkinter" \
        --exclude-module "test" \
        --exclude-module "unittest" \
        --exclude-module "distutils" \
        --exclude-module "ensurepip" \
        --exclude-module "lib2to3" \
        --exclude-module "numpy" \
        --exclude-module "matplotlib" \
        --exclude-module "pandas" \
        --exclude-module "IPython" \
        --exclude-module "notebook" \
        --exclude-module "cryptography" \
        thoughtmachine_entry.py
else
    info "  Build mode: ONE-FOLDER (fast startup, whole directory)"
    info "  Using spec file: thoughtmachine.spec"
    $PYTHON -m PyInstaller \
        --clean \
        --noconfirm \
        thoughtmachine.spec
fi

# ── Done ─────────────────────────────────────────────────────────────────
echo ""
if [ "$BUILD_MODE" = "1" ]; then
    if [ -f "dist/ThoughtMachine" ] || [ -f "dist/ThoughtMachine.exe" ]; then
        ok "Build complete! Single executable created:"
        ls -lh dist/ThoughtMachine* 2>/dev/null
    else
        warn "One-file executable not found — check dist/ for output."
    fi
else
    if [ -d "dist/ThoughtMachine" ]; then
        ok "Build complete! One-folder bundle created at:"
        du -sh "dist/ThoughtMachine"
        echo ""
        info "To run:"
        info "  ./dist/ThoughtMachine/ThoughtMachine"
        info ""
        info "On Windows, double-click dist/ThoughtMachine/ThoughtMachine.exe"
        info "or run from terminal: dist\\ThoughtMachine\\ThoughtMachine.exe"
    else
        warn "Output directory not found — check dist/ for output."
    fi
fi
