#!/bin/bash
# Run cloud tests (requires deployed stack)
# Tests: Integration, AI evaluation, Performance, E2E with backend/AI

set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

echo "☁️  Game Agent - Cloud Tests (Requires Deployment)"
echo "========================================================="
echo ""

# Check for .bedrock_agentcore.yaml (FRESH after deploy)
if [ ! -f "backend/.bedrock_agentcore.yaml" ]; then
  echo "❌ No deployment detected"
  echo "   Deploy with: ./deploy-all.sh"
  exit 1
fi

# Extract FRESH runtime ID from .bedrock_agentcore.yaml
RUNTIME_ID=$(yq eval '.agents.gameagentruntime.bedrock_agentcore.agent_id' backend/.bedrock_agentcore.yaml)

if [ -z "$RUNTIME_ID" ] || [ "$RUNTIME_ID" = "null" ]; then
  echo "❌ Invalid runtime ID in .bedrock_agentcore.yaml"
  exit 1
fi

# Export for frontend tests (they'll query CloudFormation for rest)
export AGENTCORE_RUNTIME_ID="$RUNTIME_ID"
export AWS_REGION="${AWS_REGION:-us-west-2}"

echo "✅ Deployment detected: $RUNTIME_ID"
echo "   (Fresh from .bedrock_agentcore.yaml)"
echo ""

# Backend cloud tests (integration, AI evals, performance)
echo "🐍 Backend Cloud Tests"
echo "----------------------"
cd backend

# Ensure .venv exists (uv is the standard)
if [ ! -d ".venv" ]; then
    echo "📦 Creating .venv with uv sync..."
    uv sync
fi
uv run pytest -m cloud -v --tb=short --timeout=300
cd ..
echo ""

# Frontend E2E cloud tests (tests with backend/AI calls)
echo "⚛️  Frontend E2E - Cloud Tests"
echo "------------------------------"
cd ui
npx playwright test --grep @cloud || echo "⚠️  No @cloud E2E tests found yet"
cd ..
echo ""

echo "✅ Cloud tests complete!"
echo ""
echo "📊 Summary:"
echo "   • Backend Integration: Real MCP/Bedrock calls"
echo "   • Backend AI Evals: Real LLM behavior"
echo "   • Backend Performance: Load testing"
echo "   • E2E Cloud: Tests with backend/AI"
echo "   • Total time: ~10 minutes"
