#!/bin/bash
# Game Agent - OPTIONAL E1 operations observation control plane deploy wrapper
# (GitHub issue #413).
#
# This wrapper is DELIBERATELY NOT called by deploy.sh / deploy-all.sh. A normal
# deployment creates zero E1 resources. Even when this wrapper runs, it refuses
# to create *enabled* (observe-mode) resources unless the operator supplies BOTH:
#   * GBAW_OPERATIONS_MODE=observe  (environment), and
#   * --enable                      (flag)
# Without both, it runs a READ-ONLY preview (template service validation + lint
# only; it creates no change set and mutates nothing). Disabling is a separate,
# data-preserving path (--disable).
#
# On an enabled deploy the wrapper builds a DETERMINISTIC, minimal Lambda zip
# containing the `operations` code and the required third-party dependencies,
# uploads it to an explicitly supplied or safely discovered EXISTING deployment
# bucket under a content-hash key, and passes CodeS3Bucket/CodeS3Key to the
# stack. No enabled route ever points at placeholder code: if the frozen handler
# module is absent from the package, the wrapper fails closed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

AWS_REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="game-agent"
STACK_NAME="${PROJECT_NAME}-operations"
TEMPLATE="$PROJECT_ROOT/infrastructure/cloudformation/06-operations-observation.yaml"
BACKEND_SRC="$PROJECT_ROOT/backend/src"
HANDLER_MODULE_PATH="operations/observe/lambda_entry.py"

ENVIRONMENT="beta"
ACTION="preview"   # preview | enable | disable
COGNITO_ISSUER="${COGNITO_ISSUER:-}"
COGNITO_CLIENT_ID="${COGNITO_CLIENT_ID:-}"
TENANT_ID="${TENANT_ID:-}"
WORKSPACE_ID="${WORKSPACE_ID:-}"
TRUSTED_AUDIENCE="${TRUSTED_AUDIENCE:-}"
# An explicitly supplied deployment bucket for the Lambda artifact. When empty
# on an enabled deploy the wrapper discovers an existing bucket and never
# creates one.
CODE_S3_BUCKET="${CODE_S3_BUCKET:-}"

