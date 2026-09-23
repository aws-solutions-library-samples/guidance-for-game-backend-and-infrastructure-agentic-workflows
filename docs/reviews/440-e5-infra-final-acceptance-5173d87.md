# Final acceptance review — issue #440 E5 bounded-autonomy infrastructure

- **Reviewed commit:** `5173d87` ("docs(operations): Reconcile E5 runbook and add composed drift tests")
- **Baseline:** `d09ee06`
- **Worktree:** `logs/review-e5-infra-5173d87` (git worktree pinned to `5173d87`)
- **Method:** read-only. No source edited, no AWS calls, no push. Wrappers/templates
  reviewed as data + fakes; tests, `cfn-lint`, and `shellcheck` run against the worktree.
- **Verdict:** **NOT ACCEPTED — one hard blocker.** All twelve previously-tracked blockers
  are resolved, but the E5 template (`09-operations-autonomy.yaml`) contains a real
  CloudFormation **circular dependency (E3004)** that prevents the `--enable`
  (`Provisioned=true`) stack from being created. The default (`Provisioned=false`) $0
  path and all safety invariants are unaffected.

---

## BLOCKER — B0: circular dependency blocks the enable-path stack create (NEW)

`cfn-lint 1.57.0` reports three `E3004 Circular Dependencies` on
`09-operations-autonomy.yaml`. The cycle is real (not an empty-default artifact) and
runs through three `Condition: ResourcesProvisioned` resources, i.e. exactly the
resources the `--enable` wrapper path creates:

1. `EvaluatorFunction` → `AutonomyEnvironment`
   line 840: `GBAW_OPERATIONS_AUTONOMY_APPCONFIG_ENVIRONMENT: !Ref AutonomyEnvironment`
2. `AutonomyEnvironment` → `AutonomyEvaluatorErrorsAlarm`
   line 472: `Monitors: - AlarmArn: !GetAtt AutonomyEvaluatorErrorsAlarm.Arn`
3. `AutonomyEvaluatorErrorsAlarm` → `EvaluatorFunction`
   line 923: `Dimensions: - Name: FunctionName / Value: !Ref EvaluatorFunction`

`!Ref AutonomyEnvironment` and `!Ref EvaluatorFunction` each create a hard
CloudFormation dependency edge, so `EvaluatorFunction → AutonomyEnvironment →
AutonomyEvaluatorErrorsAlarm → EvaluatorFunction` is an unbreakable cycle.
CloudFormation rejects the stack at create time; the double-opt-in
`deploy-operations-autonomy.sh --enable` path (which sets `Provisioned=true`,
`AutonomyMode=operate`) therefore cannot succeed.

**Why the test suite missed it:** the E5 infra tests load the template with a
short-form-flattening YAML loader (`_cfn_yaml.load_cfn_template`) and assert structural
facts (resource counts, conditions, env vars, IAM shape). They perform **no**
topological/dependency-cycle analysis, and the only `cfn-lint` invocation in the repo
is a benign always-pass fake used by the unrelated observe/execute template-size test.
So 129 E5 tests + 2060 operations tests pass while the template still cannot deploy.

**Suggested remediation (not applied — review only):** break one edge. Options:
inject the AppConfig environment coordinate into the Lambda by `!Sub`-constructing the
environment id from stable inputs instead of `!Ref AutonomyEnvironment`; or dimension
the errors alarm on a fixed `!Sub` function name instead of `!Ref EvaluatorFunction`;
or point the AppConfig `Monitors` at an alarm that does not itself depend on the
function. Any one break resolves the cycle. Add a real `cfn-lint` (or CloudFormation
dependency-graph) check to the E5 template tests so this class of defect is caught.

**Scope:** default path is safe. With `Provisioned=false` (the template default), all
three resources are conditioned out, so no cycle exists at rest and the default remains
zero-resource / $0.

---

## Previously-tracked blockers — all resolved at 5173d87

