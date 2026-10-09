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
# The GBAW_ prefix keeps it from colliding with an unrelated environment value.
OBS_RESOURCE_POLICY_NAME="${GBAW_OBSERVABILITY_RESOURCE_POLICY_NAME:-GameAgentTransactionSearchXRayAccess}"

# Legacy policy name written by earlier deploys of this repository. The opt-in
# path treats an existing policy under this name that already grants the needed
# access as satisfying the requirement, rather than adding a duplicate.
OBS_LEGACY_POLICY_NAME="TransactionSearchXRayAccess"

# Optional explicit X-Ray default indexing (sampling) percentage. The default
# indexing rule is account-wide and shared by every X-Ray consumer, so it is
# left unchanged unless the operator sets this to an integer in [0, 100].
GBAW_XRAY_DEFAULT_INDEXING_PERCENT="${GBAW_XRAY_DEFAULT_INDEXING_PERCENT:-}"

# Bounded poll for the trace destination to reach ACTIVE after an enable.
OBS_ACTIVE_MAX_ATTEMPTS="${GBAW_OBSERVABILITY_ACTIVE_MAX_ATTEMPTS:-30}"
OBS_ACTIVE_RETRY_SECONDS="${GBAW_OBSERVABILITY_ACTIVE_RETRY_SECONDS:-10}"

# --- Read-only state readers (safe in every mode) ---

# Print the current X-Ray trace segment destination, or UNREADABLE if the read
# fails. The API models this value as the enum XRay | CloudWatchLogs, so any
# other result (including an empty one) means the read did not succeed.
get_trace_destination() {
  local destination
  if destination="$(aws xray get-trace-segment-destination \
    --region "$AWS_REGION" \
    --query 'Destination' \
    --output text 2>/dev/null)"; then
    case "$destination" in
      XRay | CloudWatchLogs)
        printf '%s\n' "$destination"
        return 0
        ;;
    esac
  fi
  printf 'UNREADABLE\n'
}

# Return 0 if the AWS-reserved 'aws/spans' log group already exists.
spans_log_group_exists() {
  aws logs describe-log-groups \
    --log-group-name-prefix "aws/spans" \
    --region "$AWS_REGION" 2>/dev/null \
    | grep -q '"aws/spans"'
}

# Classify whether the account already supports what the runtime needs.
# Accepts the already-read destination as $1 so the destination is read once.
# Prints: supported | unsupported.
detect_account_observability_support() {
  local destination="$1"
  if [ "$destination" = "CloudWatchLogs" ] && spans_log_group_exists; then
    printf 'supported\n'
  else
    printf 'unsupported\n'
  fi
}

# Print exactly which shared settings MAY change on opt-in. Shared by the
# default "would change" warning and the opt-in scope banner.
print_account_scope() {
  echo "        1. X-Ray trace segment destination  -> CloudWatchLogs (region ${AWS_REGION})"
  echo "        2. X-Ray default indexing rule       -> unchanged unless"
  echo "           GBAW_XRAY_DEFAULT_INDEXING_PERCENT is set"
  echo "        3. CloudWatch Logs resource policy   -> ${OBS_RESOURCE_POLICY_NAME}"
  echo "           (grants xray.amazonaws.com logs:PutLogEvents on 'aws/spans' and"
  echo "            '/aws/application-signals/data')"
}

# --- Read-only default path (no opt-in) ---

report_without_mutation() {
  local support destination destination_display
  destination="$(get_trace_destination)"
  support="$(detect_account_observability_support "$destination")"

  if [ "$destination" = "UNREADABLE" ]; then
    destination_display="unknown (read failed)"
  else
    destination_display="$destination"
  fi

  echo "  ℹ️  Default mode: no account-wide X-Ray or CloudWatch Logs changes will be made."
  echo "      Current X-Ray trace segment destination: ${destination_display}"
  if [ "$support" = "supported" ]; then
    echo "  ✅ Account already supports Transaction Search (destination CloudWatchLogs, 'aws/spans' present)."
    echo "      Runtime trace delivery will be configured and verified in a later step."
  else
    echo "  ⚠️  Account does NOT yet appear to support Transaction Search for the runtime."
    echo "      X-Ray Transaction Search spans require destination 'CloudWatchLogs' and the"
    echo "      AWS-reserved 'aws/spans' log group. The deployment will NOT change these"
    echo "      shared settings automatically. The opt-in WOULD change these shared,"
    echo "      account-wide settings (every X-Ray / Transaction Search consumer):"
    print_account_scope
    echo "      To let the deployment configure them, re-run with the opt-in:"
    echo "          GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true ./deploy-all.sh"
    echo "      Or enable Transaction Search once in the AWS X-Ray / CloudWatch console."
  fi
  echo "  ℹ️  Continuing deployment (default account-observability is non-blocking)."
}

# --- Opt-in mutation path (state-preserving) ---

