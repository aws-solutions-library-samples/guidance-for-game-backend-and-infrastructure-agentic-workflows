#!/bin/bash
set -e

# Game Agent - Bedrock Prompt Management Deployment
# Creates/updates prompts in Bedrock Prompt Management for all 4 agents.
# Idempotent: safe to re-run. Writes prompt ARNs to backend/.env.local.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REGION="${AWS_REGION:-us-west-2}"

echo "=================================================="
echo " 📝 Deploying Bedrock Managed Prompts"
echo "=================================================="
echo "Region: $REGION"
echo ""

cd "$PROJECT_ROOT/backend"
uv run python "$SCRIPT_DIR/deploy_prompts.py" --region "$REGION" --env-file "$PROJECT_ROOT/backend/.env.local"

echo ""
echo "✅ Bedrock Managed Prompts deployed"
