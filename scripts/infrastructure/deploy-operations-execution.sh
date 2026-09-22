#!/usr/bin/env bash
#
# deploy-operations-execution.sh — OPTIONAL E3 execution control plane
# (GitHub issue #415).
#
# Deploys the SEPARATE 07-operations-execution.yaml stack: a dispatcher Lambda
# -> Step Functions STANDARD state machine -> dedicated executor Lambda chain
# that performs bounded, pre-approved GameLift capacity remediation on exactly
# one enrolled fleet. It is default-UNPROVISIONED ($0). Provisioning + enabling
# is a deliberate, double-opt-in owner act; --disable is an emergency,
# rebuild-free, data-preserving path (see disable-operations-execution.sh).
#
# It builds TWO deterministic, Lambda-compatible zips (dispatcher and executor)
# from the real `operations` code, verifies the Lambda target ABI, uploads them
# to an EXPLICIT, pre-existing artifact bucket (verified, never created or
# discovered) under content-hash keys, and passes both keys to the stack. If the
# frozen handler modules are absent from the package, the wrapper fails closed.
#
# It NEVER mutates AWS on the preview path and requires an explicit, verified
# profile/account/region before any write.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

AWS_REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="game-agent"
STACK_NAME="${PROJECT_NAME}-operations-execution"
TEMPLATE="$PROJECT_ROOT/infrastructure/cloudformation/07-operations-execution.yaml"
BACKEND_SRC="$PROJECT_ROOT/backend/src"

# The two frozen handler modules this stack deploys. Each is import-probed and
# packaged into its own artifact.
DISPATCHER_MODULE_PATH="operations/execute/dispatcher_entry.py"
EXECUTOR_MODULE_PATH="operations/execute/executor_entry.py"
DISPATCHER_IMPORT="operations.execute.dispatcher_entry"
EXECUTOR_IMPORT="operations.execute.executor_entry"

# The Lambda target ABI. The runtime is Python 3.13 on x86_64; the package must
# carry manylinux x86_64 wheels for any native dependency, never host wheels.
LAMBDA_PY_VERSION="3.13"
LAMBDA_PY_TAG="cp313"
LAMBDA_PLATFORM="manylinux2014_x86_64"
LAMBDA_BUILD_IMAGE="public.ecr.aws/lambda/python:3.13-x86_64"

# Transitive runtime-dependency closure, pinned EXACTLY at the versions frozen
# in backend/uv.lock (same closure as the observe/advise handler; the executor
# reuses the contract validator and canonical JSON). boto3/botocore are provided
# by the Lambda runtime and excluded. rpds-py is the only native member.
PINNED_DEPS=(
    "rfc8785==0.1.4"
    "jsonschema==4.26.0"
    "jsonschema-specifications==2025.9.1"
    "referencing==0.36.2"
    "attrs==25.4.0"
    "rpds-py==2026.5.1"
)

ENVIRONMENT="beta"
ACTION="preview"   # preview | enable
# The only enabled runtime authority to request. Selected with --mode and
# double-confirmed by a MATCHING GBAW_OPERATIONS_MODE.
REQUESTED_MODE="remediate"
COGNITO_ISSUER="${COGNITO_ISSUER:-}"
COGNITO_CLIENT_ID="${COGNITO_CLIENT_ID:-}"
ENROLLED_FLEET_ID="${GBAW_OPERATIONS_ENROLLED_FLEET_ID:-}"
# Cross-stack data-plane bindings the 06 stack exports; the executor/dispatcher
# reuse the SAME table + CMK (no new data store, no new key).
OPERATIONS_TABLE_NAME="${GBAW_OPERATIONS_TABLE_NAME:-}"
OPERATIONS_KMS_KEY_ARN="${GBAW_OPERATIONS_KMS_KEY_ARN:-}"
# Server-side trusted identity binding (must match the 06 stack). Tenant and
# workspace are required to enable; trusted audience defaults to the JWT
# audience (CognitoClientId) in the template when left empty, and the enrolled
# location defaults to the deploy region.
OPERATIONS_TENANT_ID="${GBAW_OPERATIONS_TENANT_ID:-}"
OPERATIONS_WORKSPACE_ID="${GBAW_OPERATIONS_WORKSPACE_ID:-}"
OPERATIONS_TRUSTED_AUDIENCE="${GBAW_OPERATIONS_TRUSTED_AUDIENCE:-}"
ENROLLED_LOCATION="${GBAW_OPERATIONS_ENROLLED_LOCATION:-$AWS_REGION}"
# The EXPLICIT, pre-existing artifact bucket for the Lambda zips. No discovery,
# no creation.
GBAW_OPERATIONS_ARTIFACT_BUCKET="${GBAW_OPERATIONS_ARTIFACT_BUCKET:-}"

