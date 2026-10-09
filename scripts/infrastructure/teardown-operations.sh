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
# is a separate, explicit, future cleanup performed by hand once the data is
# confirmed unneeded (empty and delete the retained table and disable the
# retained key). There is deliberately no flag here that erases audit data.

set -euo pipefail

AWS_REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="game-agent"
STACK_NAME="${PROJECT_NAME}-operations"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# AWS_PROFILE resolution mirrors scripts/deploy.sh: environment, then
# ui/.env.local, otherwise unset. The profile flag is passed only when set, so an
# operator on ambient credentials is not forced onto a "default" profile.
if [ -z "${AWS_PROFILE:-}" ] && [ -f "$PROJECT_ROOT/ui/.env.local" ]; then
    _profile="$(grep '^AWS_PROFILE=' "$PROJECT_ROOT/ui/.env.local" | cut -d= -f2 | tr -d '[:space:]' || true)"
    [ -n "$_profile" ] && export AWS_PROFILE="$_profile"
fi
if [ -n "${AWS_PROFILE:-}" ]; then
    AWS_PROFILE_ARGS=(--profile "$AWS_PROFILE")
else
    AWS_PROFILE_ARGS=()
fi

# When set, the resolved caller account must match before any stack write.
GBAW_OPERATIONS_EXPECTED_ACCOUNT_ID="${GBAW_OPERATIONS_EXPECTED_ACCOUNT_ID:-}"

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
echo "   Profile=${AWS_PROFILE:-<ambient credentials>}  Region=${AWS_REGION}"
if ! ACCOUNT_ID="$(aws sts get-caller-identity "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" --query Account --output text 2>/dev/null)"; then
    echo "❌ Unable to verify caller identity. Configure AWS_PROFILE/AWS_REGION and credentials." >&2
    exit 4
fi
echo "   Caller: account=****${ACCOUNT_ID: -4}"
MASKED_ACCOUNT="****${ACCOUNT_ID: -4}"
if [ -n "$GBAW_OPERATIONS_EXPECTED_ACCOUNT_ID" ] && [ "$ACCOUNT_ID" != "$GBAW_OPERATIONS_EXPECTED_ACCOUNT_ID" ]; then
    echo "❌ Resolved account (****${ACCOUNT_ID: -4}) does not match the expected account." >&2
    echo "   Refusing to tear down in a different account from the main deployment." >&2
    exit 4
fi
# When the expected account is NOT pre-set, bind the delete to a confirmed
# account: prompt on a TTY, else refuse (a non-interactive delete against an
# unverified account is the cross-account hazard the binding prevents).
if [ -z "$GBAW_OPERATIONS_EXPECTED_ACCOUNT_ID" ]; then
    if [ -t 0 ]; then
        printf '   Confirm deleting the operations stack in account %s [y/N]: ' "$MASKED_ACCOUNT" >&2
        read -r _confirm_account
        case "$_confirm_account" in
            y | Y | yes | YES) : ;;
            *)
                echo "❌ Account not confirmed; refusing to tear down. Set" >&2
                echo "   GBAW_OPERATIONS_EXPECTED_ACCOUNT_ID to bind the delete non-interactively." >&2
                exit 4
                ;;
        esac
    else
        echo "❌ GBAW_OPERATIONS_EXPECTED_ACCOUNT_ID is unset and stdin is not a TTY." >&2
        echo "   Set it to the expected account id so the teardown is bound non-interactively;" >&2
        echo "   this wrapper refuses to delete in an unconfirmed account." >&2
        exit 4
    fi
fi

if ! aws cloudformation describe-stacks "${AWS_PROFILE_ARGS[@]}" --stack-name "$STACK_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
    echo "⚠️  Stack $STACK_NAME does not exist; nothing to do."
    exit 0
fi

echo "ℹ️  Durable resources use a Retain policy and will be LEFT IN PLACE with"
echo "    their audit data intact. This script does not erase audit data; that is"
echo "    a separate, explicit, manual future step. The following physical"
echo "    resources are RETAINED after this teardown:"
# Enumerate the retained physical resources (table, KMS key, and BOTH log groups)
# so the operator sees exactly what remains and must clean up by hand later.
RETAINED="$(aws cloudformation describe-stack-resources \
    "${AWS_PROFILE_ARGS[@]}" \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" \
    --query "StackResources[?ResourceType=='AWS::DynamoDB::Table' || ResourceType=='AWS::KMS::Key' || ResourceType=='AWS::Logs::LogGroup'].[ResourceType,PhysicalResourceId]" \
    --output text 2>/dev/null || true)"
if [ -n "$RETAINED" ]; then
    printf '%s\n' "$RETAINED" | sed 's/^/      /'
else
    echo "      (unable to enumerate; the DynamoDB table, KMS key, and both log groups are retained by policy)"
fi

echo "🗑️  Deleting CloudFormation stack $STACK_NAME ..."
aws cloudformation delete-stack "${AWS_PROFILE_ARGS[@]}" --stack-name "$STACK_NAME" --region "$AWS_REGION"
echo "⏳ Waiting for stack deletion to complete ..."
# Do NOT swallow a delete failure: a DELETE_FAILED must surface a non-zero exit
# rather than being reported as success.
if ! aws cloudformation wait stack-delete-complete "${AWS_PROFILE_ARGS[@]}" --stack-name "$STACK_NAME" --region "$AWS_REGION"; then
    echo "❌ Stack deletion did not complete. Inspect the stack events; retained audit" >&2
    echo "   data (table, KMS key, log groups) is unaffected." >&2
    exit 8
fi

echo "✅ Stack deleted. Retained audit data (DynamoDB table, KMS key, log groups)"
echo "   is unaffected and must be removed by hand once confirmed unneeded. A"
echo "   later --enable fails until the retained fixed-name resources are deleted"
echo "   or imported, because the table and log-group names are stable."
