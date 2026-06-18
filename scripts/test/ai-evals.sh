#!/bin/bash

# Game Agent - AI Evaluation Test Runner
# Runs AI evaluation tests that validate real LLM behavior

set -e

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${BLUE}🧠 Game Agent - AI Evaluation Tests${NC}"
echo "=========================================="

# Check for deployment first
echo -e "\n${BLUE}🔍 Checking test environment...${NC}"
DEPLOYMENT_DETECTED=false
if ./scripts/infrastructure/check-deployment.sh check > /dev/null 2>&1; then
    eval $(./scripts/infrastructure/check-deployment.sh urls)
    DEPLOYMENT_DETECTED=true
    echo -e "${GREEN}✅ Deployed stack detected${NC}"
    echo -e "${BLUE}🤖 Runtime: $RUNTIME_ID${NC}"

    # Export for tests
    export AGENTCORE_RUNTIME_ID="$RUNTIME_ID"
    export AGENTCORE_RUNTIME_ARN="$RUNTIME_ARN"
    export FRONTEND_URL="$FRONTEND_URL"
else
    echo -e "${RED}❌ AI evals require deployed stack${NC}"
    echo -e "${BLUE}💡 Deploy with ./deploy-all.sh${NC}"
    exit 1
fi

# Backend AI evaluation tests
echo -e "\n${BLUE}🤖 AI Agent Behavior Tests${NC}"
cd backend

# Ensure .venv exists (uv is the standard)
if [ ! -d ".venv" ]; then
    echo -e "${BLUE}📦 Creating .venv with uv sync...${NC}"
    uv sync
fi

# Run AI evaluation tests with environment variables
AGENTCORE_RUNTIME_ID="$RUNTIME_ID" \
    AGENTCORE_RUNTIME_ARN="$RUNTIME_ARN" \
    FRONTEND_URL="$FRONTEND_URL" \
    uv run python -m pytest tests/ai_evals/ \
        -m "ai_eval" \
        -v --tb=short \
        --maxfail=3 \
        --timeout=600
cd ..

echo -e "\n${GREEN}✅ AI evaluation tests completed!${NC}"
echo -e "${BLUE}📋 These tests validate:${NC}"
echo -e "   • Real LLM agent responses and error handling"
echo -e "   • System prompt quality and structure"
echo -e "   • Anti-hallucination behavior"
echo -e "   • MCP integration reliability"
