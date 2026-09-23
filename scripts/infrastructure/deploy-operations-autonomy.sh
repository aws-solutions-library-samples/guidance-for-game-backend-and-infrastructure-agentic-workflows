#!/usr/bin/env bash
#
# deploy-operations-autonomy.sh — OPTIONAL E5 bounded-autonomy control plane
# (GitHub issue #440, Track B).
#
# Deploys the SEPARATE 09-operations-autonomy.yaml stack: a scheduled evaluator
# Lambda that, only when BOTH the static operate ceiling AND a SEPARATE AppConfig
# autonomy switch admit it, starts the EXACT existing E3 Standard workflow
# (07-operations-execution.yaml) with an identifier only. It performs ZERO
# provider writes and grants NO GameLift IAM: the single
# GameLift capacity-write grant (UpdateFleetCapacity) remains solely on the
# unchanged E3 executor role. This
# wrapper INTEGRATES with the reviewed E3 execution plane by pointing the
# evaluator at the 07 stack's exported state machine ARN; it never mutates a
# provider resource directly and never creates an executor.
#
# It is default-UNPROVISIONED ($0). The DEFAULT run is a strictly READ-ONLY
# preview. Provisioning + enabling is a deliberate DOUBLE opt-in: an explicit
# --enable flag AND a matching confirmation token
# (GBAW_OPERATIONS_AUTONOMY_CONFIRM=operate) AND GBAW_OPERATIONS_MODE=operate.
#
# On --enable it: builds ONE deterministic, Lambda-compatible zip (the evaluator)
# from the real `operations` code with a complete pinned SDK closure, verifies
# the Lambda target ABI, resolves the OFFICIAL AppConfig extension layer from the
# public SSM parameter, uploads to an EXPLICIT, pre-existing artifact bucket
# (verified owner + region, never created or discovered) under a content-hash
# key, seeds the server-owned autonomy policy AND the initial durable window
# state with CONDITIONAL (immutable / attribute_not_exists) writes, deploys the
# 09 stack size-safely (template-url fallback over the 51,200-byte inline limit),
# and finally VERIFIES the deployed function's CodeSha256 matches the artifact.
#
# It NEVER mutates AWS on the preview path and requires an explicit, verified
# profile/account/region before any write.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

AWS_REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="game-agent"
STACK_NAME="${PROJECT_NAME}-operations-autonomy"
EVALUATOR_FUNCTION_NAME="${PROJECT_NAME}-operations-autonomy-evaluator"
# The 09 autonomy template; overridable ONLY for tests (over/under-limit fixtures).
TEMPLATE="${GBAW_OPERATIONS_AUTONOMY_TEMPLATE:-$PROJECT_ROOT/infrastructure/cloudformation/09-operations-autonomy.yaml}"
BACKEND_SRC="${GBAW_OPERATIONS_AUTONOMY_BACKEND_SRC:-$PROJECT_ROOT/backend/src}"

# CloudFormation's hard limit for an inline template body. A body larger than
# this MUST reach the service through S3 (--template-url / deploy --s3-bucket).
CFN_INLINE_TEMPLATE_LIMIT_BYTES=51200

# The single frozen handler module this stack deploys.
EVALUATOR_MODULE_PATH="operations/autonomy_runtime/evaluator_entry.py"

# The Lambda target ABI. Python 3.13 on x86_64; manylinux x86_64 wheels only.
LAMBDA_PY_VERSION="3.13"
LAMBDA_PY_TAG="cp313"
LAMBDA_PLATFORM="manylinux2014_x86_64"
LAMBDA_BUILD_IMAGE="public.ecr.aws/lambda/python:3.13-x86_64"

# The public SSM parameter that yields the OFFICIAL, latest AppConfig Agent
# Lambda extension layer ARN for THIS Region (x86_64 Lambda ABI).
APPCONFIG_EXTENSION_SSM_PARAM="/aws/service/aws-appconfig/lambda-extension/x86/latest"

