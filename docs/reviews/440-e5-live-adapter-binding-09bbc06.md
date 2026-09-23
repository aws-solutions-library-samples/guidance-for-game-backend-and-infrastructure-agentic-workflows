# Issue #440 — Six live adapter/binding blockers in the E5 command adapter (read-only review at `09bbc06`)

**Base commit:** `09bbc06` (`fix(operations): Coerce measured preflight reads to bool for mypy`)
**Artifact under review:** `backend/src/operations/validation/e5_command_adapter.py`
**Mode:** read-only. No file was edited. This review reasons from source at `09bbc06`.

## What this review is (and is not)

The prior `717c9d8` review (`docs/reviews/440-e5-combined-infrastructure-717c9d8.md`)
covered the **CloudFormation infra** side of #440 — wrapper parameters, alarm
metrics, deployment of the safe default switch, the closed evaluator event, the
executor pre-write hook, and the guaranteed `finally` restore. Those are a
different layer and a different base.

This review covers the **live adapter binding**: the seam where
`e5_command_adapter.py` (`CommandAdapter` + `CommandTransport`) translates the
shakedown lifecycle into concrete `aws` CLI commands and parses their results.
The six blockers below are all cases where the adapter's **wire shape or storage
key does not match what #439 runtime and the 06/07/08 stacks actually declare**,
so the command either errors or the adapter *fabricates* a success/evidence
value that no live read supports. Each is grounded in the exact source that
defines the real shape, followed by the exact test that distinguishes real
parsing from fabricated success.

**Static infra passes to preserve.** The following are correct at `09bbc06` and
must NOT be disturbed by any fix:

- The evaluate event `_closed_event(...)` emits exactly
  `{observation_operation_id, desired, minimum, maximum}` and never `direction`
  — matches `evaluator_entry.py::_EVENT_FIELDS`
  (`{"observation_operation_id","desired","minimum","maximum"}`) and the
  unknown-field rejection in `_read_closed_event`.
- The `0/0/1` capacity window (`_WINDOW_MINIMUM=0`, `_WINDOW_MAXIMUM=1`,
  `_DESIRED_UP=1`, `_DESIRED_DOWN=0`).
- `ImaginaryRouteError` on any unmapped path and the unauthenticated-`401`
  short-circuit before any provider call.
- `subprocess_runner` uses `shell=False` argv (no injection); stderr is never
  echoed (only the return code is surfaced).
- The rate-limit reason vocabulary `_LIMIT_REASON_CODES`
  (`COOLDOWN_ACTIVE`/`FREQUENCY_EXCEEDED`/`CONCURRENCY_LIMIT`) → `429`.

---

## Blocker 1 — Lambda invoke concatenates metadata + payload; wrong observation ABI; fabricated `trusted`

### Confirmed behavior at `09bbc06`

`CommandAdapter._invoke_lambda` (lines ~148–161) builds:

```
aws lambda invoke --output json --region <r> [--profile <p>] \
  --function-name <fn> --cli-binary-format raw-in-base64-out \
  --payload <json> /dev/stdout
```

then routes the result through `_run_json` → `CommandResult.json_or_none` →
`json.loads(self.stdout)`.

`aws lambda invoke` writes the **function response payload** to the positional
`OutputFile` (`/dev/stdout`) **and** prints the **invocation metadata** object
`{"StatusCode":200,"ExecutedVersion":"$LATEST"}` to stdout. With `OutputFile`
set to `/dev/stdout`, both land on the same stream, producing two concatenated
JSON objects:

```
{"StatusCode":200,"ExecutedVersion":"$LATEST"}{"outcome":"dispatched","operation_id":"..."}
```

`json.loads` on that raises `ValueError` (Extra data) → `json_or_none` returns
`None` → `_run_json` raises `CommandError("command returned no JSON")`.
**Consequence:** `invoke_evaluate` and `invoke_observe` fail on *every* live
call; the "dispatched" branch of `_evaluate` is unreachable through the real
adapter.

