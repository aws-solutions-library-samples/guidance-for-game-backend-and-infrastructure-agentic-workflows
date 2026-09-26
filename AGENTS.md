# Repository Guidance

## Working Style

- Lead with the answer or action. Keep status updates concise and evidence-based.
- Before acting, verify the current directory, repository root, relevant paths,
  and applicable repository instructions.
- Use a plan for broad work and keep it current. Treat each session as one
  coherent outcome; start a new session when the objective changes.
- Ask before destructive operations, identity or permission changes, production
  mutations, or cleanup of resources you did not create.
- Preserve user changes and unrelated worktree state. Never rewrite history,
  delete branches, force-push, or discard files without explicit authorization.
- Contribute through your fork and a pull request (see `CONTRIBUTING.md`); treat
  any remote write as requiring an explicitly selected fork and task-specific
  approval.

## Repository Reality

This is a read-only game-infrastructure assistant, not a generic web service:

- `backend/src/agentcore_main.py` is the actual Python 3.13
  `BedrockAgentCoreApp` entrypoint. `backend/agentcore_main.py` is a thin
  compatibility wrapper. The backend is deployed as direct AgentCore code via
  CodeBuild; there is no FastAPI service. Importing the entrypoint runs startup
  work, not just handler definition: `prewarm_container()` initializes the
  cached Bedrock model, all three MCP clients, and configured Knowledge Base
  tools at module load. Pre-warm failures are logged and non-blocking; they must
  not hard-fail startup.
- `ui/` is a Next.js Pages Router application with CopilotKit. Its principal
  API boundary is `ui/src/pages/api/copilot/chat.ts`; locally it proxies to
  `localhost:8080`, while deployed it invokes AgentCore.
- The orchestrator delegates to GameLift, EKS, and Cost specialists in
  `backend/src/agents/`. GameLift uses scoped boto3 tools; EKS uses the AWS API
  and EKS MCP servers; Cost uses the Billing Cost Management MCP server plus
  deterministic Cost Explorer reports. MCP servers are embedded stdio
  subprocesses, not separately deployed services.
- The orchestrator and specialists occupy independent model roles resolved in
  `backend/src/config/settings.py` (`INFERENCE_CONFIG`). Each agent's model and
  inference settings are pinned per agent; do not assume a specialist inherits
  the orchestrator's model.
- Infrastructure is CloudFormation in `infrastructure/cloudformation/`.
  `scripts/deploy.sh` is the canonical deployment workflow; `deploy-all.sh`
  is only its wrapper. It deploys Cognito, Guardrails, managed prompts,
  AgentCore, observability, three Knowledge Bases, and, when Docker is
  available, the Next.js UI on ECS Express plus its security resources.
- The default chat path is provider-read-only. `backend/src/operations/` and
  `docs/adr/` define future operations contracts, but default deployments do
  not create an operations control plane or grant provider write permissions.
  Never add write permissions to the chat runtime.

For authorization or future operations work, read
`docs/IDENTITY_AND_AUTHORIZATION.md`, `docs/OPERATIONS_CONTRACTS.md`, and the
relevant ADRs before changing code.

## Important Boundaries

- Hosted requests require a verified Cognito access token at both the frontend
  proxy and runtime. Trusted tenant, workspace, and actor values are
  server-owned; never derive authorization from request-body fields, model
  output, or browser-supplied identity. When changing either side of this
  boundary, run the focused frontend API tests and `uv run pytest
  tests/unit/test_runtime_jwt_identity_unit.py
  tests/unit/test_agentcore_startup_unit.py`.
- `GBAW_ALLOW_LOCAL_IDENTITY_BYPASS` is a local-development-only setting made
  by `dev-start.sh`. Do not enable it in a hosted environment.
- `GBAW_ORCHESTRATOR_MODEL_ID` and `GBAW_SPECIALIST_MODEL_ID` are independent
  roles, not failover models. They take precedence over the legacy
  `GBAW_BEDROCK_MODEL_ID` and `GBAW_BEDROCK_MODEL_ID_SECONDARY` aliases.
- The source prompt definitions are
  `backend/src/agents/optimized_prompts.py`. Production consumes published
  Bedrock Prompt Management versions. `scripts/infrastructure/deploy-prompts.sh`
  publishes managed versions and writes their ARNs to `backend/.env.local`, but
  it does not update a running AgentCore runtime; the runtime sees a new version
  only after the full deployment passes those ARNs to
  `agentcore launch --auto-update-on-conflict`. Do not describe a production
  prompt change as effective until a runtime update or full deployment has run.
