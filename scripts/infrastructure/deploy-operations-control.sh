#!/usr/bin/env bash
#
# deploy-operations-control.sh — OPTIONAL E4 operations CONTROL PLANE
# (GitHub issue #416).
#
# Deploys the SEPARATE 08-operations-control-plane.yaml stack: the deployment-
# wide AWS AppConfig kill-switch (application / environment / hosted JSON-schema-
# validated profile / safe default version / gradual + immediate deployment
# strategies with a CloudWatch monitor auto-rollback) plus the admin control
# Lambda/API and the periodic EventBridge freshness-expiry sweeper the E4
# backend frozen routes expect. It is default-UNPROVISIONED ($0). Provisioning +
# enabling is a deliberate, double-opt-in owner act; --disable is an emergency,
# rebuild-free, data-preserving path (see disable-operations-control.sh).
#
# It builds ONE deterministic, Lambda-compatible zip from the real `operations`
# code, verifies the Lambda target ABI, uploads it to an EXPLICIT, pre-existing
# artifact bucket (verified, never created or discovered) under a content-hash
# key, and passes the key to the stack. It resolves the OFFICIAL AppConfig Agent
# Lambda extension layer to the AUTHORITATIVE regional version from the public
# SSM parameter and passes it as AppConfigExtensionLayerArn. If the frozen
# control handler module is absent from the package, the wrapper fails closed.
#
# It NEVER mutates AWS on the preview path and requires an explicit, verified
# profile/account/region before any write.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

AWS_REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="game-agent"
STACK_NAME="${PROJECT_NAME}-operations-control-plane"
TEMPLATE="$PROJECT_ROOT/infrastructure/cloudformation/08-operations-control-plane.yaml"
BACKEND_SRC="$PROJECT_ROOT/backend/src"

# The frozen control handler module this stack deploys. It is import-probed and
# packaged.
CONTROL_MODULE_PATH="operations/control/control_entry.py"

# The public SSM parameter that yields the OFFICIAL, latest AppConfig Agent
# Lambda extension layer ARN for THIS Region (x86_64 Lambda ABI). It is public
# and readable from any account; the wrapper resolves the authoritative regional
# version and passes it explicitly so the deployed ARN is pinned and auditable.
APPCONFIG_EXTENSION_SSM_PARAM="/aws/service/aws-appconfig/lambda-extension/x86/latest"

# The Lambda target ABI. The runtime is Python 3.13 on x86_64.
LAMBDA_PY_VERSION="3.13"
LAMBDA_PY_TAG="cp313"
LAMBDA_PLATFORM="manylinux2014_x86_64"
LAMBDA_BUILD_IMAGE="public.ecr.aws/lambda/python:3.13-x86_64"

# Transitive runtime-dependency closure, pinned EXACTLY at the versions frozen
# in backend/uv.lock (same closure as the observe/advise/execute handlers; the
# control plane reuses the contract validator and canonical JSON). boto3/botocore
# are provided by the Lambda runtime and excluded. rpds-py is the only native
# member.
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
# double-confirmed by a MATCHING GBAW_OPERATIONS_CONTROL_MODE.
REQUESTED_MODE="enabled"
COGNITO_ISSUER="${COGNITO_ISSUER:-}"
COGNITO_CLIENT_ID="${COGNITO_CLIENT_ID:-}"
# Cross-stack data-plane bindings the 06 stack exports; the control plane reuses
# the SAME table + CMK for audit records (no new data store, no new key).
OPERATIONS_TABLE_NAME="${GBAW_OPERATIONS_TABLE_NAME:-}"
OPERATIONS_KMS_KEY_ARN="${GBAW_OPERATIONS_KMS_KEY_ARN:-}"
# Server-side trusted identity binding (must match the 06 stack).
# ADR-0001 deployment ceiling (GBAW_OPERATIONS_MODE), passed to the control
# stack so capability discovery reports the true ceiling. DISTINCT from the
# control on/off lever; defaults to the fail-closed 'disabled'. Set it to match
# the ceiling the 06/07 stacks run at.
OPERATIONS_MODE="${GBAW_OPERATIONS_MODE:-disabled}"
OPERATIONS_TENANT_ID="${GBAW_OPERATIONS_TENANT_ID:-}"
OPERATIONS_WORKSPACE_ID="${GBAW_OPERATIONS_WORKSPACE_ID:-}"
OPERATIONS_TRUSTED_AUDIENCE="${GBAW_OPERATIONS_TRUSTED_AUDIENCE:-}"
# The EXPLICIT, pre-existing artifact bucket for the Lambda zip. No discovery,
# no creation.
GBAW_OPERATIONS_ARTIFACT_BUCKET="${GBAW_OPERATIONS_ARTIFACT_BUCKET:-}"

