# Issue #440 — Fix combined E5 infrastructure (read-only review at base `717c9d8`)

Read-only analysis. **No source was edited.** Every fact below was re-derived by
reading the exact blobs at `717c9d8`
(`fix(operations): Reconcile E5 infrastructure`). File/line references are to the
blobs at that commit. No AWS calls were made.

Scope: the OPTIONAL E5 bounded-autonomy control plane wired by the three wrappers
(`scripts/infrastructure/deploy-operations-autonomy.sh`,
`disable-operations-autonomy.sh`, `teardown-operations-autonomy.sh`), the
separate stack `infrastructure/cloudformation/09-operations-autonomy.yaml`, its
relationship to the reviewed E3 executor stack
`infrastructure/cloudformation/07-operations-execution.yaml`, the E1 CMK it
reuses (`06-operations-observation.yaml`), the evaluator entrypoint
`backend/src/operations/autonomy_runtime/evaluator_entry.py`, the shakedown
`backend/src/operations/validation/e5_shakedown.py`, and the cost model
`docs/operations-e5-cost-model.json`.

This review confirms each blocker against the code, states the fix shape, and —
per the task — **specifies the exact tests** to add. It does not edit source. The
tests are named so they can be added as one composed suite
(`backend/tests/unit/test_operations_e5_combined_infra_unit.py`) alongside the
existing `test_operations_e5_infra_unit.py` and
`test_operations_autonomy_wrappers_unit.py`.

Invariants that MUST remain intact through any fix (all confirmed present at
`717c9d8` and re-asserted by the tests below): default **$0 / zero resources**
(every 09 resource is `Condition: ResourcesProvisioned`, `Provisioned` default
`'false'`); the **sole `gamelift:UpdateFleetCapacity` writer is the 07 executor
role** (09 evaluator role holds only `states:StartExecution`, 09.yaml L559, and
"NO gamelift permission", L171/L469); **no provider write from the evaluator**;
**09 is excluded from the main ordered deploy** (`scripts/deploy.sh` does not
reference `09-operations-autonomy`).

---

## Blocker 1 — wrappers pass undeclared parameters and omit required `EnrolledFleetArn` / `TrustedAudience`

### Confirmed behavior at `717c9d8`

`09-operations-autonomy.yaml` declares exactly these 27 parameters (`Parameters:`
L12–246): `ProjectName, Provisioned, AutonomyMode, Environment,
OperationsTableName, OperationsKmsKeyArn, ExecutionStateMachineArn,
AppConfigExtensionLayerArn, AutonomyPolicyId, AutonomyPolicyVersion,
AutonomyPolicyHash, AutonomyStateId, AutonomySubject, AutonomyClient, TenantId,
WorkspaceId, TrustedAudience, EnrolledFleetId, EnrolledFleetArn, EnrolledLocation,
CapabilityMaximum, CodeS3Bucket, EvaluatorCodeS3Key, LambdaMemoryMb,
EvaluatorTimeoutSeconds, ReservedConcurrency, AppConfigExtensionPort`.

The deploy wrapper's `--parameter-overrides` (deploy L613–637) passes four keys
that **do not exist** in the template:
`AppConfigApplicationId`, `AppConfigEnvironmentId`, `KillSwitchProfileId`,
`AutonomySwitchProfileId`. `aws cloudformation deploy` rejects overrides that
target undeclared parameters, so the enable path fails before the stack mutates.

The same four keys appear in the disable wrapper's `update-stack --parameters`
(disable L92–95) as `UsePreviousValue=true`; `update-stack` likewise rejects a
`ParameterKey` that is not in the template, so the emergency disable path also
fails outright.

The deploy wrapper **omits** `EnrolledFleetArn` and `TrustedAudience` (and
`CapabilityMaximum`, `LambdaMemoryMb`, `EvaluatorTimeoutSeconds`,
`ReservedConcurrency`, `AppConfigExtensionPort`) from its overrides. Because they
default to `''`, the template's own `AutonomyOperate` parameter rule fails:
`EnrolledFleetArn is required when AutonomyMode=operate` (09.yaml Rules,
`Assert: !Not [!Equals [!Ref EnrolledFleetArn, '']]`, L315-316). The evaluator env
also depends on both — `GBAW_OPERATIONS_ENROLLED_FLEET_ARN: !Ref EnrolledFleetArn`
(L647) and `GBAW_OPERATIONS_TRUSTED_AUDIENCE: !Ref TrustedAudience` (L643) — so
even if the deploy succeeded the resolver would fail closed on
an empty audience/fleet ARN.

The disable wrapper additionally omits `TrustedAudience`, `EnrolledFleetArn`, and
the tuning params from its `--parameters` list, so those declared parameters
would **reset to their template defaults** on a disable rather than being
preserved with `UsePreviousValue`.

### Fix shape