# Transitive runtime-dependency closure, pinned EXACTLY at the versions frozen in
# backend/uv.lock. boto3/botocore are packaged explicitly so the deployed
# evaluator uses the same reviewed service models. rpds-py is the only native
# member.
PINNED_DEPS=(
    "boto3==1.43.32"
    "botocore==1.43.55"
    "jmespath==1.0.1"
    "s3transfer==0.19.0"
    "python-dateutil==2.9.0.post0"
    "urllib3==2.7.0"
    "six==1.17.0"
    "rfc8785==0.1.4"
    "jsonschema==4.26.0"
    "jsonschema-specifications==2025.9.1"
    "referencing==0.36.2"
    "attrs==25.4.0"
    "rpds-py==2026.5.1"
)

ENVIRONMENT="beta"
ACTION="preview"   # preview | enable

# Enabling inputs (all required to enable; empty otherwise).
OPERATIONS_TABLE_NAME="${GBAW_OPERATIONS_TABLE_NAME:-}"
OPERATIONS_KMS_KEY_ARN="${GBAW_OPERATIONS_KMS_KEY_ARN:-}"
# The EXACT E3 Standard workflow ARN (the 07 stack's ExecutionStateMachineArn
# output). This is how E5 integrates with the reviewed execution plane.
EXECUTION_STATE_MACHINE_ARN="${GBAW_OPERATIONS_EXECUTION_STATE_MACHINE_ARN:-}"
ENROLLED_FLEET_ID="${GBAW_OPERATIONS_ENROLLED_FLEET_ID:-}"
ENROLLED_LOCATION="${GBAW_OPERATIONS_ENROLLED_LOCATION:-$AWS_REGION}"
OPERATIONS_TENANT_ID="${GBAW_OPERATIONS_TENANT_ID:-}"
OPERATIONS_WORKSPACE_ID="${GBAW_OPERATIONS_WORKSPACE_ID:-}"
AUTONOMY_SUBJECT="${GBAW_OPERATIONS_AUTONOMY_SUBJECT:-}"
AUTONOMY_CLIENT="${GBAW_OPERATIONS_AUTONOMY_CLIENT:-}"
AUTONOMY_POLICY_ID="${GBAW_OPERATIONS_AUTONOMY_POLICY_ID:-}"
AUTONOMY_POLICY_VERSION="${GBAW_OPERATIONS_AUTONOMY_POLICY_VERSION:-}"
AUTONOMY_POLICY_HASH="${GBAW_OPERATIONS_AUTONOMY_POLICY_HASH:-}"
AUTONOMY_STATE_ID="${GBAW_OPERATIONS_AUTONOMY_STATE_ID:-}"
# The server-owned policy + initial window-state documents to seed. JSON files.
AUTONOMY_POLICY_FILE="${GBAW_OPERATIONS_AUTONOMY_POLICY_FILE:-}"
AUTONOMY_WINDOW_STATE_FILE="${GBAW_OPERATIONS_AUTONOMY_WINDOW_STATE_FILE:-}"
# Shared AppConfig identifiers (from the 08 control-plane stack) + the SEPARATE
# autonomy switch profile.
APPCONFIG_APPLICATION_ID="${GBAW_OPERATIONS_APPCONFIG_APPLICATION_ID:-}"
APPCONFIG_ENVIRONMENT_ID="${GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT_ID:-}"
KILL_SWITCH_PROFILE_ID="${GBAW_OPERATIONS_APPCONFIG_PROFILE_ID:-}"
AUTONOMY_SWITCH_PROFILE_ID="${GBAW_OPERATIONS_AUTONOMY_SWITCH_PROFILE_ID:-}"
# The EXPLICIT, pre-existing artifact bucket for the evaluator zip.
GBAW_OPERATIONS_ARTIFACT_BUCKET="${GBAW_OPERATIONS_ARTIFACT_BUCKET:-}"

AWS_PROFILE="${AWS_PROFILE:-default}"
AWS_PROFILE_ARGS=(--profile "$AWS_PROFILE")