AWS_PROFILE="${AWS_PROFILE:-default}"
AWS_PROFILE_ARGS=(--profile "$AWS_PROFILE")

usage() {
    cat <<'USAGE'
Usage: deploy-operations-control.sh [--enable [--mode enabled]]
                                    [--environment beta|prod]

  (no flag)     Preview only (READ-ONLY): validates and lints the template and
                creates nothing.
  --enable      Build/upload the control artifact, resolve the official
                AppConfig extension layer, and deploy the 08 control-plane stack
                with ControlMode=enabled. Requires
                GBAW_OPERATIONS_CONTROL_MODE=enabled (double opt-in) plus the
                enabling inputs below.
  --mode        enabled (the only enabled control mode). Default enabled.
  --environment Target environment (default: beta). "prod" lengthens log
                retention.

Environment for --enable:
  GBAW_OPERATIONS_CONTROL_MODE=enabled       Required confirmation; MUST match
                                             the selected --mode.
  COGNITO_ISSUER, COGNITO_CLIENT_ID          JWT issuer + audience (required).
  GBAW_OPERATIONS_TENANT_ID                  Server-side trusted tenant, matching
  GBAW_OPERATIONS_WORKSPACE_ID               the 06 stack (both required).
  GBAW_OPERATIONS_TRUSTED_AUDIENCE           Optional; defaults to CognitoClientId.
  GBAW_OPERATIONS_MODE                       Optional ADR-0001 deployment ceiling
                                             (disabled|observe|advise|remediate|
                                             operate); defaults to disabled.
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
        --mode) shift; REQUESTED_MODE="${1:-enabled}" ;;
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
    echo "🔎 Preview only (READ-ONLY). Validating and linting the 08 control-plane template."
    echo "   To deploy: GBAW_OPERATIONS_CONTROL_MODE=enabled $0 --enable"
    if command -v cfn-lint >/dev/null 2>&1; then
        echo "   Running cfn-lint ..."
        # Show warnings (e.g. W1030 on resolved-Ref pattern checks) but fail
        # ONLY on error-class (E-level) findings, so a warning-only run does not
        # abort this read-only preview under set -e before the AWS
        # validate-template call below.
        cfn-lint --non-zero-exit-code error "$TEMPLATE"
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
if [ "$REQUESTED_MODE" != "enabled" ]; then
    echo "❌ Refusing to enable: --mode must be enabled, got '$REQUESTED_MODE'." >&2
    exit 3
fi
if [ "${GBAW_OPERATIONS_CONTROL_MODE:-}" != "enabled" ]; then
    echo "❌ Refusing to enable: GBAW_OPERATIONS_CONTROL_MODE must be 'enabled'" >&2
    echo "   (it must match the selected --mode $REQUESTED_MODE) as a double opt-in." >&2
    exit 3
fi
if [ -z "$COGNITO_ISSUER" ] || [ -z "$COGNITO_CLIENT_ID" ]; then
    echo "❌ Refusing to enable: COGNITO_ISSUER and COGNITO_CLIENT_ID are required." >&2
    exit 3
fi
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
if [ ! -f "$BACKEND_SRC/$CONTROL_MODULE_PATH" ]; then
    echo "❌ Refusing to enable: the frozen control handler module is absent from $BACKEND_SRC." >&2
    echo "   Expected $CONTROL_MODULE_PATH." >&2
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
# Assert the bucket is owned by THIS verified account so a name-squatted foreign
# bucket is refused before any upload (mirrors the 06/07 wrappers).
if ! aws s3api head-bucket \
        "${AWS_PROFILE_ARGS[@]}" \
        --bucket "$GBAW_OPERATIONS_ARTIFACT_BUCKET" \
        --expected-bucket-owner "$ACCOUNT_ID" \
        --region "$AWS_REGION" >/dev/null 2>&1; then
    echo "❌ Artifact bucket '$GBAW_OPERATIONS_ARTIFACT_BUCKET' does not exist, is inaccessible," >&2
    echo "   or is not owned by account $ACCOUNT_ID in $AWS_REGION." >&2
    echo "   Create/authorize it out of band; this wrapper never creates or discovers a bucket." >&2
    exit 4
