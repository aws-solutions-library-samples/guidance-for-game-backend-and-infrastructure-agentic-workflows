# Issue #440 — Five final semantic blockers in the E5 command adapter (read-only review at `7794bcd`)

**Base commit:** `7794bcd` (`fix(operations): Close six E5 live adapter/binding blockers from #440 review`)
**Artifacts under review:**
- `backend/src/operations/validation/e5_command_adapter.py`
- `backend/src/operations/validation/e5_shakedown.py`

**Mode:** read-only. No file was edited and no AWS call was made. Every assertion
below is grounded in source at `7794bcd`, in the deployed CloudFormation
(`infrastructure/cloudformation/06/07/08`), and — for the GameLift wire shape —
in the public GameLift API reference. Line numbers are as materialized from
`7794bcd`.

## What this review is (and is not)

The prior `09bbc06` review
(`docs/reviews/440-e5-live-adapter-binding-09bbc06.md`) found six live
adapter/binding blockers. `7794bcd` is the fix commit for those six, and it does
close the gross wire-shape errors: `aws lambda invoke` now writes the response
payload to a private tempfile `OutputFile` and fails closed on `FunctionError`
(`_invoke_lambda`, lines 276–330); the Step Functions execution is described by
an ARN built from the deterministic execution name (`execution_arn_for`, lines
338–358); the DynamoDB reads use the real uppercase `PK`/`SK` with the
`AUTZBUNDLE#`/`AUTZRSV#` prefixes (`_get_item`, lines 370–380); drift is a fresh
polled `detect-stack-drift` (lines 652–705); and the `_disable` GET read is now
tri-state (lines 1015–1036). Those are correct and must be preserved.

This review covers the **five semantic blockers that survive `7794bcd`** — cases
where the command still cannot succeed against the real deployed contract, or
where the adapter reports a trusted/negative result that the evidence does not
support. Each is grounded in the exact source that defines the real shape,
followed by the exact test that distinguishes a real read from a fabricated one.

**Static infra and correct behavior to preserve.** Do not disturb, when fixing:

- The tempfile-`OutputFile` invoke + `FunctionError` fail-closed
  (`_invoke_lambda`).
- The deterministic execution-name ARN (`execution_arn_for`,
  `_EXECUTION_NAME_MAX = 80`) and the real `PK`/`SK` DynamoDB keys.
- The fresh polled `detect-stack-drift` (`observed_no_stack_drift`) and the
  `_disable` GET tri-state (`observed_autonomy_switch_state`).
- The closed evaluator event `{observation_operation_id, desired, minimum,
  maximum}` and the `0/0/1` window; `ImaginaryRouteError`; the unauthenticated
  `401` short-circuit; `shell=False` argv.
- All static-infra parse tests for `06/07/08/09`.

---

## Blocker 1 — The direct observe invoke can never succeed: empty JWT claims (401) and a body missing the required `idempotency_token` (400)

### Confirmed behavior at `7794bcd`

`CommandAdapter._observe_proxy_event` (lines 233–255) builds the event the
observe path sends to the real 06 Lambda:

```python
body = json.dumps({"fleet_id": self._config.enrolled_fleet_id})
return {
    "httpMethod": "POST",
    "routeKey": "POST /operations/observe",
    ...
    "body": body,
    "requestContext": {
        "http": {"method": "POST", "path": "/operations/observe"},
        "authorizer": {"jwt": {"claims": {}}},   # <-- EMPTY claims
    },
}
```

`observe_succeeded_observation_id` (lines 206–231) invokes this via
`aws lambda invoke` and only returns an id when the parsed proxy response is
`statusCode == 200` with a `body.observation_id`. The deployed 06 handler
(`backend/src/operations/observation_handler.py`) rejects this event **twice**,
so a `200` is unreachable:

