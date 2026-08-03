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
    --query "inferenceProfileSummaries[?inferenceProfileName=='GameAgent-Orchestrator-Claude-Haiku-4-5'].inferenceProfileId" \
    --output text)

SPECIALIST_ID=$(aws bedrock list-inference-profiles --region "$REGION" --type-equals APPLICATION \
    --query "inferenceProfileSummaries[?inferenceProfileName=='GameAgent-Specialist-Claude-Sonnet-4-6'].inferenceProfileId" \
    --output text)

if [ -n "$ORCHESTRATOR_ID" ]; then
    printf "export GBAW_ORCHESTRATOR_MODEL_ID='%s'\n" "$ORCHESTRATOR_ID"
else
    printf "export GBAW_ORCHESTRATOR_MODEL_ID='%s'\n" "$GBAW_ORCHESTRATOR_MODEL_ID"
fi

if [ -n "$SPECIALIST_ID" ]; then
    printf "export GBAW_SPECIALIST_MODEL_ID='%s'\n" "$SPECIALIST_ID"
else
    printf "export GBAW_SPECIALIST_MODEL_ID='%s'\n" "$GBAW_SPECIALIST_MODEL_ID"
fi
