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

**Data Isolation**: Memory is isolated per user (actor ID) with no cross-user access.

**Memory Actor Identifier**: The runtime does not give AgentCore Memory the raw Cognito subject. It derives the actor ID as a SHA-256 digest of the verified subject with a fixed domain-separation prefix (`backend/src/utils/memory_identity.py`), so the same user always maps to the same opaque 64-character hexadecimal ID. This matters because the memory service can emit its own trace spans. When tracing is enabled on the memory resource (the AgentCore CLI enables it for a memory it creates while observability is on), the `CreateEvent`, `GetEvent`, `ListEvents`, and `DeleteEvent` spans carry the actor ID as `actor.id`, and the memory-record retrieval spans carry the namespace, which embeds it ([AgentCore memory observability data](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-memory-metrics.html)). The service produces those spans, so the runtime cannot redact them, and a raw subject would put a stable user identifier into exported traces. The same ID is the long-term-memory namespace and the namespace for semantic memories the runtime saves. A subject that is not a usable string (not a string, empty or whitespace-only, or not encodable as UTF-8) disables memory for the request; there is no fallback to the raw value.

- *Pseudonymous, not anonymous.* Anyone who already knows a subject can compute its actor ID, which lets an operator locate or delete one user's memory: `cd backend/src && python3 -c "from utils.memory_identity import memory_actor_id as m; print(m('<subject>'))"` prints it. Cognito subjects are random UUIDs, so an actor ID does not reveal its user without a list of candidate subjects. Anyone who already holds a list of subjects — a Cognito `ListUsers` result, a copy of a JWT, or older logs — can map a trace entry back to a user by computing each subject's actor ID. The digest is domain-separated, not keyed: the prefix is a public constant, so there is no secret, no key to lose, and no rotation. A keyed construction (an HMAC with a server-owned secret) would also stop a reader who holds subjects but not the key from linking trace entries to users, at the cost of distributing the secret and making memory continuity depend on it. It is not used because the trace audience in scope does not hold subject lists. It would be reconsidered if that audience comes to include readers who hold or can obtain subjects.
- *Tenant and workspace are deliberately not part of the digest input.* The input is the subject prefixed by the domain string, with no tenant or workspace component. Cognito subjects are unique within one user pool, so this is correct for one user pool per deployment. It does not separate two pools that share a single memory resource: a subject colliding across two pools would map to the same actor ID. This deployment uses one pool, so the input is intentionally kept minimal.
- *Upgrade effect.* Memory stored under a raw subject (written by a runtime that does not use this scheme) is not read, so each user starts with an empty memory once. Nothing migrates or deletes that data. Events expire on the memory resource's event expiry, which the service applies to each event when it is written (the AgentCore CLI creates the memory with 30 days unless configured otherwise). Long-term records do not expire by default. The records the runtime saves are written with `BatchCreateMemoryRecords`, which takes a time-to-live only from the request (a strategy-level setting does not apply to batch-written records), and the runtime sends none, so they do not expire ([long-term record TTL](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/long-term-record-ttl.html)). The runtime's IAM role cannot delete records, so an operator removes them with `BatchDeleteMemoryRecords` or `DeleteMemoryRecord`, or by deleting the memory resource. The operator who deploys the upgrade cleans up the orphaned records from an operator or administrator session; the step-by-step commands are in [Clean Up Memory Records Left Under Raw-Subject Actor IDs](docs/DEPLOYMENT_GUIDE.md#clean-up-memory-records-left-under-raw-subject-actor-ids).
- *Changing the scheme* (the domain prefix or the hash) has the same effect: every user gets a new actor ID.

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
| **4** | Prompts → Model | **Bedrock Guardrails** | Input and output: Personal Advice topic blocking, harmful-content filters, the managed profanity word list, and blocking of AWS access and secret keys, passwords, card numbers, SSNs, and the Kubernetes-secret regex. Output only: anonymization of email, phone, IP address, and username, and the AWS account ID and internal-hostname regexes; user input containing them reaches the model unmasked. Input only: prompt-attack detection. The scope topics (General Programming Help, Entertainment and Casual Chat) block on input only; on output they are detect-only (`OutputAction: NONE`), and a match appears only in the per-request Guardrail trace. See [Guardrail residual risk](#guardrail-residual-risk). |

**Configuration Locations**:
- Cognito: `infrastructure/cloudformation/01-base-infrastructure.yaml` (lines 12-73)
- ECS task role: `infrastructure/cloudformation/01-base-infrastructure.yaml`
- AgentCore execution role: `infrastructure/cloudformation/01-base-infrastructure.yaml` (`AgentCoreExecutionRole`)
- Guardrails: `infrastructure/cloudformation/04-bedrock-guardrails.yaml`

### Guardrail Residual Risk

The two scope topics (General Programming Help, Entertainment and Casual Chat) block on user input and are detect-only on model output (#530). Valid infrastructure answers (kubectl commands, GameLift Server SDK steps, CloudFormation templates) were being blocked on output as off-topic. Accepted residual risk:

- **Only the latest user message is scope-checked.** The runtime sets `guardrail_latest_message=True`, so tool results, Knowledge Base passages, and earlier turns are never evaluated by the topic policy. An off-topic answer derived from that content is returned instead of blocked.
- **Language coverage.** The Guardrail uses the default CLASSIC topic tier, which supports English, French, and Spanish. An off-topic request in another language can pass the input check, and the output check will not catch it.
- **No user-visible fallback for scope detections on output.** The answer is returned unchanged. Personal Advice, content-filter, PII, and regex blocks still replace the entire response with the blocked-output message.
- **Detections are not recorded.** A scope-topic match on output appears only in the per-request Guardrail trace. The application does not log or store it.
- **Anonymize-type values in user input are not masked.** Email, phone, IP address, account ID, and internal hostnames in a user's message reach the model unchanged; anonymization applies to model output. Blocked types (keys, passwords, card numbers, SSNs, the Kubernetes-secret regex) are blocked on input too.
- **Masked values in generated templates.** `IP_ADDRESS` anonymization rewrites addresses and CIDR ranges in generated templates (for example `0.0.0.0/0` becomes `{IP_ADDRESS}`). Those templates do not deploy as written.

Every other protection is unchanged. `backend/tests/unit/test_guardrail_template_unit.py` pins the whole policy, including topic definitions, examples, and the blocked messages. `backend/tests/fixtures/guardrail_eval_corpus.json` is the evaluation corpus; `backend/tests/integration/test_guardrail_corpus_cloud.py` runs it against a deployed Guardrail with `ApplyGuardrail`.

### Data Classification by Stage

| Stage | Data | Classification | Protection |
|-------|------|----------------|------------|
| User input (browser) | Raw prompts | **Untrusted** | TLS 1.2+ in transit |
| Frontend (ECS Express) | JWT claims, validated prompt | **Authenticated** | HttpOnly cookies, Cognito verification |
| Backend (AgentCore) | Sanitized prompt, user context | **Validated** | Input validation, rate limiting, guardrails |
| AWS service responses | Fleet/cluster info, costs | **Internal** | IAM-scoped read-only access |
| Logs (CloudWatch) | Request IDs, bounded metrics, outcomes | **Redacted** | No prompt text or raw identifiers; sensitive patterns and control characters stripped by `sanitize_log_data()`/`normalize_log_value()`, correlation via `redact_identifier()`, encrypted at rest |
| Memory (AgentCore) | Conversation history, user facts | **Personal** | Per-user isolation by a pseudonymous actor ID (a digest of the verified subject, not the subject), encrypted at rest, event expiry |

### Service-to-Service Credentials

No static credentials (access keys, passwords, tokens) exist in the codebase or deployment configuration. Service-to-service authentication uses IAM roles with automatic credential rotation, except the frontend→AgentCore hop, which carries the end user's short-lived Cognito access token:

- **ECS Express → AgentCore**: the user's verified Cognito access token as a bearer token over HTTPS (no SigV4, no task-role grant). The ECS task role (`ecs-tasks.amazonaws.com`, `aws:SourceAccount` condition) has no identity-based permissions; it exists only so ECS can vend task credentials to the container. The supported chat path's one AWS call, `sts:GetCallerIdentity` (to build the runtime ARN), requires no IAM allow, so no policy is attached. The sample ships no in-app account-management route, so the role is never granted Cognito administrator permissions; access is administrator-provisioned with `add-admin-user.sh` or the PowerShell `Add-GameAgentAdmin`.
- **AgentCore → AWS Services**: AgentCore execution role (`bedrock-agentcore.amazonaws.com`) with `aws:SourceAccount` condition
- **MCP Servers**: Run as subprocesses via stdio transport (no network access), inherit the AgentCore execution role credentials

### AgentCore Execution Role (Least Privilege)

Issue #482 rebuilt `AgentCoreExecutionRole` from its reviewed callers upward. It
attaches **no AWS managed read policies** (the former
`AWSBillingReadOnlyAccess`, `AWSCloudFormationReadOnlyAccess`,
`CloudWatchReadOnlyAccess`, `CloudWatchLogsReadOnlyAccess`, and
`CostOptimizationHubReadOnlyAccess` grants allowed reading any log group, stack
template/parameter, and metric in the account). Every statement below names a
concrete reviewed caller. For every action except three, that caller worked on
the prior role, so the rebuild only removes, scopes, or makes explicit what the
managed policies previously supplied implicitly. The three exceptions are
read-only: `compute-optimizer:GetEnrollmentStatus` (the Billing MCP
compute-optimizer tool's enrollment check) and `eks:ListInsights` /
`eks:DescribeInsight` (the eks-mcp `get_eks_insights` tool). Their callers were
already offered to the model and failed only because the prior role denied
them. The rebuild adds **no write capability**. No runtime path reads log
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
| ComputeOptimizerReadAccess | `compute-optimizer:GetEC2InstanceRecommendations`, `GetAutoScalingGroupRecommendations`, `GetEnrollmentStatus` | Billing MCP compute-optimizer (EC2 + Auto Scaling rightsizing, and its enrollment probe) | `*` + `aws:RequestedRegion` (regional service, no resource type) |
| AutoScalingReadAccess | `autoscaling:DescribeAutoScalingGroups`, `DescribeAutoScalingInstances` | Compute Optimizer Auto Scaling dependent reads | `*` + `aws:RequestedRegion` (no resource type) |
| GameLiftReadAccess | 10 reviewed `gamelift:List*/Describe*` ops | GameLift specialist | `*` + `aws:RequestedRegion` (no fleet-ARN scoping for collection/capacity reads) |
| EKSReadAccess | `eks:ListClusters`, `DescribeCluster`, `List/DescribeNodegroup(s)`, `List/DescribeFargateProfile(s)`, `List/DescribeAddon(s)`, `ListInsights`, `DescribeInsight` | aws-api-mcp `call_aws` + eks-mcp (including `get_eks_insights`) | `*` + `aws:RequestedRegion` (dynamic cluster names) |
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
  `GetAutoScalingGroupRecommendations` / `GetEnrollmentStatus` and the
  **Auto Scaling** describes
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
  instead by the `aws:RequestedRegion` condition. The Insights reads
  (`ListInsights`, `DescribeInsight`) also take a `cluster` ARN and use `*` for
  the same reason.

> **Region narrowing (behavior change from the prior role).** On the prior role
> the Compute Optimizer and Auto Scaling grants carried no region condition (the
> latter came unconditioned from `CloudWatchReadOnlyAccess`), so a model-supplied
> `region` could run rightsizing in another region. The reviewed role constrains
> both to the deployment region, matching the single-region posture of the
> GameLift and EKS reads. Cross-region rightsizing is no longer available.

**Not granted (pre-existing gaps).** These actions were either never granted on
the prior role, or had a reviewed caller that never actually worked on it, so
adding them now would expand provider capability and is out of scope for the
least-privilege rebuild. The three reads named above were granted after their
denials were observed and reviewed for issue #583; each action below still
needs its own review before it is granted. Each is tracked as a follow-up, with
its runtime impact:

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

### Application Log Minimization

Application logs are limited to request/correlation IDs, bounded metrics, and
operation outcomes. They do not contain prompt text, extracted names,
semantic-memory content, email addresses, display names, raw Cognito subjects,
or raw session/thread identifiers.

- Agents run without the default Strands stdout printer
  (`callback_handler=None`), so streamed model text and tool names are not
  written to stdout, which the hosted runtime ships to its log group. A static
  test checks every `Agent(...)` construction.
- Correlation uses a per-request ID. The backend binds the AgentCore runtime
  request ID into every log record for the duration of an invocation, so lines
  from one request can be correlated without any raw identifier. A value that
  must stay tied to a principal or session across lines is emitted as a
  bounded, non-reversible token: `redact_identifier()`
  (`backend/src/utils/security.py`, a keyed HMAC-SHA-256 prefix over a random
  per-process key) on the backend and `redact()` (`ui/src/utils/logger.ts`) on
  the frontend. The HMAC key is random per process and is never read from the
  environment, so a token is stable only within a single process and does not
  correlate across processes, restarts, or tiers. The frontend additionally
  records AgentCore's service request ID from the `x-amzn-RequestId` response
  header, which identifies the invocation to the service (for example in a
  support case). It comes from a different header than the request ID the
  runtime container logs, so the two are not known to match and it is treated
  as a service-call identifier rather than a cross-tier correlation key. It is
  not derived from caller identity or content and is logged normalized rather
  than redacted.
- Externally influenced fields are normalized before emission so carriage
  returns, line feeds, other C0/C1/DEL control characters, the Unicode line and
  paragraph separators, and bidirectional-formatting controls cannot inject
  additional log records or reorder a rendered line. The backend normalizes
  every record at the logging sink (`normalize_log_value()` applied by the
  logger patcher in `backend/src/utils/logger.py`); the frontend logger strips
  the same classes in `logInfo`, `logWarning`, `logError`, `logDebug`, and
  `redact`.
- The AgentCore SDK installs its own log handler whose formatter would emit the
  raw runtime session ID on every invocation. `agentcore_main` wraps that
  formatter so the serialized `sessionId` is replaced with a bounded token while
  `requestId` is preserved.
- Semantic-memory logging records only outcomes and counts.
- The request-handling error paths that can see prompt or identity values — the
  AgentCore entrypoint, the orchestrator (including the model-loop failure over
  the user prompt), the specialists, agent timing, and semantic memory — record
  the exception class name, a typed, sanitized error code, the request ID, and a
  bounded list of `module:function:lineno` raise-site frames (no message,
  locals, arguments, source text, or file paths) through
  `log_sanitized_exception()`, never the exception message, which can embed a
  provider validation payload or caller value. The raise-site frames restore
  bug triage without reintroducing caller- or provider-authored content, and the
  frame count and each component are bounded so a deep stack cannot produce an
  unbounded line. The full rendered traceback is emitted only when debug logging
  is explicitly enabled (a non-production switch). Separately governed diagnostic
  paths that cannot carry prompt or identity values do retain the exception
  message to classify the failure: startup credential and container pre-warm
  checks and the memory-ID config read in `agentcore_main`, the GameLift
  specialist's AWS SDK describe/list calls, the Cost Explorer diagnostic path,
  the cost-report snapshot store's DynamoDB failure paths, and the Bedrock Prompt
  Management load fallback.

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
- No PII in application log messages (prompt text and identifiers excluded; see
  Application Log Minimization). Distributed-trace span content is governed
  separately: the tracing SDK can attach prompt and response content to GenAI
  spans, controlled by `OTEL_SEMCONV_STABILITY_OPT_IN`
  (`gen_ai_unredacted_attributes`). Narrowing trace-span content is tracked as a
  follow-up.

### SOC 2 / ISO 27001

- AWS services are SOC 2 and ISO 27001 certified
- Encryption at rest and in transit
- Audit logging enabled
- Access controls enforced

**AWS Compliance Programs**:
- [AWS Compliance Programs](https://aws.amazon.com/compliance/programs/)

---

## Account-Wide Observability Blast Radius

X-Ray Transaction Search depends on account-wide, cross-application settings — the
X-Ray trace segment destination, the default X-Ray indexing (sampling) rule, and a
shared CloudWatch Logs resource policy on the AWS-reserved `aws/spans` log group.
Changing them affects every X-Ray and Transaction Search consumer in the account,
so this project treats them as a blast-radius concern:

- **This repository's own setup is opt-in and read-only by default.**
  `scripts/infrastructure/setup-account-observability.sh` (and the
  `Invoke-GameAgentAccountObservability` PowerShell function) make no account-wide
  change unless the operator sets `GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true`
  (PowerShell `-ConfigureAccountObservability`). They read existing state first,
  enable Transaction Search additively (never a disable/re-enable toggle), and refuse
  to write when a state they need cannot be read, rather than guessing.
- **`agentcore launch` is a separate source of account-wide change.** When runtime
  observability is enabled (the setting that also selects the
  `opentelemetry-instrument` entrypoint), the AgentCore starter toolkit (0.3.11)
  runs its own `enable_transaction_search_if_needed` step on every launch. Version
  0.3.11 has no flag to keep the OTEL entrypoint while skipping only that account
  step. The deployment therefore runs a **read-only preflight before every
  `agentcore launch`** that predicts, using the toolkit's own conditions, which
  account-wide settings the toolkit would change — a `TransactionSearchXRayAccess`
  resource policy (name-only check), the trace segment destination, and the shared
  `Default` X-Ray indexing rule, which the toolkit sets to 1% via
  `UpdateIndexingRule`. The preflight prints either a single no-change line or the
  per-item detail with a prominent warning and only the relevant cleanup commands.
  A read failure is reported as **unknown** (a possible change), never as "no
  change"; the toolkit writes on doubt, so the warning states that for each unknown
  item it will perform the write. The default deploy is non-blocking; operators who
  require no shared account change set the fail-closed
  `GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE=true` (PowerShell
  `-StopOnAccountObservabilityChange`), which halts the deploy before launch when
  the toolkit would — or might — change account-wide settings. Only the exact value
  `true` enables it (`TRUE`, `1`, and empty are treated as off in both shells).
  Note that the toolkit checks for a policy named literally
  `TransactionSearchXRayAccess`, so it adds its own policy even when this project's
  `GameAgentTransactionSearchXRayAccess` policy already grants the access.

See [Deployment Guide — Account-wide Observability](docs/DEPLOYMENT_GUIDE.md#account-wide-observability-scoped-and-opt-in)
and the launch-time exception section that follows it for the full behavior,
warning text, and rollback commands.

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
