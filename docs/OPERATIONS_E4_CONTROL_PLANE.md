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
  application / environment / profile / strategies. It may read only the one
region-local cursor-signing secret created by this stack; that secret is never
returned or logged.

E4 reuses the 06 DynamoDB table + CMK **only** to append immutable, hash-bound
control-audit records. It never gains provider-write or `iam:PassRole`
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
**hosted configuration profile** whose `JSON_SCHEMA` validator is inlined
**byte-for-byte** from the frozen contract schema
`backend/src/operations/contracts/schemas/v1/operations-kill-switch.schema.json`
(the embedded validator and the contract file are the same 4039 bytes; the infra
test additionally asserts they parse to the same JSON). Because the schema is
self-contained (every `$ref` is a local `#/$defs` fragment; its own `$id` is not
a reference), AppConfig validates every hosted version directly, with no
external reference resolution. Any document that is not a valid kill-switch is rejected by AppConfig
at author time.

The seeded **default** hosted version disables everything
(`operations_enabled=false`, all phases `false`) and carries an already-expired
freshness window, so a fresh deploy fails closed until the control plane issues a
current document. Its parsed JSON is field-for-field identical to the fixture
`backend/tests/fixtures/operations/v1/operations-kill-switch.default-safe.json`
**except** the author-stamped `issued_at`/`not_after` timestamps (so the raw
bytes differ): the seeded pair is a fixed, safe, already-expired window.

Two deployment strategies:

- **gradual** (`game-agent-operations-gradual`) — linear rollout with a bake
  window. The **automatic rollback** is wired at the AppConfig **environment**
  level via its `Monitors`, which reference the real backend-emitted
  `KillSwitchUnavailable` alarm: if a gradual deployment breaches it during the
  bake window (the document could not be read back as a fresh, valid
  kill-switch), AppConfig rolls the deployment back automatically. Used for
  ordinary enable / tighten changes.
- **immediate** (`game-agent-operations-immediate`) — 100% instantly, no bake.
  Used for the emergency "disable everything now" path.

Consumers (the E1/E2 operations Lambda, the E3 dispatcher/executor, and the E4
control Lambda) read the current document **in-process** via the official
AppConfig Agent Lambda extension. The extension layer and the
application/environment/profile identifiers are injected into 06/07
**additively**: a deploy that does not supply them behaves exactly as before,
and every reader receives only `appconfig:StartConfigurationSession` +
`appconfig:GetLatestConfiguration`, scoped to the exact kill-switch
configuration resource.

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

The first authenticated admin change lazily initializes durable control CAS
state at the frozen safe seed's `config_version=1`. Any other bootstrap version
is rejected. This is how the intentionally stale, all-disabled seed is safely
recovered without treating an unreadable switch as enabled.

Each normal control document uses a deterministic AppConfig `VersionLabel`.
Hosted-version creation and deployment start use AppConfig's
`LatestVersionNumber` and `LatestDeploymentNumber` optimistic locks, then
bounded list/get reconciliation verifies exact content, deployment strategy,
and state. A lost provider response therefore reuses the same hosted version or
deployment; if the existing side effect cannot be proven, the change fails
closed instead of issuing a duplicate.

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
deletes a resource; each is fully reversible. The two direct AppConfig hard-down
scripts are break-glass paths that remain usable if the control API is unhealthy.
CloudTrail and AppConfig deployment history are the authoritative record of the
direct provider action. On the next authenticated admin change, while the fresh
hard-down document is readable, the control service atomically adopts its
higher `config_version` and boolean posture into the hash-bound control audit
stream before committing a re-enable. If that reconciliation cannot be proven,
the re-enable fails closed. Reissue a fresh all-disabled break-glass document
before recovery if the previous document has passed `not_after`.

## Runbook: rollback (a bad kill-switch deployment)

- A **gradual** deployment that breaches the `KillSwitchUnavailable` alarm
  during its bake window is **rolled back automatically** by AppConfig (the
  environment monitor). No action needed beyond confirming the alarm cleared.
- To roll back manually, `StopDeployment` (the control role holds it, scoped to
  the exact environment) reverts to the previously deployed version.
