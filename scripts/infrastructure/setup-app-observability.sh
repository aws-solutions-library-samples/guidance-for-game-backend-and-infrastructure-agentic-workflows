#!/bin/bash
set -e

# Application-specific observability setup (idempotent)
# Sets retention on all auto-created log groups that AWS services create without expiration

RUNTIME_ID=$1
AWS_REGION=${AWS_REGION:-us-west-2}
RETENTION_DAYS=14
PROJECT_NAME=${PROJECT_NAME:-game-agent}

# The frontend is the ECS Express service "${PROJECT_NAME}-frontend". Its
# template (02-frontend-ecs-express.yaml) sets no cluster, so the service runs in
# the "default" cluster, and ECS Express names the service's log group
# /aws/ecs/<cluster>/<service>-<generated suffix>.
FRONTEND_CLUSTER="default"
FRONTEND_SERVICE="${PROJECT_NAME}-frontend"
FRONTEND_LOG_GROUP_PREFIX="/aws/ecs/${FRONTEND_CLUSTER}/${FRONTEND_SERVICE}"

if [ -z "$RUNTIME_ID" ]; then
    echo "❌ Error: Runtime ID required"
    echo "Usage: $0 <runtime-id>"
    exit 1
fi

echo "🔍 Setting up observability for runtime: $RUNTIME_ID"

# Helper: ensure a log group exists with retention
ensure_log_group_retention() {
    local log_group="$1"
    local description="$2"
    local state found retention

    # describe-log-groups has no exact-name option, so list the groups that
    # start with the name and keep only the exact match. A failed lookup is
    # reported instead of being mistaken for a missing log group.
    if ! state=$(aws logs describe-log-groups --region "$AWS_REGION" \
        --log-group-name-prefix "$log_group" \
        --query "logGroups[?logGroupName=='${log_group}'].[logGroupName, retentionInDays]" \
        --output text 2>/dev/null); then
        echo "⚠️  $description: could not read $log_group; retention unchanged"
        return
    fi
    read -r found retention <<< "$state"

    if [ "$found" != "$log_group" ]; then
        echo "⏭️  $description: log group not found (will be created by AWS on first use)"
    elif [ "$retention" = "$RETENTION_DAYS" ]; then
        echo "✅ $description: already has ${RETENTION_DAYS}-day retention"
    else
        aws logs put-retention-policy \
            --log-group-name "$log_group" \
            --retention-in-days $RETENTION_DAYS \
            --region $AWS_REGION
        echo "✅ $description: set ${RETENTION_DAYS}-day retention"
    fi
}

# Helper: find and set retention on log groups matching a prefix
ensure_prefix_retention() {
    local prefix="$1"
    local description="$2"

    local groups
    groups=$(aws logs describe-log-groups --region $AWS_REGION \
        --log-group-name-prefix "$prefix" \
        --query 'logGroups[?!retentionInDays].logGroupName' --output text 2>/dev/null || echo "")

    if [ -z "$groups" ] || [ "$groups" = "None" ]; then
        echo "✅ $description: all log groups have retention (or none exist yet)"
        return
    fi

    for lg in $groups; do
        aws logs put-retention-policy \
            --log-group-name "$lg" \
            --retention-in-days $RETENTION_DAYS \
            --region $AWS_REGION
        echo "✅ Set ${RETENTION_DAYS}-day retention: $lg"
    done
}

# Helper: print the frontend's log group, read from the awslogs-group option of
# the service's current (PRIMARY) task definition. Fails when the service, the
# task definition, or a valid CloudWatch Logs group name cannot be read.
resolve_frontend_log_group() {
    local task_definition log_group
    local name_pattern='^[A-Za-z0-9._/#-]+$'

    task_definition=$(aws ecs describe-services --region "$AWS_REGION" \
        --cluster "$FRONTEND_CLUSTER" --services "$FRONTEND_SERVICE" \
        --query "services[?status=='ACTIVE'] | [0].deployments[?status=='PRIMARY'] | [0].taskDefinition" \
        --output text 2>/dev/null) || return 1
    if [ -z "$task_definition" ] || [ "$task_definition" = "None" ]; then
        return 1
    fi

    log_group=$(aws ecs describe-task-definition --region "$AWS_REGION" \
        --task-definition "$task_definition" \
        --query "taskDefinition.containerDefinitions[?logConfiguration.logDriver=='awslogs'] | [0].logConfiguration.options.\"awslogs-group\"" \
        --output text 2>/dev/null) || return 1
    if [ "$log_group" = "None" ] || [ "${#log_group}" -gt 512 ] || ! [[ "$log_group" =~ $name_pattern ]]; then
        return 1
    fi
    printf '%s\n' "$log_group"
}

# Helper: set retention on one log group if it has none (an existing policy is kept)
ensure_retention_if_unset() {
    local log_group="$1"
    local description="$2"
    local state found retention

    if ! state=$(aws logs describe-log-groups --region "$AWS_REGION" \
        --log-group-name-prefix "$log_group" \
        --query "logGroups[?logGroupName=='${log_group}'].[logGroupName, retentionInDays]" \
        --output text 2>/dev/null); then
        echo "⚠️  $description: could not read $log_group; retention unchanged"
        return
    fi
    read -r found retention <<< "$state"

    if [ "$found" != "$log_group" ]; then
        echo "⏭️  $description: log group not found: $log_group"
    elif [ -n "$retention" ] && [ "$retention" != "None" ]; then
        echo "✅ $description: $log_group already has ${retention}-day retention"
    else
        aws logs put-retention-policy \
            --log-group-name "$log_group" \
            --retention-in-days "$RETENTION_DAYS" \
            --region "$AWS_REGION"
        echo "✅ $description: set ${RETENTION_DAYS}-day retention: $log_group"
    fi
}

# Helper: set retention on the frontend's log group if it has none. The name
# comes from the service's task definition; if that cannot be read, every
# log group under the ECS Express name prefix is checked instead.
ensure_frontend_log_retention() {
    local description="Frontend (ECS Express) logs"
    local log_group

    if log_group=$(resolve_frontend_log_group); then
        ensure_retention_if_unset "$log_group" "$description"
    else
        echo "ℹ️  $description: no log group read from service ${FRONTEND_SERVICE}; checking ${FRONTEND_LOG_GROUP_PREFIX}*"
        ensure_prefix_retention "$FRONTEND_LOG_GROUP_PREFIX" "$description"
    fi
}

echo ""
echo "📋 Setting retention on auto-created log groups..."

# 1. AgentCore Runtime logs (auto-created by AgentCore)
ensure_log_group_retention \
    "/aws/bedrock-agentcore/runtimes/${RUNTIME_ID}-DEFAULT" \
    "AgentCore Runtime logs"

# 2. ADOT runtime logs (auto-created by ADOT)
ensure_log_group_retention \
    "/aws/bedrock-agentcore/runtimes/${RUNTIME_ID}/adot-rt-logs" \
    "ADOT runtime logs"

# 3. Frontend logs (auto-created by ECS Express, generated suffix in the name)
ensure_frontend_log_retention

# 4. AgentCore Memory log groups (auto-created, dynamic memory ID in path)
ensure_prefix_retention \
    "/aws/vendedlogs/bedrock-agentcore/memory" \
    "AgentCore Memory logs"

# Summary
echo ""
echo "📋 All game-agent-related log groups:"
aws logs describe-log-groups --region $AWS_REGION \
    --query 'logGroups[?contains(logGroupName, `game-agent`) || contains(logGroupName, `agentcore`)].{Name:logGroupName,Retention:retentionInDays}' \
    --output table

echo ""
echo "✅ Application observability setup complete"
