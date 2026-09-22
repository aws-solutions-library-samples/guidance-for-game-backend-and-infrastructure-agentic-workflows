# E4 Operations Control Plane — Deployment, Runbooks & Kill Switch

This runbook covers the **optional, default-unprovisioned** E4 operations
*control plane* for GitHub issue
[#416](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/416).
It adds the deployment-wide **AWS AppConfig kill switch** and the **admin control
API** on top of the accepted E1 observation (#413), E2 advise / human-approval
(#414), and E3 execution (#415) control planes. It is delivered as a **separate**
CloudFormation stack, `infrastructure/cloudformation/08-operations-control-plane.yaml`.

> This document describes an **optional** design. Following it does not assert
> that the control-plane stack is deployed. The base stack and normal deployment
> (`./deploy-all.sh`) never create any E4 resource. A default deploy of the 08
> stack is **$0** and provisions **zero** resources.

## Where E4 sits

```
E1 observe (#413) → E2 advise + approval (#414) → E3 execute (#415)
                          ▲                              ▲
                          └──────── E4 kill switch ──────┘  (deployment-wide, AppConfig)
```

E4 is a **new, separate** stack so that:

- a default deployment provisions nothing, and no chat / general API / model /
  E1 / E2 / E3 role gains AppConfig authoring authority; and
- the only component that can author the kill-switch document and start a
  deployment is the E4 control role, scoped to exactly the E4 AppConfig
  application / environment / profile / strategies.

E4 reuses the 06 DynamoDB table + CMK **only** to append immutable, hash-bound
control-audit records. It never gains provider-write, `iam:PassRole`, or secrets
authority.

## Two independent levers (provisioning vs. runtime authority)

Like 06/07, the 08 stack separates two concepts:

- **`Provisioned`** (default `false`) — resource **existence**. `false` creates
  zero resources ($0). Flipping it back to `false` is a **teardown**, not a
  disable.
- **`ControlMode`** (default `disabled`) — runtime **authority** for the control
  API. `disabled` fails closed on two levers: (1) the control API stage throttles
  to zero, and (2) `GBAW_OPERATIONS_CONTROL_MODE=disabled` is injected so the
  control Lambda fails closed before authoring any hosted version or starting any
  deployment. An emergency disable flips `ControlMode` to `disabled` while
  keeping `Provisioned=true`, so nothing is deleted and re-enable is reversible.

Note the control-plane `ControlMode` lever governs **who may change the switch**;
it is independent of the **kill-switch document** itself (the deployment-wide
`operations_enabled` master switch the control plane writes into AppConfig).

## The AppConfig kill switch

The stack creates one AppConfig **application**, one **environment**, and one
**hosted configuration profile** whose `JSON_SCHEMA` validator is
**byte-equivalent** to the frozen contract schema
`backend/src/operations/contracts/schemas/v1/operations-kill-switch.schema.json`.
Because the schema is self-contained (every `$ref` is a local `#/$defs`
fragment; no external `urn:` reference), AppConfig validates every hosted version
directly, with no reference resolution. Any document that is not a valid
kill-switch is rejected by AppConfig at author time.

The seeded **default** hosted version disables everything
(`operations_enabled=false`, all phases `false`) and carries an already-expired
freshness window, so a fresh deploy fails closed until the control plane issues a
current document.

Two deployment strategies:

- **gradual** (`game-agent-operations-gradual`) — linear rollout with a bake
  window and a **CloudWatch monitor + automatic rollback** wired to the
  `KillSwitchFailed` and `KillSwitchUnverified` alarms. Used for ordinary
  enable / tighten changes.
- **immediate** (`game-agent-operations-immediate`) — 100% instantly, no bake.
  Used for the emergency "disable everything now" path.

Consumers (the E1/E2 operations Lambda and the E3 dispatcher/executor) read the
current document **in-process** via the official AppConfig Agent Lambda
extension. The extension layer and the application/environment/profile
identifiers are injected into 06/07 **additively**: a deploy that does not supply
them behaves exactly as before, and the read IAM grants **only**
`appconfig:StartConfigurationSession` + `appconfig:GetLatestConfiguration`,
scoped to the exact kill-switch configuration resource.

## Deploy (opt-in, double confirmation)

A default deploy is $0. To provision and enable:

```bash
GBAW_OPERATIONS_CONTROL_MODE=enabled \
COGNITO_ISSUER=... COGNITO_CLIENT_ID=... \
GBAW_OPERATIONS_TENANT_ID=... GBAW_OPERATIONS_WORKSPACE_ID=... \
GBAW_OPERATIONS_TABLE_NAME=... GBAW_OPERATIONS_KMS_KEY_ARN=... \
GBAW_OPERATIONS_ARTIFACT_BUCKET=... \
AWS_PROFILE=<profile> AWS_REGION=<region> \
  scripts/infrastructure/deploy-operations-control.sh --enable
```

Preview only (read-only, creates nothing):

```bash
scripts/infrastructure/deploy-operations-control.sh
```

The wrapper resolves the **official** AppConfig extension layer to the
**authoritative regional version** from the public SSM parameter
`/aws/service/aws-appconfig/lambda-extension/x86/latest`, verifies the resolved
ARN is the official layer shape, and passes it as `AppConfigExtensionLayerArn`.
It verifies an explicit, owned profile/account/region and the explicit artifact
bucket before any write, builds a **deterministic** content-hash-addressed zip,
and fails closed if the frozen control handler module is absent.

## Runbook: emergency disable (reversible)

Two reversible, data-preserving emergency paths, in increasing blast radius:

1. **Disable the control API** (stop new admin changes; leave the current
   kill-switch document in place):

   ```bash
   scripts/infrastructure/disable-operations-control.sh --confirm
   ```

   Flips `ControlMode=disabled`, keeps `Provisioned=true`; deletes nothing.

2. **Per-capability hard-down** (disable one capability's phases now, via the
   immediate strategy):

   ```bash
   scripts/infrastructure/disable-operations-capability.sh --confirm \
     --capability gamelift.capacity-adjustment
   ```

3. **Deployment-wide hard-down** (disable ALL operations now):

   ```bash
   scripts/infrastructure/disable-all-operations.sh --confirm
   ```

   Deploys a fresh all-disabled document (`operations_enabled=false`) via the
   immediate strategy. Reversible — issue an enabling document through the
   control plane to restore operations. Deletes nothing.

All three verify identity/region and require an explicit `--confirm`. None
deletes a resource; each is fully reversible.

## Runbook: rollback (a bad kill-switch deployment)

- A **gradual** deployment that breaches the `KillSwitchFailed` or
  `KillSwitchUnverified` alarm during its bake window is **rolled back
  automatically** by AppConfig (the environment monitor). No action needed
  beyond confirming the alarm cleared.
- To roll back manually, `StopDeployment` (the control role holds it, scoped to
  the exact environment) reverts to the previously deployed version.
- If a rollback itself fails, the `KillSwitchRollbackFailed` alarm fires; deploy
  the safe default via `disable-all-operations.sh` (immediate) to force a known
  all-disabled state, then investigate.

## Runbook: investigation

1. **Which document is live?** Read the deployed configuration through the
   control API `GET /operations/control/kill-switch`, or inspect the AppConfig
   environment's latest deployment.
2. **Is it fresh?** Compare `not_after` against now. A stale document means the
   freshness sweeper (`game-agent-operations-control-expiry-sweeper`, a 2-minute
   EventBridge rule that targets the control Lambda) stopped refreshing —
   consumers fail closed. Check the control Lambda log group
   `/aws/lambda/game-agent-operations-control` and the sweeper log group
   `/aws/lambda/game-agent-operations-control-sweeper`.
3. **Alarm map** (namespace `GameAgent/Operations`):
   - `KillSwitchFailed` — a control write or deployment failed.
   - `KillSwitchStuck` — a deployment is past its bake window.
   - `KillSwitchRetrying` — the control plane is retrying.
   - `KillSwitchUnverified` — a deployed document could not be read back fresh.
   - `KillSwitchRollbackFailed` — a rollback did not complete.
   - `KillSwitchBudgetExceeded` — deployments started exceeded the budget
     (runaway loop / denial-of-wallet guard).
   - `OperationsDisabled` — the master switch is engaged (informational).
4. **Audit trail** — every admin decision is an immutable, `record_hash`-bound
   control-audit record in the 06 table.

## Runbook: reconciliation

The kill-switch document is the **source of truth** for whether a phase is
permitted; the backend re-checks it on every request regardless of UI state.
To reconcile:

1. Read the live document and its `config_version`.
2. Compare against the intended posture (the last approved control action in the
   audit trail).
3. If they disagree, issue a fresh document through the control plane with the
   correct booleans and the expected `config_version` (compare-and-set), so a
   stale write cannot clobber a newer document. `config_version` is immutable and
   monotonic.
4. Confirm the new document is live and fresh (`GET
   /operations/control/kill-switch`), and that `OperationsDisabled` reflects the
   intended master-switch state.

## Teardown (explicit, never automatic)

```bash
scripts/infrastructure/teardown-operations-control.sh --confirm delete-operations-control
```

Deletes only the 08 stack. The 06 table + CMK are not owned by this stack and are
untouched. Prefer `disable-operations-control.sh` for a reversible OFF.

## Cost

Default (unprovisioned): **$0**. Enabled-idle and a representative busy month are
both ≈ **$2.87/month** incremental, dominated by the seven CloudWatch alarms and
custom metrics; AppConfig config retrievals and the request-scoped control Lambda
/ API / sweeper are fractions of a cent. See
[`operations-e4-cost-model.json`](operations-e4-cost-model.json) (the machine-checked
source of truth) and [`operations-e4-cost-notes.md`](operations-e4-cost-notes.md).
