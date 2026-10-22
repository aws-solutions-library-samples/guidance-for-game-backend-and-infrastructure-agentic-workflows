#!/bin/bash
set -euo pipefail

# Emit the role model exports the runtime supports, and report any Game Agent
# application inference profiles found for the current model generation.
#
# Until the capability-aware model factory (#421), the runtime cannot tell which
# model an application inference profile wraps, so it sends the earlier-
# generation request shape (with temperature) that Claude Haiku 5.5 and Claude
# Sonnet 5.5 reject. This script therefore always exports the resolved role
# model IDs and only reports the application profiles on stderr.
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

if [ -n "$ORCHESTRATOR_ID" ] || [ -n "$SPECIALIST_ID" ]; then
    echo "Found Game Agent application inference profiles in $REGION, but they are not supported as runtime model IDs until #421: the runtime would send temperature, which the Claude 5.5 models reject. Exporting the resolved model IDs instead." >&2
else
    echo "No Game Agent application inference profiles found in $REGION; exporting the resolved model IDs." >&2
fi

printf "export GBAW_ORCHESTRATOR_MODEL_ID='%s'\n" "$GBAW_ORCHESTRATOR_MODEL_ID"
printf "export GBAW_SPECIALIST_MODEL_ID='%s'\n" "$GBAW_SPECIALIST_MODEL_ID"
