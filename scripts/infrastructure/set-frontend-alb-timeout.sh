#!/bin/bash
set -e

# Align the frontend ALB idle timeout with the application request budget.
#
# BACKGROUND: The frontend runs on AWS::ECS::ExpressGatewayService, which
# provisions and fully manages its own Application Load Balancer. That managed
# ALB ships with the default 60s idle timeout, and the resource type exposes NO
# property to configure it (verified: AWS::ECS::ExpressGatewayService has no
# load-balancer/idle-timeout attribute). So this cannot be expressed in the
# CloudFormation template and must be set out-of-band on the managed ALB — the
# same pattern this repo already uses for WAF attachment and Inspector enable.
#
# WHY IT MATTERS: Complex agent queries legitimately run up to the backend's
# 180s wall-clock cap (GBAW_AGENT_TIMEOUT_REQUEST_SECONDS), and the responses
# are buffered end-to-end (no token streaming), so no bytes flow to the client
# while the agent reasons. With a 60s ALB idle timeout the connection is dropped
# mid-request and the user has to re-ask — the retry then lands on a warm
# container / prompt cache and often finishes under 60s, which is why the
# failure looks intermittent. Raising the idle timeout above the app budget
# removes the mismatch.
#
# The request chain is:  backend cap (180s) < UI proxy fetch (185s) <= ALB idle
# so the default here (185s) lets the backend return its own clean error before
# any layer above cuts the connection.
#
# Idempotent: safe to re-run; skips the API call if the timeout already matches.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

AWS_REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="${PROJECT_NAME:-game-agent}"
# Must be >= the UI proxy invocation timeout (INVOCATION_TIMEOUT_MS = 185s in
# ui/src/pages/api/copilot/chat.ts), which is itself just above the backend cap.
IDLE_TIMEOUT="${FRONTEND_ALB_IDLE_TIMEOUT:-185}"

echo "🕒 Aligning frontend ALB idle timeout (target: ${IDLE_TIMEOUT}s)..."

# The ExpressGatewayService exposes its managed ALB ARN as a stack Output.
ALB_ARN=$(aws cloudformation describe-stacks \
  --stack-name "${PROJECT_NAME}-frontend" \
  --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`LoadBalancerArn`].OutputValue' \
  --output text 2>/dev/null || echo "")

if [ -z "$ALB_ARN" ] || [ "$ALB_ARN" = "None" ]; then
  echo "  ⚠️  Frontend LoadBalancerArn not found — is the frontend stack deployed?"
  echo "  ⚠️  Skipping idle-timeout alignment (non-fatal)."
  exit 0
fi

CURRENT=$(aws elbv2 describe-load-balancer-attributes \
  --load-balancer-arn "$ALB_ARN" \
  --region "$AWS_REGION" \
  --query "Attributes[?Key=='idle_timeout.timeout_seconds'].Value | [0]" \
  --output text 2>/dev/null || echo "")

if [ "$CURRENT" = "$IDLE_TIMEOUT" ]; then
  echo "  ✅ Idle timeout already ${IDLE_TIMEOUT}s — no change needed"
  exit 0
fi

echo "  🔧 Updating idle timeout: ${CURRENT:-unknown}s → ${IDLE_TIMEOUT}s"
aws elbv2 modify-load-balancer-attributes \
  --load-balancer-arn "$ALB_ARN" \
  --attributes "Key=idle_timeout.timeout_seconds,Value=${IDLE_TIMEOUT}" \
  --region "$AWS_REGION" \
  --query "Attributes[?Key=='idle_timeout.timeout_seconds'].Value | [0]" \
  --output text > /dev/null

echo "  ✅ Frontend ALB idle timeout set to ${IDLE_TIMEOUT}s"