Make the wrapper's override key set **exactly** the template's declared set. Do
not pass AppConfig application/environment/kill-switch/autonomy-switch **profile
ids** as stack parameters — the 09 stack **creates** its own AppConfig app,
environment, and switch profile (see Blocker 3); those four values are not
template inputs. Add `EnrolledFleetArn` and `TrustedAudience` (both required
inputs) to the enable path, validated like the other required inputs. In the
disable path, carry every declared parameter as `UsePreviousValue=true` (except
the two levers `Provisioned=true`, `AutonomyMode=disabled`) so nothing resets.

### Exact tests

- **T1a — wrapper override set equals template declared set (deploy).** Parse the
  `--parameter-overrides` block from `deploy-operations-autonomy.sh` and the
  `Parameters` keys of `09-operations-autonomy.yaml`; assert
  `passed_keys ⊆ declared_keys` (no undeclared key) and that every parameter the
  operate path needs is present. Specifically assert the four keys
  `AppConfigApplicationId, AppConfigEnvironmentId, KillSwitchProfileId,
  AutonomySwitchProfileId` are **not** passed, and `EnrolledFleetArn`,
  `TrustedAudience` **are** passed.
- **T1b — wrapper parameter set equals template declared set (disable).** Parse
  the `ParameterKey=...` list from `disable-operations-autonomy.sh`; assert every
  key is declared in the template, the same four undeclared keys are absent, and
  every declared parameter other than `Provisioned`/`AutonomyMode` is carried as
  `UsePreviousValue=true` (so none silently resets).
- **T1c — operate rule requires the fleet ARN.** Load the template and assert the
  `AutonomyOperate` rule asserts `EnrolledFleetArn` non-empty, and that the deploy
  wrapper validates/passes a `fleet-arn`-shaped `EnrolledFleetArn` before deploy.
- **T1d — env consumes audience + fleet ARN.** Assert the evaluator
  `Environment.Variables` maps `GBAW_OPERATIONS_TRUSTED_AUDIENCE` and
  `GBAW_OPERATIONS_ENROLLED_FLEET_ARN` to the corresponding `!Ref`, and that
  `resolve_autonomy_evaluator_settings` fails closed (`ValueError`) when either is
  empty.

---

## Blocker 2 — 09 aliases the E4 kill switch and the E5 autonomy switch to one profile; the separate app cannot address E4

### Confirmed behavior at `717c9d8`

09 creates its **own** AppConfig application/environment/profile
(`AutonomyApplication`, `AutonomyEnvironment`, `AutonomySwitchProfile`; 09.yaml
L362–396) and wires the evaluator env to those created resources:

```yaml
GBAW_OPERATIONS_APPCONFIG_APPLICATION: !Ref AutonomyApplication
GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT: !Ref AutonomyEnvironment
GBAW_OPERATIONS_APPCONFIG_PROFILE:     !Ref AutonomySwitchProfile
```

The env comment (09.yaml ~L659) claims "the kill-switch profile is threaded so
the E4 durable/cached execute gate the evaluator composes can read the SAME kill
switch the executor reads." But there is **no** `GBAW_OPERATIONS_KILL_SWITCH_*`
env var and no E4 application/environment/profile is passed to the evaluator. The
single `GBAW_OPERATIONS_APPCONFIG_PROFILE` points at the autonomy switch profile
itself. The evaluator resolves both a kill-switch profile and an autonomy switch
profile (`evaluator_entry.py` `resolve_autonomy_evaluator_settings`:
`kill_switch_profile=_required(source, _APPCONFIG_PROFILE_KEY)` and
`autonomy_switch_profile=_required(source, _AUTONOMY_SWITCH_PROFILE_KEY)`), so
with this wiring **both resolve to the one autonomy profile** — the E4 kill switch
is aliased away. A separate AppConfig application also cannot serve E4's profile:
the AppConfig extension reads a profile within one application/environment, and
the E4 kill switch lives in the 08 control-plane application, which this stack
never references.

### Fix shape

Thread the **08 (E4) kill-switch** application/environment/profile ids into the
evaluator as their own env vars (e.g. `GBAW_OPERATIONS_KILL_SWITCH_APPLICATION /
_ENVIRONMENT / _PROFILE`) and point `GBAW_OPERATIONS_APPCONFIG_PROFILE` (the
kill-switch profile the resolver keys on) at the **E4** profile, while
`GBAW_OPERATIONS_AUTONOMY_SWITCH_PROFILE` stays the separate autonomy profile.
Because one AppConfig extension can subscribe to multiple app/profile pairs,
supply the E4 application/environment/profile as parameters (they are external
08 outputs) and grant the evaluator role a scoped
`appconfig:GetLatestConfiguration`/`StartConfigurationSession` read on the E4
profile ARN in addition to the autonomy profile ARN. The two switches must remain
distinct documents (enabling E4 must never enable autonomy).

