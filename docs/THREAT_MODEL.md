# Game Agent - Threat Model

This document provides a threat model for Game Agent following the STRIDE
methodology.

Game Agent has two clearly separated scopes, and this document keeps them
separate:

1. **The deployed read-only chat path.** This is the current product behavior.
   It authenticates users, answers questions, and calls AWS provider APIs
   **read-only**. It is analyzed in [Part I](#part-i-deployed-read-only-chat-path).
2. **The optional operations control plane (planned).** This is an *optional*,
   **default-disabled** deployment that adds operations APIs, direct
   authenticated approval, prepared executors, remote MCP access, and narrowly
   bounded autonomy. **None of its infrastructure, permissions, executors, or
   write credentials are created by the default deployment.** Its boundaries are
   fixed by accepted Architecture Decision Records (ADRs) and versioned
   contracts and are analyzed in
   [Part II](#part-ii-optional-operations-control-plane-planned).

> **Reading rule (do not skip).** A control described in Part II is a **planned
> or default-disabled** requirement on future implementation unless it is
> explicitly labelled **Deployed today**. Do not rely on a Part II control as if
> it already runs. Read-only IAM in the deployed path is **not** future write
> authorization. Model output, an operation identifier, and an automation
> identity are **never** authorization. Source-control provider writes always
> require human approval.

## Document Information

- **Version**: 2.0
- **Last Updated**: 2026-09-21
- **Status**: Approved for the deployed chat path; planned-control analysis for
  the optional operations control plane
- **Author**: Security Engineering
- **Methodology**: STRIDE per element, with attack trees for the highest-risk
  flows
- **Authoritative sources**: This model is derived from and must stay
  consistent with the accepted ADRs
  ([0001](adr/0001-preserve-chat-and-add-optional-operations.md),
  [0002](adr/0002-use-protocol-neutral-operations-services.md),
  [0003](adr/0003-isolate-provider-writes.md),
  [0005](adr/0005-persist-operations-and-recover-workflows.md)), the proposed
  [ADR 0004](adr/0004-expose-governed-public-mcp-facade.md), the versioned
  [Operations Contracts](OPERATIONS_CONTRACTS.md), and
  [Identity and Authorization](IDENTITY_AND_AUTHORIZATION.md). If this document
  and those sources disagree, those sources win and this document is the defect.

## How to Read the Status Column

Every threat row and attack-tree leaf carries an explicit status so that a
reviewer never confuses an aspiration with a running control:

| Status | Meaning |
|---|---|
| **Deployed** | The control runs in the current default deployment. |
| **Deployed (app-layer)** | The control runs today in application/adapter code that is present in the repository but only exercised when operations are enabled. |
| **Planned** | The control is a mandatory requirement on a future phase. It is **not** deployed. It is owned by the phase named in the row. |
| **Default-disabled** | The capability exists in design/code but is off unless an owner explicitly enables it; the safe (denying) behavior is the default. |
| **Residual** | A risk that remains after the named controls and requires a compensating control, a live exercise, or explicit acceptance. |

---

# Part I: Deployed Read-Only Chat Path

## System Overview

### Purpose

Game Agent is an AI-powered conversational assistant for managing AWS game
server infrastructure, specifically Amazon GameLift and Amazon EKS resources,
with cost analysis. The deployed path is **read-only**: it never mutates a
provider resource.

### Architecture Diagram

```
+----------------+     +------------------+     +-------------------+
|    End User    |---->|  ECS Express     |---->|  Bedrock AgentCore|
| (Web Browser)  |     | (Next.js Frontend)|     |  (AI Backend)    |
+----------------+     +------------------+     +-------------------+
        |                      |                        |
        v                      v                        v
+----------------+     +------------------+     +-------------------+
| Amazon Cognito |     | CloudWatch Logs  |     |  MCP Servers     |
| (Auth)         |     | (Observability)  |     |  (AWS Integration)|
+----------------+     +------------------+     +-------------------+
                                                       |
                                         +-------------+-------------+
                                         |             |             |
                                         v             v             v
                                    +--------+   +--------+   +--------+
                                    |GameLift|   |  EKS   |   | Cost   |
                                    | API    |   |  API   |   |Explorer|
                                    | (RO)   |   | (RO)   |   | (RO)   |
                                    +--------+   +--------+   +--------+
```

### Data Flow

1. User authenticates via Cognito.
2. User sends a query through the Next.js frontend.
3. Frontend calls the Bedrock AgentCore Runtime using SigV4.
4. AgentCore invokes the Orchestrator.
5. Orchestrator routes to specialist agents.
6. Specialists query AWS APIs via MCP servers using **read-only** IAM.
7. Response flows back through the chain.

## Trust Boundaries (Deployed)

These four boundaries and their authentication mechanisms are described in
detail in [`SECURITY.md`](../SECURITY.md#trust-boundaries--data-flow).

### Boundary 1: Internet to Application

- **Entry Points**: ECS Express ALB HTTPS endpoint
- **Exit Points**: API responses
- **Trust Level**: Untrusted

### Boundary 2: Frontend to Backend

- **Entry Points**: AgentCore Runtime API (SigV4)
- **Exit Points**: Agent responses
- **Trust Level**: Authenticated users

### Boundary 3: Application to AWS Services

- **Entry Points**: MCP server calls under a **read-only** IAM role
- **Exit Points**: AWS API responses
- **Trust Level**: IAM-controlled, read-only

### Boundary 4: Prompts to Model

- **Entry Points**: Bedrock model invocation
- **Exit Points**: Model output (treated as untrusted proposal data)
- **Trust Level**: Untrusted content in, untrusted content out

## STRIDE Analysis (Deployed)

### Spoofing

| Threat ID | Threat | Component | Mitigation | Status |
|-----------|--------|-----------|------------|--------|
| S1 | Unauthorized access via stolen credentials | Cognito Auth | MFA recommended, password policy enforced | Deployed |
| S2 | JWT token theft | Frontend/Backend | HttpOnly cookies, short token lifetime | Deployed |
| S3 | Session hijacking | User sessions | Secure cookie flags, session validation | Deployed |
| S4 | API key exposure | MCP Servers | IAM roles (no static keys) | Deployed |

### Tampering

| Threat ID | Threat | Component | Mitigation | Status |
|-----------|--------|-----------|------------|--------|
| T1 | Prompt injection to modify agent behavior | AI Backend | Guardrails, input validation, topic constraints; model output treated as untrusted | Deployed |
| T2 | Request tampering | API Layer | HTTPS, SigV4 signature covers URL/headers/body, request validation | Deployed |
| T3 | Log tampering | CloudWatch | CloudWatch access controls; see ledger append-only note in Part II | Deployed |
| T4 | Configuration tampering | Infrastructure | CloudFormation managed, Git version control | Deployed |

### Repudiation

| Threat ID | Threat | Component | Mitigation | Status |
|-----------|--------|-----------|------------|--------|
| R1 | Denial of malicious queries | User actions | CloudWatch logging, audit trail | Deployed |
| R2 | Admin action denial | Administrative ops | CloudTrail logging | Deployed |
| R3 | AWS API call denial | MCP operations | CloudTrail, X-Ray tracing | Deployed |

### Information Disclosure

| Threat ID | Threat | Component | Mitigation | Status |
|-----------|--------|-----------|------------|--------|
| I1 | Sensitive data in AI responses | AI Backend | Bedrock Guardrails PII filters | Deployed |
| I2 | AWS credentials exposure | MCP Servers | IAM roles, no static credentials | Deployed |
| I3 | Log data leakage | CloudWatch | `sanitize_log_data()`, access control, no raw tokens/PII | Deployed |
| I4 | Conversation history exposure | Memory System | User-scoped sessions (`actor_id`), encryption at rest | Deployed |
| I5 | Internal infrastructure details | AI Responses | Guardrails regex filters | Deployed |
| I6 | Customer data cross-contamination | Multi-tenant | User-scoped sessions, separate contexts | Deployed |

### Denial of Service

| Threat ID | Threat | Component | Mitigation | Status |
|-----------|--------|-----------|------------|--------|
| D1 | API flooding | ECS Express | Auto-scaling, WAF rate limiting | Partial |
| D2 | Large prompt attacks | AI Backend | Input length limits (32KB) | Deployed |
| D3 | Resource exhaustion | MCP Servers | Connection pooling, timeouts | Deployed |
| D4 | Cost exhaustion | AI Backend | Bedrock quotas, monitoring | Partial |

### Elevation of Privilege

| Threat ID | Threat | Component | Mitigation | Status |
|-----------|--------|-----------|------------|--------|
| EoP1 | User to Admin escalation | Cognito | Group-based authorization, admin-only user creation | Deployed |
| EoP2 | Read to Write escalation | AWS APIs | **Read-only IAM policies** — the deployed role has no provider write, and read-only IAM is **not** a latent write grant | Deployed |
| EoP3 | Cross-tenant access | Multi-tenant | User-scoped sessions, IAM conditions | Deployed |
| EoP4 | Agent tool abuse | AI Backend | Tool allowlists, limited read-only capabilities | Deployed |

> **Note on EoP2.** The deployed AgentCore execution role grants only
> `Describe`/`Get`/`List` provider actions (GameLift, EKS, Cost Explorer,
> CloudControl read) plus read-plus-append memory access and read/write to the
> product's *own* cost-snapshot DynamoDB table and log buckets. It grants **no**
> GameLift/EKS mutation, no `iam:PassRole`, no Step Functions, and no executor
> role. Provider writes are introduced only by the optional control plane in
> Part II, behind a **separate** role. This applies the Amazon *Least Privilege
> Design* best practice (start from zero permissions and add only what a
> component needs) and *Prevent Privilege Escalation* (no mutating IAM actions,
> `PassRole` scoped and conditional).

---

# Part II: Optional Operations Control Plane (Planned)

> **Status banner.** Everything in Part II is **optional and default-disabled**.
> Per [ADR 0001](adr/0001-preserve-chat-and-add-optional-operations.md) the
> single backend-enforced deployment ceiling
> `GBAW_OPERATIONS_MODE=disabled|observe|advise|remediate|operate` defaults to
> `disabled`; the default deployment **creates no operations resources and adds
> no operations cost**. The first write capability requires its own
> implementation and security review before any executor role is deployed
> ([ADR 0003](adr/0003-isolate-provider-writes.md)).

## Phase Model

| Phase | Deployment mode | Adds | Provider writes? | Status |
|---|---|---|---|---|
| E0 | (spike) | Latency-validation harness; no infrastructure, no permissions | No | Accepted spike; nothing deployed |
| E1 | `observe` | Bounded read-only observation with durable status + append-only ledger | No | Planned |
| E2 | `advise` | Recommendations from observed state; still no write | No | Planned |
| E3 | `remediate` | Prepared operations, direct approval, prepared executor for a narrow capability | Yes, **human-approved**, per-capability role | Planned |
| E4 | `operate` (bounded) | Narrowly bounded autonomy within hard limits, emergency disablement | Yes, within calculated authority + limits | Planned |
| E5 | remote access | Optional governed public MCP facade (proposed [ADR 0004](adr/0004-expose-governed-public-mcp-facade.md)) | No (read-only first release) | Proposed |

Effective authority is the deterministic minimum of deployment mode, tenant
policy, workspace policy, principal authority, capability maximum, and operation
risk policy ([ADR 0001](adr/0001-preserve-chat-and-add-optional-operations.md)).
A UI, a model, or a request body can never raise it.

## Operations Trust Boundaries (Planned)

| # | Boundary | From → To | Trusted authentication | Key rule |
|---|---|---|---|---|
| O1 | Remote client → operations edge | External MCP/OAuth/OIDC or SigV4 client → AgentCore Gateway / Operations HTTP API | Verified OAuth2/OIDC JWT (issuer, signature, expiry, token use, audience, client, scopes, tenant/workspace claims) or a separate SigV4 service principal | Principal is built only from verified auth; **never** from tool arguments, headers, or body ([ADR 0004](adr/0004-expose-governed-public-mcp-facade.md), [Identity](IDENTITY_AND_AUTHORIZATION.md)) |
| O2 | Adapter → application services | Any adapter (HTTP, MCP, chat, event) → protocol-neutral services | Adapter constructs immutable `VerifiedPrincipal`; rejects body-supplied identity | One authorization decision service for all adapters; same principal + request ⇒ same decision ([ADR 0002](adr/0002-use-protocol-neutral-operations-services.md)) |
| O3 | Approval boundary | Direct authenticated UI/API approval action → `ApprovalService` | Fresh `VerifiedPrincipal` supplied out-of-band; untrusted payload is *only* an operation id | Approval binds to stored `operation_id` + `prepared_operation_hash`, expires, and is not reachable from chat/model ([Identity](IDENTITY_AND_AUTHORIZATION.md), [ADR 0005](adr/0005-persist-operations-and-recover-workflows.md)) |
| O4 | Durable workflow → executor | Step Functions workflow → prepared executor | Authenticated service identity distinct from requester/approver | Workflow sends **only** an operation id; identifier is not a credential; executor re-verifies everything ([ADR 0003](adr/0003-isolate-provider-writes.md), [ADR 0005](adr/0005-persist-operations-and-recover-workflows.md)) |
| O5 | Executor → provider | Prepared executor → one allowlisted provider action | Narrow per-capability IAM role | No shell, no arbitrary code, no generic AWS/K8s calls; parameter bounds, preconditions, idempotency enforced ([ADR 0003](adr/0003-isolate-provider-writes.md)) |
| O6 | Executor → source-control provider | Source-control executor → repository | **Separate write credential**, distinct from the read credential | All writes human-approved; deterministic proposal branch; provider-enforced uniqueness ([ADR 0005](adr/0005-persist-operations-and-recover-workflows.md), [Contracts](OPERATIONS_CONTRACTS.md)) |
| O7 | Autonomy engine → operations services | Trusted autonomy code → prepare/approve/execute path | Runs as trusted code, re-observes current state | Selects an exact registered capability/playbook/executor; hard risk/rate/cost/concurrency/frequency/cooldown/blast-radius limits; fail-closed escalation |
| O8 | Persistence & audit | Application services → DynamoDB + object storage + ledger | Fenced conditional/transactional writes | Append-only ledger by mandatory conditional `PutItem`; hash-verified externalized content; single-flight lease with fencing ([ADR 0005](adr/0005-persist-operations-and-recover-workflows.md)) |

## Core Invariants (apply to all operations boundaries)

These are the non-negotiable statements the issue requires to be explicit. Every
threat below is an attempt to violate one of them.

1. **Model output and automation proposals are untrusted.** They may propose;
   they never authorize, grant authority, or lower risk.
2. **An operation identifier is neither a credential nor authorization.**
   Neither is an automation/workflow identity.
3. **Identities are distinct:** requester, approver, automation, workflow,
   executor, and chat-runtime are separate principals with separate permissions.
4. **Approval binds to prepared content and expires.** It binds to the stored
   `operation_id` and the RFC 8785 canonical `prepared_operation_hash`, and only
   under current policy.
5. **Provider writes are deterministic and isolated.** They occur only through a
   prepared executor with a narrow per-capability role, never from the UI, HTTP
   API, chat tools, remote MCP tools, event adapters, or the chat runtime.
6. **Source-control writes always require human approval;** repository review
   counts as an additional gate **only** when repository policy enforces it.
7. **Any uncertainty fails closed** and escalates to an operator: stale signal,
   policy mismatch, inconclusive provider outcome, verification failure, rollback
   uncertainty, or circuit-breaker event.
8. **Read and write credentials are separate**, and the chat-runtime and
   executor permissions are separate.

## STRIDE by Operations Boundary

### O1 — Remote MCP / OAuth / OIDC clients

| ID | Threat (STRIDE) | Preventive control | Detective control | Residual risk | Owner |
|---|---|---|---|---|---|
| OP-S1 | (S) Client forges principal via tool arguments, custom headers, or body | Principal built only from verified token claims or trusted client-registration mapping; `additionalProperties:false` on request schema drops injected identity fields; `VerifiedPrincipal` constructible only by verifier-owned adapter code | Auth-failure metrics; schema-rejection logs (generic messages) | A mis-issued token from a trusted IdP is accepted until revoked | E5 / [ADR 0004](adr/0004-expose-governed-public-mcp-facade.md) |
| OP-S2 | (S) Caller bypasses Gateway to reach the MCP Runtime directly | Restrict direct Runtime access so clients must traverse Gateway auth, policy, rate limits, and audit | Network/access logs on the Runtime; Gateway request correlation | Misconfiguration that exposes the Runtime endpoint | E5 |
| OP-S3 | (S) SigV4 service client impersonates a Cognito user | Separate authenticated service-principal path; a service identity must not map to a human subject; one inbound auth type per Runtime | Principal-type assertions in logs | Over-broad service client registration | E5 |
| OP-E1 | (E) Caller's own AWS identity treated as downstream authorization | Caller authentication never grants the Runtime role's downstream permissions | IAM access analyzer; CloudTrail | — | E5 |
| OP-I1 | (I) Token/PII leakage in errors or logs | Generic client and log errors; no raw tokens, cookies, emails, or display names; correlation by request id + redacted identifiers | Log-content scan (`check_public_content.py`), redaction tests | — | E5 / [Identity](IDENTITY_AND_AUTHORIZATION.md) |

> **Deployed today:** There is **no** remote MCP HTTP adapter. The fallback is
> that remote operations are unavailable; end-to-end OAuth/OIDC propagation
> testing belongs with that future adapter
> ([Identity](IDENTITY_AND_AUTHORIZATION.md#remote-mcp-clients)). Amazon
> *API Gateway authentication* guidance (every method authenticated; separated,
> access-controlled stages; scoped CORS; access logging) applies when the edge
> is built.

### O2 — Adapter → protocol-neutral application services

| ID | Threat (STRIDE) | Preventive control | Detective control | Residual risk | Owner |
|---|---|---|---|---|---|
| OP-T1 | (T) Authorization logic duplicated/drifts across adapters, creating a bypass | Product rules live only in protocol-neutral services; adapters translate but do not own authorization; application layer must not import CopilotKit/Next.js/MCP/agent-message types | Contract-parity and authorization-parity tests as release gates | A new adapter that skips the shared service | E1+ / [ADR 0002](adr/0002-use-protocol-neutral-operations-services.md) |
| OP-S4 | (S) Request body supplies requester/approver/tenant/workspace | `prepare-operation-request` schema has no identity/tenant/workspace/correlation/idempotency/risk/executor/base-revision fields and sets `additionalProperties:false` | Schema-validation rejection logs | — | E1+ / [Contracts](OPERATIONS_CONTRACTS.md#trusted-context) |
| OP-T2 | (T) Model output injected as a deterministic decision | Language-model output is untrusted proposal data until validated by services; a playbook requiring approval cannot yield an `authorized` decision | Decision/reason-code agreement validation | — | E2+ / [ADR 0004](adr/0004-expose-governed-public-mcp-facade.md) |

### O3 — Direct authenticated approval

| ID | Threat (STRIDE) | Preventive control | Detective control | Residual risk | Owner |
|---|---|---|---|---|---|
| OP-A1 | (E) Approval triggered from chat text or model output | Approval is a **direct authenticated UI/API action** outside the chat/model tool path; chat/model can request preparation but cannot invoke or synthesize approval | Ledger records approver source; approval-path tests | — | E3 / [Identity](IDENTITY_AND_AUTHORIZATION.md#approval-identity) |
| OP-A2 | (T) Approve one thing, execute another (content swap after approval) | Approval binds to stored `operation_id` + fresh RFC 8785 `prepared_operation_hash`; executor reloads and re-verifies the hash before the first write; prepared operation is written once and never rewritten | Hash-mismatch → typed fail-closed error, logged | — | E3 / [ADR 0005](adr/0005-persist-operations-and-recover-workflows.md#approval-authority-and-state-transitions) |
| OP-A3 | (E) Self-approval / separation-of-duties bypass | Default policy **denies self-approval**; a tenant/workspace policy may allow same-principal approval **only** for an eligible low-risk operation; higher risk requires a different authorized principal; `requester` and `approver` are separate verified values | Policy-case tests (the four rows in [Identity](IDENTITY_AND_AUTHORIZATION.md#approval-identity)); ledger | A policy misconfiguration that enables self-approval too broadly | E3 |
| OP-A4 | (R) Replay of a stale/expired approval | Approval carries an exclusive `commit_not_after` deadline = earliest of credential/operation/approval expiry, rechecked immediately before commit; expiry is a conditional state transition to `expired` | Deadline-expired typed outcome; ledger | — | E3 / [Identity](IDENTITY_AND_AUTHORIZATION.md#approval-identity) |
| OP-A5 | (E) One approval reused to authorize a second, independent execution | Approval reuse can only resume the *same* idempotent execution; a new independent operation needs its own prepared operation, hash, and approval | Idempotency + approval-binding tests | — | E3 / [ADR 0005](adr/0005-persist-operations-and-recover-workflows.md) |
| OP-A6 | (T) Double approval recorded on one operation | `pending_approval → approved` is one atomic transaction with a conditional `attribute_not_exists` put of the approval record | Precondition-failed outcome; ledger | — | E3 |
| OP-A7 | (S) Forged `VerifiedPrincipal` via generic deserialization | `VerifiedPrincipal` is a trusted capability, not a request-deserializable DTO; never registered for generic deserialization; freshness + audience/client/tenant/workspace rechecked | Type/adapter tests | — | E3 |

### O3b — Cancellation

| ID | Threat (STRIDE) | Preventive control | Detective control | Residual risk | Owner |
|---|---|---|---|---|---|
| OP-C1 | (E) Unauthorized cancellation | Cancellation is an authenticated action; cancellation is a conditional transition allowed only from a non-terminal state | Ledger event per transition | Authorization model for who may cancel is owned by the approval/UI phase | E3 |
| OP-C2 | (T) Cancellation races a terminal transition and corrupts state | Conditional transition loses the race and does not alter the terminal record | Conditional-write failure metrics | — | E3 / [ADR 0005](adr/0005-persist-operations-and-recover-workflows.md#state-model-mutable-snapshot-and-immutable-transitions) |

### O4 — Durable workflow → executor, and operation-identifier abuse

| ID | Threat (STRIDE) | Preventive control | Detective control | Residual risk | Owner |
|---|---|---|---|---|---|
| OP-X1 | (S/E) Direct executor invocation by a user, chat runtime, model, or unauthenticated caller | Executor authenticates and authorizes the **workflow** caller; rejects direct user/chat/model/unauthenticated invocation; only the authenticated durable-workflow identity invokes it | Executor auth logs; rejected-invocation metrics | — | E3 / [Identity](IDENTITY_AND_AUTHORIZATION.md#executor-identity) |
| OP-X2 | (E) Operation-identifier guessing to gain authority | An operation id is neither credential nor authorization; the executor independently loads and verifies stored operation, approval, hash, tenant/workspace, contract version, and executor binding | Verification-failure fail-closed; ledger | — | E3 / [Contracts](OPERATIONS_CONTRACTS.md#approval-binding) |
| OP-X3 | (R) Replay of a workflow dispatch | Idempotent execution keyed on durable state; a terminal state replays the stored result without repeating work | Provider-result records; ledger | — | E3 / [ADR 0005](adr/0005-persist-operations-and-recover-workflows.md#single-flight-lease-fencing-and-recovery) |
| OP-X4 | (E) Workflow self-authorizes an operation | Step Functions only orchestrates the wait and dispatch; the `ApprovalService` is the sole approval authority; identifier-only payloads between states | Step Functions approval-contract test | — | E3 / [ADR 0005](adr/0005-persist-operations-and-recover-workflows.md#future-durable-execution-workflow) |

### O5 — Prepared executor → provider (isolation & excessive permissions)

| ID | Threat (STRIDE) | Preventive control | Detective control | Residual risk | Owner |
|---|---|---|---|---|---|
| OP-P1 | (E) Chat-runtime compromise reaches a provider write | Chat role stays provider-read-only; writes only through a separate prepared executor with a narrow per-capability IAM role | CloudTrail; IAM access analyzer | — | E3 / [ADR 0003](adr/0003-isolate-provider-writes.md) |
| OP-P2 | (E) Executor over-permissioned or runs arbitrary actions | Executor exposes no shell, no arbitrary Python/generated code, no generic AWS/K8s calls; calls only the allowlisted action for its capability; enforces hard parameter bounds and preconditions | Per-capability IAM review before deploy; allowlist tests | A capability whose allowlist is scoped too broadly at review time | E3 |
| OP-P3 | (T) Partial execution / verification / reconciliation / rollback failure | Executor records verification and rollback results; on inconclusive outcome without a provider idempotency primitive, it **fails closed** for human reconciliation | Provider-result records; ledger; alarms | Provider-side duplicate side effect from an in-flight expired holder (see OP-D1) | E3 / [ADR 0005](adr/0005-persist-operations-and-recover-workflows.md#single-flight-lease-fencing-and-recovery) |
| OP-D1 | (Duplicate write) Expired lease holder still has a request in flight | One stable logical-action identifier per logical action bound to a capability-specific provider primitive (native idempotency token, conditional compare-and-set, or provider-enforced uniqueness); reconcile against provider before writing on a reclaimed lease | Provider-result reconciliation; ledger | A provider offering **no** such primitive: automation stops and fails closed rather than retrying | E3 |

> **Least-privilege note.** Each write capability has an independently
> reviewable permission boundary (Amazon *Least Privilege Design*). The executor
> role must avoid mutating IAM actions and scope any `PassRole` narrowly with
> conditions (Amazon *Prevent Privilege Escalation*); highly privileged
> permissions, if ever required, must be enumerated in this threat model before
> the role is deployed.

### O6 — Source-control prepare / approve / execute

| ID | Threat (STRIDE) | Preventive control | Detective control | Residual risk | Owner |
|---|---|---|---|---|---|
| OP-SC1 | (E) Read credential used to write, or write credential over-scoped | **Read and write credentials are separate**; the minimum read contract is `user_id`, `groups`, `tenant`, `workspace` and uses the existing connector policy; write is a distinct executor credential | Credential-separation review; CloudTrail | — | E3 / [Identity](IDENTITY_AND_AUTHORIZATION.md#current-web-path); executor impl is issue #314 |
| OP-SC2 | (T) Path traversal / repository escape | Semantic validation rejects absolute paths, empty segments, `.`/`..`, backslashes, duplicate paths, and normalizes to relative POSIX paths | Schema/validation tests; fixtures | — | E3 / [Contracts](OPERATIONS_CONTRACTS.md#source-control-profile) |
| OP-SC3 | (T) Stale base revision / changed enrollment applied blindly | Prepared operation binds the verified base revision, resource enrollment id/version, and policy version; client-carried revisions and observed metadata are **advisory only** | Enrollment/version mismatch → fail closed | Base revision moves between prepare and execute (reconcile at execute) | E3 |
| OP-SC4 | (T) Wrong or forged proposal branch | Deterministic branch `gba-op-` + first 20 hex of SHA-256(operation_id); executor recalculates and verifies; branch carries no requester/tenant/workspace/repo/recommendation data | Branch-value vectors (`contract-vectors.json`) | — | E3 / [Contracts](OPERATIONS_CONTRACTS.md#workflow-identity-and-idempotency) |
| OP-SC5 | (S/E) Duplicate branch/commit/proposal on retry | Deterministic branch is the provider-enforced uniqueness primitive; retry checks stored state, then the provider, and reuses the existing branch/commit/proposal; unclear result reconciles then fails closed | Provider-result records | — | E3 / [ADR 0005](adr/0005-persist-operations-and-recover-workflows.md#single-flight-lease-fencing-and-recovery) |
| OP-SC6 | (E) Pull/merge-request side effects: CI, bots, preview deploys, automatic merge | Provider writes always require human approval; a proposal is not a merge; automatic merge and preview deployments are out of the executor's authority | Repository audit; ledger | CI/bots triggered by branch/PR creation act with **their own** permissions outside this system's control | E3 / issue #280 In-Scope |
| OP-SC7 | (T) Weak branch protection / missing human review | Repository review counts as an additional independent gate **only** when repository policy enforces it; the system's own human approval (O3) is always required regardless | Branch-protection review at enrollment | A repository with weak protection reduces defense in depth to the single approval gate | E3 |
| OP-SC8 | (I) Secret/PII leakage in a proposal | The executable document carries no provider credential, token, email, display name, SDK object, generic command, or unfiltered provider response; content is UTF-8 with hash + byte limits | `check_public_content.py`; content validation | — | E3 / [Contracts](OPERATIONS_CONTRACTS.md#source-control-profile) |

### O7 — Narrowly bounded autonomy

| ID | Threat (STRIDE) | Preventive control | Detective control | Residual risk | Owner |
|---|---|---|---|---|---|
| OP-AU1 | (T) Poisoned, stale, or ambiguous observation triggers remediation | Trusted code **re-observes current state** before acting; any stale signal, uncertainty, or inconclusive provider outcome stops automation and escalates | Freshness checks; escalation events | An observation poisoned at the provider within the freshness window | E4 |
| OP-AU2 | (E) Model proposal mistaken for deterministic authorization | Autonomy selects an **exact registered** capability/playbook/executor and calculates effective authority deterministically; model output remains proposal data | Authorization decision records | — | E4 / [ADR 0004](adr/0004-expose-governed-public-mcp-facade.md) |
| OP-AU3 | (DoS/Cost) Repeated remediation loops, oscillation, runaway cost or blast radius | Hard **risk, rate, cost, concurrency, frequency, cooldown, and blast-radius** limits; circuit breaker | Budget/rate/concurrency alarms; cooldown timers | Limits mis-tuned too high | E4 / issue #280 In-Scope |
| OP-AU4 | (E) Autonomous policy bypass / excessive preauthorization | Effective authority is the minimum of all six ceilings; a policy mismatch stops automation and escalates; preauthorization cannot exceed capability maximum | Policy-version records; deny metrics | Over-broad preauthorization granted by an owner | E4 |
| OP-AU5 | (Availability) Missing emergency disablement | Emergency disablement / kill switch stops automation; `disabled` mode denies unconditionally and is the default | Disablement audit event | Disablement path itself unavailable (test as a live exercise) | E4 / [ADR 0001](adr/0001-preserve-chat-and-add-optional-operations.md) |
| OP-AU6 | (T) Verification/rollback failure during autonomous action | Verification failure or rollback uncertainty stops automation and escalates to an operator; canary rollout limits blast radius | Verification/rollback records; alarms | — | E4 |

### O8 — Persistence, audit integrity, and fail-closed recovery

| ID | Threat (STRIDE) | Preventive control | Detective control | Residual risk | Owner |
|---|---|---|---|---|---|
| OP-L1 | (T/R) Ledger event overwritten or deleted | Append-only enforced by mandatory conditional `PutItem` (`attribute_not_exists` on strictly increasing sequence), **not** IAM alone; IAM denial of update/delete is defense in depth | Conditional-write failure metrics; ledger-authority test | This is an **application-layer** append-only boundary, **not** storage-level immutability; object-lock/write-once would need a separate control + review | E1+ / [ADR 0005](adr/0005-persist-operations-and-recover-workflows.md#append-only-ledger-boundary-and-retention) |
| OP-L2 | (T) Stale/racing writer overwrites newer state | Mutable snapshot advanced only by conditional update on expected prior state, sequence, and fencing generation; immutable `STATE#<sequence>` transitions | Conditional-failure metrics | — | E1+ |
| OP-L3 | (T) Expired lease holder corrupts state after waking late | Single-flight lease with monotonic fencing generation; every fenced write validates holder + generation + deadline at the store | Fencing-condition failure metrics | Database fencing cannot stop an in-flight **provider** side effect (see OP-D1) | E3 / [ADR 0005](adr/0005-persist-operations-and-recover-workflows.md#single-flight-lease-fencing-and-recovery) |
| OP-L4 | (T) Unverified/oversized externalized content used | Externalized content is content-addressed and hash-verified (SHA-256) against the recorded hash before use; missing object, oversize, or mismatch fails closed | Hash-mismatch typed error | — | E1+ / [ADR 0005](adr/0005-persist-operations-and-recover-workflows.md#externalized-content-and-serialized-size-ceilings) |
| OP-L5 | (T) Idempotency-token reuse mutates an existing operation | Canonical idempotency fingerprint over workspace + token + prepared-operation hash; same content resolves to the same operation, different content fails `IDEMPOTENCY_CONFLICT`; mapping retained for the full audit window (no TTL race) | `IDEMPOTENCY_CONFLICT` metrics | — | E1+ / [ADR 0005](adr/0005-persist-operations-and-recover-workflows.md#idempotency-and-independent-operations) |
| OP-L6 | (R) Audit record of record lost after workflow history expires | DynamoDB ledger is authoritative and retained for the full audit window, independent of Step Functions' 90-day history | Retention configuration review | — | E3 / [ADR 0005](adr/0005-persist-operations-and-recover-workflows.md#future-durable-execution-workflow) |
| OP-L7 | (E) Capability / playbook / policy / executor **version drift or tampering** | Playbook hash binds every transitive schema hash, action allowlist, authority requirement, retry policy, hard limits, and the immutable executor binding; consumers use an exact-version allowlist and reject unknown versions with `CONTRACT_VERSION_UNSUPPORTED`; a changed schema invalidates the playbook | Contract-vector tests; version-allowlist tests | An attacker who can rewrite trusted storage *and* recompute all bound hashes | E1+ / [Contracts](OPERATIONS_CONTRACTS.md#compatibility-and-publication) |

## Cross-Workspace and Confused-Deputy

| ID | Threat | Preventive control | Residual risk | Owner |
|---|---|---|---|---|
| OP-W1 | Cross-workspace access | Tenant/workspace are trusted deployment/claim bindings, never a browser selection or request-body field; idempotency and operations are workspace-bound | Single-deployment binding today; a future workspace registry needs its own review | E1+ / [Identity](IDENTITY_AND_AUTHORIZATION.md#trusted-principal) |
| OP-CD1 | Confused deputy: trusted service acts on attacker-controlled input | Principal/authority derived only from verified context; caller auth never confers the Runtime role's downstream AWS permissions; adapters reject body-supplied identity | A trusted-but-compromised upstream IdP | E5 |
| OP-CD2 | Indirect prompt injection via provider content (resource names, diffs, observations) | Model output is untrusted; deterministic validation and hash-binding gate every write; observations are re-checked; source-control content is byte/hash-limited and carries no executable authority | Injection that changes *human* judgement during approval | E2+ |

## Autonomy / Automation identity confusion

| ID | Threat | Preventive control | Residual risk | Owner |
|---|---|---|---|---|
| OP-ID1 | Automation/workload identity impersonates a human principal | Automation, workflow, and executor identities are distinct from human requester/approver; an automation identity is not authorization; a human-approval gate remains for writes | Over-broad automation preauthorization (OP-AU4) | E4 |
| OP-ID2 | Human-principal confusion (requester vs approver vs subject vs client) | Subject and client are distinct even in one decision; `requester` and `approver` are separate verified values; separation-of-duties enforced by policy | — | E3 |

## E3 Execution Realization (issue #415)

The optional E3 execution control plane (`07-operations-execution.yaml`, a
**separate** stack) realizes the previously-planned executor boundary. It does
not weaken any Part II invariant; it makes the following concrete:

- **OP-X1 (direct executor invocation) is prevented structurally.** Three
  separate roles enforce the only legal path
  dispatcher → Step Functions STANDARD state machine → executor: the dispatcher
  role can `states:StartExecution` on the exact state machine but **cannot**
  `lambda:InvokeFunction` the executor; only the **workflow** role can invoke
  the executor, and only that exact function. The chat / general API / model /
  E1 / E2 roles hold no execute authority and cannot assume or invoke the
  executor.
- **operation_id is neither a credential nor authorization.** Dispatch carries
  **`operation_id` only**; the state machine threads no fleet id, capacity
  number, or principal claim. The executor re-loads the approved operation from
  the table under `operation_id` and re-checks authority, so guessing an id
  (OP-X2) grants nothing.
- **No blind retry.** The state machine attaches no `Retry` to the executor
  task; a transient failure is surfaced, never silently re-attempted, so a
  bounded capacity write is never blindly repeated.
- **OP-AU5 (emergency disablement) is a reversible two-lever kill switch.**
  `ExecutionMode=disabled` (the default) throttles the dispatch API stage to
  zero (lever 1) **and** injects the mode so the executor fails closed (lever 2),
  without deleting any resource or data. Re-enable is reversible.
- **Least privilege at the write edge.** The executor holds only
  `gamelift:DescribeFleetCapacity` + `gamelift:UpdateFleetCapacity`, scoped to
  the **exact enrolled fleet ARN**, plus the underlying DynamoDB item actions and
  KMS strictly via DynamoDB. No `iam:PassRole`, secrets, source-control, generic
  execute, or wildcard appears anywhere in the stack.

## E4 Control-Plane Realization (issue #416)

The optional E4 control plane (`08-operations-control-plane.yaml`, a **separate**
stack) realizes the deployment-wide kill switch and admin control API. It does
not weaken any Part I or Part II invariant; it makes the following concrete:

- **The kill switch is enforced by AWS AppConfig, not just by code.** The hosted
  configuration profile validates every version against a `JSON_SCHEMA` that is
  **byte-equivalent** to the frozen `operations-kill-switch` contract schema and
  is fully self-contained (no external `urn:` `$ref`), so AppConfig rejects any
  malformed or tampered document at author time. The seeded default disables
  everything and is already expired, so a fresh deploy fails closed.
- **Least privilege at the authoring edge.** The control role may
  `CreateHostedConfigurationVersion` / `StartDeployment` / `StopDeployment`
  **only** for the exact E4 AppConfig application / environment / profile /
  strategies, and append **bounded** audit items (`PutItem`/`GetItem`) to the 06
  table via the 06 CMK (KMS strictly via DynamoDB). No `iam:PassRole`, secrets,
  provider write, unbounded DynamoDB, or generic AppConfig admin appears
  anywhere in the stack.
- **Consumers get read-only, scoped data-plane access.** The additive wiring
  into 06/07 grants the E1/E2/E3 roles **only**
  `appconfig:StartConfigurationSession` + `appconfig:GetLatestConfiguration`,
  scoped to the exact kill-switch configuration resource, read in-process via the
  official AppConfig Lambda extension. The wiring is opt-in: a deploy that does
  not supply the identifiers attaches no layer and grants no AppConfig authority.
- **OP-AU5 (emergency disablement) is realized deployment-wide.** A reversible
  two-lever control-API disable (`ControlMode=disabled`) stops new admin changes;
  an **immediate** (hard-down) AppConfig strategy backs `disable-all-operations`
  and per-capability disable scripts that write an all-disabled document at 100%
  with no bake. None deletes a resource; each is reversible.
- **Freshness fails closed.** Every document carries a short `not_after`; a
  periodic EventBridge sweeper re-issues it, so a stopped control plane lets the
  document expire and consumers fail closed. The **gradual** strategy carries a
  CloudWatch monitor + automatic rollback wired to the failed/unverified alarms.
- **Controls carry no identity and are compare-and-set.** A control request
  carries only the desired booleans and the expected `config_version`; the acting
  admin is resolved from the verified caller, and a stale write cannot clobber a
  newer document.

## Attack Trees

### Attack Tree: Unauthorized Provider Write

```
Unauthorized Provider Write [ROOT]
├── Via the chat runtime
│   └── [PREVENTED, Deployed] Chat role is provider-read-only (ADR 0003); no executor reachable
├── Via an adapter (HTTP/MCP/chat/event) calling a provider directly
│   └── [PLANNED] Adapters cannot mutate; only request operations (ADR 0002/0003)
├── Via a forged/guessed operation identifier
│   ├── Guess operation id → invoke executor
│   │   └── [PLANNED] Executor rejects direct invocation; only the workflow identity invokes it (OP-X1)
│   └── Present id as authorization
│       └── [PLANNED] Id is not a credential; executor re-verifies stored approval + hash (OP-X2)
├── Via approval abuse
│   ├── Trigger approval from chat/model
│   │   └── [PLANNED] Approval is a direct authenticated action outside chat/model (OP-A1)
│   ├── Self-approve
│   │   └── [PLANNED] Default deny; low-risk exception only by explicit policy (OP-A3)
│   ├── Replay stale/expired approval
│   │   └── [PLANNED] commit_not_after deadline; expiry transition (OP-A4)
│   └── Swap content after approval
│       └── [PLANNED] Approval bound to prepared_operation_hash; executor re-verifies (OP-A2)
├── Via executor misuse
│   ├── Run shell / arbitrary code / generic API
│   │   └── [PLANNED] Not exposed; single allowlisted action only (OP-P2)
│   └── Over-broad IAM role
│       └── [PLANNED] Narrow per-capability role, reviewed before deploy (OP-P1/P2)
└── Via autonomy
    ├── Act on poisoned/stale observation
    │   └── [PLANNED] Re-observe; fail closed on staleness/uncertainty (OP-AU1)
    ├── Exceed authority / preauthorization
    │   └── [PLANNED] Min-of-six-ceilings; policy mismatch escalates (OP-AU4)
    └── Loop / oscillate / runaway cost
        └── [PLANNED] Rate/cost/concurrency/frequency/cooldown/circuit-breaker/blast-radius limits (OP-AU3)
```

### Attack Tree: Source-Control Proposal Abuse

```
Source-Control Proposal Abuse [ROOT]
├── Write with the read credential
│   └── [PLANNED] Separate read/write credentials (OP-SC1)
├── Escape the repository
│   ├── Absolute path / .. / backslash
│   │   └── [PLANNED] Path normalization + rejection (OP-SC2)
│   └── Wrong proposal branch
│       └── [PLANNED] Deterministic branch recomputed and verified (OP-SC4)
├── Apply stale base / changed enrollment
│   └── [PLANNED] Bind verified base revision + enrollment version; advisory client metadata only (OP-SC3)
├── Duplicate branch/commit/proposal on retry
│   └── [PLANNED] Provider-enforced uniqueness + reconcile-then-fail-closed (OP-SC5)
└── Abuse PR side effects (CI/bots/preview/auto-merge/weak protection)
    ├── [PLANNED] Human approval always required; proposal ≠ merge (OP-SC6)
    └── [RESIDUAL] CI/bots run with their own permissions; weak branch protection lowers defense in depth (OP-SC6/SC7)
```

## Residual Risks and Required Live Exercises

The following remain after the named controls and are called out explicitly, as
the issue requires:

- **Provider-side duplicate side effects (OP-D1/OP-P3).** Database fencing
  cannot prevent an in-flight request from an expired holder. A capability whose
  provider offers no idempotency/compare-and-set/uniqueness primitive must fail
  closed rather than retry. **Live exercise:** reclaim-during-in-flight test per
  write capability before deploy.
- **CI/bot/PR side effects (OP-SC6).** Automation triggered by branch/PR
  creation runs with its own permissions outside this system. **Live exercise:**
  enumerate repository automation at enrollment.
- **Weak branch protection (OP-SC7).** Repository review counts only when policy
  enforces it; otherwise defense in depth reduces to the single approval gate.
- **Emergency disablement path (OP-AU5).** The kill switch must itself be
  exercised. **Live exercise:** trigger emergency disablement and confirm
  automation halts and denies.
- **Autonomy limit tuning (OP-AU3/AU4).** Rate/cost/concurrency/cooldown/
  blast-radius limits set too high defeat their purpose. **Live exercise:**
  loop/oscillation and budget-exhaustion abuse cases before enabling `operate`.
- **Application-layer (not storage-level) audit immutability (OP-L1).** The
  ledger's no-overwrite guarantee comes from mandatory conditional writes, not
  from object-lock. Stronger immutability requires a separate control and review.
- **Remote OAuth/OIDC propagation (OP-S1/CD1).** No remote adapter exists; its
  end-to-end propagation must be tested with that adapter, and remote operations
  stay unavailable until then.
- **Trusted-storage tampering with hash recomputation (OP-L7).** An attacker who
  can both rewrite trusted storage and recompute every bound hash is outside the
  application-layer controls and requires infrastructure-level protection.

## GenAI-Specific Threats (both parts)

### Prompt Injection

| Attack Vector | Description | Mitigation | Status |
|---------------|-------------|------------|--------|
| Direct Injection | "Ignore previous instructions..." | Pattern detection, guardrails | Deployed |
| Indirect Injection | Malicious content in AWS resources, diffs, observations | Response sanitization; model output untrusted; deterministic validation and hash-binding gate every write (OP-CD2) | Deployed (chat) / Planned (operations) |
| Jailbreak Attempts | Trying to bypass restrictions | Topic constraints, guardrails | Deployed |
| Role Play Attacks | "You are now a different AI..." | System prompt protection | Deployed |
| Proposal-as-authority | Treating a model proposal as a write decision | Model cannot approve or execute; approval is a direct authenticated action; executor re-verifies (OP-A1/AU2) | Planned |

### Data Poisoning

| Attack Vector | Description | Mitigation | Status |
|---------------|-------------|------------|--------|
| Knowledge Base Poisoning | Malicious documents in KB | Admin-only KB management | Deployed |
| Conversation History | Manipulated history | Server-side history management | Deployed |
| Observation Poisoning | Poisoned/stale provider state triggers remediation | Re-observe; fail closed on staleness/uncertainty (OP-AU1) | Planned |

## Risk Assessment

### High Risk Items

| Risk | Likelihood | Impact | Priority |
|------|------------|--------|----------|
| Prompt injection bypass (chat) | Medium | High | P1 |
| Credential exposure in responses | Low | Critical | P1 |
| Cost runaway from API abuse / autonomy loop | Medium | High | P2 |
| Provider-side duplicate write (operations) | Low | High | P2 |

### Medium Risk Items

| Risk | Likelihood | Impact | Priority |
|------|------------|--------|----------|
| Session fixation | Low | Medium | P2 |
| Self-approval policy misconfiguration | Low | High | P2 |
| Log data exposure | Low | Medium | P3 |
| Resource enumeration | Medium | Low | P3 |

### Low Risk Items

| Risk | Likelihood | Impact | Priority |
|------|------------|--------|----------|
| Timing attacks | Low | Low | P4 |
| Cache poisoning | Very Low | Low | P4 |

## Security Controls Summary

### Preventive Controls

- Input validation and sanitization
- Bedrock Guardrails (topic, content, PII)
- IAM least privilege policies (read-only deployed; narrow per-capability
  executor role planned)
- Separate read/write and chat-runtime/executor credentials (planned)
- Deterministic authorization decision service, one per system (planned)
- Approval bound to canonical prepared-operation hash with expiry (planned)
- Append-only ledger by mandatory conditional writes (planned)
- Hard autonomy limits and emergency disablement (planned)
- Cognito authentication; HTTPS/TLS

### Detective Controls

- CloudWatch Logs, CloudTrail audit logs, X-Ray tracing
- Append-only operations ledger as audit record of record (planned)
- Contract-parity, authorization-parity, and Step Functions approval-contract
  tests as release gates (planned)
- ECR vulnerability scanning; security test suite; public-content scanner

### Corrective Controls

- Auto-scaling for load
- Fail-closed recovery, reconciliation, and human escalation (planned)
- Circuit breaker, cooldown, and emergency disablement (planned)
- Incident response procedures

## Recommendations

### Immediate (P1)

1. **Rate Limiting**: Implement API rate limiting per user.
2. **WAF**: AWS WAF for additional protection.
3. **MFA**: Enable MFA for admin users.

### Short-term (P2)

1. **Cost Alerts**: Billing alerts and quotas.
2. **GuardDuty**: Enable for threat detection.
3. **Security Hub**: Aggregate security findings.

### Before enabling any operations write (P1 for the operations phase)

1. Per-capability IAM and separation-of-duties review (OP-P1/P2, OP-SC1).
2. Provider-idempotency-primitive verification per capability (OP-D1).
3. Emergency-disablement and autonomy-limit live exercises (OP-AU3/AU5).

## Review Schedule

- **Quarterly**: Review threat model for new threats.
- **After Major Changes**: Update for architecture changes; **update this
  document whenever an ADR or Operations Contract that it references changes.**
- **Before each operations phase**: Re-review the boundaries that phase enables.
- **Annually**: Full security assessment.

## Approval

| Role | Name | Date | Signature |
|------|------|------|-----------|
| Security Lead | | | Pending |
| Engineering Lead | | | Pending |
| Product Owner | | | Pending |

## Appendix A: Prompt Injection Attack Tree (chat path)

```
Prompt Injection [ROOT]
├── Direct Injection
│   ├── "Ignore instructions" patterns
│   │   └── [MITIGATED] Pattern detection
│   ├── System prompt extraction
│   │   └── [MITIGATED] Guardrails
│   └── Role override
│       └── [MITIGATED] Topic constraints
├── Indirect Injection
│   ├── Malicious resource names
│   │   └── [MITIGATED] Output sanitization
│   └── Poisoned KB documents
│       └── [MITIGATED] Admin-only KB
└── Bypass Techniques
    ├── Encoding attacks
    │   └── [MITIGATED] Input normalization
    └── Language tricks
        └── [PARTIAL] Guardrails + monitoring
```

## Appendix B: Data Flow Diagrams

### Authentication Flow

```
User -> Browser -> ECS Express (ALB) -> Cognito
                      |
                      v
                  JWT Token
                      |
                      v
                  HttpOnly Cookie
```

### Query Processing Flow (read-only chat path)

```
User Input
    |
    v
[Input Validation] --> [Reject if invalid]
    |
    v
[Guardrail Check] --> [Block if violated]
    |
    v
[Agent Processing]
    |
    v
[AWS API Calls] (Read-only)
    |
    v
[Response Sanitization]
    |
    v
[Output to User]
```

### Operations Write Flow (planned; default-disabled)

```
Prepare (untrusted proposal)
    |  trusted services select capability, bind identity/tenant/workspace,
    |  render diff, calculate risk + effective authority, store immutable op
    v
[Prepared operation + canonical hash]  (model output is proposal only)
    |
    v
Direct authenticated approval  --> ApprovalService binds op_id + hash, checks
    |                               policy/state/expiry/separation-of-duties
    v
[Approved]  (operation id is NOT a credential)
    |
    v
Durable workflow  --> sends ONLY operation id to the executor
    |
    v
Prepared executor (separate narrow role)
    |  reload + re-verify hash/approval/tenant/workspace/contract/executor binding
    |  single allowlisted provider action, hard bounds, idempotency primitive
    v
[Verify + record result / rollback]  --> fail closed + escalate on any uncertainty
    |
    v
Append-only ledger (authoritative audit)
```

## Version History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-01-12 | Security Eng | Initial draft |
| 2.2 | 2026-09-22 | Security Eng | Added the E4 Control-Plane Realization section (issue #416): the separate 08 AppConfig control-plane stack realizes the deployment-wide kill switch — AppConfig-enforced byte-equivalent schema validation, least-privilege authoring scoped to the exact AppConfig resources, opt-in read-only scoped data-plane access for E1/E2/E3 consumers, the reversible two-lever control-API disable plus immediate hard-down disable scripts, freshness fail-closed with a periodic sweeper, and gradual-strategy monitor auto-rollback. No Part I or Part II invariant weakened. |
| 2.1 | 2026-09-21 | Security Eng | Added the E3 Execution Realization section (issue #415): the separate 07 execution stack realizes the planned executor boundary — direct-invocation prevention via three separate roles (dispatcher/workflow/executor), operation_id-only invocation, no blind retry, the reversible two-lever emergency disable (OP-AU5), and fleet-ARN-scoped least privilege at the write edge. No Part I or Part II invariant weakened. |
| 2.0 | 2026-09-21 | Security Eng | Split into the deployed read-only chat path (Part I) and the optional, default-disabled operations control plane (Part II). Added trust boundaries O1-O8, per-boundary STRIDE, attack trees, and explicit residual risks for operations APIs, direct approval, immutable prepared operations, replay/stale-approval/cancellation, source-control prepare/executor, remote MCP clients, separate provider-write roles, bounded autonomy, emergency disablement, budget/authority limits, indirect prompt injection, confused deputy, audit integrity, and fail-closed recovery (issue #280). |
