#!/bin/bash

# Game Agent - bounded deployed availability test runner
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

echo "🚀 Running Game Agent Bounded Availability Tests..."
echo "================================================"

if ! source "$PROJECT_ROOT/scripts/test/resolve-deployment-targets.sh"; then
    echo "❌ Availability tests require a valid deployed stack"
    echo "   Deploy with: ./deploy-all.sh"
    exit 1
fi

cd backend
if [ ! -d ".venv" ]; then
    echo "📦 Creating .venv with uv sync..."
    uv sync
fi

echo "⚡ Running bounded, read-only frontend and AgentCore availability checks..."
uv run python -m pytest tests/performance -m "stress" -v --tb=short

echo ""
echo "✅ Bounded availability tests completed!"
echo "================================================"
