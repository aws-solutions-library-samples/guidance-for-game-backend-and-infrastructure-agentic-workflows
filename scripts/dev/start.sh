#!/bin/bash

# Game Agent Development Environment Launcher
# Pure AgentCore Runtime - same runtime for dev and prod

# Set up colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo -e "${CYAN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║              Game Agent - AgentCore Dev                   ║${NC}"
echo -e "${CYAN}║          Pure AgentCore Runtime Development              ║${NC}"
echo -e "${CYAN}║                                                          ║${NC}"
echo -e "${CYAN}║  🤖 Native AgentCore Runtime                             ║${NC}"
echo -e "${CYAN}║  ⚡ Same runtime as production                            ║${NC}"
echo -e "${CYAN}║  🚀 No FastAPI wrapper needed                            ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════════════╝${NC}"

# Function to check if a port is in use
check_port() {
    local port=$1
    if lsof -Pi :$port -sTCP:LISTEN -t >/dev/null 2>&1; then
        return 0  # Port is in use
    else
        return 1  # Port is free
    fi
}

# Function to start AgentCore Runtime
start_agentcore() {
    echo -e "\n${BLUE}🤖 Starting AgentCore Runtime (Port 8080)...${NC}"

    # Kill any existing AgentCore processes to refresh credentials
    if check_port 8080; then
        echo -e "${YELLOW}⚠️  Stopping existing AgentCore Runtime to refresh credentials...${NC}"
        pkill -f "python.*agentcore_main.py" 2>/dev/null
        sleep 2
    fi

    cd backend

    # Check UV
    if ! command -v uv &> /dev/null; then
        echo -e "${RED}❌ UV not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh${NC}"
        return 1
    fi

    # Install dependencies only if pyproject.toml changed (hash-based check)
    CURRENT_HASH=$(md5 -q pyproject.toml 2>/dev/null || md5sum pyproject.toml | awk '{print $1}')
    INSTALLED_HASH=""
    if [ -f ".venv/.pyproject_hash" ]; then
        INSTALLED_HASH=$(cat .venv/.pyproject_hash)
    fi

    if [ "$CURRENT_HASH" != "$INSTALLED_HASH" ]; then
        echo -e "${YELLOW}📦 Syncing dependencies with UV...${NC}"
        uv sync > /dev/null 2>&1
        echo "$CURRENT_HASH" > .venv/.pyproject_hash
    fi

    # Set environment for local development
    export PYTHONPATH=$(pwd)

    # Auto-detect and set Memory ID if available
    if [ -f ".bedrock_agentcore.yaml" ]; then
        MEMORY_ID=$(yq eval '.agents.gameagentruntime.memory.memory_id' .bedrock_agentcore.yaml 2>/dev/null | tr -d '"')
        if [ -n "$MEMORY_ID" ] && [ "$MEMORY_ID" != "null" ]; then
            export BEDROCK_AGENTCORE_MEMORY_ID="$MEMORY_ID"
            echo -e "${GREEN}🧠 Memory enabled: $MEMORY_ID${NC}"
        fi
    fi

    # Start AgentCore Runtime in background with verbose logging
    echo -e "${GREEN}🚀 Starting AgentCore Runtime...${NC}"
    echo -e "${BLUE}   Logs: tail -f logs/dev-agentcore.log${NC}"
    uv run python src/agentcore_main.py > ../logs/dev-agentcore.log 2>&1 &
    BACKEND_PID=$!

    # Wait a moment and check if it started
    sleep 3
    if kill -0 $BACKEND_PID 2>/dev/null; then
        echo -e "${GREEN}✅ AgentCore Runtime started successfully (PID: $BACKEND_PID)${NC}"
        echo -e "${BLUE}   Runtime: http://localhost:8080${NC}"
        echo -e "${BLUE}   Health: http://localhost:8080/ping${NC}"
        echo $BACKEND_PID > ../logs/agentcore.pid
        cd ..
        return 0
    else
        echo -e "${RED}❌ AgentCore Runtime failed to start. Check logs/dev-agentcore.log${NC}"
        cd ..
        return 1
    fi
}

