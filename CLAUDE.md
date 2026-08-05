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
- **Specialist agents** (`agents/{gamelift,eks,cost}_specialist.py`) — Domain experts using Claude Sonnet 4.6, created via `base_specialist.py` factory (`create_specialist_agent()`). Each agent's `model_id`/temperature is pinned per-agent in `settings.INFERENCE_CONFIG`; without explicit pinning every agent silently inherited the orchestrator's Haiku model
- **MCP servers** — Each specialist declares its servers via `mcp_server_names`: EKS uses `aws-api-mcp-server` (account-wide resource discovery via `call_aws`) + `eks-mcp-server` (in-cluster ops); Cost uses `billing-cost-management-mcp-server`; GameLift uses boto3 directly (no MCP). Servers run as embedded stdio subprocesses; a module-level thread-safe cache in `utils/mcp_client_factory.py` reuses clients across calls, with automatic fallback to boto3 if MCP is unavailable

### Startup Pre-warming
`agentcore_main.py` initializes Bedrock model singleton, all 3 MCP clients, and KB tools at module load time (not per-request). This cuts first-request latency by 2–4s. Failures are logged as debug and don't block startup.

### Key Backend Modules
- `config/model_settings.py` — Canonical Haiku-orchestrator/Sonnet-specialist defaults and canonical-over-legacy environment precedence
- `config/settings.py` — Runtime configuration, loads from `ui/.env.local` locally and maps resolved role models into `INFERENCE_CONFIG`; global boto3 config uses adaptive retries (max 3 attempts)
- `models/cached_bedrock.py` — Bedrock initialization with prompt/tool caching and Guardrails; role models are not implicit fallbacks for one another
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
`scripts/deploy.sh` runs an ordered, idempotent sequence (templates in `infrastructure/cloudformation/`, stacks prefixed `game-agent-`). Re-running is safe — each step skips or updates in place:
1. **solution-tracking** — Solution ID (SO9693) stack for deployment metrics
2. **infrastructure** (base) — Cognito, IAM, ECR
3. **guardrails** — Bedrock Guardrails (the returned `GuardrailId` is wired into the runtime)
4. **Managed Prompts** — `scripts/infrastructure/deploy_prompts.py` publishes the four agent prompts to Bedrock Prompt Management; it compares text, role model, and inference settings before publishing a new version and writes prompt ARNs to `backend/.env.local`
5. **AgentCore Runtime** — direct-code deploy via CodeBuild (ARM64, no FastAPI wrapper); initial launches and updates receive the resolved `GBAW_ORCHESTRATOR_MODEL_ID` and `GBAW_SPECIALIST_MODEL_ID` together with available KB, Guardrail, and prompt settings
6. **observability** — account-wide CloudWatch/X-Ray trace delivery
7. **Knowledge Bases** (GameLift, EKS, Cost) — deployed then seeded
8. **frontend** — ECS Express (managed Fargate + ALB)
9. **security** — WAF on ALB, CloudTrail (KMS-encrypted logs), Inspector ECR scanning

**Managed prompts are the source of truth in prod**: editing `optimized_prompts.py` alone is a no-op until re-seeded, but `./deploy-all.sh` re-seeds automatically (step 4). Knowledge Base IDs are wired to the runtime via env vars after stack creation; run `scripts/infrastructure/seed-kb-{gamelift,eks,cost}.sh` after KB stack creation to populate them — querying an unseeded KB returns empty results without errors.

**EKS enrollment** is optional: `infrastructure/kubernetes/enroll-cluster.sh` configures read-only RBAC (pods, deployments, services; secrets excluded) and updates aws-auth ConfigMap.

**Container note**: `aws-api-mcp-server` (the EKS specialist's resource-discovery server, replacing the yanked `ccapi-mcp-server`) writes a log under `$HOME` and needs a writable working dir — both read-only in the container. `utils/mcp_client_factory.create_mcp_client` redirects `HOME` and `AWS_API_MCP_WORKING_DIR` to `/tmp` (and sets `READ_OPERATIONS_ONLY=true`) via the server's environment — no Dockerfile patch needed.

## Code Style

### Python
- **Formatter**: Black, 120 char line length, Python target pinned `>=3.13.9,<3.14` (the upper bound stops uv from solving impossible 3.14 dependency splits)
- **Imports**: isort with Black profile; sections ordered: stdlib → third-party → local; each section has a heading comment (e.g., `# Standard library`, `# Third-party packages`, `# Local modules`)
- **Type checking**: mypy (lenient — `disallow_untyped_defs = false`, `ignore_missing_imports = true`)
- **Dependencies**: `pyproject.toml` + `uv.lock` are the source of truth. `backend/requirements.txt` is **generated** (`uv export --no-dev --no-hashes`, run by `deploy.sh`) for the CodeBuild runtime — never hand-edit it; change `pyproject.toml` and re-lock instead

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
| `GBAW_ORCHESTRATOR_MODEL_ID` | Haiku 4.5 global profile | Canonical orchestrator role; overrides `GBAW_BEDROCK_MODEL_ID` |
| `GBAW_SPECIALIST_MODEL_ID` | Sonnet 4.6 global profile | Canonical GameLift/EKS/Cost role; overrides `GBAW_BEDROCK_MODEL_ID_SECONDARY` |
| `GBAW_USE_BEDROCK_SESSIONS` | `true` | Enables cross-session memory via AgentCore |
| `GBAW_MEMORY_LONG_TERM_ENABLED` | `true` | Long-term user memory (30-day TTL) |
| `GBAW_MEMORY_REQUIRED` | `false` | If `true`, hard-fails when memory unavailable |
| `GBAW_AGENT_MAX_TURNS_ORCHESTRATOR` | `15` | Loop guard — prevents runaway Bedrock spend |
| `GBAW_AGENT_MAX_TURNS_SPECIALIST` | `10` | Loop guard per specialist |
| `GBAW_AGENT_TIMEOUT_REQUEST_SECONDS` | `180` | Hard wall-clock timeout per request |
| `GBAW_RATE_LIMIT_MAX_REQUESTS` | `10` | Per-user requests per window |
| `GBAW_RATE_LIMIT_WINDOW_SECONDS` | `60` | Rate limit window |
