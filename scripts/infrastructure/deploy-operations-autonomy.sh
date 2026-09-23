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
EXECUTION_STACK_NAME="${PROJECT_NAME}-operations-execution"
OBSERVATION_STACK_NAME="${PROJECT_NAME}-operations-observation"
CONTROL_STACK_NAME="${PROJECT_NAME}-operations-control-plane"
# The 06-owned observation Lambda the evaluator loads its trusted observation
# from (a deterministic 06 name; verified to exist during activation).
OBSERVE_FUNCTION_NAME="${PROJECT_NAME}-operations-observe"
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
# The EXACT enrolled fleet ARN and trusted audience the template requires when
# AutonomyMode=operate. The ARN is derived from the verified account/region and
# the fleet id unless supplied explicitly; both are bound server-side.
ENROLLED_FLEET_ARN="${GBAW_OPERATIONS_ENROLLED_FLEET_ARN:-}"
OPERATIONS_TRUSTED_AUDIENCE="${GBAW_OPERATIONS_TRUSTED_AUDIENCE:-}"
OPERATIONS_TENANT_ID="${GBAW_OPERATIONS_TENANT_ID:-}"
OPERATIONS_WORKSPACE_ID="${GBAW_OPERATIONS_WORKSPACE_ID:-}"
AUTONOMY_SUBJECT="${GBAW_OPERATIONS_AUTONOMY_SUBJECT:-}"
AUTONOMY_CLIENT="${GBAW_OPERATIONS_AUTONOMY_CLIENT:-}"
AUTONOMY_POLICY_ID="${GBAW_OPERATIONS_AUTONOMY_POLICY_ID:-}"
AUTONOMY_POLICY_VERSION="${GBAW_OPERATIONS_AUTONOMY_POLICY_VERSION:-}"
AUTONOMY_POLICY_HASH="${GBAW_OPERATIONS_AUTONOMY_POLICY_HASH:-}"
AUTONOMY_STATE_ID="${GBAW_OPERATIONS_AUTONOMY_STATE_ID:-}"
# OPTIONAL server-owned CLOSED scheduled evaluation event (#439). All four
# must be set to ENABLE the periodic schedule; otherwise the EventBridge rule
# stays DISABLED and no event is ever delivered (the reviewed observation
# pipeline drives evaluation instead).
SCHEDULED_OBSERVATION_OPERATION_ID="${GBAW_OPERATIONS_SCHEDULED_OBSERVATION_OPERATION_ID:-}"
SCHEDULED_DESIRED="${GBAW_OPERATIONS_SCHEDULED_DESIRED:--1}"
SCHEDULED_MINIMUM="${GBAW_OPERATIONS_SCHEDULED_MINIMUM:--1}"
SCHEDULED_MAXIMUM="${GBAW_OPERATIONS_SCHEDULED_MAXIMUM:--1}"
# The server-owned policy + initial window-state documents to seed. JSON files.
AUTONOMY_POLICY_FILE="${GBAW_OPERATIONS_AUTONOMY_POLICY_FILE:-}"
AUTONOMY_WINDOW_STATE_FILE="${GBAW_OPERATIONS_AUTONOMY_WINDOW_STATE_FILE:-}"
# The EXISTING E4 (08) kill-switch AppConfig coordinate. The evaluator reads
# the E4 kill switch from THIS application/environment/profile. The SEPARATE
# E5 autonomy application/environment/profile is created by the 09 template
# itself, so it is NOT passed here.
KILL_SWITCH_APPLICATION_ID="${GBAW_OPERATIONS_APPCONFIG_APPLICATION_ID:-}"
KILL_SWITCH_ENVIRONMENT_ID="${GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT_ID:-}"
KILL_SWITCH_PROFILE_ID="${GBAW_OPERATIONS_APPCONFIG_PROFILE_ID:-}"
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
                                             The EXISTING E4 (08) kill-switch
                                             AppConfig application/environment
                                             ids (req). The SEPARATE autonomy
                                             switch is created by the template.
  GBAW_OPERATIONS_APPCONFIG_PROFILE_ID       E4 kill-switch profile id (req).
  GBAW_OPERATIONS_TRUSTED_AUDIENCE           Server-side trusted audience (req).
  GBAW_OPERATIONS_ENROLLED_FLEET_ARN         Enrolled fleet ARN (derived if
                                             unset from account/region/id).
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
if [ -z "$ENROLLED_FLEET_ARN" ]; then
    echo "❌ Refusing to enable: GBAW_OPERATIONS_ENROLLED_FLEET_ARN (the exact enrolled fleet ARN) is required." >&2
    exit 3
