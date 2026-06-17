# Game Agent - Threat Model

This document provides an initial threat model for Game Agent following the STRIDE methodology.

## Document Information

- **Version**: 1.0
- **Last Updated**: 2026-04-23
- **Status**: Approved
- **Author**: Security Engineering

## System Overview

### Purpose

Game Agent is an AI-powered conversational assistant for managing AWS game server infrastructure, specifically Amazon GameLift and Amazon EKS resources.

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
                                    +--------+   +--------+   +--------+
```

### Data Flow

1. User authenticates via Cognito
2. User sends query through Next.js frontend
3. Frontend calls Bedrock AgentCore Runtime
4. AgentCore invokes Orchestrator
5. Orchestrator routes to specialist agents
6. Specialists query AWS APIs via MCP servers
7. Response flows back through the chain

## Trust Boundaries

### Boundary 1: Internet to Application

- **Entry Points**: ECS Express ALB HTTPS endpoint
- **Exit Points**: API responses
- **Trust Level**: Untrusted

### Boundary 2: Frontend to Backend

- **Entry Points**: AgentCore Runtime API
- **Exit Points**: Agent responses
- **Trust Level**: Authenticated users

### Boundary 3: Application to AWS Services

- **Entry Points**: MCP server calls
- **Exit Points**: AWS API responses
- **Trust Level**: IAM-controlled

## STRIDE Analysis

### Spoofing

| Threat ID | Threat | Component | Mitigation | Status |
|-----------|--------|-----------|------------|--------|
| S1 | Unauthorized access via stolen credentials | Cognito Auth | MFA recommended, password policy enforced | Mitigated |
| S2 | JWT token theft | Frontend/Backend | HttpOnly cookies, short token lifetime | Mitigated |
| S3 | Session hijacking | User sessions | Secure cookie flags, session validation | Mitigated |
| S4 | API key exposure | MCP Servers | IAM roles (no static keys) | Mitigated |

### Tampering

| Threat ID | Threat | Component | Mitigation | Status |
|-----------|--------|-----------|------------|--------|
| T1 | Prompt injection to modify agent behavior | AI Backend | Guardrails, input validation, topic constraints | Mitigated |
| T2 | Request tampering | API Layer | HTTPS, request validation | Mitigated |
| T3 | Log tampering | CloudWatch | CloudWatch immutable logs | Mitigated |
| T4 | Configuration tampering | Infrastructure | CloudFormation managed, Git version control | Mitigated |

### Repudiation

| Threat ID | Threat | Component | Mitigation | Status |
|-----------|--------|-----------|------------|--------|
| R1 | Denial of malicious queries | User actions | CloudWatch logging, audit trail | Mitigated |
| R2 | Admin action denial | Administrative ops | CloudTrail logging | Mitigated |
| R3 | AWS API call denial | MCP operations | CloudTrail, X-Ray tracing | Mitigated |

### Information Disclosure

| Threat ID | Threat | Component | Mitigation | Status |
|-----------|--------|-----------|------------|--------|
| I1 | Sensitive data in AI responses | AI Backend | Bedrock Guardrails PII filters | Mitigated |
| I2 | AWS credentials exposure | MCP Servers | IAM roles, no static credentials | Mitigated |
| I3 | Log data leakage | CloudWatch | Log sanitization, access control | Mitigated |
| I4 | Conversation history exposure | Memory System | User-scoped sessions, encryption | Mitigated |
| I5 | Internal infrastructure details | AI Responses | Guardrails regex filters | Mitigated |
| I6 | Customer data cross-contamination | Multi-tenant | User-scoped sessions, separate contexts | Mitigated |

### Denial of Service

| Threat ID | Threat | Component | Mitigation | Status |
|-----------|--------|-----------|------------|--------|
| D1 | API flooding | ECS Express | Auto-scaling, WAF rate limiting | Partial |
| D2 | Large prompt attacks | AI Backend | Input length limits (32KB) | Mitigated |
| D3 | Resource exhaustion | MCP Servers | Connection pooling, timeouts | Mitigated |
| D4 | Cost exhaustion | AI Backend | Bedrock quotas, monitoring | Partial |

### Elevation of Privilege

| Threat ID | Threat | Component | Mitigation | Status |
|-----------|--------|-----------|------------|--------|
| E1 | User to Admin escalation | Cognito | Group-based authorization, admin-only user creation | Mitigated |
| E2 | Read to Write escalation | AWS APIs | Read-only IAM policies | Mitigated |
| E3 | Cross-tenant access | Multi-tenant | User-scoped sessions, IAM conditions | Mitigated |
| E4 | Agent tool abuse | AI Backend | Tool allowlists, limited capabilities | Mitigated |

## GenAI-Specific Threats

### Prompt Injection

| Attack Vector | Description | Mitigation |
|---------------|-------------|------------|
| Direct Injection | "Ignore previous instructions..." | Pattern detection, guardrails |
| Indirect Injection | Malicious content in AWS resources | Response sanitization |
| Jailbreak Attempts | Trying to bypass restrictions | Topic constraints, guardrails |
| Role Play Attacks | "You are now a different AI..." | System prompt protection |

### Data Poisoning

| Attack Vector | Description | Mitigation |
|---------------|-------------|------------|
| Knowledge Base Poisoning | Malicious documents in KB | Admin-only KB management |
| Conversation History | Manipulated history | Server-side history management |

### Model Manipulation

| Attack Vector | Description | Mitigation |
|---------------|-------------|------------|
| Token Exhaustion | Long prompts to waste tokens | Input length limits |
| Output Manipulation | Forcing specific outputs | Output validation |

## Risk Assessment

### High Risk Items

| Risk | Likelihood | Impact | Priority |
|------|------------|--------|----------|
| Prompt injection bypass | Medium | High | P1 |
| Credential exposure in responses | Low | Critical | P1 |
| Cost runaway from API abuse | Medium | High | P2 |

### Medium Risk Items

| Risk | Likelihood | Impact | Priority |
|------|------------|--------|----------|
| Session fixation | Low | Medium | P2 |
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
- IAM least privilege policies
- Network isolation (EKS NetworkPolicies)
- Cognito authentication
- HTTPS encryption

### Detective Controls

- CloudWatch Logs
- CloudTrail audit logs
- X-Ray distributed tracing
- ECR vulnerability scanning
- Security test suite

### Corrective Controls

- Auto-scaling for load
- Automatic secret rotation (recommended)
- Incident response procedures

## Recommendations

### Immediate (P1)

1. **Rate Limiting**: Implement API rate limiting per user
2. **WAF**: Consider AWS WAF for additional protection
3. **MFA**: Enable MFA for admin users

### Short-term (P2)

1. **Cost Alerts**: Set up billing alerts and quotas
2. **GuardDuty**: Enable for threat detection
3. **Security Hub**: Aggregate security findings

### Long-term (P3)

1. **Penetration Testing**: Annual third-party pentest
2. **Red Team Exercise**: AI-specific adversarial testing
3. **SOC 2**: Compliance certification if needed

## Review Schedule

- **Quarterly**: Review threat model for new threats
- **After Major Changes**: Update for architecture changes
- **Annually**: Full security assessment

## Approval

| Role | Name | Date | Signature |
|------|------|------|-----------|
| Security Lead | | | Pending |
| Engineering Lead | | | Pending |
| Product Owner | | | Pending |

## Appendix A: Attack Trees

### Prompt Injection Attack Tree

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

### Credential Theft Attack Tree

```
Credential Theft [ROOT]
├── Token Theft
│   ├── XSS attacks
│   │   └── [MITIGATED] HttpOnly cookies
│   └── Session hijacking
│       └── [MITIGATED] Secure flags
├── AWS Credential Exposure
│   ├── In responses
│   │   └── [MITIGATED] Guardrails + filters
│   ├── In logs
│   │   └── [MITIGATED] Log sanitization
│   └── Static credentials
│       └── [MITIGATED] IAM roles only
└── Knowledge Base Leakage
    └── [MITIGATED] Access controls
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

### Query Processing Flow

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

## Version History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-01-12 | Security Eng | Initial draft |
