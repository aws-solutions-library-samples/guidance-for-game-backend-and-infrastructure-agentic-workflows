#!/bin/bash
set -euo pipefail

# Account-wide observability setup (scoped, opt-in, state-preserving)
#
# X-Ray Transaction Search and its supporting CloudWatch Logs configuration are
# ACCOUNT-WIDE, cross-application settings:
#   - the X-Ray trace segment destination (shared by every X-Ray caller),
#   - the default X-Ray indexing (sampling) rule,
#   - a shared CloudWatch Logs resource policy on the AWS-reserved 'aws/spans'
#     and application-signals log groups.
#
# Changing these affects other workloads in the account, so this script does NOT
# mutate them unless the operator explicitly opts in with
# GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true. The default path is read-only: it
# reports current state, detects whether the account already supports what the
# runtime needs, and prints the opt-in instruction if it does not — WITHOUT
# failing the deployment for that reason alone.
#
# BACKGROUND: The AgentCore CLI enables Transaction Search via API during
# `agentcore launch`, but the API path does not always create the internal
# 'aws/spans' log group that X-Ray needs. Enabling Transaction Search (or
# toggling it) through the account-observability path creates it. The runtime
# trace delivery (traces source -> X-Ray destination) is handled separately and
# verified in scripts/deploy.sh (Step 2b).
# See: https://github.com/aws/bedrock-agentcore-starter-toolkit/issues/457

AWS_REGION="${AWS_REGION:-us-west-2}"
GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY="${GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY:-false}"

# Project-owned name for the shared Logs resource policy (used only on opt-in).
OBS_RESOURCE_POLICY_NAME="${OBS_RESOURCE_POLICY_NAME:-GameAgentTransactionSearchXRayAccess}"

# --- Read-only state readers (safe in every mode) ---

# Print the current X-Ray trace segment destination, or NOT_CONFIGURED.
get_trace_destination() {
  aws xray get-trace-segment-destination \
    --region "$AWS_REGION" \
    --query 'Destination' \
    --output text 2>/dev/null || printf 'NOT_CONFIGURED\n'
}

# Return 0 if the AWS-reserved 'aws/spans' log group already exists.
spans_log_group_exists() {
  aws logs describe-log-groups \
    --log-group-name-prefix "aws/spans" \
    --region "$AWS_REGION" 2>/dev/null \
    | grep -q '"aws/spans"'
}

# Classify whether the account already supports what the runtime needs.
# Prints: supported | unsupported.
detect_account_observability_support() {
  local destination
  destination="$(get_trace_destination)"
  if [ "$destination" = "CloudWatchLogs" ] && spans_log_group_exists; then
    printf 'supported\n'
  else
    printf 'unsupported\n'
  fi
}

# Print exactly which shared settings may change before applying them.
print_account_scope() {
  echo "  ⚠️  Account-wide observability opt-in is ENABLED."
  echo "      The following SHARED, account-wide settings may be created or changed:"
  echo "        1. X-Ray trace segment destination  -> CloudWatchLogs (region ${AWS_REGION})"
  echo "        2. X-Ray default indexing rule       -> 1% probabilistic sampling"
  echo "        3. CloudWatch Logs resource policy   -> ${OBS_RESOURCE_POLICY_NAME}"
  echo "           (grants xray.amazonaws.com logs:PutLogEvents on 'aws/spans' and"
  echo "            '/aws/application-signals/data')"
  echo "      These affect every X-Ray / Transaction Search consumer in the account."
}

# --- Read-only default path (no opt-in) ---

report_without_mutation() {
  local support destination
  destination="$(get_trace_destination)"
  support="$(detect_account_observability_support)"

  echo "  ℹ️  Default mode: no account-wide X-Ray or CloudWatch Logs changes will be made."
  echo "      Current X-Ray trace segment destination: ${destination}"
  if [ "$support" = "supported" ]; then
    echo "  ✅ Account already supports Transaction Search (destination CloudWatchLogs, 'aws/spans' present)."
    echo "      Runtime trace delivery will be configured and verified in a later step."
  else
    echo "  ⚠️  Account does NOT yet appear to support Transaction Search for the runtime."
    echo "      X-Ray Transaction Search spans require destination 'CloudWatchLogs' and the"
    echo "      AWS-reserved 'aws/spans' log group. The deployment will NOT change these"
    echo "      shared settings automatically."
    echo "      To let the deployment configure them, re-run with the opt-in:"
    echo "          GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true ./deploy-all.sh"
    echo "      Or enable Transaction Search once in the AWS X-Ray / CloudWatch console."
  fi
  echo "  ℹ️  Continuing deployment (default account-observability is non-blocking)."
}

