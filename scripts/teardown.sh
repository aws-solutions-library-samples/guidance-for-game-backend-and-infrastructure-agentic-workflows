#!/bin/bash
# Game Agent - Complete Teardown

# Change to project root directory
cd "$(dirname "$0")/.."

set -e

PROJECT_NAME="game-agent"

# Read AWS_PROFILE from ui/.env.local if not already set
if [ -z "$AWS_PROFILE" ] && [ -f "ui/.env.local" ]; then
    _profile=$(grep '^AWS_PROFILE=' "ui/.env.local" | cut -d= -f2 | tr -d '[:space:]')
    [ -n "$_profile" ] && export AWS_PROFILE="$_profile"
fi

echo "🗑️  Game Agent - Complete Teardown"
echo "========================================="

# Prerequisite checks
if ! command -v jq &> /dev/null; then
    echo "⚠️  jq not found, attempting to install..."
    if [[ "$OSTYPE" == "darwin"* ]] && command -v brew &> /dev/null; then
        brew install jq
    elif command -v apt-get &> /dev/null; then
        sudo apt-get install -y jq
    else
        echo "❌ Could not auto-install jq. Install manually: https://jqlang.github.io/jq/download/"
        exit 1
    fi
    echo "   ✅ jq installed"
fi

AWS_REGION=$(aws configure get region || echo "us-west-2")
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "📋 Tearing down from AWS Account: $AWS_ACCOUNT_ID"
echo "📋 Region: $AWS_REGION"
echo ""

# Check for --yes flag
AUTO_CONFIRM=false
if [ "$1" == "--yes" ] || [ "$1" == "-y" ]; then
    AUTO_CONFIRM=true
    echo "⚠️  WARNING: Auto-confirm enabled - proceeding without prompts"
fi

if [ "$AUTO_CONFIRM" = false ]; then
    echo "⚠️  WARNING: This will delete all Game Agent resources!"
    read -p "Are you sure? (yes/no): " confirm
    if [ "$confirm" != "yes" ]; then
        echo "❌ Teardown cancelled"
        exit 0
    fi
fi

# Step 0: Delete security infrastructure first (WAF is attached to frontend ALB)
echo ""
echo "🗑️  Step 0: Deleting security infrastructure (WAF + CloudTrail)..."
if aws cloudformation describe-stacks --stack-name ${PROJECT_NAME}-security --region $AWS_REGION &>/dev/null; then
  # Stop CloudTrail logging before deletion
  aws cloudtrail stop-logging --name ${PROJECT_NAME}-trail --region $AWS_REGION 2>/dev/null || true

  # Empty the CloudTrail logs bucket (versioned bucket requires deleting all versions)
  CLOUDTRAIL_BUCKET="${PROJECT_NAME}-cloudtrail-logs-${AWS_ACCOUNT_ID}-${AWS_REGION}"
  if aws s3api head-bucket --bucket "$CLOUDTRAIL_BUCKET" 2>/dev/null; then
    echo "   Emptying CloudTrail logs bucket (versioned)..."
    # Use batch delete for efficiency and reliability
    while true; do
      # Get versions and delete markers
      VERSIONS=$(aws s3api list-object-versions --bucket "$CLOUDTRAIL_BUCKET" --max-keys 1000 --output json 2>/dev/null)

      # Build delete request JSON for batch deletion (versions + delete markers)
      DELETE_JSON=$(echo "$VERSIONS" | jq -c '{Objects: [(.Versions // [])[], (.DeleteMarkers // [])[]] | map({Key: .Key, VersionId: .VersionId})}' 2>/dev/null)

      # Check if there are objects to delete
      OBJECT_COUNT=$(echo "$DELETE_JSON" | jq '.Objects | length' 2>/dev/null)
      if [ -z "$OBJECT_COUNT" ] || [ "$OBJECT_COUNT" = "0" ]; then
        echo "   ✅ Bucket emptied"
        break
      fi

      # Batch delete using temp file (more reliable than stdin)
      echo "   Deleting $OBJECT_COUNT object versions..."
      DELETE_FILE=$(mktemp)
      echo "$DELETE_JSON" > "$DELETE_FILE"
      aws s3api delete-objects --bucket "$CLOUDTRAIL_BUCKET" --delete "file://${DELETE_FILE}" --region $AWS_REGION >/dev/null 2>&1
      rm -f "$DELETE_FILE"

      # Check if there are more objects (pagination)
      HAS_MORE=$(echo "$VERSIONS" | jq -r '.IsTruncated // false')
      if [ "$HAS_MORE" != "true" ]; then
        # Verify bucket is actually empty before declaring success
        REMAINING=$(aws s3api list-object-versions --bucket "$CLOUDTRAIL_BUCKET" --max-keys 1 --output json 2>/dev/null | jq '[(.Versions // []), (.DeleteMarkers // [])] | add | length')
        if [ "$REMAINING" = "0" ] || [ -z "$REMAINING" ]; then
          echo "   ✅ Bucket emptied"
          break
        fi
        # If objects remain, continue loop
      fi
    done
  fi

  aws cloudformation delete-stack --stack-name ${PROJECT_NAME}-security --region $AWS_REGION
  echo "⏳ Waiting for security stack deletion..."
  aws cloudformation wait stack-delete-complete --stack-name ${PROJECT_NAME}-security --region $AWS_REGION
  echo "✅ Security infrastructure deleted"