# Run a single opt-in mutation, capturing output and emitting only a bounded,
# public-safe diagnostic on failure (no account IDs or ARNs). Returns the aws
# exit status.
#   $1 public-safe operation label
#   $2.. aws CLI arguments
run_obs_mutation() {
  local label="$1"
  shift
  local output code
  if output="$(aws "$@" 2>&1)"; then
    return 0
  fi
  code=$?
  local error_code="unknown"
  if [[ "$output" =~ \(([A-Za-z]+)\) ]]; then
    error_code="${BASH_REMATCH[1]}"
  fi
  echo "❌ ${label} failed (${error_code})" >&2
  return "$code"
}

# Print the current Default indexing sampling percentage, or UNREADABLE.
get_default_indexing_percent() {
  local value
  if value="$(aws xray get-indexing-rules \
    --region "$AWS_REGION" \
    --query "IndexingRules[?Name=='Default'].Rule.Probabilistic.DesiredSamplingPercentage | [0]" \
    --output text 2>/dev/null)"; then
    if [ -n "$value" ] && [ "$value" != "None" ]; then
      printf '%s\n' "$value"
      return 0
    fi
  fi
  printf 'UNREADABLE\n'
}

# Return 0 if a resource policy already grants xray.amazonaws.com
# logs:PutLogEvents on an 'aws/spans' log group. Prints the matching policy name
# on stdout. Reads account state only.
find_existing_spans_policy() {
  local policies_json
  if ! policies_json="$(aws logs describe-resource-policies \
    --region "$AWS_REGION" --output json 2>/dev/null)"; then
    return 2
  fi
  POLICIES_JSON="$policies_json" python3 -c '
import json
import os
import sys

try:
    payload = json.loads(os.environ["POLICIES_JSON"])
except (ValueError, KeyError):
    sys.exit(2)

policies = payload.get("resourcePolicies", []) if isinstance(payload, dict) else []
match = None
count = 0
for policy in policies:
    if not isinstance(policy, dict):
        continue
    count += 1
    name = policy.get("policyName", "")
    document = policy.get("policyDocument", "")
    try:
        doc = json.loads(document) if isinstance(document, str) else document
    except (ValueError, TypeError):
        continue
    statements = doc.get("Statement", []) if isinstance(doc, dict) else []
    if isinstance(statements, dict):
        statements = [statements]
    for statement in statements:
        if not isinstance(statement, dict):
            continue
        principal = statement.get("Principal", {})
        service = principal.get("Service") if isinstance(principal, dict) else None
        services = service if isinstance(service, list) else [service]
        if "xray.amazonaws.com" not in services:
            continue
        action = statement.get("Action")
        actions = action if isinstance(action, list) else [action]
        if "logs:PutLogEvents" not in actions:
            continue
        resource = statement.get("Resource")
        resources = resource if isinstance(resource, list) else [resource]
        if any(isinstance(r, str) and "aws/spans" in r for r in resources):
            match = name
            break
    if match:
        break

print(f"COUNT={count}")
if match:
    print(f"MATCH={match}")
'
}

configure_resource_policy() {
  local account_id="$1"
  local scan existing_match policy_count
  echo "  📝 Ensuring project-owned CloudWatch Logs resource policy (${OBS_RESOURCE_POLICY_NAME})..."

  # List existing policies first so we never add a duplicate grant and never
  # exceed the account's 10-policy limit silently.
  existing_match=""
  policy_count=0
  if scan="$(find_existing_spans_policy)"; then
    policy_count="$(printf '%s\n' "$scan" | sed -n 's/^COUNT=//p')"
    existing_match="$(printf '%s\n' "$scan" | sed -n 's/^MATCH=//p')"
  else
    echo "❌ Unable to read existing CloudWatch Logs resource policies" >&2
    return 1
  fi
  : "${policy_count:=0}"

  if [ -n "$existing_match" ] && [ "$existing_match" != "$OBS_RESOURCE_POLICY_NAME" ]; then
    echo "  ✅ A resource policy ('${existing_match}') already grants xray.amazonaws.com"
    echo "      logs:PutLogEvents on 'aws/spans' — leaving it in place and skipping."
    echo "      See docs/OBSERVABILITY_ADOT_EXPORTER.md for when the legacy policy can be removed."
    return 0
  fi

  if [ -z "$existing_match" ] && [ "$policy_count" -ge 10 ]; then
    echo "❌ The account already has the maximum of 10 CloudWatch Logs resource policies" >&2
    echo "   in this region; cannot create '${OBS_RESOURCE_POLICY_NAME}'. Remove an unused" >&2
    echo "   policy (for example a legacy '${OBS_LEGACY_POLICY_NAME}') and re-run." >&2
    return 1
  fi

  if ! run_obs_mutation "resource policy" \
    logs put-resource-policy \
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
    --region "$AWS_REGION"; then
    return 1
  fi
  echo "  ✅ CloudWatch Logs resource policy configured"
}