AWS_PROFILE="${AWS_PROFILE:-default}"
AWS_PROFILE_ARGS=(--profile "$AWS_PROFILE")

usage() {
    cat <<'USAGE'
Usage: deploy-operations-execution.sh [--enable [--mode remediate]]
                                      [--environment beta|prod]

  (no flag)     Preview only (READ-ONLY): validates and lints the template and
                creates nothing.
  --enable      Build/upload the dispatcher + executor artifacts and deploy the
                07 execution stack with ExecutionMode=remediate. Requires
                GBAW_OPERATIONS_MODE=remediate (double opt-in) plus the
                enabling inputs below.
  --mode        remediate (the only enabled execution mode). Default remediate.
  --environment Target environment (default: beta). "prod" lengthens log
                retention.

Environment for --enable:
  GBAW_OPERATIONS_MODE=remediate             Required confirmation; MUST match
                                             the selected --mode.
  COGNITO_ISSUER, COGNITO_CLIENT_ID          JWT issuer + audience (required).
  GBAW_OPERATIONS_TENANT_ID                  Server-side trusted tenant, matching
  GBAW_OPERATIONS_WORKSPACE_ID               the 06 stack (both required).
  GBAW_OPERATIONS_TRUSTED_AUDIENCE           Optional; defaults to CognitoClientId.
  GBAW_OPERATIONS_ENROLLED_LOCATION          Optional; defaults to AWS_REGION.
  GBAW_OPERATIONS_ENROLLED_FLEET_ID          The EXACT enrolled fleet id
                                             (fleet-...) (required).
  GBAW_OPERATIONS_TABLE_NAME                 06-exported operations table name
                                             (required).
  GBAW_OPERATIONS_KMS_KEY_ARN                06-exported operations CMK ARN
                                             (required).
  GBAW_OPERATIONS_ARTIFACT_BUCKET            REQUIRED explicit, pre-existing
                                             artifact bucket. Verified, never
                                             discovered or created.
  AWS_PROFILE, AWS_REGION                    Credentials/region, passed
                                             explicitly and verified before any
                                             write.
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --enable)  ACTION="enable" ;;
        --mode) shift; REQUESTED_MODE="${1:-remediate}" ;;
        --environment) shift; ENVIRONMENT="${1:-beta}" ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
    esac
    shift
done

# --------------------------------------------------------------------------- #
# Preview: read-only validate + lint. Never mutates AWS.
# --------------------------------------------------------------------------- #
if [ "$ACTION" = "preview" ]; then
    echo "🔎 Preview only (READ-ONLY). Validating and linting the 07 execution template."
    echo "   To deploy: GBAW_OPERATIONS_MODE=remediate $0 --enable"
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
# Enable gate: double opt-in + required inputs (fail closed before any AWS).
# --------------------------------------------------------------------------- #
if [ "$REQUESTED_MODE" != "remediate" ]; then
    echo "❌ Refusing to enable: --mode must be remediate, got '$REQUESTED_MODE'." >&2
    exit 3
fi
if [ "${GBAW_OPERATIONS_MODE:-}" != "remediate" ]; then
    echo "❌ Refusing to enable: GBAW_OPERATIONS_MODE must be 'remediate'" >&2
    echo "   (it must match the selected --mode $REQUESTED_MODE) as a double opt-in." >&2
    exit 3
fi
if [ -z "$COGNITO_ISSUER" ] || [ -z "$COGNITO_CLIENT_ID" ]; then
    echo "❌ Refusing to enable: COGNITO_ISSUER and COGNITO_CLIENT_ID are required." >&2
    exit 3
fi
if [ -z "$ENROLLED_FLEET_ID" ]; then
    echo "❌ Refusing to enable: GBAW_OPERATIONS_ENROLLED_FLEET_ID (the exact enrolled fleet) is required." >&2
    exit 3
fi
case "$ENROLLED_FLEET_ID" in
    fleet-*) : ;;
    *) echo "❌ Refusing to enable: GBAW_OPERATIONS_ENROLLED_FLEET_ID must be a fleet id (fleet-...)." >&2; exit 3 ;;
