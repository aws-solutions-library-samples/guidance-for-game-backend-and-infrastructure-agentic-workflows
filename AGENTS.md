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
- Use pull requests for repository changes and follow `CONTRIBUTING.md` for
  external contributions. On an explicitly scoped maintainer branch or existing
  pull request, normal commits and non-force pushes to the verified task branch
  are permitted. Before pushing, verify that the repository, remote, and branch
  match the current task. Ask before using another remote target, force-pushing,
  rewriting remote history, or changing branch protections.

## Repository Reality

This is a read-only game-infrastructure assistant, not a generic web service.
Treat the checked-out source, CloudFormation, and deployment scripts as runtime
truth. Historical or locally generated review artifacts can describe revisions
not present in this checkout and must not be treated as deployed architecture.

- `backend/src/agentcore_main.py` is the actual Python 3.13
  `BedrockAgentCoreApp` entrypoint. `backend/agentcore_main.py` is a thin
  compatibility wrapper. The backend is deployed as direct AgentCore code via
  CodeBuild; there is no FastAPI service. Importing the entrypoint checks AWS
  credentials and pre-warms the model, MCP clients, and configured Knowledge
  Base tools. Credential and pre-warm failures must not crash startup; the
  request handler rechecks unavailable credentials before admitting AWS work.
- `ui/` is a Next.js Pages Router application with CopilotKit. Its principal
  API boundary is `ui/src/pages/api/copilot/chat.ts`; locally it proxies to
  `localhost:8080`, while deployed it invokes the JWT-authorized AgentCore
  runtime and forwards the verified access token only in `Authorization`.
- The orchestrator delegates to GameLift, EKS, and Cost specialists in
  `backend/src/agents/`. GameLift uses scoped boto3 tools; EKS uses the AWS API
  and EKS MCP servers; Cost uses the Billing Cost Management MCP server plus
  deterministic Cost Explorer reports. MCP servers are embedded stdio
  subprocesses, not separately deployed services.
- Hosted deterministic cost reports persist immutable, validated, TTL-bounded
  snapshots in the base stack's `CostReportSnapshotTable`. Snapshot reuse is
  bound to the trusted actor, tenant, and workspace; hosted runtimes fail
  closed without the shared store, while local development and tests may use
  the in-memory fallback.
- Infrastructure is CloudFormation in `infrastructure/cloudformation/`.
  `scripts/deploy.sh` is the canonical deployment workflow; `deploy-all.sh`
  is only its wrapper. It downloads Knowledge Base documents; deploys solution
  tracking, base infrastructure, Guardrails, managed prompts, AgentCore, and
  observability; then deploys, seeds, and wires the three Knowledge Bases.
  With Docker available, it also builds a uniquely tagged frontend image and
  deploys ECS Express plus the security resources.
- The default chat path has no mutating GameLift, EKS, or Cost APIs. Its narrow
  DynamoDB `GetItem`/`PutItem` access is only for shared cost-report snapshots;
  AgentCore memory and observability writes are likewise not provider-control
  permissions. Never add provider-write permissions to the chat runtime.
- `backend/src/operations/` currently contains versioned contracts,
  canonical validation, protocol-neutral identity/approval primitives, and an
  E0 validation harness with an opt-in DynamoDB persistence sink for disposable
  evidence tables. It does not supply a deployed operations API, production
  durable store, executor, public MCP facade, or AgentCore Gateway. The default
  deployment creates none of those resources.

`AGENTS.md` is the sole tracked agent instruction file. Use
`docs/ARCHITECTURE.md` and `docs/DEPLOYMENT_GUIDE.md` for broader context. For
authorization or future operations work, also read
`docs/IDENTITY_AND_AUTHORIZATION.md`, `docs/OPERATIONS_CONTRACTS.md`, and the
relevant ADRs before changing code. For an exact runtime permission, request
path, or deployment behavior, verify the corresponding source file,
CloudFormation policy, and shell/PowerShell workflow rather than relying on
overview or review prose.

