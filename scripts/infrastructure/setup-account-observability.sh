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
    if [ "$GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE" = "true" ]; then
      # The stop-switch preflight halts the deploy before 'agentcore launch', so
      # the launch never switches the destination and no Step 5c recheck runs.
      echo "${indent}The trace destination is 'XRay' and the AWS-reserved 'aws/spans' log group"
      echo "${indent}is not present. The stop switch is set, so the deployment stops before"
      echo "${indent}'agentcore launch' and nothing will switch the destination. Enable"
      echo "${indent}Transaction Search once in the AWS X-Ray / CloudWatch console, then re-run."
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

# --- AgentCore starter toolkit Transaction Search preflight (read-only) ---
#
# Independently of this repository's opt-in, `agentcore launch` runs the starter
# toolkit's enable_transaction_search_if_needed() whenever runtime observability
# is enabled (the same switch that selects the opentelemetry-instrument
# entrypoint). In toolkit 0.3.11 there is no flag to keep that OTEL entrypoint
# while skipping only this account-wide step, so the toolkit may change SHARED,
# account-wide X-Ray / CloudWatch Logs settings during launch. This preflight
# predicts exactly which of those changes the toolkit WOULD make, using the
# toolkit's own conditions, so the deployment can warn (and optionally stop)
# before the launch rather than discover the change afterward.
#
# The toolkit's three conditions (services/xray.py), reproduced here read-only:
#   1. resource policy  — created iff NO CloudWatch Logs resource policy named
#      literally 'TransactionSearchXRayAccess' exists. The toolkit checks the
#      NAME only, so this repository's project-owned
#      'GameAgentTransactionSearchXRayAccess' policy does NOT satisfy it: the
#      toolkit still adds its own 'TransactionSearchXRayAccess' policy.
#   2. trace destination — set to CloudWatchLogs iff the current destination is
#      not already CloudWatchLogs.
#   3. indexing rule    — the toolkit sets the shared X-Ray 'Default' indexing
#      rule to 1% via UpdateIndexingRule when NO rule named 'Default' exists. An
#      account that already has a 'Default' rule satisfies the condition, so the
#      preflight predicts no indexing change there.
#
# The exact name the toolkit uses for its own resource policy (name-only check).
TOOLKIT_RESOURCE_POLICY_NAME="TransactionSearchXRayAccess"

# Opt-in, fail-closed stop switch. Only the exact value 'true' stops the deploy
# before `agentcore launch` when the toolkit would change account-wide settings.
# Any other value (including 'TRUE', '1', 'yes', '') is treated as off.
GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE="${GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE:-false}"

# Read `describe-resource-policies` exactly once per preflight and cache the raw
# JSON in PREFLIGHT_POLICIES_JSON. PREFLIGHT_POLICIES_RC is 0 on a successful
# read and non-zero when the read failed (unknown). Both the toolkit name-only
# check and the project-owned grant annotation reuse this one read so the two
# cannot drift or hit the API twice.
read_resource_policies_once() {
  if PREFLIGHT_POLICIES_JSON="$(aws logs describe-resource-policies \
    --region "$AWS_REGION" --output json 2>/dev/null)"; then
    PREFLIGHT_POLICIES_RC=0
  else
    PREFLIGHT_POLICIES_JSON=""
    PREFLIGHT_POLICIES_RC=1
  fi
}

# Return 0 if a CloudWatch Logs resource policy named literally
# 'TransactionSearchXRayAccess' exists; 1 if it does not; 2 if the list read
# fails (unknown). Matches the toolkit's name-only _need_resource_policy check.
# Uses the single cached read from read_resource_policies_once.
toolkit_resource_policy_present() {
  if [ "${PREFLIGHT_POLICIES_RC:-1}" -ne 0 ]; then
    return 2
  fi
  TOOLKIT_NAME="$TOOLKIT_RESOURCE_POLICY_NAME" POLICIES_JSON="$PREFLIGHT_POLICIES_JSON" python3 -c '
import json
import os
import sys

try:
    payload = json.loads(os.environ["POLICIES_JSON"])
except (ValueError, KeyError):
    sys.exit(2)

name = os.environ["TOOLKIT_NAME"]
policies = payload.get("resourcePolicies", []) if isinstance(payload, dict) else []
for policy in policies:
    if isinstance(policy, dict) and policy.get("policyName") == name:
        sys.exit(0)
sys.exit(1)
'
}