fi
BUCKET_REGION="$(aws s3api get-bucket-location \
    "${AWS_PROFILE_ARGS[@]}" \
    --bucket "$GBAW_OPERATIONS_ARTIFACT_BUCKET" \
    --expected-bucket-owner "$ACCOUNT_ID" \
    --output text 2>/dev/null || true)"
if [ "$BUCKET_REGION" = "None" ] || [ -z "$BUCKET_REGION" ]; then
    BUCKET_REGION="us-east-1"
fi
if [ "$BUCKET_REGION" != "$AWS_REGION" ]; then
    echo "❌ Artifact bucket '$GBAW_OPERATIONS_ARTIFACT_BUCKET' is in region '$BUCKET_REGION', not the" >&2
    echo "   deploy region '$AWS_REGION'. Lambda requires the code bucket in-region." >&2
    exit 4
fi
echo "   Bucket verified: owner=$ACCOUNT_ID region=$BUCKET_REGION"

# --------------------------------------------------------------------------- #
# Resolve the OFFICIAL AppConfig Agent Lambda extension layer to the
# AUTHORITATIVE regional version from the public SSM parameter, then verify the
# resolved ARN is the official layer shape before passing it to the stack.
# --------------------------------------------------------------------------- #
echo "🧩 Resolving the official AppConfig Lambda extension layer (authoritative regional version) ..."
APPCONFIG_EXTENSION_LAYER_ARN="$(aws ssm get-parameter \
    "${AWS_PROFILE_ARGS[@]}" \
    --region "$AWS_REGION" \
    --name "$APPCONFIG_EXTENSION_SSM_PARAM" \
    --query "Parameter.Value" \
    --output text 2>/dev/null || true)"
if [ -z "$APPCONFIG_EXTENSION_LAYER_ARN" ]; then
    echo "❌ Unable to resolve the AppConfig extension layer from $APPCONFIG_EXTENSION_SSM_PARAM." >&2
    exit 4
fi
case "$APPCONFIG_EXTENSION_LAYER_ARN" in
    arn:aws*:lambda:"$AWS_REGION":*:layer:AWS-AppConfig-Extension*:*) : ;;
    *)
        echo "❌ Resolved layer ARN is not the official AppConfig extension in $AWS_REGION:" >&2
        echo "   $APPCONFIG_EXTENSION_LAYER_ARN" >&2
        exit 4
        ;;
esac
echo "   AppConfig extension layer: $APPCONFIG_EXTENSION_LAYER_ARN"

# --------------------------------------------------------------------------- #
# Deterministic packaging: stage the real operations code + pinned deps for the
# Lambda ABI, then emit a content-hash-addressed zip. Determinism (fixed mtimes,
# sorted entries) makes the content hash — and the S3 key — stable for an
# unchanged source tree.
# --------------------------------------------------------------------------- #
BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gbaw-ops-control-pkg.XXXXXXXX")"
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

if [ ! -f "$STAGE/$CONTROL_MODULE_PATH" ]; then
    echo "❌ Frozen control handler module $CONTROL_MODULE_PATH missing from the staged package." >&2
    exit 5
fi

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
# Import-probe the frozen control handler on a clean Linux/x86 runtime when
# available; otherwise a structural fail-closed probe. A package that omits the
# handler, a dependency, or the contract schema resources fails HERE before
# upload.
# --------------------------------------------------------------------------- #
RUNTIME_PROBE_PY="$(cat <<'PROBE'
import importlib, sys
sys.path.insert(0, "/var/task")
m = importlib.import_module("operations.control.control_entry")
assert callable(m.handler), "control_entry handler is not callable"
# The control plane reuses the contract validator; force every versioned schema
# to be read from the package so a schema-less artifact fails the probe closed.
from operations.contracts import validation as v
from operations.contracts.versions import SCHEMA_NAMES
for schema in SCHEMA_NAMES:
    v.load_schema(schema)
# The E4 control-plane contract lives in its own additive schema set. Load the
# kill-switch schema (and its siblings) so a package missing an E4 schema fails
# the probe closed BEFORE upload.
from operations.contracts.control_plane import CONTROL_SCHEMA_NAMES, load_control_schema
for schema in CONTROL_SCHEMA_NAMES:
    load_control_schema(schema)