esac
if [ -z "$OPERATIONS_TABLE_NAME" ] || [ -z "$OPERATIONS_KMS_KEY_ARN" ]; then
    echo "❌ Refusing to enable: GBAW_OPERATIONS_TABLE_NAME and GBAW_OPERATIONS_KMS_KEY_ARN" >&2
    echo "   (the 06-exported table + CMK) are required for cross-stack reuse." >&2
    exit 3
fi
if [ -z "$OPERATIONS_TENANT_ID" ] || [ -z "$OPERATIONS_WORKSPACE_ID" ]; then
    echo "❌ Refusing to enable: GBAW_OPERATIONS_TENANT_ID and GBAW_OPERATIONS_WORKSPACE_ID" >&2
    echo "   (the server-side trusted identity binding, matching the 06 stack) are required." >&2
    exit 3
fi
if [ -z "$GBAW_OPERATIONS_ARTIFACT_BUCKET" ]; then
    echo "❌ Refusing to enable: GBAW_OPERATIONS_ARTIFACT_BUCKET (explicit, pre-existing" >&2
    echo "   artifact bucket) is required. This wrapper never discovers or creates a bucket." >&2
    exit 3
fi
if [ ! -f "$BACKEND_SRC/$DISPATCHER_MODULE_PATH" ] || [ ! -f "$BACKEND_SRC/$EXECUTOR_MODULE_PATH" ]; then
    echo "❌ Refusing to enable: a frozen handler module is absent from $BACKEND_SRC." >&2
    echo "   Expected $DISPATCHER_MODULE_PATH and $EXECUTOR_MODULE_PATH." >&2
    exit 5
fi

# --------------------------------------------------------------------------- #
# Verify identity/region and the explicit artifact bucket before any write.
# --------------------------------------------------------------------------- #
echo "🔐 Verifying AWS credentials and region before any write ..."
echo "   AWS_PROFILE=${AWS_PROFILE}  AWS_REGION=${AWS_REGION}"
if ! CALLER_IDENTITY="$(aws sts get-caller-identity "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" --output text 2>/dev/null)"; then
    echo "❌ Unable to verify caller identity. Configure AWS_PROFILE/AWS_REGION and credentials." >&2
    exit 4
fi
echo "   Caller identity: $CALLER_IDENTITY"
ACCOUNT_ID="$(printf '%s\n' "$CALLER_IDENTITY" | awk '{print $1}')"

echo "🪣 Verifying the explicit artifact bucket exists in this account/region (no discovery/creation) ..."
if ! aws s3api head-bucket \
        "${AWS_PROFILE_ARGS[@]}" \
        --bucket "$GBAW_OPERATIONS_ARTIFACT_BUCKET" \
        --region "$AWS_REGION" >/dev/null 2>&1; then
    echo "❌ Artifact bucket '$GBAW_OPERATIONS_ARTIFACT_BUCKET' is not reachable in account $ACCOUNT_ID / $AWS_REGION." >&2
    echo "   Create/authorize it out of band; this wrapper never creates or discovers a bucket." >&2
    exit 4
fi

# --------------------------------------------------------------------------- #
# Deterministic packaging: build ONE staged tree (real operations code + pinned
# deps for the Lambda ABI), then emit TWO content-hash-addressed zips whose only
# difference is their handler entry. Determinism (fixed mtimes, sorted entries)
# makes each content hash — and the S3 key — stable for an unchanged source.
# --------------------------------------------------------------------------- #
BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gbaw-ops-exec-pkg.XXXXXXXX")"
trap 'rm -rf "$BUILD_DIR"' EXIT
STAGE="$BUILD_DIR/stage"
mkdir -p "$STAGE"

echo "📦 Staging operations code and runtime resources ..."
( cd "$BACKEND_SRC" && \
  find operations -type f \
    \( -name '*.py' -o -name '*.json' \) \
    -not -path '*/__pycache__/*' \
    -not -path '*/tests/*' -not -path '*/test/*' \
    -not -path '*/docs/*' \
    -print0 \
    | tar --null -cf - --files-from=- ) | ( cd "$STAGE" && tar -xf - )

# The dispatcher and executor entry modules must be present in the stage.
for mod in "$DISPATCHER_MODULE_PATH" "$EXECUTOR_MODULE_PATH"; do
    if [ ! -f "$STAGE/$mod" ]; then
        echo "❌ Frozen handler module $mod missing from the staged package." >&2
        exit 5
    fi
done

echo "📦 Installing pinned dependencies for ${LAMBDA_PLATFORM} / py${LAMBDA_PY_VERSION} (binary-only) ..."
if command -v uv >/dev/null 2>&1; then
    uv pip install \
        --python-platform x86_64-manylinux2014 \
        --python-version "$LAMBDA_PY_VERSION" \
        --only-binary :all: \
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
        --no-compile \
        --target "$STAGE" \
        "${PINNED_DEPS[@]}"
