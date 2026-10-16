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
# reports current state and detects whether the account already supports what
# the runtime needs — WITHOUT failing the deployment for that reason alone. When
# it does not, the default path recommends the opt-in only when the AWS-reserved
# 'aws/spans' log group already exists and the destination was read (so the
# opt-in can finish the job by switching the destination); otherwise it points
# to the one-time console enable or a destination toggle keyed on the current
# destination, because the opt-in cannot create 'aws/spans'.
#
# BACKGROUND: X-Ray Transaction Search writes spans to the AWS-reserved
# 'aws/spans' log group. A first-time API enable of Transaction Search
# (destination -> CloudWatchLogs) does NOT create that log group; it is created
# only by enabling Transaction Search in the AWS console or by a disable ->
# re-enable toggle of the shared destination. This script enables Transaction
# Search additively and never toggles the shared destination, so when the
# account starts from destination 'XRay' the opt-in cannot create 'aws/spans'
# on its own. The opt-in therefore fails (rather than exiting 0) when 'aws/spans'
# is still absent after the enable, and prints the console / toggle steps the
# operator must run once. The runtime trace delivery (traces source -> X-Ray
# destination) is handled separately and verified in scripts/deploy.sh (Step 2b).
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
# Non-numeric or empty values fall back to the defaults so the opt-in never
# aborts mid-run on a bad knob (the destination may already have switched).
OBS_ACTIVE_MAX_ATTEMPTS="${GBAW_OBSERVABILITY_ACTIVE_MAX_ATTEMPTS:-30}"
OBS_ACTIVE_RETRY_SECONDS="${GBAW_OBSERVABILITY_ACTIVE_RETRY_SECONDS:-10}"
[[ "$OBS_ACTIVE_MAX_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || OBS_ACTIVE_MAX_ATTEMPTS=30
[[ "$OBS_ACTIVE_RETRY_SECONDS" =~ ^[0-9]+$ ]] || OBS_ACTIVE_RETRY_SECONDS=10

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

# Classify the AWS-reserved 'aws/spans' log group as present | absent |
# unreadable. The read is captured BEFORE the match so a denied or throttled
# 'logs:DescribeLogGroups' (or any non-zero CLI exit) is reported as
# 'unreadable' rather than mistaken for 'absent'. '--output json' is requested
# explicitly so a CLI profile whose default output is 'text', 'table', or 'yaml'
# cannot render an existing group in a form that omits the quoted name and so
# reads as 'absent'. Only output that cannot be parsed as JSON at all is
# reported as 'unreadable'; parsed JSON that lacks an 'aws/spans' entry (an
# object without 'logGroups', an empty list, or null) is 'absent', matching the
# PowerShell reader. The real CLI with '--output json' always returns an object
# with a 'logGroups' key, so these edge shapes do not occur in practice.
spans_log_group_state() {
  local output code=0
  output="$(aws logs describe-log-groups \
    --log-group-name-prefix "aws/spans" \
    --region "$AWS_REGION" \
    --output json 2>/dev/null)" || code=$?
  if [ "$code" -ne 0 ]; then
    printf 'unreadable\n'
    return 0
  fi
  OBS_SPANS_JSON="$output" python3 -c '
import json
import os
import sys

try:
    payload = json.loads(os.environ["OBS_SPANS_JSON"])
except (ValueError, KeyError):
    sys.exit(3)
groups = []
if isinstance(payload, dict):
    groups = payload.get("logGroups") or []
present = any(
    isinstance(g, dict) and g.get("logGroupName") == "aws/spans" for g in groups
)
print("present" if present else "absent")
' 2>/dev/null || { printf 'unreadable\n'; return 0; }
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

# Print the one-time remedy for the Transaction Search state, keyed on BOTH the
# current trace destination and the 'aws/spans' state so the advice never
# prescribes a toggle that cannot create the group, and never tells the operator
# to create a group that already exists:
#   $1 destination  : XRay | CloudWatchLogs | UNREADABLE
#   $2 spans_state  : present | absent | unreadable
#   $3 indent       : leading whitespace for the printed lines
#   $4 mode         : default | recheck (the recheck runs AFTER the final launch)
# A first-time API enable (destination -> CloudWatchLogs) does NOT create the
# AWS-reserved 'aws/spans' group; only the console or a disable/re-enable toggle
# of an already-CloudWatchLogs destination does (file header, starter-toolkit
# issue 457). So:
#   - either read failed                       : confirm in the console, no
#     toggle commands, because the current state is unknown;
#   - destination XRay, group PRESENT          : the group already exists; only
#     the trace destination needs switching to 'CloudWatchLogs', through the
#     console or the opt-in Step 1.7 already offers for this state;
#   - destination XRay, group absent, default  : the launch switches the
#     destination (which does not create the group by itself); Step 5c then
#     prints the one-time toggle from 'CloudWatchLogs';
#   - destination XRay, group absent, recheck  : the launch already ran and did
#     NOT switch the destination (see the toolkit's "Transaction Search
#     configuration failed" warning in the launch output); enable Transaction
#     Search once in the console, then re-run;
#   - destination CloudWatchLogs, group absent : disable/re-enable toggle.
print_spans_remedy() {
  local destination="$1" spans_state="$2" indent="$3" mode="${4:-default}"
  if [ "$destination" = "UNREADABLE" ] || [ "$spans_state" = "unreadable" ]; then
    echo "${indent}The current trace destination or the 'aws/spans' log group could not be read"
    echo "${indent}(the call was denied, throttled, or otherwise failed), so Transaction Search"
    echo "${indent}support is unknown. Confirm Transaction Search in the AWS X-Ray / CloudWatch"
    echo "${indent}console, then re-run the deployment."
    return 0
  fi
  if [ "$destination" = "XRay" ] && [ "$spans_state" = "present" ]; then
    echo "${indent}The AWS-reserved 'aws/spans' log group already exists, but the trace"
    echo "${indent}destination is 'XRay', not 'CloudWatchLogs', so spans do not flow yet."
    echo "${indent}Switch the destination to 'CloudWatchLogs' by enabling Transaction Search"
    echo "${indent}in the AWS X-Ray / CloudWatch console, or re-run with the opt-in:"
    echo "${indent}    GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true ./deploy-all.sh"
    return 0
  fi
  if [ "$destination" = "XRay" ]; then
    if [ "$mode" = "recheck" ]; then
      echo "${indent}The trace destination is 'XRay' and the AWS-reserved 'aws/spans' log group"
      echo "${indent}is not present. The launch has already run and did NOT switch the"
      echo "${indent}destination. Look in the launch output for the toolkit warning"
      echo "${indent}'Transaction Search configuration failed', then enable Transaction Search"
      echo "${indent}once in the AWS X-Ray / CloudWatch console and re-run the deployment."
      return 0
    fi
    echo "${indent}The trace destination is 'XRay' and the AWS-reserved 'aws/spans' log group"
    echo "${indent}is not present. The deployment's own 'agentcore launch' switches the"
    echo "${indent}destination to 'CloudWatchLogs', which does NOT create the group by itself;"
    echo "${indent}the post-launch recheck (Step 5c) then prints the one-time toggle. You can"
    echo "${indent}instead enable Transaction Search once in the AWS X-Ray / CloudWatch console"
    echo "${indent}now."
    return 0
  fi
  # destination CloudWatchLogs with the group absent: a disable/re-enable toggle
  # of the shared destination (or the console) creates the group.
  echo "${indent}The trace destination is already 'CloudWatchLogs' but the AWS-reserved"
  echo "${indent}'aws/spans' log group is not present. Create it once with EITHER:"
  echo "${indent}  - the AWS X-Ray / CloudWatch console (enable Transaction Search), OR"
  echo "${indent}  - a one-time destination toggle (disable, then re-enable):"
  echo "${indent}      aws xray update-trace-segment-destination --destination XRay --region ${AWS_REGION}"
  echo "${indent}      # wait for Status ACTIVE, then:"
  echo "${indent}      aws xray update-trace-segment-destination --destination CloudWatchLogs --region ${AWS_REGION}"
  echo "${indent}Then re-run the deployment."
}

# --- Read-only default path (no opt-in) ---

report_without_mutation() {
  local destination destination_display spans_state

  destination="$(get_trace_destination)"
  spans_state="$(spans_log_group_state)"

  if [ "$destination" = "UNREADABLE" ]; then
    destination_display="unknown (read failed)"
  else
    destination_display="$destination"
  fi

  echo "  ℹ️  Default mode: no account-wide X-Ray or CloudWatch Logs changes will be made."
  echo "      Current X-Ray trace segment destination: ${destination_display}"

  if [ "$destination" = "CloudWatchLogs" ] && [ "$spans_state" = "present" ]; then
    echo "  ✅ Account already supports Transaction Search (destination CloudWatchLogs, 'aws/spans' present)."
    echo "      Runtime trace delivery will be configured and verified in a later step."
    echo "  ℹ️  Continuing deployment (default account-observability is non-blocking)."
    return 0
  fi

  echo "  ⚠️  Account does NOT yet appear to support Transaction Search for the runtime."
  echo "      X-Ray Transaction Search spans require destination 'CloudWatchLogs' and the"
  echo "      AWS-reserved 'aws/spans' log group. The deployment will NOT change these"
  echo "      shared settings automatically."

  # The opt-in enables Transaction Search ADDITIVELY and never toggles the shared
  # destination. A first-time API enable does not create 'aws/spans', so the
  # opt-in can finish the job (switch the destination / add the policy) only when
  # 'aws/spans' already exists AND the destination is readable. When 'aws/spans'
  # is absent or the destination is unknown, only the console or a destination
  # toggle keyed on the current destination creates the group, so recommend that
  # (via print_spans_remedy) instead of an opt-in that would fail.
  if [ "$spans_state" = "present" ] && [ "$destination" != "UNREADABLE" ]; then
    echo "      The opt-in WOULD change these shared, account-wide settings"
    echo "      (every X-Ray / Transaction Search consumer):"
    print_account_scope
    echo "      To let the deployment configure them, re-run with the opt-in:"
    echo "          GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true ./deploy-all.sh"
  else
    print_spans_remedy "$destination" "$spans_state" "      " "default"
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
  local output code=0
  output="$(aws "$@" 2>&1)" || code=$?
  if [ "$code" -eq 0 ]; then
    return 0
  fi
  local error_code="unknown"
  if [[ "$output" =~ \(([A-Za-z0-9]+)\) ]]; then
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
# logs:PutLogEvents on an 'aws/spans' log group with Effect Allow. Prints
# COUNT (number of policies), MATCH (name of a matching policy, if any), and
# OWNED (1 if a policy already uses the project-owned name). Reads account
# state only.
find_existing_spans_policy() {
  local policies_json
  if ! policies_json="$(aws logs describe-resource-policies \
    --region "$AWS_REGION" --output json 2>/dev/null)"; then
    return 2
  fi
  OWNED_POLICY_NAME="$OBS_RESOURCE_POLICY_NAME" POLICIES_JSON="$policies_json" python3 -c '
import json
import os
import sys

try:
    payload = json.loads(os.environ["POLICIES_JSON"])
except (ValueError, KeyError):
    sys.exit(2)

owned_name = os.environ.get("OWNED_POLICY_NAME", "")
policies = payload.get("resourcePolicies", []) if isinstance(payload, dict) else []
match = None
owned = False
count = 0
for policy in policies:
    if not isinstance(policy, dict):
        continue
    count += 1
    name = policy.get("policyName", "")
    if name == owned_name:
        owned = True
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
        if statement.get("Effect") != "Allow":
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
print(f"OWNED={1 if owned else 0}")
if match:
    print(f"MATCH={match}")
'
}

configure_resource_policy() {
  local account_id="$1"
  local existing_match="$2"
  local policy_count="$3"
  local owned="$4"
  echo "  📝 Ensuring project-owned CloudWatch Logs resource policy (${OBS_RESOURCE_POLICY_NAME})..."

  if [ -n "$existing_match" ] && [ "$existing_match" != "$OBS_RESOURCE_POLICY_NAME" ]; then
    echo "  ✅ A resource policy ('${existing_match}') already grants xray.amazonaws.com"
    echo "      logs:PutLogEvents on 'aws/spans' — leaving it in place and skipping."
    echo "      See docs/OBSERVABILITY_ADOT_EXPORTER.md for when the legacy policy can be removed."
    return 0
  fi

  # The 10-policy limit only matters when we would ADD a policy. Updating a
  # policy that already uses the project-owned name is an upsert, not an add.
  if [ -z "$existing_match" ] && [ "$owned" != "1" ] && [ "$policy_count" -ge 10 ]; then
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
  local current="$1"

  if [ -z "$GBAW_XRAY_DEFAULT_INDEXING_PERCENT" ]; then
    echo "  ✅ X-Ray default indexing rule left unchanged (current: ${current}% sampling)."
    echo "      Set GBAW_XRAY_DEFAULT_INDEXING_PERCENT to change this shared setting."
    return 0
  fi

  # Compare as numbers: the CLI reports an integer rule as e.g. '1.0', so a
  # string compare of '1' against '1.0' would wrongly send an identical update.
  if [ "$current" != "UNREADABLE" ] \
    && awk -v a="$GBAW_XRAY_DEFAULT_INDEXING_PERCENT" -v b="$current" \
      'BEGIN { exit !(a + 0 == b + 0) }'; then
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
  local indexing_current
  local scan existing_match policy_count owned

  echo "  ⚠️  Account-wide observability opt-in is ENABLED."
  echo "      The following SHARED, account-wide settings may be created or changed:"
  print_account_scope
  echo "      These affect every X-Ray / Transaction Search consumer in the account."
  echo ""

  # ── Read and validate everything BEFORE the first write, so a refusal or a
  #    bad input never leaves a shared setting half-changed. ──

  # Account ID for the resource-policy scope. run_obs_mutation discards a
  # successful call's output, so read it here and require a 12-digit ID; an
  # empty or malformed value would write a grant that never matches. stdout and
  # stderr are captured separately so a CLI warning printed to stderr on a
  # successful call cannot be mistaken for part of the account ID.
  local identity_output identity_code=0 identity_error="unknown" identity_err_file
  identity_err_file="$(mktemp)"
  identity_output="$(aws sts get-caller-identity --query Account --output text --region "$AWS_REGION" 2>"$identity_err_file")" \
    || identity_code=$?
  if [ "$identity_code" -eq 0 ] && [[ "$identity_output" =~ ^[0-9]{12}$ ]]; then
    account_id="$identity_output"
    rm -f "$identity_err_file"
  else
    if [[ "$(cat "$identity_err_file")" =~ \(([A-Za-z0-9]+)\) ]]; then
      identity_error="${BASH_REMATCH[1]}"
    fi
    rm -f "$identity_err_file"
    echo "❌ identity lookup returned no account ID (${identity_error}); not changing shared settings" >&2
    return 1
  fi

  destination="$(get_trace_destination)"
  echo "  🔎 Current X-Ray trace segment destination (preserved unless changed below): ${destination}"

  if [ "$destination" = "UNREADABLE" ]; then
    echo "❌ Cannot read the current trace destination; not changing it" >&2
    return 1
  fi

  # Validate the requested indexing percentage before any write.
  if [ -n "$GBAW_XRAY_DEFAULT_INDEXING_PERCENT" ]; then
    if ! [[ "$GBAW_XRAY_DEFAULT_INDEXING_PERCENT" =~ ^[0-9]+$ ]] \
      || [ "$GBAW_XRAY_DEFAULT_INDEXING_PERCENT" -gt 100 ]; then
      echo "❌ GBAW_XRAY_DEFAULT_INDEXING_PERCENT='${GBAW_XRAY_DEFAULT_INDEXING_PERCENT}' must be an integer in [0, 100]" >&2
      return 1
    fi
  fi

  # Read the indexing rule. It is only a hard requirement when a change is
  # requested; a read failure with no change requested is non-fatal.
  indexing_current="$(get_default_indexing_percent)"
  if [ -n "$GBAW_XRAY_DEFAULT_INDEXING_PERCENT" ] && [ "$indexing_current" = "UNREADABLE" ]; then
    echo "❌ Unable to read the current X-Ray default indexing rule; not changing it" >&2
    return 1
  fi

  # Read the resource-policy list and decide the action before writing.
  existing_match=""
  policy_count=0
  owned=0
  if scan="$(find_existing_spans_policy)"; then
    policy_count="$(printf '%s\n' "$scan" | sed -n 's/^COUNT=//p')"
    owned="$(printf '%s\n' "$scan" | sed -n 's/^OWNED=//p')"
    existing_match="$(printf '%s\n' "$scan" | sed -n 's/^MATCH=//p')"
  else
    echo "❌ Unable to read existing CloudWatch Logs resource policies" >&2
    return 1
  fi
  : "${policy_count:=0}"
  : "${owned:=0}"

  # Fail the 10-policy limit before any mutation, so a refusal has no side effect.
  if [ -z "$existing_match" ] && [ "$owned" != "1" ] && [ "$policy_count" -ge 10 ]; then
    echo "❌ The account already has the maximum of 10 CloudWatch Logs resource policies" >&2
    echo "   in this region; cannot create '${OBS_RESOURCE_POLICY_NAME}'. Remove an unused" >&2
    echo "   policy (for example a legacy '${OBS_LEGACY_POLICY_NAME}') and re-run." >&2
    return 1
  fi

  # ── All reads and validation passed; perform the writes in order. Any failed
  #    write stops the run with a bounded, public-safe diagnostic. ──

  # Resource policy uses a project-owned name and is reconciled against existing
  # policies so it never overwrites an unrelated policy or adds a duplicate.
  if ! configure_resource_policy "$account_id" "$existing_match" "$policy_count" "$owned"; then
    return 1
  fi

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
  else
    echo "  ⚠️  Unexpected trace destination '${destination}'; leaving it unchanged."
    echo "     Enable Transaction Search in the console if the runtime needs it."
  fi

  # The X-Ray default indexing rule is account-wide; the current value was read
  # above and is passed in, so it is changed only when an explicit percentage
  # differs from the current one.
  if ! configure_indexing_rule "$indexing_current"; then
    return 1
  fi

  # 'aws/spans' is AWS-reserved: a first-time API enable does not create it (see
  # the file header and starter-toolkit issue 457). An additive, no-toggle enable
  # therefore cannot create it when the account started from destination 'XRay',
  # so a missing group here is a hard failure with the one-time manual step, not
  # a soft warning that lets the deploy report success it did not achieve. A read
  # that fails is reported as its own, accurate failure rather than as "absent".
  local spans_state
  spans_state="$(spans_log_group_state)"
  if [ "$spans_state" = "present" ]; then
    echo "  ✅ 'aws/spans' log group present"
  elif [ "$spans_state" = "unreadable" ]; then
    echo "❌ Transaction Search is enabled (destination CloudWatchLogs) but the" >&2
    echo "   AWS-reserved 'aws/spans' log group could not be read (the describe call" >&2
    echo "   was denied, throttled, or otherwise failed). Confirm the destination" >&2
    echo "   and 'aws/spans' in the AWS X-Ray / CloudWatch console, then re-run." >&2
    return 1
  else
    echo "❌ Transaction Search is enabled (destination CloudWatchLogs) but the" >&2
    echo "   AWS-reserved 'aws/spans' log group does not exist. A first-time API" >&2
    echo "   enable does not create it; create it once with EITHER:" >&2
    echo "     - the AWS X-Ray / CloudWatch console (enable Transaction Search), OR" >&2
    echo "     - a one-time destination toggle:" >&2
    echo "         aws xray update-trace-segment-destination --destination XRay --region ${AWS_REGION}" >&2
    echo "         # wait for Status ACTIVE, then:" >&2
    echo "         aws xray update-trace-segment-destination --destination CloudWatchLogs --region ${AWS_REGION}" >&2
    echo "   Then re-run the deployment. Spans cannot flow until 'aws/spans' exists." >&2
    return 1
  fi

  echo ""
  echo "✅ Account-wide observability configured (opt-in)"
}

# --- Read-only post-launch recheck (safe in every mode) ---

# Report, without mutating anything, whether the account currently supports
# Transaction Search for the runtime. Intended to run AFTER `agentcore launch`,
# which performs its own Transaction Search enable: this surfaces a destination
# that is not CloudWatchLogs or a missing 'aws/spans' log group so a deploy does
# not silently finish with no spans flowing. Always returns 0 (non-blocking).
report_transaction_search_state() {
  local destination spans_state
  destination="$(get_trace_destination)"
  spans_state="$(spans_log_group_state)"
  echo "  🔎 X-Ray trace segment destination: ${destination}"
  if [ "$destination" = "CloudWatchLogs" ] && [ "$spans_state" = "present" ]; then
    echo "  ✅ Transaction Search is active (destination CloudWatchLogs, 'aws/spans' present)."
    return 0
  fi
  echo "  ⚠️  Transaction Search does not yet appear active for the runtime."
  if [ "$destination" != "CloudWatchLogs" ] && [ "$destination" != "UNREADABLE" ]; then
    echo "      Trace destination is '${destination}', not 'CloudWatchLogs'."
    echo "      Spans will not flow until destination is 'CloudWatchLogs' and 'aws/spans' exists."
  fi
  print_spans_remedy "$destination" "$spans_state" "      " "recheck"
  return 0
}

main() {
  # A single argument selects the post-launch read-only recheck; with no
  # argument the script runs the default/opt-in account-observability setup.
  if [ "${1:-}" = "report-transaction-search" ]; then
    echo "🔍 Post-launch Transaction Search recheck (region ${AWS_REGION})..."
    report_transaction_search_state
    return 0
  fi
  echo "🔍 Account-wide observability (region ${AWS_REGION})..."
  if [ "$GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY" = "true" ]; then
    configure_account_observability
  else
    report_without_mutation
  fi
}

# Only run main when executed directly, so tests can source the functions.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main "$@"
fi
