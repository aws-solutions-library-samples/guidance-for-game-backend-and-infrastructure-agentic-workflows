#!/bin/bash
set -e

# Application-specific observability setup (idempotent)
# Sets retention on all auto-created log groups that AWS services create without expiration

RUNTIME_ID=$1
AWS_REGION=${AWS_REGION:-us-west-2}
RETENTION_DAYS=14
PROJECT_NAME=${PROJECT_NAME:-game-agent}

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

    if aws logs describe-log-groups --region $AWS_REGION \
         --log-group-name-exact "$log_group" \
         --query 'logGroups[0].retentionInDays' --output text 2>/dev/null | grep -q "^${RETENTION_DAYS}$"; then
        echo "✅ $description: already has ${RETENTION_DAYS}-day retention"
    elif aws logs describe-log-groups --region $AWS_REGION \
         --log-group-name-exact "$log_group" \
         --query 'logGroups[0].logGroupName' --output text 2>/dev/null | grep -q "$log_group"; then
        aws logs put-retention-policy \
            --log-group-name "$log_group" \
            --retention-in-days $RETENTION_DAYS \
            --region $AWS_REGION
        echo "✅ $description: set ${RETENTION_DAYS}-day retention"
    else
        echo "⏭️  $description: log group not found (will be created by AWS on first use)"
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

# 3. ECS log groups (auto-created by ECS Express, dynamic service ID in path)
ensure_prefix_retention \
    "/ecs/${PROJECT_NAME}-frontend" \
    "ECS Express logs"

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