fi
# The ARN is a server-owned binding: it MUST end in the EXACT enrolled fleet
# id (no fleet-* wildcard fallback), be a gamelift fleet ARN, and — once the
# caller identity is known — match the verified account/region (checked
# below). A mismatched fleet ARN is refused, not silently accepted.
case "$ENROLLED_FLEET_ARN" in
    arn:aws*:gamelift:*:*:fleet/"$ENROLLED_FLEET_ID") : ;;
    *) echo "❌ Refusing to enable: GBAW_OPERATIONS_ENROLLED_FLEET_ARN must be a gamelift fleet ARN ending in fleet/$ENROLLED_FLEET_ID (the exact enrolled fleet)." >&2; exit 3 ;;
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
if [ -z "$KILL_SWITCH_APPLICATION_ID" ] || [ -z "$KILL_SWITCH_ENVIRONMENT_ID" ]; then
    echo "❌ Refusing to enable: GBAW_OPERATIONS_APPCONFIG_APPLICATION_ID and _ENVIRONMENT_ID (the E4 kill-switch coordinate) are required." >&2
    exit 3
fi
if [ -z "$KILL_SWITCH_PROFILE_ID" ]; then
    echo "❌ Refusing to enable: GBAW_OPERATIONS_APPCONFIG_PROFILE_ID (the E4 kill-switch profile) is required." >&2
    exit 3
fi
if [ -z "$OPERATIONS_TRUSTED_AUDIENCE" ]; then
    echo "❌ Refusing to enable: GBAW_OPERATIONS_TRUSTED_AUDIENCE is required (must match the 06/07 stacks)." >&2
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

# --------------------------------------------------------------------------- #
# Finding 8: VERIFY the exact 07 E3 workflow the evaluator will start — it must
# live in THIS account and region and be a Standard (never Express) Step
# Functions state machine, because bounded autonomy reuses the reviewed E3
# Standard workflow with an identifier only. We DISCOVER nothing: the ARN is
# supplied; we only confirm it is the exact reviewed workflow.
# --------------------------------------------------------------------------- #
echo "🔎 Verifying the exact 07 E3 Standard workflow (same account/region, STANDARD type) ..."
# arn:aws:states:<region>:<account>:stateMachine:<name>
SM_REGION="$(printf '%s' "$EXECUTION_STATE_MACHINE_ARN" | awk -F: '{print $4}')"
SM_ACCOUNT="$(printf '%s' "$EXECUTION_STATE_MACHINE_ARN" | awk -F: '{print $5}')"
if [ "$SM_REGION" != "$AWS_REGION" ]; then
    echo "❌ Refusing to enable: the 07 workflow region '$SM_REGION' is not the deploy region '$AWS_REGION'." >&2
    exit 6
fi
if [ "$SM_ACCOUNT" != "$ACCOUNT_ID" ]; then
    echo "❌ Refusing to enable: the 07 workflow account '$SM_ACCOUNT' is not this account '$ACCOUNT_ID'." >&2
    exit 6
fi
SM_TYPE="$(aws stepfunctions describe-state-machine \
    "${AWS_PROFILE_ARGS[@]}" \
    --region "$AWS_REGION" \
    --state-machine-arn "$EXECUTION_STATE_MACHINE_ARN" \
    --query 'type' \
    --output text 2>/dev/null || true)"
if [ "$SM_TYPE" != "STANDARD" ]; then
    echo "❌ Refusing to enable: the 07 workflow is type '$SM_TYPE', not STANDARD." >&2
    echo "   Bounded autonomy reuses the reviewed E3 STANDARD workflow; refusing to start a non-Standard one." >&2
    exit 6
fi
echo "   07 workflow verified: STANDARD in $ACCOUNT_ID/$AWS_REGION."

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
# Finding 6: bind the workflow, fleet, and audience to the EXACT deployed 06/07
# values — not merely the same account/region/shape. The 07 stack is the source
# of truth: its ExecutionStateMachineArn OUTPUT, and its EnrolledFleetId and
# TrustedAudience PARAMETERS, must byte-equal what this wrapper will hand the
# evaluator, or we refuse before granting states:StartExecution authority.
# --------------------------------------------------------------------------- #
echo "🔎 Binding workflow/fleet/audience to the EXACT 07 deployed values ..."
STACK_07_SM_ARN="$(aws cloudformation describe-stacks "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --stack-name "$EXECUTION_STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='ExecutionStateMachineArn'].OutputValue" --output text 2>/dev/null || true)"
if [ -z "$STACK_07_SM_ARN" ] || [ "$STACK_07_SM_ARN" = "None" ]; then
    echo "❌ Refusing to enable: could not read the 07 ExecutionStateMachineArn output." >&2
    echo "   Deploy/verify the 07 execution stack first." >&2
    exit 6