The evaluator's real return ABI (ground truth,
`autonomy_runtime/evaluator_entry.py`):
- success: `{"outcome": "dispatched", "operation_id": <op_id>}` (line 743)
- refusals: `{"outcome": "refused", "reason": <reason>[, "operation_id": ...]}`
  (lines 557, 709, 724, 729, 741)

This is a **plain Lambda return value** — it appears in the response *payload*
(the OutputFile), never mixed into the metadata line.

**Observe ABI + fabricated `trusted`.** `CommandTransport.__call__` for the
observe path (lines ~495–497) calls `self._adapter.invoke_observe(...)` and
then **discards the result**, returning a hardcoded `_http(200, {"trusted":
True})`. The `trusted` flag is synthesized regardless of what the observe Lambda
returned (or whether it errored before this point — it will, per the
concatenation bug above). The observe function's real output is the canonical E1
observation object loaded by `_load_succeeded_observation`
(`ObservationStatusView.SUCCEEDED` + a dict `observation`); "trusted" is a
property proven by *that* object's status, not a constant.

### Exact expected wire shape

Read the payload from a **separate OutputFile**, not `/dev/stdout`, and parse
only that file:

```
aws lambda invoke --output json --region <r> [--profile <p>] \
  --function-name <fn> --cli-binary-format raw-in-base64-out \
  --payload fileb://<tmp-in>.json  <tmp-out>.json
```

Then: check the metadata on stdout has `"StatusCode": 200` **and no
`"FunctionError"`**, and separately `json.load(<tmp-out>.json)` for the real
`{outcome, operation_id|reason}`. A `FunctionError` present in metadata (e.g.
`"Unhandled"`) must fail closed even if `StatusCode==200`. The observe response
must return `trusted` only when the parsed observation payload's status is the
succeeded canonical observation — never a constant.

### Tests that distinguish real parsing from fabricated success

1. **Concatenation regression:** fake runner returns
   `stdout='{"StatusCode":200,"ExecutedVersion":"$LATEST"}'` and a distinct
   OutputFile body `{"outcome":"dispatched","operation_id":"op-1"}`. Assert the
   adapter parses `outcome=="dispatched"` from the file. A build that reads only
   `stdout` (status quo) raises `CommandError` — the test fails, exposing the
   bug.
2. **FunctionError fail-closed:** metadata
   `{"StatusCode":200,"FunctionError":"Unhandled"}` with an error payload → the
   transport must return `denied/AMBIGUOUS_RESULT`, `wrote:false`. A status-only
   check would wrongly proceed.
3. **`trusted` not fabricated:** fake `invoke_observe` returns a **non-succeeded**
   observation (or raises). Assert the observe path does NOT return
   `{"trusted": true}`. The status-quo constant makes this test fail.

---

## Blocker 2 — operation_id used as Step Functions execution ARN; wrong lowercase/incomplete DynamoDB key; synthetic attribute fields

### Confirmed behavior at `09bbc06`

**Execution ARN.** `CommandTransport._execution_arn(operation_id)` (lines
~690–701) returns `operation_id` **verbatim**, then `describe_execution` issues:

```
aws stepfunctions describe-execution --execution-arn <operation_id>
```

`--execution-arn` requires a full ARN; a bare operation id fails
`ValidationException` (rc≠0) → `describe_execution` raises `CommandError` →
`_dispatch_evidence` sets `started=False`. So `step_functions_started` (and the
derived `wrote`) is **always False** on a live run — the dispatch can never be
proven.

Ground truth (`evaluator_entry.py`):
- `StepFunctionsStartExecution.__call__` starts the workflow with
  `name=name` where the handler passes `name=operation_id[:80]`
  (`handler.py:188`) and `input=json.dumps({"operation_id": operation_id})`.
