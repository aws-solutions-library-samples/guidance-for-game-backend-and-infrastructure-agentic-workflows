#!/bin/bash

# Game Agent - Knowledge Base Teardown
# Tears down all 3 knowledge bases

REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="game-agent"

echo "=================================================="
echo " 🧠 Tearing down Knowledge Base Stacks"
echo "=================================================="
echo "Region: $REGION"
echo ""

# Function to delete KB stack
delete_kb_stack() {
  local KB_NAME=$1
  local STACK_NAME="${PROJECT_NAME}-kb-${KB_NAME}"

  # Check if stack exists
  if ! aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" &>/dev/null; then
    echo "⚠️  Stack $STACK_NAME does not exist, skipping"
    return 0
  fi

  # Empty document bucket before deleting stack (required for non-empty buckets)
  echo "🗑️  Emptying $KB_NAME document bucket..."
  BUCKET_NAME=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query 'Stacks[0].Outputs[?OutputKey==`DocumentBucketName`].OutputValue' \
    --output text 2>/dev/null || echo "")

  if [ -n "$BUCKET_NAME" ]; then
    # Check if bucket exists before trying to empty it
    if aws s3api head-bucket --bucket "$BUCKET_NAME" --region "$REGION" 2>/dev/null; then
      aws s3 rm "s3://$BUCKET_NAME" --recursive --region "$REGION" 2>/dev/null || true
      echo "✅ Bucket emptied"
    else
      echo "⚠️  Bucket $BUCKET_NAME doesn't exist (already deleted)"
    fi
  fi

  echo "🗑️  Deleting $KB_NAME KB stack..."
  aws cloudformation delete-stack \
    --stack-name "$STACK_NAME" \
    --region "$REGION"

  echo "⏳ Waiting for $KB_NAME KB deletion (this may take a minute)..."
  if aws cloudformation wait stack-delete-complete \
    --stack-name "$STACK_NAME" \
    --region "$REGION" 2>&1; then
    echo "✅ $KB_NAME KB torn down"
  else
    # Verify it's actually gone
    if aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" &>/dev/null; then
      echo "❌ $KB_NAME KB stack deletion failed - stack still exists!"
      return 1
    else
      echo "✅ $KB_NAME KB torn down (verified)"
    fi
  fi
}

# Delete all KB stacks
delete_kb_stack "gamelift"
delete_kb_stack "eks"
delete_kb_stack "cost"

# Clean up .env.local
ENV_FILE="$(cd "$(dirname "$0")/../../backend" && pwd)/.env.local"
if [ -f "$ENV_FILE" ]; then
  sed -i.bak '/^GAMELIFT_KB_ID=/d' "$ENV_FILE" 2>/dev/null || true
  sed -i.bak '/^EKS_KB_ID=/d' "$ENV_FILE" 2>/dev/null || true
  sed -i.bak '/^COST_KB_ID=/d' "$ENV_FILE" 2>/dev/null || true
  sed -i.bak '/^KNOWLEDGE_BASE_ID=/d' "$ENV_FILE" 2>/dev/null || true
  rm -f "$ENV_FILE.bak"
  echo "✅ Cleaned up $ENV_FILE"
fi

echo ""
echo "✅ All Knowledge Bases torn down!"
