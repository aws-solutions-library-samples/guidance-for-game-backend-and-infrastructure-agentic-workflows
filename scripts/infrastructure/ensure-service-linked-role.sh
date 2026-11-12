#!/bin/bash
set -euo pipefail

# Ensure an AWS service-linked role exists, and is usable, before a stack needs it.
#
# Usage:
#   AWS_PROFILE=<profile> AWS_REGION=us-west-2 \
#     scripts/infrastructure/ensure-service-linked-role.sh <aws-service-name> <role-name>
#
# Example (deploy.sh Step 7, before the frontend stack):
#   ensure-service-linked-role.sh ecs.amazonaws.com AWSServiceRoleForECS
#
# Some AWS services create their service-linked role on first use. In an account
# that has never used the service, a CloudFormation resource can then fail
# because the role it just caused to be created is not yet usable (issue #772:
# ECS Express reported "Unable to assume the service linked role" 1.7 s after IAM
# created AWSServiceRoleForECS). This helper creates the role up front and waits
# for it to propagate:
#
#   - The role already exists: nothing is created and nothing waits.
#   - get-role reports NoSuchEntity: the role is created. A concurrent creation
#     ("has been taken in this account") counts as created.
#   - get-role fails for any other reason (AccessDenied, throttling, ...): the
#     helper fails closed without attempting a create, because it cannot tell
#     whether the role exists.
#   - After a create, get-role is polled (bounded) until the role is visible,
#     then the helper waits GBAW_SLR_PROPAGATION_SECONDS (default 15, 0-120).
#
# Failures print a bounded message naming only the operation and the AWS error
# code; raw provider output (which can carry account IDs and ARNs) is never echoed.

AWS_REGION="${AWS_REGION:-us-west-2}"

SLR_PROPAGATION_SECONDS="${GBAW_SLR_PROPAGATION_SECONDS:-15}"
# Bounded visibility poll after a create. The interval is configurable so tests
# can exercise the never-visible path without sleeping.
SLR_POLL_ATTEMPTS=10
SLR_POLL_SECONDS="${GBAW_SLR_POLL_SECONDS:-3}"

