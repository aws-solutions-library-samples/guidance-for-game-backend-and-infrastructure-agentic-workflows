# Game Agent - Architecture

**AI-Powered Game Server Management**

This document describes the architecture of Game Agent, an AI-powered game server management system built on AWS Bedrock AgentCore Runtime with specialized AI agents for GameLift, EKS, and cost management.

---

## Overview

Game Agent uses a **simplified stdio-only MCP architecture** with specialized AI agents communicating with AWS Labs MCP servers via stdio transport exclusively, with no external infrastructure dependencies.

**Key Architecture Benefits**:
- **No External Infrastructure**: No EKS cluster, ALB, or networking complexity
- **Identical Dev/Prod**: Same stdio transport mechanism everywhere
- **Simplified Deployment**: Reduced deployment complexity
- **Cost Optimization**: Cost-effective stdio transport
- **Improved Reliability**: Fewer network failure points and dependencies

### Accepted Evolution: Optional Operations Control Plane

The architecture above describes the currently deployed, read-only chat
experience. The accepted design for a future optional operations control plane
preserves that path and adds separate operations services and execution trust
boundaries. These decisions do not deploy operations resources or grant
provider write permissions.

See the [architecture decision records](adr/README.md) for the accepted
boundaries and compatibility constraints.

See [Identity and Authorization](IDENTITY_AND_AUTHORIZATION.md) for the trusted
principal, read-path, approval, remote-client, and executor identity contracts.

---

## Architecture Diagram

![Multi-Agent Architecture](../diagrams/MultiAgent_Architecture.jpg)

> Full PDF version: [`diagrams/MultiAgent_Architecture.pdf`](../diagrams/MultiAgent_Architecture.pdf)

---

## Architecture Flow

The architecture follows these 12 steps:

| Step | Description |
|:----:|-------------|
| **1** | User authenticates with **Amazon Cognito** User Pool. The frontend validates JWT tokens and stores them in HttpOnly cookies. Password policies enforce strong credentials, and admin approval is required for new users. |
| **2** | User sends a natural language query (e.g., "What's the status of my EKS clusters?") through the **Next.js frontend** hosted on **Amazon ECS Express** (Fargate + ALB). The frontend provides a conversational chat interface powered by CopilotKit. |
| **3** | The frontend constructs trusted principal context from the verified Cognito access token and deployment-bound tenant/workspace, then invokes **Bedrock AgentCore Runtime** using the AWS SDK with SigV4 authentication. Browser and model input cannot supply principal fields. |
| **4** | AgentCore routes the request to the **Orchestrator** agent, which analyzes the query intent and determines the appropriate specialist to handle the request. The orchestrator maintains conversation context across turns. |
| **5** | **Bedrock Guardrails** filter both input and output. Inbound filtering detects prompt injection attempts, blocks off-topic requests, and warns about sensitive data. Outbound filtering anonymizes PII and blocks credential exposure. |
| **6** | The Orchestrator delegates to the appropriate **Specialist Agent** based on query classification: GameLift Specialist for fleet management, EKS Specialist for Kubernetes clusters, or Cost Specialist for billing analysis. |
| **7** | The Specialist agent queries the relevant **Bedrock Knowledge Base** for domain-specific context. Knowledge bases contain curated AWS documentation for GameLift, EKS, and Cost Optimization, stored in S3 and indexed with Titan embeddings. |
| **8** | The Specialist agent invokes **MCP Servers** (EKS, Cost Explorer) or **boto3 tools** (GameLift) for AWS integration. MCP servers use stdio transport for standardized AWS API access; GameLift uses native boto3 SDK tools directly. |
| **9** | Read-only API calls to target **AWS Services**: Amazon GameLift for fleet capacity and game sessions, Amazon EKS for cluster and node information, or AWS Cost Explorer for cost analysis and recommendations. IAM policies enforce least-privilege access. |
| **10** | **Amazon CloudWatch** captures structured logs from all components, while **AWS X-Ray** provides distributed tracing for request flow visualization. OpenTelemetry instrumentation enables end-to-end observability. |
| **11** | All container images are stored in **Amazon ECR** with vulnerability scanning enabled (ScanOnPush). Lifecycle policies automatically clean up old images, and private repository access is enforced. |
| **12** | Knowledge Base source documents are stored in **Amazon S3** with server-side encryption (AES-256), access logging enabled, and public access blocked. Documents are automatically chunked and embedded during ingestion. |

