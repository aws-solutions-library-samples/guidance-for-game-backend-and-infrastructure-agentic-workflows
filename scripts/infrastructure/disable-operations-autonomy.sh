#!/usr/bin/env bash
#
# disable-operations-autonomy.sh — reversible EMERGENCY disable of the E5
# bounded-autonomy control plane (GitHub issue #440).
#
# This is lever-flipping only: it sets AutonomyMode=disabled while keeping
# Provisioned=true on BOTH the 07 executor pre-write hook and the 09 evaluator
# plane, so every autonomy lever engages — the EventBridge schedule rule is set
# to DISABLED (no timed evaluation), the injected GBAW_OPERATIONS_AUTONOMY_ENABLED
# =false / GBAW_OPERATIONS_MODE make the evaluator fail closed at startup, and
# the executor's pre-write autonomy hook is closed — WITHOUT deleting any
# resource or data. It rebuilds NO code, runs NO Docker, uploads NO artifact,
# and reuses each stack's existing parameter values. It is fully reversible via
# deploy-operations-autonomy.sh --enable.
#
# ORDERING (Finding 4): the 07 executor pre-write hook is closed and VERIFIED
# FIRST, because an already-started Step Functions execution can still reach the
# single provider write through the executor; only once the pre-write hook is
# observed disabled do we disable the 09 evaluator plane. IDEMPOTENCY: an
# already-disabled stack ("No updates are to be performed") is treated as
# success for THAT plane, so a retry can repair a previously failed close.
# FAIL-CLOSED: the script returns nonzero and NEVER prints an unconditional
# success unless BOTH planes are observed AutonomyMode=disabled at the end.
#
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="game-agent"
STACK_NAME="${PROJECT_NAME}-operations-autonomy"
EXECUTION_STACK_NAME="${PROJECT_NAME}-operations-execution"

AWS_PROFILE="${AWS_PROFILE:-default}"
AWS_PROFILE_ARGS=(--profile "$AWS_PROFILE")

usage() {
    cat <<'USAGE'
Usage: disable-operations-autonomy.sh --confirm

  Emergency, data-preserving disable of the E5 autonomy planes. Closes and
  VERIFIES the 07 executor pre-write hook FIRST, then disables the 09 evaluator
  plane; keeps Provisioned=true and every resource under CloudFormation (stable
  physical names, retained data), reuses all current parameter values, and sets
  only AutonomyMode=disabled so BOTH levers fail closed. Idempotent: an
  already-disabled plane is treated as success so a retry repairs a partial
  disable. Does NOT delete resources and does NOT rebuild code. Returns nonzero
  unless BOTH planes are observed disabled. Reversible via
  deploy-operations-autonomy.sh --enable.

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

# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #

# stack_exists NAME -> 0 if the CloudFormation stack exists.
stack_exists() {
    aws cloudformation describe-stacks \
        "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
        --stack-name "$1" >/dev/null 2>&1
}

# stack_autonomy_mode NAME -> prints the AutonomyMode parameter value (or empty).
stack_autonomy_mode() {
    aws cloudformation describe-stacks \
        "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
        --stack-name "$1" \
        --query "Stacks[0].Parameters[?ParameterKey=='AutonomyMode'].ParameterValue" \
        --output text 2>/dev/null || true
}

# disable_stack NAME KEY1 KEY2 ...
#   Sets AutonomyMode=disabled (and Provisioned=true when that parameter is
#   declared) on stack NAME, preserving every other CURRENTLY-DECLARED parameter
#   with UsePreviousValue=true. Derives the preserve list from the LIVE stack so
#   it stays correct as a template evolves. Idempotent: a "No updates are to be
#   performed" result is success (already disabled). Returns nonzero on any real
#   update failure and on a waiter that does not reach UPDATE_COMPLETE.
disable_stack() {
    local name="$1"
    local keys params err rc
    keys="$(aws cloudformation describe-stacks \
        "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
        --stack-name "$name" \
        --query 'Stacks[0].Parameters[].ParameterKey' \
        --output text 2>/dev/null || true)"
    if [ -z "$keys" ]; then
        echo "   ⚠️  Could not read $name parameters." >&2
        return 1
    fi
    params=("ParameterKey=AutonomyMode,ParameterValue=disabled")
    local key
    for key in $keys; do
        case "$key" in
            AutonomyMode) : ;;  # set explicitly above
            Provisioned) params+=("ParameterKey=Provisioned,ParameterValue=true") ;;
            *) params+=("ParameterKey=${key},UsePreviousValue=true") ;;
        esac
    done
    err="$(mktemp)"
    if aws cloudformation update-stack \
        "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
        --stack-name "$name" \
        --use-previous-template \
        --capabilities CAPABILITY_NAMED_IAM \
        --parameters "${params[@]}" 2>"$err"; then
        rm -f "$err"
        # Wait for the update to reach a terminal state; a rolled-back update
        # must NOT be treated as a successful disable.
        if aws cloudformation wait stack-update-complete \
            "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
            --stack-name "$name"; then
            return 0
        fi
        echo "   ⚠️  $name update did not reach UPDATE_COMPLETE." >&2
        return 1
    fi
    rc=$?
    if grep -q "No updates are to be performed" "$err" 2>/dev/null; then
        rm -f "$err"
        return 0  # already disabled — idempotent success for this plane
    fi
    cat "$err" >&2 || true
    rm -f "$err"
    return "$rc"
}

