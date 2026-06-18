#!/bin/bash
# Run code quality checks manually
# Use this before committing to ensure code meets standards

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "🔍 Running code quality checks..."
echo ""

cd "$PROJECT_ROOT/backend"

# Ensure .venv exists (uv is the standard)
if [ ! -d ".venv" ]; then
    echo "📦 Creating .venv with uv sync..."
    uv sync
fi
cd "$PROJECT_ROOT"

# Run pre-commit checks
echo "📋 Running pre-commit checks..."
uv run --directory backend pre-commit run --all-files

echo ""
echo "✅ All code quality checks passed!"
echo ""
echo "💡 These checks run automatically before commit (when hooks are installed)"
echo "   To install hooks: pre-commit install"