---

## Architecture Diagrams

### Production Architecture

```
┌───────────────────────────────────────────────────────────────┐
│                         User Browser                          │
└─────────────────────────────┬─────────────────────────────────┘
                              │ HTTPS
                              ▼
┌───────────────────────────────────────────────────────────────┐
│              ECS Express (Fargate + ALB) (Frontend)           │
│                  Next.js + CopilotKit                         │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  Next.js Application (Port 3000)                        │  │
│  │  • CopilotKit UI                                        │  │
│  │  • API Route: /api/copilot/chat.ts                      │  │
│  │  • Uses @aws-sdk/client-bedrock-agentcore               │  │
│  └─────────────────────────────────────────────────────────┘  │
└─────────────────────────────┬─────────────────────────────────┘
                              │ AWS SDK
                              │ InvokeAgentRuntimeCommand
                              │ (Signed SigV4)
                              ▼
┌───────────────────────────────────────────────────────────────┐
│                AWS Bedrock AgentCore Runtime                  │
│                Strands Agents + Embedded MCP                  │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  Python Application (agentcore_main.py)                 │  │
│  │  • Orchestrator (main agent)                             │  │
│  │  • GameLift Specialist                                  │  │
│  │  • EKS Specialist                                       │  │
│  │  • Cost Specialist                                      │  │
│  │                                                         │  │
│  │  Embedded MCP Servers (stdio processes):                │  │
│  │  • awslabs.eks-mcp-server (pre-installed)               │  │
│  │  • awslabs.aws-api-mcp-server (pre-installed)           │  │
│  │  • awslabs.cost-explorer-mcp-server (pre-installed)     │  │
│  └─────────────────────────────────────────────────────────┘  │
└─────────────────────────────┬─────────────────────────────────┘
                              │ AWS SDK (boto3)
                              ▼
┌───────────────────────────────────────────────────────────────┐
│                        AWS Services                           │
│  • Amazon Bedrock (Claude models)                             │
│  • Amazon EKS (Kubernetes clusters)                           │
│  • AWS GameLift (game server fleets)                          │
│  • AWS Cost Explorer (cost analysis)                          │
│  • Amazon Cognito (authentication)                            │
│  • Amazon CloudWatch (logging & monitoring)                   │
└───────────────────────────────────────────────────────────────┘
```

### Development Architecture

```
┌───────────────────────────────────────────────────────────────┐
│                          Browser                              │
│                      localhost:3000                           │
└─────────────────────────────┬─────────────────────────────────┘
                              │ HTTP
                              ▼
┌───────────────────────────────────────────────────────────────┐
│                    Next.js Dev Server                         │
│                    • CopilotKit UI                            │
│                    • Hot reload                               │
│                    • Development mode                         │
└─────────────────────────────┬─────────────────────────────────┘
                              │ HTTP API calls
                              ▼
┌───────────────────────────────────────────────────────────────┐
│                 AgentCore Runtime (Local)                     │
│                 localhost:8080                                │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  Python Application (agentcore_main.py)                 │  │
│  │  • Orchestrator                                         │  │
│  │  • GameLift Specialist                                  │  │
│  │  • EKS Specialist                                       │  │
│  │  • Cost Specialist                                      │  │
│  │                                                         │  │
│  │  Embedded MCP Servers (stdio):                          │  │
│  │  • awslabs.eks-mcp-server (pre-installed)               │  │
│  │  • awslabs.aws-api-mcp-server (pre-installed)           │  │
│  │  • awslabs.cost-explorer-mcp-server (pre-installed)     │  │
│  └─────────────────────────────────────────────────────────┘  │
└─────────────────────────────┬─────────────────────────────────┘
                              │ AWS SDK (boto3)
                              ▼
┌───────────────────────────────────────────────────────────────┐
│                        AWS Services                           │
│    • GameLift • EKS • Cost Explorer • Bedrock • CloudWatch    │
└───────────────────────────────────────────────────────────────┘
```

