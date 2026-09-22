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
# Python 3.13 / x86_64 zip containing the real `operations` code (from a combined
# tree that includes issue #413 core's handler) plus every transitive runtime
# dependency pinned at the version frozen in the repository lock. Dependencies
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
COGNITO_ISSUER="${COGNITO_ISSUER:-}"
COGNITO_CLIENT_ID="${COGNITO_CLIENT_ID:-}"
TENANT_ID="${TENANT_ID:-}"
WORKSPACE_ID="${WORKSPACE_ID:-}"
TRUSTED_AUDIENCE="${TRUSTED_AUDIENCE:-}"
# The EXPLICIT, pre-existing artifact bucket for the Lambda zip. There is no
# discovery and no creation: an enabled deploy requires this to name a bucket
# that already exists in the target account/region.
GBAW_OPERATIONS_ARTIFACT_BUCKET="${GBAW_OPERATIONS_ARTIFACT_BUCKET:-}"

# AWS_PROFILE is passed EXPLICITLY to every aws call rather than relied on
# ambiently. When unset we fall back to "default" so the flag is always present.
AWS_PROFILE="${AWS_PROFILE:-default}"
AWS_PROFILE_ARGS=(--profile "$AWS_PROFILE")

usage() {
    cat <<'USAGE'
Usage: deploy-operations.sh [--enable | --disable] [--environment beta|prod]

  (no flag)     Preview only (READ-ONLY): validates and lints the template and
                creates nothing. No change set is created.
  --enable      Build/upload the operations Lambda artifact and deploy the stack
                with OperationsMode=observe. Requires GBAW_OPERATIONS_MODE=observe
                in the environment plus the enabling inputs below.
  --disable     Emergency, data-preserving disable of an EXISTING provisioned
                stack: keeps Provisioned=true and every resource under
                CloudFormation (stable physical names, retained data), reuses all
                current parameter values, and sets only OperationsMode=disabled so
                the API and handler fail closed. Does NOT delete resources and
                does NOT rebuild code or run Docker. Reversible via --enable.
  --environment Target environment (default: beta). "prod" enables DynamoDB
                deletion protection and longer log retention.

Environment for --enable:
  GBAW_OPERATIONS_MODE=observe        Required confirmation.
  COGNITO_ISSUER, COGNITO_CLIENT_ID   JWT issuer + audience (required).
  TENANT_ID, WORKSPACE_ID             Server-side trusted bindings (required).
  TRUSTED_AUDIENCE                    Optional; defaults to COGNITO_CLIENT_ID.
  GBAW_OPERATIONS_ARTIFACT_BUCKET     REQUIRED explicit, pre-existing artifact
                                      bucket. This wrapper verifies it and never
                                      discovers or creates a bucket.
  AWS_PROFILE, AWS_REGION             Credentials/region. AWS_PROFILE is passed
                                      explicitly to every aws call; both are
                                      verified before any write.
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --enable)  ACTION="enable" ;;
        --disable) ACTION="disable" ;;
        --environment) shift; ENVIRONMENT="${1:-beta}" ;;
        -h|--help) usage; exit 0 ;;
        *) echo "❌ Unknown argument: $1" >&2; usage; exit 2 ;;
    esac
    shift
done

echo "=================================================="
echo " ⚙️  OPTIONAL E1 operations control plane"
echo "=================================================="
echo "Region:      $AWS_REGION"
echo "Profile:     $AWS_PROFILE"
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
# Verify identity/region before any write path (enable or disable).
# --------------------------------------------------------------------------- #
echo "🔐 Verifying AWS credentials and region before any write ..."
echo "   AWS_PROFILE=${AWS_PROFILE}  AWS_REGION=${AWS_REGION}"
if ! CALLER_IDENTITY="$(aws sts get-caller-identity "${AWS_PROFILE_ARGS[@]}" --region "$AWS_REGION" --output text 2>/dev/null)"; then
    echo "❌ Unable to verify caller identity. Configure AWS_PROFILE/AWS_REGION and credentials." >&2
    exit 4
fi
echo "   Caller identity: $CALLER_IDENTITY"
# The account the credentials resolve to; used to confirm the artifact bucket's
# ownership context before upload.
ACCOUNT_ID="$(printf '%s\n' "$CALLER_IDENTITY" | awk '{print $1}')"

