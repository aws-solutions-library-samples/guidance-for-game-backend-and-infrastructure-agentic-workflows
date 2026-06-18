#!/bin/bash
set -e

# Account-wide observability setup (idempotent)
# Ensures X-Ray Transaction Search is properly configured with the required
# 'aws/spans' log group (AWS-reserved namespace) and resource policy.
#
# BACKGROUND: The AgentCore CLI enables Transaction Search via API during
# `agentcore launch`, but the API path does NOT create the internal 'aws/spans'
# log group that X-Ray needs. This log group is only created when Transaction
# Search is toggled (disabled → re-enabled) or enabled via the AWS Console.
#
# This script works around the issue by toggling Transaction Search if the
# log group is missing, which triggers AWS to create it.
#
# Additionally, the direct-code-deploy path in the CLI does not set up the
# CloudWatch delivery (traces source → X-Ray destination) for the runtime,
# which is required for the Bedrock AgentCore Observability console page.
#
# See: https://github.com/aws/bedrock-agentcore-starter-toolkit/issues/457

AWS_REGION=${AWS_REGION:-us-west-2}
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "🔍 Setting up account-wide observability..."

# Step 1: Ensure 'aws/spans' log group exists (AWS-reserved namespace)
# X-Ray Transaction Search writes spans to log group 'aws/spans' (WITHOUT leading /)
# with log stream 'default'. This is in the AWS-reserved namespace — only AWS services
# can create it. The only way to trigger creation via API is to toggle Transaction Search.
echo "  📦 Checking aws/spans log group..."
if aws logs describe-log-groups --log-group-name-prefix "aws/spans" --region $AWS_REGION 2>/dev/null | grep -q '"aws/spans"'; then
    echo "  ✅ aws/spans log group exists"
else
    echo "  ⚠️  aws/spans log group missing — toggling Transaction Search to trigger creation..."

    # Check current state
    CURRENT_DEST=$(aws xray get-trace-segment-destination --region $AWS_REGION --query 'Destination' --output text 2>/dev/null || echo "UNKNOWN")

    if [ "$CURRENT_DEST" = "CloudWatchLogs" ]; then
        # Already enabled — need to toggle: disable first, wait, then re-enable
        echo "  🔄 Disabling Transaction Search temporarily..."
        aws xray update-trace-segment-destination --destination XRay --region $AWS_REGION > /dev/null 2>&1

        # Wait for PENDING → ACTIVE
        echo "  ⏳ Waiting for disable to complete..."
        for i in $(seq 1 30); do
            STATUS=$(aws xray get-trace-segment-destination --region $AWS_REGION --query 'Status' --output text 2>/dev/null)
            if [ "$STATUS" = "ACTIVE" ]; then
                break
            fi
            sleep 10
        done

        echo "  🔄 Re-enabling Transaction Search..."
        aws xray update-trace-segment-destination --destination CloudWatchLogs --region $AWS_REGION > /dev/null 2>&1
    else
        # Not enabled yet — just enable
        echo "  🎯 Enabling Transaction Search..."
        aws xray update-trace-segment-destination --destination CloudWatchLogs --region $AWS_REGION > /dev/null 2>&1
    fi

    # Wait for enable to complete and log group to appear
    echo "  ⏳ Waiting for aws/spans log group creation..."
    LG_CREATED=false
    for i in $(seq 1 30); do
        STATUS=$(aws xray get-trace-segment-destination --region $AWS_REGION --query 'Status' --output text 2>/dev/null)
        HAS_LG=$(aws logs describe-log-groups --log-group-name-prefix "aws/spans" --region $AWS_REGION 2>/dev/null | grep -c '"aws/spans"' || echo 0)
        if [ "$STATUS" = "ACTIVE" ] && [ "$HAS_LG" -gt 0 ]; then
            echo "  ✅ aws/spans log group created"
            LG_CREATED=true
            break
        fi
        sleep 10
    done

    # Fallback: if first-time enable didn't create log group, force a toggle
    if [ "$LG_CREATED" = "false" ]; then
        echo "  ⚠️  Log group not created by initial enable — forcing toggle..."
        aws xray update-trace-segment-destination --destination XRay --region $AWS_REGION > /dev/null 2>&1
        for i in $(seq 1 30); do
            STATUS=$(aws xray get-trace-segment-destination --region $AWS_REGION --query 'Status' --output text 2>/dev/null)
            [ "$STATUS" = "ACTIVE" ] && break
            sleep 10
        done
        aws xray update-trace-segment-destination --destination CloudWatchLogs --region $AWS_REGION > /dev/null 2>&1
        for i in $(seq 1 30); do
            STATUS=$(aws xray get-trace-segment-destination --region $AWS_REGION --query 'Status' --output text 2>/dev/null)
            HAS_LG=$(aws logs describe-log-groups --log-group-name-prefix "aws/spans" --region $AWS_REGION 2>/dev/null | grep -c '"aws/spans"' || echo 0)
            if [ "$STATUS" = "ACTIVE" ] && [ "$HAS_LG" -gt 0 ]; then
                echo "  ✅ aws/spans log group created (via toggle)"
                LG_CREATED=true
                break
            fi
            sleep 10
        done
        if [ "$LG_CREATED" = "false" ]; then
            echo "  ⚠️  WARNING: aws/spans log group could not be created. OTEL trace export may fail."
            echo "  ⚠️  Try enabling Transaction Search via the AWS Console as a fallback."
        fi
    fi
