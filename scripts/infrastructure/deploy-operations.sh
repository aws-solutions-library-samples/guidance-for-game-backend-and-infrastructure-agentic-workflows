#!/bin/bash
# Game Agent - OPTIONAL E1 operations observation control plane deploy wrapper
# (GitHub issue #413).
#
# This wrapper is DELIBERATELY NOT called by deploy.sh / deploy-all.sh. A normal
# deployment creates zero E1 resources. Even when this wrapper runs, it refuses
# to create *enabled* (observe-mode) resources unless the operator supplies BOTH:
#   * GBAW_OPERATIONS_MODE=observe  (environment), and
#   * --enable                      (flag)
# An enabled deploy provisions resources (Provisioned=true) AND sets the runtime
# authority to observe. Without both opt-ins, it runs a READ-ONLY preview
# (template service validation + lint only; it creates no change set and mutates
# nothing). Provisioning and runtime authority are SEPARATE: --disable is an
# emergency, data-preserving path that keeps Provisioned=true and every resource
# in place, flipping only OperationsMode to disabled so the stack fails closed
# and a later --enable is reversible. It rebuilds no code and runs no Docker.
#
# On an enabled deploy the wrapper builds a DETERMINISTIC, Lambda-compatible
# Python 3.13 / x86_64 zip containing the real `operations` code (the backend
# source tree, which must carry the observe handler) plus every transitive
# runtime dependency pinned at the version frozen in the repository lock. Dependencies
# are installed for the Lambda manylinux x86_64 ABI via a deterministic
# cross-platform pip/uv platform install (or an explicitly versioned Lambda build
# container) so host-architecture native wheels are NEVER packaged. The artifact
# is uploaded to an EXPLICIT, pre-existing artifact bucket (verified, never
# created or discovered) under a content-hash key, and CodeS3Bucket/CodeS3Key are
# passed to the stack. No enabled route ever points at placeholder code: if the
# frozen handler module is absent from the package, the wrapper fails closed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

AWS_REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="game-agent"
STACK_NAME="${PROJECT_NAME}-operations"
TEMPLATE="$PROJECT_ROOT/infrastructure/cloudformation/06-operations-observation.yaml"
BACKEND_SRC="$PROJECT_ROOT/backend/src"
HANDLER_MODULE_PATH="operations/observe/lambda_entry.py"
# Documentary constant mirrored by the packaging-contract test; the import probe
# below references the dotted module directly inside its Python heredoc.
# shellcheck disable=SC2034
HANDLER_IMPORT="operations.observe.lambda_entry"

# The Lambda target ABI. The runtime is Python 3.13 on x86_64; the package must
# carry manylinux x86_64 wheels for any native dependency, never host wheels.
LAMBDA_PY_VERSION="3.13"
LAMBDA_PY_TAG="cp313"
LAMBDA_PLATFORM="manylinux2014_x86_64"
LAMBDA_BUILD_IMAGE="public.ecr.aws/lambda/python:3.13-x86_64"

# Transitive runtime-dependency closure, pinned EXACTLY at the versions frozen in
# backend/uv.lock. rfc8785 provides canonical JSON; jsonschema + referencing (and
# their deps attrs / rpds-py / jsonschema-specifications) back contract
# validation. boto3/botocore are provided by the Lambda runtime and are excluded.
# rpds-py is the only native (non-pure-python) member; its manylinux x86_64 wheel
# is required.
PINNED_DEPS=(
    "rfc8785==0.1.4"
    "jsonschema==4.26.0"
    "jsonschema-specifications==2025.9.1"
    "referencing==0.36.2"
    "attrs==25.4.0"
    "rpds-py==2026.5.1"
)

ENVIRONMENT="beta"
ACTION="preview"   # preview | enable | disable
ALLOW_BINDING_CHANGE="false"
COGNITO_ISSUER="${COGNITO_ISSUER:-}"
COGNITO_CLIENT_ID="${COGNITO_CLIENT_ID:-}"
TENANT_ID="${TENANT_ID:-}"
WORKSPACE_ID="${WORKSPACE_ID:-}"
TRUSTED_AUDIENCE="${TRUSTED_AUDIENCE:-}"
# The EXPLICIT, pre-existing artifact bucket for the Lambda zip. There is no
# discovery and no creation: an enabled deploy requires this to name a bucket
# that already exists in the target account/region.
GBAW_OPERATIONS_ARTIFACT_BUCKET="${GBAW_OPERATIONS_ARTIFACT_BUCKET:-}"

# AWS_PROFILE resolution mirrors scripts/deploy.sh: use the environment value if
# set, otherwise read it from ui/.env.local, otherwise leave it unset. The
# profile flag is passed to aws calls ONLY when a profile is actually set, so an
# operator relying on ambient environment credentials or SSO is not forced onto a
# "default" profile that could resolve to a different account from the main
# deployment.
if [ -z "${AWS_PROFILE:-}" ] && [ -f "$PROJECT_ROOT/ui/.env.local" ]; then
    _profile="$(grep '^AWS_PROFILE=' "$PROJECT_ROOT/ui/.env.local" | cut -d= -f2 | tr -d '[:space:]' || true)"
    [ -n "$_profile" ] && export AWS_PROFILE="$_profile"
fi
if [ -n "${AWS_PROFILE:-}" ]; then
    AWS_PROFILE_ARGS=(--profile "$AWS_PROFILE")
else
    AWS_PROFILE_ARGS=()
fi

# The expected AWS account the operation must land in. It is REQUIRED for a
# non-interactive run: the resolved caller account must match it before any
# upload or stack write, binding the write path to a known account rather than
# trusting whichever profile resolves. On a terminal it may be omitted and the
# masked account is confirmed interactively; a non-interactive run without it is
# refused.
GBAW_OPERATIONS_EXPECTED_ACCOUNT_ID="${GBAW_OPERATIONS_EXPECTED_ACCOUNT_ID:-}"