fi
if [ "$EXECUTION_STATE_MACHINE_ARN" != "$STACK_07_SM_ARN" ]; then
    echo "❌ Refusing to enable: the supplied workflow ARN does not byte-equal the 07 output." >&2
    echo "   supplied: $EXECUTION_STATE_MACHINE_ARN" >&2
    echo "   07 output: $STACK_07_SM_ARN" >&2
    exit 6
fi
STACK_07_FLEET_ID="$(aws cloudformation describe-stacks "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --stack-name "$EXECUTION_STACK_NAME" \
    --query "Stacks[0].Parameters[?ParameterKey=='EnrolledFleetId'].ParameterValue" --output text 2>/dev/null || true)"
if [ -z "$STACK_07_FLEET_ID" ] || [ "$STACK_07_FLEET_ID" = "None" ]; then
    echo "❌ Refusing to enable: could not read the 07 EnrolledFleetId parameter." >&2
    exit 6
fi
if [ "$ENROLLED_FLEET_ID" != "$STACK_07_FLEET_ID" ]; then
    echo "❌ Refusing to enable: enrolled fleet id '$ENROLLED_FLEET_ID' does not match the 07 EnrolledFleetId '$STACK_07_FLEET_ID'." >&2
    exit 6
fi
# The exact enrolled fleet ARN for the VERIFIED account/region and this fleet id.
EXPECTED_FLEET_ARN="arn:aws:gamelift:${AWS_REGION}:${ACCOUNT_ID}:fleet/${ENROLLED_FLEET_ID}"
if [ "$ENROLLED_FLEET_ARN" != "$EXPECTED_FLEET_ARN" ]; then
    echo "❌ Refusing to enable: enrolled fleet ARN does not match the exact enrolled fleet." >&2
    echo "   supplied: $ENROLLED_FLEET_ARN" >&2
    echo "   expected: $EXPECTED_FLEET_ARN" >&2
    exit 6
fi
STACK_07_AUDIENCE="$(aws cloudformation describe-stacks "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --stack-name "$EXECUTION_STACK_NAME" \
    --query "Stacks[0].Parameters[?ParameterKey=='TrustedAudience'].ParameterValue" --output text 2>/dev/null || true)"
if [ -z "$STACK_07_AUDIENCE" ] || [ "$STACK_07_AUDIENCE" = "None" ]; then
    echo "❌ Refusing to enable: could not read the 07 TrustedAudience parameter (must match 06/07)." >&2
    exit 6
fi
if [ "$OPERATIONS_TRUSTED_AUDIENCE" != "$STACK_07_AUDIENCE" ]; then
    echo "❌ Refusing to enable: trusted audience '$OPERATIONS_TRUSTED_AUDIENCE' does not match the 07 TrustedAudience '$STACK_07_AUDIENCE'." >&2
    exit 6
fi
echo "   Exact bindings verified: workflow, enrolled fleet ($ENROLLED_FLEET_ID), and trusted audience all match the 07 deployment."

# --------------------------------------------------------------------------- #
# #440 review: bind the 06 (observation) and 08 (control-plane) coordinates to
# their EXACT deployed stack values, plus the shared 07 identity values. The
# earlier activation only required these env vars to be non-empty; a value that
# does not byte-equal the deployed stack output/parameter is a silent
# mis-binding that would let the evaluator address the wrong table, CMK,
# tenant/workspace/audience, kill switch, or observation source. Every mismatch
# below is a hard refusal before any deploy.
# --------------------------------------------------------------------------- #
echo "🔎 Binding the 06 operations/observation coordinates to the EXACT deployed values ..."
if ! aws cloudformation describe-stacks "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --stack-name "$OBSERVATION_STACK_NAME" >/dev/null 2>&1; then
    echo "❌ Refusing to enable: the 06 observation stack '$OBSERVATION_STACK_NAME' was not found; deploy 06 first." >&2
    exit 6
fi