---

## Component Summary

### Authentication & Frontend
- **Amazon Cognito**: User authentication with group-based authorization
- **Amazon ECS Express**: Managed container hosting for Next.js frontend (Fargate + ALB), automatic HTTPS
- **Trusted principal adapter**: Access-token subject, client, audience, groups/scopes, and server-bound tenant/workspace propagation

### AI Runtime
- **Bedrock AgentCore**: Managed runtime for AI agents with built-in memory and session management
- **Orchestrator**: Central routing agent that classifies queries and delegates to specialists
- **Specialist Agents**: Domain-specific agents (GameLift, EKS, Cost) with tailored prompts and tools

### Security
- **Bedrock Guardrails**: Content filtering, PII protection, prompt injection detection
- **IAM Least Privilege**: Scoped policies with resource constraints and region conditions

### Integration
- **MCP Servers**: Model Context Protocol servers for AWS service integration (stdio transport)
  - EKS MCP + AWS API MCP: Kubernetes cluster management and AWS CLI-based resource discovery
  - Cost Explorer MCP: Cost analysis and optimization
- **boto3 Tools**: Native AWS SDK integration for GameLift fleet management

### Knowledge & Storage
- **Bedrock Knowledge Bases**: RAG (Retrieval-Augmented Generation) with S3 Vectors
- **Amazon S3**: Document storage with encryption and access logging

### Observability
- **Amazon CloudWatch**: Centralized logging and metrics
- **AWS X-Ray**: Distributed tracing for debugging

---

## Component Details

### Frontend (Next.js on ECS Express)

**Technology**: Next.js, TypeScript, CopilotKit, AWS SDK v3

**Key Files**:
- `ui/src/pages/api/copilot/chat.ts` - Main API route that invokes AgentCore Runtime
- `ui/src/components/Chat.tsx` - Chat UI component
- `ui/Dockerfile` - Container configuration (AMD64)

**How It Works**:
1. User sends message through CopilotKit UI
2. Frontend API route receives GraphQL-style request
3. Uses `@aws-sdk/client-bedrock-agentcore` to invoke AgentCore Runtime
4. Sends `InvokeAgentRuntimeCommand` with signed SigV4 request
5. Receives streaming response from AgentCore Runtime
6. Formats response for CopilotKit and returns to UI

**Key Code Pattern**:
```typescript
import { BedrockAgentCoreClient, InvokeAgentRuntimeCommand } from '@aws-sdk/client-bedrock-agentcore';

const client = new BedrockAgentCoreClient({
  region: process.env.AWS_REGION || 'us-west-2'
});

const command = new InvokeAgentRuntimeCommand({
  agentRuntimeArn: process.env.AGENTCORE_RUNTIME_ARN,
  contentType: 'application/json',
  payload: new TextEncoder().encode(JSON.stringify({ prompt: message }))
});

const response = await client.send(command);
```