# Bounded poll until the trace destination reports ACTIVE. Warns (non-fatal) if
# it never reaches ACTIVE within the budget.
wait_for_destination_active() {
  local attempt status
  for ((attempt = 1; attempt <= OBS_ACTIVE_MAX_ATTEMPTS; attempt++)); do
    status="$(aws xray get-trace-segment-destination \
      --region "$AWS_REGION" --query 'Status' --output text 2>/dev/null || echo "")"
    if [ "$status" = "ACTIVE" ]; then
      echo "  ✅ Trace segment destination is ACTIVE"
      return 0
    fi
    if [ "$attempt" -lt "$OBS_ACTIVE_MAX_ATTEMPTS" ]; then
      sleep "$OBS_ACTIVE_RETRY_SECONDS"
    fi
  done
  echo "  ⚠️  Trace segment destination did not reach ACTIVE within the wait budget;"
  echo "      it may still converge shortly. Re-check in the AWS X-Ray console if traces"
  echo "      do not appear."
  return 0
}

configure_indexing_rule() {
  local current
  current="$(get_default_indexing_percent)"

  if [ "$current" = "UNREADABLE" ]; then
    echo "❌ Unable to read the current X-Ray default indexing rule; not changing it" >&2
    return 1
  fi

  if [ -z "$GBAW_XRAY_DEFAULT_INDEXING_PERCENT" ]; then
    echo "  ✅ X-Ray default indexing rule left unchanged (current: ${current}% sampling)."
    echo "      Set GBAW_XRAY_DEFAULT_INDEXING_PERCENT to change this shared setting."
    return 0
  fi

  if ! [[ "$GBAW_XRAY_DEFAULT_INDEXING_PERCENT" =~ ^[0-9]+$ ]] \
    || [ "$GBAW_XRAY_DEFAULT_INDEXING_PERCENT" -gt 100 ]; then
    echo "❌ GBAW_XRAY_DEFAULT_INDEXING_PERCENT='${GBAW_XRAY_DEFAULT_INDEXING_PERCENT}' must be an integer in [0, 100]" >&2
    return 1
  fi

  if [ "$GBAW_XRAY_DEFAULT_INDEXING_PERCENT" = "$current" ]; then
    echo "  ✅ X-Ray default indexing rule already ${current}% — left unchanged"
    return 0
  fi

  echo "  📊 Setting X-Ray default indexing rule to ${GBAW_XRAY_DEFAULT_INDEXING_PERCENT}% sampling..."
  echo "     Rollback: aws xray update-indexing-rule --name Default --rule '{\"Probabilistic\":{\"DesiredSamplingPercentage\":${current}}}' --region ${AWS_REGION}"
  if ! run_obs_mutation "indexing rule" \
    xray update-indexing-rule \
    --name "Default" \
    --rule "{\"Probabilistic\": {\"DesiredSamplingPercentage\": ${GBAW_XRAY_DEFAULT_INDEXING_PERCENT}}}" \
    --region "$AWS_REGION"; then
    return 1
  fi
  echo "  ✅ X-Ray default indexing rule set to ${GBAW_XRAY_DEFAULT_INDEXING_PERCENT}%"
}

configure_account_observability() {
  local account_id
  local destination
  account_id="$(aws sts get-caller-identity --query Account --output text)"

  echo "  ⚠️  Account-wide observability opt-in is ENABLED."
  echo "      The following SHARED, account-wide settings may be created or changed:"
  print_account_scope
  echo "      These affect every X-Ray / Transaction Search consumer in the account."
  echo ""

  destination="$(get_trace_destination)"
  echo "  🔎 Current X-Ray trace segment destination (preserved unless changed below): ${destination}"

  # Resource policy uses a project-owned name and is reconciled against existing
  # policies so it never overwrites an unrelated policy or adds a duplicate.
  configure_resource_policy "$account_id"

  # Enable Transaction Search only if it is not already enabled. This is an
  # ADDITIVE enable, never a disable/re-enable toggle of a shared setting.
  if [ "$destination" = "CloudWatchLogs" ]; then
    echo "  ✅ X-Ray trace destination already CloudWatchLogs — left unchanged"
  elif [ "$destination" = "XRay" ]; then
    echo "  🎯 Enabling Transaction Search (destination -> CloudWatchLogs)..."
    echo "     Rollback: aws xray update-trace-segment-destination --destination ${destination} --region ${AWS_REGION}"
    if ! run_obs_mutation "trace destination" \
      xray update-trace-segment-destination \
      --destination CloudWatchLogs \
      --region "$AWS_REGION"; then
      return 1
    fi
    echo "  ✅ X-Ray trace destination set to CloudWatchLogs"
    wait_for_destination_active
  elif [ "$destination" = "UNREADABLE" ]; then
    echo "❌ Cannot read the current trace destination; not changing it" >&2
    return 1
  else
    echo "  ⚠️  Unexpected trace destination '${destination}'; leaving it unchanged."
    echo "     Enable Transaction Search in the console if the runtime needs it."
  fi

  # The X-Ray default indexing rule is account-wide; read and preserve it, and
  # change it only when an explicit percentage is set.
  if ! configure_indexing_rule; then
    return 1
  fi

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