- The state machine name is `${ProjectName}-operations-execution`
  (`07-operations-execution.yaml:824`,
  `GBAW_OPERATIONS_STATE_MACHINE_ARN` line 699:
  `arn:aws:states:${Region}:${Account}:stateMachine:${ProjectName}-operations-execution`).

So the real execution ARN is:

```
arn:aws:states:<region>:<account>:execution:<ProjectName>-operations-execution:<operation_id[:80]>
```

Note the **80-char name truncation** — an operation id longer than 80 chars
means the execution name is a prefix, which the adapter never accounts for.

**DynamoDB key.** `CommandTransport._audit_key(operation_id)` (lines ~703–706)
returns `{"pk": {"S": operation_id}}` and `get_audit_reservation_item` issues:

```
aws dynamodb get-item --table-name <t> --key '{"pk":{"S":"<op_id>"}}' --consistent-read
```

Ground truth for the 06 table (`06-operations-observation.yaml:526–537`):
`TableName ${ProjectName}-operations`, **composite key `PK` (HASH) + `SK`
(RANGE)**, both uppercase. Ground truth for how items are keyed
(`autonomy_runtime/store.py`):
- reservation item: `PK = "AUTZRSV#" + operation_id`, `SK = "AUTZRSV"`
  (`_reservation_pk` line 1062, `_RESERVATION_SK = "AUTZRSV"` line 517).
- state item: `PK = "AUTZ#" + state_id`, `SK = "AUTZWINDOW"`.
- audit item(s): `PK = "AUTZAUDIT#" + operation_id`, `SK = "<event>#<generation>"`.
- dispatch bundle: `PK = "AUTZBUNDLE#" + operation_id`, `SK = "AUTZBUNDLE"`;
  dispatch audit `SK = "AUTZDISPATCH#<phase>"` (`_audit_sk`).

The adapter's key is wrong on **three** counts: lowercase `pk` (must be `PK`),
bare `operation_id` (must be the `AUTZRSV#`/`AUTZAUDIT#`/`AUTZBUNDLE#` prefixed
value), and it **omits the required `SK` range key**. `GetItem` against a
composite-key table without the sort key is a hard `ValidationException` — the
call errors before any item is read, so `audit_recorded`, `reservation_granted`,
and `artifact_matches_expected` are all silently `False`.

**Synthetic attribute fields.** `_dispatch_evidence` (lines ~600–615) infers:
- `audit_recorded = "audit" in record`
- `reservation_granted = "reservation" in record`
- `artifact_matches_expected = "prepared_hash" in record or audit_recorded`

None of `audit`, `reservation`, or `prepared_hash` are attribute names the store
writes. The store distinguishes records by their `PK`/`SK` **key**, not by a
literal top-level attribute. These presence checks are fabricated proxies that
would be `False` even against a correctly-keyed live item, and — worse — could
be spoofed `True` by any item that happened to carry those attribute names.

### Exact expected wire shapes / keys

Execution existence proof (two acceptable shapes):
- Construct the ARN from a configured `state_machine_arn`
  (`arn:...:stateMachine:<name>`), swap `stateMachine`→`execution`, append
  `:<operation_id[:80]>`, then `describe-execution --execution-arn <that>`; OR
- `aws stepfunctions list-executions --state-machine-arn <sm-arn>
  --name-filter <operation_id[:80]>` and assert exactly one match.

Reservation read (composite key, prefixed, both keys):
```
aws dynamodb get-item --table-name <ProjectName>-operations \
  --key '{"PK":{"S":"AUTZRSV#<op_id>"},"SK":{"S":"AUTZRSV"}}' --consistent-read
```
Presence of the item under this exact key is the reservation proof. Audit proof
is a separate `GetItem`/`Query` on `PK="AUTZAUDIT#<op_id>"` (or the dispatch
bundle `PK="AUTZBUNDLE#<op_id>", SK="AUTZBUNDLE"` and dispatch audit
`SK="AUTZDISPATCH#dispatched"`). Assertions must key off item existence under the
real key, not off invented attribute names.