**Environment Variables**:
- `AWS_REGION` - AWS region (default: us-west-2)
- `AGENTCORE_RUNTIME_ARN` - ARN of deployed AgentCore Runtime
- `NODE_ENV` - Environment (development/production)
- `BACKEND_URL` - For local development only (http://localhost:8080)

**Frontend Detection Logic**:
```typescript
const useAgentCoreSDK = !!process.env.AGENTCORE_RUNTIME_ID;

if (useAgentCoreSDK) {
  // Production: AWS SDK to cloud runtime
} else {
  // Development: HTTP to localhost:8080
}
```

### Backend (AgentCore Runtime)

**Technology**: Python 3.13 (managed by UV), Bedrock AgentCore SDK, Strands framework

**Key Files**:
- `backend/src/agentcore_main.py` - AgentCore Runtime entrypoint
- `backend/src/agents/orchestrator.py` - Main orchestrator agent
- `backend/src/agents/gamelift_specialist.py` - GameLift operations
- `backend/src/agents/eks_specialist.py` - EKS/Kubernetes operations
- `backend/src/agents/cost_specialist.py` - Cost analysis
- `backend/Dockerfile` - Container configuration (ARM64)

**How It Works**:
1. AgentCore Runtime receives invocation from frontend
2. Calls `invoke_agent()` function with prompt and context
3. Routes to Orchestrator
4. Orchestrator analyzes query and delegates to specialist agents
5. Specialists use MCP servers or AWS SDK for operations
6. Response flows back through orchestrator to AgentCore Runtime
7. AgentCore Runtime streams response to frontend

**Key Code Pattern**:
```python
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

@app.entrypoint
def invoke_agent(prompt, context=None):
    if isinstance(prompt, dict):
        user_prompt = prompt.get('prompt', '')
    else:
        user_prompt = str(prompt)

    response = orchestrator(user_prompt)
    return str(response)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
```

**Environment Variables**:
- `AWS_REGION` - AWS region (default: us-west-2)
- `GBAW_ORCHESTRATOR_MODEL_ID` - Orchestrator model/profile (default: Claude Haiku 4.5)
- `GBAW_SPECIALIST_MODEL_ID` - Specialist model/profile (default: Claude Sonnet 4.6)
- `GBAW_BEDROCK_MODEL_ID` / `GBAW_BEDROCK_MODEL_ID_SECONDARY` - Legacy compatibility aliases
- `GBAW_TENANT_ID` - Server-side tenant binding (default: `default-tenant`)
- `GBAW_WORKSPACE_ID` - Server-side workspace binding (default: `default-workspace`)
- `MCP_TIMEOUT` - MCP server execution timeout (default: 30 seconds)
- `MCP_RETRY_COUNT` - Number of retry attempts (default: 2)
- `MCP_FALLBACK_ENABLED` - Enable AWS SDK fallback (default: true)

Canonical role variables take precedence over legacy aliases, then repository defaults. Haiku handles orchestration while Sonnet handles all specialist requests. This assignment is independent from failure handling: both models receive the shared Botocore adaptive retry configuration, and the runtime never substitutes one role model for the other. After retries, specialist tools return their configured AWS SDK/CLI fallback or a generic retry response, while orchestrator errors reach the top-level request handler. Prompt caching, streaming, Guardrails, and client-side specialist tools remain enabled for both roles.

---

## MCP Integration

**Technology**: Model Context Protocol with unified stdio transport exclusively

### MCP Servers (from AWS Labs)

1. **EKS MCP Server** - Kubernetes operations beyond basic boto3
   - Command: `awslabs.eks-mcp-server` (pre-installed package)
   - Tools: Advanced EKS cluster management and troubleshooting

2. **AWS API MCP Server** - AWS CLI bridge for resource discovery
   - Command: `awslabs.aws-api-mcp-server` (pre-installed package)
   - Tools: `call_aws` (runs AWS CLI commands, e.g., `aws eks list-clusters` for EKS cluster discovery)

3. **Billing and Cost Management MCP Server** - Forecasting and optimization analysis
   - Command: `awslabs.billing-cost-management-mcp-server` (pre-installed package)
   - Tools include Cost Explorer forecasts and cost optimization operations

### Deterministic Cost Reports

Actual cost totals and service rankings use the owned `get_cost_report` boto3
tool. It converts the inclusive user end date to Cost Explorer's exclusive
`End`, aggregates every page of one grouped query, performs all arithmetic with
`Decimal`, validates the result, and emits a fixed financial section. Report
snapshots are cached by random report ID so follow-up calculations can reuse the
same data without silently mixing Cost Explorer query times.
The cost specialist rejects the Billing MCP server's historical
`getCostAndUsage` operations so they cannot bypass this path; forecast and
optimization operations remain available.

Snapshot correctness and billing finality are separate. A validated report is
internally consistent with Cost Explorer at its `queriedAt` timestamp, while an
open billing period marked `estimated` can still change when AWS backfills or
finalizes usage.

### MCP Client Factory Pattern

```python
from utils.mcp_client_factory import create_mcp_client

# Factory pattern creates new MCPClient instances per agent call
# MCP tools are automatically available to agents with fallback
```

**Technical Notes**:
- Factory Pattern: `create_mcp_client()` creates new MCPClient instances per agent call
- No Connection Pooling: Strands MCPClient uses lambda-based stdio connections
- Process Lifecycle: MCP library controls subprocess management

### Architecture Benefits
- **Zero Infrastructure**: No EKS cluster, ALB, or external dependencies
- **Embedded Execution**: MCP servers run within AgentCore Runtime container
- **Automatic Management**: Process lifecycle handled transparently
- **Reliable Fallback**: AWS SDK operations when MCP unavailable

---

## Security Controls

This architecture implements the following security measures:

- IAM least privilege with scoped policies
- SigV4 authentication for all AWS API calls
- Secrets management via environment injection
- S3 access logging for audit trails
- Security integration tests
- **GenAI**: Prompt validation, guardrails, data leakage protection

See [`SECURITY.md`](../SECURITY.md) and [`THREAT_MODEL.md`](THREAT_MODEL.md) for detailed security documentation.

---

## Deployment Architecture

### Development Environment

```
Local Machine:
├── Frontend: http://localhost:3000 (Next.js dev server)
├── Backend: http://localhost:8080 (AgentCore Runtime)
│   └── Embedded MCP Servers (stdio processes)
│       ├── awslabs.eks-mcp-server
│       ├── awslabs.aws-api-mcp-server
│       └── awslabs.cost-explorer-mcp-server
└── AWS Services: Direct boto3 integration
```

**Start**: `./dev-start.sh`

### Production Environment

```
AWS Cloud:
├── Frontend: ECS Express / Fargate + ALB (AMD64 container)
│   └── Invokes AgentCore Runtime via AWS SDK
├── Backend: AgentCore Runtime (ARM64 container)
│   └── Managed by AWS Bedrock
│   └── Embedded MCP Servers (stdio processes)
└── AWS Services: Direct boto3 integration
```

**Deploy**: `./deploy-all.sh`

### Why AgentCore Runtime?

1. **Serverless & Managed**: No infrastructure management
2. **Cost-Effective**: Pay only for actual compute time
3. **Session Isolation**: Each user gets dedicated microVM
4. **Extended Execution**: Up to 8 hours for complex operations
5. **Large Payloads**: 100MB payload support
6. **Built-in Observability**: Agent-specific tracing
7. **MCP Integration**: Native support for Model Context Protocol

### Why AWS SDK in Frontend?

AgentCore Runtime **does not expose HTTP endpoints**. It only supports:
- AWS SDK invocation via `InvokeAgentRuntime` API
- Signed SigV4 requests for security
- Streaming responses for real-time interaction

---

## AgentCore Runtime Details

### Response Format Differences

**Development (Local HTTP)**:
```json
{"response": "actual content"}
```

**Production (AgentCore SDK)**:
```json
"\"actual content\""
```

Frontend automatically handles both by detecting `AGENTCORE_RUNTIME_ID` environment variable.

### Critical: .bedrock_agentcore.yaml

The `.bedrock_agentcore.yaml` file is generated by the AgentCore CLI:

- **Generated automatically** by `agentcore launch` command
- **Used by scripts** to extract runtime ARN and memory ID
- **Not source of truth for production** - CloudFormation parameters are authoritative

**Usage in scripts**:
```bash
# Extract runtime ARN
RUNTIME_ARN=$(yq eval '.agents.gameagentruntime.bedrock_agentcore.agent_arn' .bedrock_agentcore.yaml)

# Extract memory ID
MEMORY_ID=$(yq eval '.agents.gameagentruntime.memory.memory_id' .bedrock_agentcore.yaml)
```

### Runtime ID Management

1. `agentcore launch` generates runtime and updates `.bedrock_agentcore.yaml`
2. Deploy script extracts runtime ID from YAML file
3. CloudFormation passes runtime ID to frontend stack
4. Frontend uses `AGENTCORE_RUNTIME_ID` environment variable
5. Development falls back to local HTTP when env var not set

---

## Memory Architecture

### Short-Term Memory (Default)

CopilotKit sends full conversation history with each request. No backend persistence needed.

### Long-Term Memory (Optional)

Enable cross-session memory:

```bash
export MEMORY_LONG_TERM_ENABLED=true
```

**How It Works**:
- **Load**: Previous conversations loaded from AgentCore Memory on each request
- **Persist**: Each interaction saved to AgentCore Memory after completion
- **Cross-Session**: Same user sees conversation history across browser sessions
- **User-Scoped**: Each user has isolated memory (based on `actor_id`)

**Memory Settings**:
- `MEMORY_LONG_TERM_ENABLED`: Enable/disable (default: false)
- `MEMORY_SESSION_TTL_HOURS`: Session retention (default: 24 hours)
- `MEMORY_USER_TTL_DAYS`: User retention (default: 30 days)

---

## Observability

### CloudWatch Logs

```bash
# Main runtime
aws logs tail /aws/bedrock-agentcore/runtimes/gameagentruntime-<ID>-DEFAULT --follow

# Frontend
aws logs tail /ecs/game-agent-frontend --follow
```

### X-Ray Tracing

- **Dashboard**: AWS Console > CloudWatch > Gen AI Observability > AgentCore
- **Transaction Search**: Query by user ID, session ID, or time range
- **Trace Details**: Full request flow including MCP calls

---

## Troubleshooting

### Runtime Not Responding

```bash
# Check status
aws bedrock-agentcore get-agent-runtime --agent-runtime-id <ID> --region us-west-2

# Check logs
aws logs tail /aws/bedrock-agentcore/runtimes/gameagentruntime-<ID>-DEFAULT --since 10m
```

### Frontend 404 Errors

If "Local AgentCore responded with status: 404":

1. Verify `AGENTCORE_RUNTIME_ID` is set in the ECS task definition
2. Check runtime ARN is correct
3. Ensure ECS task role has `bedrock-agentcore:InvokeAgentRuntime`

### Throttling

`throttlingException` from Bedrock:
- Wait 30-60 seconds and retry
- Request quota increase if frequent

### Memory Errors

```bash
# Check memory provisioned
yq eval '.agents.gameagentruntime.memory.memory_id' backend/.bedrock_agentcore.yaml
```

---

## Development Guide

### Required Tools

**UV - Python Package Manager**
```bash
# Install
curl -LsSf https://astral.sh/uv/install.sh | sh

# Usage
cd backend && uv add package-name
cd backend && uv sync
cd backend && uv export --format requirements-txt --output-file requirements.txt
```

**yq - YAML Processor**
```bash
# macOS
brew install yq

# Usage
yq eval '.agents.gameagentruntime.bedrock_agentcore.agent_arn' .bedrock_agentcore.yaml
```

### Script Organization

```
scripts/
├── deploy.sh           # Main deployment logic
├── teardown.sh         # Main teardown logic
├── dev/                # Development environment
├── test/               # Testing suite
└── infrastructure/     # Infrastructure utilities
```

**Wrapper scripts** (root directory):
- `./deploy-all.sh` - Deploy to AWS
- `./teardown-all.sh` - Tear down AWS resources
- `./dev-start.sh` - Start local development
- `./dev-stop.sh` - Stop local development
- `./test-full.sh` - Run all tests
- `./test-local.sh` - Fast localhost tests
- `./test-cloud.sh` - Cloud-only tests

### Best Practices

**Development**:
- Use local runtime for fast iteration
- Test with production runtime before deploying
- Check logs frequently
- Use X-Ray traces for debugging

**Production**:
- Monitor error rates and latency
- Set up CloudWatch alarms
- Use Bedrock caching for common prompts
- Keep runtime code lightweight

**Cost Optimization**:
- Use Haiku for simple queries, Sonnet for complex
- Enable Bedrock prompt caching
- Monitor token usage
- Track spending with Cost Explorer

---

*Note: All communication uses HTTPS/TLS. See the [architecture diagram](../diagrams/MultiAgent_Architecture.jpg) for visual representation.*

---

## Related Documentation

- [Deployment Guide](DEPLOYMENT_GUIDE.md) — Full deployment steps and environment variable reference
- [Security](../SECURITY.md) — Encryption, access controls, and compliance
- [Architecture Decision Records](adr/README.md) — Accepted boundaries for the optional operations control plane
