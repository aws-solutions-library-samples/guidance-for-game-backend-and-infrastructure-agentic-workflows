#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "=================================================="
echo " 📥 Downloading KB Documentation"
echo "=================================================="

cd "$PROJECT_ROOT/backend"

# Use UV as the default package manager
if command -v uv &> /dev/null; then
    echo "📦 Syncing dependencies with uv..."
    uv sync
    uv run python "$SCRIPT_DIR/scrape_aws_docs.py"
else
    # Fallback: try system Python with dependency check
    if ! python3 -c "import requests, bs4, html2text" 2>/dev/null; then
        echo "❌ UV not found and Python dependencies missing"
        echo "   Install UV: curl -LsSf https://astral.sh/uv/install.sh | sh"
        echo "   Or install deps: pip3 install --user requests beautifulsoup4 html2text"
        exit 1
    fi
    echo "📦 Using system Python..."
    python3 "$SCRIPT_DIR/scrape_aws_docs.py"
fi

echo ""
echo "✅ Documentation download complete"