### Exact tests

- **T2a — two distinct AppConfig profiles resolved.** Assert
  `resolve_autonomy_evaluator_settings` yields `kill_switch_profile !=
  autonomy_switch_profile` when the env carries distinct E4 and autonomy profile
  ids, and raises when either is missing.
- **T2b — E4 profile is threaded, not aliased.** Assert the 09 env maps a
  kill-switch application/environment/profile to parameters distinct from
  `AutonomyApplication/Environment/AutonomySwitchProfile`, and that
  `GBAW_OPERATIONS_APPCONFIG_PROFILE` is **not** `!Ref AutonomySwitchProfile`.
- **T2c — evaluator role reads both profile ARNs.** Assert the evaluator role's
  AppConfig read policy resource list includes both the autonomy switch profile
  ARN and the E4 kill-switch profile ARN, and no other.
- **T2d — enabling E4 does not enable autonomy.** With an ENABLED E4 kill-switch
  document and a DISABLED autonomy switch document, assert the composed gate still
  fails closed (autonomy disabled) — separation is preserved.

---

## Blocker 3 — the safe default AppConfig switch version is never deployed

### Confirmed behavior at `717c9d8`

09 defines a `DefaultDisabledAutonomyVersion`
(`AWS::AppConfig::HostedConfigurationVersion`, L393) with a fail-closed document
(`autonomy_enabled:false`, already-expired `not_after:1970-01-01T00:05:00Z`) and
two deployment strategies (`AutonomyGradualDeploymentStrategy`,
`AutonomyImmediateDeploymentStrategy`, L417/L429). It never defines an
`AWS::AppConfig::Deployment`. A hosted configuration version that is not deployed
to the environment is not retrievable: the extension's
`GetLatestConfiguration` returns no deployed content, so the first live read of
the autonomy switch has nothing to read. There is no deployed baseline document.

### Fix shape

Add an `AWS::AppConfig::Deployment` (`Condition: ResourcesProvisioned`) that
deploys `DefaultDisabledAutonomyVersion` to `AutonomyEnvironment` on the
`AutonomySwitchProfile` using the immediate strategy, so the safe, disabled,
already-expired baseline is the deployed document from create. Later
enable/disable is a fresh deployment issued by the reviewed control plane, not by
this stack.

### Exact tests

- **T3a — a Deployment resource exists and targets the safe version.** Assert 09
  defines exactly one `AWS::AppConfig::Deployment` under `ResourcesProvisioned`
  whose `ApplicationId/EnvironmentId/ConfigurationProfileId` reference
  `AutonomyApplication/AutonomyEnvironment/AutonomySwitchProfile` and whose
  `ConfigurationVersion` is `!Ref DefaultDisabledAutonomyVersion`.
- **T3b — deployed default is fail-closed.** Parse the
  `DefaultDisabledAutonomyVersion.Content` JSON and assert `autonomy_enabled` is
  `false`, `capabilities.*.autonomous_write` is `false`, and `not_after` is in the
  past (already expired), so a live read is never treated as fresh-enabled.

---

## Blocker 4 — alarms and the AppConfig monitor target custom metrics that are never emitted

### Confirmed behavior at `717c9d8`

Two 09 alarms watch the `GameAgent/Operations` namespace on custom metric names:
`AutonomyUnavailableAlarm` → `MetricName: AutonomySwitchUnavailable` (L731) and
`AutonomyDispatchFailuresAlarm` → `MetricName: AutonomyDispatchFailures` (L748).
`AutonomyUnavailableAlarm` is also the `AutonomyEnvironment` monitor
(`Monitors: - AlarmArn: !GetAtt AutonomyUnavailableAlarm.Arn`, L377), the
mechanism that is supposed to auto-roll-back a bad autonomy-switch deployment.

Neither metric name is ever emitted. A workspace search across
`backend/src/operations` for the literal strings `"AutonomySwitchUnavailable"`
and `"AutonomyDispatchFailures"` as emitted `MetricName`s returns nothing, and the
autonomy runtime/gate/switch call no `put_metric_data` at all (`autonomy_gate.py`,
`autonomy_switch.py`, and `autonomy_runtime/*` emit none). With
`TreatMissingData: notBreaching`, an alarm on a never-published metric stays
`INSUFFICIENT_DATA`/OK forever, so the fail-closed signal never fires and the
AppConfig rollback monitor is inert — a bad autonomy-switch deployment would not
roll back.

Note the internal contradiction with the cost model (Blocker 10), which claims
the alarms watch AWS-emitted metrics.

### Fix shape