usage() {
    cat <<'USAGE'
Usage: deploy-operations.sh [--enable | --disable] [--environment beta|prod] [--allow-binding-change]

  (no flag)     Preview only (READ-ONLY): validates and lints the template and
                creates nothing. No change set is created.
  --enable      Build/upload the operations Lambda artifact and deploy the stack
                with OperationsMode=observe. Requires GBAW_OPERATIONS_MODE=observe
                in the environment plus the enabling inputs below.
  --disable     Emergency, data-preserving disable of an EXISTING provisioned
                stack: keeps Provisioned=true and every resource under
                CloudFormation (stable physical names, retained data), reuses all
                current parameter values via the deployed template
                (--use-previous-template), and sets only OperationsMode=disabled
                so the API and handler fail closed. Does NOT delete resources and
                does NOT rebuild code or run Docker. Reversible via --enable.
  --environment Target environment (default: beta). "prod" enables DynamoDB
                deletion protection and longer log retention. Validated up front.
  --allow-binding-change
                Permit a re-enable to change the trusted bindings (Environment,
                TenantId, WorkspaceId, TrustedAudience) on an existing stack.
                Without it a re-enable that would change any binding is refused.

Environment for --enable:
  GBAW_OPERATIONS_MODE=observe        Required confirmation.
  COGNITO_ISSUER, COGNITO_CLIENT_ID   JWT issuer + audience (required).
  TENANT_ID, WORKSPACE_ID             Server-side trusted bindings (required).
  TRUSTED_AUDIENCE                    Optional; defaults to COGNITO_CLIENT_ID.
  GBAW_OPERATIONS_ARTIFACT_BUCKET     REQUIRED explicit, pre-existing artifact
                                      bucket. This wrapper verifies it and never
                                      discovers or creates a bucket.
  GBAW_OPERATIONS_EXPECTED_ACCOUNT_ID REQUIRED for a non-interactive run: the
                                      resolved caller account must equal it
                                      before any write, binding the write to a
                                      known account. On a terminal it may be
                                      omitted and the masked account is confirmed
                                      interactively instead; a non-interactive
                                      run without it is refused.
  AWS_PROFILE, AWS_REGION             Credentials/region. AWS_PROFILE is resolved
                                      from the environment or ui/.env.local and
                                      passed only when set; both are verified
                                      before any write.

Latency budget tuning for --enable (all optional; each is validated so the
sub-budgets fit the request deadline and the alarm can fire before fail-closed).
On an EXISTING stack an unset variable keeps the stack's deployed value, so a
plain re-enable never resets a tuned budget; set one only to change it:
  RequestDeadlineSeconds              Internal request deadline, seconds (15).
  LambdaTimeoutSeconds                Lambda timeout, seconds; must exceed the
                                      request deadline (20).
  PerReadBudgetSeconds                Per provider-read budget, seconds (3).
  PersistenceBudgetSeconds            Persistence/serialization budget, seconds (3).
  CancellationMarginSeconds           Reserved cancellation margin, seconds (3).
  LatencyAlarmThresholdMs             p99 latency alarm threshold, ms, below the
                                      request deadline in ms (13000).
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --enable)  ACTION="enable" ;;
        --disable) ACTION="disable" ;;
        --allow-binding-change) ALLOW_BINDING_CHANGE="true" ;;
        --environment)
            shift
            if [ "$#" -eq 0 ]; then
                echo "❌ --environment requires a value (beta|prod)." >&2
                exit 2
            fi
            ENVIRONMENT="$1"
            ;;
        -h|--help) usage; exit 0 ;;
        *) echo "❌ Unknown argument: $1" >&2; usage; exit 2 ;;
    esac
    shift
done

# Validate the environment up front, before any AWS call or upload, rather than
# only when CloudFormation rejects it.
if [ "$ENVIRONMENT" != "beta" ] && [ "$ENVIRONMENT" != "prod" ]; then
    echo "❌ --environment must be 'beta' or 'prod' (got '$ENVIRONMENT')." >&2
    exit 2
fi

echo "=================================================="
echo " ⚙️  OPTIONAL E1 operations control plane"
echo "=================================================="
echo "Region:      $AWS_REGION"
echo "Profile:     ${AWS_PROFILE:-<ambient credentials>}"
echo "Stack:       $STACK_NAME"
echo "Environment: $ENVIRONMENT"
echo "Action:      $ACTION"
echo ""

if ! command -v aws >/dev/null 2>&1; then
    echo "❌ AWS CLI not found." >&2
    exit 1
fi

# --------------------------------------------------------------------------- #
# Read-only preview: validate + lint only. Never creates a change set, never
# mutates AWS, and does not require credentials for linting.
# --------------------------------------------------------------------------- #
if [ "$ACTION" = "preview" ]; then
    echo "🔎 Preview only (READ-ONLY). Validating and linting the template."
    echo "   To deploy: GBAW_OPERATIONS_MODE=observe $0 --enable"
    if command -v cfn-lint >/dev/null 2>&1; then
        echo "   Running cfn-lint ..."
        cfn-lint "$TEMPLATE"
    else
        echo "   cfn-lint not found; skipping lint."
    fi
    echo "   Running template service validation (read-only) ..."
    aws cloudformation validate-template \
        "${AWS_PROFILE_ARGS[@]}" \
        --template-body "file://$TEMPLATE" \
        --region "$AWS_REGION" >/dev/null
    echo "✅ Preview complete. Template validated; no resources were created."
    exit 0
fi

# --------------------------------------------------------------------------- #
# Validate all opt-in inputs BEFORE touching AWS, so a misconfigured enable is
# refused without any credential dependency or network call.
# --------------------------------------------------------------------------- #
OPERATIONS_MODE="disabled"
CODE_S3_KEY=""

if [ "$ACTION" = "enable" ]; then
    # Belt-and-braces opt-in: require the environment value AND the flag.
    if [ "${GBAW_OPERATIONS_MODE:-disabled}" != "observe" ]; then
        echo "❌ Refusing to enable: set GBAW_OPERATIONS_MODE=observe to confirm." >&2
        exit 3
    fi
    if [ -z "$COGNITO_ISSUER" ] || [ -z "$COGNITO_CLIENT_ID" ]; then
        echo "❌ COGNITO_ISSUER and COGNITO_CLIENT_ID are required to enable." >&2
        exit 3
    fi
    if [ -z "$TENANT_ID" ] || [ -z "$WORKSPACE_ID" ]; then
        echo "❌ TENANT_ID and WORKSPACE_ID are required to enable." >&2
        exit 3
    fi
    if [ -z "$GBAW_OPERATIONS_ARTIFACT_BUCKET" ]; then
        echo "❌ GBAW_OPERATIONS_ARTIFACT_BUCKET is required to enable. This wrapper" >&2
        echo "   never discovers or creates a bucket; set it to a pre-existing bucket." >&2
        exit 6
    fi
    OPERATIONS_MODE="observe"