# Print 'present', 'absent', or 'UNREADABLE' for a Default X-Ray indexing rule.
# Matches the toolkit's _need_indexing_rule check, which keys off a rule NAMED
# 'Default' existing (not its sampling percentage).
get_default_indexing_rule_presence() {
  local rules_json
  if ! rules_json="$(aws xray get-indexing-rules \
    --region "$AWS_REGION" --output json 2>/dev/null)"; then
    printf 'UNREADABLE\n'
    return 0
  fi
  RULES_JSON="$rules_json" python3 -c '
import json
import os
import sys

try:
    payload = json.loads(os.environ["RULES_JSON"])
except (ValueError, KeyError):
    print("UNREADABLE")
    sys.exit(0)

rules = payload.get("IndexingRules", []) if isinstance(payload, dict) else []
for rule in rules:
    if isinstance(rule, dict) and rule.get("Name") == "Default":
        print("present")
        sys.exit(0)
print("absent")
'
}

# Predict the toolkit's account-wide changes and print a human-readable report.
# Sets the global TOOLKIT_WOULD_CHANGE to:
#   0  the toolkit would change nothing;
#   1  the toolkit would change at least one shared setting;
#   2  at least one condition could not be read (unknown) — never reported as
#      "no change".
# Output is public-safe (no account IDs or ARNs).
preflight_transaction_search_toolkit() {
  local destination policy_rc indexing_presence
  local -a changes=()
  local -a detail=()
  local -a unknown_items=()
  local unknown=0
  # Track exactly which items the toolkit would change, so the rollback block
  # names only the relevant commands.
  local policy_would_add=0
  local destination_would_change=0
  local indexing_would_create=0
  local prior_destination=""

  # Read the resource-policy list once up front; both the toolkit name-only
  # check and the project-grant annotation reuse this single read.
  read_resource_policies_once

  # 1. Resource policy (toolkit checks the literal name 'TransactionSearchXRayAccess').
  if toolkit_resource_policy_present; then
    policy_rc=0
  else
    policy_rc=$?
  fi
  case "$policy_rc" in
    0)
      : # no change — no detail line (single-line no-change output)
      ;;
    1)
      policy_would_add=1
      changes+=("Adds a CloudWatch Logs resource policy named '${TOOLKIT_RESOURCE_POLICY_NAME}'")
      detail+=("      • CloudWatch Logs resource policy: WOULD ADD '${TOOLKIT_RESOURCE_POLICY_NAME}'")
      detail+=("        (grants xray.amazonaws.com logs:PutLogEvents on 'aws/spans' and")
      detail+=("         '/aws/application-signals/data')")
      ;;
    *)
      unknown=1
      unknown_items+=("the CloudWatch Logs resource policy")
      detail+=("      • CloudWatch Logs resource policy: UNKNOWN (could not read resource policies)")
      ;;
  esac

  # 2. Trace segment destination.
  destination="$(get_trace_destination)"
  if [ "$destination" = "CloudWatchLogs" ]; then
    : # no change — no detail line
  elif [ "$destination" = "UNREADABLE" ]; then
    unknown=1
    unknown_items+=("the X-Ray trace segment destination")
    detail+=("      • X-Ray trace segment destination: UNKNOWN (could not read destination)")
  else
    destination_would_change=1
    prior_destination="$destination"
    changes+=("Sets the X-Ray trace segment destination to CloudWatchLogs (region ${AWS_REGION})")
    detail+=("      • X-Ray trace segment destination: WOULD CHANGE ${destination} -> CloudWatchLogs")
    detail+=("        (region ${AWS_REGION}; shared by every X-Ray caller)")
  fi

  # 3. Default indexing rule (toolkit sets the shared 'Default' rule via
  #    UpdateIndexingRule when no rule named 'Default' exists). Any value other
  #    than 'present' or 'absent' is treated as UNKNOWN (fail closed).
  indexing_presence="$(get_default_indexing_rule_presence)"
  if [ "$indexing_presence" = "present" ]; then
    : # no change — no detail line
  elif [ "$indexing_presence" = "absent" ]; then
    indexing_would_create=1
    changes+=("Sets the X-Ray 'Default' indexing rule to 1% via UpdateIndexingRule")
    detail+=("      • X-Ray default indexing rule: WOULD SET 'Default' to 1% (UpdateIndexingRule)")
  else
    unknown=1
    unknown_items+=("the X-Ray default indexing rule")
    detail+=("      • X-Ray default indexing rule: UNKNOWN (could not read indexing rules)")
  fi

  if [ "$unknown" -eq 1 ]; then
    TOOLKIT_WOULD_CHANGE=2
    echo "  🔎 Preflight: predicting AgentCore starter toolkit Transaction Search changes..."
    echo "      (the toolkit runs these during 'agentcore launch' when runtime"
    echo "       observability is enabled; they are account-wide and shared)"
    local line
    for line in "${detail[@]}"; do
      echo "$line"
    done
    echo "  ⚠️  Preflight could not read one or more account settings, so the toolkit's"
    echo "      effect is UNKNOWN. The toolkit writes on doubt: for each unknown item it"
    echo "      WILL perform the write during 'agentcore launch'. Treating this as a"
    echo "      possible shared change (not 'no change')."
    local unknown_item
    for unknown_item in "${unknown_items[@]}"; do
      echo "        - UNKNOWN: ${unknown_item}"
    done
    return 0
  fi

  if [ "${#changes[@]}" -eq 0 ]; then
    TOOLKIT_WOULD_CHANGE=0
    echo "  ✅ Preflight: the AgentCore starter toolkit would make NO account-wide X-Ray or CloudWatch Logs change."
    return 0
  fi

  TOOLKIT_WOULD_CHANGE=1
  echo "  🔎 Preflight: predicting AgentCore starter toolkit Transaction Search changes..."
  echo "      (the toolkit runs these during 'agentcore launch' when runtime"
  echo "       observability is enabled; they are account-wide and shared)"
  local line
  for line in "${detail[@]}"; do
    echo "$line"
  done
  echo ""
  echo "  ⚠️  WARNING: 'agentcore launch' WILL change SHARED, account-wide settings via the"
  echo "      AgentCore starter toolkit (NOT this repository's opt-in). These affect every"
  echo "      X-Ray / Transaction Search consumer in the account:"
  local change
  for change in "${changes[@]}"; do
    echo "        - ${change}"
  done
  # If the project-owned policy already grants the access, the toolkit STILL
  # adds its own name-checked policy; call that out explicitly.
  if [ "$policy_would_add" -eq 1 ] && resource_policy_grant_present; then
    echo "      Note: this repository's project-owned policy ('${OBS_RESOURCE_POLICY_NAME}')"
    echo "      already grants the needed access, but the toolkit checks only for a policy"
    echo "      named '${TOOLKIT_RESOURCE_POLICY_NAME}', so it adds its own duplicate policy."
  fi
  echo "      Cleanup / rollback after launch (run only once no other workload relies on them):"
  if [ "$policy_would_add" -eq 1 ]; then
    echo "        - Resource policy: aws logs delete-resource-policy --policy-name ${TOOLKIT_RESOURCE_POLICY_NAME} --region ${AWS_REGION}"
    echo "          Caution: '${TOOLKIT_RESOURCE_POLICY_NAME}' is also this repository's legacy policy"
    echo "          name. Confirm the launch (not an earlier deploy) created it before deleting,"
    echo "          so you do not remove a shared grant another workload relies on."
  fi
  if [ "$destination_would_change" -eq 1 ]; then
    echo "        - Trace destination: aws xray update-trace-segment-destination --destination ${prior_destination} --region ${AWS_REGION}"
    echo "          (restores the prior destination '${prior_destination}')"
  fi
  if [ "$indexing_would_create" -eq 1 ]; then
    echo "        - Indexing rule: the toolkit sets the shared 'Default' rule to 1%. To restore a"
    echo "          prior percentage: aws xray update-indexing-rule --name Default \\"
    echo "            --rule '{\"Probabilistic\":{\"DesiredSamplingPercentage\":<prior>}}' --region ${AWS_REGION}"
  fi
  if [ "$GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE" != "true" ]; then
    echo "      To stop the deploy before this happens, re-run with"
    echo "      GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE=true (deploy halts before 'agentcore launch')."
  fi
  return 0
}