fi

# Normalize for determinism.
find "$STAGE" -depth -type d -name '*.dist-info' -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -depth -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
EPOCH_STAMP="2000""01010000.00"   # CCYYMMDDhhmm.SS
find "$STAGE" -exec touch -h -t "$EPOCH_STAMP" {} +

echo "🔍 Verifying no host-architecture native wheels were packaged ..."
BAD_NATIVE="$(find "$STAGE" -type f -name '*.so' \
    \( -name '*macosx*' -o -name '*arm64*' -o -name '*aarch64*' -o -name '*win_*' -o -name '*_i686*' \) 2>/dev/null || true)"
if [ -n "$BAD_NATIVE" ]; then
    echo "❌ Host/non-x86_64 native wheel detected in package:" >&2
    printf '   %s\n' "$BAD_NATIVE" >&2
    exit 5
fi
if ! find "$STAGE" -type f -name 'rpds*.so' | grep -q .; then
    echo "❌ Native dependency rpds-py compiled extension is missing from the package." >&2
    exit 5
fi

# --------------------------------------------------------------------------- #
# Import-probe BOTH frozen handlers on a clean Linux/x86 runtime when available;
# otherwise a structural fail-closed probe. Probing the dispatcher_entry and
# executor_entry modules ensures a package that ships one but not the other, or
# omits a dependency, fails HERE before upload.
# --------------------------------------------------------------------------- #
RUNTIME_PROBE_PY="$(cat <<'PROBE'
import importlib, sys
sys.path.insert(0, "/var/task")
for name in ("operations.execute.dispatcher_entry", "operations.execute.executor_entry"):
    m = importlib.import_module(name)
    assert callable(m.handler), "%s handler is not callable" % name
# The executor reuses the contract validator; force every versioned schema to be
# read from the package so a schema-less artifact fails the probe closed.
from operations.contracts import validation as v
from operations.contracts.versions import SCHEMA_NAMES
for schema in SCHEMA_NAMES:
    v.load_schema(schema)
# The E3 execution contract lives in its own additive schema set (intent /
# result / verification). Load each so a package missing an E3 schema fails the
# probe closed BEFORE upload.
from operations.contracts.execution import EXECUTION_SCHEMA_NAMES, load_execution_schema
for schema in EXECUTION_SCHEMA_NAMES:
    load_execution_schema(schema)
# Identifier-only invocation contract probe: the executor accepts a payload of
# exactly {operation_id} and rejects any extra field or a missing id, so a
# packaged handler that widened the wire contract fails HERE.
from operations.execute.executor_service import ExecutionInvocation, ExecutorServiceError
assert ExecutionInvocation.from_payload({"operation_id": "op_probe_identifier_only"}).operation_id
for bad in ({}, {"operation_id": "op_probe", "fleet_id": "fleet-x"}, {"fleet_id": "fleet-x"}):
    try:
        ExecutionInvocation.from_payload(bad)
    except ExecutorServiceError:
        pass
    else:
        raise AssertionError("identifier-only invocation contract is not enforced: %r" % (bad,))
n = len(SCHEMA_NAMES) + len(EXECUTION_SCHEMA_NAMES)
print("runtime probe ok: dispatcher + executor import + %d schemas + identifier-only contract" % n)
PROBE
)"

echo "🧪 Import-probing the packaged dispatcher + executor on a clean Linux/x86 runtime ..."
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker run --rm --platform linux/amd64 \
        -v "$STAGE:/var/task:ro" \
        --entrypoint python3 \
        "$LAMBDA_BUILD_IMAGE" \
        -E -s -c \
        "$RUNTIME_PROBE_PY" \
        || { echo "❌ Packaged handlers failed their runtime probe on the Lambda image; refusing to deploy." >&2; exit 5; }
    echo "   Runtime probe passed on $LAMBDA_BUILD_IMAGE (dispatcher + executor import + schema load)."