### Tests that distinguish real parsing from fabricated success

1. **ARN construction:** fake runner asserts the argv passed to
   `describe-execution --execution-arn` matches
   `^arn:aws:states:[^:]+:\d{12}:execution:.+-operations-execution:.{1,80}$`.
   The status-quo (raw operation id) fails the regex.
2. **80-char truncation:** operation id of 120 chars → assert the execution name
   segment is exactly the first 80 chars.
3. **Composite-key GetItem:** fake DynamoDB runner rejects any `--key` lacking
   both `PK` and `SK` (mirrors a real `ValidationException`). Assert
   `reservation_granted` derives from an item stored under
   `PK="AUTZRSV#<op>", SK="AUTZRSV"`. The status-quo `{"pk":{"S":op}}` triggers
   the simulated validation error → `False`, exposing the bug.
4. **No synthetic attributes:** store a correctly-keyed reservation item whose
   attributes do NOT include a literal `reservation`/`audit`/`prepared_hash`
   key. Assert the adapter still reports the reservation granted (proving it
   keys off item existence, not attribute names). Conversely, an item under the
   wrong key that *does* carry `{"audit":...}` must NOT be counted.

---

## Blocker 3 — nonexistent preflight env-var keys; wrong E4/E5 AppConfig document schema; deprecated read API

### Confirmed behavior at `09bbc06`

**Static-mode env var.** `observed_static_mode_operate` (lines ~360–372) reads
`get-function-configuration` and inspects
`GBAW_OPERATIONS_STATIC_DEPLOYMENT_MODE` or `GBAW_OPERATIONS_AUTONOMY_STATIC_MODE`.
Ground truth: the only backend-enforced ceiling is **`GBAW_OPERATIONS_MODE`**
(evaluator `evaluator_entry.py:211–213`; settings module
`GBAW_OPERATIONS_MODE`/`OPERATIONS_MODES`; set in
`07-operations-execution.yaml` as the executor/dispatch env). Neither variable
the adapter reads is ever set by 06/07/08, so `observed_static_mode_operate()`
returns `False` unconditionally on a live function — a fail-closed that can
never open, i.e. it does not actually measure the deployed mode.

**AppConfig document schema.** `_read_appconfig_document` (lines ~270–320)
computes `fresh = not bool(document.get("expired", True))` and
`enabled = bool(document.get("enabled"))`. Ground truth for the E4 kill-switch
document schema (`08-operations-control-plane.yaml:489–556`): the closed object
enumerates exactly `version`, `issued_at`, **`not_after`** (freshness horizon;
"stale once now >= not_after"), **`operations_enabled`**, and the per-capability
phase booleans. There is **no `expired` and no `enabled` key**. Because
`document.get("expired", True)` defaults to `True`, `fresh` is always `False` →
`observed_e4_kill_switch_fresh()` and `observed_autonomy_switch_fresh_enabled()`
**always fail closed** regardless of the live document. The adapter never
evaluates the real `not_after` horizon or `operations_enabled`.

**Deprecated read API.** The adapter uses `aws appconfig get-configuration`
(lines ~290–300). That is the deprecated single-call API. The current API is
`appconfig start-configuration-session` → `get-latest-configuration` (returns
the body to `--output-file` and a `NextPollConfigurationToken`). Moreover, the
deployment provisions the **official AppConfig Agent Lambda extension**
(`08-...:AppConfigExtensionLayerArn`, `AppConfigExtensionPort`,
`GBAW_OPERATIONS_APPCONFIG_EXTENSION_PORT`): the runtime reads the kill switch
over `http://localhost:<port>/applications/.../configurations/...`, not via a
CLI `get-configuration`.

### Exact expected wire shape / keys

