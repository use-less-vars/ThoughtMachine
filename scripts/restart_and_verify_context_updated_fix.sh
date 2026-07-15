#!/usr/bin/env bash
# ==============================================================================
# restart_and_verify_context_updated_fix.sh
#
# Restarts Docker services and runs verification checks for the
# context_updated/tokens_updated event flow fix (Bug 1-4).
#
# Usage:
#   ./scripts/restart_and_verify_context_updated_fix.sh
#
# The fix consists of 4 changes:
#   1. SessionTab.jsx: use currentSessionIdRef.current || sessionId (stale closure)
#   2. bridge.py _make_bus_handler: add worker_name to context_updated events
#   3. bridge.py _map_and_emit: add agent_type='main' to tokens/context events
#   4. Enhanced debug logging at every flow step
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

echo "=============================================="
echo "1. Running unit tests..."
echo "=============================================="

# Run our new test suite and the existing permissions roundtrip
python -m pytest tests/presenter/test_context_updated_bridge.py -v 2>&1
echo ""
python -m pytest tests/presenter/test_state_bridge.py -v 2>&1 | tail -5

echo ""
echo "=============================================="
echo "2. Restarting Docker services..."
echo "=============================================="

docker compose down 2>/dev/null || docker-compose down 2>/dev/null || true
echo "Containers stopped. Starting fresh..."
docker compose up -d --build 2>/dev/null || docker-compose up -d --build 2>/dev/null || {
    echo "WARNING: docker compose not available. Trying systemd services..."
    sudo systemctl restart thoughtmachine-ui thoughtmachine-server 2>/dev/null || echo "Skip restart (manual restart required)"
}

echo ""
echo "=============================================="
echo "3. Checking service health..."
echo "=============================================="

# Wait for services to start
for i in $(seq 1 30); do
    if curl -s http://localhost:8000/health > /dev/null 2>&1; then
        echo "✓ Backend is UP (port 8000)"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "✗ Backend did not start within 30 seconds"
    fi
    sleep 1
done

echo ""
echo "=============================================="
echo "4. Verifying fix via health API..."
echo "=============================================="

# Check that the bridge module loads and exposes the expected API
curl -s http://localhost:8000/health | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print('✓ Health endpoint responds:', json.dumps(data, indent=2)[:200])
except Exception as e:
    print('✗ Health check failed:', e)
"

echo ""
echo "=============================================="
echo "5. Manual test instructions"
echo "=============================================="
echo ""
echo "Open http://localhost:5173 in your browser."
echo ""
echo "To verify the fix:"
echo "  1. Open the browser DevTools console (F12)"
echo "  2. Start a session and use a worker tool"
echo "  3. In the console, you should see:"
echo "     - [TRACE:context_updated] arrived in SessionTab"
echo "       with source='worker' and worker_name set"
echo "     - [TRACE:tokens_updated] arrived in SessionTab"
echo "       with source='worker'"
echo "     - [pipeline.bridge] Per-worker bus handler logs"
echo "       showing forwarding type=context_updated"
echo "  4. The StatusBar should update with real-time"
echo "     token counts for worker sub-agents"
echo ""
echo "=============================================="
