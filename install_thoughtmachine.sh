#!/usr/bin/env bash
# No -e: we want to continue and report errors ourselves
set -o pipefail

# ──────────────────────────────────────────────────────────────────────────────
# install_thoughtmachine.sh
# Full install script for ThoughtMachine.
# Creates venv, installs Python deps, installs npm deps & builds frontend.
# ──────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"

echo "============================================"
echo "  ThoughtMachine — Install"
echo "============================================"
echo ""

# ── Parse flags ────────────────────────────────────────────────────────────
INSTALL_RAG=false
for arg in "$@"; do
    case "$arg" in
        --with-rag)
            INSTALL_RAG=true
            echo "  → RAG support requested (codebase search, embeddings)"
            ;;
        --help|-h)
            echo "  Usage: $0 [--with-rag]"
            echo ""
            echo "    --with-rag    Also install RAG dependencies (sentence-transformers,"
            echo "                  ChromaDB, CPU-only PyTorch ~500 MB)"
            echo "    --help, -h    Show this help"
            echo ""
            exit 0
            ;;
        *)
            echo "  ✗ Unknown argument: $arg"
            echo "    Usage: $0 [--with-rag]"
            exit 1
            ;;
    esac
done

# ── Detect OS ────────────────────────────────────────────────────────────────
IS_WINDOWS=false
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
    IS_WINDOWS=true
fi

# ── 0. Auto-install prerequisites (Windows) ────────────────────────────────────
if $IS_WINDOWS; then
    echo ""
    echo "[0/5] Windows detected — checking prerequisites..."
    echo ""

    # Python
    PYTHON=""
    for cmd in python3.12 python3.11 python3 python; do
        if command -v "$cmd" &>/dev/null; then
            PYTHON="$cmd"
            break
        fi
    done

    if [[ -z "$PYTHON" ]]; then
        echo "  → Python not found. Installing via winget..."
        echo "    (Pin: Python 3.12 — 3.14+ lacks package wheels)"
        winget install --silent --accept-package-agreements Python.Python.3.12 2>&1
        # Re-check after install
        for cmd in python3.12 python3 python; do
            if command -v "$cmd" &>/dev/null; then
                version=$("$cmd" --version 2>&1)
                major=$(echo "$version" | awk '{print $2}' | cut -d. -f1)
                minor=$(echo "$version" | awk '{print $2}' | cut -d. -f2)
                if [[ -n "$major" && -n "$minor" && "$major" -eq 3 && "$minor" -ge 11 ]]; then
                    PYTHON="$cmd"
                fi
                break
            fi
        done
        if [[ -z "$PYTHON" ]]; then
            echo "  → Trying Python 3.11..."
            winget install --silent --accept-package-agreements Python.Python.3.11 2>&1
            for cmd in python3.11 python3 python; do
                if command -v "$cmd" &>/dev/null; then
                    version=$("$cmd" --version 2>&1)
                    minor=$(echo "$version" | awk '{print $2}' | cut -d. -f2)
                    if [[ -n "$minor" && "$minor" -ge 11 ]]; then
                        PYTHON="$cmd"
                    fi
                    break
                fi
            done
        fi
        if [[ -z "$PYTHON" ]]; then
            echo "  ✗ Could not auto-install Python via winget."
            echo "    Install Python 3.11+ manually from https://www.python.org/downloads/"
            echo "    Then re-run this script."
            exit 1
        fi
        echo "  ✓ Python installed: $($PYTHON --version 2>&1)"
        # Refresh PATH so winget-installed python is found
        export PATH="$PATH:/c/Program Files/Python312:/c/Program Files/Python312/Scripts"
        export PATH="$PATH:/c/Program Files/Python311:/c/Program Files/Python311/Scripts"
    else
        echo "  ✓ Python found"
    fi

    # Node.js
    if ! command -v node &>/dev/null; then
        echo "  → Node.js not found. Installing via winget..."
        winget install --silent --accept-package-agreements OpenJS.NodeJS.LTS 2>&1
        # Refresh PATH
        export PATH="$PATH:/c/Program Files/nodejs"
        if ! command -v node &>/dev/null; then
            echo "  ✗ Could not auto-install Node.js via winget."
            echo "    Install Node.js LTS manually from https://nodejs.org/"
            echo "    Then re-run this script."
            exit 1
        fi
        echo "  ✓ Node.js installed: $(node --version 2>&1)"
        echo "  ✓ npm installed: $(npm --version 2>&1)"
    else
        echo "  ✓ Node.js found"
    fi

    echo ""
    echo "  → Prerequisites ready, continuing with full install..."
