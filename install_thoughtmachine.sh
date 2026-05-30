#!/usr/bin/env bash
set -euo pipefail

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

# ── 1. Check prerequisites ────────────────────────────────────────────────────
echo ""
echo "[1/5] Checking prerequisites..."

PYTHON=""
for cmd in python3.12 python3.11 python3; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" --version 2>&1 | awk '{print $2}')
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [[ "$major" -ge 3 && "$minor" -ge 11 ]]; then
            PYTHON="$cmd"
            echo "  ✓ Found $PYTHON ($version)"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo "  ✗ Python >=3.11 not found. Install it first (e.g. python3.11-venv)."
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

VENV_DIR="$PROJECT_DIR/.venv"
if [[ -d "$VENV_DIR" ]]; then
    echo "  → Venv already exists at $VENV_DIR"
else
    "$PYTHON" -m venv "$VENV_DIR"
    echo "  ✓ Created venv at $VENV_DIR"
fi

source "$VENV_DIR/bin/activate"
echo "  → Activated: $(which python)"

# ── 3. Install Python dependencies ────────────────────────────────────────────
echo ""
echo "[3/5] Installing Python dependencies..."

pip install --upgrade pip --quiet
pip install -r "$PROJECT_DIR/requirements.txt" --quiet
echo "  ✓ Python deps installed ($(pip list --format=columns 2>/dev/null | wc -l) packages)"

# ── 4. Install npm dependencies ───────────────────────────────────────────────
echo ""
echo "[4/5] Installing npm dependencies..."

FRONTEND_DIR="$PROJECT_DIR/web_ui/frontend"
if [[ -d "$FRONTEND_DIR" ]]; then
    cd "$FRONTEND_DIR"
    npm install --silent
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
    npm run build
    echo "  ✓ Frontend built → $FRONTEND_DIR/dist/"
    cd "$PROJECT_DIR"
else
    echo "  ! Skipping frontend build."
fi

# ── Make scripts executable ────────────────────────────────────────────
echo ""
echo "[+] Making scripts executable..."
chmod +x "$PROJECT_DIR/start_thoughtmachine.sh"
chmod +x "$PROJECT_DIR/install_thoughtmachine.sh"
echo "  ✓ Scripts are now executable"

echo ""
echo "============================================"
echo "  ✓ Install complete!"
echo ""
echo "  Next steps:"
echo "    1. Activate the virtual environment:"
echo "       source .venv/bin/activate"
echo ""
echo "    2. Start ThoughtMachine:"
echo "       ./start_thoughtmachine.sh"
echo ""
echo "    3. Open http://127.0.0.1:8000 in your browser"
echo ""
echo "  Your config file will be created automatically"
echo "  at ~/.thoughtmachine/agent_config.json on first run."
echo "============================================"