1. **Identity (401).** `_dispatch` runs `_verified_principal` *first* (line 120).
   `_verified_principal` (lines 168–205) reads
   `requestContext.authorizer.jwt.claims` and requires `sub` (str),
   `client_id` (str), `token_use == "access"`, and a parseable `exp`. With
   `claims == {}` all four are missing →
   `IDENTITY_CONTEXT_INVALID` → HTTP **401** (`_STATUS_BY_CODE`, line 57). The
   handler *only* trusts claims that **API Gateway's JWT authorizer** populates;
   a direct `lambda:Invoke` bypasses API Gateway, so a synthetic `claims: {}`
   (or any self-asserted claims) can never be a verified caller context. This is
   by design (`observation_handler.py` module docstring, lines 4–8: "a direct or
   unattributed invocation ... is rejected before any").

2. **Contract (400), even with identity.** `_parse_observe_request` (lines
   143–157) feeds the body to `ObservationRequest.from_payload`
   (`observation.py:184–187`), which requires
   `set(payload) == {"fleet_id", "idempotency_token"}` and an
   `idempotency_token` matching `^idem_[A-Za-z0-9_-]{20,128}$`
   (`_IDEMPOTENCY_PATTERN`, `observation.py:92`). The adapter's body is
   `{"fleet_id": ...}` — it **omits `idempotency_token`** → set-inequality →
   `CONTRACT_INVALID` → HTTP **400**.

**Consequence.** `observe_succeeded_observation_id` always returns `None` on a
live call, so `CommandTransport._observe` (lines 863–869) always returns
`{"trusted": False, "error_code": "OBSERVATION_NOT_SUCCEEDED"}`. The
"trusted E1 observation" shakedown step
(`e5_shakedown.py::check_trusted_e1_observation`) can never pass against the real
Lambda — the observe seam is dead. (The fix from the prior review — "never
fabricate `trusted`" — is intact; the residual defect is that the *only* other
outcome, a real succeeded observation, is now unreachable.)

### Exact expected shape

An observe that can succeed must (a) carry a **verified** authorizer context and
(b) send a schema-valid body:

- Identity cannot be forged in a direct `lambda:Invoke`. The observe must go
  through the **authenticated HTTP API** the 06 stack exports
  (`OperationsHttpApiEndpoint`, `06:Outputs.HttpApiEndpoint`) with a real
  Cognito access token, so API Gateway's JWT authorizer populates
  `requestContext.authorizer.jwt.claims`; OR the shakedown must obtain a real
  authorizer context out-of-band and stop pretending a direct invoke is
  attributed. A synthetic `claims: {}` must be treated as *unauthenticated*, not
  as a trusted observe.
- The body must be exactly `{"fleet_id": <id>, "idempotency_token": "idem_<20-128
  url-safe chars>"}`, with a freshly generated token per new observation
  (`observation.py` module docstring, lines 11–35: "a new observation requires a
  new idempotency token").

### Tests that distinguish a real observe from a dead one

1. **Empty-claims 401 regression:** drive `observe_succeeded_observation_id`
   against a fake 06 handler wired to the *real* `ObservationHandler` (or a fake
   that mirrors `_verified_principal`). Assert the event the adapter sends is
   rejected `401` and the transport reports `trusted: False`. A build that sends
   `claims: {}` and expects success fails — exposing that the direct invoke is
   never a verified caller.
2. **Missing-idempotency 400:** with a *valid* verified principal injected,
   assert `from_payload` rejects the adapter's `{"fleet_id": ...}` body with
   `CONTRACT_INVALID`. Assert the corrected adapter includes a token matching
   `^idem_[A-Za-z0-9_-]{20,128}$` and that the body key-set is exactly
   `{"fleet_id","idempotency_token"}`.
3. **Success path:** only when the event carries a verified principal AND a
   valid `idempotency_token` does the handler return `200` with an
   `observation` body, and only then does the transport report
   `{"trusted": True, "observation_id": ...}`.

---

## Blocker 2 — `describe-fleet-capacity --locations` is not a real API; the location "proof" reads the home Region and parses the wrong response shape

### Confirmed behavior at `7794bcd`

`CommandAdapter.describe_fleet_capacity` (lines 391–399):

```python
argv = self._base("gamelift", "describe-fleet-capacity")
argv += ["--fleet-id", self._config.enrolled_fleet_id]
location = self._config.enrolled_location or self._config.region
if location:
    argv += ["--locations", location]
return self._run_json(argv)
```

`observed_fleet_capacity` (lines 420–456) then treats the result as
`data["FleetCapacity"]` = **a list**, iterates entries, and selects the one
whose `entry["Location"] == location`.

Three facts from the GameLift API reference contradict this:

- **`DescribeFleetCapacity` has no location parameter.** Its request accepts
  only `FleetIds`, `Limit`, `NextToken`
  (`API_DescribeFleetCapacity.html`). `--fleet-id` (singular) and `--locations`
  are **not** parameters of this operation; the real CLI takes `--fleet-ids`
  (plural). `aws gamelift describe-fleet-capacity --fleet-id X --locations Y`
  fails argument validation (rc≠0) → `_run_json` raises `CommandError` →
  `observed_fleet_capacity` returns `None` on every live call → `_capacity`
  emits `{"capacity": {}}` and the harness capacity checks fail closed. The
  capacity read is dead.
- **Even with the right flag, this API returns the home Region only.** "With
  multi-location fleets, this operation retrieves data for the fleet's **home
  Region only** ... each `FleetCapacity` object includes a `Location` property,
  which is set to the fleet's **home Region**." So matching `Location ==
  enrolled_location` for a *remote* enrolled location can never match — the
  entry for a remote location is simply not returned by this API.
- **The remote-location capacity API is a different call with a different
  response shape.** `DescribeFleetLocationCapacity`
  (`API_DescribeFleetLocationCapacity.html`) takes `FleetId` + `Location`
  (**singular**) and returns a **single** `FleetCapacity` **object** (not a
  list). The adapter's list-iteration + `FleetCapacity[]` parse is the wrong
  shape for the only API that can answer "capacity at the enrolled location."

### Exact expected shape

Read the enrolled location's capacity with the operation that supports a
location, and parse its single-object response:

```
aws gamelift describe-fleet-location-capacity \
  --fleet-id <enrolled_fleet_id> --location <enrolled_location>
# -> {"FleetCapacity": {"InstanceCounts": {"DESIRED":n,"MINIMUM":n,"MAXIMUM":n}, "Location": "<loc>"}}
```

(If, and only if, the enrolled location is the fleet's home Region may
`describe-fleet-capacity --fleet-ids <id>` be used, selecting the single
returned `FleetCapacity[]` entry.) `InstanceCounts.DESIRED` absence must still
fail closed to `None`.

### Tests that distinguish a real capacity read from a dead one

1. **CLI-shape regression:** a fake runner that mirrors the real CLI — rejecting
   `describe-fleet-capacity` when it sees `--locations` (unknown option) — makes
   the status-quo `observed_fleet_capacity` raise/return `None`. Assert the
   corrected adapter issues `describe-fleet-location-capacity --fleet-id ...
   --location ...` (singular) and the fake asserts exactly those args.
2. **Single-object parse:** the fake returns the `DescribeFleetLocationCapacity`
   shape (`"FleetCapacity"` = a dict, not a list). Assert
   `observed_fleet_capacity` returns `{"desired","minimum","maximum"}`. The
   status-quo list-iteration returns `None` on a dict → fails, exposing the
   shape bug.
3. **Remote-location home-Region trap:** the fake `describe-fleet-capacity`
   returns a single home-Region entry whose `Location` != the enrolled remote
   location. Assert the corrected adapter does NOT accept it as the enrolled
   location's capacity (it must call the location API instead).

---

## Blocker 3 — E4/E5 preflight reads freshness only; it ignores the master `operations_enabled` / `autonomy_enabled` flags, the per-phase switches, and the per-capability `autonomous_write` flag

### Confirmed behavior at `7794bcd`

**E4 kill switch.** `observed_e4_kill_switch_fresh` (lines 585–608) reads the 08
kill-switch document and returns `self._document_is_fresh(doc)` — i.e. **only**
`issued_at <= now < not_after` (`_document_is_fresh`, lines 553–567). It reads
none of the fields the runtime gate requires. The deployed gate
(`backend/src/operations/control/kill_switch_gate.py`) computes a decision from
`operations_enabled` and a per-phase switch map:

```python
# KillSwitchGate.evaluate, lines 157-166
document = self.read_fresh_document()
switches = document["capabilities"][self._capability_id]
return KillSwitchDecision(
    operations_enabled=bool(document["operations_enabled"]),
    ...
    _phase_document_flags={phase: bool(switches[phase]) for phase in CONTROL_PHASES},
    ...
)
# phase_allowed, lines 123-132: requires operations_enabled AND the per-phase
# switch AND the static-authority floor.
```

`CONTROL_PHASES = ("prepare", "dispatch", "execute")`
(`contracts/control_plane.py:64`). So a document can be perfectly *fresh* yet
have `operations_enabled == false` or `capabilities[CAPABILITY_ID].execute ==
false`, and the runtime would deny every phase — while the preflight reports the
switch "safe/live." The preflight is asserting a property (operations are
enabled for this phase) it never read.

**E5 autonomy switch.** `observed_autonomy_switch_state` (lines 474–495) returns
`doc.get("autonomy_enabled")` if fresh, else `None`. It **ignores the
per-capability flag**. The deployed autonomy gate
(`backend/src/operations/autonomy_switch.py`) requires **both**:

```python
# AutonomySwitchDecision.autonomy_allowed, lines 165-167
if not self.autonomy_enabled:
    return False
return bool(self._capability_flags.get(self._capability_id, False))
# where the document carries capabilities[cap] = {"autonomous_write": bool}
# (_REQUIRED_CAPABILITY_KEYS = {"autonomous_write"}, line 73)
```

So a fresh document with `autonomy_enabled == true` but
`capabilities[cap].autonomous_write == false` makes the preflight report
`enabled == True` while the runtime refuses the write. `observed_autonomy_switch_state`
also never validates `autonomy_switch_version` (`"1.0"`, exact) or
`config_version`, both of which `validate_autonomy_switch_document` (lines
95–143) requires — a version-mismatched document the runtime rejects would still
read "fresh + enabled" in preflight.

### Exact expected shape

The preflight must mirror the runtime gates, not a subset:

- **E5 autonomy:** parse with the real contract — reject unless
  `autonomy_switch_version == "1.0"`, a valid `config_version`, fresh
  `issued_at`/`not_after`, `autonomy_enabled is True`, **and**
  `capabilities[AUTONOMY_SWITCH_CAPABILITY_ID]["autonomous_write"] is True`.
  Report "enabled" only when `autonomy_allowed` would be true for the gated
  capability.
- **E4 kill switch:** require fresh **and** `operations_enabled is True`
  **and** the specific lifecycle phase(s) the shakedown drives enabled in
  `capabilities[CAPABILITY_ID]` (`prepare`/`dispatch`/`execute`), matching
  `phase_allowed`. A fresh-but-disabled document must read as "not live."

### Tests that distinguish a real gate read from a freshness-only read

1. **Autonomy capability flag:** feed a fresh document with
   `autonomy_enabled: true` but `capabilities[cap].autonomous_write: false`.
   Assert the corrected `observed_autonomy_switch_state`/`..._fresh_enabled`
   returns not-enabled. The status-quo returns `True` — failing, exposing the
   ignored per-capability flag.
2. **Autonomy version guard:** fresh document with
   `autonomy_switch_version: "2.0"`. Assert preflight treats it as unavailable
   (mirroring `validate_autonomy_switch_document`). Status-quo accepts it.
3. **Kill-switch master flag:** fresh 08 document with
   `operations_enabled: false` (all phase switches false). Assert
   `observed_e4_kill_switch_fresh`-equivalent reports not-live. The status-quo
   returns `True` on freshness alone — failing, exposing the ignored master flag.
4. **Kill-switch per-phase:** fresh document, `operations_enabled: true`, but
   `capabilities[CAPABILITY_ID].execute: false`. Assert the phase the shakedown
   drives is reported not-permitted.

---

## Blocker 4 — Force-write false-confirms `wrote: false` when the evaluate observation is unavailable (the `EVIDENCE_UNAVAILABLE` guard only covers the dispatched branch)

### Confirmed behavior at `7794bcd`

`CommandTransport._force_write` (lines 1039–1078) added a real guard for one
case: if the evaluator **dispatched** but the Step Functions execution read is
UNREADABLE, it returns `wrote: null` + `EVIDENCE_UNAVAILABLE` instead of a clean
negative (lines 1064–1072). But the guard does **not** cover the case where the
`invoke_evaluate` call itself fails:

```python
try:
    result = self._adapter.invoke_evaluate(event)
except CommandError:
    result = None                     # <-- evaluator UNREADABLE
outcome = result.get("outcome") if isinstance(result, dict) else None
...
wrote: Optional[bool] = False         # <-- stays False
evidence_unavailable = False
if outcome == _OUTCOME_DISPATCHED:
    ...                               # only this branch can set wrote=None
# else: outcome is None (CommandError) OR a refusal -> wrote stays False
body = {..., "wrote": wrote}          # wrote == False
return _http(409, body)
```

When `invoke_evaluate` raises (the evaluator Lambda is unreadable / the invoke
errors), `outcome` is `None`, the dispatched branch is skipped, and the response
is a clean `409` with `wrote: False` and `error_code` derived from an **empty**
reason (`"AUTONOMY_DISABLED"`). The shakedown accepts that as a pass:

```python
# e5_shakedown.py::check_forced_write_denied, line 473
passed = response.status >= 400 and body.get("wrote") is False
# and check_iam_negative, line 497
passed = response.status in (403, 409) and body.get("wrote") is False
```

**Consequence.** An unreadable evaluator invocation — where the adapter has
*no evidence* about whether a write happened — is reported as a confirmed
`wrote: False` and both `forced_evaluator_executor_cannot_write` and the IAM
negative check pass. This is exactly the "false-confirm on unavailable evidence"
failure the dispatched-branch guard was meant to prevent, leaking through the
evaluate-error path. (The refusal-with-a-real-reason path is fine: a parsed
refusal *is* evidence the evaluator declined. The defect is specifically the
`result is None` / `CommandError` path.)

### Exact expected shape

Treat an evaluate that could not be observed the same way the dispatched branch
treats an unreadable execution: it is UNKNOWN, not a clean negative.

- On `invoke_evaluate` `CommandError` (or a non-dict result), set
  `wrote = None`, `error_code = "EVIDENCE_UNAVAILABLE"`, and do NOT present
  `wrote: false`.
- The shakedown's `check_forced_write_denied` / `check_iam_negative` must require
  `body.get("wrote") is False` **explicitly** (already do) AND that the code is
  not `EVIDENCE_UNAVAILABLE` — an UNKNOWN must fail the "confirmed no write"
  check, not pass it.

### Tests that distinguish a proven negative from an unavailable one

1. **Evaluate-error UNKNOWN:** fake `invoke_evaluate` raises `CommandError`.
   Assert `_force_write` returns `wrote: null` (not `False`) and
   `error_code == "EVIDENCE_UNAVAILABLE"`. The status-quo returns `wrote: False`
   — failing, exposing the false-confirm.
2. **Shakedown must not pass on UNKNOWN:** feed `check_forced_write_denied` a
   `409` body with `wrote: null`/`EVIDENCE_UNAVAILABLE`. Assert `passed is
   False`. The status-quo `passed = status>=400 and wrote is False` already
   fails on `wrote is None`, but pin it so a future "truthy-deny" refactor can't
   re-admit the UNKNOWN.
3. **Proven refusal still passes:** fake returns `{"outcome":"refused",
   "reason":"AUTONOMY_DISABLED"}`. Assert `wrote: False` and the check passes —
   confirming the fix narrows only the unobserved case.

---

## Blocker 5 — `verify_activation_binding` compares caller-supplied dicts; it never reads the real 06/07 stacks, and 08 is only presence-checked (no identity/source binding to 06)

### Confirmed behavior at `7794bcd`

`verify_activation_binding` (lines 725–751) takes three **pre-built mappings**
and does string equality on identically-named keys:

```python
for field_name in _ACTIVATION_BOUND_FIELDS:   # table_name, kms_key_arn,
    expected = observation_06.get(field_name) #   tenant_id, workspace_id,
    actual = execution_07.get(field_name)     #   workflow_arn, fleet_id,
    if expected is None or actual is None or expected != actual:  # trusted_audience
        mismatches.append(field_name)
kill_switch_app = control_08.get("kill_switch_application_id")
if not isinstance(kill_switch_app, str) or not kill_switch_app.strip():
    mismatches.append("kill_switch_application_id")
```

Two structural defects:

1. **It does not actually compare 07 to 06.** The function never reads a
   deployed value. It has **no caller in `backend/src`** (grep: the only usage is
   the unit test, which builds `execution_07 = dict(observation_06)` — a literal
   copy). Nothing in the adapter extracts `table_name`/`kms_key_arn`/`tenant_id`/
   `workspace_id` from the real 06 stack (its exports) or the real 07 stack (its
   parameters / the executor Lambda's resolved env). The real cross-stack binding
   is concrete and readable:
   - 06 **exports** `${ProjectName}-OperationsTableName` and
     `${ProjectName}-OperationsKmsKeyArn`
     (`06:Outputs.OperationsTableName` line ~24, `OperationsKmsKeyArn` line ~38).
   - 07 consumes them as **parameters** `OperationsTableName` /
     `OperationsKmsKeyArn` and stamps `TenantId`/`WorkspaceId`/`TrustedAudience`
     into the executor env `GBAW_OPERATIONS_TABLE_NAME` (07 line 688),
     `GBAW_OPERATIONS_TENANT_ID` (690), `GBAW_OPERATIONS_WORKSPACE_ID` (691),
     with the same asserts requiring non-empty values (07 lines 365–389).

   The keys the wrapper compares (`table_name`, `workflow_arn`, `fleet_id`) are
   **not** the export/parameter/env names any stack uses, and the wrapper is
   never handed data derived from `describe-stacks` /
   `get-function-configuration`. So it can never detect real cross-stack drift —
   it only re-affirms two dicts the caller already reconciled. A green result
   proves nothing about the deployed 06↔07 binding.

2. **08 is presence-checked, not bound.** The seed's "08 identity/source" is not
   compared to 06. The 08 stack takes the same `OperationsTableName`,
   `OperationsKmsKeyArn`, `TenantId`, `WorkspaceId`, `TrustedAudience` parameters
   that "must match the 06 stack's" (08 lines 83–120) and owns the AppConfig
   application/environment/profile (the kill-switch *source*, 08 lines ~309+).
   `verify_activation_binding` checks only that `control_08.kill_switch_application_id`
   is a non-empty string — it never compares 08's `tenant_id`/`workspace_id`/
   `trusted_audience` (identity) or the AppConfig application/profile the 06
   observation trusts (source) against the 06 values. A present-but-mismatched 08
   binds nothing.

### Exact expected shape

Ground the wrapper in real reads and bind 08's identity/source, not its presence:

- Derive the 06 values from real reads: 06 stack **outputs**
  (`aws cloudformation describe-stacks` → `OperationsTableName`,
  `OperationsKmsKeyArn`) and the 06 handler's configured
  `TenantId`/`WorkspaceId`/`TrustedAudience`.
- Derive the 07 values from the **executor Lambda's resolved env**
  (`get-function-configuration` → `GBAW_OPERATIONS_TABLE_NAME`,
  `GBAW_OPERATIONS_TENANT_ID`, `GBAW_OPERATIONS_WORKSPACE_ID`,
  `GBAW_OPERATIONS_TRUSTED_AUDIENCE`) and/or 07 stack parameters — i.e. the
  values the running executor will actually use — and compare each to 06.
- Bind **08 identity/source to 06**: compare 08's resolved
  `tenant_id`/`workspace_id`/`trusted_audience` to 06's, and confirm the 08
  AppConfig application/environment/profile the runtime kill-switch reads is the
  one 06/07 are configured to trust — not merely that an application id string is
  present.

### Tests that distinguish a real binding from a dict passthrough

1. **Reads real reads, not caller dicts:** exercise the binding through a
   collaborator that returns fake `describe-stacks` / `get-function-configuration`
   payloads keyed by the **real** export/env names
   (`OperationsTableName`, `GBAW_OPERATIONS_TABLE_NAME`, …). Assert a 07 executor
   whose `GBAW_OPERATIONS_TABLE_NAME` differs from 06's `OperationsTableName`
   output is refused. The status-quo (compares hand-built same-key dicts) has no
   way to observe this and cannot fail — exposing the passthrough.
2. **08 identity binding:** an 08 whose `TenantId` differs from 06's must be
   refused even when `kill_switch_application_id` is present. Status-quo passes
   (it only checks presence).
3. **08 source binding:** an 08 whose AppConfig application/profile is not the
   coordinate 06/07 trust must be refused. Status-quo passes on any non-empty
   `kill_switch_application_id`.
4. **Wired into activation:** assert a real caller in `backend/src` invokes the
   binding on the activation path (the function is currently unreferenced), so a
   refusal actually blocks activation rather than being dead code.

---

## Verification performed

- Read at `7794bcd`: `e5_command_adapter.py` (1098 lines), `e5_shakedown.py`,
  `observation_handler.py`, `observation.py`, `autonomy_switch.py`,
  `control/kill_switch_gate.py`, `contracts/control_plane.py`, and CloudFormation
  `06`/`07`/`08`. Line numbers cited are from these blobs.
- GameLift wire shapes confirmed against the public API reference
  (`DescribeFleetCapacity`, `DescribeFleetLocationCapacity`).
- No source file was edited; no AWS API was called. Static infra for
  `06/07/08/09` is untouched.

## Scope note

Blockers 1–3 make three live reads (observe, capacity, E4/E5 preflight) either
dead or under-validated; blockers 4–5 let the harness report a trusted negative
(no-write) or a binding as proven on evidence that was never observed. All five
are semantic — the wire mechanics fixed at `7794bcd` are correct as far as they
go; these are the assertions that outrun their evidence.