fi

# Step 2: Create CloudWatch Logs resource policy for X-Ray to write spans
# X-Ray's internal service role needs PutLogEvents on 'aws/spans' (no leading /).
# The resource ARN uses 'log-group:aws/spans:*' (matching the actual log group name).
echo "  📝 Ensuring CloudWatch Logs resource policy..."
aws logs put-resource-policy \
  --policy-name TransactionSearchXRayAccess \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Sid\": \"TransactionSearchXRayAccess\",
      \"Effect\": \"Allow\",
      \"Principal\": {\"Service\": \"xray.amazonaws.com\"},
      \"Action\": \"logs:PutLogEvents\",
      \"Resource\": [
        \"arn:aws:logs:${AWS_REGION}:${ACCOUNT_ID}:log-group:aws/spans:*\",
        \"arn:aws:logs:${AWS_REGION}:${ACCOUNT_ID}:log-group:/aws/application-signals/data:*\"
      ],
      \"Condition\": {
        \"ArnLike\": {\"aws:SourceArn\": \"arn:aws:xray:${AWS_REGION}:${ACCOUNT_ID}:*\"},
        \"StringEquals\": {\"aws:SourceAccount\": \"${ACCOUNT_ID}\"}
      }
    }]
  }" \
  --region $AWS_REGION > /dev/null 2>&1
echo "  ✅ CloudWatch Logs resource policy configured"

# Step 3: Ensure Transaction Search destination is CloudWatch Logs
CURRENT_STATUS=$(aws xray get-trace-segment-destination --region $AWS_REGION 2>/dev/null || echo '{"Destination":"NOT_CONFIGURED"}')
if echo "$CURRENT_STATUS" | grep -q "CloudWatchLogs"; then
    echo "  ✅ X-Ray trace destination already set to CloudWatch Logs"
else
    echo "  🎯 Setting CloudWatch Logs as span destination..."
    aws xray update-trace-segment-destination \
      --destination CloudWatchLogs \
      --region $AWS_REGION > /dev/null 2>&1
    echo "  ✅ X-Ray trace destination configured"
fi

# Step 4: Configure 1% sampling (free tier)
echo "  📊 Configuring span sampling (1% free tier)..."
aws xray update-indexing-rule \
  --name "Default" \
  --rule '{"Probabilistic": {"DesiredSamplingPercentage": 1}}' \
  --region $AWS_REGION > /dev/null 2>&1
echo "  ✅ X-Ray indexing rule configured"

echo ""
echo "✅ Account-wide observability setup complete"
echo "ℹ️  Note: This is a one-time, account-wide setup"
echo "ℹ️  X-Ray Transaction Search spans flow to log group 'aws/spans' (stream: default)"