fi

# ── 1. Check prerequisites ────────────────────────────────────────────────────
echo ""
echo "[1/5] Checking prerequisites..."

PYTHON=""
for cmd in python3.12 python3.11 python3 python; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" --version 2>&1)
        # Parse major.minor — works with "Python 3.12.0" or "Python 3.11"
        major=$(echo "$version" | awk '{print $2}' | cut -d. -f1)
        minor=$(echo "$version" | awk '{print $2}' | cut -d. -f2)
        # Accept 3.11+. Warn about very new versions (3.14+) but let them try.
        if [[ -n "$major" && -n "$minor" && "$major" -eq 3 && "$minor" -ge 11 ]]; then
            PYTHON="$cmd"
            echo "  ✓ Found $PYTHON ($version)"
            if [[ "$minor" -ge 14 ]]; then
                echo "  ⚠  Python $major.$minor is very new — some packages may lack wheels."
                echo "     If pip install fails, try Python 3.11 or 3.12."
            fi
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo "  ✗ Python >=3.11 not found."
    echo "    Detected version: $($cmd --version 2>&1 2>/dev/null || echo 'none')"
    echo "    Install Python 3.11+ from https://www.python.org/downloads/"
    exit 1
fi

if ! command -v node &>/dev/null; then
    echo "  ✗ Node.js not found. Install Node.js >=18 (e.g. apt install nodejs)."
    exit 1
fi

node_ver=$(node --version 2>&1 | sed 's/v//')
echo "  ✓ Node.js ($node_ver)"

if ! command -v npm &>/dev/null; then
    echo "  ✗ npm not found. Install npm (e.g. apt install npm)."
    exit 1
fi
echo "  ✓ npm ($(npm --version))"

# ── 2. Create venv ────────────────────────────────────────────────────────────
echo ""
echo "[2/5] Creating Python virtual environment..."
echo "  → Using: $PYTHON"

VENV_DIR="$PROJECT_DIR/.venv"
if [[ -d "$VENV_DIR" ]]; then
    echo "  → Venv already exists at $VENV_DIR"
else
    echo "  → Running: $PYTHON -m venv .venv"
    if "$PYTHON" -m venv "$VENV_DIR"; then
        echo "  ✓ Created venv at $VENV_DIR"
    else
        echo "  ✗ Failed to create venv. Try: apt install python3-venv"
        exit 1
    fi
fi

# Activate — handle both Linux (bin/) and Windows (Scripts/)
if [[ -f "$VENV_DIR/bin/activate" ]]; then
    source "$VENV_DIR/bin/activate"
elif [[ -f "$VENV_DIR/Scripts/activate" ]]; then
    source "$VENV_DIR/Scripts/activate"
else
    echo "  ✗ Cannot find venv activate script"
    exit 1
fi
echo "  → Activated: $(which python)"

# ── 3. Install Python dependencies ────────────────────────────────────────────
echo ""
echo "[3/5] Installing Python dependencies..."

echo "  → Upgrading pip..."
pip install --upgrade pip 2>&1
if [[ $? -ne 0 ]]; then
    echo "  ✗ pip upgrade failed"
    exit 1
fi

