#!/bin/bash
set -e

# Game Agent - Bedrock Prompt Management Teardown
# Deletes all game-agent-* managed prompts and cleans up .env.local entries.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REGION="${AWS_REGION:-us-west-2}"
PREFIX="game-agent-"
ENV_FILE="$PROJECT_ROOT/backend/.env.local"

echo "=================================================="
echo " 🗑️  Tearing down Bedrock Managed Prompts"
echo "=================================================="
echo "Region: $REGION"
echo ""

# List all game-agent prompts and delete them
PROMPT_IDS=$(aws bedrock-agent list-prompts --region "$REGION" \
  --query "promptSummaries[?starts_with(name, '${PREFIX}')].id" \
  --output text 2>/dev/null || echo "")

if [ -z "$PROMPT_IDS" ]; then
  echo "⚠️  No game-agent prompts found, skipping"
else
  for PROMPT_ID in $PROMPT_IDS; do
    PROMPT_NAME=$(aws bedrock-agent get-prompt \
      --prompt-identifier "$PROMPT_ID" \
      --region "$REGION" \
      --query "name" --output text 2>/dev/null || echo "$PROMPT_ID")
    echo "🗑️  Deleting prompt: $PROMPT_NAME ($PROMPT_ID)..."
    aws bedrock-agent delete-prompt \
      --prompt-identifier "$PROMPT_ID" \
      --region "$REGION" 2>/dev/null || true
    echo "   ✅ Deleted"
  done
fi

# Clean up .env.local entries
if [ -f "$ENV_FILE" ]; then
  echo ""
  echo "🧹 Cleaning prompt ARNs from $ENV_FILE..."
  sed -i '' '/^GBAW_ORCHESTRATOR_PROMPT_ARN=/d' "$ENV_FILE" 2>/dev/null || true
  sed -i '' '/^GBAW_GAMELIFT_PROMPT_ARN=/d' "$ENV_FILE" 2>/dev/null || true
  sed -i '' '/^GBAW_EKS_PROMPT_ARN=/d' "$ENV_FILE" 2>/dev/null || true
  sed -i '' '/^GBAW_COST_PROMPT_ARN=/d' "$ENV_FILE" 2>/dev/null || true
  echo "   ✅ Cleaned"
fi

echo ""
echo "✅ Bedrock Managed Prompts torn down"
