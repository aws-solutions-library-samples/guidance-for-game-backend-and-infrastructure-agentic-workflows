# Game Agent - Deployment Guide

This guide walks you through deploying Game Agent to your AWS account.

## Prerequisites

Before you begin, ensure you have the following installed and configured:

### Required Tools

| Tool | Version | Installation |
|------|---------|--------------|
| **AWS CLI** | v2+ | [Install Guide](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) |
| **Node.js** | 18+ | [Download](https://nodejs.org/) |
| **UV** | 0.9+ | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **Docker** | Latest | [Install Guide](https://docs.docker.com/get-docker/) |
| **yq** | v4+ | [Install Guide](https://github.com/mikefarah/yq#install) |
| **python3** | 3.8+ | [Download](https://www.python.org/downloads/) |

**Note:** Python 3.13 and the AgentCore CLI (`bedrock-agentcore-starter-toolkit`) are automatically installed by UV during setup (`uv sync`). UV does not add a `python3` to `PATH`, so a standalone Python 3 is still required: the deployment scripts call `python3` directly to parse AWS CLI responses in the account-observability steps (the Step 1.7 default read, the Step 2b trace-delivery verification, and the Step 5c post-launch recheck).

**Windows users:** Native PowerShell deployment scripts are available in `scripts/powershell/`. See [scripts/powershell/README.md](../scripts/powershell/README.md) for setup and usage. Requires PowerShell 7.0+ and AWS CLI v2 — no WSL2 or Linux environment needed.

### AWS Account Requirements

Your AWS account needs the following:
- **Bedrock Model Access**: Claude Sonnet 5.5 and Claude Haiku 5.5 enabled in your region
- **Service Quotas**: Default quotas are sufficient for most deployments
- **IAM Permissions**: Administrator access (or equivalent) for initial deployment

### Enable Bedrock Models

1. Go to [Amazon Bedrock Console](https://console.aws.amazon.com/bedrock/)
2. Navigate to **Model access** in the left sidebar
3. Click **Manage model access**
4. Enable:
   - Anthropic Claude Sonnet 5.5
   - Anthropic Claude Haiku 5.5
5. Click **Save changes**

## Quick Start

**Step 1:** Configure and verify AWS credentials (run `aws configure` separately — it's interactive):
```bash
aws configure
aws sts get-caller-identity  # Verify credentials work
```

**Step 2:** Configure and deploy:
```bash
# Set your preferred region (optional, defaults to us-west-2)
export AWS_REGION=us-west-2

# Create environment config — you can also set AWS_PROFILE here instead of the command below
cp ui/.env.local.example ui/.env.local

# Deploy everything
./deploy-all.sh
# Optional: override AWS profile (can also be set in ui/.env.local)
# AWS_PROFILE=<your-profile> ./deploy-all.sh

# Create your admin user
./scripts/infrastructure/add-admin-user.sh
```

That's it! Access your deployment at the URL shown after deployment completes.

## Detailed Deployment Steps

### Step 1: Configure AWS Credentials

**Option A:** Use AWS CLI configuration (interactive — run separately):
```bash
aws configure
```

**Option B:** Use environment variables:
```bash
export AWS_ACCESS_KEY_ID=your_access_key
export AWS_SECRET_ACCESS_KEY=your_secret_key
export AWS_REGION=us-west-2
```

Verify credentials work:
```bash
aws sts get-caller-identity
```

### Step 2: Set Your Region

Game Agent defaults to `us-west-2`. To deploy to a different region:

```bash
export AWS_REGION=your-preferred-region
```

**Supported regions** (must have Bedrock with Claude models):
- us-east-1 (N. Virginia)
- us-west-2 (Oregon)
- eu-west-1 (Ireland)
- ap-northeast-1 (Tokyo)

### Step 3: Create Environment Configuration

```bash
cp ui/.env.local.example ui/.env.local
```

Edit `ui/.env.local` if you need to customize:
- `AWS_REGION`: Your deployment region
- `GBAW_ORCHESTRATOR_MODEL_ID`: Orchestrator model or inference profile (commented out; default from `model_settings.py`: Claude Haiku 5.5)
- `GBAW_SPECIALIST_MODEL_ID`: GameLift, EKS, and Cost model or inference profile (commented out; default from `model_settings.py`: Claude Sonnet 5.5)
- `GBAW_TENANT_ID`: Trusted tenant binding for this deployment (default: `default-tenant`)
- `GBAW_WORKSPACE_ID`: Trusted workspace binding for this deployment (default: `default-workspace`)

Process environment values take precedence over `ui/.env.local`. The canonical role variables above take precedence over the compatibility aliases `GBAW_BEDROCK_MODEL_ID` and `GBAW_BEDROCK_MODEL_ID_SECONDARY`; empty values are treated as unset. The deployment passes the resolved role IDs to AgentCore on both initial launch and updates.

> **Upgrading an existing deployment:** The two model-ID lines ship commented out so the defaults come from `backend/src/config/model_settings.py`. If your existing `ui/.env.local` was created from an earlier example, it may still pin `GBAW_ORCHESTRATOR_MODEL_ID` and `GBAW_SPECIALIST_MODEL_ID` to the previous model generation. Those pinned values take precedence over the defaults, so a redeploy keeps those models until you update or delete those two lines. After redeploying, confirm the resolved model IDs in the startup log and `scripts/deploy.sh` output name the models you expect.

Tenant and workspace are server-side identity bindings. The deployment passes
them to the frontend container without a `NEXT_PUBLIC_` prefix; browser request
data cannot override them. See
[Identity and Authorization](IDENTITY_AND_AUTHORIZATION.md).

### Step 4: Deploy

```bash
./deploy-all.sh
# Optional: override AWS profile (can also be set in ui/.env.local)
# AWS_PROFILE=<your-profile> ./deploy-all.sh
```

This automated script:
1. Creates base infrastructure (Cognito, IAM, ECR)
2. Deploys Bedrock Guardrails for AI safety
3. Launches AgentCore Runtime with AI agents
4. Deploys observability stack
5. Creates and seeds Knowledge Bases
6. Builds and pushes frontend container
7. Deploys frontend on ECS Express (Fargate + ALB)

**Expected duration**: 15-25 minutes

### Step 5: Create Admin User

After deployment completes:

```bash
./scripts/infrastructure/add-admin-user.sh
```

You'll be prompted for:
- Email address
- Password (min 8 characters, uppercase, lowercase, number, symbol)

Self-signup is disabled and the app has no in-app user management. To add
another administrator, re-run this script. To grant chat-only access, create
the user the same way but add it to the `users` group instead of `admin`. The
sign-in page does not handle a temporary-password challenge, so set a permanent
password:

```bash
USER_POOL_ID=<user-pool-id>   # the UserPoolId output of the base stack
CHAT_USER_EMAIL=<email>
read -rsp "Password: " CHAT_USER_PASSWORD; echo
aws cognito-idp admin-create-user --user-pool-id "$USER_POOL_ID" --username "$CHAT_USER_EMAIL" \
  --user-attributes Name=email,Value="$CHAT_USER_EMAIL" Name=email_verified,Value=true \
  --message-action SUPPRESS
aws cognito-idp admin-set-user-password --user-pool-id "$USER_POOL_ID" --username "$CHAT_USER_EMAIL" \
  --password "$CHAT_USER_PASSWORD" --permanent
aws cognito-idp admin-add-user-to-group --user-pool-id "$USER_POOL_ID" --username "$CHAT_USER_EMAIL" \
  --group-name users
unset CHAT_USER_PASSWORD
```

### Step 6: Access Your Deployment

The deployment script outputs your frontend URL:

```
Frontend URL: https://<service-id>.ecs.<region>.on.aws
```

Log in with the admin credentials you created.

## What Gets Deployed

| Component | AWS Service | Purpose |
|-----------|-------------|---------|
| Frontend | ECS Express (Fargate + ALB) | Web UI with chat interface |
| Backend | Bedrock AgentCore | AI agents and orchestration |
| Auth | Cognito | User authentication |
| AI Models | Bedrock | Claude Haiku 5.5 orchestrator and Claude Sonnet 5.5 specialists |
| Knowledge Bases | Bedrock | RAG for GameLift, EKS, Cost |
| Guardrails | Bedrock | AI safety controls |
| Observability | CloudWatch | Logging and monitoring |

ADOT runtime telemetry is auto-configured by AgentCore. For a known cold-start exporter credential limitation (transient `403` on telemetry export) and the read-only detection check, see [`OBSERVABILITY_ADOT_EXPORTER.md`](OBSERVABILITY_ADOT_EXPORTER.md).

### Account-wide Observability (Scoped and Opt-in)

X-Ray Transaction Search relies on account-wide, cross-application settings: the
X-Ray trace segment destination, the default X-Ray indexing (sampling) rule, and
a shared CloudWatch Logs resource policy on the AWS-reserved `aws/spans` log
group. Changing these affects every X-Ray and Transaction Search consumer in the
account.

**Scope.** By default this repository's deployment scripts make **no**
account-wide Transaction Search settings changes. They read the current trace
destination, detect whether the account already supports what the runtime needs,
and — if it does not — print an actionable warning. That warning recommends the
opt-in only when the AWS-reserved `aws/spans` log group already exists and the
destination was read (so the opt-in can finish the job); otherwise it points to
the one-time console enable or a destination toggle keyed on the current
destination, because the opt-in cannot create that group. A default deployment is **not** failed for a missing account
configuration. The per-runtime trace delivery (Step 2b) binds only when X-Ray
Transaction Search is active, so the deploy first reads the account trace segment
destination: when it is `CloudWatchLogs` and `ACTIVE` the delivery is created and
verified (and any delivery failure then fails the deploy); when it is
`CloudWatchLogs` but not yet `ACTIVE` the deploy waits a bounded budget for it;
and when the destination is not `CloudWatchLogs`, never becomes `ACTIVE` within
the budget, or cannot be read, the deploy prints one WARNING naming the state and
skips the delivery rather than aborting. The read-only Step 5c recheck still runs
and reports.

> **Residual not controlled by this opt-in.** `agentcore launch` — which the
> deploy script runs in its Step 2 (only on a first deploy) and Step 5b
> launches — performs its own Transaction Search setup on every launch,
> regardless of `GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY`. On every launch, the
> AgentCore starter toolkit may make up to three shared changes, each also when
> its own read fails: it adds a `TransactionSearchXRayAccess` resource policy
> whenever no policy already has that exact name (even when this opt-in's own
> policy already grants access), switches the account trace segment destination
> to `CloudWatchLogs` when it is not already `CloudWatchLogs`, and sets the
> `Default` X-Ray indexing rule to 1% sampling when no `Default` rule exists.
> This is behavior of the toolkit, not of this repository's scripts. The
> deployment predicts these changes before every launch and warns about them,
> and a fail-closed stop switch
> (`GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE=true`, PowerShell
> `-StopOnAccountObservabilityChange`) can halt the deploy before `agentcore
> launch` makes any of these changes. The residual is that, without the stop switch,
> the toolkit still performs its own setup during `agentcore launch`; the
> toolkit exposes no flag to skip only that step.
> A read-only post-launch recheck (Step 5c) reports the resulting Transaction
> Search state without changing it. For the full launch-time behavior and the
> stop switch, see
> [AgentCore Starter Toolkit Transaction Search (Launch-Time Exception)](#agentcore-starter-toolkit-transaction-search-launch-time-exception).

**Opt-in.** To let the deployment configure the shared settings, set the
purpose-named opt-in:

```bash
GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true ./deploy-all.sh
```

PowerShell uses the matching switch:

```powershell
Deploy-GameAgent -ConfigureAccountObservability
```

Before applying anything, the opt-in path prints exactly which shared settings
may change.

`GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY` and `GBAW_XRAY_DEFAULT_INDEXING_PERCENT`
are read from the process environment only; they are not read from
`ui/.env.local`. The PowerShell switch `-ConfigureAccountObservability` and the
environment variable are equivalent opt-in triggers.

**Ownership and preservation.** This repository's opt-in reads existing state
first and preserves it. It enables Transaction Search only additively (it never
performs a disable/re-enable toggle of the shared destination) and leaves an
already-enabled `CloudWatchLogs` destination unchanged. Because a first-time API
enable does not create the AWS-reserved `aws/spans` log group (only the console
or a destination toggle does), the opt-in **fails** — with the one-time console
or toggle step — rather than reporting success when `aws/spans` is still absent
after the enable. On a fresh account whose destination starts at `XRay`, the
opt-in has by then already written the shared resource policy and switched the
trace destination to `CloudWatchLogs` (and, if you set
`GBAW_XRAY_DEFAULT_INDEXING_PERCENT`, changed the `Default` indexing rule); those
changes stay in place (the destination rollback command was printed earlier in
the run), and only the console or a destination toggle creates `aws/spans`. If it cannot read the
current trace destination, the opt-in refuses to change it and fails rather than
guessing. The
account-wide X-Ray **default indexing (sampling) rule is left unchanged**; it is
modified only when you set `GBAW_XRAY_DEFAULT_INDEXING_PERCENT` to an integer in
`[0, 100]`, and only then is a rollback command printed with the prior value. The
shared Logs resource policy uses a project-owned name
(`GameAgentTransactionSearchXRayAccess`). Before writing it, the deployment lists
existing resource policies: if another policy (including a legacy
`TransactionSearchXRayAccess` written by earlier deploys) already grants
`xray.amazonaws.com` `logs:PutLogEvents` on `aws/spans`, it is left in place and
the write is skipped; if the account is already at the 10-policy limit, the
deployment fails with a bounded message instead of adding a duplicate.

**Legacy policy migration.** Some earlier versions of this project wrote a shared
policy named `TransactionSearchXRayAccess`. The current opt-in writes its own
project-owned policy name and leaves any `TransactionSearchXRayAccess` policy
untouched. If such a policy exists and already grants the needed access, it
continues to work and the opt-in skips its own write. Delete the legacy
`TransactionSearchXRayAccess` policy only once you have confirmed no other
workload relies on it.

**Validation.** The per-runtime trace delivery step first reads the account
trace segment destination: it creates and verifies the delivery only when the
destination is `CloudWatchLogs` and `ACTIVE`, waits a bounded, configurable
budget when the destination is `CloudWatchLogs` but still converging, and
otherwise prints one WARNING and skips the delivery (the deploy continues). The
wait budget is overridable with `GBAW_TRACE_DESTINATION_ACTIVE_MAX_ATTEMPTS`
(default 30) and `GBAW_TRACE_DESTINATION_ACTIVE_RETRY_SECONDS` (default 10). When
the delivery does run, each mutation (delivery source → destination → delivery)
distinguishes a genuine already-exists conflict from authorization, validation,
throttling, and service errors by the AWS error code. Retryable errors are
retried with a small bounded backoff; a real error fails the deployment with a
bounded diagnostic. The deployment then queries the delivery (retrying a
transient read) and fails unless a delivery with destination type `XRAY` binds
the intended source — whose resources include the runtime — to the intended
destination.

**Rollback.** The account-wide changes made by *this repository's opt-in* are
listed above; `agentcore launch` may make the separate, toolkit-owned changes
noted in the Scope callout. The opt-in prints a rollback command for the trace
destination when it enables Transaction Search, and for the default indexing rule
only when you explicitly change it (with the prior percentage). To remove the
project-owned resource policy:

```bash
aws logs delete-resource-policy --policy-name GameAgentTransactionSearchXRayAccess --region <region>
```

Teardown intentionally preserves account-wide observability resources because
they may be shared with other applications.

### AgentCore Starter Toolkit Transaction Search (Launch-Time Exception)

The account-wide setup above is this repository's own, opt-in behavior. Separately,
`agentcore launch` itself can change account-wide X-Ray / CloudWatch Logs settings.
When runtime observability is enabled — the same setting that selects the
`opentelemetry-instrument` entrypoint used for tracing — the AgentCore starter
toolkit (version 0.3.11) runs its `enable_transaction_search_if_needed` step after
the runtime is created or updated. In 0.3.11 there is no flag to keep that OTEL
entrypoint while skipping only this account step, so this deployment keeps the
entrypoint and instead makes the behavior explicit.

**What the toolkit may change (its own conditions):**

- **CloudWatch Logs resource policy** — the toolkit creates a policy named
  literally `TransactionSearchXRayAccess` whenever no policy with that exact name
  exists. It checks the *name* only, so this repository's project-owned
  `GameAgentTransactionSearchXRayAccess` policy does **not** satisfy it: the
  toolkit adds its own `TransactionSearchXRayAccess` policy even when the opt-in
  already granted the access.
- **X-Ray trace segment destination** — set to `CloudWatchLogs` when it is not
  already `CloudWatchLogs` (account-wide, shared by every X-Ray caller).
- **X-Ray default indexing rule** — the toolkit calls `UpdateIndexingRule` to set
  the shared `Default` rule to 1% when no rule named `Default` exists. There is no
  separate create operation; this is an upsert of the account-wide `Default` rule.
- **Write-on-doubt.** When the toolkit cannot read one of these settings, it
  performs the write anyway. A read that it cannot interpret therefore still
  results in the account-wide change.

**Read-only preflight and warning.** Before every `agentcore launch`, the
deployment runs a read-only preflight that predicts exactly which of these the
toolkit would change, using the toolkit's own conditions. If it would change
nothing, the deployment prints a single no-change line (no per-item detail). If
it would change something, it prints the per-item detail followed by a prominent
warning that names each change and the affected shared settings, explains that
the change comes from the AgentCore starter toolkit (not this repository's
opt-in), and prints only the cleanup/rollback commands relevant to the predicted
changes. If a read fails, the preflight reports the toolkit's effect as
**unknown** (a possible change), never as "no change", and states that for each
unknown item the toolkit will perform the write.

**Default is non-blocking.** A fresh-account `./deploy-all.sh` still completes; the
preflight warns but does not stop.

**Fail-closed stop switch.** Operators who require no shared account change can set
a stop switch that halts the deploy *before* `agentcore launch` whenever the
toolkit would (or might) change account-wide settings:

```bash
GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE=true ./deploy-all.sh
```

PowerShell uses the matching switch (or the same environment variable):

```powershell
Deploy-GameAgent -StopOnAccountObservabilityChange
```

Only the exact value `true` enables it; any other value (including `TRUE` and `1`)
is treated as off. When the stop switch is set, the preflight runs once up front —
before the Knowledge Base and observability steps — so a predicted or unknown
change halts the deploy before any of those steps run. Because the stop is
fail-closed, it also halts when the preflight cannot read the current state
(unknown).

**Reaching a passing run with the stop switch.** The stop switch keeps halting
until the account already satisfies all three toolkit conditions, so that the
toolkit's launch-time step would be a no-op:

- a CloudWatch Logs resource policy named *literally* `TransactionSearchXRayAccess`,
- the X-Ray trace segment destination set to `CloudWatchLogs`, and
- an X-Ray indexing rule named `Default`.

Enable Transaction Search once in the AWS X-Ray / CloudWatch console. That
creates the AWS-reserved `aws/spans` log group, switches the trace destination,
and sets the `Default` indexing rule. Then re-run with the stop switch still set.
If the preflight still reports the resource policy, the account has no policy
with the toolkit's exact name yet. If another policy already grants `aws/spans`
access under a different name, such as this repository's
`GameAgentTransactionSearchXRayAccess`, neither the console nor the opt-in adds
the name `TransactionSearchXRayAccess`. Add a policy named exactly
`TransactionSearchXRayAccess`, or run one deploy without the stop switch so the
toolkit adds it.

This repository's opt-in uses the project-owned name
`GameAgentTransactionSearchXRayAccess` by default, which does **not** satisfy the
toolkit's name-only check. The opt-in cannot create `aws/spans`, and it skips its
own policy write when another policy already grants `xray.amazonaws.com` access
to `aws/spans`. So it reaches a passing state only when `aws/spans` already
exists and no other policy grants that access. On such an account, run the opt-in
with the matching policy name to switch the destination and add the policy the
toolkit looks for (the opt-in leaves the shared `Default` indexing rule in place,
so the console enable above still creates it when it is missing):

```bash
GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY=true \
  GBAW_OBSERVABILITY_RESOURCE_POLICY_NAME=TransactionSearchXRayAccess ./deploy-all.sh
```

On an account **without** `aws/spans`, do not use the opt-in to clear the stop
switch: it would write the shared policy (and switch an `XRay` destination) but
still stop at Step 1.7, because it cannot create the group. Use the console
enable above instead.

**Cleanup.** The warning prints only the rollback commands for the changes it
predicted. If the toolkit added its own policy, remove it after launch (only once
no other workload relies on it):

```bash
aws logs delete-resource-policy --policy-name TransactionSearchXRayAccess --region <region>
```

Note that `TransactionSearchXRayAccess` is also this repository's legacy policy
name, so confirm the launch (not an earlier deploy) created it before deleting.
If the launch changed the trace destination, restore the prior value with
`aws xray update-trace-segment-destination --destination <prior> --region <region>`.
If the launch set the shared `Default` indexing rule to 1%, restore a prior
percentage with
`aws xray update-indexing-rule --name Default --rule '{"Probabilistic":{"DesiredSamplingPercentage":<prior>}}' --region <region>`.


## Post-Deployment Configuration

### Enroll EKS Clusters (Optional)

To enable Kubernetes monitoring for your EKS clusters:

```bash
cd infrastructure/kubernetes
./enroll-cluster.sh your-cluster-name us-west-2
```

This grants Game Agent read-only access to monitor pods, nodes, deployments, and services. The script uses EKS access entries for `API` and `API_AND_CONFIG_MAP` clusters and `aws-auth` for legacy `CONFIG_MAP` clusters. If the active identity cannot apply Kubernetes RBAC, pass `--kube-role-arn` with an administrator role.

### Verify Deployment Health

```bash
# Check all stacks
aws cloudformation list-stacks --query 'StackSummaries[?contains(StackName, `game-agent`)]'

# Validate resources and cloud integration
./validate-deployment.sh
./test-cloud.sh

# Verify all three Knowledge Bases (GameLift, EKS, Cost) return retrieval
# results; exits non-zero if any stack, KB, or retrieval is broken
./scripts/infrastructure/test-kb.sh

# Exercise Guardrail behavior and specialist routing/tool use with a
# short-lived access token from a provisioned Cognito test user (in the
# `admin` or `users` group)
export GBAW_TEST_ACCESS_TOKEN='<short-lived-access-token>'
./test-ai-evals.sh

# Confirm startup logged both resolved model roles
aws logs tail /aws/bedrock-agentcore/runtimes/gameagentruntime-<ID>-DEFAULT \
  --since 30m --region "$AWS_REGION" --profile <your-profile> \
  --filter-pattern '"Orchestrator model" || "Specialist model"'
```

For the deployment smoke test, send one off-topic request expected to activate the Guardrail and one in-domain request such as "List my EKS clusters" expected to route to a specialist and invoke a client-side tool. Confirm the Guardrail result and tool-use span in AgentCore traces, and confirm the startup log identifies Haiku 5.5 for the orchestrator and Sonnet 5.5 for specialists. Response text alone is not sufficient evidence of model or tool selection.

### Clean Up Memory Records Left Under Raw-Subject Actor IDs

Run this once, as the operator who deploys the upgrade to the pseudonymous memory actor ID, after the update has fully rolled out, from an operator or administrator session that has AgentCore Memory data-plane permissions. Do not run it from the chat runtime: the runtime role has no record-delete permission by design (`infrastructure/cloudformation/01-base-infrastructure.yaml` grants memory record read and write but no delete), and that boundary stays in place. The shell blocks below are bash; macOS defaults to zsh, so run `bash` first if your terminal is zsh, otherwise the `#` comment lines fail to parse. See the Windows note for the PowerShell form.

**Operator permissions.** Discovery (Step 1) needs `bedrock-agentcore:ListActors` and `bedrock-agentcore:ListMemoryRecords` on the memory resource, and `cognito-idp:ListUsers` on the user pool. The delete (Steps 2–3) needs `bedrock-agentcore:ListMemoryRecords`, `bedrock-agentcore:BatchDeleteMemoryRecords`, and `bedrock-agentcore:DeleteMemoryRecord` on the memory resource. The optional short-term-event cleanup (Step 4) additionally needs `bedrock-agentcore:ListSessions`, `bedrock-agentcore:ListEvents`, and `bedrock-agentcore:DeleteEvent` on the memory resource.

**Run after the rollout completes.** Old runtime sessions keep writing raw-subject events and records until they end, so a cleanup run while the previous version is still serving will leave fresh orphans behind. Wait until the digest-based runtime has fully replaced the old one before starting.

**Namespace assumption.** This deploy path configures the memory resource as short-term only (`agentcore configure` without a long-term strategy), and the runtime writes records with `namespaces=[actorId]` — the actor ID is the whole, single-segment namespace. This runbook also assumes the memory resource is dedicated to this deployment, so any non-digest namespace is treated as a leftover to clean up. The AWS CLI's `--namespace` option is documented as a prefix filter, but on this layout it matches a whole namespace path segment, not an arbitrary character of a segment: on a live memory resource, `--namespace <full actor ID>` returned that actor's records while `--namespace <first few characters>` and `--namespace <single character>` returned nothing. Discovery below therefore lists each candidate actor ID in full rather than enumerating character prefixes. A memory configured with the toolkit's long-term strategies instead stores extracted records under paths such as `/users/{actorId}/facts/`, which these steps do not list; adjust the namespace filters if your resource uses those strategies.

**Why this is needed.** The runtime derives each user's AgentCore Memory actor ID as a 64-character lowercase SHA-256 digest of the verified Cognito subject (`backend/src/utils/memory_identity.py`). A runtime that uses the raw subject writes under a different actor ID — the raw Cognito subject, a 36-character UUID such as `a1b2c3d4-5e6f-7a8b-9c0d-e1f2a3b4c5d6`. Once the digest-based runtime is deployed, those raw-subject records are never read again, but long-term records carry no time-to-live, so they persist until an operator removes them. Short-term events expire on the memory resource's event expiry (30 days by default), so they usually need no manual cleanup; a step to remove them early is included below if you need it.

The actor ID the runtime now uses and the records left behind differ by the shape of their namespace, which is how you tell them apart:

| | Namespace format | Example (synthetic) |
|---|---|---|
| New actor ID (keep) | 64 lowercase hex characters | `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa` |
| Old actor ID (clean up) | an actor the current runtime does not write: a raw Cognito subject (a 36-character UUID), `anonymous`, or a test actor | `a1b2c3d4-5e6f-7a8b-9c0d-e1f2a3b4c5d6` |

The current runtime only writes 64-character lowercase hex digests, so any namespace that is not one is a cleanup candidate. Cognito subjects are 36-character UUIDs with hyphens, which a digest never contains, so a real subject is never mistaken for a digest. The hosted subject pattern `[A-Za-z0-9][A-Za-z0-9._:-]{2,127}` would also admit a 64-character hex string; Step 1 treats such a value as a digest and leaves its records in place, which is the safe direction.

Set your resource and region first. The memory ID is the one the runtime uses (`BEDROCK_AGENTCORE_MEMORY_ID`); the AgentCore CLI names it after the runtime with a short random suffix, for example `gameagentruntime_mem-0a1b2c3d4e`.

```bash
export AWS_REGION=us-west-2
export MEMORY_ID='gameagentruntime_mem-0a1b2c3d4e'   # synthetic; use your resource ID
export USER_POOL_ID='us-west-2_EXAMPLE01'            # the UserPoolId output of the base stack
export AWS_PROFILE='<operator-profile>'              # an operator/administrator profile
```

**Step 1 — Dry run: discover the old namespaces from candidate actor IDs.** This only lists; it deletes nothing. `--namespace` matches a whole namespace path segment, so you cannot sweep records by character prefix; instead, build the set of actor IDs that could hold leftovers and list each one in full. Two sources together cover most leftovers: `list-actors` reports actors that still have unexpired events, and the Cognito user pool's subjects are the actor IDs the old runtime wrote under. An actor that is neither a current pool subject nor still has unexpired events — a deleted user, `anonymous`, or a test actor past its event expiry — is in neither source, so run this cleanup within the memory's event expiry (30 days by default) after the rollout. Combine both sources, drop any 64-hex digest (the current actor-ID shape, which you keep), de-duplicate, and list each remaining candidate by exact namespace, reporting the ones that hold records with their counts.

A user who was deleted from the user pool **and** whose events have expired is in neither source, so this discovery cannot find their leftover records; deleting the memory resource is the only way to remove those. More generally, an actor that is not a current pool subject — a deleted user, `anonymous`, or a test actor — is found only while it still has unexpired events, so run this cleanup within the memory's event expiry (30 days by default) after the rollout to catch them while `list-actors` still reports them.

```bash
# Write the working files outside the checkout so raw subjects do not land next
# to tracked content; remove them and clear their variables when you are done
# (the final line below). Reusing an already-set working-file variable means
# re-running Step 1 after a failed listing overwrites the same file instead of
# leaving the failed run's file behind; all three variables are reused this way.
CANDIDATES_FILE="${CANDIDATES_FILE:-$(mktemp "${TMPDIR:-/tmp}/gbaw-memory-candidates.XXXXXX")}"
LEFTOVERS_FILE="${LEFTOVERS_FILE:-$(mktemp "${TMPDIR:-/tmp}/gbaw-memory-leftovers.XXXXXX")}"

# Collect candidate actor IDs from the live actor list and the user pool's
# subjects. Each listing runs in a subshell so a failed call stops discovery
# with a non-zero status without closing the operator's shell, and the next
# stage runs only on success (&&). The CLI follows pagination itself.
RAW_CANDIDATES_FILE="${RAW_CANDIDATES_FILE:-$(mktemp "${TMPDIR:-/tmp}/gbaw-memory-raw.XXXXXX")}"
( aws bedrock-agentcore list-actors \
    --memory-id "$MEMORY_ID" --region "$AWS_REGION" \
    --query 'actorSummaries[].actorId' --output text \
    || { echo "list-actors failed; discovery is incomplete" >&2; exit 1; }
  aws cognito-idp list-users \
    --user-pool-id "$USER_POOL_ID" --region "$AWS_REGION" \
    --query 'Users[].Attributes[?Name==`sub`].Value' --output text \
    || { echo "list-users failed; discovery is incomplete" >&2; exit 1; }
) > "$RAW_CANDIDATES_FILE" &&

# Drop 64-hex digests (the current shape, which you keep) and de-duplicate. A
# store with no non-digest candidates leaves CANDIDATES_FILE empty, which is a
# clean result, not a failure (grep's "no match" status is tolerated).
{ tr '\t' '\n' < "$RAW_CANDIDATES_FILE" | sort -u | grep -vE '^[0-9a-f]{64}$' || true; } > "$CANDIDATES_FILE" &&

# List each candidate by exact namespace; a candidate that holds records is an
# old namespace to clean up. Print "<count>\t<namespace>" per leftover.
( : > "$LEFTOVERS_FILE"
  while IFS= read -r actor; do
    [ -n "$actor" ] || continue
    ids=$(aws bedrock-agentcore list-memory-records \
      --memory-id "$MEMORY_ID" --region "$AWS_REGION" \
      --namespace "$actor" \
      --query "memoryRecordSummaries[?namespaces[0]=='$actor'].memoryRecordId" \
      --output text) \
      || { echo "listing failed for $actor; discovery is incomplete" >&2; exit 1; }
    n=$(printf '%s' "$ids" | tr '\t' '\n' | grep -c . || true)
    if [ "$n" -gt 0 ]; then
      printf '%s\t%s\n' "$n" "$actor" >> "$LEFTOVERS_FILE"
    fi
  done < "$CANDIDATES_FILE"
) &&
cat "$LEFTOVERS_FILE"

# When finished with discovery, remove the working files and clear their
# variables so a later Step 1 re-run creates fresh mktemp files (mode 0600)
# rather than recreating these paths by redirection at your umask:
#   rm -f "$RAW_CANDIDATES_FILE" "$CANDIDATES_FILE" "$LEFTOVERS_FILE"; unset RAW_CANDIDATES_FILE CANDIDATES_FILE LEFTOVERS_FILE
```

**Step 2 — Dry run: list the records under one old namespace, with their details.** Pick one old namespace from Step 1. List its records by **exact** namespace match (the JMESPath `namespaces[0]==` filter; a whole-segment `--namespace` listing also returns records whose namespace is nested under the value, such as `<old actor>/facts`, and the exact filter keeps only the old actor's own records). Show `namespaces[0]` and `createdAt` so you can confirm what matched. This only lists.

```bash
export OLD_ACTOR='a1b2c3d4-5e6f-7a8b-9c0d-e1f2a3b4c5d6'   # one old namespace from Step 1

# The same guard as Step 3: never preview a 64-hex digest (a current actor ID)
# or an empty value as deletable. Uses no exit/return so a paste keeps the shell.
if printf '%s' "$OLD_ACTOR" | grep -Eq '^[0-9a-f]{64}$' || [ -z "$OLD_ACTOR" ]; then
  echo "Refusing: OLD_ACTOR is empty or a 64-hex digest (a current actor ID)" >&2
else
  aws bedrock-agentcore list-memory-records \
    --memory-id "$MEMORY_ID" --region "$AWS_REGION" \
    --namespace "$OLD_ACTOR" \
    --query "memoryRecordSummaries[?namespaces[0]=='$OLD_ACTOR'].[namespaces[0],memoryRecordId,createdAt]" \
    --output text
fi
```

Confirm these are records you intend to remove, and that every `namespaces[0]` equals `$OLD_ACTOR`. Each `memoryRecordId` is a `mem-` identifier (40–50 characters).

**Step 3 — Delete (explicit, opt-in).** Nothing is deleted until you run this step. `batch-delete-memory-records` takes at most 100 records per call. Collect the IDs for the **exact** namespace into a bash array, then delete in chunks of 100:

```bash
# Refuse to run if OLD_ACTOR is empty or a 64-hex digest: an empty value would
# be rejected by the API, but a digest is a current, live user's namespace, and
# deleting it is irreversible. The guard wraps the whole step with an if/else so
# a pasted block never stops the operator's interactive shell.
if printf '%s' "$OLD_ACTOR" | grep -Eq '^[0-9a-f]{64}$' || [ -z "$OLD_ACTOR" ]; then
  echo "Refusing: OLD_ACTOR is empty or a 64-hex digest (a current actor ID)" >&2
else
  # Collect the record IDs for the exact OLD_ACTOR namespace into an array
  RECORD_IDS=()
  while IFS= read -r id; do
    [ -n "$id" ] && RECORD_IDS+=("$id")
  done < <(aws bedrock-agentcore list-memory-records \
    --memory-id "$MEMORY_ID" --region "$AWS_REGION" \
    --namespace "$OLD_ACTOR" \
    --query "memoryRecordSummaries[?namespaces[0]=='$OLD_ACTOR'].memoryRecordId" \
    --output text | tr '\t' '\n')

  # Print the count so an empty collection (for example after expired
  # credentials) is visible rather than a silent no-op.
  echo "Collected ${#RECORD_IDS[@]} record ID(s) for namespace $OLD_ACTOR"

  # Delete in batches of at most 100 (the API cap). After each call, surface any
  # failedRecords (zero exit status, but the records were not deleted).
  for ((i = 0; i < ${#RECORD_IDS[@]}; i += 100)); do
    RECORDS_ARG=()
    for id in "${RECORD_IDS[@]:i:100}"; do
      RECORDS_ARG+=("memoryRecordId=$id")
    done
    FAILED=$(aws bedrock-agentcore batch-delete-memory-records \
      --memory-id "$MEMORY_ID" --region "$AWS_REGION" \
      --records "${RECORDS_ARG[@]}" \
      --query 'failedRecords[].memoryRecordId' --output text)
    if [ -n "$FAILED" ]; then echo "Failed to delete: $FAILED" >&2; fi
  done
fi
```

To remove a single record instead, use `aws bedrock-agentcore delete-memory-record --memory-id "$MEMORY_ID" --memory-record-id "<mem-...>" --region "$AWS_REGION"`. The loop above already handles an actor with more than 100 records by sending one request per 100-record chunk. If Step 3 prints an error, only part of the namespace may have been deleted; fix the session and run Steps 2 and 3 again.

**Step 4 (optional) — Short-term events.** Short-term events expire on the resource's event expiry, so this is only needed if you want them gone sooner. `list-sessions` and `list-events` are keyed by actor ID, so this path uses the old actor directly. List sessions and events for a raw-subject actor, then delete per event:

```bash
# Sessions for the raw-subject actor
aws bedrock-agentcore list-sessions \
  --memory-id "$MEMORY_ID" --actor-id "$OLD_ACTOR" --region "$AWS_REGION" \
  --query 'sessionSummaries[].sessionId' --output text

# Events in one session (dry run)
export OLD_SESSION='<session-id-from-above>'
aws bedrock-agentcore list-events \
  --memory-id "$MEMORY_ID" --session-id "$OLD_SESSION" --actor-id "$OLD_ACTOR" \
  --region "$AWS_REGION" --query 'events[].eventId' --output text

# Delete one event (repeat per event ID)
aws bedrock-agentcore delete-event \
  --memory-id "$MEMORY_ID" --session-id "$OLD_SESSION" --actor-id "$OLD_ACTOR" \
  --event-id '<event-id>' --region "$AWS_REGION"
```

**Verify.** Re-run Step 1; it should print nothing at all, with no leftover row and no `discovery is incomplete` line. Running Step 1 again recreates the three working files, so remove them once more with the cleanup line above when you are done. For each namespace you deleted, Step 2 should print no rows. Candidates whose users were deleted from the pool and whose events have expired are in neither discovery source and so cannot be verified this way; deleting the memory resource is the only way to remove those. New 64-hex actors and their records are left untouched. An actor that is not a current pool subject (a deleted user, `anonymous`, or a test actor) is found only while it still has unexpired events, so run this cleanup within the memory's event expiry (30 days by default) after the rollout.

**Windows (PowerShell).** The deployment lifecycle has a PowerShell module (`scripts/powershell/`), but the ad-hoc operator runbooks in this guide — EKS enrollment, health verification, this cleanup — are plain bash with no PowerShell wrapper, and the module exposes no memory-cleanup command. The same AWS CLI v2 operations and parameters work from PowerShell 7+; the following is the discover-and-delete flow for one old namespace, matching the bash Steps 1–3 as three separate blocks so nothing is deleted until you run the last one. PowerShell uses backtick line continuations and `$env:` for process variables, and splits the tab-separated text output explicitly. Matching and sorting use `-cnotmatch` and `-CaseSensitive` because PowerShell ignores case by default, and `@(...)` keeps a single record ID as a one-element array.

Set your resource and region first:

```powershell
$env:AWS_REGION = 'us-west-2'
$env:AWS_PROFILE = '<operator-profile>'              # an operator/administrator profile
$MemoryId = 'gameagentruntime_mem-0a1b2c3d4e'   # synthetic; use your resource ID
$UserPoolId = 'us-west-2_EXAMPLE01'             # the UserPoolId output of the base stack
```

PowerShell Step 1 — Discovery: build candidate actor IDs from the live actor list and the Cognito user pool's subjects, drop 64-hex digests, de-duplicate, and list each candidate by exact namespace, reporting the ones that hold records with their counts. The whole flow runs inside a `& { ... }` script block, and each leftover row is collected into `$leftovers` and emitted only after the loop finishes, so a failed listing throws and stops discovery before any leftover is printed, rather than letting a line-by-line paste classify a partial list. An actor that is not a current pool subject (a deleted user, `anonymous`, or a test actor) is found only while it still has unexpired events, so run this within the memory's event expiry (30 days by default) after the rollout. This only lists:

```powershell
& {
  $actors = aws bedrock-agentcore list-actors `
    --memory-id $MemoryId --region $env:AWS_REGION `
    --query 'actorSummaries[].actorId' --output text
  if ($LASTEXITCODE -ne 0) { throw 'list-actors failed; discovery is incomplete' }
  $subjects = aws cognito-idp list-users `
    --user-pool-id $UserPoolId --region $env:AWS_REGION `
    --query 'Users[].Attributes[?Name==`sub`].Value' --output text
  if ($LASTEXITCODE -ne 0) { throw 'list-users failed; discovery is incomplete' }

  $candidates = @($actors, $subjects) -split '\s+' |
    Where-Object { $_ -and $_ -cnotmatch '^[0-9a-f]{64}$' } |
    Sort-Object -Unique -CaseSensitive

  $leftovers = foreach ($actor in $candidates) {
    $ids = aws bedrock-agentcore list-memory-records `
      --memory-id $MemoryId --region $env:AWS_REGION `
      --namespace $actor `
      --query "memoryRecordSummaries[?namespaces[0]=='$actor'].memoryRecordId" `
      --output text
    if ($LASTEXITCODE -ne 0) { throw "listing failed for $actor; discovery is incomplete" }
    $n = @($ids -split '\s+' | Where-Object { $_ }).Count
    if ($n -gt 0) { "$n`t$actor" }
  }
  $leftovers
}
```

PowerShell Step 2 — Dry run: pick one old namespace from discovery and list its records by exact namespace match. This only lists:

```powershell
$OldActor = 'a1b2c3d4-5e6f-7a8b-9c0d-e1f2a3b4c5d6'   # one old namespace from discovery

# The same guard as Step 3: never preview a 64-hex digest (a current actor ID)
# or an empty value as deletable. A pasted bare throw does not stop later
# top-level statements, so the whole step runs inside a & { ... } script block.
& {
  if (-not $OldActor -or $OldActor -cmatch '^[0-9a-f]{64}$') {
    throw 'Refusing: $OldActor is empty or a 64-hex digest (a current actor ID)'
  }
  aws bedrock-agentcore list-memory-records `
    --memory-id $MemoryId --region $env:AWS_REGION `
    --namespace $OldActor `
    --query "memoryRecordSummaries[?namespaces[0]=='$OldActor'].[namespaces[0],memoryRecordId,createdAt]" `
    --output text
}
```

PowerShell Step 3 — Delete (explicit, opt-in). Nothing is deleted until you run this step. Collect the IDs for the exact namespace, then delete in batches of at most 100:

```powershell
# A pasted bare throw does not stop later top-level statements, so the whole
# step runs inside a & { ... } script block with the guard before any delete.
& {
  if (-not $OldActor -or $OldActor -cmatch '^[0-9a-f]{64}$') {
    throw 'Refusing: $OldActor is empty or a 64-hex digest (a current actor ID)'
  }
  # @(...) keeps a single ID as a one-element array rather than a scalar string.
  $recordIds = @((aws bedrock-agentcore list-memory-records `
    --memory-id $MemoryId --region $env:AWS_REGION `
    --namespace $OldActor `
    --query "memoryRecordSummaries[?namespaces[0]=='$OldActor'].memoryRecordId" `
    --output text) -split '\s+' | Where-Object { $_ })

  # Print the count so an empty collection is visible rather than a silent no-op.
  Write-Host "Collected $($recordIds.Count) record ID(s) for namespace $OldActor"

  for ($i = 0; $i -lt $recordIds.Count; $i += 100) {
    $chunk = $recordIds[$i..([Math]::Min($i + 99, $recordIds.Count - 1))]
    $recordsArg = $chunk | ForEach-Object { "memoryRecordId=$_" }
    # Surface any failedRecords (zero exit status, but not deleted).
    $failed = aws bedrock-agentcore batch-delete-memory-records `
      --memory-id $MemoryId --region $env:AWS_REGION `
      --records $recordsArg `
      --query 'failedRecords[].memoryRecordId' --output text
    if ($failed) { Write-Error "Failed to delete: $failed" -ErrorAction Continue }
  }
}
```

## Teardown

To remove all deployed resources:

```bash
./teardown-all.sh
# Optional: override AWS profile
# AWS_PROFILE=<your-profile> ./teardown-all.sh
```

This removes all CloudFormation stacks, the AgentCore Runtime, and associated resources.

**Warning**: This action is irreversible. All data in Knowledge Bases will be deleted.

## Troubleshooting

### Common Issues

**"AgentCore CLI not found"**
```bash
# The agentcore CLI is provided by bedrock-agentcore-starter-toolkit,
# which is a dev dependency managed by uv. Run from the backend directory:
cd backend && uv sync
```

**"Bedrock model access denied"**
- Verify Claude models are enabled in the Bedrock console for your region

**"CloudFormation stack failed"**
```bash
# Check stack events
aws cloudformation describe-stack-events --stack-name game-agent-infrastructure
```

**"Frontend not loading"**
- Wait 5-10 minutes for ECS Express to fully deploy
- Check ECS frontend logs in CloudWatch

### Getting Help

- Check logs: `./scripts/infrastructure/check-deployment.sh`
- View CloudWatch dashboards in the AWS Console
- Review [Architecture Documentation](ARCHITECTURE.md)

## Cost Estimates

Approximate monthly costs at minimal usage (development/demo) in `us-west-2`:

| Service | Estimated Cost | Notes |
|---------|---------------|-------|
| Bedrock (Claude Sonnet 5.5 + Haiku 5.5) | ~$480 (excluding `between_tools` thinking) at ~10,000 queries/month | Uses `global.*` cross-region model IDs; Sonnet 5.5 $2.00/M input + $10.00/M output, Haiku 5.5 $0.10/M input + $0.50/M output (AWS Price List API, us-west-2, retrieved 2026-10-09). Token volumes scaled up ~30% from the equivalent Claude 4.x workload for the newer tokenizer; Haiku 5.5 thinking is disabled, Sonnet 5.5 uses `between_tools` whose thinking blocks bill as output. Scales with query volume |
| ECS Fargate | $36-47 | 1 vCPU, 2 GB task; $36.04 at MinTasks: 1 (730 hrs), ~$47 at avg ~1.3 tasks under moderate load (~950 task-hrs/mo) |
| Bedrock Guardrails | $3-32 | 4 guarded calls/query × (input + output) TU per safeguard: content filters ($0.15/1K TU) + denied topics ($0.15/1K TU) + PII ($0.10/1K TU) |
| Bedrock AgentCore Runtime | $1-10 | CPU billed on active consumption only (I/O wait free); memory billed for full session duration |
| Bedrock AgentCore Memory | $5-13 | STM: ~3 events/query @ $0.25/1K + LTM retrieval: 1 request/query @ $0.50/1K (billed for empty results) + LTM storage: ~1K records/mo @ $0.25/1K |
| WAF | $11 | $5 WebACL + $6 rules + $0.60/M requests (shared ~100K HTTP request assumption) |
| CloudWatch + X-Ray | $5-10 | Log ingestion, metrics, traces |
| ALB | ~$17 | $16.43 fixed + LCU variable (max of: new connections, active connections, processed bytes, rule evaluations). At 100K reqs/mo, 100KB total request+response/req, no mTLS, ≤10 rules: ~$0.08 LCU |
| Knowledge Bases (S3 Vectors) | <$1 | S3 Vectors storage + $2.50/M query requests + Titan Embed V2 @ $0.02/M tokens |
| CloudTrail + KMS | ~$2 | Management events (first trail free) + 1 CMK @ $1/mo |
| S3 (docs + logs + artifacts) | $1-5 | Standard storage across 5 buckets |
| Cognito | Free | Up to 50,000 MAUs |

**Base infrastructure**: ~$80-140/month (Fargate, ALB, WAF, Guardrails, CloudWatch, KMS, CloudTrail)
**AI usage (variable)**: ~$480/month (excluding `between_tools` thinking) at ~10,000 queries/month (Claude Sonnet 5.5 + Haiku 5.5; Haiku thinking disabled, Sonnet `between_tools`); scales with query volume and conversation length

### Optional operations control plane (E1)

The optional E1 operations control plane is **default-disabled** and adds
**$0.00** incremental cost. It creates no HTTP API route, compute, table,
bucket, metric, or alarm unless an owner explicitly enables operations. When
enabled, it deploys the accepted synchronous, read-only GameLift observation
design ([ADR 0005](adr/0005-persist-operations-and-recover-workflows.md)) with
**no queue, worker, dead-letter queue, or Step Functions** in the observation
path and no provider-write permission.

Incremental monthly cost in `us-west-2` (pricing as of **2026-09-21**;
free-tier allowances excluded):

| Scenario | Fixed | Variable | Total [USD] |
| --- | --- | --- | --- |
| Default-disabled | $0.00 | $0.00 | **$0.00** |
| Enabled, idle | $1.65 | $0.00 | **$1.65** |
| Enabled, 100,000 observations/mo | $1.84 | $3.49 | **$5.33** |

Enabled resources and their charge basis:

| Service | Basis | Notes |
| --- | --- | --- |
| API Gateway (HTTP API) | $1.00 / million requests | 1 route; 100K req ≈ $0.10 |
| Compute (Lambda, x86, 512 MB) | $0.20 / M requests + $0.0000166667 / GB-s | 1.0 s billed/req modelled; scales to zero when idle |
| DynamoDB (on-demand) | WRU $0.625/M, RRU $0.125/M | 16 WRU + 4 RRU per observation (transactional) |
| DynamoDB storage + PITR | $0.25 + $0.20 / GB-mo | small standing footprint |
| S3 (content-addressed) | $0.023 / GB-mo + $0.005/1K PUT + $0.0004/1K GET | 1 PUT + 1 GET per observation, ~8 KB objects |
| CloudWatch metrics + alarms | $0.30 / metric-mo + $0.10 / alarm-mo | 4 metrics + 4 alarms (always-on fixed cost) |
| CloudWatch logs | $0.50 / GB ingest + $0.03 / GB-mo | ~8 KB per request |
| X-Ray | $5.00 / million traces | 1 trace per observation |

All rates, source URLs, and assumptions live in
[`docs/operations-cost-model.json`](operations-cost-model.json) and are
recomputed by `backend/tests/unit/test_operations_cost_model_unit.py` so these
tables cannot drift. **Denial-of-wallet controls:** create an AWS Budget on the
optional stack's cost-allocation tag; set DynamoDB on-demand per-table maximum
read/write request units; enable HTTP API stage throttling; and alarm on
request count and error/timeout rate. Disable by setting operations mode to
`disabled` and removing the optional stack. E1 rollout (#413) is blocked on this
reviewed cost evidence.

### Cost Optimization Tips

- Prompt caching is enabled by default and uses the default 5-minute TTL. Cache-read share must exceed ~22% of cached tokens for Haiku 5.5 and ~21% for Sonnet 5.5 to break even (writes cost 1.25× input; reads cost 0.1× input for Haiku 5.5 and 0.05× for Sonnet 5.5). Min checkpoint: 512 tokens for both Claude Haiku 5.5 and Claude Sonnet 5.5 (see the Bedrock [prompt caching](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html) guide). Monitor `CacheReadInputTokenCount` vs `CacheWriteInputTokenCount` in CloudWatch
- Knowledge Bases use S3 Vectors (not OpenSearch) for cost-effective vector storage — near-zero cost at small scale
- ECS Fargate scales between 1-4 tasks based on load (configurable via MinTasks/MaxTasks)
- AgentCore Runtime only charges for active CPU time; memory is billed for full session duration regardless of I/O wait
- Monitor CloudWatch dashboards to track AI token consumption

### Optional: application inference profiles for cost attribution

The optional helper scripts `scripts/infrastructure/manage-inference-profile.sh` and `scripts/infrastructure/get-inference-profile-ids.sh` create and check Game Agent application inference profiles for per-role Cost Explorer attribution. They are not called by `scripts/deploy.sh`. An application inference profile records only the requests sent through it, so the profiles attribute no runtime spend until the runtime can use them (see the note below).

> **Not supported as runtime model IDs yet.** Do not set `GBAW_ORCHESTRATOR_MODEL_ID` or `GBAW_SPECIALIST_MODEL_ID` to an application inference profile that wraps Claude Haiku 5.5 or Claude Sonnet 5.5 until the capability-aware model factory ([#421](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/421)). An application inference profile ID is opaque, so the runtime cannot tell which model it wraps and sends the earlier-generation request shape, which includes `temperature`. The 5.5 models reject `temperature`, so every request through such a profile fails. You can create the profiles ahead of #421, but keep the role variables on the `global.` system inference profile IDs. `get-inference-profile-ids.sh` always exports those resolved IDs and prints a stderr notice when it finds the application profiles.

The profile names track the current model generation (`GameAgent-Orchestrator-Claude-Haiku-5-5` and `GameAgent-Specialist-Claude-Sonnet-5-5`). If you created profiles under earlier names, run `manage-inference-profile.sh create <region>` to create the current ones and delete the previous-generation profiles manually; they are not removed automatically. `get-inference-profile-ids.sh` looks up only the current names.

## Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AWS_REGION` | No | us-west-2 | AWS deployment region |
| `AWS_PROFILE` | No | default | AWS credentials profile |
| `GBAW_ORCHESTRATOR_MODEL_ID` | No | `global.anthropic.claude-haiku-5-5` | Orchestrator model/profile |
| `GBAW_SPECIALIST_MODEL_ID` | No | `global.anthropic.claude-sonnet-5-5` | All specialist models/profiles |
| `GBAW_BEDROCK_MODEL_ID` | No | unset | Legacy orchestrator alias |
| `GBAW_BEDROCK_MODEL_ID_SECONDARY` | No | unset | Legacy specialist alias |
| `GBAW_TENANT_ID` | No | `default-tenant` | Server-side trusted tenant binding |
| `GBAW_WORKSPACE_ID` | No | `default-workspace` | Server-side trusted workspace binding |
| `NEXT_PUBLIC_SKIP_AUTH` | No | false | Skip auth (dev only) |
| `GBAW_MEMORY_LONG_TERM_ENABLED` | No | true | Enable cross-session memory |
| `GBAW_BEDROCK_GUARDRAIL_ENABLED` | No | true | Enable AI safety Guardrails |
| `GBAW_CONFIGURE_ACCOUNT_OBSERVABILITY` | No | false | Opt in to account-wide X-Ray / CloudWatch Logs changes (trace destination, indexing rule, shared Logs resource policy). See [Account-wide Observability](#account-wide-observability-scoped-and-opt-in) |
| `GBAW_XRAY_DEFAULT_INDEXING_PERCENT` | No | unset | Opt-in only: X-Ray default indexing (sampling) percent, integer [0, 100]. Left unchanged when unset |
| `GBAW_OBSERVABILITY_RESOURCE_POLICY_NAME` | No | `GameAgentTransactionSearchXRayAccess` | Opt-in only: project-owned CloudWatch Logs resource policy name |
| `GBAW_OBSERVABILITY_ACTIVE_MAX_ATTEMPTS` | No | 30 | Opt-in only: max polls for the trace destination to reach ACTIVE |
| `GBAW_OBSERVABILITY_ACTIVE_RETRY_SECONDS` | No | 10 | Opt-in only: delay between ACTIVE polls, in seconds |
| `GBAW_DELIVERY_MAX_ATTEMPTS` | No | 4 (capped at 20) | Runtime trace-delivery retry attempts |
| `GBAW_DELIVERY_RETRY_SECONDS` | No | 5 (capped at 60) | Delay between trace-delivery retries, in seconds |
| `GBAW_TRACE_DESTINATION_ACTIVE_MAX_ATTEMPTS` | No | 30 | Step 2b: polls for the X-Ray trace destination to reach `ACTIVE` before creating the trace delivery |
| `GBAW_TRACE_DESTINATION_ACTIVE_RETRY_SECONDS` | No | 10 | Step 2b: delay between trace-destination `ACTIVE` polls, in seconds |
| `GBAW_STOP_ON_ACCOUNT_OBSERVABILITY_CHANGE` | No | false | Fail-closed: stop before `agentcore launch` if the AgentCore starter toolkit would change account-wide X-Ray / CloudWatch Logs settings. Only the exact value `true` enables it. See [AgentCore Starter Toolkit Transaction Search](#agentcore-starter-toolkit-transaction-search-launch-time-exception) |

## Security Notes

- **Authentication**: Cognito enforces admin-only user creation
- **Identity propagation**: Authorization uses verified access-token claims plus server-bound tenant/workspace; ID-token presentation data does not grant backend authority
- **AI Safety**: Bedrock Guardrails filter harmful content
- **IAM**: Least-privilege roles for all components
- **EKS Access**: Read-only permissions, secrets excluded
- **Data**: All data stays within your AWS account