echo "  → Installing core Python packages from requirements.txt..."
MAX_RETRIES=2
RETRY_DELAY=3
for attempt in $(seq 1 $MAX_RETRIES); do
    pip install -r "$PROJECT_DIR/requirements.txt" 2>&1
    pip_exit=$?
    if [[ $pip_exit -eq 0 ]]; then
        break
    elif [[ $attempt -lt $MAX_RETRIES ]]; then
        echo "  → Network issue? Retrying in ${RETRY_DELAY}s (attempt $((attempt+1))/${MAX_RETRIES})..."
        sleep $RETRY_DELAY
    else
        echo "  ✗ pip install failed after ${MAX_RETRIES} attempts — see output above"
        echo "    Try: pip install --pre -r requirements.txt"
        echo "    (--pre allows pre-release wheels for newer Python versions)"
        exit 1
    fi
done
echo "  ✓ Core Python deps installed ($(pip list --format=columns 2>/dev/null | wc -l) packages)"

if $INSTALL_RAG; then
    echo ""
    echo "  → Installing RAG dependencies (CPU-only PyTorch, ChromaDB, etc.)..."
    if [[ -f "$PROJECT_DIR/requirements-rag.txt" ]]; then
        pip install -r "$PROJECT_DIR/requirements-rag.txt" 2>&1
        rag_exit=$?
        if [[ $rag_exit -eq 0 ]]; then
            echo "  ✓ RAG dependencies installed"
        else
            echo "  ⚠  RAG installation had issues (exit code $rag_exit)"
            echo "     You can install manually later: pip install -r requirements-rag.txt"
        fi
    else
        echo "  ! requirements-rag.txt not found, skipping RAG install"
    fi
fi

# ── 4. Install npm dependencies ───────────────────────────────────────────────
echo ""
echo "[4/5] Installing npm dependencies..."

FRONTEND_DIR="$PROJECT_DIR/web_ui/frontend"
if [[ -d "$FRONTEND_DIR" ]]; then
    cd "$FRONTEND_DIR"
    echo "  → Installing npm packages (this may take a while)..."
    npm install 2>&1
    if [[ $? -ne 0 ]]; then
        echo "  ✗ npm install failed — see output above"
        exit 1
    fi
    echo "  ✓ npm deps installed"
    cd "$PROJECT_DIR"
else
    echo "  ! Frontend directory not found at $FRONTEND_DIR, skipping."
fi

# ── 5. Build frontend ─────────────────────────────────────────────────────────
echo ""
echo "[5/5] Building frontend..."

if [[ -d "$FRONTEND_DIR" ]]; then
    cd "$FRONTEND_DIR"
    echo "  → Building frontend bundle..."
    if npm run build 2>&1; then
        echo "  ✓ Frontend built → $FRONTEND_DIR/dist/"
    else
        echo "  ✗ Frontend build failed — see output above"
        exit 1
    fi
    cd "$PROJECT_DIR"
else
    echo "  ! Skipping frontend build."
fi

# ── Make scripts executable ────────────────────────────────────────────
echo ""
echo "[+] Making scripts executable..."
chmod +x "$PROJECT_DIR/start_thoughtmachine.sh" 2>/dev/null || true
chmod +x "$PROJECT_DIR/install_thoughtmachine.sh" 2>/dev/null || true
echo "  ✓ Scripts are now executable"

echo ""
echo "============================================"
echo "  ✓ Install complete!"
echo ""
echo "  Next steps:"
if $IS_WINDOWS; then
    echo "    1. Double-click: start_thoughtmachine.bat"
    echo ""
    echo "    2. Or for production mode (serves from dist/):"
    echo "       start_thoughtmachine.bat --prod"
else
    echo "    1. Activate the virtual environment:"
    echo "       source .venv/bin/activate"
    echo ""
    echo "    2. Start ThoughtMachine (default: dev mode with hot-reload):"
    echo "       ./start_thoughtmachine.sh"
    echo ""
    echo "    3. Or for production mode (serves from dist/):"
    echo "       ./start_thoughtmachine.sh --prod"
fi
echo ""
echo "    4. Open http://127.0.0.1:8000 in your browser"
echo ""
echo "  In dev mode, the frontend runs on http://127.0.0.1:5173"
echo "  (hot-reload) and the backend API on http://127.0.0.1:8000."
echo ""
echo "  Your config file will be created automatically"
echo "  at ~/.thoughtmachine/agent_config.json on first run."
echo "============================================"