else
    echo "   No container runtime available; running a structural fail-closed probe."
    for mod in "$DISPATCHER_MODULE_PATH" "$EXECUTOR_MODULE_PATH"; do
        if [ ! -f "$STAGE/$mod" ]; then
            echo "❌ Frozen handler module $mod missing from the package." >&2
            exit 5
        fi
    done
    if ! find "$STAGE/operations/contracts/schemas/v1" -type f -name '*.schema.json' 2>/dev/null | grep -q .; then
        echo "❌ Runtime contract schema resources are absent from the package." >&2
        exit 5
    fi
    for e3schema in \
        gamelift-capacity-execution-intent \
        gamelift-capacity-execution-result \
        gamelift-capacity-execution-verification; do
        if [ ! -f "$STAGE/operations/contracts/schemas/v1/${e3schema}.schema.json" ]; then
            echo "❌ E3 execution contract schema ${e3schema}.schema.json is absent from the package." >&2
            exit 5
        fi
    done
    for required in rfc8785 jsonschema referencing rpds; do
        if ! find "$STAGE" -maxdepth 2 \( -name "${required}" -o -name "${required}.py" -o -name "${required}*.so" \) | grep -q .; then
            echo "❌ Required runtime dependency '$required' is absent from the package." >&2
            exit 5
        fi
    done
    echo "   Structural probe passed (dispatcher + executor + dependencies + schema resources present)."
fi

# --------------------------------------------------------------------------- #
# Build the deterministic zip (single artifact carrying both handlers) and a
# content-hash key. Both handler entries live in the same package; the stack
# points each function at its own handler module, so ONE artifact addressed by
# ONE content hash serves both. Sorted entries + fixed mtimes make the zip
# reproducible and the sha256 stable for an unchanged source tree.
# --------------------------------------------------------------------------- #
ARTIFACT="$BUILD_DIR/operations-execution.zip"
echo "🗜️  Building deterministic zip ..."
( cd "$STAGE" && find . -type f | LC_ALL=C sort | zip -X -q "$ARTIFACT" -@ )

CONTENT_HASH="$(shasum -a 256 "$ARTIFACT" | awk '{print $1}')"
S3_KEY="operations-execution/${CONTENT_HASH}.zip"
echo "   Content hash: $CONTENT_HASH"
echo "   S3 key: $S3_KEY"

echo "⬆️  Uploading artifact to s3://$GBAW_OPERATIONS_ARTIFACT_BUCKET/$S3_KEY ..."
aws s3api put-object \
    "${AWS_PROFILE_ARGS[@]}" \
    --bucket "$GBAW_OPERATIONS_ARTIFACT_BUCKET" \
    --key "$S3_KEY" \
    --body "$ARTIFACT" \
    --region "$AWS_REGION" >/dev/null

# Both functions load the same content-hash-addressed artifact; they differ only
# by their Handler entry in the template.
DISPATCHER_S3_KEY="$S3_KEY"
EXECUTOR_S3_KEY="$S3_KEY"

# --------------------------------------------------------------------------- #
# Deploy the stack. Provisioned=true + ExecutionMode=remediate + the verified
# bindings. This is the only mutating CloudFormation call.
# --------------------------------------------------------------------------- #
echo "🚀 Deploying $STACK_NAME (Provisioned=true, ExecutionMode=remediate) ..."
aws cloudformation deploy \
    "${AWS_PROFILE_ARGS[@]}" \
    --region "$AWS_REGION" \
    --stack-name "$STACK_NAME" \
    --template-file "$TEMPLATE" \
    --capabilities CAPABILITY_NAMED_IAM \
    --no-fail-on-empty-changeset \
    --parameter-overrides \
        "ProjectName=$PROJECT_NAME" \
        "Provisioned=true" \
        "ExecutionMode=remediate" \
        "Environment=$ENVIRONMENT" \
        "CognitoIssuer=$COGNITO_ISSUER" \
        "CognitoClientId=$COGNITO_CLIENT_ID" \
        "OperationsTableName=$OPERATIONS_TABLE_NAME" \
        "OperationsKmsKeyArn=$OPERATIONS_KMS_KEY_ARN" \
        "EnrolledFleetId=$ENROLLED_FLEET_ID" \
        "EnrolledLocation=$ENROLLED_LOCATION" \
        "TenantId=$OPERATIONS_TENANT_ID" \
        "WorkspaceId=$OPERATIONS_WORKSPACE_ID" \
        "TrustedAudience=$OPERATIONS_TRUSTED_AUDIENCE" \
        "CodeS3Bucket=$GBAW_OPERATIONS_ARTIFACT_BUCKET" \
        "DispatcherCodeS3Key=$DISPATCHER_S3_KEY" \
        "ExecutorCodeS3Key=$EXECUTOR_S3_KEY"

echo "✅ Deployed $STACK_NAME with ExecutionMode=remediate on the enrolled fleet $ENROLLED_FLEET_ID."
