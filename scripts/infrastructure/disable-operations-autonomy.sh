#!/usr/bin/env bash
#
# disable-operations-autonomy.sh — reversible EMERGENCY disable of the E5
# bounded-autonomy control plane (GitHub issue #440).
#
# This is lever-flipping only: it sets AutonomyMode=disabled while keeping
# Provisioned=true, so BOTH autonomy levers engage — the EventBridge schedule
# rule is set to DISABLED (no timed evaluation) AND the injected
# GBAW_OPERATIONS_AUTONOMY_ENABLED=false / GBAW_OPERATIONS_MODE=disabled make the
# evaluator fail closed at startup before any evaluation or pre-write — WITHOUT
# deleting any resource or data. It rebuilds NO code, runs NO Docker, uploads NO
# artifact, and reuses the stack's existing parameter values. It is fully
# reversible via deploy-operations-autonomy.sh --enable.
#
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="game-agent"
STACK_NAME="${PROJECT_NAME}-operations-autonomy"

AWS_PROFILE="${AWS_PROFILE:-default}"
AWS_PROFILE_ARGS=(--profile "$AWS_PROFILE")

usage() {
    cat <<'USAGE'
Usage: disable-operations-autonomy.sh --confirm

  Emergency, data-preserving disable of an EXISTING provisioned 09 autonomy
  stack. Keeps Provisioned=true and every resource under CloudFormation (stable
  physical names, retained data), reuses all current parameter values, and sets
  only AutonomyMode=disabled so BOTH levers fail closed (schedule DISABLED +
  evaluator fails closed at startup). Does NOT delete resources and does NOT
  rebuild code. Reversible via deploy-operations-autonomy.sh --enable.

  --confirm   Required acknowledgement that this flips the runtime autonomy switch.
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
    echo "❌ Refusing to disable without --confirm." >&2
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

# Reuse the stack's EXISTING parameter values for everything except the mode.
# Provisioned stays true (resources + data retained); only AutonomyMode flips to
# disabled. UsePreviousValue keeps the code key, bindings, policy pins, and
# switch ids exactly as deployed so no rebuild or re-upload is needed.
echo "🛑 Disabling $STACK_NAME (AutonomyMode=disabled, Provisioned=true, no rebuild) ..."
aws cloudformation update-stack \
    "${AWS_PROFILE_ARGS[@]}" \
    --region "$AWS_REGION" \
    --stack-name "$STACK_NAME" \
    --use-previous-template \
    --capabilities CAPABILITY_NAMED_IAM \
    --parameters \
        "ParameterKey=Provisioned,ParameterValue=true" \
        "ParameterKey=AutonomyMode,ParameterValue=disabled" \
        "ParameterKey=ProjectName,UsePreviousValue=true" \
        "ParameterKey=Environment,UsePreviousValue=true" \
        "ParameterKey=OperationsTableName,UsePreviousValue=true" \
        "ParameterKey=OperationsKmsKeyArn,UsePreviousValue=true" \
        "ParameterKey=ExecutionStateMachineArn,UsePreviousValue=true" \
        "ParameterKey=EnrolledFleetId,UsePreviousValue=true" \
        "ParameterKey=EnrolledLocation,UsePreviousValue=true" \
        "ParameterKey=TenantId,UsePreviousValue=true" \
        "ParameterKey=WorkspaceId,UsePreviousValue=true" \
        "ParameterKey=AutonomySubject,UsePreviousValue=true" \
        "ParameterKey=AutonomyClient,UsePreviousValue=true" \
        "ParameterKey=AutonomyPolicyId,UsePreviousValue=true" \
        "ParameterKey=AutonomyPolicyVersion,UsePreviousValue=true" \
        "ParameterKey=AutonomyPolicyHash,UsePreviousValue=true" \
        "ParameterKey=AutonomyStateId,UsePreviousValue=true" \
        "ParameterKey=AppConfigApplicationId,UsePreviousValue=true" \
        "ParameterKey=AppConfigEnvironmentId,UsePreviousValue=true" \
        "ParameterKey=KillSwitchProfileId,UsePreviousValue=true" \
        "ParameterKey=AutonomySwitchProfileId,UsePreviousValue=true" \
        "ParameterKey=AppConfigExtensionLayerArn,UsePreviousValue=true" \
        "ParameterKey=CodeS3Bucket,UsePreviousValue=true" \
        "ParameterKey=EvaluatorCodeS3Key,UsePreviousValue=true"

echo "   Waiting for the update to complete ..."
aws cloudformation wait stack-update-complete \
    "${AWS_PROFILE_ARGS[@]}" \
    --region "$AWS_REGION" \
    --stack-name "$STACK_NAME"

echo "✅ $STACK_NAME is disabled. Both levers fail closed; resources and data retained."
echo "   Re-enable is reversible:"
echo "     GBAW_OPERATIONS_MODE=operate GBAW_OPERATIONS_AUTONOMY_CONFIRM=operate deploy-operations-autonomy.sh --enable ..."
