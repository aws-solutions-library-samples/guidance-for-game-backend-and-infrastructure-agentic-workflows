#!/usr/bin/env bash
# Game Agent - OPTIONAL E5 operations BOUNDED-AUTONOMY control plane teardown
# wrapper (GitHub issue #440).
#
# Teardown is EXPLICIT and is NEVER invoked automatically. teardown-all.sh /
# scripts/teardown.sh do not call this script. It requires an explicit
# confirmation token and deletes only the 09 CloudFormation stack.
#
# The E5 stack keeps NO data of its own: it operates on the 06 stack's DynamoDB
# table + KMS key (owned and retained by 06) and starts the 07 stack's executor
# workflow, none of which are touched here. The E5 evaluator log group uses a
# Retain deletion policy so its audit trail survives a teardown. If you want a
# rebuild-free, reversible OFF instead of a delete, use
# disable-operations-autonomy.sh (flips AutonomyMode=disabled, keeps everything).
# This teardown is a Provisioned=false decision, not a disable.

set -euo pipefail

AWS_REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="game-agent"
STACK_NAME="${PROJECT_NAME}-operations-autonomy"

AWS_PROFILE="${AWS_PROFILE:-default}"
AWS_PROFILE_ARGS=(--profile "$AWS_PROFILE")

CONFIRM=""

usage() {
    cat <<'USAGE'
Usage: teardown-operations-autonomy.sh --confirm delete-operations-autonomy

  --confirm delete-operations-autonomy   Required confirmation token. Without
                                         it this script does nothing.

Deletes only the 09 CloudFormation stack. The 06 operations table + KMS key and
the 07 executor workflow are NOT owned by this stack and are untouched. TWO 09
resources use a Retain policy and SURVIVE deletion: the E5 evaluator log group
AND the evaluator dead-letter queue. Both continue to incur (minimal) storage
cost until deleted by hand. Prefer disable-operations-autonomy.sh for a
reversible OFF. This script is never called by teardown-all.sh; run it by hand.
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

if [ "$CONFIRM" != "delete-operations-autonomy" ]; then
    echo "❌ Refusing to tear down without --confirm delete-operations-autonomy." >&2
    usage
    exit 3
fi

if ! command -v aws >/dev/null 2>&1; then
    echo "❌ AWS CLI not found." >&2
    exit 1
fi

echo "=================================================="
echo " 🧨 Tearing down OPTIONAL E5 operations autonomy stack"
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

echo "ℹ️  The 06 operations table + KMS key and the 07 executor workflow are NOT"
echo "    owned by this stack and are left untouched. TWO 09 resources use a"
echo "    Retain policy and SURVIVE: the evaluator log group AND the evaluator"
echo "    dead-letter queue. Both keep incurring minimal storage cost until"
echo "    deleted by hand."

echo "🗑️  Deleting CloudFormation stack $STACK_NAME ..."
aws cloudformation delete-stack "${AWS_PROFILE_ARGS[@]}" --stack-name "$STACK_NAME" --region "$AWS_REGION"
aws cloudformation wait stack-delete-complete "${AWS_PROFILE_ARGS[@]}" --stack-name "$STACK_NAME" --region "$AWS_REGION" || true

echo "✅ Stack deletion requested. The 06 data plane, 07 executor, and the retained"
echo "    E5 log group + dead-letter queue are unaffected."
