#!/usr/bin/env bash
# Game Agent - Deployment Validation Script
# Verifies that all deployed resources are healthy and accessible
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_NAME="game-agent"

# Default region (script uses `set -u`, so a bare $AWS_REGION would abort). Honor
# an exported AWS_REGION, else the profile's configured region, else us-west-2.
AWS_REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || echo us-west-2)}"

# Resolve AWS profile from environment or ui/.env.local (matches scripts/deploy.sh).
# An explicitly set AWS_PROFILE always wins; otherwise fall back to ui/.env.local.
if [ -z "${AWS_PROFILE:-}" ] && [ -f "$SCRIPT_DIR/ui/.env.local" ]; then
    _profile=$(grep '^AWS_PROFILE=' "$SCRIPT_DIR/ui/.env.local" | cut -d= -f2 | tr -d '[:space:]')
    [ -n "$_profile" ] && export AWS_PROFILE="$_profile"
fi

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS=0
FAIL=0
WARN=0

check() {
    local desc="$1"
    shift
    if "$@" > /dev/null 2>&1; then
        echo -e "  ${GREEN}PASS${NC} $desc"
        PASS=$((PASS + 1))
    else
        echo -e "  ${RED}FAIL${NC} $desc"
        FAIL=$((FAIL + 1))
    fi
}

warn_check() {
    local desc="$1"
    shift
    if "$@" > /dev/null 2>&1; then
        echo -e "  ${GREEN}PASS${NC} $desc"
        PASS=$((PASS + 1))
    else
        echo -e "  ${YELLOW}WARN${NC} $desc"
        WARN=$((WARN + 1))
    fi
}

echo "============================================"
echo "  Game Agent - Deployment Validation"
echo "============================================"
echo ""

# --- CloudFormation Stacks ---
echo "CloudFormation Stacks:"
for stack in "${PROJECT_NAME}-infrastructure" "${PROJECT_NAME}-guardrails" "${PROJECT_NAME}-frontend" "${PROJECT_NAME}-security"; do
    check "Stack ${stack}" aws cloudformation describe-stacks --stack-name "$stack" --query "Stacks[0].StackStatus" --output text
done

for kb in gamelift eks cost; do
    check "Stack ${PROJECT_NAME}-kb-${kb}" aws cloudformation describe-stacks --stack-name "${PROJECT_NAME}-kb-${kb}" --query "Stacks[0].StackStatus" --output text
done
echo ""

# --- AgentCore Runtime ---
echo "AgentCore Runtime:"
if [ -f backend/.bedrock_agentcore.yaml ]; then
    RUNTIME_ID=$(yq eval '.agents.gameagentruntime.bedrock_agentcore.agent_id' backend/.bedrock_agentcore.yaml 2>/dev/null || echo "")
    if [ -n "$RUNTIME_ID" ]; then
        check "Runtime registered (${RUNTIME_ID})" test -n "$RUNTIME_ID"
        warn_check "Runtime reachable" aws bedrock-agentcore get-agent-runtime --agent-runtime-id "$RUNTIME_ID" --query "status" --output text
    else
        echo -e "  ${RED}FAIL${NC} Runtime ID not found in .bedrock_agentcore.yaml"
        FAIL=$((FAIL + 1))
    fi
else
    echo -e "  ${RED}FAIL${NC} .bedrock_agentcore.yaml not found"
    ((FAIL++))
fi
echo ""

# --- Frontend (ECS Express) ---
echo "Frontend (ECS Express):"
FRONTEND_URL=$(aws cloudformation describe-stacks --stack-name "${PROJECT_NAME}-frontend" --region $AWS_REGION --query 'Stacks[0].Outputs[?OutputKey==`ServiceUrl`].OutputValue' --output text 2>/dev/null || echo "")
if [ -n "$FRONTEND_URL" ] && [ "$FRONTEND_URL" != "None" ]; then
    check "ECS Express service exists" test -n "$FRONTEND_URL"
    warn_check "Health endpoint" curl -sf "https://${FRONTEND_URL}/api/health"
else
    echo -e "  ${RED}FAIL${NC} ECS Express frontend service not found"
    ((FAIL++))
fi
echo ""

# --- Knowledge Bases ---
echo "Knowledge Bases:"
if [ -f backend/.env.local ]; then
    for kb_var in GBAW_GAMELIFT_KB_ID GBAW_EKS_KB_ID GBAW_COST_KB_ID; do
        KB_ID=$(grep "^${kb_var}=" backend/.env.local 2>/dev/null | cut -d'=' -f2 || echo "")
        if [ -n "$KB_ID" ]; then
            check "KB ${kb_var} configured (${KB_ID})" test -n "$KB_ID"
        else
            echo -e "  ${YELLOW}WARN${NC} ${kb_var} not configured"
            WARN=$((WARN + 1))
        fi
    done
else
    echo -e "  ${YELLOW}WARN${NC} backend/.env.local not found (KB IDs not configured)"
    ((WARN++))
fi
echo ""

# --- Cognito ---
echo "Cognito:"
USER_POOL_ID=$(aws cloudformation describe-stacks --stack-name "${PROJECT_NAME}-infrastructure" --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text 2>/dev/null || echo "")
if [ -n "$USER_POOL_ID" ]; then
    check "User Pool exists (${USER_POOL_ID})" aws cognito-idp describe-user-pool --user-pool-id "$USER_POOL_ID" --query "UserPool.Status" --output text
else
    echo -e "  ${RED}FAIL${NC} User Pool not found"
    ((FAIL++))
fi
echo ""

# --- Guardrails ---
echo "Bedrock Guardrails:"
GUARDRAIL_ID=$(aws cloudformation describe-stacks --stack-name "${PROJECT_NAME}-guardrails" --query "Stacks[0].Outputs[?OutputKey=='GuardrailId'].OutputValue" --output text 2>/dev/null || echo "")
if [ -n "$GUARDRAIL_ID" ]; then
    check "Guardrail exists (${GUARDRAIL_ID})" aws bedrock get-guardrail --guardrail-identifier "$GUARDRAIL_ID" --query "status" --output text
else
    echo -e "  ${YELLOW}WARN${NC} Guardrail not found"
    ((WARN++))
fi
echo ""

# --- Summary ---
echo "============================================"
echo -e "  Results: ${GREEN}${PASS} passed${NC}, ${RED}${FAIL} failed${NC}, ${YELLOW}${WARN} warnings${NC}"
echo "============================================"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
