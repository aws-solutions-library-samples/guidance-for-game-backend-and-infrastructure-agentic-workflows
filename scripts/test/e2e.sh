#!/bin/bash

# Game Agent - E2E Test Runner
# Runs end-to-end tests that validate complete user workflows

set -e

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}🎭 Game Agent - E2E Tests${NC}"
echo "================================"

# Ensure frontend and backend are running
echo -e "\n${BLUE}🔍 Checking services availability...${NC}"
source scripts/test/ensure-services.sh both

# Frontend E2E tests
echo -e "\n${BLUE}🌐 Frontend E2E Tests${NC}"
cd ui
npx playwright install --with-deps 2>/dev/null
npx playwright test
cd ..

echo -e "\n${GREEN}✅ E2E tests completed!${NC}"
echo -e "${BLUE}📋 These tests validate:${NC}"
echo -e "   • Complete user workflows"
echo -e "   • UI interactions and responsiveness"
echo -e "   • Memory system behavior"
echo -e "   • Performance benchmarks"

# Cleanup if we started services
if [ "$SERVICES_STARTED_BY_TESTS" = true ]; then
    echo -e "\n${BLUE}🧹 Stopping services started by tests...${NC}"
    ./dev-stop.sh
fi