usage() {
    cat <<'USAGE'
Usage: deploy-operations-autonomy.sh [--enable] [--environment beta|prod]

  (no flag)     Preview only (READ-ONLY): lints + parses the 09 template and
                creates nothing.
  --enable      Build/upload the evaluator artifact, resolve the official
                AppConfig extension layer, seed the policy + window state, and
                deploy the 09 autonomy stack with AutonomyMode=operate. Requires
                a DOUBLE opt-in (see below).
  --environment Target environment (default: beta). "prod" lengthens log
                retention.

Double opt-in for --enable (activation is refused unless ALL hold):
  --enable                                   Explicit flag (not enough alone).
  GBAW_OPERATIONS_AUTONOMY_CONFIRM=operate   Required confirmation token.
  GBAW_OPERATIONS_MODE=operate               Required static ceiling.

Enabling inputs:
  GBAW_OPERATIONS_TABLE_NAME                 06-exported operations table (req).
  GBAW_OPERATIONS_KMS_KEY_ARN                06-exported operations CMK ARN (req).
  GBAW_OPERATIONS_EXECUTION_STATE_MACHINE_ARN
                                             The EXACT E3 workflow ARN from the
                                             07 stack output (req).
  GBAW_OPERATIONS_ENROLLED_FLEET_ID          The EXACT enrolled fleet id (req).
  GBAW_OPERATIONS_TENANT_ID / _WORKSPACE_ID  Server-side identity binding (req).
  GBAW_OPERATIONS_AUTONOMY_SUBJECT / _CLIENT Trusted automation principal (req).
  GBAW_OPERATIONS_AUTONOMY_POLICY_ID/_VERSION/_HASH
                                             Pinned server-owned policy (req).
  GBAW_OPERATIONS_AUTONOMY_STATE_ID          Durable window-state id (req).
  GBAW_OPERATIONS_AUTONOMY_POLICY_FILE       Policy JSON to seed (conditional).
  GBAW_OPERATIONS_AUTONOMY_WINDOW_STATE_FILE Initial window-state JSON to seed.
  GBAW_OPERATIONS_APPCONFIG_APPLICATION_ID / _ENVIRONMENT_ID
                                             Shared AppConfig ids (req).
  GBAW_OPERATIONS_APPCONFIG_PROFILE_ID       E4 kill-switch profile id (req).
  GBAW_OPERATIONS_AUTONOMY_SWITCH_PROFILE_ID SEPARATE autonomy switch profile (req).
  GBAW_OPERATIONS_ARTIFACT_BUCKET            REQUIRED explicit, pre-existing
                                             artifact bucket (verified).
  AWS_PROFILE, AWS_REGION                    Credentials/region, verified first.
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --enable) ACTION="enable" ;;
        --environment) shift; ENVIRONMENT="${1:-beta}" ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
    esac
    shift
done

# --------------------------------------------------------------------------- #
# Portable byte-size of a file.
# --------------------------------------------------------------------------- #
file_size_bytes() {
    local f="$1"
    if stat -f%z "$f" >/dev/null 2>&1; then
        stat -f%z "$f"
    elif stat -c%s "$f" >/dev/null 2>&1; then
        stat -c%s "$f"
    else
        wc -c < "$f" | tr -d ' '
    fi
}

# --------------------------------------------------------------------------- #
# Preview: read-only lint + local parse, then a SIZE-AWARE service validation.
# Never mutates AWS and never uploads to S3.
# --------------------------------------------------------------------------- #
if [ "$ACTION" = "preview" ]; then
    echo "🔎 Preview only (READ-ONLY). Linting and parsing the 09 autonomy template."
    echo "   To deploy (DOUBLE opt-in):"
    echo "     GBAW_OPERATIONS_MODE=operate GBAW_OPERATIONS_AUTONOMY_CONFIRM=operate $0 --enable"
    if command -v cfn-lint >/dev/null 2>&1; then
        echo "   Running cfn-lint ..."
        cfn-lint --non-zero-exit-code error "$TEMPLATE"
    else
        echo "   cfn-lint not found; skipping lint."
    fi
    echo "   Locally parsing the template (no AWS) ..."
    python3 - "$TEMPLATE" <<'PARSE' || { echo "❌ Template failed local parse." >&2; exit 6; }
