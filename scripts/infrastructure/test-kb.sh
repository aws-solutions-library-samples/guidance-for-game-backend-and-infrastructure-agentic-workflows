#!/bin/bash
set -e

# Test Knowledge Base retrieval
# This script tests that the KB is working and returning relevant results

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="game-agent"
STACK_NAME="${PROJECT_NAME}-knowledge-base"

echo "=================================================="
echo "🧪 Testing Knowledge Base"
echo "=================================================="

# Get KB ID
KB_ID=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query 'Stacks[0].Outputs[?OutputKey==`KnowledgeBaseId`].OutputValue' \
    --output text 2>/dev/null || echo "")

if [ -z "$KB_ID" ]; then
    echo "❌ Knowledge Base not found. Deploy it first:"
    echo "   ./scripts/infrastructure/deploy-kb.sh"
    exit 1
fi

echo "Knowledge Base ID: $KB_ID"
echo ""

# Test queries
TEST_QUERIES=(
    "How to size a GameLift fleet for 1000 concurrent players?"
    "What are GameLift auto-scaling best practices?"
    "How to troubleshoot GameLift fleet capacity issues?"
)

echo "🔍 Running test queries..."
echo ""

for query in "${TEST_QUERIES[@]}"; do
    echo "Query: $query"
    echo "---"

    # Use Python to test retrieval
    python3 << EOF
import boto3
import json

client = boto3.client('bedrock-agent-runtime', region_name='$REGION')

try:
    response = client.retrieve(
        knowledgeBaseId='$KB_ID',
        retrievalQuery={'text': '$query'},
        retrievalConfiguration={
            'vectorSearchConfiguration': {
                'numberOfResults': 3
            }
        }
    )

    results = response.get('retrievalResults', [])

    if not results:
        print("⚠️  No results found (KB may still be ingesting)")
    else:
        print(f"✅ Found {len(results)} results")
        for i, result in enumerate(results, 1):
            score = result.get('score', 0)
            content = result.get('content', {}).get('text', '')[:200]
            print(f"\n  Result {i} (score: {score:.2f}):")
            print(f"  {content}...")

except Exception as e:
    print(f"❌ Error: {e}")

EOF

    echo ""
    echo "---"
    echo ""
done

echo "=================================================="
echo "✅ Knowledge Base Test Complete"
echo "=================================================="
