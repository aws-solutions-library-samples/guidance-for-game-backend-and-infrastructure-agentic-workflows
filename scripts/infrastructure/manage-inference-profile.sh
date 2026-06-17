#!/bin/bash
# Manage Bedrock Inference Profiles for Game Agent

ACTION=$1
REGION=${2:-us-west-2}

# System profiles (source for copying)
SYSTEM_SONNET_PROFILE="global.anthropic.claude-sonnet-4-5-20250929-v1:0"
SYSTEM_HAIKU_PROFILE="global.anthropic.claude-haiku-4-5-20251001-v1:0"

# Custom GameAgent profiles
GAMEAGENT_SONNET_NAME="GameAgent-Claude-Sonnet-4-5"
GAMEAGENT_HAIKU_NAME="GameAgent-Claude-Haiku-4-5"

get_system_profile_arn() {
    aws bedrock list-inference-profiles --region $REGION \
        --query "inferenceProfileSummaries[?inferenceProfileId=='$1'].inferenceProfileArn" \
        --output text
}

get_app_profile_id() {
    aws bedrock list-inference-profiles --region $REGION --type-equals APPLICATION \
        --query "inferenceProfileSummaries[?inferenceProfileName=='$1'].inferenceProfileId" \
        --output text
}

check_profile_exists() {
    local profile_id=$(get_app_profile_id "$1")
    [ -n "$profile_id" ]
}

create_custom_profile() {
    local profile_name=$1
    local description=$2
    local source_profile_id=$3

    echo "Creating custom profile: $profile_name"

    SOURCE_ARN=$(get_system_profile_arn "$source_profile_id")
    if [ -z "$SOURCE_ARN" ]; then
        echo "❌ Source profile not found: $source_profile_id"
        return 1
    fi

    aws bedrock create-inference-profile \
        --region $REGION \
        --inference-profile-name "$profile_name" \
        --description "$description" \
        --model-source copyFrom="$SOURCE_ARN" \
        --tags key=Project,value=GameAgent key=ManagedBy,value=CloudFormation

    if [ $? -eq 0 ]; then
        echo "✅ Created: $profile_name"
    else
        echo "❌ Failed to create: $profile_name"
        return 1
    fi
}

delete_custom_profile() {
    local profile_name=$1
    local profile_id=$(get_app_profile_id "$profile_name")

    if [ -z "$profile_id" ]; then
        echo "ℹ️  Profile not found: $profile_name"
        return 0
    fi

    echo "Deleting custom profile: $profile_name"
    aws bedrock delete-inference-profile \
        --region $REGION \
        --inference-profile-identifier "$profile_id"

    if [ $? -eq 0 ]; then
        echo "✅ Deleted: $profile_name"
    else
        echo "❌ Failed to delete: $profile_name"
        return 1
    fi
}

case $ACTION in
  create)
    echo "🔧 Creating GameAgent custom inference profiles..."
    echo ""

    if check_profile_exists "$GAMEAGENT_SONNET_NAME"; then
        echo "✅ $GAMEAGENT_SONNET_NAME already exists"
    else
        create_custom_profile \
            "$GAMEAGENT_SONNET_NAME" \
            "Game Agent application profile for Claude Sonnet 4.5" \
            "$SYSTEM_SONNET_PROFILE"
    fi

    echo ""

    if check_profile_exists "$GAMEAGENT_HAIKU_NAME"; then
        echo "✅ $GAMEAGENT_HAIKU_NAME already exists"
    else
        create_custom_profile \
            "$GAMEAGENT_HAIKU_NAME" \
            "Game Agent application profile for Claude Haiku 4.5" \
            "$SYSTEM_HAIKU_PROFILE"
    fi

    echo ""
    echo "✅ GameAgent inference profiles ready"
    ;;

  delete)
    echo "🗑️  Deleting GameAgent custom inference profiles..."
    echo ""

    delete_custom_profile "$GAMEAGENT_SONNET_NAME"
    echo ""
    delete_custom_profile "$GAMEAGENT_HAIKU_NAME"

    echo ""
    echo "✅ Cleanup complete"
    ;;

  check)
    echo "🔍 Checking GameAgent inference profiles..."
    echo ""

    sonnet_exists=0
    haiku_exists=0

    if check_profile_exists "$GAMEAGENT_SONNET_NAME"; then
        SONNET_ID=$(get_app_profile_id "$GAMEAGENT_SONNET_NAME")
        echo "✅ $GAMEAGENT_SONNET_NAME exists (ID: $SONNET_ID)"
        sonnet_exists=1
    else
        echo "❌ $GAMEAGENT_SONNET_NAME not found"
    fi

    echo ""

    if check_profile_exists "$GAMEAGENT_HAIKU_NAME"; then
        HAIKU_ID=$(get_app_profile_id "$GAMEAGENT_HAIKU_NAME")
        echo "✅ $GAMEAGENT_HAIKU_NAME exists (ID: $HAIKU_ID)"
        haiku_exists=1
    else
        echo "❌ $GAMEAGENT_HAIKU_NAME not found"
    fi

    [ $sonnet_exists -eq 1 ] && [ $haiku_exists -eq 1 ]
    ;;

  *)
    echo "Usage: $0 {create|delete|check} [region]"
    echo ""
    echo "Manages GameAgent custom inference profiles:"
    echo "  - $GAMEAGENT_SONNET_NAME (wraps $SYSTEM_SONNET_PROFILE)"
    echo "  - $GAMEAGENT_HAIKU_NAME (wraps $SYSTEM_HAIKU_PROFILE)"
    exit 1
    ;;
esac
