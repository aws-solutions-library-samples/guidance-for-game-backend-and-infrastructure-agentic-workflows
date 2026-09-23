# E5 Bounded-Autonomy Control Plane (issue #440)

> **Status:** OPTIONAL, default-**disabled**, default-**unprovisioned** ($0).
> The main `deploy-all.sh` path never provisions this. Enabling it is a
> deliberate, reviewed, **double-opt-in** owner action.

E5 adds a *bounded autonomy runtime* on top of the accepted E1 observation, E2
advise, E3 bounded-execution, and E4 control planes. A scheduled **evaluator**
Lambda reads the reused operations table, evaluates the **server-owned autonomy
policy**, and — only when **both** independent enable gates admit it — starts the
**exact existing E3 Standard workflow** with an identifier only. The single
GameLift capacity write is still performed **only** by the unchanged E3 executor.

## What E5 does and does not do

- **Does:** evaluate a fully-clamped `[0, 1]` capacity policy with a single-
  instance step on **one** enrolled demo/test fleet, behind atomic
  budget/cooldown/frequency/concurrency limits, and start the E3 workflow with
  `{operation_id}` only when authorized.
- **Does not:** hold any GameLift IAM, invoke the executor directly, create an
  executor or a new workflow, touch the chat runtime, or change the v1 human-
  approval path or the E4 kill-switch document. The chat path stays read-only.

## The two independent enable gates

Autonomy is admitted only when **all** of the following hold; each is de-escalation-only:

1. **Static `operate` ceiling.** `GBAW_OPERATIONS_MODE=operate` **and** the
   executor capability maximum is exactly `operate`. The evaluator refuses at
   startup otherwise.
2. **Separate AppConfig autonomy switch.** A **different** AppConfig document
   from the E4 kill switch (`AutonomySwitchProfileId` ≠ `KillSwitchProfileId`),
   so enabling the E4 kill switch can never enable autonomy. The evaluator reads
   **both** the E4 kill switch and the autonomy switch; either one closed denies.

Plus the deploy-time **double opt-in**: an explicit `--enable` flag **and** the
confirmation token `GBAW_OPERATIONS_AUTONOMY_CONFIRM=operate`.

## Two-lever emergency disable (reversible, deletes nothing)

`AutonomyMode=disabled` fails closed on two independent levers:

1. the EventBridge schedule rule is set to **DISABLED**, so the evaluator never
   fires on a timer; and
2. `GBAW_OPERATIONS_AUTONOMY_ENABLED=false` / `GBAW_OPERATIONS_MODE=disabled` are
   injected, so any invocation **fails closed at startup** before any evaluation
   or pre-write.

The AppConfig autonomy switch is the immediate lever: flipping it closed blocks
the next evaluation and any immediate pre-write, without a redeploy.

## Deploy / disable / teardown

All wrappers live in `scripts/infrastructure/` and are Bash-only (the optional
operations planes E3/E4/E5 are intentionally shell-only; on Windows run them from
WSL2). Identifiers below are **synthetic** placeholders.

```bash
# 1) READ-ONLY preview (default; lints + parses the 09 template; creates nothing):
scripts/infrastructure/deploy-operations-autonomy.sh

# 2) Enable (DOUBLE opt-in). Requires the confirmation token AND operate mode,
#    an explicit artifact bucket, the 06 table + CMK, the EXACT 07 workflow ARN,
#    the pinned policy id/version/hash, the durable state id, the automation
#    subject/client, and BOTH AppConfig switch profiles (which must differ):
GBAW_OPERATIONS_MODE=operate \
GBAW_OPERATIONS_AUTONOMY_CONFIRM=operate \
GBAW_OPERATIONS_ARTIFACT_BUCKET=my-explicit-artifact-bucket \
GBAW_OPERATIONS_TABLE_NAME=game-agent-operations \
GBAW_OPERATIONS_KMS_KEY_ARN=arn:aws:kms:us-west-2:000000000000:key/EXAMPLE \
GBAW_OPERATIONS_EXECUTION_STATE_MACHINE_ARN=arn:aws:states:us-west-2:000000000000:stateMachine:game-agent-operations-execution \
GBAW_OPERATIONS_ENROLLED_FLEET_ID=fleet-0000aaaa-11bb-22cc-33dd-4444eeee5555 \
GBAW_OPERATIONS_ENROLLED_FLEET_ARN=arn:aws:gamelift:us-west-2:000000000000:fleet/fleet-0000aaaa-11bb-22cc-33dd-4444eeee5555 \
GBAW_OPERATIONS_TRUSTED_AUDIENCE=aud.default \
GBAW_OPERATIONS_TENANT_ID=tenant.default \
GBAW_OPERATIONS_WORKSPACE_ID=workspace.default \
GBAW_OPERATIONS_AUTONOMY_SUBJECT=automation.gamelift-capacity-autonomy \
GBAW_OPERATIONS_AUTONOMY_CLIENT=client.gamelift-capacity-autonomy \
GBAW_OPERATIONS_AUTONOMY_POLICY_ID=policy.gamelift-capacity-autonomy \
GBAW_OPERATIONS_AUTONOMY_POLICY_VERSION=2026-09-01 \
GBAW_OPERATIONS_AUTONOMY_POLICY_HASH=sha256:<64-hex> \
GBAW_OPERATIONS_AUTONOMY_STATE_ID=state.gamelift-capacity-autonomy \
GBAW_OPERATIONS_APPCONFIG_APPLICATION_ID=<app-id> \
GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT_ID=<env-id> \
GBAW_OPERATIONS_APPCONFIG_PROFILE_ID=<e4-kill-switch-profile-id> \
GBAW_OPERATIONS_AUTONOMY_SWITCH_PROFILE_ID=<separate-autonomy-profile-id> \
GBAW_OPERATIONS_AUTONOMY_POLICY_FILE=./autonomy-policy.json \
GBAW_OPERATIONS_AUTONOMY_WINDOW_STATE_FILE=./autonomy-window-state.json \
  scripts/infrastructure/deploy-operations-autonomy.sh --enable

# 3) Reversible emergency disable (keeps everything, deletes nothing):
scripts/infrastructure/disable-operations-autonomy.sh --confirm

# 4) Explicit teardown (never automatic; exact token required):
scripts/infrastructure/teardown-operations-autonomy.sh --confirm delete-operations-autonomy
```

