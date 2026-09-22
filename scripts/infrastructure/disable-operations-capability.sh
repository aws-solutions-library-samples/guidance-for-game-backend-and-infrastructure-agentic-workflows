#!/usr/bin/env bash
#
# disable-operations-capability.sh — EMERGENCY per-capability disable of the E4
# operations kill switch (GitHub issue #416).
#
# This disables exactly ONE capability's phases (all three phases of the single
# gamelift.capacity-adjustment capability) by deploying a fresh kill-switch
# document via the AppConfig IMMEDIATE (hard-down) strategy — 100% instantly, no
# bake. operations_enabled is written fail-closed (false) so this path only ever
# narrows authority, never widens the master switch.
#
# It is default-zero in effect (it only ever sets phase booleans to false) and
# fully reversible: a later enabling document restores the capability. It
# deletes NO resource and NO data; it only appends a new hosted configuration
# version and starts one immediate deployment. It verifies an explicit
# profile/account/region before any write and requires an explicit --confirm.
#
# It talks to AppConfig directly (not through the control API) so it works even
# when the control Lambda / API is unhealthy.
#
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="game-agent"
STACK_NAME="${PROJECT_NAME}-operations-control-plane"
FRESHNESS_SECONDS="${GBAW_OPERATIONS_KILL_SWITCH_FRESHNESS_SECONDS:-300}"

# The only supported capability today. Kept explicit so the script is truthful
# about exactly what it can disable.
CAPABILITY="gamelift.capacity-adjustment"

AWS_PROFILE="${AWS_PROFILE:-default}"
AWS_PROFILE_ARGS=(--profile "$AWS_PROFILE")

usage() {
    cat <<'USAGE'
Usage: disable-operations-capability.sh --confirm [--capability gamelift.capacity-adjustment]

  Emergency per-capability disable: deploys a kill-switch document that sets the
  named capability's prepare/dispatch/execute phases to false via the AppConfig
  immediate strategy (operations_enabled is written fail-closed). Reversible and
  non-destructive. Verifies identity/region and requires --confirm.

  --confirm       Required acknowledgement.
  --capability    Capability to disable. Default (and only supported):
                  gamelift.capacity-adjustment.
USAGE
}

CONFIRMED="false"
while [ "$#" -gt 0 ]; do
    case "$1" in
        --confirm) CONFIRMED="true" ;;
        --capability) shift; CAPABILITY="${1:-$CAPABILITY}" ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
    esac
    shift
done

if [ "$CONFIRMED" != "true" ]; then
    echo "❌ Refusing to disable a capability without --confirm." >&2
    usage
    exit 3
fi
if [ "$CAPABILITY" != "gamelift.capacity-adjustment" ]; then
    echo "❌ Refusing: unknown capability '$CAPABILITY'. Only gamelift.capacity-adjustment is supported." >&2
    exit 3
fi

echo "🔐 Verifying AWS credentials and region before any write ..."
echo "   AWS_PROFILE=${AWS_PROFILE}  AWS_REGION=${AWS_REGION}"
if ! CALLER_IDENTITY="$(aws sts get-caller-identity "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" --output text 2>/dev/null)"; then
    echo "❌ Unable to verify caller identity." >&2
    exit 4
fi
echo "   Caller identity: $CALLER_IDENTITY"

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
    exit 4
fi
STRATEGY_NAME="${PROJECT_NAME}-operations-immediate"
STRATEGY_ID="$(aws appconfig list-deployment-strategies \
    "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --query "Items[?Name=='${STRATEGY_NAME}'].Id | [0]" --output text 2>/dev/null || true)"
if [ -z "$STRATEGY_ID" ] || [ "$STRATEGY_ID" = "None" ]; then
    echo "❌ Could not resolve the immediate deployment strategy '${STRATEGY_NAME}'." >&2
    exit 4
fi
echo "   application=$APP_ID environment=$ENV_ID profile=$PROFILE_ID strategy=$STRATEGY_ID"

NOW_EPOCH="$(date -u +%s)"
ISSUED_AT="$(date -u -r "$NOW_EPOCH" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "@$NOW_EPOCH" +%Y-%m-%dT%H:%M:%SZ)"
NOT_AFTER_EPOCH=$((NOW_EPOCH + FRESHNESS_SECONDS))
NOT_AFTER="$(date -u -r "$NOT_AFTER_EPOCH" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "@$NOT_AFTER_EPOCH" +%Y-%m-%dT%H:%M:%SZ)"
CONFIG_VERSION="$NOW_EPOCH"

DOC_FILE="$(mktemp "${TMPDIR:-/tmp}/gbaw-killswitch-cap.XXXXXXXX.json")"
trap 'rm -f "$DOC_FILE"' EXIT
# The single capability's phases are set to false. operations_enabled is written
# fail-closed (false) so this per-capability path never widens the master switch;
# to re-enable, issue an enabling document through the control plane.
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

echo "🛑 Creating a hosted version disabling capability '$CAPABILITY' ..."
VERSION_NUMBER="$(aws appconfig create-hosted-configuration-version \
    "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --application-id "$APP_ID" \
    --configuration-profile-id "$PROFILE_ID" \
    --content-type application/json \
    --content "fileb://$DOC_FILE" \
    --query "VersionNumber" --output text)"
echo "   Hosted version: $VERSION_NUMBER"

echo "🚨 Deploying via the IMMEDIATE (hard-down) strategy ..."
aws appconfig start-deployment \
    "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --application-id "$APP_ID" \
    --environment-id "$ENV_ID" \
    --deployment-strategy-id "$STRATEGY_ID" \
    --configuration-profile-id "$PROFILE_ID" \
    --configuration-version "$VERSION_NUMBER" \
    --description "Emergency disable of capability ${CAPABILITY}." >/dev/null

echo "✅ Capability '$CAPABILITY' disabled (all phases false). Reversible via the control plane."