## Important Boundaries

- Hosted requests require a verified Cognito access token at both the frontend
  proxy and runtime. The runtime independently verifies the bearer access
  token before admitting its subject, client, groups, or scopes; an ID token
  is presentation data only. Trusted tenant, workspace, and actor values are
  server-owned; never derive authorization from request-body fields, model
  output, browser-supplied identity, or custom passthrough headers.
- Local identity bypasses have explicit, separate guards: the frontend requires
  non-production `NODE_ENV` and `NEXT_PUBLIC_SKIP_AUTH=true`, while the runtime
  requires `GBAW_ALLOW_LOCAL_IDENTITY_BYPASS=true` and
  `GBAW_HOSTED_RUNTIME=false`. `scripts/dev/start.sh` sets the runtime flag.
  Do not enable or emulate either bypass in a hosted environment.
- `GBAW_ORCHESTRATOR_MODEL_ID` and `GBAW_SPECIALIST_MODEL_ID` are independent
  roles, not failover models. They take precedence over the legacy
  `GBAW_BEDROCK_MODEL_ID` and `GBAW_BEDROCK_MODEL_ID_SECONDARY` aliases.
  Defaults live in `backend/src/config/model_settings.py`; per-agent settings
  live in `config.settings.INFERENCE_CONFIG`. Model construction in
  `backend/src/models/cached_bedrock.py` applies
  `backend/src/config/model_capabilities.py`; preserve model-specific
  temperature and thinking behavior. The current Claude 5.5 defaults require
  `global.` system inference profiles; application inference profiles over
  those models remain unsupported by the capability resolver.
- The source prompt definitions are
  `backend/src/agents/optimized_prompts.py`. Production consumes published
  Bedrock Prompt Management versions.
  `scripts/infrastructure/deploy-prompts.sh` publishes versions and writes their
  ARNs to `backend/.env.local`; it does not update the running runtime. A runtime
  update must pass those ARNs to `agentcore launch --auto-update-on-conflict`,
  as the full deployment does, before hosted behavior changes.
- Each specialist has its own Bedrock Knowledge Base. Infrastructure creation
  alone is insufficient: source documents must be seeded before retrieval can
  return results.
- EKS enrollment is optional and deliberately read-only: retain the exclusion
  of Kubernetes secrets and mutating verbs. Do not relax MCP read-only
  configuration or cluster RBAC to work around an unsupported chat request.
- The Cost specialist must render monetary figures from the owned,
  deterministic Cost Explorer snapshot path. Do not let model-authored prose
  replace validated report totals, rankings, or percentages.
- AgentCore's hosted filesystem is read-only outside writable temporary paths.
  `backend/src/utils/mcp_client_factory.py` redirects AWS API MCP's `HOME` and
  working directory, and Billing MCP's logs and SQLite session database, to
  writable temporary storage. Preserve `READ_OPERATIONS_ONLY` and the EKS/Cost
  tool guards when changing subprocess startup.
- The orchestrator derives AgentCore Memory's actor ID through
  `backend/src/utils/memory_identity.py`, using a domain-separated SHA-256 digest
  of the trusted user identifier. Never send a raw Cognito subject to the memory
  service. Changing this digest contract changes the user's memory namespace;
  existing raw-subject records require the explicit cleanup runbook in
  `docs/DEPLOYMENT_GUIDE.md`.

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
- `ui/package.json` and `ui/package-lock.json` are the frontend dependency
  source of truth. Keep the lockfile synchronized with intentional dependency
  changes.
- Hosted cost snapshots require `GBAW_COST_SNAPSHOT_TABLE_NAME` and
  `GBAW_COST_SNAPSHOT_REQUIRED=true`, wired by the deployment workflow. Do not
  weaken the hosted fail-closed behavior into a process-local fallback.
- `.bedrock_agentcore/`, `.bedrock_agentcore.yaml`, test reports, coverage,
  and SBOMs are generated artifacts. Do not edit or commit them.