else
  echo "⚠️  Security stack not found"
fi

# Step 1: Delete observability CloudFormation stack
echo ""
echo "🗑️  Step 1: Deleting observability stack..."
if aws cloudformation describe-stacks --stack-name ${PROJECT_NAME}-observability --region $AWS_REGION &>/dev/null; then
  aws cloudformation delete-stack --stack-name ${PROJECT_NAME}-observability --region $AWS_REGION
  echo "⏳ Waiting for observability stack deletion..."
  aws cloudformation wait stack-delete-complete --stack-name ${PROJECT_NAME}-observability --region $AWS_REGION
  echo "✅ Observability stack deleted"
else
  echo "⚠️  Observability stack not found"
fi

# Step 1.5: Delete Knowledge Bases
echo ""
echo "🗑️  Step 1.5: Deleting Knowledge Bases..."
bash scripts/infrastructure/teardown-kb.sh --yes

# Step 1.6: Delete Bedrock Managed Prompts
echo ""
echo "🗑️  Step 1.6: Deleting Bedrock Managed Prompts..."
bash scripts/infrastructure/teardown-prompts.sh

# Step 2: Delete frontend CloudFormation stack
echo ""
echo "🗑️  Step 2: Deleting frontend..."
if aws cloudformation describe-stacks --stack-name ${PROJECT_NAME}-frontend --region $AWS_REGION &>/dev/null; then
  aws cloudformation delete-stack --stack-name ${PROJECT_NAME}-frontend --region $AWS_REGION
  echo "⏳ Waiting for frontend stack deletion..."
  aws cloudformation wait stack-delete-complete --stack-name ${PROJECT_NAME}-frontend --region $AWS_REGION
  echo "✅ Frontend deleted"
else
  echo "⚠️  Frontend stack not found"
fi

# Step 3: Clean up legacy EKS MCP infrastructure (if any)
echo ""
echo "🗑️  Step 3: Cleaning up legacy EKS MCP infrastructure..."

# Step 3.5: Delete Bedrock Guardrails
echo ""
echo "🗑️  Step 3.5: Deleting Bedrock Guardrails..."
if aws cloudformation describe-stacks --stack-name ${PROJECT_NAME}-guardrails --region $AWS_REGION &>/dev/null; then
  aws cloudformation delete-stack --stack-name ${PROJECT_NAME}-guardrails --region $AWS_REGION
  echo "⏳ Waiting for guardrails stack deletion..."
  aws cloudformation wait stack-delete-complete --stack-name ${PROJECT_NAME}-guardrails --region $AWS_REGION
  echo "✅ Guardrails deleted"
else
  echo "⚠️  Guardrails stack not found"
fi

# Step 4: Delete AgentCore Runtime and Memory
echo ""
echo "🗑️  Step 4: Deleting AgentCore Runtime and Memory..."
cd backend
if [ -f .bedrock_agentcore.yaml ]; then
    # Extract runtime ID and memory ID before destroying
    RUNTIME_ARN=$(yq eval '.agents.gameagentruntime.bedrock_agentcore.agent_arn' .bedrock_agentcore.yaml 2>/dev/null)
    RUNTIME_ID=$(echo "$RUNTIME_ARN" | awk -F'/' '{print $NF}')
    MEMORY_ID=$(yq eval '.agents.gameagentruntime.memory.memory_id' .bedrock_agentcore.yaml 2>/dev/null)

    # Delete Memory FIRST (before runtime)
    if [ -n "$MEMORY_ID" ] && [ "$MEMORY_ID" != "null" ]; then
        echo "🗑️  Deleting AgentCore Memory: $MEMORY_ID"
        aws bedrock-agentcore-control delete-memory \
            --memory-id "$MEMORY_ID" \
            --region $AWS_REGION 2>/dev/null && echo "✅ Memory deleted" || echo "⚠️  Memory already deleted or not found"
    else
        echo "⚠️  No memory ID found in configuration"
    fi

    # Delete AgentCore Runtime (including ECR repository)
    echo "🗑️  Deleting AgentCore Runtime: $RUNTIME_ID"
    uv run agentcore destroy --force --delete-ecr-repo || echo "⚠️  Runtime already deleted or not found"

    # CRITICAL: Wait for runtime deletion to complete
    if [ -n "$RUNTIME_ID" ] && [ "$RUNTIME_ID" != "null" ]; then
        echo "⏳ Waiting for AgentCore Runtime deletion to complete..."
        MAX_WAIT=300  # 5 minutes max
        ELAPSED=0
        while [ $ELAPSED -lt $MAX_WAIT ]; do
            if aws bedrock-agentcore-control get-agent-runtime \
                --agent-runtime-id "$RUNTIME_ID" \
                --region $AWS_REGION &>/dev/null; then
                echo "   Still deleting... (${ELAPSED}s elapsed)"
                sleep 10
                ELAPSED=$((ELAPSED + 10))
            else
                echo "✅ AgentCore Runtime deleted"
                break
            fi
        done

        if [ $ELAPSED -ge $MAX_WAIT ]; then
            echo "⚠️  Warning: Runtime deletion timeout after ${MAX_WAIT}s"
            echo "   Continuing teardown, but manual cleanup may be needed"
        fi
    fi

    rm -f .bedrock_agentcore.yaml