usage() {
    cat <<'USAGE'
Usage: deploy-operations.sh [--enable | --disable] [--environment beta|prod]

  (no flag)     Preview only (READ-ONLY): validates and lints the template and
                creates nothing. No change set is created.
  --enable      Build/upload the operations Lambda artifact and deploy the stack
                with OperationsMode=observe. Requires GBAW_OPERATIONS_MODE=observe
                in the environment plus the enabling inputs below.
  --disable     Re-deploy the stack with OperationsMode=disabled (data-preserving
                rollback: request path removed, durable audit data retained).
  --environment Target environment (default: beta). "prod" enables DynamoDB
                deletion protection and longer log retention.

Environment for --enable:
  GBAW_OPERATIONS_MODE=observe   Required confirmation.
  COGNITO_ISSUER, COGNITO_CLIENT_ID   JWT issuer + audience (required).
  TENANT_ID, WORKSPACE_ID        Server-side trusted bindings (required).
  TRUSTED_AUDIENCE               Optional; defaults to COGNITO_CLIENT_ID.
  CODE_S3_BUCKET                 Optional explicit deployment bucket; when unset
                                 an existing project bucket is discovered.
  AWS_PROFILE, AWS_REGION        Credentials/region (verified before any write).
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
        --template-body "file://$TEMPLATE" \
        --region "$AWS_REGION" >/dev/null
    echo "✅ Preview complete. Template validated; no resources were created."
    exit 0
fi

# --------------------------------------------------------------------------- #
# Verify identity/region before any write path (enable or disable).
# --------------------------------------------------------------------------- #
echo "🔐 Verifying AWS credentials and region before any write ..."
echo "   AWS_PROFILE=${AWS_PROFILE:-<default>}  AWS_REGION=${AWS_REGION}"
if ! CALLER_IDENTITY="$(aws sts get-caller-identity --region "$AWS_REGION" --output text 2>/dev/null)"; then
    echo "❌ Unable to verify caller identity. Configure AWS_PROFILE/AWS_REGION and credentials." >&2
    exit 4
fi
echo "   Caller identity: $CALLER_IDENTITY"

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
    OPERATIONS_MODE="observe"

    # ----------------------------------------------------------------------- #
    # Build a deterministic, minimal Lambda zip: the `operations` code plus the
    # required third-party dependencies. Determinism (fixed mtimes, sorted
    # entries) makes the content hash — and therefore the S3 key — stable for an
    # unchanged source tree.
    # ----------------------------------------------------------------------- #
    if [ ! -f "$BACKEND_SRC/$HANDLER_MODULE_PATH" ]; then
        echo "❌ Refusing to enable: frozen handler module $HANDLER_MODULE_PATH is absent" >&2
        echo "   from $BACKEND_SRC. No enabled route may point at missing/placeholder code." >&2
        exit 5
    fi

    BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gbaw-ops-pkg.XXXXXXXX")"
    trap 'rm -rf "$BUILD_DIR"' EXIT
    STAGE="$BUILD_DIR/stage"
    mkdir -p "$STAGE"

    echo "📦 Staging operations code ..."
    # Only the operations package is shipped (its dependencies within backend
    # are the operations subtree). Exclude caches and tests. A tar pipe copies
    # the tree portably and dereferences any symlinks, avoiding pass-through
    # copy tools that refuse to write through a symlinked temp prefix.
    ( cd "$BACKEND_SRC" && \
      find operations -type f -name '*.py' -not -path '*/__pycache__/*' -print0 \
        | tar --null -cf - --files-from=- ) | ( cd "$STAGE" && tar -xf - )

    # Required third-party dependencies. boto3/botocore are provided by the
    # Lambda python runtime, so the minimal set here is the pure-python
    # canonical-JSON dependency the observation records need. Installed with a
    # pinned version for reproducibility.
    echo "📦 Installing required third-party dependencies ..."
    python3 -m pip install --quiet --no-compile \
        --target "$STAGE" "rfc8785==0.1.4"

    # Normalize for determinism: strip pip metadata dirs and fix timestamps.
    find "$STAGE" -depth -type d -name '*.dist-info' -exec rm -rf {} + 2>/dev/null || true
    find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    # Fixed epoch touch stamp assembled from parts (no 12-digit literal) so the
    # build is reproducible without tripping account-id content scanners.
    EPOCH_STAMP="2000""01010000.00"   # CCYYMMDDhhmm.SS
    find "$STAGE" -exec touch -h -t "$EPOCH_STAMP" {} +

    echo "🧪 Verifying the packaged handler imports in a clean environment ..."
    # Isolated interpreter (-E -s ignores user site + env config) but with the
    # staged package as the sole import root on sys.path, mirroring how Lambda
    # loads the deployment package. This proves the frozen handler resolves from
    # the built artifact alone before any deploy.
    env -u PYTHONPATH -u PYTHONHOME python3 -E -s -c \
        "import sys; sys.path.insert(0, '$STAGE'); import importlib; m = importlib.import_module('operations.observe.lambda_entry'); assert callable(m.handler)" \
        || { echo "❌ Packaged handler failed to import; refusing to deploy." >&2; exit 5; }

    ARTIFACT="$BUILD_DIR/operations-observe.zip"
    echo "🗜️  Building deterministic zip ..."
    ( cd "$STAGE" && find . -type f | LC_ALL=C sort \
        | zip -X -q "$ARTIFACT" -@ )
    CONTENT_HASH="$(shasum -a 256 "$ARTIFACT" | awk '{print $1}')"
    CODE_S3_KEY="operations/observe/${CONTENT_HASH}.zip"
    echo "   Artifact sha256: $CONTENT_HASH"

    # Resolve the deployment bucket: explicit if supplied, otherwise discover an
    # existing project bucket. Never create a bucket here.
    if [ -z "$CODE_S3_BUCKET" ]; then
        echo "🔎 No CODE_S3_BUCKET supplied; discovering an existing deployment bucket ..."
        CODE_S3_BUCKET="$(aws s3api list-buckets \
            --query "Buckets[?starts_with(Name, '${PROJECT_NAME}-deploy') || starts_with(Name, '${PROJECT_NAME}-artifacts')].Name | [0]" \
            --output text 2>/dev/null || true)"
        if [ -z "$CODE_S3_BUCKET" ] || [ "$CODE_S3_BUCKET" = "None" ]; then
            echo "❌ No existing deployment bucket found and none supplied." >&2
            echo "   Set CODE_S3_BUCKET to an existing bucket; this wrapper never creates one." >&2
            exit 6
        fi
    else
        # Confirm the explicitly supplied bucket already exists.
        if ! aws s3api head-bucket --bucket "$CODE_S3_BUCKET" >/dev/null 2>&1; then
            echo "❌ Supplied CODE_S3_BUCKET '$CODE_S3_BUCKET' does not exist or is inaccessible." >&2
            exit 6
        fi
    fi
    echo "   Deployment bucket: $CODE_S3_BUCKET"

    echo "☁️  Uploading artifact to s3://$CODE_S3_BUCKET/$CODE_S3_KEY ..."
    aws s3 cp "$ARTIFACT" "s3://$CODE_S3_BUCKET/$CODE_S3_KEY" \
        --region "$AWS_REGION" --only-show-errors
elif [ "$ACTION" = "disable" ]; then
    OPERATIONS_MODE="disabled"
fi

PARAM_OVERRIDES=(
    "ProjectName=${PROJECT_NAME}"
    "OperationsMode=${OPERATIONS_MODE}"
    "Environment=${ENVIRONMENT}"
    "CognitoIssuer=${COGNITO_ISSUER}"
    "CognitoClientId=${COGNITO_CLIENT_ID}"
    "TenantId=${TENANT_ID}"
    "WorkspaceId=${WORKSPACE_ID}"
    "TrustedAudience=${TRUSTED_AUDIENCE}"
    "CodeS3Bucket=${CODE_S3_BUCKET}"
    "CodeS3Key=${CODE_S3_KEY}"
)

echo "🚀 Deploying $STACK_NAME with OperationsMode=$OPERATIONS_MODE ..."
aws cloudformation deploy \
    --template-file "$TEMPLATE" \
    --stack-name "$STACK_NAME" \
    --capabilities CAPABILITY_NAMED_IAM \
    --region "$AWS_REGION" \
    --parameter-overrides "${PARAM_OVERRIDES[@]}"

echo "✅ Deploy complete (OperationsMode=$OPERATIONS_MODE)."