Choose one and make code and template agree:
(a) emit the two metrics from the runtime — publish `AutonomySwitchUnavailable`
(when `autonomy_switch` raises `AutonomySwitchUnavailable`, `autonomy_switch.py`
L76+) and `AutonomyDispatchFailures` (when the dispatch bracket fails closed after
an ambiguous `StartExecution`) to `GameAgent/Operations`, granting the evaluator
role scoped `cloudwatch:PutMetricData` on that namespace; **or**
(b) repoint the alarms/monitor at metrics that are actually emitted (e.g.
`AWS/Lambda Errors` on the evaluator function and `AWS/Events FailedInvocations`
on the rule) and update the monitor `AlarmArn` accordingly. The monitor alarm must
watch a metric that can breach when the autonomy switch is unreadable.

### Exact tests

- **T4a — every alarm MetricName is emitted (or AWS-native).** Collect each
  `AWS::CloudWatch::Alarm`'s `(Namespace, MetricName)` from 09. For each pair in
  the `GameAgent/Operations` namespace, assert a matching emitter exists in
  `backend/src/operations` (a `put_metric_data`/emitter call with that exact
  `MetricName`). Pairs in `AWS/*` namespaces are exempt. This test fails today for
  `AutonomySwitchUnavailable` and `AutonomyDispatchFailures`.
- **T4b — the AppConfig monitor watches a breachable metric.** Assert the
  `AutonomyEnvironment.Monitors[0].AlarmArn` alarm's `(Namespace, MetricName)`
  passes T4a (i.e., is emitted or AWS-native), so a bad autonomy-switch deployment
  can actually breach and roll back.
- **T4c — emitter fires on the fail-closed path (behavioral).** If fix (a): drive
  `autonomy_switch` to raise `AutonomySwitchUnavailable` and assert exactly one
  `AutonomySwitchUnavailable` datapoint is emitted; drive an ambiguous dispatch and
  assert one `AutonomyDispatchFailures` datapoint. If fix (b): assert no custom
  metric is emitted and the alarms reference the AWS-native metrics.

---

## Blocker 5 — the scheduled event violates the closed #439 evaluator event

### Confirmed behavior at `717c9d8`

The evaluator event contract is closed:
`_EVENT_FIELDS = frozenset({"observation_operation_id", "desired", "minimum",
"maximum"})` and `_read_closed_event` requires `keys == _EVENT_FIELDS`, appending
`unknown fields` / missing-field detail and raising `ValueError` otherwise
(`evaluator_entry.py` L91, L562–582). The `observation_operation_id` must be a
non-empty string.

The EventBridge target `Input` is `'{"source":"autonomy-evaluation"}'` (09.yaml
L701). That payload carries an **unknown** field `source` and is **missing** all
four required fields, so every scheduled invocation raises `ValueError` in
`_read_closed_event` before any work — the schedule can only ever fail to the DLQ.

### Fix shape

The scheduled tick must deliver a payload that satisfies the closed contract.
Because identity/policy/limits are server-owned and must never come from the
event, the only event-borne field is the observation id plus the requested
triple. The closed liveness tick should either (a) be replaced by a source-owned
producer (the reviewed observation pipeline) that emits a valid closed event, or
(b) if the schedule remains a liveness probe, the handler must accept a distinct,
explicitly-modeled liveness event that performs a no-op health check rather than
being fed through `_read_closed_event`. Do **not** widen `_EVENT_FIELDS` to admit
`source`; keep the dispatch contract closed.

### Exact tests

- **T5a — the schedule Input satisfies the contract (or is not a dispatch event).**
  Parse the `EvaluationRule` target `Input` JSON. If it is routed to
  `_read_closed_event`, assert `set(keys) == _EVENT_FIELDS` and
  `observation_operation_id` is a non-empty string. If a separate liveness path is
  chosen, assert the Input matches that liveness schema and that
  `_read_closed_event` is not invoked for it.
- **T5b — closed contract still rejects unknown/missing.** Assert
  `_read_closed_event({"source": "autonomy-evaluation"})` raises `ValueError`
  naming the unknown `source` field and the four missing fields (guards against a
  "fix" that loosens the contract).
- **T5c — round-trip.** A valid closed event
  `{"observation_operation_id": "...", "desired": d, "minimum": m, "maximum": M}`
  parses to the expected `(id, requested)` tuple.

---

## Blocker 6 — the deploy does not engage the 07 executor autonomy pre-write hook

### Confirmed behavior at `717c9d8`

The autonomy pre-write hook lives in the **07 executor stack**, gated by 07's own
`AutonomyMode` parameter: "When AutonomyMode=operate the EXISTING executor's
autonomy pre-write hook is engaged (autonomy env injected + autonomy AppConfig
read ...)" (`07-operations-execution.yaml` `Conditions.AutonomyOperate` and env,
L343–347, L684). 07 declares `AutonomyMode`, `AutonomyStateMachineArn`,
`AutonomySwitchProfileId`, `AutonomyApplicationId`, `AutonomyEnvironmentId`, etc.
(L185–224) and asserts them when operate (Rules, L396–419).

