#!/bin/bash

# Helper script to ensure dev services are running for tests
# Usage: source scripts/test/ensure-services.sh [backend|frontend|both]

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Check what's needed
NEED_BACKEND=${1:-both}
NEED_FRONTEND=${1:-both}

if [ "$1" = "backend" ]; then
    NEED_FRONTEND="no"
elif [ "$1" = "frontend" ]; then
    NEED_BACKEND="no"
fi

# Track if we started services (for cleanup decision)
SERVICES_STARTED_BY_TESTS=false

# Check backend
BACKEND_RUNNING=false
if curl -s http://localhost:8080/health > /dev/null 2>&1; then
    BACKEND_RUNNING=true
fi

# Check frontend
FRONTEND_RUNNING=false
if curl -s http://localhost:3000 > /dev/null 2>&1; then
    FRONTEND_RUNNING=true
fi

# Start services if needed
NEED_TO_START=false

if [ "$NEED_BACKEND" != "no" ] && [ "$BACKEND_RUNNING" = false ]; then
    echo -e "${YELLOW}⚙️  Backend not running, starting...${NC}"
    NEED_TO_START=true
fi

if [ "$NEED_FRONTEND" != "no" ] && [ "$FRONTEND_RUNNING" = false ]; then
    echo -e "${YELLOW}⚙️  Frontend not running, starting...${NC}"
    NEED_TO_START=true
fi

if [ "$NEED_TO_START" = true ]; then
    echo -e "${BLUE}🚀 Starting dev environment...${NC}"
    ./dev-start.sh
    SERVICES_STARTED_BY_TESTS=true

    # Wait for services to be ready
    echo -e "${BLUE}⏳ Waiting for services to be ready...${NC}"
    sleep 5

    # Verify backend if needed
    if [ "$NEED_BACKEND" != "no" ]; then
        for i in {1..30}; do
            if curl -s http://localhost:8080/health > /dev/null 2>&1; then
                echo -e "${GREEN}✅ Backend ready${NC}"
                break
            fi
            sleep 1
        done
    fi

    # Verify frontend if needed
    if [ "$NEED_FRONTEND" != "no" ]; then
        for i in {1..30}; do
            if curl -s http://localhost:3000 > /dev/null 2>&1; then
                echo -e "${GREEN}✅ Frontend ready${NC}"
                break
            fi
            sleep 1
        done
    fi
else
    echo -e "${GREEN}✅ Required services already running${NC}"
fi
