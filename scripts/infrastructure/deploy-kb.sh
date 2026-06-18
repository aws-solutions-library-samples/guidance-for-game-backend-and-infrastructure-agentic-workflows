#!/bin/bash
set -e

# Game Agent - Knowledge Base Deployment
# Deploys all 3 knowledge bases: GameLift, EKS, Cost

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="game-agent"

echo "=================================================="
echo " 🧠 Deploying Knowledge Base Stacks"
echo "=================================================="
echo "Region: $REGION"
echo ""

# Function to check if stack has orphaned bucket (bucket deleted but stack exists)
check_and_fix_orphaned_stack() {
  local STACK_NAME=$1

  # Check if stack exists
  if ! aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" &>/dev/null; then
    return 0  # Stack doesn't exist, nothing to fix
  fi

  # Get bucket name from stack outputs
  local BUCKET_NAME=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query 'Stacks[0].Outputs[?OutputKey==`DocumentBucketName`].OutputValue' \
    --output text 2>/dev/null || echo "")

  if [ -z "$BUCKET_NAME" ]; then
    return 0  # No bucket output, let deploy handle it
  fi

  # Check if bucket actually exists
  if ! aws s3api head-bucket --bucket "$BUCKET_NAME" --region "$REGION" 2>/dev/null; then
    echo "⚠️  Detected orphaned stack: $STACK_NAME (bucket $BUCKET_NAME was deleted)"
    echo "   Deleting orphaned stack to allow clean recreation..."
    aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"
    echo "   Waiting for stack deletion..."
    aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION"
    echo "   ✅ Orphaned stack cleaned up"
  else
    # Bucket exists - check if stack needs update by verifying resources are healthy
    # If stack is in a bad state, empty bucket and delete for clean recreation
    local STACK_STATUS=$(aws cloudformation describe-stacks \
      --stack-name "$STACK_NAME" \
      --region "$REGION" \
      --query 'Stacks[0].StackStatus' \
      --output text 2>/dev/null || echo "UNKNOWN")

    if [[ "$STACK_STATUS" == *"FAILED"* ]] || [[ "$STACK_STATUS" == *"ROLLBACK"* ]]; then
      echo "⚠️  Stack $STACK_NAME in bad state: $STACK_STATUS"
      echo "   Emptying bucket and deleting for clean recreation..."
      aws s3 rm "s3://$BUCKET_NAME" --recursive --region "$REGION" 2>/dev/null || true
      aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"
      echo "   Waiting for stack deletion..."
      aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION"
      echo "   ✅ Failed stack cleaned up"
    fi
  fi
}

# Check for orphaned stacks before deploying
echo "🔍 Checking for orphaned stacks..."
check_and_fix_orphaned_stack "${PROJECT_NAME}-kb-gamelift"
check_and_fix_orphaned_stack "${PROJECT_NAME}-kb-eks"
check_and_fix_orphaned_stack "${PROJECT_NAME}-kb-cost"
echo ""

# Deploy GameLift KB
echo "🎮 Deploying GameLift Knowledge Base..."
aws cloudformation deploy \
  --template-file "$PROJECT_ROOT/infrastructure/cloudformation/knowledge-base-gamelift.yaml" \
  --stack-name "${PROJECT_NAME}-kb-gamelift" \
  --parameter-overrides ProjectName="$PROJECT_NAME" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$REGION"

GAMELIFT_KB_ID=$(aws cloudformation describe-stacks \
  --stack-name "${PROJECT_NAME}-kb-gamelift" \
  --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`KnowledgeBaseId`].OutputValue' \
  --output text)

echo "✅ GameLift KB deployed: $GAMELIFT_KB_ID"
echo ""

# Deploy EKS KB
echo "☸️  Deploying EKS Knowledge Base..."
aws cloudformation deploy \
  --template-file "$PROJECT_ROOT/infrastructure/cloudformation/knowledge-base-eks.yaml" \
  --stack-name "${PROJECT_NAME}-kb-eks" \
  --parameter-overrides ProjectName="$PROJECT_NAME" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$REGION"

EKS_KB_ID=$(aws cloudformation describe-stacks \
  --stack-name "${PROJECT_NAME}-kb-eks" \
  --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`KnowledgeBaseId`].OutputValue' \
  --output text)

echo "✅ EKS KB deployed: $EKS_KB_ID"
echo ""

# Deploy Cost KB
echo "💰 Deploying Cost Optimization Knowledge Base..."
aws cloudformation deploy \
  --template-file "$PROJECT_ROOT/infrastructure/cloudformation/knowledge-base-cost.yaml" \
  --stack-name "${PROJECT_NAME}-kb-cost" \
  --parameter-overrides ProjectName="$PROJECT_NAME" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$REGION"

COST_KB_ID=$(aws cloudformation describe-stacks \
  --stack-name "${PROJECT_NAME}-kb-cost" \
  --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`KnowledgeBaseId`].OutputValue' \
  --output text)

echo "✅ Cost KB deployed: $COST_KB_ID"
echo ""

echo "=================================================="
echo "✅ All Knowledge Bases Deployed"
echo "=================================================="
echo "GameLift KB ID: $GAMELIFT_KB_ID"
echo "EKS KB ID: $EKS_KB_ID"
echo "Cost KB ID: $COST_KB_ID"
echo ""

# Update backend .env.local with all KB IDs
ENV_FILE="$PROJECT_ROOT/backend/.env.local"
if [ -f "$ENV_FILE" ]; then
    # Remove old KB entries (both legacy and GBAW_ prefixed)
    sed -i.bak '/^KNOWLEDGE_BASE_ID=/d' "$ENV_FILE" 2>/dev/null || true
    sed -i.bak '/^GBAW_KNOWLEDGE_BASE_ID=/d' "$ENV_FILE" 2>/dev/null || true
    sed -i.bak '/^GAMELIFT_KB_ID=/d' "$ENV_FILE" 2>/dev/null || true
    sed -i.bak '/^GBAW_GAMELIFT_KB_ID=/d' "$ENV_FILE" 2>/dev/null || true
    sed -i.bak '/^EKS_KB_ID=/d' "$ENV_FILE" 2>/dev/null || true
    sed -i.bak '/^GBAW_EKS_KB_ID=/d' "$ENV_FILE" 2>/dev/null || true
    sed -i.bak '/^COST_KB_ID=/d' "$ENV_FILE" 2>/dev/null || true
    sed -i.bak '/^GBAW_COST_KB_ID=/d' "$ENV_FILE" 2>/dev/null || true
    rm -f "$ENV_FILE.bak"
fi

# Add all KB IDs
echo "GBAW_GAMELIFT_KB_ID=$GAMELIFT_KB_ID" >> "$ENV_FILE"
echo "GBAW_EKS_KB_ID=$EKS_KB_ID" >> "$ENV_FILE"
echo "GBAW_COST_KB_ID=$COST_KB_ID" >> "$ENV_FILE"
echo "✅ Updated $ENV_FILE"
echo ""
