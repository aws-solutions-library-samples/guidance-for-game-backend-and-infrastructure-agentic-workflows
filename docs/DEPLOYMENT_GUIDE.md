# Game Agent - Deployment Guide

This guide walks you through deploying Game Agent to your AWS account.

## Prerequisites

Before you begin, ensure you have the following installed and configured:

### Required Tools

| Tool | Version | Installation |
|------|---------|--------------|
| **AWS CLI** | v2+ | [Install Guide](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) |
| **Node.js** | 18+ | [Download](https://nodejs.org/) |
| **UV** | 0.9+ | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **Docker** | Latest | [Install Guide](https://docs.docker.com/get-docker/) |
| **yq** | v4+ | [Install Guide](https://github.com/mikefarah/yq#install) |

**Note:** Python 3.13 and the AgentCore CLI (`bedrock-agentcore-starter-toolkit`) are automatically installed by UV during setup (`uv sync`).

**Windows users:** Native PowerShell deployment scripts are available in `scripts/powershell/`. See [scripts/powershell/README.md](../scripts/powershell/README.md) for setup and usage. Requires PowerShell 7.0+ and AWS CLI v2 — no WSL2 or Linux environment needed.

### AWS Account Requirements

Your AWS account needs the following:
- **Bedrock Model Access**: Claude Sonnet 4.6 and Claude Haiku 4.5 enabled in your region
- **Service Quotas**: Default quotas are sufficient for most deployments
- **IAM Permissions**: Administrator access (or equivalent) for initial deployment

### Enable Bedrock Models

1. Go to [Amazon Bedrock Console](https://console.aws.amazon.com/bedrock/)
2. Navigate to **Model access** in the left sidebar
3. Click **Manage model access**
4. Enable:
   - Anthropic Claude Sonnet 4.6
   - Anthropic Claude 4.5 Haiku
5. Click **Save changes**

## Quick Start

**Step 1:** Configure and verify AWS credentials (run `aws configure` separately — it's interactive):
```bash
aws configure
aws sts get-caller-identity  # Verify credentials work
```

**Step 2:** Configure and deploy:
```bash
# Set your preferred region (optional, defaults to us-west-2)
export AWS_REGION=us-west-2

# Create environment config — you can also set AWS_PROFILE here instead of the command below
cp ui/.env.local.example ui/.env.local

# Deploy everything
./deploy-all.sh
# Optional: override AWS profile (can also be set in ui/.env.local)
# AWS_PROFILE=<your-profile> ./deploy-all.sh

# Create your admin user
./scripts/infrastructure/add-admin-user.sh
```

That's it! Access your deployment at the URL shown after deployment completes.

## Detailed Deployment Steps

### Step 1: Configure AWS Credentials

**Option A:** Use AWS CLI configuration (interactive — run separately):
```bash
aws configure
```

**Option B:** Use environment variables:
```bash
export AWS_ACCESS_KEY_ID=your_access_key
export AWS_SECRET_ACCESS_KEY=your_secret_key
export AWS_REGION=us-west-2
```

Verify credentials work:
```bash
aws sts get-caller-identity
```

### Step 2: Set Your Region

Game Agent defaults to `us-west-2`. To deploy to a different region:

```bash
export AWS_REGION=your-preferred-region
```

**Supported regions** (must have Bedrock with Claude models):
- us-east-1 (N. Virginia)
- us-west-2 (Oregon)
- eu-west-1 (Ireland)
- ap-northeast-1 (Tokyo)

### Step 3: Create Environment Configuration

```bash
cp ui/.env.local.example ui/.env.local
```

Edit `ui/.env.local` if you need to customize:
- `AWS_REGION`: Your deployment region
- `GBAW_ORCHESTRATOR_MODEL_ID`: Orchestrator model or inference profile (default: Claude Haiku 4.5)
- `GBAW_SPECIALIST_MODEL_ID`: GameLift, EKS, and Cost model or inference profile (default: Claude Sonnet 4.6)
- `GBAW_TENANT_ID`: Trusted tenant binding for this deployment (default: `default-tenant`)
- `GBAW_WORKSPACE_ID`: Trusted workspace binding for this deployment (default: `default-workspace`)

Process environment values take precedence over `ui/.env.local`. The canonical role variables above take precedence over the compatibility aliases `GBAW_BEDROCK_MODEL_ID` and `GBAW_BEDROCK_MODEL_ID_SECONDARY`; empty values are treated as unset. The deployment passes the resolved role IDs to AgentCore on both initial launch and updates.

Tenant and workspace are server-side identity bindings. The deployment passes
them to the frontend container without a `NEXT_PUBLIC_` prefix; browser request
data cannot override them. See
[Identity and Authorization](IDENTITY_AND_AUTHORIZATION.md).

### Step 4: Deploy

```bash
./deploy-all.sh
# Optional: override AWS profile (can also be set in ui/.env.local)
# AWS_PROFILE=<your-profile> ./deploy-all.sh
```

This automated script:
1. Creates base infrastructure (Cognito, IAM, ECR)
2. Deploys Bedrock Guardrails for AI safety
3. Launches AgentCore Runtime with AI agents
4. Deploys observability stack
5. Creates and seeds Knowledge Bases
6. Builds and pushes frontend container
7. Deploys frontend on ECS Express (Fargate + ALB)

**Expected duration**: 15-25 minutes

### Step 5: Create Admin User

After deployment completes:

```bash
./scripts/infrastructure/add-admin-user.sh
```

You'll be prompted for:
- Email address
- Password (min 8 characters, uppercase, lowercase, number, symbol)

### Step 6: Access Your Deployment

The deployment script outputs your frontend URL:

```
Frontend URL: https://<service-id>.ecs.<region>.on.aws
```

Log in with the admin credentials you created.

## What Gets Deployed

| Component | AWS Service | Purpose |
|-----------|-------------|---------|
| Frontend | ECS Express (Fargate + ALB) | Web UI with chat interface |
| Backend | Bedrock AgentCore | AI agents and orchestration |
| Auth | Cognito | User authentication |
| AI Models | Bedrock | Claude Haiku 4.5 orchestrator and Claude Sonnet 4.6 specialists |
| Knowledge Bases | Bedrock | RAG for GameLift, EKS, Cost |
| Guardrails | Bedrock | AI safety controls |
| Observability | CloudWatch | Logging and monitoring |

## Post-Deployment Configuration

### Enroll EKS Clusters (Optional)

To enable Kubernetes monitoring for your EKS clusters:

```bash
cd infrastructure/kubernetes
./enroll-cluster.sh your-cluster-name us-west-2
```

This grants Game Agent read-only access to monitor pods, deployments, and services.

### Verify Deployment Health

```bash
# Check all stacks
aws cloudformation list-stacks --query 'StackSummaries[?contains(StackName, `game-agent`)]'

# Validate resources and cloud integration
./validate-deployment.sh
./test-cloud.sh

# Verify all three Knowledge Bases (GameLift, EKS, Cost) return retrieval
# results; exits non-zero if any stack, KB, or retrieval is broken
./scripts/infrastructure/test-kb.sh

# Exercise Guardrail behavior and specialist routing/tool use
./test-ai-evals.sh

# Confirm startup logged both resolved model roles
aws logs tail /aws/bedrock-agentcore/runtimes/gameagentruntime-<ID>-DEFAULT \
  --since 30m --region "$AWS_REGION" --profile <your-profile> \
  --filter-pattern '"Orchestrator model" || "Specialist model"'
```

For the deployment smoke test, send one off-topic request expected to activate the Guardrail and one in-domain request such as "List my EKS clusters" expected to route to a specialist and invoke a client-side tool. Confirm the Guardrail result and tool-use span in AgentCore traces, and confirm the startup log identifies Haiku 4.5 for the orchestrator and Sonnet 4.6 for specialists. Response text alone is not sufficient evidence of model or tool selection.

## Teardown

To remove all deployed resources:

```bash
./teardown-all.sh
# Optional: override AWS profile
# AWS_PROFILE=<your-profile> ./teardown-all.sh
```

This removes all CloudFormation stacks, the AgentCore Runtime, and associated resources.

**Warning**: This action is irreversible. All data in Knowledge Bases will be deleted.

## Troubleshooting

### Common Issues

**"AgentCore CLI not found"**
```bash
# The agentcore CLI is provided by bedrock-agentcore-starter-toolkit,
# which is a dev dependency managed by uv. Run from the backend directory:
cd backend && uv sync
```

**"Bedrock model access denied"**
- Verify Claude models are enabled in the Bedrock console for your region

**"CloudFormation stack failed"**
```bash
# Check stack events
aws cloudformation describe-stack-events --stack-name game-agent-infrastructure
```

**"Frontend not loading"**
- Wait 5-10 minutes for ECS Express to fully deploy
- Check ECS frontend logs in CloudWatch

### Getting Help

- Check logs: `./scripts/infrastructure/check-deployment.sh`
- View CloudWatch dashboards in the AWS Console
- Review [Architecture Documentation](ARCHITECTURE.md)

## Cost Estimates

Approximate monthly costs at minimal usage (development/demo) in `us-west-2`:

| Service | Estimated Cost | Notes |
|---------|---------------|-------|
| Bedrock (Claude Sonnet 4.6 + Haiku 4.5) | $20-630+ | Dominant cost; uses `global.*` cross-region model IDs (~$3/M input, ~$15/M output for Sonnet 4.6; ~$1/M input, ~$5/M output for Haiku 4.5) |
| ECS Fargate | $36-47 | 1 vCPU, 2 GB task; $36.04 at MinTasks: 1 (730 hrs), ~$47 at avg ~1.3 tasks under moderate load (~950 task-hrs/mo) |
| Bedrock Guardrails | $3-32 | 4 guarded calls/query × (input + output) TU per safeguard: content filters ($0.15/1K TU) + denied topics ($0.15/1K TU) + PII ($0.10/1K TU) |
| Bedrock AgentCore Runtime | $1-10 | CPU billed on active consumption only (I/O wait free); memory billed for full session duration |
| Bedrock AgentCore Memory | $5-13 | STM: ~3 events/query @ $0.25/1K + LTM retrieval: 1 request/query @ $0.50/1K (billed for empty results) + LTM storage: ~1K records/mo @ $0.25/1K |
| WAF | $11 | $5 WebACL + $6 rules + $0.60/M requests (shared ~100K HTTP request assumption) |
| CloudWatch + X-Ray | $5-10 | Log ingestion, metrics, traces |
| ALB | ~$17 | $16.43 fixed + LCU variable (max of: new connections, active connections, processed bytes, rule evaluations). At 100K reqs/mo, 100KB total request+response/req, no mTLS, ≤10 rules: ~$0.08 LCU |
| Knowledge Bases (S3 Vectors) | <$1 | S3 Vectors storage + $2.50/M query requests + Titan Embed V2 @ $0.02/M tokens |
| CloudTrail + KMS | ~$2 | Management events (first trail free) + 1 CMK @ $1/mo |
| S3 (docs + logs + artifacts) | $1-5 | Standard storage across 5 buckets |
| Cognito | Free | Up to 50,000 MAUs |

**Base infrastructure**: ~$80-140/month (Fargate, ALB, WAF, Guardrails, CloudWatch, KMS, CloudTrail)
**AI usage (variable)**: $20-630+/month depending on query volume and conversation length

### Cost Optimization Tips

- Prompt caching is enabled by default. Cache-read share must exceed ~22% of cached tokens to break even (writes cost 1.25×, reads cost 0.1×). Min checkpoint: 1,024 tokens (Sonnet 4.6), 4,096 tokens (Haiku 4.5). Monitor `CacheReadInputTokenCount` vs `CacheWriteInputTokenCount` in CloudWatch
- Knowledge Bases use S3 Vectors (not OpenSearch) for cost-effective vector storage — near-zero cost at small scale
- ECS Fargate scales between 1-4 tasks based on load (configurable via MinTasks/MaxTasks)
- AgentCore Runtime only charges for active CPU time; memory is billed for full session duration regardless of I/O wait
- Monitor CloudWatch dashboards to track AI token consumption

## Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AWS_REGION` | No | us-west-2 | AWS deployment region |
| `AWS_PROFILE` | No | default | AWS credentials profile |
| `GBAW_ORCHESTRATOR_MODEL_ID` | No | `global.anthropic.claude-haiku-4-5-20251001-v1:0` | Orchestrator model/profile |
| `GBAW_SPECIALIST_MODEL_ID` | No | `global.anthropic.claude-sonnet-4-6` | All specialist models/profiles |
| `GBAW_BEDROCK_MODEL_ID` | No | unset | Legacy orchestrator alias |
| `GBAW_BEDROCK_MODEL_ID_SECONDARY` | No | unset | Legacy specialist alias |
| `GBAW_TENANT_ID` | No | `default-tenant` | Server-side trusted tenant binding |
| `GBAW_WORKSPACE_ID` | No | `default-workspace` | Server-side trusted workspace binding |
| `NEXT_PUBLIC_SKIP_AUTH` | No | false | Skip auth (dev only) |
| `GBAW_MEMORY_LONG_TERM_ENABLED` | No | true | Enable cross-session memory |
| `GBAW_BEDROCK_GUARDRAIL_ENABLED` | No | true | Enable AI safety Guardrails |

## Security Notes

- **Authentication**: Cognito enforces admin-only user creation
- **Identity propagation**: Authorization uses verified access-token claims plus server-bound tenant/workspace; ID-token presentation data does not grant backend authority
- **AI Safety**: Bedrock Guardrails filter harmful content
- **IAM**: Least-privilege roles for all components
- **EKS Access**: Read-only permissions, secrets excluded
- **Data**: All data stays within your AWS account