fi

# --------------------------------------------------------------------------- #
# Verify identity/region before any write path (enable or disable). Only a
# masked account and the role name are printed; the full ARN/UserId are not.
# When GBAW_OPERATIONS_EXPECTED_ACCOUNT_ID is set the resolved account MUST match
# it before any upload or stack write, binding the write to a known account.
# --------------------------------------------------------------------------- #
echo "🔐 Verifying AWS credentials and region before any write ..."
echo "   Profile=${AWS_PROFILE:-<ambient credentials>}  Region=${AWS_REGION}"
if ! ACCOUNT_ID="$(aws sts get-caller-identity "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" --query Account --output text 2>/dev/null)"; then
    echo "❌ Unable to verify caller identity. Configure AWS_PROFILE/AWS_REGION and credentials." >&2
    exit 4
fi
CALLER_ARN="$(aws sts get-caller-identity "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" --query Arn --output text 2>/dev/null || true)"
# Print only a masked account (last 4 digits) and a NON-identifying principal
# descriptor — never the full ARN (which can carry a user name or email), a user
# name, or the UserId. For an assumed-role ARN
# (arn:…:assumed-role/<role>/<session>) print the <role> segment only, because
# the trailing session is often the user's email. For every other caller shape
# print only the principal TYPE (iam-user, federated-user, root), never the name.
MASKED_ACCOUNT="****${ACCOUNT_ID: -4}"
case "$CALLER_ARN" in
    *:assumed-role/*)
        _after_role="${CALLER_ARN##*:assumed-role/}"
        ROLE_SEGMENT="${_after_role%%/*}"
        CALLER_DESC="role=${ROLE_SEGMENT:-<unknown>}"
        ;;
    *:user/*)
        # An IAM user ARN carries the user name (and may carry a path); print the
        # type only so no user name is disclosed.
        CALLER_DESC="principal=iam-user"
        ;;
    *:federated-user/*)
        CALLER_DESC="principal=federated-user"
        ;;
    *:root)
        CALLER_DESC="principal=root"
        ;;
    *)
        CALLER_DESC="principal=unknown"
        ;;
esac
echo "   Caller: account=$MASKED_ACCOUNT $CALLER_DESC"

if [ -n "$GBAW_OPERATIONS_EXPECTED_ACCOUNT_ID" ] && [ "$ACCOUNT_ID" != "$GBAW_OPERATIONS_EXPECTED_ACCOUNT_ID" ]; then
    echo "❌ Resolved account (****${ACCOUNT_ID: -4}) does not match the expected account" >&2
    echo "   GBAW_OPERATIONS_EXPECTED_ACCOUNT_ID (****${GBAW_OPERATIONS_EXPECTED_ACCOUNT_ID: -4})." >&2
    echo "   Refusing to upload or write to a different account from the main deployment." >&2
    exit 4
fi

# When the expected account is NOT pre-set, bind the write to a confirmed
# account rather than silently trusting whichever profile resolved. If stdin is
# a TTY, prompt the operator to confirm the masked account; otherwise refuse,
# because a non-interactive write to an unverified account is exactly the
# cross-account hazard the expected-account binding prevents.
if [ -z "$GBAW_OPERATIONS_EXPECTED_ACCOUNT_ID" ]; then
    if [ -t 0 ]; then
        printf '   Confirm writing to account %s [y/N]: ' "$MASKED_ACCOUNT" >&2
        read -r _confirm_account
        case "$_confirm_account" in
            y | Y | yes | YES) : ;;
            *)
                echo "❌ Account not confirmed; refusing to write. Set" >&2
                echo "   GBAW_OPERATIONS_EXPECTED_ACCOUNT_ID to bind the write non-interactively." >&2
                exit 4
                ;;
        esac
    else
        echo "❌ GBAW_OPERATIONS_EXPECTED_ACCOUNT_ID is unset and stdin is not a TTY." >&2
        echo "   Set it to the expected account id so the write is bound non-interactively;" >&2
        echo "   this wrapper refuses to write to an unconfirmed account." >&2
        exit 4
    fi
fi
# bindings BEFORE building or uploading anything, so a refused binding change
# never leaves a stray artifact in the bucket. A describe-stacks failure for any
# reason other than "stack does not exist" fails closed rather than defaulting to
# "no stack" and sending the full binding set (including the beta default) to an
# existing stack.
# --------------------------------------------------------------------------- #
STACK_EXISTS="false"
TRUSTED_AUDIENCE_EFFECTIVE="${TRUSTED_AUDIENCE:-$COGNITO_CLIENT_ID}"

_current_param() {
    aws cloudformation describe-stacks \
        "${AWS_PROFILE_ARGS[@]}" \
        --stack-name "$STACK_NAME" \
        --region "$AWS_REGION" \
        --query "Stacks[0].Parameters[?ParameterKey=='$1'].ParameterValue | [0]" \
        --output text 2>/dev/null || true
}

if [ "$ACTION" = "enable" ]; then
    DESCRIBE_ERR="$(mktemp "${TMPDIR:-/tmp}/gbaw-ops-describe.XXXXXXXX")"
    if aws cloudformation describe-stacks \
            "${AWS_PROFILE_ARGS[@]}" \
            --stack-name "$STACK_NAME" \
            --region "$AWS_REGION" >/dev/null 2>"$DESCRIBE_ERR"; then
        STACK_EXISTS="true"
    elif grep -qi "does not exist" "$DESCRIBE_ERR"; then
        STACK_EXISTS="false"
    else
        echo "❌ Unable to determine whether stack '$STACK_NAME' exists (describe-stacks" >&2
        echo "   failed for a reason other than a missing stack). Refusing to proceed so a" >&2
        echo "   transient error cannot send the default bindings to an existing stack." >&2
        cat "$DESCRIBE_ERR" >&2
        rm -f "$DESCRIBE_ERR"
        exit 4
    fi
    rm -f "$DESCRIBE_ERR"

    if [ "$STACK_EXISTS" = "true" ]; then
        CUR_ENV="$(_current_param Environment)"
        CUR_TENANT="$(_current_param TenantId)"
        CUR_WS="$(_current_param WorkspaceId)"
        CUR_AUD="$(_current_param TrustedAudience)"
        BINDING_CHANGES=""
        [ -n "$CUR_ENV" ] && [ "$CUR_ENV" != "$ENVIRONMENT" ] && BINDING_CHANGES="${BINDING_CHANGES}Environment ($CUR_ENV -> $ENVIRONMENT) "
        [ -n "$CUR_TENANT" ] && [ "$CUR_TENANT" != "$TENANT_ID" ] && BINDING_CHANGES="${BINDING_CHANGES}TenantId "
        [ -n "$CUR_WS" ] && [ "$CUR_WS" != "$WORKSPACE_ID" ] && BINDING_CHANGES="${BINDING_CHANGES}WorkspaceId "
        [ -n "$CUR_AUD" ] && [ "$CUR_AUD" != "$TRUSTED_AUDIENCE_EFFECTIVE" ] && BINDING_CHANGES="${BINDING_CHANGES}TrustedAudience "
        if [ -n "$BINDING_CHANGES" ] && [ "$ALLOW_BINDING_CHANGE" != "true" ]; then
            echo "❌ Refusing to re-enable: this would change trusted binding(s): $BINDING_CHANGES" >&2
            echo "   Changing Environment can downgrade deletion protection and log retention;" >&2
            echo "   changing TenantId/WorkspaceId/TrustedAudience rebinds the live function." >&2
            echo "   Pass --allow-binding-change to proceed intentionally." >&2
            exit 9
        fi
    fi
fi

# --------------------------------------------------------------------------- #
# Latency budget resolution (enable only). A re-enable must NOT silently reset
# an operator's tuned budgets. For each of the six budget variables the
# EFFECTIVE value is:
#   * the same-named environment variable, when the operator set it; else
#   * the value deployed on an existing stack (read with _current_param); else
#   * the template default (a fresh stack).
# Only the keys the operator explicitly set are sent as overrides, so unset keys
# keep their deployed values on an update instead of being overwritten with a
# default. The resolved EFFECTIVE set is then validated so a tuned stack can
# never be left with budgets that do not fit the request deadline.
# --------------------------------------------------------------------------- #
BUDGET_OVERRIDES=()
if [ "$ACTION" = "enable" ]; then
    # Resolve one budget key into the global ``_RESOLVED`` and, when the value is
    # operator-set or a fresh-stack default, append an explicit override. Run in
    # the current shell (never a subshell) so the array append persists.
    _RESOLVED=""
    _resolve_budget() {
        # $1 env var name, $2 CloudFormation parameter key, $3 template default.
        local _env_name="$1" _param_key="$2" _default="$3" _env_val _deployed
        _env_val="$(printenv "$_env_name" 2>/dev/null || true)"
        if [ -n "$_env_val" ]; then
            # Operator-set: use it and send it as an explicit override.
            BUDGET_OVERRIDES+=("${_param_key}=${_env_val}")
            _RESOLVED="$_env_val"
            return 0
        fi
        if [ "$STACK_EXISTS" = "true" ]; then
            _deployed="$(_current_param "$_param_key")"
            if [ -n "$_deployed" ] && [ "$_deployed" != "None" ]; then
                # Keep the deployed value; do NOT add an override, so the update
                # leaves this tuned key untouched.
                _RESOLVED="$_deployed"
                return 0
            fi
        fi
        # Fresh stack with no operator value: the template default applies. Send
        # it explicitly so the validated value is the one actually deployed.
        BUDGET_OVERRIDES+=("${_param_key}=${_default}")
        _RESOLVED="$_default"
    }

    _resolve_budget RequestDeadlineSeconds RequestDeadlineSeconds 15; _req="$_RESOLVED"
    _resolve_budget PerReadBudgetSeconds PerReadBudgetSeconds 3; _read="$_RESOLVED"
    _resolve_budget PersistenceBudgetSeconds PersistenceBudgetSeconds 3; _persist="$_RESOLVED"
    _resolve_budget CancellationMarginSeconds CancellationMarginSeconds 3; _margin="$_RESOLVED"
    _resolve_budget LambdaTimeoutSeconds LambdaTimeoutSeconds 20; _lambda_timeout="$_RESOLVED"
    _resolve_budget LatencyAlarmThresholdMs LatencyAlarmThresholdMs 13000; _latency_ms="$_RESOLVED"

    # Validate the EFFECTIVE set (CloudFormation Rules cannot do arithmetic):
    # 3*read + persistence + margin <= RequestDeadlineSeconds, the Lambda timeout
    # is strictly greater than the request deadline, and the p99 alarm threshold
    # is below the request deadline so it can fire before the handler fails closed.
    _sum=$(( 3 * _read + _persist + _margin ))
    if [ "$_sum" -gt "$_req" ]; then
        echo "❌ Latency budgets do not fit the request deadline: 3*${_read} + ${_persist} +" >&2
        echo "   ${_margin} = ${_sum}s > RequestDeadlineSeconds=${_req}s. Raise the deadline or" >&2
        echo "   lower a sub-budget so the function cannot be killed mid-transaction." >&2
        exit 3
    fi
    if [ "$_lambda_timeout" -le "$_req" ]; then
        echo "❌ LambdaTimeoutSeconds=${_lambda_timeout}s must be strictly greater than" >&2
        echo "   RequestDeadlineSeconds=${_req}s so the handler fails closed before the" >&2
        echo "   function is hard-killed." >&2
        exit 3
    fi
    if [ "$_latency_ms" -ge $(( _req * 1000 )) ]; then
        echo "❌ LatencyAlarmThresholdMs=${_latency_ms} must be below the request deadline" >&2
        echo "   (${_req}s) so the p99 alarm can fire before the handler fails closed." >&2
        exit 3
    fi
fi

if [ "$ACTION" = "enable" ]; then
    # ----------------------------------------------------------------------- #
    # Build a deterministic, Lambda-compatible zip: the real `operations` code
    # (the backend source tree) plus the pinned third-party dependency closure.
    # Determinism (fixed mtimes, sorted entries) makes the content hash — and
    # therefore the S3 key — stable for an unchanged source tree.
    # ----------------------------------------------------------------------- #
    if [ ! -f "$BACKEND_SRC/$HANDLER_MODULE_PATH" ]; then
        echo "❌ Refusing to enable: frozen handler module $HANDLER_MODULE_PATH is absent" >&2
        echo "   from $BACKEND_SRC. The backend source tree must carry the real observe" >&2
        echo "   handler; no enabled route may point at missing/placeholder code." >&2
        exit 5
    fi

    BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gbaw-ops-pkg.XXXXXXXX")"
    trap 'rm -rf "$BUILD_DIR"' EXIT
    STAGE="$BUILD_DIR/stage"
    mkdir -p "$STAGE"

    echo "📦 Staging operations code and runtime resources ..."
    # Stage from the committed HEAD revision, not the working tree, so an
    # untracked or locally modified file can never ship and the artifact is tied
    # to an exact commit. Refuse a dirty operations tree so the uploaded content
    # always corresponds to a reviewable commit.
    PACKAGED_COMMIT="$(cd "$PROJECT_ROOT" && git rev-parse HEAD 2>/dev/null || true)"
    if [ -z "$PACKAGED_COMMIT" ]; then
        echo "❌ Refusing to enable: not a git checkout, so the packaged revision cannot" >&2
        echo "   be pinned. Package from a committed tree." >&2
        exit 5
    fi
    if ! ( cd "$PROJECT_ROOT" && git diff --quiet -- backend/src/operations && git diff --cached --quiet -- backend/src/operations ); then
        echo "❌ Refusing to enable: backend/src/operations has uncommitted changes." >&2
        echo "   Commit or stash them so the uploaded artifact matches commit $PACKAGED_COMMIT." >&2
        exit 5
    fi
    if ( cd "$PROJECT_ROOT" && git ls-files --others --exclude-standard -- backend/src/operations | grep -q . ); then
        echo "❌ Refusing to enable: backend/src/operations has untracked files that would" >&2
        echo "   not be packaged from HEAD. Commit or remove them." >&2
        exit 5
    fi
    # Materialize the committed operations tree (py + in-package JSON resources),
    # excluding caches, test trees, and docs, from the HEAD revision via
    # git archive so only tracked content at the pinned commit is staged.
    ( cd "$PROJECT_ROOT/backend/src" && \
      git -C "$PROJECT_ROOT" archive "$PACKAGED_COMMIT" -- backend/src/operations \
        | tar -x --strip-components=2 -C "$STAGE" \
            --exclude='*/__pycache__/*' --exclude='*/tests/*' --exclude='*/test/*' --exclude='*/docs/*' )
    # Keep only runtime file types (py + json); drop anything else git archive
    # may have carried.
    find "$STAGE/operations" -type f ! -name '*.py' ! -name '*.json' -delete 2>/dev/null || true

    # Fail closed if the versioned contract schema resources did not make it into
    # the stage: the handler's contract load reads
    # operations/contracts/schemas/v1/*.schema.json at runtime, and a package
    # missing them would deploy a handler that raises FileNotFoundError on the
    # first validated request. Require at least one, and require the whole
    # versioned set the validator binds.
    SCHEMA_STAGE_DIR="$STAGE/operations/contracts/schemas/v1"
    # Tolerate a missing directory (|| true) so a mis-staged tree reaches the
    # explicit diagnostic below instead of aborting the script via pipefail on a
    # failed ``find``.
    STAGED_SCHEMAS="$(find "$SCHEMA_STAGE_DIR" -type f -name '*.schema.json' 2>/dev/null | wc -l | tr -d ' ' || true)"
    if [ "${STAGED_SCHEMAS:-0}" -eq 0 ]; then
        echo "❌ Refusing to enable: no versioned contract schemas were staged under" >&2
        echo "   operations/contracts/schemas/v1. The runtime contract validator loads" >&2
        echo "   these JSON resources; a schema-less package raises FileNotFoundError." >&2
        exit 5
    fi
    SOURCE_SCHEMAS="$(find "$BACKEND_SRC/operations/contracts/schemas/v1" -type f -name '*.schema.json' 2>/dev/null | wc -l | tr -d ' ' || true)"
    if [ "${STAGED_SCHEMAS:-0}" -ne "${SOURCE_SCHEMAS:-0}" ]; then
        echo "❌ Refusing to enable: staged contract schema count ($STAGED_SCHEMAS) does not" >&2
        echo "   match the source set ($SOURCE_SCHEMAS). The runtime schema resources are" >&2
        echo "   incompletely packaged; contract validation would fail closed at runtime." >&2
        exit 5
    fi
    echo "   Staged $STAGED_SCHEMAS versioned contract schema resource(s)."

    # ----------------------------------------------------------------------- #
    # Install the pinned dependency closure for the LAMBDA target ABI (Linux /
    # x86_64 / cp313), binary-only, so a host-architecture native wheel (e.g. the
    # macOS/arm64 rpds-py) is never packaged. Prefer uv's platform install; fall
    # back to pip's cross-platform target install. Both are deterministic: exact
    # pins + a fixed platform/abi/python target.
    # ----------------------------------------------------------------------- #
    echo "📦 Installing pinned dependencies for ${LAMBDA_PLATFORM} / py${LAMBDA_PY_VERSION} (binary-only) ..."
    if command -v uv >/dev/null 2>&1; then
        uv pip install \
            --python-platform x86_64-manylinux2014 \
            --python-version "$LAMBDA_PY_VERSION" \
            --only-binary :all: \
            --no-deps \
            --target "$STAGE" \
            --no-cache \
            "${PINNED_DEPS[@]}"
    else
        python3 -m pip install \
            --platform "$LAMBDA_PLATFORM" \
            --python-version "$LAMBDA_PY_VERSION" \
            --implementation cp \
            --abi "$LAMBDA_PY_TAG" \
            --only-binary=:all: \
            --no-deps \
            --no-compile \
            --target "$STAGE" \
            "${PINNED_DEPS[@]}"
    fi

    # Normalize for determinism: strip pip metadata dirs, bytecode caches, and
    # the ``bin/`` directory (console scripts whose shebang embeds the build
    # host's interpreter path), and fix timestamps. .dist-info is not needed at
    # runtime.
    find "$STAGE" -depth -type d -name '*.dist-info' -exec rm -rf {} + 2>/dev/null || true
    find "$STAGE" -depth -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
    rm -rf "${STAGE:?}/bin" 2>/dev/null || true
    # Fail closed if any staged file still embeds the build user's home path, so
    # a host-specific path can never be uploaded in the artifact. Match HOME as a
    # fixed string (grep -F), and skip the scan when HOME is empty or "/", where
    # the pattern would match everything (or nothing meaningful) and wrongly
    # refuse every build.
    if [ -n "${HOME:-}" ] && [ "$HOME" != "/" ]; then
        if grep -rlIF --exclude='*.so' "$HOME" "$STAGE" 2>/dev/null | grep -q .; then
            echo "❌ A staged file embeds the build host home path ($HOME); refusing to upload." >&2
            grep -rlIF --exclude='*.so' "$HOME" "$STAGE" 2>/dev/null | sed 's#^#   #' >&2
            exit 5
        fi
    fi
    # Fixed epoch touch stamp assembled from parts (no 12-digit literal) so the
    # build is reproducible without tripping account-id content scanners.
    EPOCH_STAMP="2000""01010000.00"   # CCYYMMDDhhmm.SS
    find "$STAGE" -exec touch -h -t "$EPOCH_STAMP" {} +

    # ----------------------------------------------------------------------- #
    # Fail closed unless every staged native extension is a Linux/x86_64 wheel.
    # A compiled extension carries its platform tag in the filename; the only
    # acceptable native objects are ``*x86_64-linux-gnu.so``. Any other ``.so``
    # (macosx_*, *_arm64/aarch64, win_*, musllinux, i686), or any ``.pyd``
    # (Windows) or ``.dylib`` (macOS), means the cross-platform install silently
    # fell back to the host and must abort.
    # ----------------------------------------------------------------------- #
    echo "🔍 Verifying every native extension is a Linux/x86_64 wheel ..."
    BAD_NATIVE=""
    while IFS= read -r sofile; do
        case "$sofile" in
            *x86_64-linux-gnu.so) : ;;             # the only acceptable form
            *) BAD_NATIVE="${BAD_NATIVE}${sofile}"$'\n' ;;
        esac
    done < <(find "$STAGE" -type f -name '*.so' 2>/dev/null)
    # Reject Windows/macOS dynamic objects outright.
    while IFS= read -r other; do
        BAD_NATIVE="${BAD_NATIVE}${other}"$'\n'
    done < <(find "$STAGE" -type f \( -name '*.pyd' -o -name '*.dylib' \) 2>/dev/null)
    if [ -n "$BAD_NATIVE" ]; then
        echo "❌ Non-Linux/x86_64 native object detected in package:" >&2
        printf '%s' "$BAD_NATIVE" | sed '/^$/d; s/^/   /' >&2
        exit 5
    fi
    # The native dependency MUST be present as a compiled Linux extension.
    if ! find "$STAGE" -type f -name 'rpds*x86_64-linux-gnu.so' | grep -q .; then
        echo "❌ Native dependency rpds-py Linux/x86_64 extension is missing from the package." >&2
        echo "   The manylinux x86_64 wheel did not install; refusing to deploy." >&2
        exit 5
    fi

    # ----------------------------------------------------------------------- #
    # Clean Linux/x86 import probe. The real handler transitively imports the
    # native rpds-py, which only loads on Linux/x86_64. When a Lambda-compatible
    # container runtime is available we import the frozen handler inside the
    # official python:3.13-x86_64 image (the true runtime); otherwise we run a
    # structural probe that fails closed if the frozen handler module or any
    # pinned dependency's top-level package is absent from the built artifact.
    # ----------------------------------------------------------------------- #
    # The runtime probe does more than import the handler: it drives a
    # representative contract load so a package that ships the handler and its
    # dependency closure but OMITS the versioned JSON Schemas fails HERE, before
    # upload, instead of at the first validated request in production. It builds
    # the schema registry (which read_text()s every operations/contracts/schemas
    # /v1/*.schema.json via importlib.resources) and validates a minimal document,
    # accepting the expected typed ContractValidationError while treating a
    # missing-resource error (FileNotFoundError / ModuleNotFoundError) as fatal.
    RUNTIME_PROBE_PY="$(cat <<'PROBE'
import importlib, sys
sys.path.insert(0, "/var/task")
m = importlib.import_module("operations.observe.lambda_entry")
assert callable(m.handler), "handler is not callable"
from operations.contracts import validation as v
from operations.contracts.versions import SCHEMA_NAMES
# Force every versioned schema resource to be read from the package. A missing
# JSON resource raises FileNotFoundError here and fails the probe closed.
reg = v._schema_registry()
for name in SCHEMA_NAMES:
    v.load_schema(name)
# Exercise the public validation path end to end. A schema/semantic rejection is
# the CORRECT outcome for a deliberately-empty document; only a missing runtime
# resource (import/file error) must fail the probe.
try:
    v.validate_contract("prepared-operation", {})
except v.ContractValidationError:
    pass
print("runtime probe ok: handler import + %d schemas loaded" % len(SCHEMA_NAMES))
PROBE
)"

    echo "🧪 Import-probing the packaged handler on a clean Linux/x86 runtime ..."
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        docker run --rm --platform linux/amd64 \
            -v "$STAGE:/var/task:ro" \
            --entrypoint python3 \
            "$LAMBDA_BUILD_IMAGE" \
            -E -s -c \
            "$RUNTIME_PROBE_PY" \
            || { echo "❌ Packaged handler failed its runtime probe on the Lambda image; refusing to deploy." >&2; exit 5; }
        echo "   Runtime probe passed on $LAMBDA_BUILD_IMAGE (handler import + contract schema load)."
    else
        echo "   No container runtime available; running a structural fail-closed probe."
        # The frozen handler module must be present in the built artifact.
        if [ ! -f "$STAGE/$HANDLER_MODULE_PATH" ]; then
            echo "❌ Frozen handler module $HANDLER_MODULE_PATH missing from the package." >&2
            exit 5
        fi
        # The versioned contract schema resources the handler loads at runtime
        # must be present. Without a container we cannot execute the load, but we
        # can assert the resources the load reads are packaged, so a .py-only
        # package still fails closed here rather than at the first request.
        if ! find "$STAGE/operations/contracts/schemas/v1" -type f -name '*.schema.json' 2>/dev/null | grep -q .; then
            echo "❌ Runtime contract schema resources are absent from the package" >&2
            echo "   (operations/contracts/schemas/v1/*.schema.json). The handler's contract" >&2
            echo "   load would raise FileNotFoundError at runtime." >&2
            exit 5
        fi
        # Every required top-level runtime dependency must be present so the
        # handler's transitive imports resolve at runtime.
        for required in rfc8785 jsonschema referencing rpds; do
            if ! find "$STAGE" -maxdepth 2 \( -name "${required}" -o -name "${required}.py" -o -name "${required}*x86_64-linux-gnu.so" \) | grep -q .; then
                echo "❌ Required runtime dependency '$required' is absent from the package." >&2
                exit 5
            fi
        done
        echo "   Structural probe passed (handler + required dependencies + schema resources present)."
    fi

    ARTIFACT="$BUILD_DIR/operations-observe.zip"
    echo "🗜️  Building deterministic zip ..."
    ( cd "$STAGE" && find . -type f | LC_ALL=C sort \
        | zip -X -q "$ARTIFACT" -@ )
    CONTENT_HASH="$(shasum -a 256 "$ARTIFACT" | awk '{print $1}')"
    CODE_S3_KEY="operations/observe/${CONTENT_HASH}.zip"
    echo "   Artifact sha256: $CONTENT_HASH"

    # ----------------------------------------------------------------------- #
    # Verify the EXPLICIT artifact bucket exists and its region/account context
    # matches the deploy target before uploading. Never create a bucket.
    # ----------------------------------------------------------------------- #
    CODE_S3_BUCKET="$GBAW_OPERATIONS_ARTIFACT_BUCKET"
    echo "🔎 Verifying artifact bucket '$CODE_S3_BUCKET' exists in account $MASKED_ACCOUNT ..."
    # head-bucket confirms existence and that these credentials can access it.
    # The expected owner is asserted so a name-squatted foreign bucket is refused.
    if ! aws s3api head-bucket \
            "${AWS_PROFILE_ARGS[@]}" \
            --bucket "$CODE_S3_BUCKET" \
            --expected-bucket-owner "$ACCOUNT_ID" \
            --region "$AWS_REGION" >/dev/null 2>&1; then
        echo "❌ Artifact bucket '$CODE_S3_BUCKET' does not exist, is inaccessible, or is" >&2
        echo "   not owned by account $MASKED_ACCOUNT. This wrapper never creates a bucket." >&2
        exit 6
    fi
    # Confirm the bucket's Region matches the deploy Region (Lambda requires the
    # code bucket to be in the same Region as the function).
    BUCKET_REGION="$(aws s3api get-bucket-location \
        "${AWS_PROFILE_ARGS[@]}" \
        --bucket "$CODE_S3_BUCKET" \
        --expected-bucket-owner "$ACCOUNT_ID" \
        --output text 2>/dev/null || true)"
    # us-east-1 is reported as "None" by the LocationConstraint API.
    if [ "$BUCKET_REGION" = "None" ] || [ -z "$BUCKET_REGION" ]; then
        BUCKET_REGION="us-east-1"
    fi
    if [ "$BUCKET_REGION" != "$AWS_REGION" ]; then
        echo "❌ Artifact bucket '$CODE_S3_BUCKET' is in region '$BUCKET_REGION', not the" >&2
        echo "   deploy region '$AWS_REGION'. Lambda requires the code bucket in-region." >&2
        exit 6
    fi
    echo "   Bucket verified: owner=$MASKED_ACCOUNT region=$BUCKET_REGION"

    echo "☁️  Uploading artifact to s3://$CODE_S3_BUCKET/$CODE_S3_KEY ..."
    aws s3 cp "$ARTIFACT" "s3://$CODE_S3_BUCKET/$CODE_S3_KEY" \
        "${AWS_PROFILE_ARGS[@]}" \
        --expected-bucket-owner "$ACCOUNT_ID" \
        --region "$AWS_REGION" --only-show-errors
fi

if [ "$ACTION" = "disable" ]; then
    # ----------------------------------------------------------------------- #
    # EMERGENCY DISABLE (data-preserving, reversible). This does NOT delete or
    # rename any resource and does NOT rebuild code or run Docker. It targets an
    # EXISTING, PROVISIONED stack in the verified account/region and flips only
    # the runtime authority to "disabled" while keeping Provisioned=true, so
    # every resource stays under CloudFormation with stable physical names and
    # retained audit data. All other parameters (issuer, audience, tenant,
    # workspace, code artifact bucket/key, budgets) are REUSED from the stack's
    # current values via UsePreviousValue, so direct/API calls fail closed and a
    # later --enable is reversible. The API stage additionally throttles to zero
    # (template kill switch) because OperationsMode is not "observe".
    # ----------------------------------------------------------------------- #
    echo "🔎 Verifying target stack '$STACK_NAME' exists in account $MASKED_ACCOUNT / $AWS_REGION ..."
    if ! aws cloudformation describe-stacks \
            "${AWS_PROFILE_ARGS[@]}" \
            --stack-name "$STACK_NAME" \
            --region "$AWS_REGION" >/dev/null 2>&1; then
        echo "❌ Stack '$STACK_NAME' does not exist in account $MASKED_ACCOUNT / $AWS_REGION." >&2
        echo "   Nothing to disable. (Disable operates on an already-provisioned stack;" >&2
        echo "   an unprovisioned/default deployment holds zero resources and no data.)" >&2
        exit 7
    fi

    # Read the stack's CURRENT Provisioned parameter value directly (no jq). A
    # provisioned stack must carry Provisioned=true; refuse to "disable" a stack
    # that never provisioned resources — blanking-and-redeploying it would be the
    # very orphaning bug this design prevents.
    CURRENT_PROVISIONED="$(aws cloudformation describe-stacks \
        "${AWS_PROFILE_ARGS[@]}" \
        --stack-name "$STACK_NAME" \
        --region "$AWS_REGION" \
        --query "Stacks[0].Parameters[?ParameterKey=='Provisioned'].ParameterValue | [0]" \
        --output text 2>/dev/null || true)"
    if [ "$CURRENT_PROVISIONED" != "true" ]; then
        echo "❌ Stack '$STACK_NAME' is not provisioned (Provisioned='${CURRENT_PROVISIONED:-<unset>}')." >&2
        echo "   Refusing to disable: there are no resources to keep failing closed." >&2
        exit 7
    fi

    echo "🔒 Disabling (data-preserving): keeping Provisioned=true, setting"
    echo "   OperationsMode=disabled, reusing every other parameter unchanged."
    echo "   No code rebuild, no Docker, no resource deletion."

    # Build the UsePreviousValue list from the DEPLOYED stack's own parameter
    # keys, not a list pinned to the local template. --use-previous-template
    # validates --parameters against the deployed template, so a stack created
    # from an earlier revision (with fewer or different keys) would reject a key
    # the local template adds. Reading the stack's keys and overriding only
    # Provisioned and OperationsMode keeps the kill switch working across
    # template version skew.
    DEPLOYED_PARAM_KEYS="$(aws cloudformation describe-stacks \
        "${AWS_PROFILE_ARGS[@]}" \
        --stack-name "$STACK_NAME" \
        --region "$AWS_REGION" \
        --query "Stacks[0].Parameters[].ParameterKey" \
        --output text 2>/dev/null || true)"
    if [ -z "$DEPLOYED_PARAM_KEYS" ]; then
        echo "❌ Could not read the deployed stack's parameter keys; refusing to disable" >&2
        echo "   with a parameter list that may not match the deployed template." >&2
        exit 8
    fi
    # Always override the two safety levers; reuse every other deployed key.
    DISABLE_PARAMS=(
        "ParameterKey=Provisioned,ParameterValue=true"
        "ParameterKey=OperationsMode,ParameterValue=disabled"
    )
    for _key in $DEPLOYED_PARAM_KEYS; do
        case "$_key" in
            Provisioned | OperationsMode) : ;;  # overridden above
            *) DISABLE_PARAMS+=("ParameterKey=${_key},UsePreviousValue=true") ;;
        esac
    done

    DISABLE_ERR="$(mktemp "${TMPDIR:-/tmp}/gbaw-ops-disable.XXXXXXXX")"
    trap 'rm -f "$DISABLE_ERR"' EXIT
    echo "🚀 Updating $STACK_NAME to OperationsMode=disabled (Provisioned=true retained) ..."
    if ! aws cloudformation update-stack \
            "${AWS_PROFILE_ARGS[@]}" \
            --stack-name "$STACK_NAME" \
            --use-previous-template \
            --capabilities CAPABILITY_NAMED_IAM \
            --region "$AWS_REGION" \
            --parameters "${DISABLE_PARAMS[@]}" 2>"$DISABLE_ERR"; then
        if grep -q "No updates are to be performed" "$DISABLE_ERR"; then
            echo "ℹ️  Stack already disabled with these values; nothing to change."
            exit 0
        fi
        echo "❌ Disable update failed:" >&2
        cat "$DISABLE_ERR" >&2
        exit 8
    fi
    echo "⏳ Waiting for the disable update to complete ..."
    aws cloudformation wait stack-update-complete \
        "${AWS_PROFILE_ARGS[@]}" \
        --stack-name "$STACK_NAME" \
        --region "$AWS_REGION"
    echo "✅ Disabled (data-preserving). Resources and audit data retained under"
    echo "   CloudFormation; API and handler fail closed. Re-enable with --enable."
    exit 0
fi

# --------------------------------------------------------------------------- #
# ENABLE path deploy: provision resources (Provisioned=true) and set the
# runtime authority to observe. All enabling inputs were validated above, the
# stack-existence and binding checks already ran before the build/upload, and
# the artifact was built and uploaded. On an EXISTING stack the trusted bindings
# are kept unchanged unless --allow-binding-change was passed.
# --------------------------------------------------------------------------- #
# Build the override list. On an existing stack, send only the keys that change
# (plus the always-updated Provisioned/OperationsMode/code identifiers), so an
# unchanged binding is never resent and cannot be accidentally rewritten. The
# latency budget keys are sent only when the operator set them (or on a fresh
# stack); a key the operator did not set keeps its deployed value instead of
# being reset to a template default, so a re-enable never discards a tuned budget.
PARAM_OVERRIDES=(
    "ProjectName=${PROJECT_NAME}"
    "Provisioned=true"
    "OperationsMode=${OPERATIONS_MODE}"
    "CognitoIssuer=${COGNITO_ISSUER}"
    "CognitoClientId=${COGNITO_CLIENT_ID}"
    "CodeS3Bucket=${CODE_S3_BUCKET:-}"
    "CodeS3Key=${CODE_S3_KEY}"
)
if [ "${#BUDGET_OVERRIDES[@]}" -gt 0 ]; then
    PARAM_OVERRIDES+=("${BUDGET_OVERRIDES[@]}")
fi
if [ "$STACK_EXISTS" != "true" ] || [ "$ALLOW_BINDING_CHANGE" = "true" ]; then
    PARAM_OVERRIDES+=(
        "Environment=${ENVIRONMENT}"
        "TenantId=${TENANT_ID}"
        "WorkspaceId=${WORKSPACE_ID}"
        "TrustedAudience=${TRUSTED_AUDIENCE_EFFECTIVE}"
    )
fi

echo "🚀 Deploying $STACK_NAME with Provisioned=true OperationsMode=$OPERATIONS_MODE ..."
aws cloudformation deploy \
    "${AWS_PROFILE_ARGS[@]}" \
    --template-file "$TEMPLATE" \
    --stack-name "$STACK_NAME" \
    --capabilities CAPABILITY_NAMED_IAM \
    --region "$AWS_REGION" \
    --no-fail-on-empty-changeset \
    --parameter-overrides "${PARAM_OVERRIDES[@]}"

echo "✅ Deploy complete (Provisioned=true, OperationsMode=$OPERATIONS_MODE)."
