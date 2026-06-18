#!/bin/bash
# Get Game Agent custom inference profile IDs

REGION=${1:-us-west-2}

# Default to system profiles
SYSTEM_SONNET="global.anthropic.claude-sonnet-4-5-20250929-v1:0"
SYSTEM_HAIKU="global.anthropic.claude-haiku-4-5-20251001-v1:0"

# Try to get custom profile IDs
SONNET_ID=$(aws bedrock list-inference-profiles --region $REGION --type-equals APPLICATION \
    --query "inferenceProfileSummaries[?inferenceProfileName=='GameAgent-Claude-Sonnet-4-5'].inferenceProfileId" \
    --output text)

HAIKU_ID=$(aws bedrock list-inference-profiles --region $REGION --type-equals APPLICATION \
    --query "inferenceProfileSummaries[?inferenceProfileName=='GameAgent-Claude-Haiku-4-5'].inferenceProfileId" \
    --output text)

# Export (fallback to system profiles if custom don't exist)
if [ -n "$SONNET_ID" ]; then
    echo "export BEDROCK_MODEL_ID='$SONNET_ID'"
else
    echo "export BEDROCK_MODEL_ID='$SYSTEM_SONNET'"
fi

if [ -n "$HAIKU_ID" ]; then
    echo "export BEDROCK_MODEL_ID_SECONDARY='$HAIKU_ID'"
else
    echo "export BEDROCK_MODEL_ID_SECONDARY='$SYSTEM_HAIKU'"
fi