## Focused Commands

Run commands from the directory shown:

```bash
# backend/
uv sync                          # requires Python >=3.13.9,<3.14
uv run pytest tests/unit/
uv run pytest tests/integration/ -m "not slow"   # inspect markers; may call AWS

# ui/
npm install
npm test -- --watchAll=false
npm run lint

# repository root
python3 -m unittest discover -s scripts/test -p 'test_*.py'
python3 scripts/check_public_content.py AGENTS.md  # substitute changed text files
./test-unit.sh                    # script checks + backend unit + frontend Jest
./test-local.sh                   # unit suites and localhost-tagged Playwright tests
./test-powershell.sh              # PowerShell module changes; Pester may install per-user
./scripts/check-code-quality.sh   # pre-commit hooks over the repository
```

`./test-unit.sh` writes `backend/htmlcov/`. Run focused tests while iterating,
then the relevant lint or quality checks for the files changed.

`./test-e2e.sh` and `./test-full.sh` may start local services. `test-full.sh`
selects deployed tests when it detects a stack; deployed integration, AI-eval,
and browser checks require valid AWS configuration and the documented test
credentials/tokens. `./test-cloud.sh`, `./test-ai-evals.sh`, and
`./test-stress.sh` target live AWS resources—do not run them casually.
`./test-memory.sh` explicitly starts and stops local services. Do not run
`./test-powershell.sh` if installing Pester for the current user is outside
the requested scope.

The dev start/stop scripts can terminate existing processes; verify ownership
before using them. Local backend execution still requires AWS credentials for
Bedrock and provider calls. Its health endpoint is `/ping` on port 8080; the
frontend health endpoint is `/api/health` on port 3000.

## Implementation Conventions

- Python lives in `backend/src/`; tests are organized under
  `backend/tests/{unit,integration,ai_evals,performance}`. Pytest markers and
  the 30-second default timeout are defined in `backend/pytest.ini`.
- Keep Python imports grouped as standard library, third-party packages, and
  local modules, with the configured isort heading comments
  (`# Standard library`, `# Third-party packages`, `# Local modules`). Black and
  isort use a 120-character line limit; mypy is configured in the backend
  project. Root `pyproject.toml` covers repository Python tooling.
- Frontend source and Jest tests live in `ui/src/`; Playwright tests live in
  `ui/tests/`. The `@/` alias maps to `ui/src/`.
- Repository script and documentation regression checks live in
  `scripts/test/test_*.py` and use standard-library `unittest`; they also run in
  `./test-unit.sh`. Prefer focused checks here for script or tracked-file
  invariants. `scripts/test/test_agent_guidance_docs.py` enforces consolidated
  agent guidance.
- For deployment changes, update CloudFormation and the matching shell and
  PowerShell paths where the repository intentionally supports both. Use
  `scripts/infrastructure/check-deployment.sh` and
  `validate-deployment.sh` for read-only deployed-state checks.
- Treat examples, tests, documentation, logs, and generated diagnostics as
  public content. Use synthetic identifiers and never expose credentials,
  customer data, JWTs, account IDs, or deployment-specific ARNs.
- The production deployment resolves model roles and tenant/workspace bindings
  through `config/load_deployment_settings.py`, then passes the Cognito,
  prompt, Knowledge Base, and cost-snapshot settings to AgentCore. Keep
  `scripts/deploy.sh` and
  `scripts/powershell/Private/New-GameAgentAgentCoreEnvArgs.ps1` aligned when
  changing that environment contract; Cognito values are appended by
  `scripts/powershell/Public/Deploy-GameAgent.ps1`.

## Change Impact Map

- For an authenticated chat-path change, trace both sides of the boundary:
  `ui/src/pages/api/copilot/chat.ts` verifies the access token and builds the
  trusted principal; `backend/src/agentcore_main.py` verifies the runtime
  context. Preserve the rule that body fields, browser headers, and model
  output cannot establish actor, tenant, workspace, group, or authorization
  values. Run the focused frontend API tests and
  `uv run pytest tests/unit/test_runtime_jwt_identity_unit.py tests/unit/test_agentcore_startup_unit.py`.