import sys
path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    text = fh.read()
try:
    import yaml  # type: ignore

    class _CfnLoader(yaml.SafeLoader):
        pass

    def _passthrough(loader, tag_suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node)

    _CfnLoader.add_multi_constructor("!", _passthrough)
    yaml.load(text, Loader=_CfnLoader)
except ImportError:
    import json
    stripped = text.lstrip()
    if stripped[:1] in "{[":
        json.loads(text)
PARSE
    TEMPLATE_BYTES="$(file_size_bytes "$TEMPLATE")"
    if [ "$TEMPLATE_BYTES" -le "$CFN_INLINE_TEMPLATE_LIMIT_BYTES" ]; then
        echo "   Template is ${TEMPLATE_BYTES} bytes (<= ${CFN_INLINE_TEMPLATE_LIMIT_BYTES}); running service validation (read-only) ..."
        aws cloudformation validate-template \
            "${AWS_PROFILE_ARGS[@]}" \
            --template-body "file://$TEMPLATE" \
            --region "$AWS_REGION" >/dev/null
        echo "✅ Preview complete. Template linted, parsed, and service-validated; no resources were created."
    else
        echo "   Template is ${TEMPLATE_BYTES} bytes (> ${CFN_INLINE_TEMPLATE_LIMIT_BYTES}-byte inline limit)."
        echo "   ⏭️  Service validate-template is DEFERRED to the write-gated --enable deploy,"
        echo "      which sends the template through the verified artifact bucket. Preview uploads NOTHING."
        echo "✅ Preview complete (read-only). Linted + parsed; service validation deferred; no resources were created."
    fi
    exit 0
fi

# --------------------------------------------------------------------------- #
# Enable gate: DOUBLE opt-in + required inputs (fail closed before any AWS).
# --------------------------------------------------------------------------- #
if [ "${GBAW_OPERATIONS_AUTONOMY_CONFIRM:-}" != "operate" ]; then
    echo "❌ Refusing to enable: GBAW_OPERATIONS_AUTONOMY_CONFIRM must be exactly 'operate'." >&2
    echo "   Activation requires an explicit --enable AND this confirmation token." >&2
    exit 3
fi
if [ "${GBAW_OPERATIONS_MODE:-}" != "operate" ]; then
    echo "❌ Refusing to enable: GBAW_OPERATIONS_MODE must be exactly 'operate' (the static ceiling)." >&2
    exit 3
fi
if [ -z "$OPERATIONS_TABLE_NAME" ] || [ -z "$OPERATIONS_KMS_KEY_ARN" ]; then
    echo "❌ Refusing to enable: GBAW_OPERATIONS_TABLE_NAME and GBAW_OPERATIONS_KMS_KEY_ARN (06 table + CMK) are required." >&2
    exit 3
fi
if [ -z "$EXECUTION_STATE_MACHINE_ARN" ]; then
    echo "❌ Refusing to enable: GBAW_OPERATIONS_EXECUTION_STATE_MACHINE_ARN (the exact 07 E3 workflow) is required." >&2
    echo "   E5 integrates with the reviewed execution plane; it never creates an executor." >&2
    exit 3
fi
case "$EXECUTION_STATE_MACHINE_ARN" in
    arn:aws*:states:*:stateMachine:*) : ;;
    *) echo "❌ Refusing to enable: GBAW_OPERATIONS_EXECUTION_STATE_MACHINE_ARN must be a Step Functions ARN." >&2; exit 3 ;;
esac
if [ -z "$ENROLLED_FLEET_ID" ]; then
    echo "❌ Refusing to enable: GBAW_OPERATIONS_ENROLLED_FLEET_ID is required." >&2
    exit 3
fi
case "$ENROLLED_FLEET_ID" in
    fleet-*) : ;;
    *) echo "❌ Refusing to enable: GBAW_OPERATIONS_ENROLLED_FLEET_ID must be a fleet id (fleet-...)." >&2; exit 3 ;;
