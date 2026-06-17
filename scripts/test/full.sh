#!/bin/bash
# DON'T use set -e - we want to run ALL tests even if some fail

# Track failures
FAILED=0

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${BLUE}🧪 Game Agent - Smart Test Suite${NC}"
echo "========================================"

# Start dev environment for testing
echo -e "\n${BLUE}🚀 Ensuring dev environment is running...${NC}"
source scripts/test/ensure-services.sh both

# Check deployment status
echo -e "\n${BLUE}🔍 Checking test environment...${NC}"

# Check for localhost services FIRST
LOCALHOST_BACKEND=false
LOCALHOST_FRONTEND=false

if curl -s http://localhost:8080/health > /dev/null 2>&1; then
    LOCALHOST_BACKEND=true
    echo -e "${GREEN}✅ Localhost backend detected (port 8080)${NC}"
fi

if curl -s http://localhost:3000 > /dev/null 2>&1; then
    LOCALHOST_FRONTEND=true
    echo -e "${GREEN}✅ Localhost frontend detected (port 3000)${NC}"
fi

# Check for deployed stack
DEPLOYMENT_DETECTED=false
if ./scripts/infrastructure/check-deployment.sh check; then
    eval $(./scripts/infrastructure/check-deployment.sh urls)
    DEPLOYMENT_DETECTED=true
    echo -e "${GREEN}✅ Deployed stack available${NC}"
    echo -e "${BLUE}🌐 Frontend: $FRONTEND_URL${NC}"
    echo -e "${BLUE}🤖 Runtime: $RUNTIME_ID${NC}"
fi

# Determine test mode - PRIORITIZE DEPLOYED for integration/AI tests
if [ "$DEPLOYMENT_DETECTED" = true ]; then
    TEST_MODE="deployed"
    echo -e "${BLUE}📋 Test Mode: DEPLOYED (testing against AWS)${NC}"
elif [ "$LOCALHOST_BACKEND" = true ]; then
    TEST_MODE="localhost"
    echo -e "${BLUE}📋 Test Mode: LOCALHOST (limited tests - memory UI only)${NC}"
else
    TEST_MODE="unit-only"
    echo -e "${YELLOW}⚠️  Test Mode: UNIT-ONLY (no services running)${NC}"
    echo -e "${BLUE}💡 Deploy with ./deploy-all.sh for integration and AI eval tests${NC}"
fi

# Ensure backend .venv exists (uv is the standard)
if [ ! -d "backend/.venv" ]; then
    echo -e "${YELLOW}📦 Creating backend .venv with uv sync...${NC}"
    cd backend && uv sync && cd ..
fi

# Check frontend dependencies
if [ ! -d "ui/node_modules" ]; then
    echo -e "${YELLOW}📦 Installing frontend dependencies...${NC}"
    cd ui && npm install && cd ..
fi

# Run test suite based on deployment mode
echo -e "\n${BLUE}🚀 Running test suite (mode: $TEST_MODE)...${NC}"

# 1. Unit Tests (always run - fast feedback)
echo -e "\n${BLUE}1️⃣  Unit Tests (Fast feedback)${NC}"
./test-unit.sh || FAILED=1

# 2. Integration Tests (deployment-aware)
echo -e "\n${BLUE}2️⃣  Integration Tests${NC}"
cd backend

if [ "$TEST_MODE" = "deployed" ]; then
    export AGENTCORE_RUNTIME_ID="$RUNTIME_ID"
    export AGENTCORE_RUNTIME_ARN="$RUNTIME_ARN"
    export FRONTEND_URL="$FRONTEND_URL"
    echo -e "${BLUE}   Testing against deployed stack${NC}"
elif [ "$TEST_MODE" = "localhost" ]; then
    echo -e "${BLUE}   Testing memory UI against localhost (other tests require deployed stack)${NC}"
else
    echo -e "${YELLOW}   Skipping (deploy with ./deploy-all.sh for integration tests)${NC}"
    cd ..
    # Skip to next section
    echo -e "\n${BLUE}3️⃣  Frontend E2E Tests${NC}"
    echo -e "${YELLOW}   Skipping (no services available)${NC}"

    echo -e "\n${GREEN}✅ Unit tests completed!${NC}"
    echo -e "${BLUE}💡 Deploy with ./deploy-all.sh for integration and AI eval tests${NC}"
    exit 0
fi

uv run python -m pytest tests/integration/ \
    -m "not slow" \
    -v --tb=short \
    --maxfail=5 \
    --timeout=300 || FAILED=1
cd ..