| # | Prior blocker | Status | Evidence at 5173d87 |
|---|---|---|---|
| 1 | Wrapper passes AppConfig ids / params not declared by template; `EnrolledFleetArn`/`TrustedAudience` omitted | RESOLVED | Every param the wrapper passes to 09 is declared in the template (set-diff empty). Both `EnrolledFleetArn` and `TrustedAudience` are passed (deploy block) and declared. |
| 2 | E4 kill switch and E5 autonomy share one AppConfig app (aliasing); E4 unreachable | RESOLVED | 09 creates a **separate** `AutonomyApplication`/`AutonomyEnvironment`/`AutonomySwitchProfile`. Evaluator reads BOTH the E4 (08) coordinate and the E5 coordinate via two distinct scoped grants. 07 asserts `AutonomySwitchProfileId != KillSwitchProfileId`. |
| 3 | Log group encrypted with a 06 CMK whose key policy can't authorize Logs → AccessDenied | RESOLVED | `EvaluatorLogGroup` uses default service-managed encryption (no `KmsKeyId`); `RetainExceptOnCreate`/`UpdateReplacePolicy: Retain` preserve audit data. Comment documents the rationale. |
| 4 | Safe disabled AppConfig document validated but never Deployed | RESOLVED | `DefaultDisabledAutonomyDeployment` deploys `DefaultDisabledAutonomyVersion` via the immediate strategy. Doc is `autonomy_enabled=false`, `autonomous_write=false`, freshness window already expired (`1970-...`). Validated by `JSON_SCHEMA` on the profile **and** in-code (`operations.autonomy_switch`). |
| 5 | Alarms/monitor watch never-emitted custom `GameAgent/Operations` metrics | RESOLVED | All four alarms watch AWS-emitted metrics: `AWS/Lambda` Errors + Throttles, `AWS/Events` FailedInvocations, `AWS/SQS` DLQ depth. The AppConfig rollback monitor keys off the AWS/Lambda Errors alarm. `TreatMissingData: notBreaching`. |
| 6 | Scheduled EventBridge Input violates the closed #439 evaluator event | RESOLVED | Rule `State: !If [ScheduledEvaluationEnabled, ENABLED, DISABLED]`; Input carries exactly `observation_operation_id` + `desired`/`minimum`/`maximum` (the closed #439 event), no identity/policy/limits. |
| 7 | Deploy never flips 07 `AutonomyMode=operate` (executor pre-write hook not engaged) | RESOLVED | Wrapper reads back 09 AppConfig outputs and `update-stack`s the existing 07 stack with `AutonomyMode=operate` + autonomy bindings (drift-proof `UsePreviousValue` for all other params, `--use-previous-template`). 07 injects the autonomy env into the **existing** executor. |
| 8 | Disable never flips 07 back (pre-write not closed) | RESOLVED | `disable-operations-autonomy.sh` flips 09 → `AutonomyMode=disabled` (both levers) then 07 → `AutonomyMode=disabled` (closes pre-write), `UsePreviousValue` for the rest. Deletes nothing; `Provisioned` stays true. |
| 9 | State-machine ARN syntax-only, not the exact 07 STANDARD workflow; same account/region unverified | RESOLVED | Preflight parses region/account from the ARN and refuses on mismatch, then `describe-state-machine` and refuses unless `type == STANDARD`. Measured, not syntactic. |
| 10 | Shakedown probes nonexistent routes; echoes a self-asserted preflight | RESOLVED | `ADAPTER_COMMAND_CONTRACT` maps each lifecycle step to a concrete AWS operation (lambda invoke, stepfunctions Describe, dynamodb GetItem, cloudtrail LookupEvents, gamelift DescribeFleetCapacity, appconfig deploy). Docstring states the HTTP `path` strings are an in-memory test seam only. Preflight is a hard gate: `refusal_codes()` = preflight refusals + both confirmations; run proceeds only when empty. |
| 11 | No guaranteed finally restore + disable in the shakedown | RESOLVED | `run()` wraps the sequence in `try/finally`; `_guaranteed_restore_and_disable` runs in `finally`, re-reads capacity, issues the separately-confirmed inverse `1→0` if not zero, then disables; never re-raises (records FAILED). Summary enforces `_FORBIDDEN_MARKERS` (no ARN/token/AKIA leakage). |
| 12 | Docs / cost-model / retention disagree with the template; composed reconciliation suite missing | RESOLVED (see B0 caveat) | `docs/operations-e5-cost-model.json` is machine-checkable: default `$0`, provisioned-disabled `$0.40` (4 alarms × $0.10), enabled-idle `$0.44`; 4 alarms and 0 custom metrics reconciled against the template by tests. Composed suite `test_operations_e5_composed_unit.py` present. It does **not** reconcile deployability (B0). |

---

## Invariants verified

- **Default zero resources / $0:** `Provisioned` defaults `false`, `AutonomyMode` defaults
  `disabled`; every 09 resource is `Condition: ResourcesProvisioned`. Cost model
  `default_unprovisioned.monthly_total_usd = 0.0`. The B0 cycle does not exist at default.
- **No evaluator provider write:** `EvaluatorRole` holds no gamelift permission, no
  `lambda:InvokeFunction`, no `iam:PassRole`, no wildcard write, and never the invalid
  `dynamodb:TransactWriteItems` action. Its only cross-component authority is
  `states:StartExecution` on the exact `ExecutionStateMachineArn`.
- **Sole existing executor writer:** the single gamelift capacity-write grant remains the
  unchanged E3 executor role in 07. The only 07 IAM addition is an additive AppConfig
  **read** policy on the existing `ExecutorRole` (Condition `AutonomyOperate`). No second
  executor, no second write role (`test_no_second_executor_or_provider_write_function`).
- **Main deploy exclusion:** 09 is deployed only by the opt-in
  `deploy-operations-autonomy.sh --enable`; it is not part of the default `scripts/deploy.sh`
  workflow.

## Checks run (worktree at 5173d87)

- `pytest` E5 targeted (composed, infra, shakedown, cost-model, wrapper-param-drift,
  wrappers, appconfig-split): **129 passed**.
- `pytest tests/unit -k operations`: **2060 passed**, 0 failed.
- `pytest tests/runbook/test_e5_cost_model_unit.py`: **7 passed**.
- `shellcheck` on the three wrappers: **clean (exit 0)**.
- `cfn-lint 07-operations-execution.yaml`: only pre-existing `W1030` empty-default and
  `W1001` conditional-ref warnings (no errors).
- `cfn-lint 09-operations-autonomy.yaml`: **3 × E3004 circular-dependency errors** (B0),
  plus the same benign `W1030` empty-default warnings.

## Bottom line

Twelve prior blockers are genuinely fixed and the safety invariants hold, but the enable
path is undeployable because of the B0 circular dependency. Fix one edge of the
`EvaluatorFunction ↔ AutonomyEnvironment ↔ AutonomyEvaluatorErrorsAlarm` cycle and add a
real dependency-graph / `cfn-lint` gate to the E5 template tests, then re-run acceptance.