esac
if [ -z "$OPERATIONS_TENANT_ID" ] || [ -z "$OPERATIONS_WORKSPACE_ID" ]; then
    echo "❌ Refusing to enable: GBAW_OPERATIONS_TENANT_ID and GBAW_OPERATIONS_WORKSPACE_ID are required." >&2
    exit 3
fi
if [ -z "$AUTONOMY_SUBJECT" ] || [ -z "$AUTONOMY_CLIENT" ]; then
    echo "❌ Refusing to enable: GBAW_OPERATIONS_AUTONOMY_SUBJECT and GBAW_OPERATIONS_AUTONOMY_CLIENT are required." >&2
    exit 3
fi
if [ -z "$AUTONOMY_POLICY_ID" ] || [ -z "$AUTONOMY_POLICY_VERSION" ] || [ -z "$AUTONOMY_POLICY_HASH" ]; then
    echo "❌ Refusing to enable: the pinned policy id/version/hash are required." >&2
    exit 3
fi
if [ -z "$AUTONOMY_STATE_ID" ]; then
    echo "❌ Refusing to enable: GBAW_OPERATIONS_AUTONOMY_STATE_ID is required." >&2
    exit 3
fi
if [ -z "$APPCONFIG_APPLICATION_ID" ] || [ -z "$APPCONFIG_ENVIRONMENT_ID" ]; then
    echo "❌ Refusing to enable: GBAW_OPERATIONS_APPCONFIG_APPLICATION_ID and _ENVIRONMENT_ID are required." >&2
    exit 3
fi
if [ -z "$KILL_SWITCH_PROFILE_ID" ] || [ -z "$AUTONOMY_SWITCH_PROFILE_ID" ]; then
    echo "❌ Refusing to enable: both the E4 kill-switch profile and the SEPARATE autonomy switch profile are required." >&2
    exit 3
fi
if [ "$KILL_SWITCH_PROFILE_ID" = "$AUTONOMY_SWITCH_PROFILE_ID" ]; then
    echo "❌ Refusing to enable: the autonomy switch profile MUST be a SEPARATE document from the kill-switch profile." >&2
    exit 3
fi
if [ -z "$GBAW_OPERATIONS_ARTIFACT_BUCKET" ]; then
    echo "❌ Refusing to enable: GBAW_OPERATIONS_ARTIFACT_BUCKET (explicit, pre-existing bucket) is required." >&2
    exit 3
fi
if [ ! -f "$BACKEND_SRC/$EVALUATOR_MODULE_PATH" ]; then
    echo "❌ Refusing to enable: the frozen evaluator module is absent from $BACKEND_SRC." >&2
    echo "   Expected $EVALUATOR_MODULE_PATH." >&2
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
# AUTHORITATIVE regional version from the public SSM parameter.
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
# Deterministic packaging: build ONE staged tree (real operations code + pinned
# deps for the Lambda ABI), then emit a content-hash-addressed zip. Fixed mtimes
# + sorted entries make the content hash — and the S3 key — stable for unchanged
# source.
# --------------------------------------------------------------------------- #
BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gbaw-ops-autonomy-pkg.XXXXXXXX")"
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

if [ ! -f "$STAGE/$EVALUATOR_MODULE_PATH" ]; then
    echo "❌ Frozen evaluator module $EVALUATOR_MODULE_PATH missing from the staged package." >&2
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
        --no-deps \
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
        --no-deps \
        "${PINNED_DEPS[@]}"
fi

find "$STAGE" -depth -type d -name '*.dist-info' -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -depth -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
EPOCH_STAMP="2000""01010000.00"
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
# Import-probe the frozen evaluator on a clean Linux/x86 runtime when available;
# a structural fail-closed probe otherwise. The evaluator import is AWS-free and
# must NOT construct a GameLift client.
# --------------------------------------------------------------------------- #
RUNTIME_PROBE_PY="$(cat <<'PROBE'
import importlib, sys
sys.path.insert(0, "/var/task")
m = importlib.import_module("operations.autonomy_runtime.evaluator_entry")
assert callable(m.handler), "evaluator handler is not callable"
# The v2 autonomy contract schemas must be readable from the package.
from operations.contracts.autonomy import AUTONOMY_SCHEMA_NAMES, load_autonomy_schema
for schema in AUTONOMY_SCHEMA_NAMES:
    load_autonomy_schema(schema)