# 3. Frontend E2E Tests
echo -e "\n${BLUE}3️⃣  Frontend E2E Tests${NC}"
if [ "$LOCALHOST_FRONTEND" = true ] || [ "$TEST_MODE" = "deployed" ]; then
    cd ui
    npx playwright install --with-deps 2>/dev/null
    npx playwright test || FAILED=1
    cd ..
else
    echo -e "${YELLOW}   Skipping (frontend not available)${NC}"
fi

# 4. AI Evaluation Tests
echo -e "\n${BLUE}4️⃣  AI Evaluation Tests${NC}"
if [ "$TEST_MODE" = "deployed" ]; then
    echo -e "${BLUE}   Testing against deployed stack${NC}"
    export AGENTCORE_RUNTIME_ID="$RUNTIME_ID"
    export AGENTCORE_RUNTIME_ARN="$RUNTIME_ARN"
    export FRONTEND_URL="$FRONTEND_URL"
else
    echo -e "${YELLOW}   Skipping (AI evals require deployed stack - deploy with ./deploy-all.sh)${NC}"
    echo -e "\n${GREEN}✅ Test suite completed!${NC}"
    if [ $FAILED -ne 0 ]; then
        exit 1
    fi
    exit 0
fi

cd backend

uv run python -m pytest tests/ai_evals/ \
    -m "ai_eval" \
    -v --tb=short \
    --maxfail=3 \
    --timeout=600 || FAILED=1

cd ..

# 5. Stress Tests (deployed only)
if [ "$TEST_MODE" = "deployed" ]; then
    echo -e "\n${BLUE}5️⃣  Stress/Performance Tests${NC}"
    cd backend

    # Check if stress tests exist
    if ls tests/performance/test_*.py 1> /dev/null 2>&1; then
        uv run python -m pytest tests/performance/ \
            -m "stress" \
            -v --tb=short \
            --maxfail=2 \
            --timeout=300 || FAILED=1
    else
        echo -e "${YELLOW}   No stress tests found (skipping)${NC}"
    fi

    cd ..
fi

# Final summary based on test mode
if [ "$TEST_MODE" = "deployed" ]; then
    echo -e "\n${GREEN}🎉 Full test suite completed against deployed stack!${NC}"
    echo -e "${BLUE}📊 Test Summary (Deployed Mode):${NC}"
    echo -e "   • Unit Tests: Fast behavioral validation"
    echo -e "   • Integration Tests: Against deployed AWS stack"
    echo -e "   • E2E Tests: Complete user workflows"
    echo -e "   • AI Evaluation Tests: Real LLM behavior validation"
    echo -e "   • Stress Tests: Performance under load"

elif [ "$TEST_MODE" = "localhost" ]; then
    echo -e "\n${GREEN}🎉 Test suite completed against localhost!${NC}"
    echo -e "${BLUE}📊 Test Summary (Localhost Mode):${NC}"
    echo -e "   • Unit Tests: Fast behavioral validation ✅"
    echo -e "   • Integration Tests: Memory UI tests only ✅"
    echo -e "   • E2E Tests: Complete user workflows ✅"
    echo -e "   • AI Evaluation Tests: Skipped (requires deployed stack)"
    echo -e "   • Stress Tests: Skipped (requires deployed stack)"
    echo -e "\n${BLUE}💡 Deploy with ./deploy-all.sh to run full test suite${NC}"

else
    echo -e "\n${GREEN}✅ Unit test suite completed!${NC}"
    echo -e "${BLUE}📊 Test Summary (Unit-Only Mode):${NC}"
    echo -e "   • Unit Tests: Fast behavioral validation ✅"
    echo -e "\n${YELLOW}💡 For more tests:${NC}"
    echo -e "   • ./dev-start.sh - Start localhost for E2E and memory UI tests"
    echo -e "   • ./deploy-all.sh - Deploy for integration, AI evals, and stress tests"
fi

echo -e "\n${BLUE}📈 Coverage report: backend/htmlcov/index.html${NC}"

# Cleanup if we started services
if [ "$SERVICES_STARTED_BY_TESTS" = true ]; then
    echo -e "\n${BLUE}🧹 Stopping services started by tests...${NC}"
    ./dev-stop.sh
fi

# Exit with failure if any tests failed
if [ $FAILED -ne 0 ]; then
    echo -e "\n${RED}❌ SOME TESTS FAILED - Review output above${NC}"
    exit 1
else
    echo -e "\n${GREEN}✅ ALL TESTS PASSED${NC}"
    exit 0
fi
