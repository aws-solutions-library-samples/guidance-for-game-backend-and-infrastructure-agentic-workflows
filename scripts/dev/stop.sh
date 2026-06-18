#!/bin/bash

# Game Agent Development Environment Stopper

# Set up colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo -e "${CYAN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║            Game Agent Development Stop                    ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════════════╝${NC}"

# Function to stop service by PID file
stop_service() {
    local service_name=$1
    local pid_file=$2

    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            echo -e "${YELLOW}🛑 Stopping $service_name (PID: $pid)...${NC}"
            kill "$pid"
            sleep 2
            if kill -0 "$pid" 2>/dev/null; then
                echo -e "${YELLOW}   Force killing $service_name...${NC}"
                kill -9 "$pid"
            fi
            echo -e "${GREEN}✅ $service_name stopped${NC}"
        else
            echo -e "${YELLOW}⚠️  $service_name PID $pid not running${NC}"
        fi
        rm -f "$pid_file"
    else
        echo -e "${YELLOW}⚠️  No PID file found for $service_name${NC}"
    fi
}

# Function to stop by port
stop_by_port() {
    local service_name=$1
    local port=$2

    local pid=$(lsof -ti:$port 2>/dev/null)
    if [ -n "$pid" ]; then
        echo -e "${YELLOW}🛑 Stopping $service_name on port $port (PID: $pid)...${NC}"
        kill "$pid" 2>/dev/null
        sleep 2
        # Check if still running
        local still_running=$(lsof -ti:$port 2>/dev/null)
        if [ -n "$still_running" ]; then
            echo -e "${YELLOW}   Force killing $service_name...${NC}"
            kill -9 "$still_running" 2>/dev/null
        fi
        echo -e "${GREEN}✅ $service_name stopped${NC}"
    else
        echo -e "${BLUE}ℹ️  No $service_name running on port $port${NC}"
    fi
}

# Create logs directory if it doesn't exist
mkdir -p logs

echo -e "${YELLOW}🔍 Stopping development services...${NC}"

# Stop AgentCore Runtime
stop_service "AgentCore Runtime" "logs/agentcore.pid"
stop_by_port "AgentCore Runtime" "8080"

# Stop frontend
stop_service "Frontend" "logs/frontend.pid"
stop_by_port "Frontend" "3000"

# Clean up any remaining processes
echo -e "${YELLOW}🧹 Cleaning up any remaining processes...${NC}"

# Kill any remaining AgentCore processes
pkill -f "python.*agentcore_main.py" 2>/dev/null
sleep 1
pkill -9 -f "python.*agentcore_main.py" 2>/dev/null

# Kill any remaining Next.js dev processes
pkill -f "next dev" 2>/dev/null
sleep 1
pkill -9 -f "next dev" 2>/dev/null

echo -e "\n${GREEN}✅ All development services stopped${NC}"
echo -e "${BLUE}📋 Log files preserved in logs/ directory${NC}"
echo -e "${YELLOW}💡 To restart: ./dev-start.sh${NC}"