Freshness/enabled evaluation against the real schema (whichever read path):
```
now = <utc now, normalized Z>
stale   = now >= document["not_after"]           # NOT document["expired"]
enabled = bool(document["operations_enabled"])   # NOT document["enabled"]
```
For the current CLI path:
```
SID=$(aws appconfig start-configuration-session --application-identifier <app> \
  --environment-identifier <env> --configuration-profile-identifier <prof> \
  --query InitialConfigurationToken --output text)
aws appconfig get-latest-configuration --configuration-token "$SID" <out.json>
```
Static mode read must inspect `GBAW_OPERATIONS_MODE == "operate"`.

### Tests that distinguish real parsing from fabricated success

1. **Real schema fresh+enabled:** OutputFile body
   `{"version":3,"issued_at":"...Z","not_after":"<future>Z","operations_enabled":true, ...}`.
   Assert `observed_autonomy_switch_fresh_enabled()` is `True`. The status-quo
   (`expired`/`enabled` lookups) returns `False` → test fails, exposing the bug.
2. **Stale by `not_after`:** same body with `not_after` in the past → must be
   `False`. Confirms the horizon is actually evaluated, not a missing-key
   default.
3. **Static mode env truth:** function config Variables
   `{"GBAW_OPERATIONS_MODE":"operate"}` → `observed_static_mode_operate()` must
   be `True`; `{"GBAW_OPERATIONS_STATIC_DEPLOYMENT_MODE":"operate"}` alone must
   be `False` (proves the real key is read).

---

## Blocker 4 — location / drift / enrollment reads are last-cached, not fresh/current

### Confirmed behavior at `09bbc06`

- `observed_no_stack_drift` (lines ~390–408) reads
  `cloudformation describe-stacks` → `Stacks[0].DriftInformation.StackDriftStatus
  == "IN_SYNC"`. `StackDriftStatus` on `describe-stacks` is the **last-detected**
  summary — it can be `IN_SYNC` from a detection run performed days ago, or
  `NOT_CHECKED` on a stack that has drifted since. It does not prove the stack is
  currently in sync.
- `observed_fleet_enrolled_active` (lines ~340–352) reads
  `gamelift describe-fleet-attributes` → `FleetAttributes[0].Status == "ACTIVE"`.
  `ACTIVE` is enrollment liveness, not proof the fleet's **location** matches the
  reviewed window or that its capacity is the current observed one.
- The adapter carries no notion of the enrolled fleet **location**; the executor
  env (`07-...:GBAW_OPERATIONS_ENROLLED_FLEET_ARN`) and the evaluator's
  `enrolled fleet id/ARN/location` inputs (`evaluator_entry.py:105`) show
  location is part of the trusted coordinate set the preflight should compare.

### Exact expected wire shape

Freshness must be forced, not read from cache:
```
aws cloudformation detect-stack-drift --stack-name <name>   # -> StackDriftDetectionId
aws cloudformation describe-stack-drift-detection-status \
  --stack-drift-detection-id <id>   # poll until DetectionStatus==DETECTION_COMPLETE
# then require StackDriftStatus==IN_SYNC from THIS detection
```
Enrollment currency should assert `Status==ACTIVE` **and** the fleet's location
matches the configured location, e.g. via
`gamelift describe-fleet-location-attributes --fleet-id <id> --locations <loc>`
returning `ACTIVE`, cross-checked against the reviewed `EnrolledFleetArn` region.

### Tests that distinguish fresh from cached

1. **Drift freshness:** fake runner where `describe-stacks` returns
   `IN_SYNC` but no fresh detection was requested → `observed_no_stack_drift()`
   must be `False` until a `detect-stack-drift` + completed
   `describe-stack-drift-detection-status` returns `IN_SYNC`. The status-quo
   passes on the cached value — the test exposes it.
2. **Location currency:** fleet `ACTIVE` but its location differs from the
   configured location → enrollment proof must be `False`.

---

## Blocker 5 — cleanup false-confirms an unreadable switch; force-write false-confirms an evidence failure

### Confirmed behavior at `09bbc06`

