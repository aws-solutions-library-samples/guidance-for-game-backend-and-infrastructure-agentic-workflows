#!/bin/bash
set -e
set -o pipefail  # fail if any command in a pipe fails (not just the last)

# Game Agent - Complete Deployment Script
# Uses AgentCore direct code deployment (CodeBuild)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Default values
AWS_REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="game-agent"

# Read AWS_PROFILE from ui/.env.local if not already set
if [ -z "$AWS_PROFILE" ] && [ -f "$PROJECT_ROOT/ui/.env.local" ]; then
    _profile=$(grep '^AWS_PROFILE=' "$PROJECT_ROOT/ui/.env.local" | cut -d= -f2 | tr -d '[:space:]')
    [ -n "$_profile" ] && export AWS_PROFILE="$_profile"
fi

echo "=================================================="
echo "🚀 Game Agent - Complete Deployment"
echo "=================================================="
echo "Region: $AWS_REGION"
echo "Project: $PROJECT_NAME"
echo ""

# Prerequisite checks
echo "🔍 Checking prerequisites..."
PREREQ_WARNINGS=()

if ! command -v aws &> /dev/null; then
    echo "❌ AWS CLI not found. Install: https://aws.amazon.com/cli/"
    exit 1
fi

if ! command -v uv &> /dev/null; then
    echo "❌ UV not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

if ! command -v yq &> /dev/null; then
    echo "❌ yq not found. Install: https://github.com/mikefarah/yq#install"
    exit 1
fi

if ! command -v jq &> /dev/null; then
    echo "⚠️  jq not found, attempting to install..."
    if [[ "$OSTYPE" == "darwin"* ]] && command -v brew &> /dev/null; then
        brew install jq
    elif command -v apt-get &> /dev/null; then
        sudo apt-get install -y jq
    else
        echo "❌ Could not auto-install jq. Install manually: https://jqlang.github.io/jq/download/"
        exit 1
    fi
    echo "   ✅ jq installed"
fi

# python3 is required to parse AWS CLI JSON in the account-observability steps
# (Step 1.7 default read, Step 2b trace-delivery verification, and the Step 5c
# recheck). uv installs Python 3.13 for the backend but does not put it on PATH,
# so a standalone python3 is a hard prerequisite. Unlike jq it is not
# auto-installed: fail with a clear message rather than guess an interpreter.
if ! command -v python3 &> /dev/null; then
    echo "❌ python3 not found. It is required to parse AWS responses in the"
    echo "   account-observability steps (Step 1.7 default read, Step 2b"
    echo "   trace-delivery verification, and the Step 5c recheck). Install"
    echo "   Python 3 from https://www.python.org/downloads/ (or your OS package"
    echo "   manager) and re-run."
    exit 1
fi

# Docker is optional — backend deploys without it, but frontend (Steps 6-8) will be skipped
DOCKER_AVAILABLE=true
if ! command -v docker &> /dev/null; then
    DOCKER_AVAILABLE=false
    PREREQ_WARNINGS+=("Docker not installed")
elif ! docker info &> /dev/null; then
    DOCKER_AVAILABLE=false
    PREREQ_WARNINGS+=("Docker is installed but not running")
fi

if ! aws sts get-caller-identity &> /dev/null; then
    echo "❌ AWS credentials not configured. Run: aws configure"
    exit 1
fi

if [ "$DOCKER_AVAILABLE" = false ]; then
    echo "⚠️  ${PREREQ_WARNINGS[0]}. Frontend (Steps 6-8) will be skipped."
    echo "   Install/start Docker to deploy the UI: https://docs.docker.com/get-docker/"
    echo "   You can re-run this script after starting Docker to deploy the frontend."
    echo ""
fi

echo "✅ All required prerequisites met"
echo ""

# Preflight: install backend dependencies locally, then guard against an
# unbootable long-path runtime BEFORE any AWS-mutating command runs (#517).
#
# `agentcore launch` (Step 2) does not ship this venv's console scripts: the
# AgentCore starter toolkit rebuilds a dependency bundle with its own
# `uv pip install --target` inside the uv-selected project environment and zips
# that into dependencies.zip. uv embeds the environment interpreter's absolute
# local path into each console script's launcher line; the embedded basename is
# the longer of the names uv may embed (`<env>/bin/python3` or `<env>/bin/python`
# depending on the uv version), so this guard measures `<env>/bin/python3`. When
# that interpreter path is over the uv shebang byte limit or contains a space,
# uv emits a /bin/sh trampoline instead of a portable shebang, producing a
# runtime that dies at exec — silently, because deploy still exits 0 and the
# control plane reaches READY. Running the check here, right after local
# `uv sync` and before Step 0.5 (the first `aws cloudformation deploy`), means a
# doomed deploy fails fast with a clear remediation and leaves zero AWS side
# effects behind. The guard runs from inside the physical backend directory
# (the same working directory `agentcore launch` resolves the project from) so
# it measures the interpreter path that actually ships.
echo "📦 Preflight: Installing backend dependencies..."
if ! _uv_sync_out=$(cd "$PROJECT_ROOT/backend" && uv sync 2>&1); then
    echo "❌ Preflight: 'uv sync' failed:" >&2
    echo "$_uv_sync_out" >&2
    exit 1
fi
echo "🔎 Preflight: Verifying the runtime interpreter path is bootable..."
_runtime_path_status=0
_runtime_path_out="$(
  cd "$PROJECT_ROOT/backend"
  _backend_physical="$(pwd -P)"
  uv run python \
    "$SCRIPT_DIR/infrastructure/check_runtime_path.py" --backend-dir "$_backend_physical"
)" || _runtime_path_status=$?
# Exit code 3 is the guard's dedicated "skipped" signal (a native Windows host,
# out of scope; see #610): the path was not measured, so report the skip rather
# than a portability result and let the deploy continue. 0 means it measured and
# passed. Any other nonzero is a real failure; re-raise it so `set -e` aborts.
if [ "$_runtime_path_status" -eq 0 ]; then
  echo "✅ Runtime interpreter path is portable"
elif [ "$_runtime_path_status" -eq 3 ]; then
  printf '%s\n' "$_runtime_path_out"
else
  printf '%s\n' "$_runtime_path_out" >&2
  exit "$_runtime_path_status"
fi
echo ""

# Step 0: Download KB documentation
echo "📥 Step 0: Downloading KB documentation..."
bash "$SCRIPT_DIR/infrastructure/download-kb-docs.sh"

echo "✅ Documentation downloaded"
echo ""

# Step 0.5: Deploy Solution ID tracking stack (for WWSO deployment metrics)
echo "📊 Step 0.5: Deploying Solution ID tracking stack..."
aws cloudformation deploy \
  --template-file "$PROJECT_ROOT/infrastructure/cloudformation/00-solution-tracking.yaml" \
  --stack-name "${PROJECT_NAME}-solution-tracking" \
  --region $AWS_REGION \
  --no-fail-on-empty-changeset
echo "✅ Solution tracking deployed (SO9693)"
echo ""

# Step 1: Deploy base infrastructure
echo "📦 Step 1: Deploying base infrastructure..."
aws cloudformation deploy \
  --template-file "$PROJECT_ROOT/infrastructure/cloudformation/01-base-infrastructure.yaml" \
  --stack-name "${PROJECT_NAME}-infrastructure" \
  --parameter-overrides ProjectName="$PROJECT_NAME" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region $AWS_REGION

echo "✅ Base infrastructure deployed"
echo ""

# Resolve the Cognito bindings used by AgentCore JWT bearer authorization.
COGNITO_USER_POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name "${PROJECT_NAME}-infrastructure" \
  --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' \
  --output text)