# 06 OUTPUTS: operations table + customer-managed KMS key.
STACK_06_TABLE="$(aws cloudformation describe-stacks "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --stack-name "$OBSERVATION_STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='OperationsTableName'].OutputValue" --output text 2>/dev/null || true)"
if [ -z "$STACK_06_TABLE" ] || [ "$STACK_06_TABLE" = "None" ]; then
    echo "❌ Refusing to enable: could not read the 06 OperationsTableName output." >&2
    exit 6
fi
if [ "$OPERATIONS_TABLE_NAME" != "$STACK_06_TABLE" ]; then
    echo "❌ Refusing to enable: operations table '$OPERATIONS_TABLE_NAME' does not match the 06 OperationsTableName '$STACK_06_TABLE'." >&2
    exit 6
fi
STACK_06_KMS="$(aws cloudformation describe-stacks "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --stack-name "$OBSERVATION_STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='OperationsKmsKeyArn'].OutputValue" --output text 2>/dev/null || true)"
if [ -z "$STACK_06_KMS" ] || [ "$STACK_06_KMS" = "None" ]; then
    echo "❌ Refusing to enable: could not read the 06 OperationsKmsKeyArn output." >&2
    exit 6
fi
if [ "$OPERATIONS_KMS_KEY_ARN" != "$STACK_06_KMS" ]; then
    echo "❌ Refusing to enable: operations CMK '$OPERATIONS_KMS_KEY_ARN' does not match the 06 OperationsKmsKeyArn '$STACK_06_KMS'." >&2
    exit 6
fi

# 06 PARAMETERS: the shared tenant / workspace / trusted audience identity. The
# 06, 07, and 08 stacks all carry the same values; 06 is the identity source.
STACK_06_TENANT="$(aws cloudformation describe-stacks "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --stack-name "$OBSERVATION_STACK_NAME" \
    --query "Stacks[0].Parameters[?ParameterKey=='TenantId'].ParameterValue" --output text 2>/dev/null || true)"
if [ -z "$STACK_06_TENANT" ] || [ "$STACK_06_TENANT" = "None" ]; then
    echo "❌ Refusing to enable: could not read the 06 TenantId parameter." >&2
    exit 6
fi
if [ "$OPERATIONS_TENANT_ID" != "$STACK_06_TENANT" ]; then
    echo "❌ Refusing to enable: tenant '$OPERATIONS_TENANT_ID' does not match the 06 TenantId '$STACK_06_TENANT'." >&2
    exit 6
fi
STACK_06_WORKSPACE="$(aws cloudformation describe-stacks "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --stack-name "$OBSERVATION_STACK_NAME" \
    --query "Stacks[0].Parameters[?ParameterKey=='WorkspaceId'].ParameterValue" --output text 2>/dev/null || true)"
if [ -z "$STACK_06_WORKSPACE" ] || [ "$STACK_06_WORKSPACE" = "None" ]; then
    echo "❌ Refusing to enable: could not read the 06 WorkspaceId parameter." >&2
    exit 6
fi
if [ "$OPERATIONS_WORKSPACE_ID" != "$STACK_06_WORKSPACE" ]; then
    echo "❌ Refusing to enable: workspace '$OPERATIONS_WORKSPACE_ID' does not match the 06 WorkspaceId '$STACK_06_WORKSPACE'." >&2
    exit 6
fi
STACK_06_AUDIENCE="$(aws cloudformation describe-stacks "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --stack-name "$OBSERVATION_STACK_NAME" \
    --query "Stacks[0].Parameters[?ParameterKey=='TrustedAudience'].ParameterValue" --output text 2>/dev/null || true)"
if [ -z "$STACK_06_AUDIENCE" ] || [ "$STACK_06_AUDIENCE" = "None" ]; then
    echo "❌ Refusing to enable: could not read the 06 TrustedAudience parameter." >&2
    exit 6
fi
if [ "$OPERATIONS_TRUSTED_AUDIENCE" != "$STACK_06_AUDIENCE" ]; then
    echo "❌ Refusing to enable: trusted audience '$OPERATIONS_TRUSTED_AUDIENCE' does not match the 06 TrustedAudience '$STACK_06_AUDIENCE'." >&2
    exit 6
fi

# 06 OBSERVATION LAMBDA: the evaluator loads its trusted observation from this
# exact 06 function. Verify it exists (least-privilege read) rather than assume.
if ! aws lambda get-function "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --function-name "$OBSERVE_FUNCTION_NAME" >/dev/null 2>&1; then
    echo "❌ Refusing to enable: the 06 observation Lambda '$OBSERVE_FUNCTION_NAME' was not found in $ACCOUNT_ID/$AWS_REGION." >&2
    exit 6
