#!/bin/bash
set -e

REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="game-agent"
STACK_NAME="${PROJECT_NAME}-kb-gamelift"

# Resolve AWS profile from environment or ui/.env.local (matches scripts/deploy.sh).
# An explicitly set AWS_PROFILE always wins; otherwise fall back to ui/.env.local.
# (Inherited automatically when invoked by deploy.sh; needed for standalone runs.)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
if [ -z "${AWS_PROFILE:-}" ] && [ -f "$PROJECT_ROOT/ui/.env.local" ]; then
    _profile=$(grep '^AWS_PROFILE=' "$PROJECT_ROOT/ui/.env.local" | cut -d= -f2 | tr -d '[:space:]')
    [ -n "$_profile" ] && export AWS_PROFILE="$_profile"
fi

echo "=================================================="
echo "🎮 Seeding GameLift Knowledge Base"
echo "=================================================="

# Check if stack exists
if ! aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" &>/dev/null; then
  echo "⚠️  Stack $STACK_NAME not found, skipping seeding"
  exit 0
fi

# Get stack outputs
KB_ID=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`KnowledgeBaseId`].OutputValue' \
  --output text 2>/dev/null || echo "")

DATASOURCE_ID=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`DataSourceId`].OutputValue' \
  --output text 2>/dev/null | cut -d'|' -f2)

BUCKET_NAME=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`DocumentBucketName`].OutputValue' \
  --output text 2>/dev/null || echo "")

# Validate outputs
if [ -z "$KB_ID" ] || [ -z "$DATASOURCE_ID" ] || [ -z "$BUCKET_NAME" ]; then
  echo "❌ Failed to get stack outputs"
  echo "   KB ID: ${KB_ID:-missing}"
  echo "   DataSource ID: ${DATASOURCE_ID:-missing}"
  echo "   Bucket: ${BUCKET_NAME:-missing}"
  exit 1
fi

echo "Knowledge Base ID: $KB_ID"
echo "Data Source ID: $DATASOURCE_ID"
echo "Document Bucket: $BUCKET_NAME"
echo ""

# Check if docs directory exists
DOCS_DIR="$(cd "$(dirname "$0")/../../docs/kb-sources/gamelift" && pwd)"
if [ ! -d "$DOCS_DIR" ]; then
  echo "❌ Documentation directory not found: $DOCS_DIR"
  echo "   Run: ./scripts/infrastructure/download-kb-docs.sh"
  exit 1
fi

# Check if docs directory has content
if [ -z "$(ls -A "$DOCS_DIR"/*.md 2>/dev/null)" ]; then
  echo "❌ No markdown files found in: $DOCS_DIR"
  echo "   Run: ./scripts/infrastructure/download-kb-docs.sh"
  exit 1
fi

# Upload documentation
echo "📤 Uploading GameLift documentation..."
if ! aws s3 sync "$DOCS_DIR" "s3://$BUCKET_NAME/gamelift/" \
  --region "$REGION" \
  --exclude ".*"; then
  echo "❌ Failed to upload documents to S3"
  exit 1
fi

echo "✅ Documents uploaded"

# Start ingestion job
echo "🔄 Starting ingestion job..."
INGESTION_JOB=$(aws bedrock-agent start-ingestion-job \
  --knowledge-base-id "$KB_ID" \
  --data-source-id "$DATASOURCE_ID" \
  --description "GameLift KB seeding" \
  --region "$REGION" \
  --query 'ingestionJob.ingestionJobId' \
  --output text 2>/dev/null || echo "")

if [ -z "$INGESTION_JOB" ]; then
  echo "❌ Failed to start ingestion job"
  exit 1
fi

echo "Ingestion Job ID: $INGESTION_JOB"

# Wait for ingestion
echo "⏳ Waiting for ingestion to complete..."
MAX_WAIT=120
ELAPSED=0
while [ $ELAPSED -lt $MAX_WAIT ]; do
  STATUS=$(aws bedrock-agent get-ingestion-job \
    --knowledge-base-id "$KB_ID" \
    --data-source-id "$DATASOURCE_ID" \
    --ingestion-job-id "$INGESTION_JOB" \
    --region "$REGION" \
    --query 'ingestionJob.status' \
    --output text 2>/dev/null || echo "UNKNOWN")

  if [ "$STATUS" = "COMPLETE" ]; then
    echo "✅ GameLift KB ingestion complete!"
    aws bedrock-agent get-ingestion-job \
      --knowledge-base-id "$KB_ID" \
      --data-source-id "$DATASOURCE_ID" \
      --ingestion-job-id "$INGESTION_JOB" \
      --region "$REGION" \
      --query 'ingestionJob.statistics' \
      --output table 2>/dev/null || true
    exit 0
  elif [ "$STATUS" = "FAILED" ]; then
    echo "❌ Ingestion failed!"
    aws bedrock-agent get-ingestion-job \
      --knowledge-base-id "$KB_ID" \
      --data-source-id "$DATASOURCE_ID" \
      --ingestion-job-id "$INGESTION_JOB" \
      --region "$REGION" \
      --query 'ingestionJob.failureReasons' \
      --output text 2>/dev/null || echo "Unknown failure reason"
    exit 1
  elif [ "$STATUS" = "UNKNOWN" ]; then
    echo "❌ Failed to check ingestion status"
    exit 1
  fi

  echo "Status: $STATUS (${ELAPSED}s elapsed)"
  sleep 5
  ELAPSED=$((ELAPSED + 5))
done

echo "⚠️  Ingestion still in progress after ${MAX_WAIT}s"
echo "   Check status manually:"
echo "   aws bedrock-agent get-ingestion-job --knowledge-base-id $KB_ID --data-source-id $DATASOURCE_ID --ingestion-job-id $INGESTION_JOB --region $REGION"
exit 0