- If a manual rollback does not restore a healthy state, deploy the safe default
  via `disable-all-operations.sh` (immediate strategy) to force a known
  all-disabled state, then investigate. There is no dedicated
  "rollback-failed" alarm because the control plane emits no such signal; the
  operational signal that the system is not in a healthy readable state remains
  `KillSwitchUnavailable`.

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
3. **E4-owned alarm map** (namespace `GameAgent/Operations`). E4 provisions
   exactly **five** alarms, each on a metric the backend actually emits:
   - `<project>-operations-KillSwitchUnavailable` (`KillSwitchUnavailable`) — a
     phase failed closed because the kill-switch could not be read back as a
     fresh, valid document (extension unavailable/malformed/stale). This is the
     alarm wired to the AppConfig environment monitor for automatic rollback.
   - `<project>-operations-ControlVersionConflict` (`ControlVersionConflict`) —
     a control write hit a compare-and-set conflict (a stale or racing write was
     rejected).
   - `<project>-operations-ControlDenied` (`ControlDenied`) — a control change
     was denied on authority (non-admin / untrusted caller).
   - `<project>-operations-OperationsExpirySweepExpired`
     (`OperationsExpirySweepExpired`) — a periodic sweep expired one or more due
     operations (informational; confirms the sweeper is expiring due
     operations).
   - `<project>-operations-ExecutionHumanReconciliationRequired`
     (`ExecutionHumanReconciliationRequired`) — an executed operation requires
     human reconciliation.
4. **Reused upstream alarm coverage.** Failure / stuck / unverified conditions
   *upstream* of the control plane are already covered by the E1 (06) and E3
   (07) alarms; E4 does not duplicate them:
   - **Failure:** `<project>-operations-failures` (`ObservationFailures`),
     `<project>-operations-preparation-failures` (`PreparationFailures`),
     `<project>-operations-approval-failures` (`ApprovalFailures`) from 06; and
     `<project>-operations-DispatchFailures` (`DispatchFailures`),
     `<project>-operations-ExecutionFailures` (`ExecutionFailures`) from 07.
   - **Stuck:** `<project>-operations-stuck` (`StuckOperations`) and
     `<project>-operations-timeouts` (`ObservationTimeouts`) from 06.
   - **Unverified / not-reconciled:** the E4-owned
     `ExecutionHumanReconciliationRequired` above, plus
     `<project>-operations-approval-expired` (`ApprovalExpired`) from 06.
5. **Diagnostic (no-alarm) metrics.** Some emitted metrics are steady-state /
   success signals and are intentionally **not** alarmed, because paging on a
   healthy event is noise:
   - `ControlApplied` — a control change succeeded.
   - `ControlPublicationReconciled` — a published control document was
     reconciled against the intended posture. This is a success / no-op
     confirmation, not an actionable failure, so it has **no alarm**; its
     actionable failure siblings are already alarmed (a fail-closed read raises
     `KillSwitchUnavailable`; a rejected racing write raises
     `ControlVersionConflict`). Use it as a diagnostic metric / dashboard line.
6. **Rollback visibility** — operation detail reports `not_recorded` when no
   explicit rollback record exists. It never infers rollback from terminal
   state, workflow history, or a provider response.
7. **Audit trail** — every control-API admin decision is an immutable,
   `record_hash`-bound control-audit record in the 06 table. Break-glass direct
   AppConfig actions are recorded by CloudTrail/AppConfig history and are
   atomically imported into this audit stream before a later API re-enable.

## Runbook: reconciliation

The kill-switch document is the **source of truth** for whether a phase is
permitted; the backend re-checks it on every request regardless of UI state.
To reconcile:

1. Read the live document and its `config_version`.
2. Compare against the intended posture (the last approved control action in the
   audit trail).
3. If the live document came from a higher-version break-glass hard-down, send
   the next authenticated admin request while that document is fresh. The
   service first imports the live posture into the durable CAS/audit state with
   one atomic transaction. It then applies the requested change against that
   exact version. A race fails with `ControlVersionConflict`.
4. Otherwise, if the intended posture differs, issue a fresh document through
   the control plane with the correct booleans and expected `config_version`
   (compare-and-set), so a stale write cannot clobber a newer document.
   `config_version` is immutable and monotonic across normal and imported
   break-glass changes.
5. Confirm the new document is live and fresh (`GET
   /operations/control/kill-switch`). A successful reconciliation emits the
   `ControlPublicationReconciled` diagnostic metric; a stale-read or racing-write
   failure instead raises the `KillSwitchUnavailable` or
   `ControlVersionConflict` alarm.

## Teardown (explicit, never automatic)

```bash
scripts/infrastructure/teardown-operations-control.sh --confirm delete-operations-control
```

Deletes only the 08 stack. The 06 table + CMK are not owned by this stack and are
untouched. Prefer `disable-operations-control.sh` for a reversible OFF.

## Cost

Default (unprovisioned): **$0**. Enabled-idle and a representative busy month are
both ≈ **$2.87/month** incremental, dominated by the CloudWatch alarms and
custom metrics; AppConfig config retrievals and the request-scoped control Lambda
/ API / sweeper are fractions of a cent. See
[`operations-e4-cost-model.json`](operations-e4-cost-model.json) (the machine-checked
source of truth, which pins the alarm and custom-metric counts to the template)
and [`operations-e4-cost-notes.md`](operations-e4-cost-notes.md).