### What `--enable` does, in order

1. Fails closed unless the double opt-in and every binding are present.
2. Verifies the caller identity/region and the **explicit** artifact bucket
   (owner + region; never created or discovered).
3. Resolves the **official** AppConfig extension layer from the public SSM
   parameter and pins the resolved ARN.
4. Deterministically packages the evaluator with the **complete pinned SDK
   closure** for the Lambda ABI, verifies no host-arch wheels, and import-probes
   the handler (which must **not** build a GameLift client).
5. Uploads the artifact under a content-hash key.
6. **Seeds** the server-owned policy and the initial durable window state with
   **conditional, immutable** writes (`attribute_not_exists` / hash-bound); an
   identical re-seed is idempotent, a differing one is refused without overwrite.
7. Size-safely deploys the 09 stack via `--template-url` (over the 51,200-byte
   inline limit) after service-validating it, then deletes the transient
   template object.
8. **Verifies** the exact 07 E3 workflow is a **STANDARD** Step Functions
   state machine in the **same account and region** before enabling (refusing an
   EXPRESS or cross-account/region workflow).
9. **Verifies** the deployed evaluator `CodeSha256` matches the built artifact,
   failing closed on any mismatch.
10. **Enables the 07 executor pre-write hook**: updates the 07 stack to
    `AutonomyMode=operate` and binds the 09-created autonomy AppConfig ids and
    policy pins, reusing every other 07 value. The evaluator only *starts* the
    workflow; the executor's own pre-write hook is the gate that re-checks the
    autonomy switch and durable window before the single provider write.

## Cost

See [`operations-e5-cost-notes.md`](operations-e5-cost-notes.md) (machine-checked
by `operations-e5-cost-model.json`). Default $0; provisioned-disabled ≈ $0.40/mo;
enabled-idle ≈ $0.44/mo, incremental over E1–E4 (four AWS-emitted alarms).

## Invariants (enforced by tests)

- Default deploy creates **zero** resources and costs **$0**.
- No autonomy component has any provider-write permission or executor credential;
  the only GameLift write grant is the unchanged E3 executor role.
- Only the existing authenticated E3 Standard workflow can invoke the executor;
  the evaluator's only outbound authority is `states:StartExecution` on the exact
  E3 workflow, plus three DynamoDB item actions, KMS via DynamoDB, and AppConfig
  read on the two exact switch documents.
- The autonomy AppConfig switch is separate from the E4 kill switch; the E4
  schema is unchanged.
- Emergency disable blocks evaluation and immediate pre-write, and also flips
  the **07 executor** to `AutonomyMode=disabled` so an in-flight pre-write
  closes (both stacks fail closed; nothing is deleted).
- The evaluator log group uses **default (service-managed) CloudWatch**
  encryption; the 06 CMK cannot encrypt a log group, so no CMK is attached.
- The four owned alarms all watch **AWS-emitted** metrics (evaluator errors +
  throttles, scheduled-evaluation delivery failures, DLQ depth); the evaluator
  emits no custom metrics, and the AppConfig deployment rollback monitor keys
  off the AWS/Lambda errors alarm.
- The scheduled EventBridge rule is DISABLED unless the server-owned closed
  #439 event (observation operation id + exact desired/min/max) is fully
  specified; it never emits a synthetic event.
