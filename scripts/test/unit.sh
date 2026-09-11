#!/bin/bash

# Game Agent - Unit Test Runner
# Runs only unit tests for fast feedback during development

set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}🧪 Game Agent - Unit Tests${NC}"
echo "=================================="

# Repository script tests (mocked; no AWS or Kubernetes access)
echo -e "\n${BLUE}🛠️  Repository Script Tests${NC}"
python3 -m unittest discover -s scripts/test -p 'test_*.py' -v

# Backend unit tests (fast, behavioral validation)
echo -e "\n${BLUE}🐍 Backend Unit Tests (1.3s execution)${NC}"
cd backend

# Ensure .venv exists (uv is the standard)
if [ ! -d ".venv" ]; then
    echo -e "${BLUE}📦 Creating .venv with uv sync...${NC}"
    uv sync
fi

# Run organized unit tests
uv run python -m pytest tests/unit/ \
    -v --tb=short \
    --maxfail=5

echo -e "\n${BLUE}📊 Coverage Report${NC}"
uv run python -m pytest tests/unit/ \
    --cov=src \
    --cov-report=term-missing \
    --cov-report=html:htmlcov \
    -q
cd ..

# Frontend unit tests
echo -e "\n${BLUE}⚛️  Frontend Unit Tests${NC}"
cd ui
npm test -- --passWithNoTests --watchAll=false --silent
cd ..

echo -e "\n${GREEN}✅ Unit tests completed! (Fast feedback: 1.3s backend + frontend)${NC}"
echo -e "${BLUE}📈 Coverage report available at: backend/htmlcov/index.html${NC}"