COGNITO_CLIENT_ID=$(aws cloudformation describe-stacks \
  --stack-name "${PROJECT_NAME}-infrastructure" \
  --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolClientId`].OutputValue' \
  --output text)
if [ -z "${COGNITO_USER_POOL_ID:-}" ] || [ "$COGNITO_USER_POOL_ID" = "None" ] || \
   [ -z "${COGNITO_CLIENT_ID:-}" ] || [ "$COGNITO_CLIENT_ID" = "None" ]; then
  echo "❌ Base stack did not export Cognito JWT configuration" >&2
  exit 1
fi
COGNITO_ISSUER="https://cognito-idp.${AWS_REGION}.amazonaws.com/${COGNITO_USER_POOL_ID}"
AGENTCORE_AUTHORIZER_CONFIG=$(jq -cn \
  --arg discoveryUrl "${COGNITO_ISSUER}/.well-known/openid-configuration" \
  --arg clientId "$COGNITO_CLIENT_ID" \
  '{customJWTAuthorizer:{discoveryUrl:$discoveryUrl,allowedClients:[$clientId]}}')
echo "✅ Cognito JWT authorizer resolved"
echo ""

# Step 1.5: Deploy Bedrock Guardrails
echo "🛡️  Step 1.5: Deploying Bedrock Guardrails..."
aws cloudformation deploy \
  --template-file "$PROJECT_ROOT/infrastructure/cloudformation/04-bedrock-guardrails.yaml" \
  --stack-name "${PROJECT_NAME}-guardrails" \
  --parameter-overrides ProjectName="$PROJECT_NAME" \
  --region $AWS_REGION

# Get guardrail ID for AgentCore configuration
GUARDRAIL_ID=$(aws cloudformation describe-stacks \
  --stack-name "${PROJECT_NAME}-guardrails" \
  --region $AWS_REGION \
  --query 'Stacks[0].Outputs[?OutputKey==`GuardrailId`].OutputValue' \
  --output text)

echo "✅ Guardrails deployed: $GUARDRAIL_ID"
echo ""

# Step 1.6: Deploy Bedrock Managed Prompts
echo "📝 Step 1.6: Deploying Bedrock Managed Prompts..."
bash "$SCRIPT_DIR/infrastructure/deploy-prompts.sh"

# Read prompt ARNs for AgentCore env vars
GBAW_ORCHESTRATOR_PROMPT_ARN=$(grep "^GBAW_ORCHESTRATOR_PROMPT_ARN=" "$PROJECT_ROOT/backend/.env.local" 2>/dev/null | cut -d'=' -f2 || echo "")
GBAW_GAMELIFT_PROMPT_ARN=$(grep "^GBAW_GAMELIFT_PROMPT_ARN=" "$PROJECT_ROOT/backend/.env.local" 2>/dev/null | cut -d'=' -f2 || echo "")
GBAW_EKS_PROMPT_ARN=$(grep "^GBAW_EKS_PROMPT_ARN=" "$PROJECT_ROOT/backend/.env.local" 2>/dev/null | cut -d'=' -f2 || echo "")
GBAW_COST_PROMPT_ARN=$(grep "^GBAW_COST_PROMPT_ARN=" "$PROJECT_ROOT/backend/.env.local" 2>/dev/null | cut -d'=' -f2 || echo "")

echo "✅ Managed Prompts deployed"
echo ""

# Step 1.7: Account-wide observability setup
# Account-wide X-Ray / CloudWatch Logs changes (trace segment destination,
# indexing rule, shared Logs resource policy) are shared, cross-application
# settings. They run ONLY when GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true. By
# default the deployment is read-only here: it detects whether the account
# already supports Transaction Search and, if not, prints an actionable warning
# without failing. That warning recommends the opt-in only when the AWS-reserved
# 'aws/spans' log group already exists and the destination was read; otherwise
# it points to the one-time console enable or a destination toggle, because the
# opt-in cannot create that group. See docs/OBSERVABILITY_ADOT_EXPORTER.md.
echo "📡 Step 1.7: Account-wide observability..."
GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY="${GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY:-false}" \
  AWS_REGION="$AWS_REGION" \
  bash "$SCRIPT_DIR/infrastructure/setup-account-observability.sh"

echo "✅ Account-wide observability step complete"
echo ""

# Read-only preflight for the AgentCore starter toolkit's account-wide
# Transaction Search step, run before each `agentcore launch`. The script is run
# as a SUBPROCESS (not sourced) so its own `set -euo pipefail` cannot leak into
# this deploy's shell options; deploy.sh keeps its own options and variables.
# Returns non-zero when the fail-closed stop switch is set and the toolkit would
# (or might) change account-wide settings, or when python3 is not on PATH (the
# preflight parses AWS CLI JSON with python3). deploy.sh checks python3 as a
# prerequisite above, so a full deploy reaches this only for the stop-switch case.
run_toolkit_preflight() {
  AWS_REGION="$AWS_REGION" \
    GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE="${GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE:-false}" \
    GBAW_OBSERVABILITY_RESOURCE_POLICY_NAME="${GBAW_OBSERVABILITY_RESOURCE_POLICY_NAME:-}" \
    bash "$SCRIPT_DIR/infrastructure/setup-account-observability.sh" --toolkit-preflight
}

# When the operator has set the fail-closed stop switch, run the preflight ONCE
# up front so a predicted (or unknown) account-wide change halts the deploy
# before the Knowledge Base and observability steps — not partway through. The
# per-launch preflights below still run; they re-check immediately before each
# launch and can still halt if a later read fails or the account state changes.
if [ "${GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE:-false}" = "true" ]; then
  echo "🔎 Stop switch set: running the toolkit Transaction Search preflight before Step 2..."
  if ! run_toolkit_preflight; then
    exit 1
  fi
fi

# Step 2: Launch AgentCore Runtime (direct code deployment via CodeBuild)
echo "🤖 Step 2: Launching AgentCore Runtime..."
cd "$PROJECT_ROOT/backend"

# Ensure backend dependencies (including agentcore CLI) are installed.
# The preflight already synced and ran the runtime path guard (#517); this
# re-sync is idempotent and keeps Step 2 self-contained.
echo "📦 Installing backend dependencies..."
if ! _uv_sync_out=$(uv sync 2>&1); then
    echo "❌ 'uv sync' failed:" >&2
    echo "$_uv_sync_out" >&2
    exit 1
fi

# Resolve model roles through the same canonical Python configuration used by
# local runtime. Process variables override ui/.env.local; canonical role names
# override the legacy compatibility aliases.
if ! MODEL_EXPORTS=$(uv run python "$PROJECT_ROOT/config/load_deployment_settings.py" --models-only); then
  echo "❌ Unable to resolve canonical role models" >&2
  exit 1
fi
eval "$MODEL_EXPORTS"
echo "   Orchestrator model: $GBAW_ORCHESTRATOR_MODEL_ID"
echo "   Specialist model:   $GBAW_SPECIALIST_MODEL_ID"

if ! IDENTITY_EXPORTS=$(uv run python "$PROJECT_ROOT/config/load_deployment_settings.py" --identity-only); then
  echo "❌ Unable to resolve trusted tenant and workspace bindings" >&2
  exit 1
fi
eval "$IDENTITY_EXPORTS"
echo "   Tenant binding:      $GBAW_TENANT_ID"
echo "   Workspace binding:   $GBAW_WORKSPACE_ID"

# Resolve the shared cost report snapshot table (#365) from the base stack so the
# runtime can reuse report IDs across workers. The base stack (deployed above)
# always exports this output, so a missing value means a broken or stale stack —
# fail closed rather than silently degrading to a process-local in-memory cache
# that cannot satisfy cross-worker reuse.
COST_SNAPSHOT_TABLE_NAME=$(aws cloudformation describe-stacks \
  --stack-name "${PROJECT_NAME}-infrastructure" \
  --region $AWS_REGION \
  --query 'Stacks[0].Outputs[?OutputKey==`CostReportSnapshotTableName`].OutputValue' \
  --output text 2>/dev/null || echo "")
if [ -z "$COST_SNAPSHOT_TABLE_NAME" ] || [ "$COST_SNAPSHOT_TABLE_NAME" = "None" ]; then
  echo "❌ Base stack did not export CostReportSnapshotTableName." >&2
  echo "   The shared cost report snapshot store is required for a hosted deployment;" >&2
  echo "   redeploy the base infrastructure stack so the table and output exist." >&2
  exit 1
fi
# Hosted deployments require the shared store: never fall back to memory.
COST_SNAPSHOT_REQUIRED=true

# Validate a bounded, positive TTL (seconds) before passing it to the runtime.
COST_SNAPSHOT_TTL_SECONDS="${GBAW_COST_SNAPSHOT_TTL_SECONDS:-1800}"
if ! [[ "$COST_SNAPSHOT_TTL_SECONDS" =~ ^[0-9]+$ ]] \
    || [ "$COST_SNAPSHOT_TTL_SECONDS" -lt 60 ] \
    || [ "$COST_SNAPSHOT_TTL_SECONDS" -gt 86400 ]; then
  echo "❌ GBAW_COST_SNAPSHOT_TTL_SECONDS='$COST_SNAPSHOT_TTL_SECONDS' must be an integer in [60, 86400]." >&2
  exit 1
fi
echo "   Cost snapshot table: ${COST_SNAPSHOT_TABLE_NAME}"
echo "   Cost snapshot TTL:   ${COST_SNAPSHOT_TTL_SECONDS}s (required=${COST_SNAPSHOT_REQUIRED})"

is_resolved_deployment_value() {
  [ -n "${1:-}" ] && [ "$1" != "None" ]
}

append_agentcore_env_if_resolved() {
  local name="$1"
  local value="${2:-}"

  if is_resolved_deployment_value "$value"; then
    AGENTCORE_ENV_ARGS+=(-env "$name=$value")
  fi
  return 0
}

build_agentcore_env_args() {
  AGENTCORE_ENV_ARGS=(
    -env "GBAW_HOSTED_RUNTIME=true"
    -env "GBAW_ORCHESTRATOR_MODEL_ID=$GBAW_ORCHESTRATOR_MODEL_ID"
    -env "GBAW_SPECIALIST_MODEL_ID=$GBAW_SPECIALIST_MODEL_ID"
    # Keep model and tool content out of exported telemetry (spans, span events,
    # and the OpenTelemetry log records derived from GenAI events). The runtime
    # also applies these at startup before any tracer is built; passing them here
    # means the AWS OpenTelemetry distro sees them at process start too. An
    # explicit "false" wins over the distro's AGENT_OBSERVABILITY_ENABLED
    # setdefault; the empty unredacted-attributes list turns on Strands redaction
    # of prompt, response, and system-prompt attributes.
    -env "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false"
    -env "OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_unredacted_attributes="
  )

  if is_resolved_deployment_value "${GUARDRAIL_ID:-}"; then
    AGENTCORE_ENV_ARGS+=(
      -env "GBAW_BEDROCK_GUARDRAIL_ID=$GUARDRAIL_ID"
      -env "GBAW_BEDROCK_GUARDRAIL_VERSION=DRAFT"
    )
  fi
  append_agentcore_env_if_resolved "GBAW_ORCHESTRATOR_PROMPT_ARN" "${GBAW_ORCHESTRATOR_PROMPT_ARN:-}"
  append_agentcore_env_if_resolved "GBAW_GAMELIFT_PROMPT_ARN" "${GBAW_GAMELIFT_PROMPT_ARN:-}"
  append_agentcore_env_if_resolved "GBAW_EKS_PROMPT_ARN" "${GBAW_EKS_PROMPT_ARN:-}"
  append_agentcore_env_if_resolved "GBAW_COST_PROMPT_ARN" "${GBAW_COST_PROMPT_ARN:-}"
  append_agentcore_env_if_resolved "GBAW_GAMELIFT_KB_ID" "${GAMELIFT_KB_ID:-}"
  append_agentcore_env_if_resolved "GBAW_EKS_KB_ID" "${EKS_KB_ID:-}"
  append_agentcore_env_if_resolved "GBAW_COST_KB_ID" "${COST_KB_ID:-}"
  # Trusted deployment identity + shared cost report snapshot store (#365)
  append_agentcore_env_if_resolved "GBAW_TENANT_ID" "${GBAW_TENANT_ID:-}"
  append_agentcore_env_if_resolved "GBAW_WORKSPACE_ID" "${GBAW_WORKSPACE_ID:-}"
  append_agentcore_env_if_resolved "GBAW_COGNITO_ISSUER" "${COGNITO_ISSUER:-}"
  append_agentcore_env_if_resolved "GBAW_COGNITO_CLIENT_ID" "${COGNITO_CLIENT_ID:-}"
  append_agentcore_env_if_resolved "GBAW_COST_SNAPSHOT_TABLE_NAME" "${COST_SNAPSHOT_TABLE_NAME:-}"
  append_agentcore_env_if_resolved "GBAW_COST_SNAPSHOT_REQUIRED" "${COST_SNAPSHOT_REQUIRED:-}"
  append_agentcore_env_if_resolved "GBAW_COST_SNAPSHOT_TTL_SECONDS" "${COST_SNAPSHOT_TTL_SECONDS:-}"
  return 0
}

is_transient_waf_error() {
  local error_text="$1"
  [[ "$error_text" == *"WAFNonexistentItemException"* \
    || "$error_text" == *"WAFUnavailableEntityException"* \
    || "$error_text" == *"WAFInternalErrorException"* ]]
}

get_active_web_acl_arn() {
  local resource_arn="$1"
  local query_result

  if query_result=$(aws wafv2 get-web-acl-for-resource \
    --resource-arn "$resource_arn" \
    --region "$AWS_REGION" \
    --query 'WebACL.ARN' \
    --output text 2>&1); then
    if [ "$query_result" != "None" ]; then
      printf '%s\n' "$query_result"
    fi
    return 0
  fi

  if is_transient_waf_error "$query_result"; then
    return 75
  fi
  echo "❌ Unable to inspect the active WAF association" >&2
  return 1
}

web_acl_has_required_rules() {
  local web_acl_arn="$1"
  local web_acl_id="${web_acl_arn##*/}"
  local web_acl_path="${web_acl_arn%/*}"
  local web_acl_name="${web_acl_path##*/}"
  local query_result
  local normalized_rule_names
  local required_rule
  local required_rules=(
    RateLimitAuthPaths
    RateLimitAdminPaths
    RateLimitPerIP
    AWSManagedRulesCommonRuleSet
    AWSManagedRulesSQLiRuleSet
    AWSManagedRulesKnownBadInputsRuleSet
  )

  if ! query_result=$(aws wafv2 get-web-acl \
    --scope REGIONAL \
    --id "$web_acl_id" \
    --name "$web_acl_name" \
    --region "$AWS_REGION" \
    --query 'WebACL.Rules[].Name' \
    --output text 2>&1); then
    if is_transient_waf_error "$query_result"; then
      return 75
    fi
    echo "❌ Unable to inspect the project WebACL rules" >&2
    return 1
  fi

  normalized_rule_names=" ${query_result//$'\t'/ } "
  for required_rule in "${required_rules[@]}"; do
    if [[ "$normalized_rule_names" != *" $required_rule "* ]]; then
      return 2
    fi
  done
  return 0
}

reconcile_waf_association() {
  local expected_web_acl_arn="$1"
  local resource_arn="$2"
  local association_max_attempts="${GBAW_WAF_ASSOCIATION_MAX_ATTEMPTS:-36}"
  local verification_max_attempts="${GBAW_WAF_VERIFICATION_MAX_ATTEMPTS:-36}"
  local retry_seconds="${GBAW_WAF_RETRY_SECONDS:-5}"
  local active_web_acl_arn=""
  local command_output
  local lookup_status
  local rule_status
  local attempt
  local associated=false

  if ! is_resolved_deployment_value "$expected_web_acl_arn" \
    || ! is_resolved_deployment_value "$resource_arn"; then
    echo "❌ Cannot reconcile WAF without resolved WebACL and frontend ALB ARNs" >&2
    return 1
  fi
  if ! [[ "$association_max_attempts" =~ ^[1-9][0-9]*$ ]] \
    || ! [[ "$verification_max_attempts" =~ ^[1-9][0-9]*$ ]] \
    || ! [[ "$retry_seconds" =~ ^[0-9]+$ ]]; then
    echo "❌ WAF retry settings must use positive attempts and non-negative seconds" >&2
    return 1
  fi

  if active_web_acl_arn=$(get_active_web_acl_arn "$resource_arn"); then
    lookup_status=0
  else
    lookup_status=$?
    if [ "$lookup_status" -ne 75 ]; then
      return 1
    fi
  fi

  if [ "$lookup_status" -eq 0 ] && [ "$active_web_acl_arn" = "$expected_web_acl_arn" ]; then
    if web_acl_has_required_rules "$expected_web_acl_arn"; then
      echo "✅ Expected WAF and required rules are already active on the frontend ALB"
      return 0
    else
      rule_status=$?
      if [ "$rule_status" -eq 1 ]; then
        return 1
      fi
    fi
  else
    if [ -n "$active_web_acl_arn" ]; then
      echo "🔄 Replacing a different active WAF association with the project WebACL"
    else
      echo "🔄 Associating the project WebACL with the frontend ALB"
    fi

    for ((attempt = 1; attempt <= association_max_attempts; attempt++)); do
      if command_output=$(aws wafv2 associate-web-acl \
        --web-acl-arn "$expected_web_acl_arn" \
        --resource-arn "$resource_arn" \
        --region "$AWS_REGION" 2>&1); then
        associated=true
        break
      fi
      if ! is_transient_waf_error "$command_output"; then
        echo "❌ Unable to associate the project WebACL" >&2
        return 1
      fi
      if [ "$attempt" -lt "$association_max_attempts" ]; then
        sleep "$retry_seconds"
      fi
    done
    if [ "$associated" != true ]; then
      echo "❌ Project WAF association did not succeed within the retry budget" >&2
      return 1
    fi
  fi

  for ((attempt = 1; attempt <= verification_max_attempts; attempt++)); do
    active_web_acl_arn=""
    if active_web_acl_arn=$(get_active_web_acl_arn "$resource_arn"); then
      lookup_status=0
    else
      lookup_status=$?
      if [ "$lookup_status" -ne 75 ]; then
        return 1
      fi
    fi

    if [ "$lookup_status" -eq 0 ] && [ "$active_web_acl_arn" = "$expected_web_acl_arn" ]; then
      if web_acl_has_required_rules "$expected_web_acl_arn"; then
        echo "✅ Project WAF association and required rules converged after ${attempt} check(s)"
        return 0
      else
        rule_status=$?
        if [ "$rule_status" -eq 1 ]; then
          return 1
        fi
      fi
    fi
    if [ "$attempt" -lt "$verification_max_attempts" ]; then
      sleep "$retry_seconds"
    fi
  done

  echo "❌ Project WAF association and required rules did not converge" >&2
  return 1
}

# --- Runtime trace-delivery classification and verification (#471) ---
#
# AWS CloudWatch Logs delivery setup for the AgentCore runtime must distinguish a
# genuine "already exists" conflict (idempotent success) from authorization,
# validation, throttling, and service errors. A failure that is misread as an
# idempotent conflict can report a broken delivery as success.

# Classify a CloudWatch Logs delivery CLI error from its combined output.
# Prints one of: conflict | retryable | fatal. The output text is consumed
# locally and never re-emitted, so account IDs and ARNs are not printed.
#
# Classification keys off the modeled AWS error CODE extracted from the standard
# "An error occurred (<Code>) when calling ..." form, so free text such as an
# account ID that happens to contain "500" or a validation message that mentions
# "already exists" cannot be misread. CLI transport failures (which carry no
# error code) are matched on their fixed phrasing and treated as retryable.
classify_delivery_error() {
  local error_text="$1"
  local code=""
  if [[ "$error_text" =~ \(([A-Za-z0-9]+)\) ]]; then
    code="${BASH_REMATCH[1]}"
  fi

  case "$code" in
    ConflictException | ResourceAlreadyExistsException)
      printf 'conflict\n'
      return 0
      ;;
    ThrottlingException | ServiceUnavailableException | InternalFailure \
      | 500 | 502 | 503 | 504)
      printf 'retryable\n'
      return 0
      ;;
  esac

  # Transport-level CLI failures have no modeled error code.
  if [[ "$error_text" == *"Could not connect to the endpoint URL"* \
    || "$error_text" == *"Read timeout"* ]]; then
    printf 'retryable\n'
    return 0
  fi

  printf 'fatal\n'
  return 0
}

# Run a single CloudWatch Logs delivery mutation with bounded retry on
# retryable errors. A fatal error (authorization, validation, service) fails
# with a bounded, public-safe diagnostic naming only the operation. A conflict
# is NOT auto-resolved here: it returns 2 so the caller can decide whether the
# operation is a safe idempotent rerun (create-delivery) or needs a follow-up
# check (the put upserts).
#   $1 label (public-safe operation name, e.g. "delivery source")
#   $2.. the aws CLI arguments
# Honors GBAW_DELIVERY_MAX_ATTEMPTS (default 4, capped at 20) and
# GBAW_DELIVERY_RETRY_SECONDS (default 5, capped at 60). On success the captured
# combined output (stdout and stderr) is available in DELIVERY_LAST_STDOUT; the
# caller parses it as JSON only with a safe fallback, so stderr noise cannot
# produce a bad ARN.
# Returns: 0 success | 1 fatal/exhausted | 2 conflict.
run_delivery_mutation() {
  local label="$1"
  shift
  local max_attempts="${GBAW_DELIVERY_MAX_ATTEMPTS:-4}"
  local retry_seconds="${GBAW_DELIVERY_RETRY_SECONDS:-5}"
  local attempt
  local command_output
  local classification

  DELIVERY_LAST_STDOUT=""
  if ! [[ "$max_attempts" =~ ^[1-9][0-9]*$ ]] || ! [[ "$retry_seconds" =~ ^[0-9]+$ ]]; then
    echo "❌ Delivery retry settings must use positive attempts and non-negative seconds" >&2
    return 1
  fi
  # Cap at the same bounds the PowerShell path enforces (attempts 1-20, delay 0-60).
  [ "$max_attempts" -gt 20 ] && max_attempts=20
  [ "$retry_seconds" -gt 60 ] && retry_seconds=60

  for ((attempt = 1; attempt <= max_attempts; attempt++)); do
    if command_output=$(aws "$@" 2>&1); then
      DELIVERY_LAST_STDOUT="$command_output"
      return 0
    fi
    classification=$(classify_delivery_error "$command_output")
    case "$classification" in
      conflict)
        return 2
        ;;
      retryable)
        if [ "$attempt" -lt "$max_attempts" ]; then
          echo "  ⏳ ${label}: retryable AWS error, retrying (${attempt}/${max_attempts})..."
          sleep "$retry_seconds"
          continue
        fi
        echo "❌ ${label} did not succeed after ${max_attempts} attempts (retryable AWS error)" >&2
        return 1
        ;;
      *)
        echo "❌ ${label} failed with a non-retryable AWS error" >&2
        return 1
        ;;
    esac
  done
  echo "❌ ${label} did not succeed within the retry budget" >&2
  return 1
}

# Verify that a delivery binding matches the intended runtime. Reads only
# stdout. Checks, from `describe-deliveries` JSON ($1): a delivery whose
# deliverySourceName is $2, deliveryDestinationArn is $3, and
# deliveryDestinationType is XRAY; and, from `get-delivery-source` JSON ($5),
# that the source's resourceArns contains the runtime ARN $4.
delivery_is_active() {
  local deliveries_json="$1"
  local expected_source="$2"
  local expected_dest_arn="$3"
  local runtime_arn="$4"
  local source_json="$5"

  if ! is_resolved_deployment_value "$expected_source" \
    || ! is_resolved_deployment_value "$expected_dest_arn" \
    || ! is_resolved_deployment_value "$runtime_arn"; then
    return 1
  fi

  SOURCE="$expected_source" DEST="$expected_dest_arn" RUNTIME="$runtime_arn" \
    SOURCE_JSON="$source_json" python3 -c '
import json
import os
import sys


def _load(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


deliveries_payload = _load(sys.stdin.read())
if isinstance(deliveries_payload, dict):
    deliveries = deliveries_payload.get("deliveries", [])
else:
    deliveries = deliveries_payload
if not isinstance(deliveries, list):
    sys.exit(1)

expected_source = os.environ["SOURCE"]
expected_dest = os.environ["DEST"]
runtime_arn = os.environ["RUNTIME"]

matched = False
for delivery in deliveries:
    if not isinstance(delivery, dict):
        continue
    if delivery.get("deliverySourceName") != expected_source:
        continue
    if delivery.get("deliveryDestinationArn") != expected_dest:
        continue
    if delivery.get("deliveryDestinationType") != "XRAY":
        continue
    matched = True
    break
if not matched:
    sys.exit(1)

# The delivery source must actually be bound to the runtime ARN.
source_payload = _load(os.environ.get("SOURCE_JSON", ""))
source = source_payload.get("deliverySource") if isinstance(source_payload, dict) else None
if not isinstance(source, dict):
    sys.exit(1)
resource_arns = source.get("resourceArns", [])
if not isinstance(resource_arns, list) or runtime_arn not in resource_arns:
    sys.exit(1)
sys.exit(0)
' <<< "$deliveries_json"
}

# Read account state to confirm an already-existing delivery resource matches
# the intended runtime after a conflict. Returns 0 only when the resource is
# ours. Output is consumed locally.
#   $1 "source"|"destination", $2 resource name, $3 runtime arn
delivery_resource_matches_runtime() {
  local kind="$1"
  local name="$2"
  local runtime_arn="$3"
  local json

  if [ "$kind" = "source" ]; then
    if ! json=$(aws logs get-delivery-source --name "$name" --region "$AWS_REGION" --output json 2>/dev/null); then
      return 1
    fi
    RESOURCE_JSON="$json" RUNTIME="$runtime_arn" python3 -c '
import json, os, sys
try:
    source = json.loads(os.environ["RESOURCE_JSON"]).get("deliverySource", {})
except (ValueError, KeyError):
    sys.exit(1)
if source.get("logType") not in (None, "TRACES"):
    sys.exit(1)
arns = source.get("resourceArns", [])
sys.exit(0 if isinstance(arns, list) and os.environ["RUNTIME"] in arns else 1)
'
    return $?
  fi

  if ! json=$(aws logs get-delivery-destination --name "$name" --region "$AWS_REGION" --output json 2>/dev/null); then
    return 1
  fi
  RESOURCE_JSON="$json" python3 -c '
import json, os, sys
try:
    dest = json.loads(os.environ["RESOURCE_JSON"]).get("deliveryDestination", {})
except (ValueError, KeyError):
    sys.exit(1)
sys.exit(0 if dest.get("deliveryDestinationType") == "XRAY" else 1)
'
}

# Resolve a project-owned delivery destination ARN from the AWS API rather than
# fabricating it. Prints the ARN on success; returns non-zero if it cannot be
# resolved.
resolve_delivery_destination_arn() {
  local name="$1"
  local arn
  arn=$(aws logs get-delivery-destination \
    --name "$name" \
    --region "$AWS_REGION" \
    --query 'deliveryDestination.arn' \
    --output text 2>/dev/null || echo "")
  if [ -z "$arn" ] || [ "$arn" = "None" ]; then
    return 1
  fi
  printf '%s\n' "$arn"
}

# Full Step 2b workflow: create source, destination, and delivery with
# classification + bounded retry, then verify the delivery is active. Fails
# (returns non-zero) on any real error or on verification failure.
#   $1 runtime id, $2 runtime arn
ensure_runtime_trace_delivery() {
  local runtime_id="$1"
  local runtime_arn="$2"
  local delivery_source_name="${runtime_id}-traces-source"
  local delivery_dest_name="${runtime_id}-traces-destination"
  local delivery_dest_arn=""
  local deliveries_json=""
  local source_json=""
  local rc

  # Delivery source (upsert). A conflict here means a differing source already
  # covers this runtime, which is only safe if it is actually bound to us.
  run_delivery_mutation "Delivery source" \
    logs put-delivery-source \
    --name "$delivery_source_name" \
    --log-type "TRACES" \
    --resource-arn "$runtime_arn" \
    --region "$AWS_REGION"
  rc=$?
  if [ "$rc" -eq 2 ]; then
    if ! delivery_resource_matches_runtime "source" "$delivery_source_name" "$runtime_arn"; then
      echo "❌ A conflicting delivery source exists for this runtime" >&2
      return 1
    fi
    echo "  ✅ Delivery source already present for this runtime"
  elif [ "$rc" -ne 0 ]; then
    return 1
  else
    echo "  ✅ Delivery source ready"
  fi

  # Delivery destination (upsert). Same conflict rule applies.
  run_delivery_mutation "Delivery destination" \
    logs put-delivery-destination \
    --name "$delivery_dest_name" \
    --delivery-destination-type "XRAY" \
    --region "$AWS_REGION"
  rc=$?
  if [ "$rc" -eq 2 ]; then
    if ! delivery_resource_matches_runtime "destination" "$delivery_dest_name" "$runtime_arn"; then
      echo "❌ A conflicting delivery destination exists for this runtime" >&2
      return 1
    fi
    echo "  ✅ Delivery destination already present"
  elif [ "$rc" -ne 0 ]; then
    return 1
  else
    echo "  ✅ Delivery destination ready"
  fi

  # Resolve the destination ARN from the put response when it parses as JSON;
  # the capture may include stderr, so a failed parse falls back to the API read
  # below rather than yielding a bad ARN. Never fabricate it.
  if [ -n "$DELIVERY_LAST_STDOUT" ]; then
    delivery_dest_arn=$(printf '%s' "$DELIVERY_LAST_STDOUT" \
      | python3 -c "import json,sys; print(json.load(sys.stdin)['deliveryDestination']['arn'])" 2>/dev/null || echo "")
  fi
  if [ -z "$delivery_dest_arn" ]; then
    if ! delivery_dest_arn=$(resolve_delivery_destination_arn "$delivery_dest_name"); then
      echo "❌ Unable to resolve the delivery destination ARN" >&2
      return 1
    fi
  fi

  # Delivery binding (create). A conflict here is a safe idempotent rerun of the
  # same source/destination pair.
  run_delivery_mutation "Delivery" \
    logs create-delivery \
    --delivery-source-name "$delivery_source_name" \
    --delivery-destination-arn "$delivery_dest_arn" \
    --region "$AWS_REGION"
  rc=$?
  if [ "$rc" -eq 2 ]; then
    echo "  ✅ Delivery already exists"
  elif [ "$rc" -ne 0 ]; then
    return 1
  else
    echo "  ✅ Delivery ready"
  fi

  # Verification (bounded retry; stdout only): the deployment must not succeed
  # unless the intended source and destination are actually bound to the runtime.
  local verify_attempts="${GBAW_DELIVERY_MAX_ATTEMPTS:-4}"
  local verify_delay="${GBAW_DELIVERY_RETRY_SECONDS:-5}"
  [[ "$verify_attempts" =~ ^[1-9][0-9]*$ ]] || verify_attempts=4
  [[ "$verify_delay" =~ ^[0-9]+$ ]] || verify_delay=5
  [ "$verify_attempts" -gt 20 ] && verify_attempts=20
  [ "$verify_delay" -gt 60 ] && verify_delay=60

  local attempt verified=false
  for ((attempt = 1; attempt <= verify_attempts; attempt++)); do
    if deliveries_json=$(aws logs describe-deliveries \
      --region "$AWS_REGION" --output json 2>/dev/null) \
      && source_json=$(aws logs get-delivery-source \
        --name "$delivery_source_name" --region "$AWS_REGION" --output json 2>/dev/null) \
      && delivery_is_active "$deliveries_json" "$delivery_source_name" \
        "$delivery_dest_arn" "$runtime_arn" "$source_json"; then
      verified=true
      break
    fi
    if [ "$attempt" -lt "$verify_attempts" ]; then
      sleep "$verify_delay"
    fi
  done
  if [ "$verified" != true ]; then
    echo "❌ Runtime trace delivery is not active for the intended source and destination" >&2
    return 1
  fi
  echo "  ✅ Delivery verified active"
  return 0
}

# Read the account X-Ray trace segment destination in one place. Prints two
# space-separated tokens, "<destination> <status>", using the same enum the API
# models (destination XRay | CloudWatchLogs; status PENDING | ACTIVE). A failed
# or unparseable read prints "UNREADABLE UNREADABLE" so the caller can treat a
# read failure the same as a not-yet-active destination. Read-only.
read_trace_segment_destination() {
  local output
  if ! output=$(aws xray get-trace-segment-destination \
    --region "$AWS_REGION" --output json 2>/dev/null); then
    printf 'UNREADABLE UNREADABLE\n'
    return 0
  fi
  TRACE_DEST_JSON="$output" python3 -c '
import json
import os
import sys

try:
    payload = json.loads(os.environ["TRACE_DEST_JSON"])
except (ValueError, KeyError):
    print("UNREADABLE UNREADABLE")
    sys.exit(0)
if not isinstance(payload, dict):
    print("UNREADABLE UNREADABLE")
    sys.exit(0)
destination = payload.get("Destination") or "UNREADABLE"
status = payload.get("Status") or "UNREADABLE"
print(f"{destination} {status}")
' 2>/dev/null || printf 'UNREADABLE UNREADABLE\n'
}

# Bounded poll until the trace segment destination reports ACTIVE. Returns 0 as
# soon as it is ACTIVE, or non-zero when the budget is exhausted. The budget is
# overridable for tests and operators via GBAW_TRACE_DESTINATION_ACTIVE_* (the
# defaults mirror the account-observability wait: 30 attempts, 10s apart).
wait_for_trace_destination_active() {
  local attempts="${GBAW_TRACE_DESTINATION_ACTIVE_MAX_ATTEMPTS:-30}"
  local delay="${GBAW_TRACE_DESTINATION_ACTIVE_RETRY_SECONDS:-10}"
  [[ "$attempts" =~ ^[1-9][0-9]*$ ]] || attempts=30
  [[ "$delay" =~ ^[0-9]+$ ]] || delay=10
  local attempt status
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    status="$(aws xray get-trace-segment-destination \
      --region "$AWS_REGION" --query 'Status' --output text 2>/dev/null || echo "")"
    if [ "$status" = "ACTIVE" ]; then
      return 0
    fi
    if [ "$attempt" -lt "$attempts" ]; then
      sleep "$delay"
    fi
  done
  return 1
}

# Step 2b gate. Transaction Search must be active for the per-runtime trace
# delivery to bind: the delivery destination type is XRAY and the account trace
# segment destination has to be CloudWatchLogs and ACTIVE. On a fresh account the
# destination can stay PENDING for several minutes after agentcore launch
# switches it, so this gate decides whether to create the delivery now, wait for
# it, or defer it without failing the deploy:
#   - CloudWatchLogs + ACTIVE: create and verify the delivery; every failure is
#     fatal (issue #471 requires a failed delivery to fail the deploy).
#   - CloudWatchLogs + not yet ACTIVE: wait for a bounded, configurable budget;
#     if it becomes ACTIVE, create and verify as above.
#   - Not CloudWatchLogs, still not ACTIVE after the wait, or an unreadable read:
#     print one WARNING naming the state and skip the delivery. The deploy
#     continues and the read-only Step 5c recheck still reports.
#   $1 runtime id, $2 runtime arn
ensure_runtime_trace_delivery_gated() {
  local runtime_id="$1"
  local runtime_arn="$2"
  local destination status

  read -r destination status <<< "$(read_trace_segment_destination)"

  if [ "$destination" = "CloudWatchLogs" ] && [ "$status" = "ACTIVE" ]; then
    ensure_runtime_trace_delivery "$runtime_id" "$runtime_arn"
    return $?
  fi

  if [ "$destination" = "CloudWatchLogs" ]; then
    echo "  ⏳ Trace segment destination is CloudWatchLogs but not yet ACTIVE (status: ${status}); waiting..."
    if wait_for_trace_destination_active; then
      ensure_runtime_trace_delivery "$runtime_id" "$runtime_arn"
      return $?
    fi
  fi

  echo "⚠️  WARNING: Skipping runtime trace delivery setup."
  echo "   X-Ray trace segment destination is '${destination}' (status: ${status}), not an"
  echo "   active 'CloudWatchLogs' destination. Traces will not reach CloudWatch Logs until"
  echo "   X-Ray Transaction Search is active for this account. Check it with:"
  echo "     aws xray get-trace-segment-destination --region ${AWS_REGION}"
  echo "   and re-run the deployment after it reports CloudWatchLogs and ACTIVE."
  return 0
}

build_agentcore_env_args

# Get execution role from CloudFormation
EXECUTION_ROLE_ARN=$(aws cloudformation describe-stacks \
  --stack-name "${PROJECT_NAME}-infrastructure" \
  --region $AWS_REGION \
  --query 'Stacks[0].Outputs[?OutputKey==`AgentCoreExecutionRoleArn`].OutputValue' \
  --output text)

echo "Using execution role: $EXECUTION_ROLE_ARN"

# Export requirements.txt from uv.lock for AgentCore compatibility
echo "📦 Exporting requirements.txt from uv.lock..."
if command -v uv &> /dev/null; then
  uv export --format requirements-txt --no-dev --no-hashes --output-file requirements.txt.tmp > /dev/null 2>&1

  # Compare only the dependency lines (skip headers)
  if ! diff <(grep -v '^#' requirements.txt | grep -v '^$') <(grep -v '^#' requirements.txt.tmp | grep -v '^$') > /dev/null 2>&1; then
    echo "⚠️  requirements.txt dependencies out of sync, updating..."
    # `uv export` already writes a complete, correct file (its own 2-line header
    # + the full dependency list), so use it as-is. The previous `head -n 21`
    # approach assumed a 21-line header and duplicated the first ~19 packages.
    mv requirements.txt.tmp requirements.txt
  else
    rm requirements.txt.tmp
  fi
  echo "✅ requirements.txt verified"
else
  echo "⚠️  UV not found, using existing requirements.txt"
fi

echo "📝 Configuring AgentCore Cognito JWT authorization..."
uv run agentcore configure \
  --entrypoint agentcore_main.py \
  --name gameagentruntime \
  --region "$AWS_REGION" \
  --execution-role "$EXECUTION_ROLE_ARN" \
  --requirements-file requirements.txt \
  --authorizer-config "$AGENTCORE_AUTHORIZER_CONFIG" \
  --request-header-allowlist Authorization \
  --non-interactive
echo "✅ AgentCore JWT configuration ready"

# Note: no Dockerfile patching needed for MCP servers. The previous ccapi-mcp-server
# required a writable .schemas dir (read-only in the container); it was replaced by
# aws-api-mcp-server, whose log/working-dir are redirected to /tmp via environment
# variables in utils/mcp_client_factory.create_mcp_client (no filesystem patch needed).

# Check if runtime already exists
EXISTING_RUNTIME=$(yq eval '.agents.gameagentruntime.bedrock_agentcore.agent_arn' .bedrock_agentcore.yaml 2>/dev/null || echo "")

if [ -n "$EXISTING_RUNTIME" ] && [ "$EXISTING_RUNTIME" != "null" ]; then
  echo "⚠️  Runtime already exists: $EXISTING_RUNTIME"
  echo "   Skipping launch (use teardown to remove existing runtime)"
  RUNTIME_ARN="$EXISTING_RUNTIME"
else
  echo "🚀 Launching new AgentCore Runtime (CodeBuild)..."
  # Preflight: predict (read-only) which account-wide X-Ray / CloudWatch Logs
  # settings the AgentCore starter toolkit would change during this launch, and
  # honor the fail-closed stop switch before any launch is attempted.
  if ! run_toolkit_preflight; then
    exit 1
  fi
  uv run agentcore launch --auto-update-on-conflict "${AGENTCORE_ENV_ARGS[@]}"

  # Wait for runtime to be ready
  echo "⏳ Waiting for runtime to be ready..."
  sleep 10

  RUNTIME_ARN=$(yq eval '.agents.gameagentruntime.bedrock_agentcore.agent_arn' .bedrock_agentcore.yaml)
fi

RUNTIME_ID=$(echo $RUNTIME_ARN | awk -F'/' '{print $NF}')
echo "✅ AgentCore Runtime ready: $RUNTIME_ID"
echo ""

# Step 2b: Ensure CloudWatch delivery for runtime traces (idempotent)
# The AgentCore CLI's direct-code-deploy path does not set up the CloudWatch
# delivery (traces source → X-Ray destination) for the runtime. Without this,
# the Bedrock AgentCore Observability console page shows errors.
# Also skipped when --auto-update-on-conflict updates an existing runtime.
# See: https://github.com/aws/bedrock-agentcore-starter-toolkit/issues/457
echo "📡 Step 2b: Ensuring CloudWatch delivery for runtime traces..."
if ! ensure_runtime_trace_delivery_gated "$RUNTIME_ID" "$RUNTIME_ARN"; then
  echo "❌ Runtime traces delivery could not be configured and verified" >&2
  exit 1
fi

echo "✅ Runtime traces delivery configured"
echo ""

# Step 3: Deploy observability stack
echo "📊 Step 3: Deploying observability stack..."
aws cloudformation deploy \
  --template-file "$PROJECT_ROOT/infrastructure/cloudformation/03-agentcore-observability.yaml" \
  --stack-name "${PROJECT_NAME}-observability" \
  --parameter-overrides \
    ProjectName="$PROJECT_NAME" \
    RuntimeId="$RUNTIME_ID" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region $AWS_REGION

echo "✅ Observability stack deployed"
echo ""

# Step 4: Deploy Knowledge Bases
echo "🧠 Step 4: Deploying Knowledge Bases..."

bash "$SCRIPT_DIR/infrastructure/deploy-kb.sh"

echo "✅ Knowledge Bases deployed"
echo ""

# Step 5: Seed Knowledge Bases
echo "📚 Step 5: Seeding Knowledge Bases..."
bash "$SCRIPT_DIR/infrastructure/seed-kb-gamelift.sh"
bash "$SCRIPT_DIR/infrastructure/seed-kb-eks.sh"
bash "$SCRIPT_DIR/infrastructure/seed-kb-cost.sh"

echo "✅ Knowledge Bases seeded"
echo ""

# Step 5b: Wire KB IDs to AgentCore Runtime
echo "🔗 Step 5b: Wiring Knowledge Bases to AgentCore Runtime..."
cd "$PROJECT_ROOT/backend"

# Read KB IDs from .env.local (written by deploy-kb.sh)
GAMELIFT_KB_ID=$(grep "^GBAW_GAMELIFT_KB_ID=" .env.local 2>/dev/null | cut -d'=' -f2 || echo "")
EKS_KB_ID=$(grep "^GBAW_EKS_KB_ID=" .env.local 2>/dev/null | cut -d'=' -f2 || echo "")
COST_KB_ID=$(grep "^GBAW_COST_KB_ID=" .env.local 2>/dev/null | cut -d'=' -f2 || echo "")

if [ -n "$GAMELIFT_KB_ID" ]; then echo "   GameLift KB: $GAMELIFT_KB_ID"; fi
if [ -n "$EKS_KB_ID" ]; then echo "   EKS KB:      $EKS_KB_ID"; fi
if [ -n "$COST_KB_ID" ]; then echo "   Cost KB:     $COST_KB_ID"; fi

# Always update the runtime with the complete set of currently resolved values.
# This keeps role models synchronized even when one or more optional KBs are not
# available and avoids dropping prompt or Guardrail settings on replacement.
build_agentcore_env_args
echo "🚀 Updating AgentCore Runtime environment..."
# Preflight again: --auto-update-on-conflict updates the existing runtime, and
# the toolkit re-runs its account-wide Transaction Search step on every launch.
if ! run_toolkit_preflight; then
  exit 1
fi
uv run agentcore launch --auto-update-on-conflict "${AGENTCORE_ENV_ARGS[@]}"
echo "✅ AgentCore Runtime updated with role models and available service configuration"

cd "$PROJECT_ROOT"
echo ""

# Step 5c: Post-launch Transaction Search recheck (read-only, non-blocking)
# `agentcore launch` performs its own Transaction Search enable on every launch
# (Step 2, only on a first deploy, and Step 5b). A first-time API enable does
# not complete it: the AWS-reserved 'aws/spans' log group is created only by the
# console or a destination toggle. Running this recheck after the run's final
# launch reports whether spans can actually flow so a deploy does not finish
# silently with no Transaction Search. The `|| true` keeps the recheck
# non-blocking at the call site regardless of the child's exit status; the
# recheck never mutates account state.
# See: https://github.com/aws/bedrock-agentcore-starter-toolkit/issues/457
echo "📡 Step 5c: Rechecking Transaction Search state..."
AWS_REGION="$AWS_REGION" \
  bash "$SCRIPT_DIR/infrastructure/setup-account-observability.sh" report-transaction-search || true
echo ""

# Steps 6-8: Frontend build, deploy, and security (require Docker)
if [ "$DOCKER_AVAILABLE" = true ]; then

# Step 6: Build and push frontend container
echo "🐳 Step 6: Building and pushing frontend container..."
cd "$PROJECT_ROOT/ui"

# Get ECR repository URI
FRONTEND_ECR_REPO=$(aws cloudformation describe-stacks \
  --stack-name "${PROJECT_NAME}-infrastructure" \
  --region $AWS_REGION \
  --query 'Stacks[0].Outputs[?OutputKey==`FrontendRepositoryUri`].OutputValue' \
  --output text)

echo "Frontend ECR Repository: $FRONTEND_ECR_REPO"

# Login to ECR
aws ecr get-login-password --region $AWS_REGION | \
  docker login --username AWS --password-stdin $FRONTEND_ECR_REPO

# Build and push under BOTH :latest and a unique per-deploy tag. The unique tag
# is passed to the frontend stack as ImageTag: with a constant tag the stack
# parameters never change, CloudFormation no-ops, and ECS Express keeps running
# the task it resolved at first deploy — new images land in ECR but are never
# served (frontend fixes silently don't ship).
FRONTEND_IMAGE_TAG="deploy-$(git -C "$PROJECT_ROOT" rev-parse --short HEAD 2>/dev/null || echo manual)-$(date +%Y%m%d%H%M%S)"
docker build --platform linux/amd64 -t $FRONTEND_ECR_REPO:latest -t $FRONTEND_ECR_REPO:$FRONTEND_IMAGE_TAG .
docker push $FRONTEND_ECR_REPO:latest
docker push $FRONTEND_ECR_REPO:$FRONTEND_IMAGE_TAG
echo "   Frontend image tag: $FRONTEND_IMAGE_TAG"

echo "✅ Frontend container pushed"

# Generate SBOMs (if Syft is installed)
if command -v syft &> /dev/null; then
  echo ""
  echo "📦 Step 6b: Generating SBOMs..."
  bash "$SCRIPT_DIR/generate-sbom.sh" "$FRONTEND_ECR_REPO:latest"
  echo "✅ SBOMs generated"
else
  echo "⚠️  Syft not installed, skipping SBOM generation (brew install syft)"
fi
echo ""

cd "$PROJECT_ROOT"

# Step 7: Deploy frontend
echo "🌐 Step 7: Deploying frontend..."

# Get KB IDs
GAMELIFT_KB_ID=$(grep "^GBAW_GAMELIFT_KB_ID=" "$PROJECT_ROOT/backend/.env.local" 2>/dev/null | cut -d'=' -f2 || echo "")
EKS_KB_ID=$(grep "^GBAW_EKS_KB_ID=" "$PROJECT_ROOT/backend/.env.local" 2>/dev/null | cut -d'=' -f2 || echo "")
COST_KB_ID=$(grep "^GBAW_COST_KB_ID=" "$PROJECT_ROOT/backend/.env.local" 2>/dev/null | cut -d'=' -f2 || echo "")

aws cloudformation deploy \
  --template-file "$PROJECT_ROOT/infrastructure/cloudformation/02-frontend-ecs-express.yaml" \
  --stack-name "${PROJECT_NAME}-frontend" \
  --parameter-overrides \
    ProjectName="$PROJECT_NAME" \
    RuntimeId="$RUNTIME_ID" \
    ImageTag="$FRONTEND_IMAGE_TAG" \
    TenantId="$GBAW_TENANT_ID" \
    WorkspaceId="$GBAW_WORKSPACE_ID" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region $AWS_REGION

# Get frontend URL
FRONTEND_URL=$(aws cloudformation describe-stacks \
  --stack-name "${PROJECT_NAME}-frontend" \
  --region $AWS_REGION \
  --query 'Stacks[0].Outputs[?OutputKey==`ServiceUrl`].OutputValue' \
  --output text)

echo "✅ Frontend deployed"
echo ""

# Step 7b: Set retention on auto-created log groups
echo "📋 Step 7b: Setting log retention on auto-created log groups..."
bash "$SCRIPT_DIR/infrastructure/setup-app-observability.sh" "$RUNTIME_ID"
echo ""

# Step 8: Deploy security infrastructure (WAF on ALB + CloudTrail)
# Note: WAF is REGIONAL scope, attached to the ECS Express ALB
echo "🔒 Step 8: Deploying security infrastructure..."
echo "   Note: WAF attached to ECS Express ALB"

# Get ALB ARN from frontend stack for WAF attachment
FRONTEND_ALB_ARN=$(aws cloudformation describe-stacks \
  --stack-name "${PROJECT_NAME}-frontend" \
  --region $AWS_REGION \
  --query 'Stacks[0].Outputs[?OutputKey==`LoadBalancerArn`].OutputValue' \
  --output text)

echo "   ALB ARN: $FRONTEND_ALB_ARN"

# Raise the ALB idle timeout above the agent wall-clock budget. The ECS Express
# service manages its own ALB (no CloudFormation handle we can set attributes on),
# and the AWS default idle timeout is 60s — but a complex multi-specialist agent
# run can take up to GBAW_AGENT_TIMEOUT_REQUEST_SECONDS (180s) and the proxy waits
# 185s. At the 60s default the ALB severs the connection mid-run, returning a 504
# while the backend keeps working and persists the answer to memory — the user
# sees no answer and no error (issue #250). 190s sits just above the proxy budget.
# Idempotent: re-running just re-asserts the same value.
if [ -n "$FRONTEND_ALB_ARN" ] && [ "$FRONTEND_ALB_ARN" != "None" ]; then
  echo "   Setting ALB idle timeout to 190s (default 60s severs long agent runs — #250)..."
  aws elbv2 modify-load-balancer-attributes \
    --load-balancer-arn "$FRONTEND_ALB_ARN" \
    --attributes Key=idle_timeout.timeout_seconds,Value=190 \
    --region $AWS_REGION \
    --query 'Attributes[?Key==`idle_timeout.timeout_seconds`].Value' \
    --output text
fi

aws cloudformation deploy \
  --template-file "$PROJECT_ROOT/infrastructure/cloudformation/05-security-infrastructure.yaml" \
  --stack-name "${PROJECT_NAME}-security" \
  --parameter-overrides \
    ProjectName="$PROJECT_NAME" \
    FrontendResourceArn="$FRONTEND_ALB_ARN" \
    RateLimitPerIP=2000 \
    AuthAdminRateLimitPerIP=100 \
    CloudTrailRetentionDays=90 \
    AIChatMode=true \
  --capabilities CAPABILITY_NAMED_IAM \
  --region $AWS_REGION

# Step 8b: Enable AWS Inspector for ECR vulnerability scanning via CLI
# AWS::Inspector2::Enabler is not a valid CloudFormation resource type in all regions,
# so we enable Inspector via the AWS CLI instead. This is idempotent.
echo "🔍 Step 8b: Enabling AWS Inspector for ECR scanning..."
if aws inspector2 enable --resource-types ECR --region $AWS_REGION 2>/dev/null; then
  echo "✅ AWS Inspector ECR scanning enabled"
else
  echo "⚠️  AWS Inspector could not be enabled (may not be available in $AWS_REGION)"
fi

# Get WAF and CloudTrail info
WAF_ACL_ARN=$(aws cloudformation describe-stacks \
  --stack-name "${PROJECT_NAME}-security" \
  --region $AWS_REGION \
  --query 'Stacks[0].Outputs[?OutputKey==`WebACLArn`].OutputValue' \
  --output text 2>/dev/null || echo "")

# ECS Express owns the ALB lifecycle and can leave a platform WAF attached even
# when CloudFormation reports this association as complete. Reassert and verify
# the project ACL after the frontend deployment so the declared auth/admin and
# application rate limits are the active controls.
echo "🔗 Step 8c: Reconciling WAF association..."
reconcile_waf_association "$WAF_ACL_ARN" "$FRONTEND_ALB_ARN"

CLOUDTRAIL_ARN=$(aws cloudformation describe-stacks \
  --stack-name "${PROJECT_NAME}-security" \
  --region $AWS_REGION \
  --query 'Stacks[0].Outputs[?OutputKey==`CloudTrailArn`].OutputValue' \
  --output text 2>/dev/null || echo "")

echo "✅ Security infrastructure deployed"
echo ""

else
  # Docker not available — skip Steps 6-8
  echo "⏭️  Steps 6-8: Skipped (Docker not available)"
  echo ""
fi

# Final summary
echo "=================================================="
if [ "$DOCKER_AVAILABLE" = true ]; then
  echo "✅ Deployment Complete!"
else
  echo "⚠️  Deployment Partially Complete (backend only)"
fi
echo "=================================================="
echo ""

if [ "$DOCKER_AVAILABLE" = true ]; then
echo "📍 Access URL:"
echo "   Frontend: https://$FRONTEND_URL"
echo ""
fi

echo "🔑 Infrastructure IDs:"
echo "   Runtime ID:    $RUNTIME_ID"
echo "   Guardrail ID:  $GUARDRAIL_ID"
echo "   GameLift KB:   $GAMELIFT_KB_ID"
echo "   EKS KB:        $EKS_KB_ID"
echo "   Cost KB:       $COST_KB_ID"
if [ -n "$CLOUDTRAIL_ARN" ] && [ "$CLOUDTRAIL_ARN" != "None" ]; then
  echo "   CloudTrail:    $CLOUDTRAIL_ARN"
fi
if [ -n "$WAF_ACL_ARN" ] && [ "$WAF_ACL_ARN" != "None" ]; then
  echo "   WAF ACL:       $WAF_ACL_ARN"
fi
echo ""

if [ "$DOCKER_AVAILABLE" = true ]; then
echo "🔒 Security Features:"
echo "   ✅ WAF attached to ECS Express ALB"
echo "   ✅ Rate limiting: 2000 req/5min/IP"
echo "   ✅ OWASP managed rules active"
echo "   ✅ SQL injection protection"
echo "   ✅ CloudTrail API audit logging enabled"
echo ""
echo "🚀 Next Steps:"
echo "   1. Create admin user: ./scripts/infrastructure/add-admin-user.sh"
echo "   2. Access frontend: https://$FRONTEND_URL"
echo "   3. Subscribe to security alerts:"
echo "      aws sns subscribe --topic-arn \$(aws cloudformation describe-stacks --stack-name ${PROJECT_NAME}-security --region $AWS_REGION --query 'Stacks[0].Outputs[?OutputKey==\`SecurityAlertsTopicArn\`].OutputValue' --output text) --protocol email --notification-endpoint YOUR_EMAIL"
else
echo "⚠️  Frontend was not deployed — Docker is required for Steps 6-8."
echo "   Install/start Docker and re-run this script to deploy the UI."
echo "   https://docs.docker.com/get-docker/"
fi
echo ""
