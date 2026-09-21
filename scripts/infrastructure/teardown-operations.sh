#!/bin/bash
# Game Agent - OPTIONAL E1 operations observation control plane teardown wrapper
# (GitHub issue #413).
#
# Teardown is EXPLICIT and is NEVER invoked automatically. teardown-all.sh /
# scripts/teardown.sh do not call this script. It requires an explicit
# confirmation token and deletes only the CloudFormation stack.
#
# Durable audit data is RETAINED by design: the DynamoDB table, the KMS key, and
# their contents use a Retain deletion policy and are intentionally left in
# place. This wrapper does NOT delete audit data. Removing retained audit data
# is a separate, explicit, future cleanup performed by hand after confirming the
# data is no longer needed (empty and delete the retained table and disable the
# retained key). There is deliberately no flag here that erases audit data.

set -euo pipefail

AWS_REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="game-agent"
STACK_NAME="${PROJECT_NAME}-operations"

# AWS_PROFILE is passed EXPLICITLY to every aws call rather than relied on
# ambiently. When unset we fall back to "default" so the flag is always present.
AWS_PROFILE="${AWS_PROFILE:-default}"
AWS_PROFILE_ARGS=(--profile "$AWS_PROFILE")

CONFIRM=""

usage() {
    cat <<'USAGE'
Usage: teardown-operations.sh --confirm delete-operations

  --confirm delete-operations   Required confirmation token. Without it this
                                script does nothing.

Deletes only the CloudFormation stack. Durable audit data (DynamoDB table, KMS
key) is RETAINED by policy and is never erased by this script. Removing retained
audit data is a separate, explicit, manual future step.
This script is never called by teardown-all.sh; run it by hand.
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --confirm) shift; CONFIRM="${1:-}" ;;
        -h|--help) usage; exit 0 ;;
        *) echo "❌ Unknown argument: $1" >&2; usage; exit 2 ;;
    esac
    shift
done

if [ "$CONFIRM" != "delete-operations" ]; then
    echo "❌ Refusing to tear down without --confirm delete-operations." >&2
    usage
    exit 3
fi

if ! command -v aws >/dev/null 2>&1; then
    echo "❌ AWS CLI not found." >&2
    exit 1
fi

echo "=================================================="
echo " 🧨 Tearing down OPTIONAL E1 operations stack"
echo "=================================================="
echo "Region: $AWS_REGION"
echo "Stack:  $STACK_NAME"
echo ""

echo "🔐 Verifying AWS credentials and region before any write ..."
echo "   AWS_PROFILE=${AWS_PROFILE}  AWS_REGION=${AWS_REGION}"
if ! CALLER_IDENTITY="$(aws sts get-caller-identity "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" --output text 2>/dev/null)"; then
    echo "❌ Unable to verify caller identity. Configure AWS_PROFILE/AWS_REGION and credentials." >&2
    exit 4
fi
echo "   Caller identity: $CALLER_IDENTITY"

if ! aws cloudformation describe-stacks "${AWS_PROFILE_ARGS[@]}" --stack-name "$STACK_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
    echo "⚠️  Stack $STACK_NAME does not exist; nothing to do."
    exit 0
fi

echo "ℹ️  Durable resources (DynamoDB table, KMS key) use a Retain policy and"
echo "    will be LEFT IN PLACE with their audit data intact. This script does"
echo "    not erase audit data; that is a separate, explicit, manual future step."

echo "🗑️  Deleting CloudFormation stack $STACK_NAME ..."
aws cloudformation delete-stack "${AWS_PROFILE_ARGS[@]}" --stack-name "$STACK_NAME" --region "$AWS_REGION"
aws cloudformation wait stack-delete-complete "${AWS_PROFILE_ARGS[@]}" --stack-name "$STACK_NAME" --region "$AWS_REGION" || true

echo "✅ Stack deletion requested. Retained audit data is unaffected."