# Like find_existing_spans_policy, but parses the already-cached
# PREFLIGHT_POLICIES_JSON (read once by read_resource_policies_once) instead of
# calling AWS again. Prints MATCH=<name> when a policy already grants the access.
scan_cached_spans_policy() {
  if [ "${PREFLIGHT_POLICIES_RC:-1}" -ne 0 ]; then
    return 2
  fi
  OWNED_POLICY_NAME="$OBS_RESOURCE_POLICY_NAME" POLICIES_JSON="$PREFLIGHT_POLICIES_JSON" python3 -c '
import json
import os
import sys

try:
    payload = json.loads(os.environ["POLICIES_JSON"])
except (ValueError, KeyError):
    sys.exit(2)

policies = payload.get("resourcePolicies", []) if isinstance(payload, dict) else []
match = None
for policy in policies:
    if not isinstance(policy, dict):
        continue
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

if match:
    print(f"MATCH={match}")
'
}

# Return 0 if ANY CloudWatch Logs resource policy already grants
# xray.amazonaws.com logs:PutLogEvents on an 'aws/spans' log group (Effect
# Allow), regardless of policy name. Used only to annotate the warning when the
# project-owned grant is present but the toolkit will still add its own. Reuses
# the single cached describe-resource-policies read from the preflight.
resource_policy_grant_present() {
  local scan
  if scan="$(scan_cached_spans_policy)"; then
    [ -n "$(printf '%s\n' "$scan" | sed -n 's/^MATCH=//p')" ]
    return $?
  fi
  return 1
}