# --- Opt-in mutation path (state-preserving) ---

configure_resource_policy() {
  local account_id="$1"
  echo "  📝 Ensuring project-owned CloudWatch Logs resource policy (${OBS_RESOURCE_POLICY_NAME})..."
  aws logs put-resource-policy \
    --policy-name "$OBS_RESOURCE_POLICY_NAME" \
    --policy-document "{
      \"Version\": \"2012-10-17\",
      \"Statement\": [{
        \"Sid\": \"TransactionSearchXRayAccess\",
        \"Effect\": \"Allow\",
        \"Principal\": {\"Service\": \"xray.amazonaws.com\"},
        \"Action\": \"logs:PutLogEvents\",
        \"Resource\": [
          \"arn:aws:logs:${AWS_REGION}:${account_id}:log-group:aws/spans:*\",
          \"arn:aws:logs:${AWS_REGION}:${account_id}:log-group:/aws/application-signals/data:*\"
        ],
        \"Condition\": {
          \"ArnLike\": {\"aws:SourceArn\": \"arn:aws:xray:${AWS_REGION}:${account_id}:*\"},
          \"StringEquals\": {\"aws:SourceAccount\": \"${account_id}\"}
        }
      }]
    }" \
    --region "$AWS_REGION" > /dev/null
  echo "  ✅ CloudWatch Logs resource policy configured"
}

configure_account_observability() {
  local account_id
  local destination
  account_id="$(aws sts get-caller-identity --query Account --output text)"

  print_account_scope
  echo ""

  destination="$(get_trace_destination)"
  echo "  🔎 Current X-Ray trace segment destination (preserved unless changed below): ${destination}"

  # Resource policy uses a project-owned name, so it never overwrites an
  # unrelated policy.
  configure_resource_policy "$account_id"

  # Enable Transaction Search only if it is not already enabled. This is an
  # ADDITIVE enable, never a disable/re-enable toggle of a shared setting.
  if [ "$destination" = "CloudWatchLogs" ]; then
    echo "  ✅ X-Ray trace destination already CloudWatchLogs — left unchanged"
  elif [ "$destination" = "NOT_CONFIGURED" ] || [ "$destination" = "XRay" ]; then
    echo "  🎯 Enabling Transaction Search (destination -> CloudWatchLogs)..."
    echo "     Rollback: aws xray update-trace-segment-destination --destination ${destination} --region ${AWS_REGION}"
    aws xray update-trace-segment-destination \
      --destination CloudWatchLogs \
      --region "$AWS_REGION" > /dev/null
    echo "  ✅ X-Ray trace destination set to CloudWatchLogs"
  else
    echo "  ⚠️  Unexpected trace destination '${destination}'; leaving it unchanged."
    echo "     Enable Transaction Search in the console if the runtime needs it."
  fi

  echo "  📊 Configuring default X-Ray indexing rule (1% sampling, free tier)..."
  echo "     Rollback: review 'aws xray get-indexing-rules' and restore the prior DesiredSamplingPercentage."
  aws xray update-indexing-rule \
    --name "Default" \
    --rule '{"Probabilistic": {"DesiredSamplingPercentage": 1}}' \
    --region "$AWS_REGION" > /dev/null
  echo "  ✅ X-Ray indexing rule configured"

  if spans_log_group_exists; then
    echo "  ✅ 'aws/spans' log group present"
  else
    echo "  ⚠️  'aws/spans' log group not visible yet. It is created by AWS shortly after"
    echo "      Transaction Search is enabled; if traces do not appear, enable Transaction"
    echo "      Search once via the AWS console."
  fi

  echo ""
  echo "✅ Account-wide observability configured (opt-in)"
}

main() {
  echo "🔍 Account-wide observability (region ${AWS_REGION})..."
  if [ "$GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY" = "true" ]; then
    configure_account_observability
  else
    report_without_mutation
  fi
}

# Only run main when executed directly, so tests can source the functions.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main
fi
