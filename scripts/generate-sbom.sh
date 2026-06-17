#!/bin/bash
set -e

# Generate Software Bill of Materials (SBOM) for Game Agent
# Uses Syft (https://github.com/anchore/syft) to produce SPDX-JSON SBOMs

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SBOM_DIR="$PROJECT_ROOT/sbom"

echo "📦 SBOM Generation - Game Agent"
echo "======================================="

# Check for Syft
if ! command -v syft &> /dev/null; then
    echo "❌ Syft not found."
    echo ""
    echo "Install Syft:"
    echo "  macOS:  brew install syft"
    echo "  Linux:  curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s -- -b /usr/local/bin"
    echo ""
    echo "More info: https://github.com/anchore/syft#installation"
    exit 1
fi

mkdir -p "$SBOM_DIR"

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKEND_SBOM="backend-sbom-${TIMESTAMP}.spdx.json"
FRONTEND_SBOM="frontend-sbom-${TIMESTAMP}.spdx.json"

# Backend SBOM (from source - scans pyproject.toml, requirements.txt, uv.lock)
echo ""
echo "📦 Generating backend SBOM from source..."
syft dir:"$PROJECT_ROOT/backend" \
    -o spdx-json="$SBOM_DIR/$BACKEND_SBOM" \
    --name "game-agent-backend" \
    2>/dev/null
echo "✅ Backend SBOM: sbom/$BACKEND_SBOM"

# Frontend SBOM
# If a Docker image tag is passed as $1, scan the image (more comprehensive).
# Otherwise scan the source directory.
if [ -n "$1" ]; then
    FRONTEND_IMAGE="$1"
    echo ""
    echo "📦 Generating frontend SBOM from image: $FRONTEND_IMAGE..."
    syft "$FRONTEND_IMAGE" \
        -o spdx-json="$SBOM_DIR/$FRONTEND_SBOM" \
        --name "game-agent-frontend" \
        2>/dev/null
else
    echo ""
    echo "📦 Generating frontend SBOM from source..."
    syft dir:"$PROJECT_ROOT/ui" \
        -o spdx-json="$SBOM_DIR/$FRONTEND_SBOM" \
        --name "game-agent-frontend" \
        2>/dev/null
fi
echo "✅ Frontend SBOM: sbom/$FRONTEND_SBOM"

# Update latest symlinks
ln -sf "$BACKEND_SBOM" "$SBOM_DIR/backend-sbom-latest.spdx.json"
ln -sf "$FRONTEND_SBOM" "$SBOM_DIR/frontend-sbom-latest.spdx.json"

echo ""
echo "📋 SBOM Summary"
echo "  Backend:  sbom/$BACKEND_SBOM"
echo "  Frontend: sbom/$FRONTEND_SBOM"
echo ""
echo "🔍 Scan for vulnerabilities with Grype:"
echo "  grype sbom:$SBOM_DIR/backend-sbom-latest.spdx.json"
echo "  grype sbom:$SBOM_DIR/frontend-sbom-latest.spdx.json"
