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

# Step 1.7: Account-wide observability setup (idempotent)
# Ensures 'aws/spans' log group exists (AWS-reserved namespace) and configures
# Transaction Search. Toggles Transaction Search if log group is missing.
# Workaround for: https://github.com/aws/bedrock-agentcore-starter-toolkit/issues/457
echo "📡 Step 1.7: Setting up account-wide observability..."
bash "$SCRIPT_DIR/infrastructure/setup-account-observability.sh"

echo "✅ Account-wide observability configured"
echo ""

# Step 2: Launch AgentCore Runtime (direct code deployment via CodeBuild)
echo "🤖 Step 2: Launching AgentCore Runtime..."
cd "$PROJECT_ROOT/backend"

# Ensure backend dependencies (including agentcore CLI) are installed
echo "📦 Installing backend dependencies..."
uv sync > /dev/null 2>&1

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

# Run configure only if no existing runtime (first deploy)
EXISTING_RUNTIME=$(yq eval '.agents.gameagentruntime.bedrock_agentcore.agent_arn' .bedrock_agentcore.yaml 2>/dev/null || echo "")
if [ -z "$EXISTING_RUNTIME" ] || [ "$EXISTING_RUNTIME" = "null" ]; then
  echo "📝 Configuring AgentCore (first deploy)..."
  uv run agentcore configure \
    --entrypoint agentcore_main.py \
    --name gameagentruntime \
    --region $AWS_REGION \
    --execution-role "$EXECUTION_ROLE_ARN" \
    --requirements-file requirements.txt \
    --non-interactive
  echo "✅ Configuration ready"
else
  echo "📝 AgentCore already configured, skipping configure step"
fi

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
  uv run agentcore launch --auto-update-on-conflict

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
DELIVERY_SOURCE_NAME="${RUNTIME_ID}-traces-source"
DELIVERY_DEST_NAME="${RUNTIME_ID}-traces-destination"

# Create delivery source (idempotent)
if aws logs put-delivery-source \
    --name "$DELIVERY_SOURCE_NAME" \
    --log-type "TRACES" \
    --resource-arn "$RUNTIME_ARN" \
    --region $AWS_REGION > /dev/null 2>&1; then
  echo "  ✅ Delivery source created"
else
  echo "  ✅ Delivery source already exists"
fi

# Create delivery destination (idempotent)
DELIVERY_DEST_ARN=""
if DEST_RESULT=$(aws logs put-delivery-destination \
    --name "$DELIVERY_DEST_NAME" \
    --delivery-destination-type "XRAY" \
    --region $AWS_REGION 2>&1); then
  DELIVERY_DEST_ARN=$(echo "$DEST_RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin)['deliveryDestination']['arn'])" 2>/dev/null || echo "")
  echo "  ✅ Delivery destination created"
else
  # Already exists — construct the ARN
  DELIVERY_DEST_ARN="arn:aws:logs:${AWS_REGION}:$(aws sts get-caller-identity --query Account --output text):delivery-destination:${DELIVERY_DEST_NAME}"
  echo "  ✅ Delivery destination already exists"
fi

# Create delivery (idempotent)
if [ -n "$DELIVERY_DEST_ARN" ]; then
  if aws logs create-delivery \
      --delivery-source-name "$DELIVERY_SOURCE_NAME" \
      --delivery-destination-arn "$DELIVERY_DEST_ARN" \
      --region $AWS_REGION > /dev/null 2>&1; then
    echo "  ✅ Delivery created"
  else
    echo "  ✅ Delivery already exists"
  fi
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

if [ -n "$GAMELIFT_KB_ID" ] && [ -n "$EKS_KB_ID" ] && [ -n "$COST_KB_ID" ]; then
  echo "   GameLift KB: $GAMELIFT_KB_ID"
  echo "   EKS KB: $EKS_KB_ID"
  echo "   Cost KB: $COST_KB_ID"
  echo "🚀 Updating AgentCore Runtime with KB environment variables..."
  uv run agentcore launch --auto-update-on-conflict \
    -env "GBAW_GAMELIFT_KB_ID=$GAMELIFT_KB_ID" \
    -env "GBAW_EKS_KB_ID=$EKS_KB_ID" \
    -env "GBAW_COST_KB_ID=$COST_KB_ID" \
    -env "GBAW_BEDROCK_GUARDRAIL_ID=$GUARDRAIL_ID" \
    -env "GBAW_BEDROCK_GUARDRAIL_VERSION=DRAFT" \
    -env "GBAW_ORCHESTRATOR_PROMPT_ARN=$GBAW_ORCHESTRATOR_PROMPT_ARN" \
    -env "GBAW_GAMELIFT_PROMPT_ARN=$GBAW_GAMELIFT_PROMPT_ARN" \
    -env "GBAW_EKS_PROMPT_ARN=$GBAW_EKS_PROMPT_ARN" \
    -env "GBAW_COST_PROMPT_ARN=$GBAW_COST_PROMPT_ARN"
  echo "✅ AgentCore Runtime updated with KB IDs and Prompt ARNs"
else
  echo "⚠️  KB IDs not found in .env.local - skipping runtime update"
fi

cd "$PROJECT_ROOT"
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

# Build and push
docker build --platform linux/amd64 -t $FRONTEND_ECR_REPO:latest .
docker push $FRONTEND_ECR_REPO:latest

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
