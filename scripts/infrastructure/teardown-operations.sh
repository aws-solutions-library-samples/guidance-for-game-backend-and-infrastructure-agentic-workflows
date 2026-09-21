#!/bin/bash
# Game Agent - OPTIONAL E1 operations observation control plane teardown wrapper
# (GitHub issue #413).
#
# Teardown is EXPLICIT and is NEVER invoked automatically. teardown-all.sh /
# scripts/teardown.sh do not call this script. It requires an explicit
# confirmation token and, because the DynamoDB table and content bucket are
# retained by policy in production, it will NOT force-delete durable audit data
# without a second explicit acknowledgement.

set -euo pipefail

AWS_REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="game-agent"
STACK_NAME="${PROJECT_NAME}-operations"

CONFIRM=""
DELETE_DATA="false"

usage() {
    cat <<'USAGE'
Usage: teardown-operations.sh --confirm delete-operations [--delete-data]

  --confirm delete-operations   Required confirmation token. Without it this
                                script does nothing.
  --delete-data                 Additionally empty and remove the retained
                                DynamoDB table and content bucket. Omit to keep
                                durable audit data (the safe default).

This script is never called by teardown-all.sh; run it by hand.
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --confirm) shift; CONFIRM="${1:-}" ;;
        --delete-data) DELETE_DATA="true" ;;
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

if ! aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
    echo "⚠️  Stack $STACK_NAME does not exist; nothing to do."
    exit 0
fi

if [ "$DELETE_DATA" != "true" ]; then
    echo "ℹ️  Durable resources (DynamoDB table, content bucket, KMS key) use a"
    echo "    Retain policy and will be left in place. Re-run with --delete-data"
    echo "    to remove them after confirming the audit data is no longer needed."
fi

echo "🗑️  Deleting CloudFormation stack $STACK_NAME ..."
aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$AWS_REGION"
aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$AWS_REGION" || true

echo "✅ Stack deletion requested. Retained resources (if any) are unaffected."