# The evaluator resolves settings and fails closed when autonomy is not enabled;
# resolving against an empty env must raise (never silently construct a handler).
from operations.autonomy_runtime.evaluator_entry import resolve_autonomy_evaluator_settings
try:
    resolve_autonomy_evaluator_settings({})
except ValueError:
    pass
else:
    raise AssertionError("evaluator settings did not fail closed on an empty env")
print("runtime probe ok: evaluator import + %d autonomy schemas + fail-closed settings" % len(AUTONOMY_SCHEMA_NAMES))
PROBE
)"

echo "🧪 Import-probing the packaged evaluator on a clean Linux/x86 runtime ..."
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker run --rm --platform linux/amd64 \
        -v "$STAGE:/var/task:ro" \
        --entrypoint python3 \
        "$LAMBDA_BUILD_IMAGE" \
        -E -s -c \
        "$RUNTIME_PROBE_PY" \
        || { echo "❌ Packaged evaluator failed its runtime probe on the Lambda image; refusing to deploy." >&2; exit 5; }
    echo "   Runtime probe passed on $LAMBDA_BUILD_IMAGE."
else
    echo "   No container runtime available; running a structural fail-closed probe."
    if [ ! -f "$STAGE/$EVALUATOR_MODULE_PATH" ]; then
        echo "❌ Frozen evaluator module $EVALUATOR_MODULE_PATH missing from the package." >&2
        exit 5
    fi
    if ! find "$STAGE/operations/contracts/schemas/v2" -type f -name '*autonomy*.schema.json' 2>/dev/null | grep -q .; then
        echo "❌ Autonomy contract schema resources are absent from the package." >&2
        exit 5
    fi
    for required in rfc8785 jsonschema referencing rpds; do
        if ! find "$STAGE" -maxdepth 2 \( -name "${required}" -o -name "${required}.py" -o -name "${required}*.so" \) | grep -q .; then
            echo "❌ Required runtime dependency '$required' is absent from the package." >&2
            exit 5
        fi
    done
    echo "   Structural probe passed (evaluator + dependencies + autonomy schema resources present)."
fi

# --------------------------------------------------------------------------- #
# Build the deterministic zip + content-hash key and upload.
# --------------------------------------------------------------------------- #
ARTIFACT="$BUILD_DIR/operations-autonomy.zip"
echo "🗜️  Building deterministic zip ..."
( cd "$STAGE" && find . -type f | LC_ALL=C sort | zip -X -q "$ARTIFACT" -@ )

CONTENT_HASH="$(shasum -a 256 "$ARTIFACT" | awk '{print $1}')"
S3_KEY="operations-autonomy/${CONTENT_HASH}.zip"
# Lambda's GetFunctionConfiguration.CodeSha256 is the base64 of the raw SHA-256
# of the zip bytes; compute it now so we can verify the DEPLOYED code matches.
EXPECTED_CODE_SHA256="$(shasum -a 256 "$ARTIFACT" | awk '{print $1}' | xxd -r -p | base64)"
echo "   Content hash: $CONTENT_HASH"
echo "   S3 key: $S3_KEY"
echo "   Expected deployed CodeSha256: $EXPECTED_CODE_SHA256"

echo "⬆️  Uploading artifact to s3://$GBAW_OPERATIONS_ARTIFACT_BUCKET/$S3_KEY ..."
aws s3api put-object \
    "${AWS_PROFILE_ARGS[@]}" \
    --bucket "$GBAW_OPERATIONS_ARTIFACT_BUCKET" \
    --key "$S3_KEY" \
    --body "$ARTIFACT" \
    --region "$AWS_REGION" >/dev/null

EVALUATOR_S3_KEY="$S3_KEY"