- `CommandTransport._disable` GET branch (lines ~648–655) returns
  `{"autonomy_enabled": bool(still_enabled)}` where `still_enabled =
  observed_autonomy_switch_fresh_enabled()`. Per Blocker 3, that method returns
  `False` for a **stale, absent, OR unreadable** switch as well as for a
  genuinely disabled one. So a switch the adapter simply **could not read**
  reports `autonomy_enabled: false` — the guaranteed teardown "confirms" a
  disable it never actually observed. An unreadable state must be
  *indeterminate* (retry/fail), not "confirmed disabled."
- `_force_write` (lines ~658–685): `result = invoke_evaluate(...)`. If the
  underlying invoke **errors** (which it does per Blocker 1's concatenation bug,
  raising `CommandError`), the exception propagates out of `_force_write` — but
  where it is caught upstream, `isinstance(result, dict)` is `False`, `outcome`
  is `None`, and the method returns `409 refused, wrote:false`. That is a
  *fabricated* "refused with no write": an evidence-collection failure is
  reported as a proven negative. The negative ("no write happened") must be
  proven by a real read (no new execution / no new audit item), not inferred
  from an evaluate call that failed to return.

### Exact expected behavior

- Disable-confirm GET must distinguish three states: **enabled** (fail),
  **provably disabled** (a fresh, readable, `operations_enabled:false`/expired
  document), and **unreadable/ambiguous** (neither confirm nor deny — the
  teardown must retry within its wait budget, then fail closed loudly).
- Force-write refusal must prove the negative: after the evaluator refuses,
  assert **no new Step Functions execution** exists for the forced attempt's
  operation id (via the corrected ARN/list path) and **no new dispatched audit
  item** — not merely that the evaluate call did not return a dispatched dict.

### Tests

1. **Unreadable ≠ disabled:** fake `get-configuration`/`get-latest-configuration`
   returns rc≠0 (unreadable). Assert the disable-confirm GET does NOT report
   `autonomy_enabled:false` as confirmed — it must signal indeterminate. The
   status-quo reports a false confirm.
2. **Evidence-failure ≠ proven negative:** fake `invoke_evaluate` raises
   `CommandError`. Assert `_force_write` does NOT return a clean
   `refused/wrote:false` confirmation; it must surface the read failure. A build
   that swallows it into `409 refused` fails this test.
3. **Real proven negative:** evaluator refuses AND `list-executions`/GetItem show
   no new execution/audit → `wrote:false` is legitimate; assert it is derived
   from those reads.

---

## Blocker 6 — activation omits the exact 07↔06 table / KMS / tenant / workspace comparison

### Confirmed behavior at `09bbc06`

The measured preflight reads a bare `operations_table_name` from
`CommandAdapterConfig` and reads alarms, fleet, switch, kill switch, static mode,
and drift — but it **never verifies that the 07 execution stack addresses the
same table, KMS key, tenant, and workspace as the 06 observation stack**. Ground
truth that this cross-stack identity is a hard requirement:

- `07-operations-execution.yaml`:
  - `OperationsTableName` param (line 81): "E3 does not create a table … the 06
    stack exports it as its `OperationsTableName` output" — 07 must consume the
    06 export.
  - `TenantId` (line 122): "**Must match the 06 stack's TenantId** so the
    dispatcher authorizes the same workspace scope."
  - `WorkspaceId` (line 132): "**Must match the 06 stack's WorkspaceId**."
  - Executor/dispatch env (lines 688–691, 1001–1004):
    `GBAW_OPERATIONS_TABLE_NAME`, `GBAW_OPERATIONS_TENANT_ID`,
    `GBAW_OPERATIONS_WORKSPACE_ID`.
  - KMS: executor/dispatch IAM is scoped to `OperationsKmsKeyArn` with
    `kms:ViaService dynamodb.<region>.amazonaws.com` (lines 445/455/606/615/935).