# Function to start frontend
start_frontend() {
    echo -e "\n${BLUE}🎨 Starting Frontend (Port 3000)...${NC}"

    # Kill any existing frontend processes
    if check_port 3000; then
        echo -e "${YELLOW}⚠️  Stopping existing frontend...${NC}"
        pkill -f "next dev" 2>/dev/null
        sleep 2
    fi

    cd ui

    # Check Node.js
    if ! command -v node &> /dev/null; then
        echo -e "${RED}❌ Node.js not found. Please install Node.js 18+${NC}"
        return 1
    fi

    # Install dependencies
    if [ ! -d "node_modules" ] || [ package.json -nt node_modules ]; then
        echo -e "${YELLOW}📦 Installing/updating Node.js dependencies...${NC}"
        npm install > /dev/null 2>&1
    fi

    # Check .env.local
    if [ ! -f ".env.local" ]; then
        echo -e "${YELLOW}⚙️  Creating .env.local from example...${NC}"
        cp .env.local.example .env.local
        echo -e "${YELLOW}⚠️  Please update .env.local with your AWS credentials${NC}"
    fi

    # Configure frontend for AgentCore Runtime
    if ! grep -q "NEXT_PUBLIC_AGENTCORE_ENDPOINT" .env.local; then
        echo "" >> .env.local
        echo "# AgentCore Runtime Configuration" >> .env.local
        echo "NEXT_PUBLIC_AGENTCORE_ENDPOINT=http://localhost:8080" >> .env.local
        echo "NEXT_PUBLIC_USE_AGENTCORE=true" >> .env.local
    fi
    echo -e "${GREEN}🚀 Starting frontend (configured for AgentCore)...${NC}"

    npm run dev > ../logs/dev-frontend.log 2>&1 &
    FRONTEND_PID=$!

    # Wait a moment and check if it started
    sleep 5
    if kill -0 $FRONTEND_PID 2>/dev/null; then
        echo -e "${GREEN}✅ Frontend started successfully (PID: $FRONTEND_PID)${NC}"
        echo -e "${BLUE}   UI: http://localhost:3000${NC}"
        echo $FRONTEND_PID > ../logs/frontend.pid
        cd ..
        return 0
    else
        echo -e "${RED}❌ Frontend failed to start. Check logs/dev-frontend.log${NC}"
        cd ..
        return 1
    fi
}

# Create logs directory if it doesn't exist
mkdir -p logs

# Start services
echo -e "${CYAN}🚀 Starting Game Agent AgentCore Development Environment...${NC}"

# Start AgentCore Runtime
if start_agentcore; then
    BACKEND_STARTED=true
else
    BACKEND_STARTED=false
fi

# Start frontend
if start_frontend; then
    FRONTEND_STARTED=true
else
    FRONTEND_STARTED=false
fi

# Summary
echo -e "\n${CYAN}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}  ║                    🎉 STARTUP COMPLETE                     ║${NC}"
echo -e "${CYAN}  ╚════════════════════════════════════════════════════════════╝${NC}"

if [ "$BACKEND_STARTED" = true ]; then
    echo -e "${GREEN}✅ AgentCore Runtime: http://localhost:8080${NC}"
    echo -e "${GREEN}   Health Check: http://localhost:8080/ping${NC}"
else
    echo -e "${RED}❌ AgentCore Runtime: Failed to start${NC}"
fi

if [ "$FRONTEND_STARTED" = true ]; then
    echo -e "${GREEN}✅ Frontend: http://localhost:3000 (configured for AgentCore)${NC}"
else
    echo -e "${RED}❌ Frontend: Failed to start${NC}"
fi

echo -e "\n${YELLOW}📋 Management Commands:${NC}"
echo -e "${YELLOW}   Stop all: ./dev-stop.sh${NC}"
echo -e "${YELLOW}   Logs: tail -f logs/dev-agentcore.log logs/dev-frontend.log${NC}"
echo -e "${YELLOW}   Test: curl -X POST http://localhost:8080/invocations -H 'Content-Type: application/json' -d '{\"prompt\": \"Hello\"}'${NC}"

echo -e "\n${CYAN}🤖 AgentCore Runtime Features:${NC}"
echo -e "${GREEN}   • Native AgentCore Runtime execution${NC}"
echo -e "${GREEN}   • Direct agent invocation (no FastAPI wrapper)${NC}"
echo -e "${GREEN}   • Same runtime as production deployment${NC}"
echo -e "${GREEN}   • Development-production parity${NC}"
echo -e "${GREEN}   • All your agents, MCP integrations, and optimizations work as-is${NC}"

if [ "$BACKEND_STARTED" = true ] && [ "$FRONTEND_STARTED" = true ]; then
    echo -e "\n${GREEN}🎉 AgentCore development environment ready! 🤖${NC}"
    exit 0
else
    echo -e "\n${YELLOW}⚠️  Some services failed to start. Check the logs above.${NC}"
    exit 1
fi
