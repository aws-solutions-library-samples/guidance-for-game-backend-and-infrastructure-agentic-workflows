# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI-powered game server management platform using AWS Bedrock AgentCore Runtime with Strands Agents. An orchestrator agent routes natural language queries to specialist agents (GameLift, EKS, Cost) that use MCP servers and boto3 tools to interact with AWS services. Frontend is Next.js + CopilotKit; backend is Python 3.13 + Strands Agents.

## Common Commands

### Backend (from `backend/`)
```bash
uv sync                              # Install/update dependencies
uv run pytest -m unit                # Run unit tests (fast, no AWS needed)
uv run pytest -m integration         # Integration tests (needs AWS services)
uv run pytest -m cloud               # Cloud tests (needs deployed stack)
uv run pytest tests/unit/test_foo.py # Run a single test file
uv run pytest -k "test_name"         # Run tests matching a name pattern
```

### Frontend (from `ui/`)
```bash
npm install                          # Install dependencies
npm test                             # Jest unit tests
npm run test:coverage                # Coverage report
npm run test:e2e                     # Playwright E2E tests
npm run lint                         # ESLint
npm run dev                          # Dev server on port 3000
```

### Full Stack
```bash
./dev-start.sh                       # Start backend (8080) + frontend (3000)
./dev-stop.sh                        # Stop all services
./test-unit.sh                       # All unit tests (backend + frontend)
./test-local.sh                      # Local integration tests
./test-full.sh                       # Full test suite (auto-detects deployment)
./test-cloud.sh                      # Cloud tests (needs deployed stack)
./test-ai-evals.sh                   # AI evaluation tests
./test-stress.sh                     # Stress/performance tests
```

### Deployment
```bash
./deploy-all.sh                      # Full stack deployment (runs scripts/deploy.sh)
./validate-deployment.sh             # Validate deployed stack is healthy
./deployment-status.sh               # Check current deployment status
./teardown-all.sh                    # Tear down all stacks
```

### Code Quality
```bash
./scripts/check-code-quality.sh      # Run all pre-commit checks
uv run --directory backend pre-commit run --all-files  # Same, manual
```

## Architecture

### Agent Hierarchy
- **Orchestrator** (`agents/orchestrator.py`) — Routes queries to specialists, uses Claude Haiku 4.5 for fast classification; optionally uses `AgentCoreMemorySessionManager` for session + long-term memory (controlled by `GBAW_USE_BEDROCK_SESSIONS`)
- **Specialist agents** (`agents/{gamelift,eks,cost}_specialist.py`) — Domain experts using Claude Sonnet 4.5, created via `base_specialist.py` factory (`create_specialist_agent()`)
- **MCP servers** — EKS, Cost Explorer, and CCAPI servers run as embedded stdio subprocesses; module-level thread-safe cache in `utils/mcp_client_factory.py` reuses clients across calls; automatic fallback to boto3 if MCP unavailable

### Startup Pre-warming
`agentcore_main.py` initializes Bedrock model singleton, all 3 MCP clients, and KB tools at module load time (not per-request). This cuts first-request latency by 2–4s. Failures are logged as debug and don't block startup.

### Key Backend Modules
- `config/settings.py` — All configuration via env vars (prefixed `GBAW_`), loads from `ui/.env.local` locally; global boto3 config sets adaptive retry mode (max 3 attempts)
- `models/cached_bedrock.py` — Bedrock model initialization with caching
- `utils/security.py` — Input validation, rate limiting, sanitization
- `utils/kb_tools.py` — Bedrock Knowledge Base retrieval tools (GameLift, EKS, Cost KBs)
- `utils/wall_clock_timeout_hook.py` — Strands hook enforcing `GBAW_AGENT_TIMEOUT_REQUEST_SECONDS`
- `utils/max_turns_hook.py` — Strands hook enforcing `GBAW_AGENT_MAX_TURNS_*` limits
- `agents/optimized_prompts.py` — Prompt caching setup; edit here to tune agent system prompts
- `agentcore_main.py` — Runtime entrypoint, uses `BedrockAgentCoreApp`

