#!/usr/bin/env bash
#
# disable-all-operations.sh — EMERGENCY deployment-wide hard-down of the E4
# operations kill switch (GitHub issue #416).
#
# This flips the deployment-wide master switch OFF by deploying a fresh,
# all-disabled kill-switch document (operations_enabled=false, every capability
# phase false) via the AppConfig IMMEDIATE (hard-down) strategy — 100% instantly,
# no bake. It is the "disable everything now" path.
#
# It is default-zero in effect (the document it writes only ever disables) and
# fully reversible: a later enabling document issued through the control plane
# restores operations. It deletes NO resource and NO data; it only appends a new
# safe hosted configuration version and starts one immediate deployment. It
# verifies an explicit profile/account/region before any write and requires an
# explicit --confirm.
#
# It talks to AppConfig directly (not through the control API) so it works even
# when the control Lambda / API is unhealthy.
#
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="game-agent"
STACK_NAME="${PROJECT_NAME}-operations-control-plane"
FRESHNESS_SECONDS="${GBAW_OPERATIONS_KILL_SWITCH_FRESHNESS_SECONDS:-300}"

AWS_PROFILE="${AWS_PROFILE:-default}"
AWS_PROFILE_ARGS=(--profile "$AWS_PROFILE")

usage() {
    cat <<'USAGE'
Usage: disable-all-operations.sh --confirm

  Emergency deployment-wide hard-down: deploys an all-disabled kill-switch
  document (operations_enabled=false) via the AppConfig immediate strategy.
  Reversible (a later enabling document restores operations) and non-destructive
  (deletes nothing). Verifies identity/region and requires --confirm.

  --confirm   Required acknowledgement that this hard-downs ALL operations now.
USAGE
}

CONFIRMED="false"
while [ "$#" -gt 0 ]; do
    case "$1" in
        --confirm) CONFIRMED="true" ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
    esac
    shift
done

if [ "$CONFIRMED" != "true" ]; then
    echo "❌ Refusing to hard-down without --confirm." >&2
    usage
    exit 3
fi

echo "🔐 Verifying AWS credentials and region before any write ..."
echo "   AWS_PROFILE=${AWS_PROFILE}  AWS_REGION=${AWS_REGION}"
if ! CALLER_IDENTITY="$(aws sts get-caller-identity "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" --output text 2>/dev/null)"; then
    echo "❌ Unable to verify caller identity." >&2
    exit 4
fi
echo "   Caller identity: $CALLER_IDENTITY"

# Resolve the AppConfig identifiers from the 08 stack outputs (no guessing).
echo "🔎 Resolving AppConfig identifiers from $STACK_NAME outputs ..."
read_output() {
    aws cloudformation describe-stacks \
        "${AWS_PROFILE_ARGS[@]}" \
        --region "$AWS_REGION" \
        --stack-name "$STACK_NAME" \
        --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" \
        --output text 2>/dev/null || true
}
APP_ID="$(read_output ControlApplicationId)"
ENV_ID="$(read_output ControlEnvironmentId)"
PROFILE_ID="$(read_output KillSwitchProfileId)"
if [ -z "$APP_ID" ] || [ "$APP_ID" = "None" ] || [ -z "$PROFILE_ID" ] || [ "$PROFILE_ID" = "None" ]; then
    echo "❌ Could not resolve AppConfig application/profile ids from $STACK_NAME." >&2
    echo "   Is the 08 control-plane stack provisioned?" >&2
    exit 4
fi
# The immediate (hard-down) deployment strategy created by the 08 stack.
STRATEGY_NAME="${PROJECT_NAME}-operations-immediate"
STRATEGY_ID="$(aws appconfig list-deployment-strategies \
    "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --query "Items[?Name=='${STRATEGY_NAME}'].Id | [0]" --output text 2>/dev/null || true)"
if [ -z "$STRATEGY_ID" ] || [ "$STRATEGY_ID" = "None" ]; then
    echo "❌ Could not resolve the immediate deployment strategy '${STRATEGY_NAME}'." >&2
    exit 4
fi
echo "   application=$APP_ID environment=$ENV_ID profile=$PROFILE_ID strategy=$STRATEGY_ID"

# Build a fresh, all-disabled, freshness-stamped kill-switch document. The
# config_version is derived from the current epoch seconds so it is monotonic
# and unique per write.
NOW_EPOCH="$(date -u +%s)"
ISSUED_AT="$(date -u -r "$NOW_EPOCH" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "@$NOW_EPOCH" +%Y-%m-%dT%H:%M:%SZ)"
NOT_AFTER_EPOCH=$((NOW_EPOCH + FRESHNESS_SECONDS))
NOT_AFTER="$(date -u -r "$NOT_AFTER_EPOCH" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "@$NOT_AFTER_EPOCH" +%Y-%m-%dT%H:%M:%SZ)"
CONFIG_VERSION="$NOW_EPOCH"

DOC_FILE="$(mktemp "${TMPDIR:-/tmp}/gbaw-killswitch.XXXXXXXX.json")"
trap 'rm -f "$DOC_FILE"' EXIT
cat > "$DOC_FILE" <<JSON
{
  "contract_version": "1.0",
  "config_version": ${CONFIG_VERSION},
  "issued_at": "${ISSUED_AT}",
  "not_after": "${NOT_AFTER}",
  "operations_enabled": false,
  "capabilities": {
    "gamelift.capacity-adjustment": {
      "prepare": false,
      "dispatch": false,
      "execute": false
    }
  }
}
JSON

echo "🛑 Creating an all-disabled hosted kill-switch version (operations_enabled=false) ..."
VERSION_NUMBER="$(aws appconfig create-hosted-configuration-version \
    "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --application-id "$APP_ID" \
    --configuration-profile-id "$PROFILE_ID" \
    --content-type application/json \
    --content "fileb://$DOC_FILE" \
    --query "VersionNumber" --output text)"
echo "   Hosted version: $VERSION_NUMBER"

echo "🚨 Deploying the all-disabled document via the IMMEDIATE (hard-down) strategy ..."
aws appconfig start-deployment \
    "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --application-id "$APP_ID" \
    --environment-id "$ENV_ID" \
    --deployment-strategy-id "$STRATEGY_ID" \
    --configuration-profile-id "$PROFILE_ID" \
    --configuration-version "$VERSION_NUMBER" \
    --description "Emergency hard-down: all operations disabled." >/dev/null

echo "✅ Deployment-wide operations kill switch engaged (operations_enabled=false)."
echo "   Reversible: issue an enabling document through the control plane to restore operations."
