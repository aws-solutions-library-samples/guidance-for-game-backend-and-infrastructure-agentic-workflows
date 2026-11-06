#!/bin/bash
# Run fast localhost tests (no deployment needed)
# Tests: Unit tests (backend + frontend) + Fast E2E UI tests

set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

echo "🧪 Game Agent - Localhost Tests (Fast Feedback)"
echo "======================================================"
echo ""

# Backend unit tests
echo "🐍 Backend Unit Tests"
echo "---------------------"
cd backend

# Ensure .venv exists (uv is the standard)
if [ ! -d ".venv" ]; then
    echo "📦 Creating .venv with uv sync..."
    uv sync
fi
uv run pytest tests/unit/ -v --tb=short
cd ..
echo ""

# Frontend unit tests (Jest). An empty selection fails instead of passing.
echo "⚛️  Frontend Unit Tests"
echo "----------------------"
cd ui
npm test -- --watchAll=false
cd ..
echo ""

# Frontend E2E: the UI specs tagged @localhost, run against the local
# frontend at http://localhost:3000. Playwright reuses a server you already
# started (for example scripts/dev/start.sh); otherwise it starts one itself
# and waits for "/" to compile before the first test. A failing spec, or a
# selection that matches no spec, fails this script (#742). Specs that need
# deployed credentials or configured AgentCore Memory are not tagged.
echo "🎭 Frontend E2E - @localhost specs"
echo "----------------------------------"
cd ui
npx playwright test --grep @localhost
cd ..
echo ""

echo "✅ Localhost tests complete!"
echo ""
echo "📊 Summary:"
echo "   • Backend unit tests (pytest tests/unit/)"
echo "   • Frontend unit tests (Jest)"
echo "   • Frontend E2E: Playwright specs tagged @localhost"
echo ""
echo "💡 To run cloud tests: ./test-cloud.sh (requires deployment)"