The deploy wrapper sets `AutonomyMode=operate` on the **09** stack only
(deploy L600–637); it never runs an `update-stack`/`deploy` against the 07 stack
and never sets 07's `AutonomyMode`. So the evaluator (once fixed) starts the 07
Standard workflow, but 07's executor still runs with `AutonomyMode=disabled`: the
autonomy env is not injected, the autonomy AppConfig read IAM is not granted, and
the executor's autonomy pre-write re-verify hook is not engaged. The autonomous
dispatch reaches an executor that is not in autonomy mode.

### Fix shape

The enable path must, as a distinct step, `update-stack` the **07** stack to
`AutonomyMode=operate` with the matching autonomy identifiers
(`AutonomyStateMachineArn`, autonomy switch profile/app/env, policy id/version/hash,
subject/client, state id), reusing the 07 stack's other values with
`UsePreviousValue`. This engages the executor pre-write hook and its scoped
autonomy AppConfig read. The 07 update must precede enabling the 09 schedule so
the first dispatch meets an autonomy-mode executor.

### Exact tests

- **T6a — enable updates 07 AutonomyMode=operate.** Assert
  `deploy-operations-autonomy.sh` issues a CloudFormation update to the 07 stack
  (`game-agent-operations-execution`) setting `AutonomyMode=operate` and the
  required autonomy identifiers, and that it does so before enabling the 09
  schedule.
- **T6b — 07 hook is conditioned on AutonomyMode.** Assert 07's `AutonomyOperate`
  condition gates the executor autonomy env and the autonomy AppConfig read IAM
  (so setting operate is what engages the pre-write hook).
- **T6c — ordering.** Assert the wrapper fails closed if the 07 update fails
  (no 09 enable proceeds on a failed 07 hook engagement).

---

## Blocker 7 — the disable does not close the 07 executor / pre-write path

### Confirmed behavior at `717c9d8`

