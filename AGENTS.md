# AGENTS.md

Codex-facing guidance for this repository. Keep this file short and update `CLAUDE.md` when project architecture, commands, env vars, or governance rules change.

## First steps

- Run `git status --short --branch` before editing. The worktree may contain user changes; do not revert them unless explicitly asked.
- Read `CLAUDE.md` for the full project map, commands, architecture, testing markers, environment variables, and governance invariants.
- If present, read `tmp/GOVERNANCE-REMEDIATION.md` before governance or backlog work. It is local-only because `tmp/` is gitignored.

## Project shape

- Backend: Python 3.13, Strands Agents, Bedrock AgentCore Runtime under `backend/src/`.
- Frontend: Next.js, TypeScript, CopilotKit under `ui/src/`.
- Infrastructure: CloudFormation under `infrastructure/cloudformation/`, deployment scripts under `scripts/` and root wrapper scripts.
- Key backend files:
  - `backend/src/agentcore_main.py`: AgentCore runtime implementation.
  - `backend/agentcore_main.py`: deployment wrapper delegating to `backend/src/agentcore_main.py`.
  - `backend/src/agents/orchestrator.py`: routes requests to specialists.
  - `backend/src/agents/{gamelift,eks,cost}_specialist.py`: domain specialists.
  - `backend/src/agents/optimized_prompts.py`: managed prompt definitions.
  - `backend/src/config/settings.py`: `GBAW_*` configuration and inference settings.
  - `backend/src/utils/mcp_client_factory.py`: embedded MCP stdio client setup.

## Common commands

Backend, from `backend/`:

```bash
uv sync
uv run pytest -m unit
uv run pytest -m integration
uv run pytest -m cloud
```

Frontend, from `ui/`:

```bash
npm install
npm test
npm run lint
npm run test:e2e
```

Root helpers:

```bash
./test-unit.sh
./test-local.sh
./test-full.sh
./test-cloud.sh
./test-ai-evals.sh
./test-stress.sh
./scripts/check-code-quality.sh
```

Deployment:

```bash
./deploy-all.sh
./validate-deployment.sh
./deployment-status.sh
./teardown-all.sh
```

## Testing expectations

- Match test scope to risk. For docs-only changes, no runtime tests are usually needed; for backend logic, run targeted pytest or `./test-unit.sh`; for frontend work, run targeted Jest/lint/Playwright as appropriate.
- Backend pytest markers include `unit`, `integration`, `cloud`, `e2e`, `ai_eval`, `stress`, `mcp`, `fast`, `medium`, and `slow`.
- Unit tests auto-mock MCP clients via `backend/tests/conftest.py`. Integration and cloud tests use live AWS services.
- If tests are not run, state that clearly and say why.

## Dependency rules

- Backend source of truth: `backend/pyproject.toml` and `backend/uv.lock`.
- `backend/requirements.txt` is generated for CodeBuild runtime; do not hand-edit it.
- Frontend source of truth: `ui/package.json` and `ui/package-lock.json`.

## Governance invariants

- Required branch-protection checks must report on every PR type, including docs-only and lockfile-only dependency PRs.
- Prefer stable aggregate required contexts such as `codeql-required` or `policy-required` over raw matrix job names or default-tool contexts.
- Path-sensitive gates must use an always-reporting policy job that decides internally whether cloud tests, AI evals, stress tests, rollback notes, or docs updates are required.
- Keep maintainer bypass scoped to status-check recovery. Baseline PR review, CODEOWNERS review, conversation resolution, deletion, force-push, and direct-push protections should live in a separate ruleset with no routine bypass.
- Test branch-protection changes with a dependency-only PR before declaring governance complete.

## Current backlog handoff

- The current governance/product-backlog design is in `tmp/GOVERNANCE-REMEDIATION.md` when working in this local workspace.
- Immediate backlog priority from that doc: unblock governance with advanced CodeQL plus a stable `codeql-required` wrapper, split baseline protection from status-check protection, then update CODEOWNERS, PR template, CONTRIBUTING, and path-sensitive policy checks.
- `CLAUDE.md` already contains the durable governance invariants; keep it synchronized with any implemented backlog changes.
