#!/bin/bash
set -e
cd "$(dirname "$0")/.."
source .venv/bin/activate 2>/dev/null || true
echo '=== Integration Tests ==='
python -m pytest tests/integration/ -x --tb=short
echo '=== Frontend Tests ==='
npm test --prefix web_ui/frontend
echo '=== All checks passed ==='