- Each specialist has its own Bedrock Knowledge Base. Infrastructure creation
  alone is insufficient: source documents must be seeded before retrieval can
  return results.
- Cost totals and service breakdowns come from the owned deterministic Cost
  Explorer rendering path, not from unvalidated model prose. Preserve that path
  when changing cost behavior and cover it with
  `backend/tests/unit/test_cost_report_unit.py` and the focused cost-report
  integration tests.
- The AgentCore container runs with a read-only home and working directory. The
  AWS API MCP server writes a log under `$HOME` and needs a writable working
  dir, so `backend/src/utils/mcp_client_factory.py` redirects `HOME` and
  `AWS_API_MCP_WORKING_DIR` to a writable path and sets `READ_OPERATIONS_ONLY`.
  Configure this in the MCP client environment; do not patch the Dockerfile.

## Configuration and Generated Files

- Local backend settings load from `ui/.env.local`; process environment values
  take precedence. Start from `ui/.env.local.example`. Do not commit real
  environment files or credentials.
- `backend/.env.local` is deployment state written by the prompt and Knowledge
  Base scripts. It contains prompt ARNs and KB IDs; treat it as generated,
  sensitive local state.
- `backend/pyproject.toml` and `backend/uv.lock` are the backend dependency
  source of truth. `backend/requirements.txt` is generated by `deploy.sh` for
  AgentCore and must not be hand-edited.
- `.bedrock_agentcore/`, `.bedrock_agentcore.yaml`, test reports, coverage,
  and SBOMs are generated artifacts. Do not edit or commit them.

## Focused Commands

Run commands from the directory shown:

```bash
# backend/
uv sync
uv run pytest tests/unit/
uv run pytest tests/integration/ -m "not slow"   # deployed services required

# ui/
npm install
npm test -- --watchAll=false
npm run lint

# repository root
./test-unit.sh                    # script checks + backend unit + frontend Jest
./test-local.sh                   # unit suites and localhost-tagged Playwright tests
./scripts/check-code-quality.sh   # pre-commit hooks over the repository
```

`./test-unit.sh` writes `backend/htmlcov/`. Run focused tests while iterating,
then the relevant lint or quality checks for the files changed.

`./test-e2e.sh` and `./test-full.sh` may start local services. `test-full.sh`
selects deployed tests when it detects a stack; deployed integration, AI-eval,
and browser checks require valid AWS configuration and the documented test
credentials/tokens. `./test-cloud.sh`, `./test-ai-evals.sh`, and
`./test-stress.sh` target live AWS resources—do not run them casually.

## Implementation Conventions

- Python lives in `backend/src/`; tests are organized under
  `backend/tests/{unit,integration,ai_evals,performance}`. Pytest markers and
  the 30-second default timeout are defined in `backend/pytest.ini`.
- Keep Python imports grouped as standard library, third-party packages, and
  local modules, each under its configured isort heading comment
  (`# Standard library`, `# Third-party packages`, `# Local modules`). Black and
  isort use a 120-character line limit; mypy is configured in the backend
  project.
- Frontend source and Jest tests live in `ui/src/`; Playwright tests live in
  `ui/tests/`. The `@/` alias maps to `ui/src/`.
- Repository script and documentation regression checks live in
  `scripts/test/test_*.py` and run via `python3 -m unittest discover -s
  scripts/test` (also covered by `./test-unit.sh`). Add a focused check here
  when a script or tracked-file invariant must not regress.
- For deployment changes, update CloudFormation and the matching shell and
  PowerShell paths where the repository intentionally supports both. Use
  `scripts/infrastructure/check-deployment.sh` and
  `validate-deployment.sh` for read-only deployed-state checks.
- Treat examples, tests, documentation, logs, and generated diagnostics as
  public content. Use synthetic identifiers and never expose credentials,
  customer data, JWTs, account IDs, or deployment-specific ARNs.

## Completion

- State what changed, what was verified, and any unresolved risk.
- Do not claim deployment success from a source diff. When deployment is in
  scope, verify the relevant stack/runtime state and, where appropriate, the
  Knowledge Base retrieval and authenticated request path.