# --------------------------------------------------------------------------- #
# Seed the server-owned autonomy policy AND the initial durable window state via
# the code-owned CONDITIONAL (immutable / attribute_not_exists) seed APIs. A
# second seed of identical documents is idempotent; a differing document is
# refused without overwrite. Both run BEFORE the stack deploy so the enabled
# evaluator has its pinned policy + window snapshot the first time it fires.
# --------------------------------------------------------------------------- #
if [ -n "$AUTONOMY_POLICY_FILE" ] || [ -n "$AUTONOMY_WINDOW_STATE_FILE" ]; then
    echo "🌱 Seeding autonomy policy and window state with conditional (immutable) writes ..."
    PYTHONPATH="$BACKEND_SRC" python3 - <<PYSEED
import json, os, sys
import boto3

table = "$OPERATIONS_TABLE_NAME"
region = "$AWS_REGION"
client = boto3.client("dynamodb", region_name=region)

from operations.autonomy_runtime.evaluator_entry import DynamoDbAutonomyPolicyLoader
from operations.autonomy_runtime.store import DynamoDbReservationStore

policy_file = "$AUTONOMY_POLICY_FILE"
window_file = "$AUTONOMY_WINDOW_STATE_FILE"

if policy_file:
    with open(policy_file, "r", encoding="utf-8") as fh:
        policy = json.load(fh)
    DynamoDbAutonomyPolicyLoader(client=client, table_name=table).seed(policy)
    print("   policy seeded (conditional immutable put)")

if window_file:
    with open(window_file, "r", encoding="utf-8") as fh:
        window = json.load(fh)
    DynamoDbReservationStore(client=client, table_name=table).seed_window_state(window)
    print("   window state seeded (conditional attribute_not_exists put)")
PYSEED
else
    echo "   No policy/window-state seed files supplied; the pinned policy + state must already be sealed."
fi

# --------------------------------------------------------------------------- #
# Size-safe deploy through the SAME verified artifact bucket. Upload the template
# under a deterministic content-hash key, service-validate it via --template-url
# BEFORE any stack mutation, and always delete that transient template object on
# exit.
# --------------------------------------------------------------------------- #
TEMPLATE_HASH="$(shasum -a 256 "$TEMPLATE" | awk '{print $1}')"
TEMPLATE_S3_PREFIX="operations-autonomy/templates"
TEMPLATE_S3_KEY="${TEMPLATE_S3_PREFIX}/${TEMPLATE_HASH}.yaml"
TEMPLATE_URL="https://s3.${AWS_REGION}.amazonaws.com/${GBAW_OPERATIONS_ARTIFACT_BUCKET}/${TEMPLATE_S3_KEY}"

cleanup_autonomy() {
    rm -rf "$BUILD_DIR"
    aws s3api delete-object \
        "${AWS_PROFILE_ARGS[@]}" \
        --bucket "$GBAW_OPERATIONS_ARTIFACT_BUCKET" \
        --key "$TEMPLATE_S3_KEY" \
        --region "$AWS_REGION" >/dev/null 2>&1 || true
}
trap cleanup_autonomy EXIT

echo "⬆️  Uploading template for service validation to s3://$GBAW_OPERATIONS_ARTIFACT_BUCKET/$TEMPLATE_S3_KEY ..."
aws s3api put-object \
    "${AWS_PROFILE_ARGS[@]}" \
    --bucket "$GBAW_OPERATIONS_ARTIFACT_BUCKET" \
    --key "$TEMPLATE_S3_KEY" \
    --body "$TEMPLATE" \
    --region "$AWS_REGION" >/dev/null

echo "🔎 Service-validating the template via --template-url BEFORE any stack mutation ..."
aws cloudformation validate-template \
    "${AWS_PROFILE_ARGS[@]}" \
    --template-url "$TEMPLATE_URL" \
    --region "$AWS_REGION" >/dev/null