fi
echo "   Exact 06 bindings verified: operations table, CMK, tenant/workspace/audience, and observation Lambda."

echo "🔎 Binding the 08 kill-switch AppConfig coordinate to the EXACT deployed values ..."
if ! aws cloudformation describe-stacks "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --stack-name "$CONTROL_STACK_NAME" >/dev/null 2>&1; then
    echo "❌ Refusing to enable: the 08 control-plane stack '$CONTROL_STACK_NAME' was not found; deploy 08 first." >&2
    exit 6
fi
STACK_08_APP="$(aws cloudformation describe-stacks "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --stack-name "$CONTROL_STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='ControlApplicationId'].OutputValue" --output text 2>/dev/null || true)"
if [ -z "$STACK_08_APP" ] || [ "$STACK_08_APP" = "None" ]; then
    echo "❌ Refusing to enable: could not read the 08 ControlApplicationId output." >&2
    exit 6
fi
if [ "$KILL_SWITCH_APPLICATION_ID" != "$STACK_08_APP" ]; then
    echo "❌ Refusing to enable: kill-switch application '$KILL_SWITCH_APPLICATION_ID' does not match the 08 ControlApplicationId '$STACK_08_APP'." >&2
    exit 6
fi
STACK_08_ENV="$(aws cloudformation describe-stacks "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --stack-name "$CONTROL_STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='ControlEnvironmentId'].OutputValue" --output text 2>/dev/null || true)"
if [ -z "$STACK_08_ENV" ] || [ "$STACK_08_ENV" = "None" ]; then
    echo "❌ Refusing to enable: could not read the 08 ControlEnvironmentId output." >&2
    exit 6
fi
if [ "$KILL_SWITCH_ENVIRONMENT_ID" != "$STACK_08_ENV" ]; then
    echo "❌ Refusing to enable: kill-switch environment '$KILL_SWITCH_ENVIRONMENT_ID' does not match the 08 ControlEnvironmentId '$STACK_08_ENV'." >&2
    exit 6
fi
STACK_08_PROFILE="$(aws cloudformation describe-stacks "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --stack-name "$CONTROL_STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='KillSwitchProfileId'].OutputValue" --output text 2>/dev/null || true)"
if [ -z "$STACK_08_PROFILE" ] || [ "$STACK_08_PROFILE" = "None" ]; then
    echo "❌ Refusing to enable: could not read the 08 KillSwitchProfileId output." >&2
    exit 6
fi
if [ "$KILL_SWITCH_PROFILE_ID" != "$STACK_08_PROFILE" ]; then
    echo "❌ Refusing to enable: kill-switch profile '$KILL_SWITCH_PROFILE_ID' does not match the 08 KillSwitchProfileId '$STACK_08_PROFILE'." >&2
    exit 6
fi
echo "   Exact 08 bindings verified: kill-switch application, environment, and profile all match the 08 deployment."


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

echo "🚀 Phase 1: deploying $STACK_NAME DISABLED (Provisioned=true, AutonomyMode=disabled) ..."
# Two-phase enable (Finding 3): provision 09 DISABLED first so the AppConfig
# autonomy coordinate and every resource exist, but the schedule stays DISABLED
# and the evaluator fails closed. This prevents any scheduled evaluation from
# firing before the 07 executor pre-write hook is installed and verified.
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
        "AutonomyMode=disabled" \
        "Environment=$ENVIRONMENT" \
        "OperationsTableName=$OPERATIONS_TABLE_NAME" \
        "OperationsKmsKeyArn=$OPERATIONS_KMS_KEY_ARN" \
        "ExecutionStateMachineArn=$EXECUTION_STATE_MACHINE_ARN" \
        "EnrolledFleetId=$ENROLLED_FLEET_ID" \
        "EnrolledLocation=$ENROLLED_LOCATION" \
        "TenantId=$OPERATIONS_TENANT_ID" \
        "WorkspaceId=$OPERATIONS_WORKSPACE_ID" \
        "TrustedAudience=$OPERATIONS_TRUSTED_AUDIENCE" \
        "EnrolledFleetArn=$ENROLLED_FLEET_ARN" \
        "AutonomySubject=$AUTONOMY_SUBJECT" \
        "AutonomyClient=$AUTONOMY_CLIENT" \
        "AutonomyPolicyId=$AUTONOMY_POLICY_ID" \
        "AutonomyPolicyVersion=$AUTONOMY_POLICY_VERSION" \
        "AutonomyPolicyHash=$AUTONOMY_POLICY_HASH" \
        "AutonomyStateId=$AUTONOMY_STATE_ID" \
        "ScheduledObservationOperationId=$SCHEDULED_OBSERVATION_OPERATION_ID" \
        "ScheduledDesired=$SCHEDULED_DESIRED" \
        "ScheduledMinimum=$SCHEDULED_MINIMUM" \
        "ScheduledMaximum=$SCHEDULED_MAXIMUM" \
        "KillSwitchApplicationId=$KILL_SWITCH_APPLICATION_ID" \
        "KillSwitchEnvironmentId=$KILL_SWITCH_ENVIRONMENT_ID" \
        "KillSwitchProfileId=$KILL_SWITCH_PROFILE_ID" \
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