# --------------------------------------------------------------------------- #
# Finding 4, phase 1: close and VERIFY the 07 executor pre-write hook FIRST.
#
# A missing 07 stack is FATAL unless the 09 plane is already unprovisioned or
# absent (nothing can execute), because a present-but-unclosable executor is
# exactly the window we must refuse to leave open.
# --------------------------------------------------------------------------- #
NINE_PRESENT="false"
if stack_exists "$STACK_NAME"; then
    NINE_PRESENT="true"
fi

echo "🛑 Phase 1: closing the 07 executor pre-write autonomy hook FIRST ..."
if stack_exists "$EXECUTION_STACK_NAME"; then
    if ! disable_stack "$EXECUTION_STACK_NAME"; then
        echo "❌ Could not close the 07 executor pre-write hook; an in-flight execution" >&2
        echo "   could still take an autonomous write. Refusing to proceed. Re-run once" >&2
        echo "   the 07 stack is stable." >&2
        exit 6
    fi
    EXEC_MODE="$(stack_autonomy_mode "$EXECUTION_STACK_NAME")"
    if [ "$EXEC_MODE" != "disabled" ]; then
        echo "❌ 07 executor AutonomyMode is '$EXEC_MODE', not 'disabled' after the update." >&2
        exit 6
    fi
    echo "   ✅ 07 executor pre-write hook observed disabled."
else
    if [ "$NINE_PRESENT" = "true" ]; then
        echo "❌ The 09 evaluator plane is provisioned but the 07 executor stack" >&2
        echo "   '$EXECUTION_STACK_NAME' is missing, so the pre-write hook cannot be closed." >&2
        echo "   Refusing to claim a safe disable. Restore/point at the 07 stack and re-run." >&2
        exit 6
    fi
    echo "   No 07 executor stack and no 09 plane; nothing can execute."
fi

# --------------------------------------------------------------------------- #
# Finding 4, phase 2: disable and VERIFY the 09 evaluator plane.
# --------------------------------------------------------------------------- #
echo "🛑 Phase 2: disabling the 09 evaluator plane ..."
if [ "$NINE_PRESENT" = "true" ]; then
    if ! disable_stack "$STACK_NAME"; then
        echo "❌ Could not disable the 09 evaluator plane. The 07 pre-write hook is" >&2
        echo "   already closed; re-run to complete the evaluator-plane disable." >&2
        exit 7
    fi
    NINE_MODE="$(stack_autonomy_mode "$STACK_NAME")"
    if [ "$NINE_MODE" != "disabled" ]; then
        echo "❌ 09 evaluator AutonomyMode is '$NINE_MODE', not 'disabled' after the update." >&2
        exit 7
    fi
    echo "   ✅ 09 evaluator plane observed disabled."
else
    echo "   No 09 evaluator stack found; nothing to disable on the evaluator plane."
fi

# --------------------------------------------------------------------------- #
# Both planes are now observed disabled (or provably unable to execute). Only
# here do we print the success claim.
# --------------------------------------------------------------------------- #
echo "✅ Autonomy is disabled: the 07 pre-write hook and the 09 evaluator plane are"
echo "   both observed AutonomyMode=disabled. All levers fail closed; resources and"
echo "   data are retained (nothing deleted)."
echo "   Re-enable is reversible:"
echo "     GBAW_OPERATIONS_MODE=operate GBAW_OPERATIONS_AUTONOMY_CONFIRM=operate deploy-operations-autonomy.sh --enable ..."