n = len(SCHEMA_NAMES) + len(CONTROL_SCHEMA_NAMES)
print("runtime probe ok: control import + %d schemas" % n)
PROBE
)"

echo "🧪 Import-probing the packaged control handler on a clean Linux/x86 runtime ..."
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker run --rm --platform linux/amd64 \
        -v "$STAGE:/var/task:ro" \
        --entrypoint python3 \
        "$LAMBDA_BUILD_IMAGE" \
        -E -s -c \
        "$RUNTIME_PROBE_PY" \
        || { echo "❌ Packaged control handler failed its runtime probe on the Lambda image; refusing to deploy." >&2; exit 5; }
    echo "   Runtime probe passed on $LAMBDA_BUILD_IMAGE (control import + schema load)."
else
    echo "   No container runtime available; running a structural fail-closed probe."
    if [ ! -f "$STAGE/$CONTROL_MODULE_PATH" ]; then
        echo "❌ Frozen control handler module $CONTROL_MODULE_PATH missing from the package." >&2
        exit 5
    fi
    if [ ! -f "$STAGE/operations/contracts/schemas/v1/operations-kill-switch.schema.json" ]; then
        echo "❌ E4 kill-switch contract schema is absent from the package." >&2
        exit 5
    fi
    for required in rfc8785 jsonschema referencing rpds; do
        if ! find "$STAGE" -maxdepth 2 \( -name "${required}" -o -name "${required}.py" -o -name "${required}*.so" \) | grep -q .; then
            echo "❌ Required runtime dependency '$required' is absent from the package." >&2
            exit 5
        fi
    done
    echo "   Structural probe passed (control handler + dependencies + schema resources present)."
fi

# --------------------------------------------------------------------------- #
# Build the deterministic zip and a content-hash key.
# --------------------------------------------------------------------------- #
ARTIFACT="$BUILD_DIR/operations-control.zip"
echo "🗜️  Building deterministic zip ..."
( cd "$STAGE" && find . -type f | LC_ALL=C sort | zip -X -q "$ARTIFACT" -@ )

CONTENT_HASH="$(shasum -a 256 "$ARTIFACT" | awk '{print $1}')"
S3_KEY="operations-control/${CONTENT_HASH}.zip"
echo "   Content hash: $CONTENT_HASH"
echo "   S3 key: $S3_KEY"

echo "⬆️  Uploading artifact to s3://$GBAW_OPERATIONS_ARTIFACT_BUCKET/$S3_KEY ..."
aws s3api put-object \
    "${AWS_PROFILE_ARGS[@]}" \
    --bucket "$GBAW_OPERATIONS_ARTIFACT_BUCKET" \
    --key "$S3_KEY" \
    --body "$ARTIFACT" \
    --region "$AWS_REGION" >/dev/null

# --------------------------------------------------------------------------- #
# Deploy the stack. Provisioned=true + ControlMode=enabled + the verified
# bindings + the resolved extension layer. This is the only mutating
# CloudFormation call.
# --------------------------------------------------------------------------- #
echo "🚀 Deploying $STACK_NAME (Provisioned=true, ControlMode=enabled) ..."
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
        "ControlMode=enabled" \
        "Environment=$ENVIRONMENT" \
        "CognitoIssuer=$COGNITO_ISSUER" \
        "CognitoClientId=$COGNITO_CLIENT_ID" \
        "OperationsTableName=$OPERATIONS_TABLE_NAME" \
        "OperationsKmsKeyArn=$OPERATIONS_KMS_KEY_ARN" \
        "TenantId=$OPERATIONS_TENANT_ID" \
        "WorkspaceId=$OPERATIONS_WORKSPACE_ID" \
        "TrustedAudience=$OPERATIONS_TRUSTED_AUDIENCE" \
        "CodeS3Bucket=$GBAW_OPERATIONS_ARTIFACT_BUCKET" \
        "ControlCodeS3Key=$S3_KEY" \
        "OperationsMode=$OPERATIONS_MODE" \
        "AppConfigExtensionLayerArn=$APPCONFIG_EXTENSION_LAYER_ARN"

echo "✅ Deployed $STACK_NAME with ControlMode=enabled (AppConfig kill switch live)."
