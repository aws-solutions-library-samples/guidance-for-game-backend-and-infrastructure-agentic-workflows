#!/bin/bash

# Game Agent - Stress Test Runner
# Runs performance and stress tests independently

set -e

echo "🚀 Running Game Agent Stress Tests..."
echo "================================================"

cd backend

# Ensure .venv exists (uv is the standard)
if [ ! -d ".venv" ]; then
    echo "📦 Creating .venv with uv sync..."
    uv sync
fi

# Run stress tests only
echo "⚡ Running stress/performance tests..."
uv run python -m pytest tests/ -m "stress" -v --tb=short

echo ""
echo "✅ Stress tests completed!"
echo "================================================"