else
    echo "⚠️  No runtime configuration found"
fi

# Fallback: Clean up any orphaned gameagent memories/runtimes by querying AWS directly
echo ""
echo "🔍 Checking for orphaned AgentCore resources..."

# Clean up orphaned runtimes
ORPHAN_RUNTIMES=$(aws bedrock-agentcore-control list-agent-runtimes \
    --region $AWS_REGION \
    --query 'agentRuntimes[?starts_with(agentRuntimeName, `gameagent`)].agentRuntimeId' \
    --output text 2>/dev/null)

if [ -n "$ORPHAN_RUNTIMES" ]; then
    for RUNTIME_ID in $ORPHAN_RUNTIMES; do
        echo "🗑️  Deleting orphaned runtime: $RUNTIME_ID"
        aws bedrock-agentcore-control delete-agent-runtime \
            --agent-runtime-id "$RUNTIME_ID" \
            --region $AWS_REGION 2>/dev/null && echo "✅ Runtime deleted" || echo "⚠️  Could not delete runtime"
    done
fi

# Clean up orphaned memories
ORPHAN_MEMORIES=$(aws bedrock-agentcore-control list-memories \
    --region $AWS_REGION \
    --query 'memories[?starts_with(id, `gameagent`)].id' \
    --output text 2>/dev/null)

if [ -n "$ORPHAN_MEMORIES" ]; then
    for MEMORY_ID in $ORPHAN_MEMORIES; do
        echo "🗑️  Deleting orphaned memory: $MEMORY_ID"
        aws bedrock-agentcore-control delete-memory \
            --memory-id "$MEMORY_ID" \
            --region $AWS_REGION 2>/dev/null && echo "✅ Memory deleted" || echo "⚠️  Could not delete memory"
    done
else
    echo "✅ No orphaned memories found"
fi

cd ..

# Step 5: Delete base infrastructure (ECR repos auto-deleted via EmptyOnDelete)
echo ""
echo "🗑️  Step 5: Deleting base infrastructure..."

# Step 5a: Delete access logs bucket (has DeletionPolicy: Retain in CloudFormation)
ACCESS_LOGS_BUCKET="${PROJECT_NAME}-access-logs-${AWS_ACCOUNT_ID}-${AWS_REGION}"
if aws s3api head-bucket --bucket "$ACCESS_LOGS_BUCKET" 2>/dev/null; then
    echo "🗑️  Deleting access logs bucket: $ACCESS_LOGS_BUCKET"
    aws s3 rb "s3://${ACCESS_LOGS_BUCKET}" --force --region $AWS_REGION 2>/dev/null && \
        echo "✅ Access logs bucket deleted" || \
        echo "⚠️  Could not delete access logs bucket (may be already empty/deleted)"
else
    echo "ℹ️  Access logs bucket not found"
fi

# Step 5b: Delete CloudFormation stack
if aws cloudformation describe-stacks --stack-name ${PROJECT_NAME}-infrastructure --region $AWS_REGION &>/dev/null; then
  aws cloudformation delete-stack --stack-name ${PROJECT_NAME}-infrastructure --region $AWS_REGION
  echo "⏳ Waiting for base infrastructure deletion..."
  aws cloudformation wait stack-delete-complete --stack-name ${PROJECT_NAME}-infrastructure --region $AWS_REGION
  echo "✅ Base infrastructure deleted"
else
  echo "⚠️  Infrastructure stack not found"
fi

# Step 6: Clean up downloaded KB documentation
echo ""
echo "🗑️  Step 6: Cleaning up downloaded documentation..."
if [ -d "docs/kb-sources" ]; then
    rm -rf docs/kb-sources/*/*.md
    echo "✅ Removed downloaded markdown files"
fi
if [ -d "docs/.kb-cache" ]; then
    rm -rf docs/.kb-cache
    echo "✅ Removed scraper cache"
fi

echo ""
echo "========================================="
echo "🎉 Game Agent teardown completed successfully!"
echo "   All AWS resources have been cleaned up."
echo ""

# IMPORTANT: Preserve account-wide observability resources
echo ""
echo "========================================="
echo "ℹ️  ACCOUNT-WIDE RESOURCES PRESERVED"
echo "========================================="
echo ""
echo "The following account-wide observability resources were NOT deleted:"
echo "  - /aws/spans (CloudWatch Transaction Search log group)"
echo "  - /aws/spans/default (CloudWatch Transaction Search log group)"
echo "  - CloudWatch Transaction Search configuration"
echo "  - X-Ray resource policies"
echo ""
echo "These are shared resources that may be used by other applications."
echo "They will continue to function for other deployments in this account."
echo ""
echo "✅ Teardown complete (application-specific resources deleted)"
