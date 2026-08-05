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

Process environment values take precedence over `ui/.env.local`. The canonical role variables above take precedence over the compatibility aliases `GBAW_BEDROCK_MODEL_ID` and `GBAW_BEDROCK_MODEL_ID_SECONDARY`; empty values are treated as unset. The deployment passes the resolved role IDs to AgentCore on both initial launch and updates.

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

Approximate monthly costs at minimal usage (development/demo):

| Service | Estimated Cost | Notes |
|---------|---------------|-------|
| ECS Fargate | $30-70 | 1 vCPU, 2GB task (~$0.04048/vCPU-hour + $0.004445/GB-hour) |
| Bedrock AgentCore | $10-30 | Compute time per request |
| Bedrock (Claude Sonnet) | $20-100+ | ~$3/M input tokens, ~$15/M output tokens |
| Knowledge Bases (S3 Vectors) | $5-15 | S3 storage + ~$0.0004/1K vector queries |
| CloudWatch | $5-10 | Logs, metrics, dashboards |
| Cognito | Free | Up to 50,000 MAUs |
| S3 (source docs + logs) | $1-5 | Standard storage pricing |

**Base infrastructure**: ~$50-100/month
**AI usage (variable)**: $20-200+/month depending on query volume

### Cost Optimization Tips

- Use Bedrock prompt caching (enabled by default) to reduce token costs
- Knowledge Bases use S3 Vectors (not OpenSearch) for cost-effective vector storage
- ECS Fargate scales between 1-4 tasks based on load (configurable via MinTasks/MaxTasks)
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
| `NEXT_PUBLIC_SKIP_AUTH` | No | false | Skip auth (dev only) |
| `GBAW_MEMORY_LONG_TERM_ENABLED` | No | true | Enable cross-session memory |
| `GBAW_BEDROCK_GUARDRAIL_ENABLED` | No | true | Enable AI safety Guardrails |

## Security Notes

- **Authentication**: Cognito enforces admin-only user creation
- **AI Safety**: Bedrock Guardrails filter harmful content
- **IAM**: Least-privilege roles for all components
- **EKS Access**: Read-only permissions, secrets excluded
- **Data**: All data stays within your AWS account
