#!/bin/bash
set -euo pipefail

# Emit canonical role model exports, preferring Game Agent application profiles.
REGION=${1:-us-west-2}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if ! MODEL_EXPORTS=$(uv run --directory "$PROJECT_ROOT/backend" python \
    "$PROJECT_ROOT/config/load_deployment_settings.py" --models-only); then
    echo "Unable to resolve canonical role models" >&2
    exit 1
fi
eval "$MODEL_EXPORTS"

ORCHESTRATOR_ID=$(aws bedrock list-inference-profiles --region "$REGION" --type-equals APPLICATION \
    --query "inferenceProfileSummaries[?inferenceProfileName=='GameAgent-Orchestrator-Claude-Haiku-5-5'].inferenceProfileId" \
    --output text)

SPECIALIST_ID=$(aws bedrock list-inference-profiles --region "$REGION" --type-equals APPLICATION \
    --query "inferenceProfileSummaries[?inferenceProfileName=='GameAgent-Specialist-Claude-Sonnet-5-5'].inferenceProfileId" \
    --output text)

if [ -n "$ORCHESTRATOR_ID" ]; then
    printf "export GBAW_ORCHESTRATOR_MODEL_ID='%s'\n" "$ORCHESTRATOR_ID"
else
    echo "No 'GameAgent-Orchestrator-Claude-Haiku-5-5' application inference profile found in $REGION; falling back to the resolved model ID '$GBAW_ORCHESTRATOR_MODEL_ID'. Run 'manage-inference-profile.sh create' to create the application profiles for cost attribution, and delete any previous-generation profiles manually." >&2
    printf "export GBAW_ORCHESTRATOR_MODEL_ID='%s'\n" "$GBAW_ORCHESTRATOR_MODEL_ID"
fi

if [ -n "$SPECIALIST_ID" ]; then
    printf "export GBAW_SPECIALIST_MODEL_ID='%s'\n" "$SPECIALIST_ID"
else
    echo "No 'GameAgent-Specialist-Claude-Sonnet-5-5' application inference profile found in $REGION; falling back to the resolved model ID '$GBAW_SPECIALIST_MODEL_ID'. Run 'manage-inference-profile.sh create' to create the application profiles for cost attribution, and delete any previous-generation profiles manually." >&2
    printf "export GBAW_SPECIALIST_MODEL_ID='%s'\n" "$GBAW_SPECIALIST_MODEL_ID"
fi
