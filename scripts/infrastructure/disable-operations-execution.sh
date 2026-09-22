#!/usr/bin/env bash
#
# disable-operations-execution.sh — reversible EMERGENCY disable of the E3
# execution control plane (GitHub issue #415).
#
# This is lever-flipping only: it sets ExecutionMode=disabled while keeping
# Provisioned=true, so BOTH kill-switch levers engage — the dispatch API stage
# throttles to zero (no new dispatch) and the injected
# GBAW_OPERATIONS_EXECUTION_MODE makes the executor fail closed — WITHOUT
# deleting any resource or data. It rebuilds NO code, runs NO Docker, uploads
# NO artifact, and reuses the stack's existing parameter values. It is fully
# reversible via deploy-operations-execution.sh --enable.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

AWS_REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="game-agent"
STACK_NAME="${PROJECT_NAME}-operations-execution"
TEMPLATE="$PROJECT_ROOT/infrastructure/cloudformation/07-operations-execution.yaml"

AWS_PROFILE="${AWS_PROFILE:-default}"
AWS_PROFILE_ARGS=(--profile "$AWS_PROFILE")

usage() {
    cat <<'USAGE'
Usage: disable-operations-execution.sh --confirm

  Emergency, data-preserving disable of an EXISTING provisioned 07 execution
  stack. Keeps Provisioned=true and every resource under CloudFormation (stable
  physical names, retained data), reuses all current parameter values, and sets
  only ExecutionMode=disabled so BOTH levers fail closed. Does NOT delete
  resources and does NOT rebuild code. Reversible via
  deploy-operations-execution.sh --enable.

  --confirm   Required acknowledgement that this flips the runtime kill switch.
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
# Provisioned stays true (resources + data retained); only ExecutionMode flips
# to disabled. UsePreviousValue keeps the code keys, bindings, and fleet exactly
# as deployed so no rebuild or re-upload is needed.
echo "🛑 Disabling $STACK_NAME (ExecutionMode=disabled, Provisioned=true, no rebuild) ..."
aws cloudformation update-stack \
    "${AWS_PROFILE_ARGS[@]}" \
    --region "$AWS_REGION" \
    --stack-name "$STACK_NAME" \
    --use-previous-template \
    --capabilities CAPABILITY_NAMED_IAM \
    --parameters \
        "ParameterKey=Provisioned,ParameterValue=true" \
        "ParameterKey=ExecutionMode,ParameterValue=disabled" \
        "ParameterKey=ProjectName,UsePreviousValue=true" \
        "ParameterKey=Environment,UsePreviousValue=true" \
        "ParameterKey=CognitoIssuer,UsePreviousValue=true" \
        "ParameterKey=CognitoClientId,UsePreviousValue=true" \
        "ParameterKey=OperationsTableName,UsePreviousValue=true" \
        "ParameterKey=OperationsKmsKeyArn,UsePreviousValue=true" \
        "ParameterKey=EnrolledFleetId,UsePreviousValue=true" \
        "ParameterKey=CodeS3Bucket,UsePreviousValue=true" \
        "ParameterKey=DispatcherCodeS3Key,UsePreviousValue=true" \
        "ParameterKey=ExecutorCodeS3Key,UsePreviousValue=true"

echo "   Waiting for the update to complete ..."
aws cloudformation wait stack-update-complete \
    "${AWS_PROFILE_ARGS[@]}" \
    --region "$AWS_REGION" \
    --stack-name "$STACK_NAME"

echo "✅ $STACK_NAME is disabled. Both levers fail closed; resources and data retained."
echo "   Re-enable is reversible: GBAW_OPERATIONS_EXECUTION_MODE=remediate deploy-operations-execution.sh --enable ..."
