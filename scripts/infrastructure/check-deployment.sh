#!/bin/bash

# Game Agent - Deployment Detection Utility
# Checks if stacks are deployed and returns deployment info

set -e

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

AGENTCORE_CONFIG="backend/.bedrock_agentcore.yaml"

# Resolve AWS profile from environment or ui/.env.local (same as backend settings.py)
if [ -z "$AWS_PROFILE" ]; then
    ENV_LOCAL="ui/.env.local"
    if [ -f "$ENV_LOCAL" ]; then
        AWS_PROFILE=$(grep -E '^AWS_PROFILE=' "$ENV_LOCAL" | cut -d'=' -f2 | tr -d '[:space:]')
    fi
fi
AWS_PROFILE_FLAG=()
if [ -n "$AWS_PROFILE" ]; then
    AWS_PROFILE_FLAG=(--profile "$AWS_PROFILE")
fi

# Function to check if deployment exists
check_deployment() {
    # Check if AgentCore config exists
    if [ ! -f "$AGENTCORE_CONFIG" ]; then
        return 1
    fi

    # Extract runtime ID from AgentCore config
    RUNTIME_ID=$(yq eval '.agents.gameagentruntime.bedrock_agentcore.agent_id' "$AGENTCORE_CONFIG" 2>/dev/null)
    if [ -z "$RUNTIME_ID" ] || [ "$RUNTIME_ID" = "null" ]; then
        return 1
    fi

    # Verify runtime exists in AWS
    if ! aws bedrock-agentcore-control get-agent-runtime \
        --agent-runtime-id "$RUNTIME_ID" \
        --region us-west-2 "${AWS_PROFILE_FLAG[@]}" >/dev/null 2>&1; then
        return 1
    fi

    return 0
}

# Function to get deployment URLs
get_deployment_urls() {
    if [ ! -f "$AGENTCORE_CONFIG" ]; then
        echo "ERROR: No AgentCore config found"
        return 1
    fi

    # Get runtime info from AgentCore config
    RUNTIME_ARN=$(yq eval '.agents.gameagentruntime.bedrock_agentcore.agent_arn' "$AGENTCORE_CONFIG" 2>/dev/null)
    RUNTIME_ID=$(yq eval '.agents.gameagentruntime.bedrock_agentcore.agent_id' "$AGENTCORE_CONFIG" 2>/dev/null)

    # Get frontend URL from CloudFormation
    FRONTEND_URL=$(aws cloudformation describe-stacks \
        --stack-name game-agent-frontend \
        --region us-west-2 "${AWS_PROFILE_FLAG[@]}" \
        --query 'Stacks[0].Outputs[?OutputKey==`ServiceUrl`].OutputValue' \
        --output text 2>/dev/null || echo "")

    if [ -n "$FRONTEND_URL" ]; then
        FRONTEND_URL="https://$FRONTEND_URL"
    fi

    echo "FRONTEND_URL=$FRONTEND_URL"
    echo "RUNTIME_ARN=$RUNTIME_ARN"
    echo "RUNTIME_ID=$RUNTIME_ID"
    echo "DEPLOYED=true"
}

# Main logic
case "${1:-check}" in
    "check")
        if check_deployment; then
            echo -e "${GREEN}✅ Deployment detected${NC}"
            exit 0
        else
            echo -e "${RED}❌ No deployment found${NC}"
            exit 1
        fi
        ;;
    "urls")
        if check_deployment; then
            get_deployment_urls
        else
            echo "DEPLOYED=false"
            exit 1
        fi
        ;;
    "status")
        if check_deployment; then
            echo -e "${GREEN}✅ Stack Status: DEPLOYED${NC}"

            # Get URLs
            eval $(get_deployment_urls)

            echo -e "${BLUE}🌐 Frontend: $FRONTEND_URL${NC}"
            echo -e "${BLUE}🤖 Runtime: $RUNTIME_ID${NC}"
        else
            echo -e "${RED}❌ Stack Status: NOT DEPLOYED${NC}"
            echo -e "${YELLOW}💡 Run ./deploy-all.sh to deploy${NC}"
        fi
        ;;
    *)
        echo "Usage: $0 [check|urls|status]"
        echo "  check  - Exit 0 if deployed, 1 if not"
        echo "  urls   - Output deployment URLs as env vars"
        echo "  status - Human-readable deployment status"
        exit 1
        ;;
esac