# --------------------------------------------------------------------------- #
# Finding 3, phase 2: UPDATE and VERIFY the 07 executor pre-write hook BEFORE
# enabling the 09 evaluator plane. The 09 evaluator only STARTS the workflow;
# the executor's own pre-write hook (in 07) is the gate that re-checks the
# autonomy switch and durable window before the single provider write.
#
# We apply the REVIEWED 07 template (this commit's 07-operations-execution.yaml)
# through S3 — NOT --use-previous-template, which cannot install this commit's
# new autonomy parameters/IAM/wiring onto a pre-E5 07 stack — and REQUIRE every
# autonomy parameter to exist on the reviewed template before proceeding.
# --------------------------------------------------------------------------- #
EXECUTION_TEMPLATE="${GBAW_OPERATIONS_EXECUTION_TEMPLATE:-$PROJECT_ROOT/infrastructure/cloudformation/07-operations-execution.yaml}"
if [ ! -f "$EXECUTION_TEMPLATE" ]; then
    echo "❌ Refusing: the reviewed 07 template '$EXECUTION_TEMPLATE' is missing." >&2
    exit 6
fi
# Require the reviewed template to declare every autonomy parameter we set, so a
# stale/pre-E5 template cannot silently drop them.
for required in AutonomyMode AutonomyStateMachineArn AutonomySwitchProfileId \
    AutonomyApplicationId AutonomyEnvironmentId AutonomyPolicyId AutonomyPolicyVersion \
    AutonomyPolicyHash AutonomyStateId AutonomySubject AutonomyClient; do
    if ! grep -qE "^  ${required}:" "$EXECUTION_TEMPLATE"; then
        echo "❌ Refusing: the reviewed 07 template does not declare required autonomy parameter '$required'." >&2
        exit 6
    fi
done
if ! aws cloudformation describe-stacks \
    "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --stack-name "$EXECUTION_STACK_NAME" >/dev/null 2>&1; then
    echo "❌ Refusing: the 07 execution stack '$EXECUTION_STACK_NAME' was not found; deploy 07 first." >&2
    exit 6
fi

echo "🔗 Reading the 09 autonomy AppConfig ids to bind the 07 executor pre-write hook ..."
AUTONOMY_APP_ID="$(aws cloudformation describe-stacks "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='AutonomyApplicationId'].OutputValue" --output text 2>/dev/null || true)"
AUTONOMY_ENV_ID="$(aws cloudformation describe-stacks "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='AutonomyEnvironmentId'].OutputValue" --output text 2>/dev/null || true)"
AUTONOMY_PROFILE_ID="$(aws cloudformation describe-stacks "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='AutonomySwitchProfileId'].OutputValue" --output text 2>/dev/null || true)"
if [ -z "$AUTONOMY_APP_ID" ] || [ "$AUTONOMY_APP_ID" = "None" ] \
    || [ -z "$AUTONOMY_ENV_ID" ] || [ "$AUTONOMY_ENV_ID" = "None" ] \
    || [ -z "$AUTONOMY_PROFILE_ID" ] || [ "$AUTONOMY_PROFILE_ID" = "None" ]; then
    echo "❌ Refusing: could not read the 09 autonomy AppConfig ids to bind the 07 hook." >&2
    exit 6
fi