if [ $# -ne 2 ]; then
    echo "❌ Error: service name and role name required"
    echo "Usage: $0 <aws-service-name> <role-name>"
    exit 1
fi
SERVICE_NAME="$1"
ROLE_NAME="$2"

# Both values reach the AWS CLI and status messages; accept only the documented
# character sets so neither can carry anything else.
if ! [[ "$SERVICE_NAME" =~ ^[a-z0-9][a-z0-9.-]{0,127}$ ]]; then
    echo "❌ Error: invalid AWS service name"
    exit 1
fi
# Service-linked roles are always named AWSServiceRoleFor...; requiring the
# prefix keeps the helper from being pointed at an ordinary role by mistake.
if ! [[ "$ROLE_NAME" =~ ^AWSServiceRoleFor[A-Za-z0-9+=,.@_-]{1,47}$ ]]; then
    echo "❌ Error: invalid service-linked role name (it must start with AWSServiceRoleFor)"
    exit 1
fi

# Validate a whole-second setting as an integer in [0, max].
#   $1 setting name   $2 value   $3 maximum
validate_seconds() {
    local name="$1" value="$2" max="$3"
    if ! [[ "$value" =~ ^[0-9]{1,3}$ ]] || [ "$((10#$value))" -gt "$max" ]; then
        echo "❌ Error: ${name} must be an integer from 0 to ${max}"
        exit 1
    fi
}
validate_seconds GBAW_SLR_PROPAGATION_SECONDS "$SLR_PROPAGATION_SECONDS" 120
validate_seconds GBAW_SLR_POLL_SECONDS "$SLR_POLL_SECONDS" 30
SLR_PROPAGATION_SECONDS=$((10#$SLR_PROPAGATION_SECONDS))
SLR_POLL_SECONDS=$((10#$SLR_POLL_SECONDS))

# Print the AWS error code from "An error occurred (<Code>) ...", or "unknown".
error_code_of() {
    if [[ "$1" =~ \(([A-Za-z0-9]+)\) ]]; then
        printf '%s\n' "${BASH_REMATCH[1]}"
    else
        printf 'unknown\n'
    fi
}

# Run get-role. Sets GET_ROLE_OUTPUT and returns the CLI exit status.
get_role() {
    local code=0
    GET_ROLE_OUTPUT="$(aws iam get-role \
        --role-name "$ROLE_NAME" \
        --region "$AWS_REGION" \
        --query 'Role.RoleName' \
        --output text 2>&1)" || code=$?
    return "$code"
}

echo "🔍 Checking service-linked role ${ROLE_NAME} (${SERVICE_NAME})..."

if get_role; then
    echo "✅ Service-linked role ${ROLE_NAME} already exists"
    exit 0
fi

get_role_error="$(error_code_of "$GET_ROLE_OUTPUT")"
if [ "$get_role_error" != "NoSuchEntity" ]; then
    echo "❌ Read service-linked role ${ROLE_NAME} failed (${get_role_error})" >&2
    echo "   The deployment cannot tell whether the role exists, so it did not try to create it." >&2
    echo "   Allow iam:GetRole and iam:CreateServiceLinkedRole for this identity, or create" >&2
    echo "   the role once and re-run:" >&2
    echo "       aws iam create-service-linked-role --aws-service-name ${SERVICE_NAME}" >&2
    exit 1
fi

echo "ℹ️  Service-linked role ${ROLE_NAME} not found; creating it..."
create_code=0
create_output="$(aws iam create-service-linked-role \
    --aws-service-name "$SERVICE_NAME" \
    --region "$AWS_REGION" \
    --query 'Role.RoleName' \
    --output text 2>&1)" || create_code=$?

if [ "$create_code" -eq 0 ]; then
    echo "✅ Created service-linked role ${ROLE_NAME}"
else
    create_error="$(error_code_of "$create_output")"
    if [ "$create_error" = "InvalidInput" ] && [[ "$create_output" == *"has been taken in this account"* ]]; then
        # Another caller (often the AWS service itself) created it concurrently.
        echo "ℹ️  Service-linked role ${ROLE_NAME} was created concurrently"
    else
        echo "❌ Create service-linked role ${ROLE_NAME} failed (${create_error})" >&2
        echo "   Allow iam:CreateServiceLinkedRole for this identity, or create the role once and re-run:" >&2
        echo "       aws iam create-service-linked-role --aws-service-name ${SERVICE_NAME}" >&2
        exit 1
    fi
fi

# The role was just created: wait (bounded) until IAM returns it, then give it
# time to become usable by the service that will assume it.
visible=false
for ((attempt = 1; attempt <= SLR_POLL_ATTEMPTS; attempt++)); do
    if get_role; then
        visible=true
        break
    fi
    if [ "$attempt" -lt "$SLR_POLL_ATTEMPTS" ] && [ "$SLR_POLL_SECONDS" -gt 0 ]; then
        sleep "$SLR_POLL_SECONDS"
    fi
done

if [ "$visible" != true ]; then
    echo "❌ Service-linked role ${ROLE_NAME} not visible after ${SLR_POLL_ATTEMPTS} checks ($(error_code_of "$GET_ROLE_OUTPUT"))" >&2
    echo "   Re-run the deployment once IAM reports the role." >&2
    exit 1
fi

if [ "$SLR_PROPAGATION_SECONDS" -gt 0 ]; then
    echo "⏳ Waiting ${SLR_PROPAGATION_SECONDS}s for ${ROLE_NAME} to propagate..."
    sleep "$SLR_PROPAGATION_SECONDS"
fi
echo "✅ Service-linked role ${ROLE_NAME} ready"
