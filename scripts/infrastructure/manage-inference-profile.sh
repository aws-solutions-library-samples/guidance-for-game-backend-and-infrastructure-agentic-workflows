#!/bin/bash
set -euo pipefail

# Manage role-based Bedrock application inference profiles for Game Agent.
ACTION=${1:-}
REGION=${2:-us-west-2}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Profile creation always wraps the canonical system defaults. Runtime model
# overrides are handled separately by the role environment variables.
if ! MODEL_EXPORTS=$(uv run --directory "$PROJECT_ROOT/backend" python \
    "$PROJECT_ROOT/config/load_deployment_settings.py" --default-models-only); then
    echo "Unable to resolve canonical model defaults" >&2
    exit 1
fi
eval "$MODEL_EXPORTS"
SYSTEM_ORCHESTRATOR_PROFILE="$GBAW_ORCHESTRATOR_MODEL_ID"
SYSTEM_SPECIALIST_PROFILE="$GBAW_SPECIALIST_MODEL_ID"

ORCHESTRATOR_PROFILE_NAME="GameAgent-Orchestrator-Claude-Haiku-4-5"
SPECIALIST_PROFILE_NAME="GameAgent-Specialist-Claude-Sonnet-4-6"

get_system_profile_arn() {
    aws bedrock list-inference-profiles --region "$REGION" \
        --query "inferenceProfileSummaries[?inferenceProfileId=='$1'].inferenceProfileArn" \
        --output text
}

get_app_profile_id() {
    aws bedrock list-inference-profiles --region "$REGION" --type-equals APPLICATION \
        --query "inferenceProfileSummaries[?inferenceProfileName=='$1'].inferenceProfileId" \
        --output text
}

check_profile_exists() {
    local profile_id
    profile_id=$(get_app_profile_id "$1")
    [ -n "$profile_id" ]
}

create_custom_profile() {
    local profile_name=$1
    local description=$2
    local source_profile_id=$3
    local source_arn

    echo "Creating application profile: $profile_name"
    source_arn=$(get_system_profile_arn "$source_profile_id")
    if [ -z "$source_arn" ]; then
        echo "Source profile not found: $source_profile_id" >&2
        return 1
    fi

    aws bedrock create-inference-profile \
        --region "$REGION" \
        --inference-profile-name "$profile_name" \
        --description "$description" \
        --model-source copyFrom="$source_arn" \
        --tags key=Project,value=GameAgent key=ManagedBy,value=GameAgentScripts
}

delete_custom_profile() {
    local profile_name=$1
    local profile_id
    profile_id=$(get_app_profile_id "$profile_name")

    if [ -z "$profile_id" ]; then
        echo "Profile not found: $profile_name"
        return 0
    fi

    echo "Deleting application profile: $profile_name"
    aws bedrock delete-inference-profile \
        --region "$REGION" \
        --inference-profile-identifier "$profile_id"
}

case "$ACTION" in
  create)
    if check_profile_exists "$ORCHESTRATOR_PROFILE_NAME"; then
        echo "$ORCHESTRATOR_PROFILE_NAME already exists"
    else
        create_custom_profile \
            "$ORCHESTRATOR_PROFILE_NAME" \
            "Game Agent orchestrator profile for Claude Haiku 4.5" \
            "$SYSTEM_ORCHESTRATOR_PROFILE"
    fi

    if check_profile_exists "$SPECIALIST_PROFILE_NAME"; then
        echo "$SPECIALIST_PROFILE_NAME already exists"
    else
        create_custom_profile \
            "$SPECIALIST_PROFILE_NAME" \
            "Game Agent specialist profile for Claude Sonnet 4.6" \
            "$SYSTEM_SPECIALIST_PROFILE"
    fi
    ;;

  delete)
    # This command removes only the current role-named profiles. Older model-
    # named profiles are intentionally left untouched for safe migration.
    delete_custom_profile "$ORCHESTRATOR_PROFILE_NAME"
    delete_custom_profile "$SPECIALIST_PROFILE_NAME"
    ;;

  check)
    missing=0
    for profile_name in "$ORCHESTRATOR_PROFILE_NAME" "$SPECIALIST_PROFILE_NAME"; do
        if check_profile_exists "$profile_name"; then
            profile_id=$(get_app_profile_id "$profile_name")
            echo "$profile_name exists (ID: $profile_id)"
        else
            echo "$profile_name not found"
            missing=1
        fi
    done
    exit "$missing"
    ;;

  *)
    echo "Usage: $0 {create|delete|check} [region]" >&2
    echo "  $ORCHESTRATOR_PROFILE_NAME wraps $SYSTEM_ORCHESTRATOR_PROFILE" >&2
    echo "  $SPECIALIST_PROFILE_NAME wraps $SYSTEM_SPECIALIST_PROFILE" >&2
    exit 1
    ;;
esac
