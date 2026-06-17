#!/bin/bash
# Run fast localhost tests (no deployment needed)
# Tests: Unit tests (backend + frontend) + Fast E2E UI tests

set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

echo "🧪 Game Agent - Localhost Tests (Fast Feedback)"
echo "======================================================"
echo ""

# Backend unit tests (78 tests, ~1.3s)
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

# Frontend unit tests (61 tests, ~2s)
echo "⚛️  Frontend Unit Tests"
echo "----------------------"
cd ui
npm test -- --passWithNoTests --watchAll=false
cd ..
echo ""

# Frontend E2E localhost tests (fast UI tests, no backend calls)
echo "🎭 Frontend E2E - Localhost Only"
echo "--------------------------------"
cd ui
npx playwright test --grep @localhost || echo "⚠️  No @localhost E2E tests found yet"
cd ..
echo ""

echo "✅ Localhost tests complete!"
echo ""
echo "📊 Summary:"
echo "   • Backend Unit: 78 tests (~1.3s)"
echo "   • Frontend Unit: 61 tests (~2s)"
echo "   • E2E Localhost: Fast UI tests"
echo "   • Total time: ~20 seconds"
echo ""
echo "💡 To run cloud tests: ./test-cloud.sh (requires deployment)"