### Frontend
- Next.js with CopilotKit for chat UI
- API routes in `pages/api/` proxy to backend
- Auth via Amazon Cognito (skippable in dev with `NEXT_PUBLIC_SKIP_AUTH=true`)

### Infrastructure
Five CloudFormation stacks deployed in order via `scripts/deploy.sh`:
1. **Base** — Cognito, IAM, ECR
2. **Guardrails** — Bedrock Guardrails
3. **AgentCore Runtime** — deployed via CodeBuild direct-code (no FastAPI wrapper); prompt ARNs written to `backend/.env.local` then passed as env vars
4. **Frontend** — ECS Express (managed Fargate + ALB)
5. **Security** — WAF on ALB, CloudTrail, Inspector ECR scanning

Knowledge Bases (GameLift, EKS, Cost) are deployed and seeded separately; KB IDs are wired to the runtime via env vars after stack creation. Run `scripts/infrastructure/seed-kb-{gamelift,eks,cost}.sh` after KB stack creation to populate them — querying an unseeded KB returns empty results without errors.

**EKS enrollment** is optional: `infrastructure/kubernetes/enroll-cluster.sh` configures read-only RBAC (pods, deployments, services; secrets excluded) and updates aws-auth ConfigMap.

**Container note**: `ccapi-mcp-server` writes to `.schemas` at a path that's read-only in the default container image. The deploy script patches this by creating a writable directory during the Docker image build.

## Code Style

### Python
- **Formatter**: Black, 120 char line length, Python 3.13 target
- **Imports**: isort with Black profile; sections ordered: stdlib → third-party → local; each section has a heading comment (e.g., `# Standard library`, `# Third-party packages`, `# Local modules`)
- **Type checking**: mypy (lenient — `disallow_untyped_defs = false`, `ignore_missing_imports = true`)

### TypeScript/JavaScript
- ESLint with `next/core-web-vitals` and `next/typescript` configs
- Path alias: `@/` maps to `src/`

### Pre-commit Hooks
Configured in `.pre-commit-config.yaml`: Black, isort, mypy (src only), ESLint (with auto-fix), trailing whitespace, YAML checks, large file detection, private key detection.

## Testing

Backend pytest markers: `unit`, `integration`, `cloud`, `e2e`, `ai_eval`, `stress`, `mcp`, `fast`, `medium`, `slow`. Default timeout is 30s (autouse fixture — applies to all markers). Tests are in `backend/tests/{unit,integration,ai_evals,performance}/`.

Unit tests auto-mock MCP clients via `conftest.py` fixtures; `integration` and `cloud` tests do not — they require live AWS services.

Frontend Jest tests in `ui/src/__tests__/`; Playwright E2E tests in `ui/tests/`.

## Environment

Backend config reads env vars from `ui/.env.local` when running locally. Create it with `cp ui/.env.local.example ui/.env.local`. Key env vars use `GBAW_` prefix.

Notable vars not in the example file:

| Var | Default | Notes |
|-----|---------|-------|
| `GBAW_USE_BEDROCK_SESSIONS` | `true` | Enables cross-session memory via AgentCore |
| `GBAW_MEMORY_LONG_TERM_ENABLED` | `true` | Long-term user memory (30-day TTL) |
| `GBAW_MEMORY_REQUIRED` | `false` | If `true`, hard-fails when memory unavailable |
| `GBAW_AGENT_MAX_TURNS_ORCHESTRATOR` | `15` | Loop guard — prevents runaway Bedrock spend |
| `GBAW_AGENT_MAX_TURNS_SPECIALIST` | `10` | Loop guard per specialist |
| `GBAW_AGENT_TIMEOUT_REQUEST_SECONDS` | `180` | Hard wall-clock timeout per request |
| `GBAW_RATE_LIMIT_MAX_REQUESTS` | `10` | Per-user requests per window |
| `GBAW_RATE_LIMIT_WINDOW_SECONDS` | `60` | Rate limit window |