`disable-operations-autonomy.sh` updates only the **09** stack to
`AutonomyMode=disabled` (disable L67–98). It never touches 07. So an emergency
disable stops the 09 schedule and fails the evaluator closed, but if 07 was set to
`AutonomyMode=operate` (per Blocker 6's fix), the executor's autonomy pre-write
path remains **engaged** after disable — the disable does not close 07/prewrite.
The wrapper's own header claims "BOTH autonomy levers engage" and the evaluator
"fail[s] closed at startup before any evaluation or pre-write," but that only
describes the 09 evaluator, not the 07 executor hook.

### Fix shape

The disable path must also `update-stack` the **07** stack to
`AutonomyMode=disabled` (reusing all other 07 values with `UsePreviousValue`),
closing the executor autonomy pre-write hook and dropping the autonomy AppConfig
read — data-preserving, reversible. Disable should flip 07 first (close the write
path) then 09 (stop the schedule), so no window exists where the schedule is off
but the executor still accepts an autonomous write.

### Exact tests

- **T7a — disable sets 07 AutonomyMode=disabled.** Assert
  `disable-operations-autonomy.sh` updates the 07 stack to
  `AutonomyMode=disabled` with `UsePreviousValue` for all other 07 parameters.
- **T7b — disable closes both planes.** Assert disable flips both 09
  (`AutonomyMode=disabled`, schedule DISABLED) and 07 (`AutonomyMode=disabled`,
  hook closed), and that the 07 flip precedes the 09 flip.
- **T7c — reversibility.** Assert disable deletes no resource and rebuilds no
  code (no `s3api put-object`, no zip build), consistent with a lever-only flip.

---

## Blocker 8 — the state-machine reference is syntax-only, not the exact 07 Standard workflow; the shakedown uses nonexistent routes and a self-asserted preflight

### Confirmed behavior at `717c9d8`

**Standard workflow.** 07 defines a Step Functions **STANDARD** state machine
(07.yaml header L6, comment L31) and exports its ARN
(`ExecutionStateMachineArn` → export `${ProjectName}-OperationsExecutionStateMachineArn`,
Outputs L17–22). The evaluator starts `GBAW_OPERATIONS_STATE_MACHINE_ARN`, whose
value the wrapper validates only as a syntactic Step Functions ARN
(`case ... arn:aws*:states:*:stateMachine:*`, deploy L264–267) and the template
constrains only by `AllowedPattern` (`^$|^arn:...:stateMachine:.+$`). Nothing
asserts the ARN is the **exact 07 export** or that the target is a `STANDARD`
(not `EXPRESS`) machine. A syntactically valid but foreign/EXPRESS ARN passes.

**Shakedown routes.** `e5_shakedown.py` drives HTTP routes
`POST /operations/observe`, `POST /operations/autonomy/{id}/evaluate`,
`GET /operations/autonomy/{id}/capacity`, `POST /operations/autonomy/disable`,
`POST /operations/autonomy/{id}/force-write` (L307–450). None of these routes
exist anywhere in the backend or UI at `717c9d8` (07 exposes only a `DispatchApi`;
there is no `/operations/autonomy/*` REST surface). The harness probes a fabricated
API.

**Self-asserted preflight.** `_preflight_echo_check` returns
`CheckResult(name=name, passed=bool(value))` where `value` is an operator-supplied
CLI boolean (`alarms_safe`, `drift_safe` from `_build_preflight(args)`,
L518–519/L561+). The harness echoes the operator's self-declared safety claims as
"passed" checks rather than verifying alarm state or drift.

### Fix shape

Bind the started ARN to the **exact** 07 export and confirm the target is
`STANDARD` at enable time (read the 07 stack export / `DescribeStateMachine.type`
== `STANDARD`), rather than accepting any `stateMachine:` ARN. Point the shakedown
at the real deployed surface (the 07 `DispatchApi` / the actual authenticated
operations endpoints) or convert it to an offline harness against the real handler
functions (`evaluator_entry.handler`, the executor entry) with in-memory fakes —
no fabricated routes. Replace `_preflight_echo_check` with checks that **verify**
the fact (query the alarm state; compute drift) instead of echoing an operator
flag; a self-declared boolean must not count as a passed safety check.

### Exact tests

- **T8a — started ARN is the exact 07 export and STANDARD.** Assert the enable
  path resolves the state-machine ARN from the 07 stack export
  `${ProjectName}-OperationsExecutionStateMachineArn` (not a free-form input) and
  asserts the machine type is `STANDARD` before enabling; a foreign or `EXPRESS`
  ARN is refused.
- **T8b — 07 is STANDARD.** Assert the 07 `AWS::StepFunctions::StateMachine` has
  no `StateMachineType: EXPRESS` (Standard is the default/declared type).
- **T8c — shakedown targets real routes.** Assert every path the shakedown issues
  resolves to a route/handler that exists at this commit (or, for the offline
  harness, that it calls the real handler symbols) — no path matches a
  nonexistent `/operations/autonomy/*` REST route.
- **T8d — preflight verifies, not echoes.** Assert each preflight check derives
  `passed` from an observed fact (alarm-state query result, computed drift), and
  that supplying `alarms_safe=true` while the alarm is in ALARM does **not** yield
  a passed check.

---

## Blocker 9 — no guaranteed `finally` restore + disable in the shakedown

### Confirmed behavior at `717c9d8`

`E5Shakedown.run()` (L465–512) executes the lifecycle as a linear list of
`checks.append(...)`: the forward write `0 -> 1` (steps 3–4, `self._evaluate("up",
...)`, which drives the fleet to capacity 1) happens before the inverse restore
`1 -> 0` (step 7) and the disable (step 9). There is **no `try/finally`**. If any
step between the forward write and the restore raises (step 5 `_check_capacity`,
step 6 the limits check, or any transport error), the exception propagates out of
`run()` and the fleet is left at capacity 1 with autonomy still enabled — the
restore and disable never run. The forward mutation is not bracketed by a cleanup
that always fires.

### Fix shape

Wrap the mutating lifecycle so that, once the forward `0 -> 1` write has been
issued, a `finally` **always** attempts the separately-confirmed inverse
`1 -> 0` restore and the autonomy disable, regardless of any intervening failure,
and records those cleanup attempts in the summary. The restore must use the
same separately-confirmed inverse token the normal path uses.

### Exact tests

- **T9a — restore + disable run on mid-lifecycle failure.** Inject a transport
  that raises on step 5 (`capacity_is_one`); assert the harness still issues the
  inverse `1 -> 0` write and the disable in a `finally`, and the summary records
  both cleanup attempts.
- **T9b — restore uses the confirmed inverse.** Assert the `finally` restore
  carries the REQUIRED_INVERSE_CONFIRMATION token (same as the normal step-7
  path), so cleanup is not an unconfirmed write.
- **T9c — no cleanup before a forward write.** If `run()` refuses at preflight (no
  authenticated request made), assert the `finally` issues no restore/disable
  (nothing to clean up), preserving the fail-closed / no-AWS-on-refusal property.

---

## Blocker 10 — docs / cost model / resource-retention disagree with the template

### Confirmed behavior at `717c9d8`

`docs/operations-e5-cost-model.json`:

- `design.notes` and the `custom_metrics.$comment` state E5's alarms watch
  **AWS-emitted** metrics ("evaluator errors, dispatch failures, and dead-letter
  delivery" / `AWS/Lambda Errors, AWS/Events FailedInvocations`) and that E5
  "emits NO custom CloudWatch metrics." The **template** alarms actually watch
  `GameAgent/Operations` **custom** metrics `AutonomySwitchUnavailable` and
  `AutonomyDispatchFailures` (Blocker 4) plus one `AWS/SQS` DLQ metric. So the cost
  model's alarm characterization contradicts the template's alarm definitions.
- `design.notes` says E5 "provisions NO KMS key (it reuses the 06 CMK via
  DynamoDB)". The 09 template reuses the 06 CMK to encrypt a **CloudWatch Logs**
  group (`EvaluatorLogGroup.KmsKeyId: !Ref OperationsKmsKeyArn`), not via DynamoDB
  — and that attachment is broken (Blocker 11). The "via DynamoDB" claim
  misdescribes the actual encryption use.
- `alarms.e5_owned` lists three alarms and the model reconciles `cw_alarms == 3`
  against the template's `AWS::CloudWatch::Alarm` count (which is 3 — this part
  agrees), but the metric-source description above does not.