echo "🚀 Deploying $STACK_NAME (Provisioned=true, AutonomyMode=operate) via S3-backed template ..."
# Provisioned defaults false / AutonomyMode defaults disabled at the TEMPLATE
# level (a default deploy is $0). This wrapper only sets Provisioned=true and
# AutonomyMode=operate under the double-opt-in --enable path.
aws cloudformation deploy \
    "${AWS_PROFILE_ARGS[@]}" \
    --region "$AWS_REGION" \
    --stack-name "$STACK_NAME" \
    --template-file "$TEMPLATE" \
    --s3-bucket "$GBAW_OPERATIONS_ARTIFACT_BUCKET" \
    --s3-prefix "$TEMPLATE_S3_PREFIX" \
    --capabilities CAPABILITY_NAMED_IAM \
    --no-fail-on-empty-changeset \
    --parameter-overrides \
        "ProjectName=$PROJECT_NAME" \
        "Provisioned=true" \
        "AutonomyMode=operate" \
        "Environment=$ENVIRONMENT" \
        "OperationsTableName=$OPERATIONS_TABLE_NAME" \
        "OperationsKmsKeyArn=$OPERATIONS_KMS_KEY_ARN" \
        "ExecutionStateMachineArn=$EXECUTION_STATE_MACHINE_ARN" \
        "EnrolledFleetId=$ENROLLED_FLEET_ID" \
        "EnrolledLocation=$ENROLLED_LOCATION" \
        "TenantId=$OPERATIONS_TENANT_ID" \
        "WorkspaceId=$OPERATIONS_WORKSPACE_ID" \
        "AutonomySubject=$AUTONOMY_SUBJECT" \
        "AutonomyClient=$AUTONOMY_CLIENT" \
        "AutonomyPolicyId=$AUTONOMY_POLICY_ID" \
        "AutonomyPolicyVersion=$AUTONOMY_POLICY_VERSION" \
        "AutonomyPolicyHash=$AUTONOMY_POLICY_HASH" \
        "AutonomyStateId=$AUTONOMY_STATE_ID" \
        "AppConfigApplicationId=$APPCONFIG_APPLICATION_ID" \
        "AppConfigEnvironmentId=$APPCONFIG_ENVIRONMENT_ID" \
        "KillSwitchProfileId=$KILL_SWITCH_PROFILE_ID" \
        "AutonomySwitchProfileId=$AUTONOMY_SWITCH_PROFILE_ID" \
        "AppConfigExtensionLayerArn=$APPCONFIG_EXTENSION_LAYER_ARN" \
        "CodeS3Bucket=$GBAW_OPERATIONS_ARTIFACT_BUCKET" \
        "EvaluatorCodeS3Key=$EVALUATOR_S3_KEY"

# --------------------------------------------------------------------------- #
# Verify the DEPLOYED evaluator's code hash matches the artifact we built and
# uploaded. A mismatch means CloudFormation resolved a different S3 object (a
# stale key, a foreign upload) — fail closed rather than trusting the deploy.
# --------------------------------------------------------------------------- #
echo "🔒 Verifying the deployed evaluator CodeSha256 matches the built artifact ..."
DEPLOYED_CODE_SHA256="$(aws lambda get-function \
    "${AWS_PROFILE_ARGS[@]}" \
    --function-name "$EVALUATOR_FUNCTION_NAME" \
    --region "$AWS_REGION" \
    --query 'Configuration.CodeSha256' \
    --output text 2>/dev/null || true)"
if [ -z "$DEPLOYED_CODE_SHA256" ] || [ "$DEPLOYED_CODE_SHA256" = "None" ]; then
    echo "❌ Unable to read the deployed evaluator CodeSha256 for verification." >&2
    exit 7
fi
if [ "$DEPLOYED_CODE_SHA256" != "$EXPECTED_CODE_SHA256" ]; then
    echo "❌ Deployed evaluator CodeSha256 does not match the built artifact:" >&2
    echo "   expected $EXPECTED_CODE_SHA256" >&2
    echo "   deployed $DEPLOYED_CODE_SHA256" >&2
    exit 7
fi
echo "   Deployed CodeSha256 verified: $DEPLOYED_CODE_SHA256"

echo "✅ Deployed $STACK_NAME with AutonomyMode=operate. The evaluator starts only the exact E3 workflow $EXECUTION_STATE_MACHINE_ARN."
echo "   Emergency disable (reversible, deletes nothing): disable-operations-autonomy.sh --confirm"