- `06-operations-observation.yaml`: creates `OperationsKmsKey` (line 448),
  encrypts the table with it (`KMSMasterKeyId: !Ref OperationsKmsKey` line 546),
  and **exports** `OperationsTableName` (lines 1172–1177). `TenantId`/`WorkspaceId`
  are the observe function's env (lines 815–816).
- `08-operations-control-plane.yaml` `TenantId`/`WorkspaceId` (lines 101/111)
  repeat "Must match the 06 stack's".

Because the adapter never compares these, an "activation" preflight can pass
against a 07 execution stack pointed at a **different** table, KMS key, tenant,
or workspace than the 06 observation the evaluator trusts — exactly the mismatch
the shakedown exists to catch.

### Exact expected wire shape

Resolve and cross-check the four coordinates from live provider reads, not from
adapter config alone:
```
# 06 exports (source of truth)
aws cloudformation describe-stacks --stack-name <06-stack> \
  --query "Stacks[0].Outputs[?ExportName=='<ProjectName>-OperationsTableName'].OutputValue"
# 07 executor function env (what the writer actually uses)
aws lambda get-function-configuration --function-name <ProjectName>-operations-executor \
  --query "Environment.Variables.{t:GBAW_OPERATIONS_TABLE_NAME,\
tenant:GBAW_OPERATIONS_TENANT_ID,ws:GBAW_OPERATIONS_WORKSPACE_ID}"
# table's CMK
aws dynamodb describe-table --table-name <table> \
  --query "Table.SSEDescription.KMSMasterKeyArn"
```
Require: 07 executor `GBAW_OPERATIONS_TABLE_NAME` == 06 exported table name; both
functions' `GBAW_OPERATIONS_TENANT_ID`/`GBAW_OPERATIONS_WORKSPACE_ID` equal the
06 observe function's; the table's `KMSMasterKeyArn` equals the 06
`OperationsKmsKey` ARN. Any mismatch fails the activation closed.

### Tests

1. **Table mismatch:** fake reads where 07 executor `GBAW_OPERATIONS_TABLE_NAME`
   differs from the 06-exported table → activation must fail. Status quo (no
   comparison) passes — the test exposes the gap.
2. **Tenant/workspace mismatch:** 07 tenant/workspace differ from 06 → fail.
3. **KMS mismatch:** table CMK differs from the 06 key ARN → fail.
4. **All equal:** identical coordinates → activation proceeds. Confirms the
   comparison is real, not vacuous.

---

## Summary of ground-truth anchors

| # | Blocker | Real shape (source) |
|---|---------|---------------------|
| 1 | Lambda invoke concat / observe ABI / fabricated `trusted` | payload → separate OutputFile; metadata `StatusCode`/`FunctionError` on stdout; evaluator return `{"outcome":"dispatched","operation_id":...}` (`evaluator_entry.py:743`) |
| 2 | op-id-as-ARN / lowercase incomplete key / synthetic fields | ARN `...:execution:${ProjectName}-operations-execution:<op[:80]>` (`07:824,699`, `handler.py:188`); key `PK="AUTZRSV#<op>",SK="AUTZRSV"` (`store.py:517,1062`; `06:534-537`) |
| 3 | nonexistent preflight keys / wrong docs | `GBAW_OPERATIONS_MODE` (`evaluator_entry.py:211`); doc keys `not_after`/`operations_enabled` (`08:489-556`); `get-latest-configuration` / AppConfig extension |
| 4 | drift/enrollment not fresh | `detect-stack-drift` + completed detection status; `describe-fleet-location-attributes` |
| 5 | cleanup / force-write false-confirm | indeterminate vs disabled; prove no-write by `list-executions`/GetItem |
| 6 | activation omits 07↔06 identity | 06 `OperationsTableName` export, `OperationsKmsKey`, tenant/workspace (`06:546,815-816,1172`; `07:81,122,132`) |

All findings are read-only observations at `09bbc06`; no source was modified.