Resource retention: the 09 `EvaluatorLogGroup` and `EvaluatorDeadLetterQueue` are
`RetainExceptOnCreate` / `UpdateReplacePolicy: Retain` (09.yaml L328–349). The cost
model / notes should state which E5 resources survive teardown; the teardown
wrapper's messaging ("retained E5 logs are unaffected") must match the template's
actual retention set.

### Fix shape

Reconcile the cost model's `design.notes`/`custom_metrics` text with the template
after Blocker 4 is resolved: if the fix keeps custom metrics, list them and their
namespace and price them; if it moves to AWS-native metrics, keep
`custom_metrics.e5_autonomy: []` and correct the "evaluator errors / dispatch
failures" wording to the actual AWS metrics. Correct the CMK sentence to describe
CloudWatch-Logs encryption (not "via DynamoDB"). State the retained-resource set
(evaluator log group + DLQ) explicitly and align the teardown wrapper's messaging.

### Exact tests

- **T10a — alarm count reconciles.** Assert `cost_model["alarms"]["e5_owned"]`
  length equals the number of `AWS::CloudWatch::Alarm` resources in 09 (already
  passes; keep as a guard).
- **T10b — custom-metric claim matches reality.** Assert that if
  `cost_model["custom_metrics"]["e5_autonomy"]` is empty, **no** 09 alarm watches a
  `GameAgent/Operations` custom metric (i.e., all alarm metrics are AWS-native);
  otherwise assert the listed custom metrics exactly equal the set of
  `GameAgent/Operations` `MetricName`s used by 09 alarms. This test fails today.
- **T10c — CMK description matches usage.** Assert the model's CMK reuse note
  matches the template's actual CMK consumer (a CloudWatch Logs group), not
  "via DynamoDB", when 09 sets `KmsKeyId` on a `AWS::Logs::LogGroup`.
- **T10d — retention set is documented.** Assert every 09 resource with
  `DeletionPolicy: RetainExceptOnCreate`/`Retain` is enumerated in the
  cost/notes retention list and matches the teardown wrapper's "retained"
  messaging.

---

## Blocker 11 — the evaluator log group references a CMK key policy that cannot encrypt it

### Confirmed behavior at `717c9d8`

09's `EvaluatorLogGroup` (L328) sets
`LogGroupName: /aws/lambda/${ProjectName}-operations-autonomy-evaluator` and
`KmsKeyId: !Ref OperationsKmsKeyArn` (the 06 CMK). Attaching a CMK to a log group
requires the CMK **key policy** to permit `logs.<region>.amazonaws.com` to
`kms:Encrypt/GenerateDataKey*/Describe*` for that log group, scoped by the
`kms:EncryptionContext:aws:logs:arn` condition.

The 06 CMK key policy statement `AllowCloudWatchLogsEncryptDecrypt`
(`06-operations-observation.yaml` L473–498) scopes that grant by an
`ArnEquals` allowlist of **exactly five** log-group ARNs:
`/aws/lambda/${ProjectName}-operations-observe`,
`/aws/apigateway/${ProjectName}-operations-access`,
`/aws/lambda/${ProjectName}-operations-dispatch`,
`/aws/lambda/${ProjectName}-operations-executor`,
`/aws/apigateway/${ProjectName}-operations-dispatch-access`.

The 09 evaluator log group ARN
(`/aws/lambda/${ProjectName}-operations-autonomy-evaluator`) is **not** in that
allowlist. When CloudWatch Logs tries to use the key for the new group, its
encryption context fails the `ArnEquals` condition → `AccessDenied` → the
`AWS::Logs::LogGroup` create fails → the 09 stack fails to provision. This is the
exact class of failure the key-policy comment says it was fixing for the other
groups.

### Fix shape

Add the evaluator log-group ARN
(`arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/lambda/${ProjectName}-operations-autonomy-evaluator`)
to the `AllowCloudWatchLogsEncryptDecrypt` statement's
`kms:EncryptionContext:aws:logs:arn` allowlist in `06-operations-observation.yaml`
(the CMK owner). Because 06 owns the key, this is a 06 change consumed by 09; do
not weaken the allowlist to a wildcard. Alternatively (if 06 cannot change),
encrypt the 09 log group with an AWS-owned/managed key — but the intended design
reuses the 06 CMK, so extending the allowlist is the fix that matches the model's
"reuses the 06 CMK" claim.

