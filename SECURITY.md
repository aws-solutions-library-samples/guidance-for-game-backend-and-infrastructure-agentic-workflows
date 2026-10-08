# Security Documentation

This document provides comprehensive security information for Game Agent, including encryption, data protection, access controls, and compliance measures.

## Reporting Security Issues

If you discover a potential security issue in this project, report it to AWS/Amazon Security through the [AWS vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/). Do not create a public GitHub issue or pull request for suspected vulnerabilities.

Include reproduction steps, affected versions or deployment details, and potential impact when possible.

## Table of Contents

- [Reporting Security Issues](#reporting-security-issues)
- [Data Encryption](#data-encryption)
- [Transport Layer Security (TLS)](#transport-layer-security-tls)
- [Key Management](#key-management)
- [Timeout Configurations](#timeout-configurations)
- [Data Retention Policies](#data-retention-policies)
- [Conversation Memory Security](#conversation-memory-security)
- [Vulnerability Management](#vulnerability-management)
- [Software Bill of Materials (SBOM)](#software-bill-of-materials-sbom)
- [Dependency Management](#dependency-management)
- [Trust Boundaries & Data Flow](#trust-boundaries--data-flow)
- [Patching Strategy](#patching-strategy)
- [Security Scanning Posture](#security-scanning-posture)

---

## Data Encryption

### Encryption at Rest

All data stored in Game Agent is encrypted at rest using AWS-managed encryption:

**S3 Buckets:**
- **Knowledge Base Storage**: AES-256 server-side encryption (SSE-S3)
- **CloudTrail Logs**: AES-256 server-side encryption (SSE-S3)
- **Application Assets**: AES-256 server-side encryption (SSE-S3)

**Configuration Location**: `infrastructure/cloudformation/*.yaml`

**AWS Documentation**:
- [S3 Server-Side Encryption](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingServerSideEncryption.html)
- [S3 Encryption Best Practices](https://docs.aws.amazon.com/AmazonS3/latest/userguide/security-best-practices.html)

### Encryption in Transit

All data in transit is encrypted using TLS:

**ECS Express / ALB (Frontend):**
- Enforces TLS 1.2+ for all HTTPS connections via ALB
- AWS-managed certificates with automatic rotation
- No support for legacy protocols (SSLv3, TLS 1.0, TLS 1.1)

**API Gateway / AgentCore:**
- TLS 1.2+ enforced by AWS Bedrock AgentCore
- All hops use HTTPS. The frontend→AgentCore invocation authenticates with the
  end user's Cognito access token as an `Authorization: Bearer` header (verified
  independently by the runtime), not SigV4. AgentCore→AWS service calls use AWS
  SigV4 under the AgentCore execution role.

**AWS Documentation**:
- [ECS Security Best Practices](https://docs.aws.amazon.com/AmazonECS/latest/bestpracticesguide/security.html)
- [Bedrock Security Best Practices](https://docs.aws.amazon.com/bedrock/latest/userguide/security-best-practices.html)

---

## Transport Layer Security (TLS)

### TLS Version Requirements

**Minimum Version**: TLS 1.2  
**Recommended Version**: TLS 1.3 (where supported)

**Enforcement**:
- ECS Express / ALB: TLS 1.2+ enforced by default (AWS managed)
- AWS Bedrock AgentCore: TLS 1.2+ enforced by default (AWS managed)
- CloudFront (if used): TLS 1.2+ configurable in security policy

**Certificate Management**:
- AWS-managed certificates with automatic rotation
- No manual certificate management required

**AWS Documentation**:
- [ECS Data Protection](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/data-protection.html)
- [AWS Certificate Manager](https://docs.aws.amazon.com/acm/latest/userguide/acm-overview.html)

---

## Key Management

### AWS-Managed Keys

Game Agent uses AWS-managed encryption keys (SSE-S3) for all data at rest. This is appropriate for a reference architecture and provides:

**Automatic Key Rotation**:
- AWS automatically rotates SSE-S3 keys
- No manual intervention required
- Rotation occurs transparently without service interruption

**Key Security**:
- Keys are managed by AWS and never exposed
- FIPS 140-2 validated cryptographic modules
- Keys are unique per object

**AWS Documentation**:
- [S3 Encryption with AWS-Managed Keys](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingServerSideEncryption.html)
- [AWS Key Management Best Practices](https://docs.aws.amazon.com/kms/latest/developerguide/best-practices.html)

### Customer-Managed Keys (Optional)

For production deployments requiring additional control, consider migrating to AWS KMS Customer Managed Keys (CMKs):

**Benefits**:
- Custom key rotation policies (recommended: 365 days)
- Granular access control via IAM policies
- CloudTrail logging of all key usage
- Cross-account key sharing capabilities

**Implementation**: Update CloudFormation templates to use `AWS::KMS::Key` resources with `EnableKeyRotation: true`

---

## Timeout Configurations

### Agent Execution Timeouts (Wall-Clock)

Wall-clock timeouts enforce a hard ceiling on elapsed time for agent execution, catching
hung Bedrock calls, stuck MCP servers, or slow multi-step reasoning that turn limits alone
cannot stop.

**Configuration Location**: `backend/src/config/settings.py`

```python
AGENT_TIMEOUT_REQUEST_SECONDS = 180       # 3 min - hard ceiling on entire request
AGENT_TIMEOUT_ORCHESTRATOR_SECONDS = 150  # 2.5 min - orchestrator agent loop
AGENT_TIMEOUT_SPECIALIST_SECONDS = 90     # 1.5 min - per-specialist agent loop
```

**Implementation**:
- `invoke_agent()`: Wraps the orchestrator call in `concurrent.futures` with `AGENT_TIMEOUT_REQUEST_SECONDS`. Returns a graceful timeout message to the user.
- `WallClockTimeoutHook` (`utils/wall_clock_timeout_hook.py`): Strands `BeforeToolCallEvent` hook that checks elapsed `time.monotonic()` before each tool call. Cancels further tool execution when timeout is exceeded.
- Applied to both orchestrator and specialist agents alongside `MaxTurnsHook`.

**Override via environment variables**: `AGENT_TIMEOUT_REQUEST_SECONDS`, `AGENT_TIMEOUT_ORCHESTRATOR_SECONDS`, `AGENT_TIMEOUT_SPECIALIST_SECONDS`

### Agent Turn Limits

Turn limits cap the number of reasoning/tool-call cycles per agent invocation:

**Configuration Location**: `backend/src/config/settings.py`

```python
AGENT_MAX_TURNS_ORCHESTRATOR = 15  # Max tool-call cycles for orchestrator
AGENT_MAX_TURNS_SPECIALIST = 10    # Max tool-call cycles per specialist
```

**Implementation**: `MaxTurnsHook` (`utils/max_turns_hook.py`) — cancels tool calls after limit.

### Bedrock API Timeouts

Strands `BedrockModel` sets a `read_timeout` of 120 seconds on the underlying boto3 client.
This prevents individual `InvokeModelWithResponseStream` calls from hanging indefinitely.

**Default**: 120 seconds (set by Strands SDK)
**Override**: Pass `boto_client_config=BotocoreConfig(read_timeout=N)` to `BedrockModel`

### Cognito Session Timeouts

**Access Token Lifetime**: 60 minutes (default)  
**Refresh Token Lifetime**: 30 days (default)  
**ID Token Lifetime**: 60 minutes (default)

**Configuration Location**: `infrastructure/cloudformation/01-base-infrastructure.yaml`

```yaml
AccessTokenValidity: 60
RefreshTokenValidity: 30
IdTokenValidity: 60
```

**AWS Documentation**:
- [Cognito Token Lifetimes](https://docs.aws.amazon.com/cognito/latest/developerguide/amazon-cognito-user-pools-using-tokens-with-identity-providers.html)

### ECS Express Health Check Timeouts

Health check path `/api/health` is configured in the template; interval, timeout, and thresholds use ECS Express managed defaults.

**Configuration Location**: `infrastructure/cloudformation/02-frontend-ecs-express.yaml`

**AWS Documentation**:
- [ECS Health Checks](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/healthcheck.html)

---

## Data Retention Policies

### CloudWatch Logs

**Application Logs**: 14 days  
**Audit Logs (CloudTrail)**: 90 days (configurable)

**Configuration Location**:
- Application: `infrastructure/cloudformation/03-agentcore-observability.yaml`
- Audit: `infrastructure/cloudformation/05-security-infrastructure.yaml`

**Rationale**:
- Application logs: Short retention for debugging and troubleshooting
- Audit logs: Extended retention for compliance and security investigations

**AWS Documentation**:
- [CloudWatch Logs Retention](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/Working-with-log-groups-and-streams.html)

### S3 Lifecycle Policies

**CloudTrail Logs**: 90-day expiration policy  
**Knowledge Base Documents**: No expiration (persistent)

**Configuration Location**: `infrastructure/cloudformation/05-security-infrastructure.yaml`

```yaml
LifecycleConfiguration:
  Rules:
    - Id: ExpireOldLogs
      Status: Enabled
      ExpirationInDays: 90
```

**AWS Documentation**:
- [S3 Lifecycle Configuration](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lifecycle-mgmt.html)

---

## Conversation Memory Security

### AgentCore Memory Encryption

**Encryption at Rest**: Enabled by default (AWS-managed)  
**Encryption in Transit**: TLS 1.2+ for all API calls

**Memory Types**:
1. **Event Memory**: Conversation history (encrypted at rest)
2. **Semantic Memory**: Long-term knowledge storage (encrypted at rest)

**Configuration Location**: `backend/src/config/settings.py`

```python
EVENT_MEMORY_STRATEGY = "BEDROCK_AGENTCORE"
SEMANTIC_MEMORY_STRATEGY = "BEDROCK_AGENTCORE"
```

**Data Isolation**: Memory is isolated per user (actor_id) with no cross-user access.

**AWS Documentation**:
- [Bedrock AgentCore Memory](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-memory.html)
- [Bedrock Data Protection](https://docs.aws.amazon.com/bedrock/latest/userguide/data-protection.html)

---

## Vulnerability Management

### Known Advisories & Risk Acceptance

We remediate dependency advisories by upgrading to patched versions rather than
suppressing scanners. Where an advisory cannot be patched, it is documented here
with its rationale rather than ignored.

| Advisory | Package | Status | Rationale |
|----------|---------|--------|-----------|
| [GHSA-9h52-p55h-vw2f](https://github.com/advisories/GHSA-9h52-p55h-vw2f) — MCP Python SDK DNS rebinding protection not enabled by default (high) | `mcp` (`>=1.23.0`) | **Resolved (patched)** | Upgraded `mcp` to ≥1.23.0 (patched line). Previously blocked by a `<1.19.0` pin for an `eks-mcp-server` incompatibility (awslabs/mcp#1577); that is fixed upstream — `eks-mcp-server>=0.1.32` itself now requires `mcp[cli]>=1.23.0` — so the pin was lifted. (Only stdio transport is used here, so the DNS-rebinding vector did not apply regardless.) |

### AWS Inspector

AWS Inspector is enabled by default (`EnableInspector: true`) for continuous vulnerability scanning:

**Scan Targets**:
- ECR container images (enhanced scanning)
- Lambda functions (if deployed)
- EC2 instances (if used)

**Scan Frequency**: Continuous (on image push to ECR)

**Configuration Location**: `infrastructure/cloudformation/05-security-infrastructure.yaml`

**Findings**: Available in AWS Security Hub and Inspector console

**AWS Documentation**:
- [Amazon Inspector](https://docs.aws.amazon.com/inspector/latest/user/what-is-inspector.html)
- [ECR Image Scanning](https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-scanning.html)

### ECR Enhanced Scanning

**Scan Type**: Inspector-based enhanced scanning  
**CVE Database**: Continuously updated by AWS  
**Scan Triggers**: Automatic on image push

**Configuration Location**: `infrastructure/cloudformation/05-security-infrastructure.yaml`

### Local Vulnerability Scanning with Grype

On-demand dependency vulnerability scanning using [Grype](https://github.com/anchore/grype) against SBOMs:

```bash
# Run full scan (generates SBOMs if needed, fails on medium+ severity)
./scripts/scan-vulnerabilities.sh

# Override severity threshold
FAIL_ON=high ./scripts/scan-vulnerabilities.sh
```

**Install Grype**: `brew install grype` (macOS) or see [Grype installation](https://github.com/anchore/grype#installation)

---

## Software Bill of Materials (SBOM)

### What is an SBOM?

A Software Bill of Materials (SBOM) is a comprehensive inventory of all software components, libraries, and dependencies used in an application. Think of it as an "ingredient list" for your software.

**Purpose**:
- Identify vulnerable dependencies quickly
- Meet compliance requirements (government, enterprise)
- Supply chain security and transparency
- License compliance tracking

### SBOM Generation with Syft

Game Agent uses [Syft](https://github.com/anchore/syft) (free, open-source) to generate SBOMs as part of the deployment pipeline.

**Format**: SPDX-JSON (industry standard)
**Location**: `sbom/` directory (gitignored, generated at deploy time)
**Generation**: Automatic during `deploy.sh` (Step 6b), or on-demand via `scripts/generate-sbom.sh`

**What gets scanned**:
- **Backend**: Source directory (`backend/`) — scans `pyproject.toml`, `requirements.txt`, `uv.lock`
- **Frontend**: Docker image after build — scans all OS and npm packages in the final image

**Configuration Location**: `scripts/generate-sbom.sh`

**Install Syft**:
```bash
# macOS
brew install syft

# Linux
curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s -- -b /usr/local/bin
```

### Using the SBOM

**Generate on-demand**:
```bash
# From source (no Docker image needed)
./scripts/generate-sbom.sh

# From a specific frontend Docker image
./scripts/generate-sbom.sh <ecr-repo>:latest
```

**Scan for vulnerabilities**:
```bash
# Using Grype (Anchore's vulnerability scanner)
grype sbom:sbom/backend-sbom-latest.spdx.json
grype sbom:sbom/frontend-sbom-latest.spdx.json
```

**SBOM Standards**:
- **SPDX**: Software Package Data Exchange (Linux Foundation)
- **CycloneDX**: OWASP security-focused format

**External Resources**:
- [NTIA SBOM Minimum Elements](https://www.ntia.gov/report/2021/minimum-elements-software-bill-materials-sbom)
- [Syft Documentation](https://github.com/anchore/syft)

---

## Dependency Management

### Automated Dependency Updates

Game Agent uses GitHub Dependabot for automated dependency updates:

**Configuration Location**: `.github/dependabot.yml`

**Update Schedule**: Monthly  
**CVE Alerts**: Enabled (immediate notifications)

**Monitored Files**:
- `backend/pyproject.toml` (Python dependencies)
- `ui/package.json` (npm dependencies)

**Process**:
1. Dependabot checks for updates monthly
2. Creates pull requests for outdated dependencies
3. Includes CVE information if security-related
4. Automated tests run on PRs before merge

### Manual Dependency Review

**Python (UV)**:
```bash
cd backend
uv sync --upgrade  # Update dependencies
uv export --format requirements-txt --output-file requirements.txt
```

**Node.js (npm)**:
```bash
cd ui
npm outdated  # Check for updates
npm update    # Update dependencies
```

### Version Pinning Strategy

**Critical Dependencies**: Pinned to specific versions with documented reasons  
**Example**: `awslabs.eks-mcp-server>=0.1.32` (requires `mcp>=1.23.0`; the former `mcp<1.19.0` pin was lifted once awslabs/mcp#1577 was fixed upstream)

**Documentation Location**: `backend/pyproject.toml` (inline comments)

**GitHub Documentation**:
- [Dependabot Configuration](https://docs.github.com/en/code-security/dependabot/dependabot-version-updates/configuration-options-for-the-dependabot.yml-file)

---

## Trust Boundaries & Data Flow

### Request Flow

Game Agent has four trust boundaries. Every hop uses a distinct authentication mechanism with no static credentials anywhere in the chain.

```
                        TRUST BOUNDARY 1               TRUST BOUNDARY 2
                     (Internet → Frontend)         (Frontend → Backend)
                              │                             │
  ┌──────────┐    HTTPS/TLS   │   ┌──────────────┐  Bearer  │   ┌──────────────────┐
  │  User    │───────────────►│──►│  ECS Express  │─────────►│──►│  Bedrock         │
  │  Browser │  JWT in cookie  │   │  (Frontend)   │  (JWT)   │   │  AgentCore       │
  └──────────┘                 │   └──────────────┘          │   │  Runtime         │
                               │                             │   └────────┬─────────┘
                               │                             │            │
                               │                             │   TRUST BOUNDARY 3
                               │                             │  (Backend → AWS APIs)
                               │                             │            │
                               │                             │     IAM Role
                               │                             │     (SigV4)
                               │                             │            │
                               │                             │   ┌────────▼─────────┐
                               │                             │   │  AWS Services     │
                               │                             │   │  (read-only)      │
                               │                             │   │  GameLift, EKS,   │
                               │                             │   │  Cost Explorer,   │
                               │                             │   │  Bedrock KBs      │
                               │                             │   └──────────────────┘
```

### Authentication at Each Boundary

| Boundary | From → To | Mechanism | Details |
|----------|-----------|-----------|---------|
| **1** | User → ECS Express (ALB) | **Cognito JWT** | HttpOnly/Secure/SameSite cookies; `CognitoJwtVerifier` validates signature, expiration, and audience. Users must be in `admin` or `users` group. |
| **2** | ECS Express → AgentCore | **Cognito JWT (bearer)** | The proxy forwards the end user's verified Cognito **access token** as an `Authorization: Bearer` header over HTTPS (TLS 1.2+). The AgentCore runtime independently verifies the token and reconstructs authority. No SigV4 and no task-role signing — the bearer token is the credential, so the ECS task role needs no `bedrock-agentcore` grant and has no identity policies. |
| **3** | AgentCore → AWS Services | **IAM Role** | AgentCore execution role assumed by `bedrock-agentcore.amazonaws.com` with an `aws:SourceAccount` condition. Rebuilt from reviewed callers (#482): no AWS managed read policies; scoped Bedrock, AgentCore memory, Cost Explorer / Cost Optimization Hub / Compute Optimizer, GameLift, EKS, EC2, ECR, logs, and X-Ray reads. See **AgentCore Execution Role (Least Privilege)** below. |
| **4** | Prompts → Model | **Bedrock Guardrails** | Input/output content filtering: topic blocking, PII anonymization, prompt injection detection, profanity filtering. |

**Configuration Locations**:
- Cognito: `infrastructure/cloudformation/01-base-infrastructure.yaml` (lines 12-73)
- ECS task role: `infrastructure/cloudformation/01-base-infrastructure.yaml`
- AgentCore execution role: `infrastructure/cloudformation/01-base-infrastructure.yaml` (`AgentCoreExecutionRole`)
- Guardrails: `infrastructure/cloudformation/04-bedrock-guardrails.yaml`

### Data Classification by Stage

| Stage | Data | Classification | Protection |
|-------|------|----------------|------------|
| User input (browser) | Raw prompts | **Untrusted** | TLS 1.2+ in transit |
| Frontend (ECS Express) | JWT claims, validated prompt | **Authenticated** | HttpOnly cookies, Cognito verification |
| Backend (AgentCore) | Sanitized prompt, user context | **Validated** | Input validation, rate limiting, guardrails |
| AWS service responses | Fleet/cluster info, costs | **Internal** | IAM-scoped read-only access |
| Logs (CloudWatch) | Sanitized excerpts | **Redacted** | PII/credentials stripped by `sanitize_log_data()`, encrypted at rest |
| Memory (AgentCore) | Conversation history, user facts | **Personal** | Per-user isolation (`actor_id`), encrypted at rest, TTL-enforced |

### Service-to-Service Credentials

No static credentials (access keys, passwords, tokens) exist in the codebase or deployment configuration. Service-to-service authentication uses IAM roles with automatic credential rotation, except the frontend→AgentCore hop, which carries the end user's short-lived Cognito access token:

- **ECS Express → AgentCore**: the user's verified Cognito access token as a bearer token over HTTPS (no SigV4, no task-role grant). The ECS task role (`ecs-tasks.amazonaws.com`, `aws:SourceAccount` condition) has no identity-based permissions; it exists only so ECS can vend task credentials to the container. The supported chat path's one AWS call, `sts:GetCallerIdentity` (to build the runtime ARN), requires no IAM allow, so no policy is attached. Dormant Cognito Admin/List routes remain intentionally unprivileged pending removal in #473.
- **AgentCore → AWS Services**: AgentCore execution role (`bedrock-agentcore.amazonaws.com`) with `aws:SourceAccount` condition
- **MCP Servers**: Run as subprocesses via stdio transport (no network access), inherit the AgentCore execution role credentials

### AgentCore Execution Role (Least Privilege)

Issue #482 rebuilt `AgentCoreExecutionRole` from its reviewed callers upward. It
attaches **no AWS managed read policies** (the former
`AWSBillingReadOnlyAccess`, `AWSCloudFormationReadOnlyAccess`,
`CloudWatchReadOnlyAccess`, `CloudWatchLogsReadOnlyAccess`, and
`CostOptimizationHubReadOnlyAccess` grants allowed reading any log group, stack
template/parameter, and metric in the account). Every statement below names a
concrete caller that worked on the prior role, so the rebuild **adds no net-new
provider capability** — it only removes, scopes, or makes explicit what the
managed policies previously supplied implicitly. No runtime path reads log
**content** or deletes memory **records**; the only provider mutations are the
scoped memory/event writes (including session-event `DeleteEvent` within this
project's own `gameagent*` memory), telemetry writes, and the cost-snapshot
`PutItem`.

| Statement | Actions | Caller | Resource scope |
|-----------|---------|--------|----------------|
| BedrockModelInvocation | `bedrock:InvokeModel`, `InvokeModelWithResponseStream` | Strands Bedrock model | foundation-model + account inference-profile ARNs |
| BedrockKnowledgeBaseAccess | `bedrock:Retrieve` | Knowledge Base RAG (`kb_tools.py`) | `knowledge-base/*` |
| BedrockGuardrailAccess | `bedrock:ApplyGuardrail` | Guardrails on model invocation | `guardrail/*` |
| BedrockPromptManagementAccess | `bedrock:GetPrompt` | Managed prompts (`optimized_prompts.py`) | `prompt/*` |
| AgentCoreMemoryAccess | `bedrock-agentcore:CreateEvent`, `GetEvent`, `ListEvents`, `DeleteEvent`, `RetrieveMemoryRecords`, `BatchCreateMemoryRecords` | Strands session manager (STM events) + `semantic_memory.py` (LTM write/retrieve) | `memory/gameagent*` |
| CostExplorerReadAccess | `ce:GetCostAndUsage`, `GetCostForecast`, `aws-portal:ViewBilling` | Owned deterministic report + Billing MCP cost forecast | `*` (global endpoint) |
| CostOptimizationHubReadAccess | `cost-optimization-hub:ListRecommendationSummaries`, `ListRecommendations`, `GetRecommendation` | Billing MCP cost-optimization | `*` (global; no resource type) |
| ComputeOptimizerReadAccess | `compute-optimizer:GetEC2InstanceRecommendations`, `GetAutoScalingGroupRecommendations` | Billing MCP compute-optimizer (EC2 + Auto Scaling rightsizing) | `*` + `aws:RequestedRegion` (regional service, no resource type) |
| AutoScalingReadAccess | `autoscaling:DescribeAutoScalingGroups`, `DescribeAutoScalingInstances` | Compute Optimizer Auto Scaling dependent reads | `*` + `aws:RequestedRegion` (no resource type) |
| GameLiftReadAccess | 10 reviewed `gamelift:List*/Describe*` ops | GameLift specialist | `*` + `aws:RequestedRegion` (no fleet-ARN scoping for collection/capacity reads) |
| EKSReadAccess | `eks:ListClusters`, `DescribeCluster`, `List/DescribeNodegroup(s)`, `List/DescribeFargateProfile(s)`, `List/DescribeAddon(s)` | aws-api-mcp `call_aws` + eks-mcp | `*` + `aws:RequestedRegion` (dynamic cluster names) |
| EC2ReadAccess | `ec2:DescribeInstances` | Compute Optimizer EC2 dependent read | `*` + `aws:RequestedRegion` (EC2 describes have no resource type) |
| ECRImageAccess | `ecr:BatchGetImage`, `GetDownloadUrlForLayer`, `BatchCheckLayerAvailability` | container-mode image pull | `repository/bedrock-agentcore-gameagentruntime` |
| ECRTokenAccess | `ecr:GetAuthorizationToken` | container-mode registry auth | `*` + `aws:RequestedRegion` (no resource type) |
| ObservabilityAccess | `logs:CreateLogGroup/CreateLogStream/PutLogEvents`, `DescribeLogStreams` (this runtime's `runtimes/gameagentruntime-*` group only), `DescribeLogGroups` (metadata), `cloudwatch:GetMetricData`, `xray:PutTraceSegments/PutTelemetryRecords/GetSamplingRules/GetSamplingTargets` | Runtime logging, eks-mcp metrics, ADOT/X-Ray | runtime log groups; `DescribeLogStreams` scoped to `runtimes/gameagentruntime-*`; `log-group:*` for `DescribeLogGroups` metadata only; `*` + `aws:RequestedRegion` for GetMetricData; `*` for X-Ray |
| CostReportSnapshotStoreAccess | `dynamodb:GetItem`, `PutItem` | Shared cost-report snapshot store (#365) | snapshot table ARN |

**Justification for remaining account-scoped (`Resource: '*'`) reads.** Two
distinct reasons apply, and the role never mixes them silently:

*Actions with no resource-level scoping in the AWS Service Authorization
Reference, or that address a global endpoint:*

- **Cost Explorer** (`ce:GetCostAndUsage`, `ce:GetCostForecast`, with their
  dependent `aws-portal:ViewBilling`) and **Cost Optimization Hub** — global
  us-east-1 endpoints; no `aws:RequestedRegion` condition is applied and the SAR
  exposes no per-report resource type. `aws-portal:ViewBilling` is listed by the
  SAR operation table as a dependent action of every Cost Explorer read; it was
  previously supplied only by the now-removed `AWSBillingReadOnlyAccess` managed
  policy.
- **`cloudwatch:GetMetricData`**, **`ecr:GetAuthorizationToken`**, and **X-Ray
  sampling/put** — no scopable resource type. GetMetricData and the ECR token
  are additionally constrained with an `aws:RequestedRegion` condition (their
  only callers run in the deployment region); the ECR token is implicitly scoped
  to the caller's own registry.
- **`logs:DescribeLogGroups` on `log-group:*`** — returns only group metadata
  (names/ARNs), never log content; required by the AgentCore toolkit for group
  discovery and is the only log read on the account-wide resource.
- **EC2** `DescribeInstances` — no resource-type support; constrained by an
  `aws:RequestedRegion` condition.
- **Compute Optimizer** `GetEC2InstanceRecommendations` /
  `GetAutoScalingGroupRecommendations` and the **Auto Scaling** describes
  (`DescribeAutoScalingGroups`, `DescribeAutoScalingInstances`) — no resource
  types; constrained by an `aws:RequestedRegion` condition.

*Actions that DO support ARNs but use `*` because the resource names are dynamic
and account-owned, constrained instead by an `aws:RequestedRegion` condition to
the deployment region:*

- **GameLift** `DescribeContainerFleet`, `DescribeContainerGroupDefinition`, and
  `DescribeScalingPolicies` support ARNs, but fleet/definition names are created
  at runtime; the collection and capacity reads (`List*`, `DescribeFleetCapacity`,
  `DescribeFleetUtilization`) have no ARN scoping at all.
- **EKS** per-resource reads support an ARN of the matching resource type
  (`DescribeCluster` a `cluster*` ARN; `DescribeNodegroup`, `DescribeFargateProfile`,
  and `DescribeAddon` their nodegroup, fargateprofile, and addon ARNs), but those
  names are dynamic. Among the `List*` reads, only `ListClusters` has no resource
  type; `ListNodegroups`, `ListFargateProfiles`, and `ListAddons` take a `cluster`
  ARN but use `*` because cluster names are created at runtime, constrained
  instead by the `aws:RequestedRegion` condition.

> **Region narrowing (behavior change from the prior role).** On the prior role
> the Compute Optimizer and Auto Scaling grants carried no region condition (the
> latter came unconditioned from `CloudWatchReadOnlyAccess`), so a model-supplied
> `region` could run rightsizing in another region. The reviewed role constrains
> both to the deployment region, matching the single-region posture of the
> GameLift and EKS reads. Cross-region rightsizing is no longer available.

**Not granted (pre-existing gaps).** These actions were either never granted on
the prior role, or had a reviewed caller that never actually worked on it, so
adding them now would expand provider capability and is out of scope for the
least-privilege rebuild. Each is tracked as a follow-up, with its runtime
impact:

- `ce:GetUsageForecast` — the Billing MCP cost-explorer **usage** forecast.
  `AWSBillingReadOnlyAccess` carried no forecast action, so usage forecasts never
  worked on the prior role; only cost forecasts (`ce:GetCostForecast`) are
  granted. Impact: the operation is removed from the model-visible catalog by the
  Billing MCP guard, and is denied if called.
- EBS and Lambda rightsizing — the Billing MCP compute-optimizer
  `GetEBSVolumeRecommendations` / `GetLambdaFunctionRecommendations` operations
  and their SAR-dependent reads (`ec2:DescribeVolumes`, `lambda:ListFunctions`,
  `lambda:ListProvisionedConcurrencyConfigs`) never worked. Impact: these two
  rightsizing categories are off the reviewed surface and are removed from the
  model-visible catalog by the Billing MCP guard.
- `eks:ListInsights` / `eks:DescribeInsight` (the EKS insights tool). Impact: an
  insights request fails with access denied.
- `eks-mcpserver:QueryKnowledgeBase` (the EKS troubleshooting-guide search tool).
  Impact: the troubleshooting-guide search returns nothing / fails.
- `get_eks_vpc_config` (the eks-mcp VPC tool) is **wholly non-functional**: it
  reads VPCs, subnets, **and** route tables in one error path, and
  `ec2:DescribeRouteTables` was never granted, so the whole tool always failed.
  Its `ec2:DescribeVpcs` / `ec2:DescribeSubnets` reads therefore had no working
  caller and are not granted; restoring the tool needs all three EC2 reads (or a
  decision to remove the tool). Impact: any VPC-config request fails.
- `cloudwatch:PutMetricData` (namespace `bedrock-agentcore`) and
  `logs:PutResourcePolicy` — listed by the AgentCore direct-deploy execution-role
  documentation but granted by neither the prior nor the reviewed role. Impact:
  if the platform ever emits these, they would be denied; no reviewed caller
  needs them today.
- `compute-optimizer:GetEnrollmentStatus` — the Billing MCP compute-optimizer
  tool probes it before each operation; neither the prior nor the reviewed role
  grants it. Impact: the probe is denied and the tool logs the denial and
  continues (a tolerated, non-fatal denial).

**Container-fallback ECR grant.** The deployed runtime uses AgentCore
`direct_code_deploy` and pulls no image, but the toolkit prints a warning and
continues with container deployment when `zip` is unavailable (e.g. the
native-Windows PowerShell path). The scoped ECR pull/auth grant is retained so
that fallback keeps working; making the Windows path force `direct_code_deploy`
would let the grant be dropped later.

---

## Patching Strategy

### Infrastructure Model

Game Agent uses a **fully serverless architecture** with no self-managed EC2 instances or EKS nodes. All compute infrastructure is AWS-managed:

| Component | AWS Service | OS Patching |
|-----------|-------------|-------------|
| Frontend | ECS Express (Fargate + ALB) | AWS-managed (automatic) |
| Backend | Bedrock AgentCore Runtime | AWS-managed (automatic) |
| Authentication | Cognito | AWS-managed (automatic) |
| Storage | S3 | AWS-managed (automatic) |
| Logging | CloudWatch, CloudTrail | AWS-managed (automatic) |
| AI Models | Bedrock | AWS-managed (automatic) |
| Knowledge Bases | Bedrock Knowledge Bases | AWS-managed (automatic) |

**Implication**: There is no OS-level patching responsibility for the deploying team. AWS applies security patches to the underlying infrastructure transparently.

### Container Base Images

The two container workloads use the following base images:

| Container | Base Image | Pinning |
|-----------|------------|---------|
| Frontend | `node:18-alpine` | Tag-based (mutable) |
| Backend | `ghcr.io/astral-sh/uv:python3.13-bookworm-slim` | Tag-based (mutable) |

**Update strategy**: Base image tags are mutable, so each redeployment (`deploy.sh`) pulls the latest patched version of the tag. Dependabot monitors base images and opens PRs when updates are available.

**Configuration Locations**:
- Frontend Dockerfile: `ui/Dockerfile`
- Backend Dockerfile: `backend/.bedrock_agentcore/gameagentruntime/Dockerfile` (auto-generated by AgentCore SDK)

### Application Dependencies

| Ecosystem | Tool | Schedule | Configuration |
|-----------|------|----------|---------------|
| Python | Dependabot | Monthly | `.github/dependabot.yml` |
| npm | Dependabot | Monthly | `.github/dependabot.yml` |
| Docker base images | Dependabot | Monthly | `.github/dependabot.yml` |
| GitHub Actions | Dependabot | Monthly | `.github/dependabot.yml` |

Three Python dependencies are pinned to specific versions due to known incompatibilities and are excluded from automatic updates. See `backend/pyproject.toml` for details and rationale.

### Vulnerability Detection

| Layer | Tool | Trigger |
|-------|------|---------|
| Container images | AWS Inspector (ECR enhanced scanning) | Automatic on `docker push` |
| Application dependencies | Dependabot security alerts | Automatic (immediate) |
| On-demand scanning | Grype + Syft SBOMs | Manual (`scripts/scan-vulnerabilities.sh`) |

---

## Security Scanning Posture

### Current State

Game Agent does not yet have a CI/CD pipeline. All builds run on developer machines and deploy directly to AWS accounts. Security scanning is available through the following mechanisms:

| Scanning Type | Tool | Integration | Status |
|---------------|------|-------------|--------|
| **Dependency vulnerabilities** | Dependabot | GitHub (automatic PRs) | Active |
| **Container image vulnerabilities** | AWS Inspector | ECR (scan on push) | Active |
| **SBOM generation** | Syft | On-demand / deploy-time | Available (optional install) |
| **Dependency CVE scanning** | Grype | On-demand | Available (optional install) |
| **Code formatting/linting** | Black, isort, mypy, ESLint | Pre-commit hooks | Active |
| **Secret detection** | pre-commit `detect-private-key` | Pre-commit hooks | Active |

### What Runs Automatically

- **Dependabot**: Monitors Python, npm, Docker, and GitHub Actions dependencies monthly. Opens PRs with CVE information for security-related updates. Immediate alerts for critical vulnerabilities.
- **AWS Inspector**: Continuously scans ECR images after each push. Findings available in the AWS Inspector and Security Hub consoles.
- **Pre-commit hooks**: Enforce code quality (formatting, linting, type checking) and detect committed private keys.

### What Requires Manual Installation

Syft and Grype are optional developer tools for SBOM generation and vulnerability scanning. They are not required for deployment. The deployment script skips SBOM generation if Syft is not installed.

**Install (all platforms)**:
```bash
# macOS
brew install syft grype

# Linux
curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s -- -b /usr/local/bin
curl -sSfL https://raw.githubusercontent.com/anchore/grype/main/install.sh | sh -s -- -b /usr/local/bin

# Windows
scoop install syft grype
```

### CI/CD Integration (Planned)

When a CI/CD pipeline is introduced, the existing scripts (`scripts/generate-sbom.sh`, `scripts/scan-vulnerabilities.sh`) are designed to be plugged into build steps. Planned additions:
- SBOM generation as a build artifact
- Grype vulnerability gate (fail on medium+ severity)
- SAST tooling (e.g., Bandit for Python, CodeQL)

---

## Security Best Practices

### Least Privilege Access

- IAM roles follow principle of least privilege
- AgentCore execution role has minimal required permissions
- Cognito users have role-based access control

### Network Security

- ECS Express deployed in AWS-managed VPC
- No public access to backend services (AgentCore)
- WAF rules protect against common attacks

### Network Binding Configuration

**Local development binds to loopback (`127.0.0.1`).**

Local development enables an identity-verification bypass
(`GBAW_ALLOW_LOCAL_IDENTITY_BYPASS=true`, set by `scripts/dev/start.sh`) so a
developer can exercise the runtime without Cognito. Because that backend is
unauthenticated, it must not be reachable from other machines: local execution
always binds to `127.0.0.1`. A machine that could reach a non-loopback bind
would be able to invoke agents using the developer's active AWS credentials.
There is no host or port override — the listen address is decided solely by
the runtime mode, and the port is the fixed AgentCore platform port `8080`.

The bind decision is made in one place — `resolve_runtime_host()` in
`backend/src/config/settings.py` — and both entrypoints
(`backend/src/agentcore_main.py` and the thin wrapper
`backend/agentcore_main.py`) call the shared `run_server()`, so they cannot
diverge.

**Hosted AgentCore Runtime binds to all interfaces (`0.0.0.0`) — intentionally.**

The hosted runtime selects the all-interface bind through an explicit
condition (`GBAW_HOSTED_RUNTIME=true`). Inside the AWS-managed AgentCore
container the port is **not published** to the host or the internet, so the
in-container `0.0.0.0` bind is not exposure — it lets the AWS-managed container
network route to the application. Narrowing it would break that routing. The
real boundary is the unpublished container network plus the AgentCore JWT
verifier (a Cognito access token checked per request), backed by:

- AWS-managed VPC with security groups
- ECS Express service-level access controls
- Amazon Cognito authentication (verified per request in application code)
- WAF rate limiting and attack protection

**Fail-closed invariant (defense-in-depth).** The all-interface bind is never
granted while the identity bypass is active. If `GBAW_HOSTED_RUNTIME=true` is
combined with `GBAW_ALLOW_LOCAL_IDENTITY_BYPASS=true`, `resolve_runtime_host()`
raises `RuntimeBindError` and the process refuses to start. This is a
config-drift guard rather than the sole barrier to exposure: `invoke_agent`
already disregards the bypass whenever `GBAW_HOSTED_RUNTIME=true` (it computes
`ALLOW_LOCAL_IDENTITY_BYPASS and not HOSTED_RUNTIME`), so the per-request
Cognito JWT verifier runs in hosted mode regardless of the bypass flag.
Refusing the contradictory combination at startup keeps a drifted configuration
from taking effect silently. The deployment selectors set the hosted flag and
never set the bypass, so hosted configuration cannot reach this combination.

| Mode | Selector | Bind | Boundary |
|------|----------|------|----------|
| Local development | default (bypass on) | `127.0.0.1` | Loopback only — not reachable off-host |
| Hosted AgentCore | `GBAW_HOSTED_RUNTIME=true`, bypass off | `0.0.0.0` | Unpublished container network + per-request JWT verifier |
| Hosted + bypass | contradictory (config drift) | refuses to start | Fail-closed guard (`RuntimeBindError`); hosted mode ignores the bypass anyway |

**Configuration Location**: `backend/src/config/settings.py` (`resolve_runtime_host`),
`backend/src/agentcore_main.py` (`run_server`), and `backend/agentcore_main.py`.

**Security Scanners**: Tools like Bandit (B104) and Semgrep may flag the
`0.0.0.0` constant used for the hosted container bind. In that hosted,
port-unpublished context it is a false positive; the local default is loopback.
The constant is annotated `# nosec B104` at its single definition with a
reference to `resolve_runtime_host`.

**AWS Documentation**:
- [Amazon ECS Security](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/security.html)
- [Amazon Bedrock AgentCore Runtime](https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore-runtime.html)

### Audit Logging

- CloudTrail logs all AWS API calls
- CloudWatch logs all application activity
- Logs retained per compliance requirements

### Secrets Management

- No hardcoded credentials in code
- Environment variables for configuration
- AWS Secrets Manager for sensitive data (if needed)

---

## Compliance Considerations

### Data Residency

- All data stored in configured AWS region
- No cross-region data transfer (unless explicitly configured)
- AgentCore Memory stays in deployment region

### GDPR / Privacy

- User data isolated per actor_id
- Memory can be cleared via API (`/api/memory/clear`)
- No PII stored in logs (filtered by guardrails)

### SOC 2 / ISO 27001

- AWS services are SOC 2 and ISO 27001 certified
- Encryption at rest and in transit
- Audit logging enabled
- Access controls enforced

**AWS Compliance Programs**:
- [AWS Compliance Programs](https://aws.amazon.com/compliance/programs/)

---

## Security Contacts

For security issues or questions:

1. **GitHub Security Advisories**: Report vulnerabilities via GitHub
2. **AWS Support**: For AWS service security questions
3. **Project Maintainers**: For architecture security questions

---

## Additional Resources

- [AWS Security Best Practices](https://aws.amazon.com/architecture/security-identity-compliance/)
- [OWASP Top 10](https://owasp.org/www-project-top-ten/)
- [NIST Cybersecurity Framework](https://www.nist.gov/cyberframework)
- [AWS Well-Architected Framework - Security Pillar](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/welcome.html)

---

## Related Documentation

- [Architecture](docs/ARCHITECTURE.md) — System architecture, agent flow, and diagrams
- [Deployment Guide](docs/DEPLOYMENT_GUIDE.md) — Full deployment steps and environment variable reference
