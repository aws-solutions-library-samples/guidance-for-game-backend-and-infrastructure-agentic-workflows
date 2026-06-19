# Game Agent - Dependency Matrix

This document provides a comprehensive overview of major dependencies, their versions, purposes, and license information for security compliance.

## Overview

- **Last Updated**: 2026-01-12
- **Python Version**: >=3.13.9
- **Node.js Version**: 18+

## Backend Dependencies (Python)

### Core AI/ML Dependencies

| Package | Version | Purpose | License | Security Notes |
|---------|---------|---------|---------|----------------|
| strands-agents | >=1.19.0 | AI agent framework with OTEL support | Apache-2.0 | AWS managed |
| strands-agents-tools | >=0.2.17 | Agent tools library | Apache-2.0 | AWS managed |
| strands-agents-builder | >=0.1.10 | Agent builder utilities | Apache-2.0 | AWS managed |
| bedrock-agentcore | >=1.0.6 | AWS Bedrock AgentCore SDK | Apache-2.0 | AWS managed |
| bedrock-agentcore-starter-toolkit | >=0.1.33 | AgentCore starter utilities | Apache-2.0 | AWS managed |

### MCP (Model Context Protocol) Dependencies

| Package | Version | Purpose | License | Security Notes |
|---------|---------|---------|---------|----------------|
| mcp | >=1.23.0 | MCP client library | Apache-2.0 | Resolves GHSA-9h52-p55h-vw2f. Former <1.19.0 pin lifted (awslabs/mcp#1577 fixed in eks-mcp-server>=0.1.32) |
| awslabs.aws-api-mcp-server | >=1.3.45 | AWS API (CLI) MCP server — resource discovery | Apache-2.0 | Replaces YANKED ccapi-mcp-server (issue #92); log/workdir redirected to /tmp via env |
| awslabs.cost-explorer-mcp-server | >=0.0.12 | Cost Explorer MCP server | Apache-2.0 | AWS managed |
| awslabs.eks-mcp-server | >=0.1.32 | EKS MCP server | Apache-2.0 | Requires mcp>=1.23.0 (unpinned from 0.1.14) |

### AWS SDK Dependencies

| Package | Version | Purpose | License | Security Notes |
|---------|---------|---------|---------|----------------|
| boto3 | >=1.40.75 | AWS SDK for Python | Apache-2.0 | Keep updated |
| botocore | >=1.40.75 | Low-level AWS SDK | Apache-2.0 | Keep updated |
| httpx-auth-awssigv4 | >=0.1.4 | SigV4 signing for HTTP requests | MIT | Used for API authentication |

### Security & Authentication

| Package | Version | Purpose | License | Security Notes |
|---------|---------|---------|---------|----------------|
| PyJWT[crypto] | >=2.8.0 | JWT token handling | MIT | Includes cryptography extras |
| python-dotenv | >=1.1.1 | Environment variable management | BSD-3-Clause | Do not commit .env files |

### Observability

| Package | Version | Purpose | License | Security Notes |
|---------|---------|---------|---------|----------------|
| aws-opentelemetry-distro | >=0.14.0 | AWS OTEL distribution | Apache-2.0 | AWS managed |
| opentelemetry-instrumentation | >=0.54b0 | OTEL instrumentation | Apache-2.0 | - |
| loguru | >=0.7.3 | Structured logging | MIT | - |

### Utilities

| Package | Version | Purpose | License | Security Notes |
|---------|---------|---------|---------|----------------|
| pydantic | >=2.11.9 | Data validation | MIT | - |
| PyYAML | >=6.0.1 | YAML parsing | MIT | Use safe_load only |
| httpx | >=0.28.1 | HTTP client | BSD-3-Clause | - |
| psutil | >=6.1.0 | Process monitoring | BSD-3-Clause | - |

### Documentation Tools

| Package | Version | Purpose | License | Security Notes |
|---------|---------|---------|---------|----------------|
| requests | >=2.31.0 | HTTP requests | Apache-2.0 | - |
| beautifulsoup4 | >=4.12.0 | HTML parsing | MIT | - |
| html2text | >=2024.2.26 | HTML to text conversion | GPL-3.0 | - |
| pypdf | >=5.1.0 | PDF parsing | BSD-3-Clause | - |

### Development & Testing

| Package | Version | Purpose | License | Security Notes |
|---------|---------|---------|---------|----------------|
| pytest | >=8.4.2 | Testing framework | MIT | - |
| pytest-cov | >=7.0.0 | Code coverage | MIT | - |
| pytest-asyncio | >=1.2.0 | Async test support | Apache-2.0 | - |
| black | >=24.10.0 | Code formatting | MIT | - |
| isort | >=5.13.2 | Import sorting | MIT | - |
| mypy | >=1.13.0 | Type checking | MIT | - |
| pre-commit | >=4.0.1 | Git hooks | MIT | - |

## Frontend Dependencies (Node.js)

### Core Framework

| Package | Version | Purpose | License | Security Notes |
|---------|---------|---------|---------|----------------|
| next | ^15.5.7 | React framework | MIT | Keep updated (security fixes) |
| react | ^18.3.1 | UI library | MIT | - |
| react-dom | ^18.3.1 | React DOM bindings | MIT | - |

### AWS SDK for JavaScript

| Package | Version | Purpose | License | Security Notes |
|---------|---------|---------|---------|----------------|
| @aws-sdk/client-bedrock-agentcore | ^3.911.0 | AgentCore client | Apache-2.0 | AWS managed |
| @aws-sdk/client-bedrock-runtime | ^3.896.0 | Bedrock runtime | Apache-2.0 | AWS managed |
| @aws-sdk/client-cognito-identity-provider | ^3.911.0 | Cognito client | Apache-2.0 | AWS managed |
| @aws-sdk/client-cloudwatch | ^3.896.0 | CloudWatch client | Apache-2.0 | AWS managed |
| @aws-sdk/client-cost-explorer | ^3.898.0 | Cost Explorer client | Apache-2.0 | AWS managed |
| @aws-sdk/client-eks | ^3.896.0 | EKS client | Apache-2.0 | AWS managed |
| @aws-sdk/client-gamelift | ^3.896.0 | GameLift client | Apache-2.0 | AWS managed |
| @aws-sdk/client-sts | ^3.911.0 | STS client | Apache-2.0 | AWS managed |

### CopilotKit

| Package | Version | Purpose | License | Security Notes |
|---------|---------|---------|---------|----------------|
| @copilotkit/react-core | ^1.10.6 | AI copilot core | MIT | - |
| @copilotkit/react-ui | ^1.10.6 | AI copilot UI components | MIT | - |
| @copilotkit/shared | ^1.10.4 | Shared utilities | MIT | - |

### Observability (OpenTelemetry)

| Package | Version | Purpose | License | Security Notes |
|---------|---------|---------|---------|----------------|
| @opentelemetry/api | ^1.9.0 | OTEL API | Apache-2.0 | - |
| @opentelemetry/sdk-node | ^0.206.0 | OTEL Node.js SDK | Apache-2.0 | - |
| @opentelemetry/exporter-trace-otlp-http | ^0.206.0 | Trace exporter | Apache-2.0 | - |
| @opentelemetry/propagator-aws-xray | ^2.1.3 | X-Ray propagation | Apache-2.0 | AWS managed |

### Authentication

| Package | Version | Purpose | License | Security Notes |
|---------|---------|---------|---------|----------------|
| amazon-cognito-identity-js | ^6.3.15 | Cognito JS SDK | Apache-2.0 | AWS managed |
| aws-jwt-verify | ^5.1.1 | JWT verification | Apache-2.0 | AWS managed |
| cookie | ^1.0.2 | Cookie handling | MIT | HttpOnly cookies only |

### Development & Testing

| Package | Version | Purpose | License | Security Notes |
|---------|---------|---------|---------|----------------|
| @playwright/test | ^1.56.1 | E2E testing | Apache-2.0 | - |
| jest | ^30.1.3 | Unit testing | MIT | - |
| @testing-library/react | ^16.3.0 | React testing | MIT | - |
| typescript | 5.9.2 | Type checking | Apache-2.0 | - |
| eslint | 9.37.0 | Linting | MIT | - |

## License Summary

| License Type | Count | Notes |
|--------------|-------|-------|
| Apache-2.0 | 35+ | Most AWS SDKs and tools |
| MIT | 25+ | Common open source |
| BSD-3-Clause | 5+ | Utilities |
| GPL-3.0 | 1 | html2text (documentation only) |

## Security Recommendations

1. **Regular Updates**: Run `npm audit` and `pip-audit` weekly
2. **Version Pinning**: Pin versions for reproducible builds in CI/CD
3. **Vulnerability Scanning**: Enable GitHub Dependabot or similar
4. **License Compliance**: Review GPL-licensed packages for distribution requirements
5. **Supply Chain**: All packages are from trusted sources (npm, PyPI, AWS)

## Vulnerability Monitoring

- **Python**: Use `pip-audit` or `safety` for vulnerability scanning
- **Node.js**: Use `npm audit` for vulnerability scanning
- **CI/CD**: Integrate dependency scanning into build pipeline

## Update History

| Date | Changes |
|------|---------|
| 2026-01-12 | Initial security audit, updated Next.js to fix CVE |

---

## Related Documentation

- [Deployment Guide](DEPLOYMENT_GUIDE.md) — Full deployment steps and environment variable reference
