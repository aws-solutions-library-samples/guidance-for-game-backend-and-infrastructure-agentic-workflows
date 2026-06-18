#!/bin/bash
# Memory System Test Suite
# Tests both unit tests and E2E tests for memory functionality

set -e

echo "🧪 Game Agent - Memory Test Suite"
echo "========================================"
echo ""

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Test 1: Unit Tests
echo "📋 Test 1: Unit Tests for Semantic Memory"
echo "-------------------------------------------"
cd backend

# Ensure .venv exists (uv is the standard)
if [ ! -d ".venv" ]; then
    echo "📦 Creating .venv with uv sync..."
    uv sync
fi
uv run python -m pytest tests/unit/test_semantic_memory_unit.py -v --tb=short

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✅ Unit tests passed${NC}"
else
    echo -e "${RED}❌ Unit tests failed${NC}"
    exit 1
fi

echo ""
echo "📋 Test 2: E2E Memory Tests (Local)"
echo "-------------------------------------------"
echo -e "${YELLOW}⚠️  Note: These tests require local backend running${NC}"
echo "Starting local environment..."

cd ..
./dev-start.sh > /tmp/memory-test-dev.log 2>&1 &
DEV_PID=$!

# Wait for services to start
echo "Waiting for services to start (30 seconds)..."
sleep 30

# Check if services are up
if curl -s http://localhost:3000/api/health > /dev/null 2>&1; then
    echo -e "${GREEN}✅ Frontend ready${NC}"
else
    echo -e "${RED}❌ Frontend not ready${NC}"
    kill $DEV_PID 2>/dev/null
    exit 1
fi

if curl -s http://localhost:8080/health > /dev/null 2>&1; then
    echo -e "${GREEN}✅ Backend ready${NC}"
else
    echo -e "${YELLOW}⚠️  Backend not responding (may still be starting)${NC}"
fi

# Run E2E tests
cd ui
echo "Running Playwright memory tests..."
npx playwright test tests/memory.spec.ts --reporter=line

TEST_RESULT=$?

# Cleanup
echo ""
echo "Cleaning up..."
cd ..
./dev-stop.sh > /dev/null 2>&1

if [ $TEST_RESULT -eq 0 ]; then
    echo -e "${GREEN}✅ E2E tests passed${NC}"
else
    echo -e "${RED}❌ E2E tests failed${NC}"
    exit 1
fi

echo ""
echo "========================================"
echo -e "${GREEN}✅ All memory tests passed!${NC}"
echo "========================================"
