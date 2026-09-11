#!/bin/bash
# Resolve and validate deployed test targets. Intended to be sourced.

resolve_game_agent_deployment_targets() {
    local script_dir project_root frontend_host runtime_id runtime_arn env_file
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    project_root="$(cd "$script_dir/../.." && pwd)"
    env_file="${GBAW_ENV_FILE:-$project_root/ui/.env.local}"

    if [ -z "${AWS_PROFILE:-}" ] && [ -f "$env_file" ]; then
        AWS_PROFILE=$(grep -E '^AWS_PROFILE=' "$env_file" | cut -d= -f2 | tr -d '[:space:]')
    fi
    export AWS_PROFILE
    AWS_REGION="${AWS_REGION:-us-west-2}"
    export AWS_REGION

    unset FRONTEND_URL RUNTIME_ID RUNTIME_ARN AGENTCORE_RUNTIME_ID AGENTCORE_RUNTIME_ARN

    if ! "$project_root/scripts/infrastructure/check-deployment.sh" check >/dev/null 2>&1; then
        return 1
    fi

    frontend_host=$(aws cloudformation describe-stacks \
        --stack-name game-agent-frontend \
        --region "$AWS_REGION" \
        --query 'Stacks[0].Outputs[?OutputKey==`ServiceUrl`].OutputValue' \
        --output text) || return 1
    runtime_id=$(yq eval '.agents.gameagentruntime.bedrock_agentcore.agent_id' \
        "$project_root/backend/.bedrock_agentcore.yaml") || return 1
    runtime_arn=$(yq eval '.agents.gameagentruntime.bedrock_agentcore.agent_arn' \
        "$project_root/backend/.bedrock_agentcore.yaml") || return 1

    if [[ ! "$frontend_host" =~ ^[A-Za-z0-9-]+\.ecs\.${AWS_REGION}\.on\.aws$ ]]; then
        echo "❌ Refusing unrecognized frontend target: $frontend_host" >&2
        return 1
    fi
    if [[ ! "$runtime_id" =~ ^[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10}$ ]]; then
        echo "❌ Refusing invalid AgentCore runtime ID" >&2
        return 1
    fi
    if [[ ! "$runtime_arn" =~ ^arn:aws[a-zA-Z-]*:bedrock-agentcore:${AWS_REGION}:[0-9]{12}:runtime/${runtime_id}$ ]]; then
        echo "❌ Refusing invalid AgentCore runtime ARN" >&2
        return 1
    fi

    FRONTEND_URL="https://$frontend_host"
    RUNTIME_ID="$runtime_id"
    RUNTIME_ARN="$runtime_arn"
    AGENTCORE_RUNTIME_ID="$runtime_id"
    AGENTCORE_RUNTIME_ARN="$runtime_arn"
    export FRONTEND_URL RUNTIME_ID RUNTIME_ARN AGENTCORE_RUNTIME_ID AGENTCORE_RUNTIME_ARN
}

resolve_game_agent_deployment_targets