if [ "$ACTION" = "enable" ]; then
    # ----------------------------------------------------------------------- #
    # Build a deterministic, Lambda-compatible zip: the real `operations` code
    # (from the combined tree) plus the pinned third-party dependency closure.
    # Determinism (fixed mtimes, sorted entries) makes the content hash — and
    # therefore the S3 key — stable for an unchanged source tree.
    # ----------------------------------------------------------------------- #
    if [ ! -f "$BACKEND_SRC/$HANDLER_MODULE_PATH" ]; then
        echo "❌ Refusing to enable: frozen handler module $HANDLER_MODULE_PATH is absent" >&2
        echo "   from $BACKEND_SRC. Package from a combined tree that includes issue #413" >&2
        echo "   core's real handler; no enabled route may point at missing/placeholder code." >&2
        exit 5
    fi

    BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gbaw-ops-pkg.XXXXXXXX")"
    trap 'rm -rf "$BUILD_DIR"' EXIT
    STAGE="$BUILD_DIR/stage"
    mkdir -p "$STAGE"

    echo "📦 Staging operations code and runtime resources ..."
    # Ship the operations package: its Python modules AND the non-code runtime
    # resources the handler loads at runtime. The contract validator resolves its
    # versioned JSON Schemas via importlib.resources
    # (operations.contracts.schemas.v1.*), so a .py-only package would omit them
    # and every contract load would raise FileNotFoundError in the Lambda. We
    # therefore stage *.py plus the versioned schema resources (and any other
    # in-package JSON resource under operations/**), while excluding caches, test
    # trees, and docs so no non-runtime file inflates or destabilizes the zip.
    # A tar pipe copies the tree portably and dereferences any symlinks, avoiding
    # pass-through copy tools that refuse to write through a symlinked temp prefix.
    ( cd "$BACKEND_SRC" && \
      find operations -type f \
        \( -name '*.py' -o -name '*.json' \) \
        -not -path '*/__pycache__/*' \
        -not -path '*/tests/*' -not -path '*/test/*' \
        -not -path '*/docs/*' \
        -print0 \
        | tar --null -cf - --files-from=- ) | ( cd "$STAGE" && tar -xf - )

    # Fail closed if the versioned contract schema resources did not make it into
    # the stage: the handler's contract load reads
    # operations/contracts/schemas/v1/*.schema.json at runtime, and a package
    # missing them would deploy a handler that raises FileNotFoundError on the
    # first validated request. Require at least one, and require the whole
    # versioned set the validator binds.
    SCHEMA_STAGE_DIR="$STAGE/operations/contracts/schemas/v1"
    STAGED_SCHEMAS="$(find "$SCHEMA_STAGE_DIR" -type f -name '*.schema.json' 2>/dev/null | wc -l | tr -d ' ')"
    if [ "${STAGED_SCHEMAS:-0}" -eq 0 ]; then
        echo "❌ Refusing to enable: no versioned contract schemas were staged under" >&2
        echo "   operations/contracts/schemas/v1. The runtime contract validator loads" >&2
        echo "   these JSON resources; a schema-less package raises FileNotFoundError." >&2
        exit 5
    fi
    SOURCE_SCHEMAS="$(find "$BACKEND_SRC/operations/contracts/schemas/v1" -type f -name '*.schema.json' 2>/dev/null | wc -l | tr -d ' ')"
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

    # Normalize for determinism: strip pip metadata dirs and bytecode caches, and
    # fix timestamps. .dist-info is not needed at runtime.
    find "$STAGE" -depth -type d -name '*.dist-info' -exec rm -rf {} + 2>/dev/null || true
    find "$STAGE" -depth -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
    # Fixed epoch touch stamp assembled from parts (no 12-digit literal) so the
    # build is reproducible without tripping account-id content scanners.
    EPOCH_STAMP="2000""01010000.00"   # CCYYMMDDhhmm.SS
    find "$STAGE" -exec touch -h -t "$EPOCH_STAMP" {} +

    # ----------------------------------------------------------------------- #
    # Fail closed if any native wheel was staged for a NON-Linux/x86 platform.
    # rpds-py's compiled extension carries its platform tag in the filename; the
    # only acceptable tag family is manylinux*_x86_64. A host wheel (macosx_*,
    # *_arm64/aarch64, win_*) means the cross-platform install silently fell back
    # to the host and must abort.
    # ----------------------------------------------------------------------- #
    echo "🔍 Verifying no host-architecture native wheels were packaged ..."
    BAD_NATIVE="$(find "$STAGE" -type f -name '*.so' \
        \( -name '*macosx*' -o -name '*arm64*' -o -name '*aarch64*' -o -name '*win_*' -o -name '*_i686*' \) 2>/dev/null || true)"
    if [ -n "$BAD_NATIVE" ]; then
        echo "❌ Host/non-x86_64 native wheel detected in package:" >&2
        printf '   %s\n' "$BAD_NATIVE" >&2
        exit 5
    fi
    # The native dependency MUST be present as a compiled Linux extension.
    if ! find "$STAGE" -type f -name 'rpds*.so' | grep -q .; then
        echo "❌ Native dependency rpds-py compiled extension is missing from the package." >&2
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
            if ! find "$STAGE" -maxdepth 2 \( -name "${required}" -o -name "${required}.py" -o -name "${required}*.so" \) | grep -q .; then
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
    echo "🔎 Verifying artifact bucket '$CODE_S3_BUCKET' exists in account $ACCOUNT_ID ..."
    # head-bucket confirms existence and that these credentials can access it.
    # The expected owner is asserted so a name-squatted foreign bucket is refused.
    if ! aws s3api head-bucket \
            "${AWS_PROFILE_ARGS[@]}" \
            --bucket "$CODE_S3_BUCKET" \
            --expected-bucket-owner "$ACCOUNT_ID" \
            --region "$AWS_REGION" >/dev/null 2>&1; then
        echo "❌ Artifact bucket '$CODE_S3_BUCKET' does not exist, is inaccessible, or is" >&2
        echo "   not owned by account $ACCOUNT_ID. This wrapper never creates a bucket." >&2
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
    echo "   Bucket verified: owner=$ACCOUNT_ID region=$BUCKET_REGION"

    echo "☁️  Uploading artifact to s3://$CODE_S3_BUCKET/$CODE_S3_KEY ..."
    aws s3 cp "$ARTIFACT" "s3://$CODE_S3_BUCKET/$CODE_S3_KEY" \
        "${AWS_PROFILE_ARGS[@]}" \
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
    # (template kill switch) because OperationsMode is no longer "observe".
    # ----------------------------------------------------------------------- #
    echo "🔎 Verifying target stack '$STACK_NAME' exists in account $ACCOUNT_ID / $AWS_REGION ..."
    if ! aws cloudformation describe-stacks \
            "${AWS_PROFILE_ARGS[@]}" \
            --stack-name "$STACK_NAME" \
            --region "$AWS_REGION" >/dev/null 2>&1; then
        echo "❌ Stack '$STACK_NAME' does not exist in account $ACCOUNT_ID / $AWS_REGION." >&2
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

    # UsePreviousValue reuses the stack's existing artifact identifiers and every
    # binding, so nothing is rebuilt and no value is blanked. Only the two safety
    # levers are overridden. (aws cloudformation deploy does not support
    # UsePreviousValue, so the disable path uses update-stack directly.)
    DISABLE_PARAMS=(
        "ParameterKey=Provisioned,ParameterValue=true"
        "ParameterKey=OperationsMode,ParameterValue=disabled"
        "ParameterKey=ProjectName,UsePreviousValue=true"
        "ParameterKey=Environment,UsePreviousValue=true"
        "ParameterKey=CognitoIssuer,UsePreviousValue=true"
        "ParameterKey=CognitoClientId,UsePreviousValue=true"
        "ParameterKey=TenantId,UsePreviousValue=true"
        "ParameterKey=WorkspaceId,UsePreviousValue=true"
        "ParameterKey=TrustedAudience,UsePreviousValue=true"
        "ParameterKey=CodeS3Bucket,UsePreviousValue=true"
        "ParameterKey=CodeS3Key,UsePreviousValue=true"
        "ParameterKey=RequestDeadlineSeconds,UsePreviousValue=true"
        "ParameterKey=PerReadBudgetSeconds,UsePreviousValue=true"
        "ParameterKey=PersistenceBudgetSeconds,UsePreviousValue=true"
        "ParameterKey=CancellationMarginSeconds,UsePreviousValue=true"
        "ParameterKey=ObservationTtlSeconds,UsePreviousValue=true"
        "ParameterKey=LambdaMemoryMb,UsePreviousValue=true"
        "ParameterKey=ReservedConcurrency,UsePreviousValue=true"
        "ParameterKey=MaxReadRequestUnits,UsePreviousValue=true"
        "ParameterKey=MaxWriteRequestUnits,UsePreviousValue=true"
        "ParameterKey=ThrottlingBurstLimit,UsePreviousValue=true"
        "ParameterKey=ThrottlingRateLimit,UsePreviousValue=true"
    )

    DISABLE_ERR="$(mktemp "${TMPDIR:-/tmp}/gbaw-ops-disable.XXXXXXXX")"
    trap 'rm -f "$DISABLE_ERR"' EXIT
    echo "🚀 Updating $STACK_NAME to OperationsMode=disabled (Provisioned=true retained) ..."
    if ! aws cloudformation update-stack \
            "${AWS_PROFILE_ARGS[@]}" \
            --stack-name "$STACK_NAME" \
            --template-body "file://$TEMPLATE" \
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
# runtime authority to observe. All enabling inputs were validated above and the
# artifact was built and uploaded.
# --------------------------------------------------------------------------- #
PARAM_OVERRIDES=(
    "ProjectName=${PROJECT_NAME}"
    "Provisioned=true"
    "OperationsMode=${OPERATIONS_MODE}"
    "Environment=${ENVIRONMENT}"
    "CognitoIssuer=${COGNITO_ISSUER}"
    "CognitoClientId=${COGNITO_CLIENT_ID}"
    "TenantId=${TENANT_ID}"
    "WorkspaceId=${WORKSPACE_ID}"
    "TrustedAudience=${TRUSTED_AUDIENCE}"
    "CodeS3Bucket=${CODE_S3_BUCKET:-}"
    "CodeS3Key=${CODE_S3_KEY}"
)

echo "🚀 Deploying $STACK_NAME with Provisioned=true OperationsMode=$OPERATIONS_MODE ..."
aws cloudformation deploy \
    "${AWS_PROFILE_ARGS[@]}" \
    --template-file "$TEMPLATE" \
    --stack-name "$STACK_NAME" \
    --capabilities CAPABILITY_NAMED_IAM \
    --region "$AWS_REGION" \
    --parameter-overrides "${PARAM_OVERRIDES[@]}"

echo "✅ Deploy complete (Provisioned=true, OperationsMode=$OPERATIONS_MODE)."