- For an agent behavior or prompt change, inspect the relevant specialist,
  orchestrator, `backend/src/agents/optimized_prompts.py`, and its focused
  tests. Hosted prompt changes require both managed-version publication and a
  runtime update with the published ARNs; follow the boundary above.
- For a provider integration, project model-visible responses into bounded,
  code-owned schemas. Preserve truthful empty, denied, partial, and truncated
  states, and keep raw provider payloads and exception text out of model context
  and logs. Check the relevant projection/guard code and tests rather than
  assuming the status in `docs/PROVIDER_RESPONSE_INVENTORY.md` is current.
- For deterministic cost-report behavior, preserve the owned Cost Explorer
  rendering path rather than allowing financial figures from unvalidated model
  prose. Use `backend/tests/unit/test_cost_report_unit.py` and the focused
  cost-report integration tests when changing this path.
- For shared cost-snapshot changes, trace `cost_report_scope.py`,
  `cost_snapshot_*.py`, the base-stack table/IAM policy, and both deployment
  environment builders. Preserve immutable writes, TTL bounds, trusted scope
  binding, and the hosted fail-closed requirement. Run
  `test_cost_snapshot_store_unit.py`, `test_cost_snapshot_cross_worker_unit.py`,
  and `test_cost_snapshot_iac_unit.py`.
- For operations-contract changes, update the versioned schemas, fixtures,
  implementation, contract tests, and the corresponding documentation or ADR
  together. Read `docs/IDENTITY_AND_AUTHORIZATION.md`,
  `docs/OPERATIONS_CONTRACTS.md`, and the relevant ADR before modifying the
  operations layer; it remains an optional, disabled control-plane design and
  must not broaden chat-runtime permissions.
- For deployment workflow changes, keep the shell and PowerShell implementations
  aligned where both intentionally cover the same behavior. Prefer the
  read-only `scripts/infrastructure/check-deployment.sh` and
  `validate-deployment.sh` for validation; deployment, teardown, cloud,
  AI-eval, and stress scripts target live resources and require explicit scope.
- For EKS enrollment changes, preserve equivalent setup and deregistration
  behavior and run `scripts/test/test_eks_cluster_access_scripts.py`. A
  successful chat-path test does not prove cluster RBAC remains read-only.
- For chart changes, keep `backend/src/agents/chart_directive.py`,
  `ui/src/components/charts/chartContract.ts`, `docs/CHART_CONTRACT.md`, and
  `docs/chart-contract-parity-corpus.json` aligned. Run the backend
  `test_chart_directive_unit.py`, frontend chart-contract tests, and API
  chart-proxy roundtrip tests; invalid chart payloads must remain inert text.
- For session-lifecycle changes, trace `ui/src/utils/authSession.ts`,
  `sessionCoordinator.ts`, `useIdleSession.ts`, the auth API routes, and
  `ui/src/pages/api/config.ts`. Run focused Jest tests and the session/idle
  configuration checks in `scripts/test/`; refresh completion must not restore
  cookies after logout.
- For memory-identity changes, trace the orchestrator and
  `backend/src/utils/memory_identity.py`; run `test_memory_identity_unit.py` and
  `test_orchestrator_memory_config_unit.py`. Changing namespace mapping is not
  authorization to delete old records.
- For milestone-release changes, read `docs/RELEASING.md` and trace
  `.github/workflows/release.yml` plus `scripts/release/`. Run focused release
  checks in `scripts/test/`; the workflow publishes accepted source and
  evidence without deploying AWS resources.

## Completion

- State what changed, what was verified, and any unresolved risk.
- Do not claim deployment success from a source diff. When deployment is in
  scope, verify the relevant stack/runtime state and, where appropriate, the
  Knowledge Base retrieval and authenticated request path.