# Entry point used by scripts/deploy.sh before each `agentcore launch`. Runs the
# read-only preflight, then enforces the fail-closed stop switch. Returns:
#   0  safe to launch (no change, or operator accepted the warning);
#   1  stop requested AND the toolkit would (or might) change shared settings.
# A read failure (UNKNOWN) is treated as a possible change, so the stop switch
# halts on it too (fail closed).
run_transaction_search_preflight() {
  # The preflight reads AWS CLI JSON through python3 (the shell cannot). Without
  # python3 every read would be misread, so fail here with an accurate message
  # before any read, exactly as main() does for the default/opt-in path.
  # (scripts/deploy.sh runs the same check with its other prerequisites, so a
  # full deploy never reaches this branch without python3.)
  if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ python3 not found. It is required to parse AWS responses in the" >&2
    echo "   toolkit Transaction Search preflight. Install Python 3 from" >&2
    echo "   https://www.python.org/downloads/ (or your OS package manager) and" >&2
    echo "   re-run." >&2
    return 1
  fi
  TOOLKIT_WOULD_CHANGE=0
  preflight_transaction_search_toolkit

  if [ "$GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE" = "true" ] \
    && [ "${TOOLKIT_WOULD_CHANGE:-0}" -ne 0 ]; then
    # The opt-in cannot create the AWS-reserved 'aws/spans' log group. Lead with
    # the one-time console enable (which always reaches a passing state) and only
    # offer the opt-in route when 'aws/spans' already exists, so an operator on an
    # account without that group is not sent to an opt-in that writes the policy,
    # switches the destination, and still stops at Step 1.7.
    local remedy_spans_state
    remedy_spans_state="$(spans_log_group_state)"
    echo "❌ GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE=true and the AgentCore starter toolkit" >&2
    echo "   would change (or might change) account-wide X-Ray / CloudWatch Logs settings." >&2
    echo "   Stopping before 'agentcore launch' so no shared setting is modified." >&2
    echo "   To reach a passing run without any shared change, enable Transaction Search once" >&2
    echo "   in the AWS X-Ray / CloudWatch console. The account then already satisfies the three" >&2
    echo "   toolkit conditions (a CloudWatch Logs resource policy named 'TransactionSearchXRayAccess'," >&2
    echo "   the X-Ray trace segment destination 'CloudWatchLogs', and an X-Ray 'Default' indexing" >&2
    echo "   rule), so this deploy proceeds without changing any shared setting, with the stop" >&2
    echo "   switch still in place." >&2
    if [ "$remedy_spans_state" = "present" ]; then
      echo "   Because the AWS-reserved 'aws/spans' log group already exists, you can instead reach" >&2
      echo "   that state with this repository's opt-in, which switches the destination and adds the" >&2
      echo "   matching policy:" >&2
      echo "     GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true \\" >&2
      echo "       GBAW_OBSERVABILITY_RESOURCE_POLICY_NAME=TransactionSearchXRayAccess ./deploy-all.sh" >&2
    fi
    return 1
  fi
  return 0
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
  # The read-only state readers parse AWS CLI JSON with python3 (the shell
  # cannot). A standalone run of this script therefore needs python3 on PATH;
  # without it the describe calls would be misread as denied or throttled, so
  # fail here with an accurate message before any read. (scripts/deploy.sh runs
  # the same check with its other prerequisites.)
  if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ python3 not found. It is required to parse AWS responses in this" >&2
    echo "   account-observability step. Install Python 3 from" >&2
    echo "   https://www.python.org/downloads/ (or your OS package manager) and" >&2
    echo "   re-run." >&2
    return 1
  fi
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

# Only act when executed directly, so tests can source the functions without
# running anything. scripts/deploy.sh invokes this script as a subprocess —
# both for the default Step 1.7 account-observability path and, with
# --toolkit-preflight, for the read-only toolkit Transaction Search preflight
# before each `agentcore launch`. Running it as a subprocess (rather than
# sourcing it) keeps this script's own `set -euo pipefail` from leaking into
# deploy.sh's shell options.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  # Three run modes, selected by the first argument:
  #   (no argument)             the Step 1.7 default/opt-in account-observability
  #                             setup (main);
  #   --toolkit-preflight       the read-only toolkit Transaction Search preflight
  #                             deploy.sh runs before each `agentcore launch`;
  #   report-transaction-search the Step 5c post-launch recheck, handled by main.
  case "${1:-}" in
    --toolkit-preflight)
      run_transaction_search_preflight
      exit $?
      ;;
    *)
      main "$@"
      ;;
  esac
fi