# Upload the reviewed 07 template to S3 (it exceeds the 51,200-byte inline
# limit) and address it via --template-url. The transient object is deleted by
# the EXIT trap alongside the 09 template object.
EXEC_TEMPLATE_HASH="$(shasum -a 256 "$EXECUTION_TEMPLATE" | awk '{print $1}')"
EXEC_TEMPLATE_S3_KEY="${TEMPLATE_S3_PREFIX}/exec-${EXEC_TEMPLATE_HASH}.yaml"
EXEC_TEMPLATE_URL="https://s3.${AWS_REGION}.amazonaws.com/${GBAW_OPERATIONS_ARTIFACT_BUCKET}/${EXEC_TEMPLATE_S3_KEY}"
aws s3api put-object \
    "${AWS_PROFILE_ARGS[@]}" \
    --bucket "$GBAW_OPERATIONS_ARTIFACT_BUCKET" \
    --key "$EXEC_TEMPLATE_S3_KEY" \
    --body "$EXECUTION_TEMPLATE" \
    --region "$AWS_REGION" >/dev/null
# Extend cleanup to also remove the 07 template object.
cleanup_exec_template() {
    aws s3api delete-object \
        "${AWS_PROFILE_ARGS[@]}" \
        --bucket "$GBAW_OPERATIONS_ARTIFACT_BUCKET" \
        --key "$EXEC_TEMPLATE_S3_KEY" \
        --region "$AWS_REGION" >/dev/null 2>&1 || true
}
trap 'cleanup_autonomy; cleanup_exec_template' EXIT

echo "🔗 Phase 2: applying the REVIEWED 07 template with AutonomyMode=operate ..."
# Build the 07 parameter list dynamically from the LIVE stack (drift-proof):
# reuse every current 07 value except the autonomy ones we set explicitly.
EXEC_PARAM_KEYS="$(aws cloudformation describe-stacks \
    "${AWS_PROFILE_ARGS[@]}" \
    --region "$AWS_REGION" \
    --stack-name "$EXECUTION_STACK_NAME" \
    --query 'Stacks[0].Parameters[].ParameterKey' \
    --output text 2>/dev/null || true)"
declare -A EXEC_SET
EXEC_SET[AutonomyMode]="ParameterKey=AutonomyMode,ParameterValue=operate"
EXEC_SET[AutonomyStateMachineArn]="ParameterKey=AutonomyStateMachineArn,ParameterValue=$EXECUTION_STATE_MACHINE_ARN"
EXEC_SET[AutonomySwitchProfileId]="ParameterKey=AutonomySwitchProfileId,ParameterValue=$AUTONOMY_PROFILE_ID"
EXEC_SET[AutonomyApplicationId]="ParameterKey=AutonomyApplicationId,ParameterValue=$AUTONOMY_APP_ID"
EXEC_SET[AutonomyEnvironmentId]="ParameterKey=AutonomyEnvironmentId,ParameterValue=$AUTONOMY_ENV_ID"
EXEC_SET[AutonomyPolicyId]="ParameterKey=AutonomyPolicyId,ParameterValue=$AUTONOMY_POLICY_ID"
EXEC_SET[AutonomyPolicyVersion]="ParameterKey=AutonomyPolicyVersion,ParameterValue=$AUTONOMY_POLICY_VERSION"
EXEC_SET[AutonomyPolicyHash]="ParameterKey=AutonomyPolicyHash,ParameterValue=$AUTONOMY_POLICY_HASH"
EXEC_SET[AutonomyStateId]="ParameterKey=AutonomyStateId,ParameterValue=$AUTONOMY_STATE_ID"
EXEC_SET[AutonomySubject]="ParameterKey=AutonomySubject,ParameterValue=$AUTONOMY_SUBJECT"
EXEC_SET[AutonomyClient]="ParameterKey=AutonomyClient,ParameterValue=$AUTONOMY_CLIENT"
EXEC_PARAMS=()
# Set the autonomy params explicitly; reuse every OTHER live 07 param. Because we
# apply the reviewed template (which may add parameters absent from the live
# stack), any reviewed-template parameter we do not set explicitly and that is
# absent from the live stack keeps its template default.
for key in $EXEC_PARAM_KEYS; do
    if [ -n "${EXEC_SET[$key]:-}" ]; then
        EXEC_PARAMS+=("${EXEC_SET[$key]}")
        unset "EXEC_SET[$key]"
    else
        EXEC_PARAMS+=("ParameterKey=${key},UsePreviousValue=true")
    fi
done
# Append any autonomy params NOT present on the live stack (a pre-E5 stack) so
# the reviewed-template update installs them explicitly.
for remaining in "${EXEC_SET[@]}"; do
    EXEC_PARAMS+=("$remaining")