### Exact tests

- **T11a — CMK allowlist covers the evaluator log group.** Parse the 06 CMK
  `KeyPolicy` `AllowCloudWatchLogsEncryptDecrypt` statement and assert the
  `kms:EncryptionContext:aws:logs:arn` `ArnEquals` list includes the 09 evaluator
  log-group ARN. This test fails today.
- **T11b — the 09 log group uses the CMK the policy permits.** Assert
  `EvaluatorLogGroup.KmsKeyId` is `!Ref OperationsKmsKeyArn` and that the group's
  computed ARN matches an allowlisted context ARN in T11a (compatibility, not just
  presence).
- **T11c — no wildcard weakening.** Assert the statement's encryption-context
  condition remains an exact ARN allowlist (no `*` / account-wide grant).

---

## Blocker 12 — the composed reconciliation tests are missing

### Confirmed behavior at `717c9d8`

The existing suites (`test_operations_e5_infra_unit.py`,
`test_operations_autonomy_wrappers_unit.py`,
`test_operations_e5_cost_model_unit.py`, `test_operations_autonomy_env_contract_unit.py`)
assert **individual** facts about the template, wrappers, cost model, and env
contract, but none of them **reconciles across artifacts**. In particular there is
no test that:
- reconciles the wrapper `--parameter-overrides`/`--parameters` key set against
  the template's declared `Parameters` (Blocker 1) — a search of
  `test_operations_autonomy_wrappers_unit.py` for `parameter-overrides`/`declared`
  finds nothing;
- checks the EventBridge target `Input` against `_EVENT_FIELDS` (Blocker 5);
- checks every alarm `MetricName` is emitted or AWS-native (Blocker 4);
- checks the evaluator log-group ARN against the 06 CMK key-policy allowlist
  (Blocker 11);
- checks that a `Deployment` deploys the safe version (Blocker 3);
- checks that deploy/disable flip the **07** `AutonomyMode` (Blockers 6, 7);
- checks the shakedown restore/disable is `finally`-guaranteed (Blocker 9).

### Fix shape

Add one composed suite
`backend/tests/unit/test_operations_e5_combined_infra_unit.py` that loads the 09
and 07 templates, the 06 CMK policy, both wrapper scripts (parsed), the evaluator
event contract, the cost model, and the shakedown module, and asserts T1–T11
above. Keep it a pure-parse / in-memory suite (no AWS, no network), consistent
with the existing infra tests and `pytest -m unit`.

### Exact tests

- **T12a — composed suite exists and is unit-marked.** The suite is collected
  under `pytest -m unit`, imports the templates/wrappers/model without AWS, and
  contains T1a–T11c.
- **T12b — cross-artifact reconciliation runs green only when all blockers are
  fixed.** As a meta-assertion for the reviewer: at `717c9d8` T1a, T1b, T4a, T4b,
  T5a, T6a, T7a, T8a, T8c, T8d, T9a, T10b, T10c, and T11a all FAIL; the suite is
  the acceptance gate for the #440 fix.

---

## Invariants preserved by the specified fixes

| Invariant | How the fixes preserve it |
|---|---|
| Default $0 / zero resources | No fix removes a `Condition: ResourcesProvisioned`; `Provisioned` default stays `'false'`; the new `Deployment`/`update-stack` steps run only on the double-opt-in enable path. Asserted by the default-zero checks. |
| Sole `UpdateFleetCapacity` writer = 07 executor role | No fix grants the 09 evaluator role any GameLift permission; Blocker-2/4 fixes add only scoped AppConfig read (and optional `PutMetricData`), Blocker-6/7 fixes only flip 07's `AutonomyMode`. |
| No provider write from the evaluator | Evaluator keeps `states:StartExecution` only; the started target is now bound to the exact 07 STANDARD export (Blocker 8), not a new writer. |
| 09 excluded from the main deploy | Fixes touch only the optional wrappers and 09/06/07 templates; `scripts/deploy.sh` still does not reference `09-operations-autonomy`. |
| No evaluator provider write / no AWS during implementation | This review made no AWS calls; all tests specified are pure-parse/in-memory (no network, no AWS). |
| E4 kill switch and E5 autonomy switch remain distinct | Blocker-2 fix threads a separate E4 profile and keeps the autonomy profile distinct; T2d asserts enabling E4 never enables autonomy. |
| Fail-closed default | Blocker-3 fix deploys an already-expired, disabled baseline; Blocker-5 keeps the event contract closed; Blocker-9 guarantees restore+disable on failure. |
