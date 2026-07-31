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
- **Bedrock Model Access**: Claude Sonnet 4.5 and Claude Haiku 4.5 enabled in your region
- **Service Quotas**: Default quotas are sufficient for most deployments
- **IAM Permissions**: Administrator access (or equivalent) for initial deployment

### Enable Bedrock Models

1. Go to [Amazon Bedrock Console](https://console.aws.amazon.com/bedrock/)
2. Navigate to **Model access** in the left sidebar
3. Click **Manage model access**
4. Enable:
   - Anthropic Claude 4.5 Sonnet
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
- `BEDROCK_MODEL_ID`: AI model to use (default: Claude Sonnet 4.5)

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
| AI Models | Bedrock | Claude 4.5 Sonnet/Haiku |
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

# Run validation tests
./test-cloud.sh
```

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
| Bedrock (Claude Sonnet + Haiku) | $20-700+ | Dominant cost; ~$3.30/M input, ~$16.50/M output (Sonnet). Token costs at standard (uncached) rates. Caching: reads 0.1× input, writes 1.25× (5-min TTL); net benefit requires >12% hit rate |
| ECS Fargate | $36-47 | 1 vCPU, 2 GB task; $36.04 at MinTasks: 1 (730 hrs), ~$47 at avg ~1.3 tasks under moderate load (~950 task-hrs/mo) |
| Bedrock Guardrails | $3-32 | Billed per input + output text unit per safeguard: content filters ($0.15/1K TU) + denied topics ($0.15/1K TU) + PII ($0.10/1K TU) |
| Bedrock AgentCore Runtime | $1-10 | CPU billed on active consumption only (I/O wait free); memory billed for full session duration |
| Bedrock AgentCore Memory | $5-13 | STM: ~3 events per query @ $0.25/1K events + LTM retrieval: 1 request/query @ $0.50/1K requests (billed even for empty results) |
| WAF | $11 | $5 WebACL + $6 rules + $0.60/M requests (shared ~100K HTTP request assumption) |
| CloudWatch + X-Ray | $5-10 | Log ingestion, metrics, traces |
| ALB | $17-20 | $16.43 fixed hourly + LCU (based on max of: new connections, active connections, bytes, rule evaluations) |
| Knowledge Bases (S3 Vectors) | <$1 | S3 Vectors storage + $2.50/M query requests + Titan Embed V2 @ $0.02/M tokens |
| CloudTrail + KMS | ~$2 | Management events (first trail free) + 1 CMK @ $1/mo |
| S3 (docs + logs + artifacts) | $1-5 | Standard storage across 5 buckets |
| Cognito | Free | Up to 50,000 MAUs |

**Base infrastructure**: ~$80-140/month (Fargate, ALB, WAF, Guardrails, CloudWatch, KMS, CloudTrail)
**AI usage (variable)**: $20-700+/month depending on query volume and conversation length

### Cost Optimization Tips

- Prompt caching is enabled by default. Monitor `CacheReadInputTokenCount` vs `CacheWriteInputTokenCount` in CloudWatch — a read-to-write ratio >3:1 indicates net savings; below that, caching may cost more than uncached input
- Knowledge Bases use S3 Vectors (not OpenSearch) for cost-effective vector storage — near-zero cost at small scale
- ECS Fargate scales between 1-4 tasks based on load (configurable via MinTasks/MaxTasks)
- AgentCore Runtime only charges for active CPU time; memory is billed for full session duration regardless of I/O wait
- Monitor CloudWatch dashboards to track AI token consumption

## Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AWS_REGION` | No | us-west-2 | AWS deployment region |
| `AWS_PROFILE` | No | default | AWS credentials profile |
| `BEDROCK_MODEL_ID` | No | claude-sonnet-4-5 | Primary AI model |
| `NEXT_PUBLIC_SKIP_AUTH` | No | false | Skip auth (dev only) |
| `MEMORY_LONG_TERM_ENABLED` | No | true | Enable cross-session memory |
| `BEDROCK_GUARDRAIL_ENABLED` | No | true | Enable AI safety guardrails |

## Security Notes

- **Authentication**: Cognito enforces admin-only user creation
- **AI Safety**: Bedrock Guardrails filter harmful content
- **IAM**: Least-privilege roles for all components
- **EKS Access**: Read-only permissions, secrets excluded
- **Data**: All data stays within your AWS account