done

EXEC_ERR="$(mktemp)"
if aws cloudformation update-stack \
    "${AWS_PROFILE_ARGS[@]}" \
    --region "$AWS_REGION" \
    --stack-name "$EXECUTION_STACK_NAME" \
    --template-url "$EXEC_TEMPLATE_URL" \
    --capabilities CAPABILITY_NAMED_IAM \
    --parameters "${EXEC_PARAMS[@]}" 2>"$EXEC_ERR"; then
    rm -f "$EXEC_ERR"
    # HONOR the waiter (Finding 3): a rolled-back 07 update must abort BEFORE 09
    # is enabled. No '|| true'.
    if ! aws cloudformation wait stack-update-complete \
        "${AWS_PROFILE_ARGS[@]}" \
        --region "$AWS_REGION" \
        --stack-name "$EXECUTION_STACK_NAME"; then
        echo "❌ The 07 executor update did not reach UPDATE_COMPLETE; aborting BEFORE enabling 09." >&2
        exit 6
    fi
elif grep -q "No updates are to be performed" "$EXEC_ERR" 2>/dev/null; then
    rm -f "$EXEC_ERR"
    echo "   07 executor already bound to this autonomy configuration (no change)."
else
    echo "❌ Failed to update the 07 executor pre-write hook; aborting BEFORE enabling 09." >&2
    cat "$EXEC_ERR" >&2 || true
    rm -f "$EXEC_ERR"
    exit 6
fi
# VERIFY the 07 executor is now observed AutonomyMode=operate.
EXEC_MODE_NOW="$(aws cloudformation describe-stacks "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" \
    --stack-name "$EXECUTION_STACK_NAME" \
    --query "Stacks[0].Parameters[?ParameterKey=='AutonomyMode'].ParameterValue" --output text 2>/dev/null || true)"
if [ "$EXEC_MODE_NOW" != "operate" ]; then
    echo "❌ 07 executor AutonomyMode is '$EXEC_MODE_NOW', not 'operate'; aborting BEFORE enabling 09." >&2
    exit 6
fi
echo "   ✅ 07 executor pre-write autonomy hook enabled, bound, and verified operate."

# --------------------------------------------------------------------------- #
# Finding 3, phase 3: only NOW enable the 09 evaluator plane (AutonomyMode=
# operate) so the schedule/evaluator engage AFTER the executor hook exists.
# --------------------------------------------------------------------------- #
echo "🚀 Phase 3: enabling the 09 evaluator plane (AutonomyMode=operate) ..."
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
        "TrustedAudience=$OPERATIONS_TRUSTED_AUDIENCE" \
        "EnrolledFleetArn=$ENROLLED_FLEET_ARN" \
        "AutonomySubject=$AUTONOMY_SUBJECT" \
        "AutonomyClient=$AUTONOMY_CLIENT" \
        "AutonomyPolicyId=$AUTONOMY_POLICY_ID" \
        "AutonomyPolicyVersion=$AUTONOMY_POLICY_VERSION" \
        "AutonomyPolicyHash=$AUTONOMY_POLICY_HASH" \
        "AutonomyStateId=$AUTONOMY_STATE_ID" \
        "ScheduledObservationOperationId=$SCHEDULED_OBSERVATION_OPERATION_ID" \
        "ScheduledDesired=$SCHEDULED_DESIRED" \
        "ScheduledMinimum=$SCHEDULED_MINIMUM" \
        "ScheduledMaximum=$SCHEDULED_MAXIMUM" \
        "KillSwitchApplicationId=$KILL_SWITCH_APPLICATION_ID" \
        "KillSwitchEnvironmentId=$KILL_SWITCH_ENVIRONMENT_ID" \
        "KillSwitchProfileId=$KILL_SWITCH_PROFILE_ID" \
        "AppConfigExtensionLayerArn=$APPCONFIG_EXTENSION_LAYER_ARN" \
        "CodeS3Bucket=$GBAW_OPERATIONS_ARTIFACT_BUCKET" \
        "EvaluatorCodeS3Key=$EVALUATOR_S3_KEY"

echo "✅ Enabled: 07 executor pre-write hook (verified operate) THEN the 09 evaluator plane."
echo "   The evaluator starts only the exact E3 workflow $EXECUTION_STATE_MACHINE_ARN."
echo "   Emergency disable (reversible, deletes nothing): disable-operations-autonomy.sh --confirm"
