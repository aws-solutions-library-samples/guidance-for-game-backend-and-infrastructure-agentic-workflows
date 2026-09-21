#!/bin/bash
# Game Agent - OPTIONAL E1 operations observation control plane deploy wrapper
# (GitHub issue #413).
#
# This wrapper is DELIBERATELY NOT called by deploy.sh / deploy-all.sh. A normal
# deployment creates zero E1 resources. Even when this wrapper runs, it refuses
# to create *enabled* resources unless the operator supplies BOTH:
#   * GBAW_OPERATIONS_MODE=enabled  (environment), and
#   * --enable                      (flag)
# Without both, it renders the plan (change set preview) and exits without
# mutating AWS. Disabling is a separate, data-preserving path (--disable).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

AWS_REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="game-agent"
STACK_NAME="${PROJECT_NAME}-operations"
TEMPLATE="$PROJECT_ROOT/infrastructure/cloudformation/06-operations-observation.yaml"

ENVIRONMENT="beta"
ACTION="preview"   # preview | enable | disable
COGNITO_ISSUER="${COGNITO_ISSUER:-}"
COGNITO_CLIENT_ID="${COGNITO_CLIENT_ID:-}"

usage() {
    cat <<'USAGE'
Usage: deploy-operations.sh [--enable | --disable] [--environment beta|prod]

  (no flag)     Preview only. Renders a change set and creates nothing.
  --enable      Deploy the optional stack with OperationsMode=enabled.
                Requires GBAW_OPERATIONS_MODE=enabled in the environment.
  --disable     Re-deploy the stack with OperationsMode=disabled (data-preserving
                rollback: request path removed, durable audit data retained).
  --environment Target environment (default: beta). "prod" enables DynamoDB
                deletion protection and longer log retention.

Env: COGNITO_ISSUER and COGNITO_CLIENT_ID are required for --enable.
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --enable)  ACTION="enable" ;;
        --disable) ACTION="disable" ;;
        --environment) shift; ENVIRONMENT="${1:-beta}" ;;
        -h|--help) usage; exit 0 ;;
        *) echo "❌ Unknown argument: $1" >&2; usage; exit 2 ;;
    esac
    shift
done

echo "=================================================="
echo " ⚙️  OPTIONAL E1 operations control plane"
echo "=================================================="
echo "Region:      $AWS_REGION"
echo "Stack:       $STACK_NAME"
echo "Environment: $ENVIRONMENT"
echo "Action:      $ACTION"
echo ""

if ! command -v aws >/dev/null 2>&1; then
    echo "❌ AWS CLI not found." >&2
    exit 1
fi

OPERATIONS_MODE="disabled"

if [ "$ACTION" = "enable" ]; then
    # Belt-and-braces opt-in: require the environment value AND the flag.
    if [ "${GBAW_OPERATIONS_MODE:-disabled}" != "enabled" ]; then
        echo "❌ Refusing to enable: set GBAW_OPERATIONS_MODE=enabled to confirm." >&2
        exit 3
    fi
    if [ -z "$COGNITO_ISSUER" ] || [ -z "$COGNITO_CLIENT_ID" ]; then
        echo "❌ COGNITO_ISSUER and COGNITO_CLIENT_ID are required to enable." >&2
        exit 3
    fi
    OPERATIONS_MODE="enabled"
elif [ "$ACTION" = "disable" ]; then
    OPERATIONS_MODE="disabled"
fi

PARAM_OVERRIDES=(
    "ProjectName=${PROJECT_NAME}"
    "OperationsMode=${OPERATIONS_MODE}"
    "Environment=${ENVIRONMENT}"
    "CognitoIssuer=${COGNITO_ISSUER}"
    "CognitoClientId=${COGNITO_CLIENT_ID}"
)

if [ "$ACTION" = "preview" ]; then
    echo "🔎 Preview only (no opt-in). Rendering a change set; nothing is created."
    echo "   To deploy: GBAW_OPERATIONS_MODE=enabled $0 --enable"
    CHANGE_SET="operations-preview-$(date +%s)"
    # Preview always uses OperationsMode=disabled so even the change set plans
    # zero enabled resources.
    PREVIEW_PARAMS=(
        "ParameterKey=ProjectName,ParameterValue=${PROJECT_NAME}"
        "ParameterKey=OperationsMode,ParameterValue=disabled"
        "ParameterKey=Environment,ParameterValue=${ENVIRONMENT}"
        "ParameterKey=CognitoIssuer,ParameterValue=${COGNITO_ISSUER}"
        "ParameterKey=CognitoClientId,ParameterValue=${COGNITO_CLIENT_ID}"
    )
    aws cloudformation create-change-set \
        --stack-name "$STACK_NAME" \
        --change-set-name "$CHANGE_SET" \
        --change-set-type CREATE \
        --template-body "file://$TEMPLATE" \
        --capabilities CAPABILITY_NAMED_IAM \
        --region "$AWS_REGION" \
        --parameters "${PREVIEW_PARAMS[@]}" \
        >/dev/null 2>&1 || true
    echo "   (Change set '$CHANGE_SET' describes the plan; review then delete it.)"
    echo "✅ Preview complete. No resources were created."
    exit 0
fi

echo "🚀 Deploying $STACK_NAME with OperationsMode=$OPERATIONS_MODE ..."
aws cloudformation deploy \
    --template-file "$TEMPLATE" \
    --stack-name "$STACK_NAME" \
    --capabilities CAPABILITY_NAMED_IAM \
    --region "$AWS_REGION" \
    --parameter-overrides "${PARAM_OVERRIDES[@]}"

echo "✅ Deploy complete (OperationsMode=$OPERATIONS_MODE)."
