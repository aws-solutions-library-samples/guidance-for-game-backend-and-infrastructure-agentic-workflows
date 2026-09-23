# Operations Contracts

This document defines contract version `1.0` for the optional operations
control plane. The contracts implement the boundaries accepted in
[ADR 0002](adr/0002-use-protocol-neutral-operations-services.md) and
[ADR 0003](adr/0003-isolate-provider-writes.md). They do not enable operations,
deploy an executor, or change the existing chat path.

The normative JSON Schemas are in
`backend/src/operations/contracts/schemas/v1`. Example documents and fixed
test vectors are in `backend/tests/fixtures/operations/v1`.

## Published Contracts

| Contract | Version field | Schema |
|---|---|---|
| Playbook body | `playbook_contract_version` | `playbook.schema.json` |
| Prepare-operation request body | `request_contract_version` | `prepare-operation-request.schema.json` |
| Prepared-operation core | `operation_contract_version` | `prepared-operation.schema.json` |
| Source-control prepared-operation profile | `operation_contract_version` | `source-control-prepared-operation.schema.json` |
| Authorization decision | `authorization_contract_version` | `authorization-decision.schema.json` |
| Approval record | `approval_contract_version` | `approval-record.schema.json` |
| Operation state change | `state_contract_version` | `operation-state-change.schema.json` |
| Append-only ledger event | `ledger_contract_version` | `ledger-event.schema.json` |
| Typed application error | `error_contract_version` | `application-error.schema.json` |

The prepared-operation core owns identity, correlation, policy, risk,
idempotency, playbook, and executor fields. A prepared operation MUST also
validate against its named profile. Contract `1.0` publishes the
`source-control.change-proposal/1.0` profile. Future profiles MUST have their
own strict schema and playbook binding.

All contracts use JSON values and domain identifiers only. Application
services MUST NOT accept CopilotKit, Next.js, MCP, provider SDK, or HTTP
request types.

## Trusted Context

`prepare-operation-request.schema.json` describes untrusted proposal data. It
intentionally has no requester, approver, subject, client, tenant, workspace,
correlation, idempotency, policy, enrollment, risk, executor, or verified base
revision field. `additionalProperties` is false, so attempts to inject these
fields fail validation.

An authenticated adapter supplies request context separately. Trusted
application services select the exact playbook, resolve requester and client
identity, bind the configured tenant and workspace, read the current base
revision, render the diff, calculate risk and effective authority, resolve
policy and enrollment versions, assign correlation and idempotency values, and
then store the immutable prepared operation.

The prepared document stores opaque subject and client identifiers. It never
stores an email address or display name.

## Canonicalization and Hashes

Every canonical contract hash uses the following algorithm:

1. Parse one JSON document as I-JSON. Reject duplicate object names,
   non-finite numbers, and numbers outside the RFC 8785 interoperable domain.
2. Serialize the parsed value with the JSON Canonicalization Scheme in
   [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785).
3. Hash the canonical UTF-8 bytes with SHA-256.
4. Encode the result as `sha256:` followed by 64 lowercase hexadecimal
   characters.

RFC 8785 does not normalize Unicode. Producers MUST preserve the exact string
values that were validated. Hashes MUST be calculated from parsed JSON values,
not from source file whitespace or object member order.

The hashes have distinct inputs:

- **Schema hash:** the complete parsed JSON Schema document.
- **Playbook hash:** the complete playbook body. The body binds every
  transitive schema hash, provider action allowlist, authority requirement,
  retry policy, hard limit, and immutable executor binding.
- **Prepared-operation hash:** the complete prepared-operation document. The
  hash is stored beside the immutable document, not inside it. It therefore
  binds the operation identifier, idempotency token, contract version,
  requester and client, tenant, workspace, correlation, recommendation,
  target, verified base revision, files, diff, enrollment, policy, risk,
  capability, retry policy, and executor binding without a circular hash
  field.
- **Duplicate-content hash:** only the source-control profile, action,
  provider, stable repository identifier, target branch, verified base
  revision, sorted path and content-hash pairs, and diff hash. It is a policy
  input and never grants authority.

Inline file `content_hash` and `diff_hash` values use SHA-256 over the exact
UTF-8 bytes of the content or rendered diff. An immutable content reference
MUST resolve to bytes with the bound hash.

`operations.contracts.canonical_sha256` is the canonical implementation.
`contract-vectors.json` pins the expected playbook, prepared-operation,
duplicate-content, and branch values.

## Workflow Identity and Idempotency

The following values are separate even when stored in one document:

- `operation_id` identifies one workflow.
- The prepared-operation hash binds approval to exact immutable content.
- `idempotency_token` maps retries of one submission to the same operation.
- `operation_contract_version` selects the executor interpretation.
- The duplicate-content hash supports an explicit pre-approval duplicate
  policy.

The same idempotency token and trusted workspace MUST resolve to the same
operation. Reusing it with different content MUST fail with
`IDEMPOTENCY_CONFLICT`.

Two independent submissions with identical duplicate-content hashes remain
different operations with different canonical operation hashes. Policy may
reject a duplicate or link it before approval, but it MUST NOT silently merge
workflows or reuse approval.

For source control, the proposal branch is:

```text
gba-op- + first 20 lowercase hex characters of SHA-256(UTF-8(operation_id))
```

The executor recalculates and verifies this value. The branch contains no
requester, tenant, workspace, repository, or recommendation data.

## Approval Binding

An approval record names both `operation_id` and
`prepared_operation_hash`. Approval authority applies only when:

- the operation identifier matches the stored operation;
- a fresh RFC 8785 hash of the stored document matches the approval hash;
- the decision is valid and unexpired;
- approver identity and workspace came from a trusted boundary; and
- current policy allows that approver and any required separation of duties.

The durable workflow sends only `operation_id` to an authenticated executor.
The executor loads the stored operation, approval, and playbook and verifies
all bindings before the first provider write. The identifier is neither a
credential nor authorization.

`operations.contracts.validate_playbook_binding` validates the named profile,
immutable playbook binding, executor and retry settings, and every playbook
hard limit. Referenced file or diff content MUST be supplied through its
`content_resolver`; unresolved content fails closed because its hash and byte
limits cannot be verified. `validate_authorization_binding` binds a decision to
the exact operation, requester, policy, authority requirement, and mandatory
approval mode. `validate_approval_binding` requires one granted approval under
the operation's exact policy and binds it to exact stored content.

`operations.approval.ApprovalService` enforces the runtime side of that
contract. It accepts only an operation identifier from the untrusted action
payload, requires a fresh `VerifiedPrincipal` supplied out-of-band by an
authenticated adapter, rechecks the configured client, audience, tenant, and
workspace boundary, and reloads the stored operation. `VerifiedPrincipal` is a
trusted capability and not a request-deserializable data-transfer object. The
service recalculates the canonical hash, enforces operation state and expiry,
applies the exact versioned approval policy, and constructs the approver field
from verified identity rather than request data.

Credential and operation expiry are rechecked immediately before persistence.
The persistence port carries expected hash and state preconditions plus an
exclusive `commit_not_after` deadline set to the earliest credential,
operation, or approval-record expiry. It returns a typed recorded,
precondition-failed, or deadline-expired outcome; raw string lookalikes fail
closed. The durable store must enforce the hash, state, and deadline in the same
conditional transaction that records the approval and state transition.

## Source-Control Profile

The source-control prepared operation binds:

- requester subject, trusted client, tenant, and workspace;
- provider and stable repository identifier;
- target branch, verified base revision, and derived proposal branch;
- normalized relative POSIX paths;
- exact inline UTF-8 content and hash, or immutable content reference and hash;
- exact rendered diff and hash, or immutable diff reference and hash;
- resource enrollment and policy identifiers and versions;
- calculated risk level, score, and factors;
- capability identifier and version;
- the single allowed action, retry limits, operation contract version, and
  executor binding; and
- recommendation source and correlation identifiers.

Semantic validation rejects absolute paths, empty segments, `.` and `..`
segments, backslashes, duplicate paths, mismatched inline or resolved hashes,
an incorrect derived branch, or an incorrect duplicate-content hash. Playbook
binding additionally enforces allowed extensions, per-file UTF-8 byte limits,
total file byte limits, the maximum file count, and a 5,000,000-byte rendered
diff limit.

The executable document carries no provider credential, token, email address,
display name, provider SDK object, generic provider command, or unfiltered
provider response. Its provider vocabulary is limited to stable domain values.

## Authorization and State

An authorization decision records all six authority ceilings from ADR 0001:
deployment, tenant, workspace, principal, capability, and risk policy.
`effective_authority` MUST be their deterministic minimum. Positive decisions
require authority within the playbook's inclusive minimum and maximum range in
contract `1.0`; a disabled deployment always denies. A playbook's minimum
authority cannot exceed its maximum. The decision and reason codes MUST agree, and a playbook that requires approval
cannot produce an `authorized` decision. The exact policy version makes the
result reproducible.

State changes are immutable records. Contract `1.0` publishes these
transitions:

| From | Allowed next states |
|---|---|
| No state | `prepared` |
| `prepared` | `pending_approval`, `rejected`, `cancelled`, `expired` |
| `pending_approval` | `approved`, `rejected`, `cancelled`, `expired` |
| `approved` | `dispatched`, `cancelled`, `expired` |
| `dispatched` | `executing`, `retry_pending`, `failed` |
| `executing` | `succeeded`, `retry_pending`, `failed` |
| `retry_pending` | `dispatched`, `failed`, `expired` |
| Terminal states | No transitions |

The required-approval v1 workflow cannot reach `approved` or `dispatched`
directly from `prepared`. Each transition has exactly one matching reason code;
unrelated audit reasons fail validation. The terminal states are `succeeded`,
`failed`, `rejected`, `cancelled`, and `expired`. Retry attempts occur before a
terminal `failed` transition.

## Ledger and Errors

Ledger storage MUST be append-only. Each operation has a strictly increasing
`sequence`; an event identifier is unique and an accepted event is never
updated or deleted. Event type and payload type must match. Payload schemas
permit only bounded domain results and stable provider identifiers, never raw
provider responses.

Application errors have a closed error-code set, a bounded safe message,
retryability, and correlation. They expose only bounded field paths and state.
Adapters map these errors to their protocol without changing product meaning.

## Additive Capacity-Adjustment Profile (E2)

The `gamelift.capacity-adjustment/1.0` capability adds an **E2 prepare** layer on
top of the read-only E1 observation. It is delivered as four additive schemas
that reuse the immutable `common` `$defs` but are deliberately **not** part of
the published source-control write-contract set (`SCHEMA_NAMES`) or its playbook
binding, exactly like the additive `gamelift-observation` contract. Adding them
therefore cannot change a published v1 write contract or its playbook hash.

| Contract | Version field | Schema |
|---|---|---|
| Capacity proposal request (untrusted) | `request_contract_version` | `gamelift-capacity-proposal-request.schema.json` |
| Capacity advice (deterministic) | `advice_contract_version` | `gamelift-capacity-advice.schema.json` |
| Capacity authorization decision (trusted) | `authorization_contract_version` | `gamelift-capacity-authorization.schema.json` |
| Capacity prepared operation (immutable) | `operation_contract_version` | `gamelift-capacity-prepared-operation.schema.json` |

The **untrusted** proposal request carries only bounded target/requested capacity
intent (`fleet_id`, `location`, desired/min/max) and an idempotency token. It has
no identity, correlation, policy, enrollment, risk, executor, credential,
approval, deployment-mode, playbook, or trusted current-state field, and
`additionalProperties` is false everywhere, so injecting any such field fails
validation.

A trusted `AdviceService` resolves the requester only from the verified
principal, loads the current fleet capacity from a trusted E1 observation/status
port at advice time, applies server-owned enrollment/policy bounds, and computes
a deterministic change and risk. A trusted `PrepareService` then selects the
exact playbook/capability in code — never from model text — evaluates the six
ADR 0001 authority inputs and their deterministic minimum, and produces exactly
one of `approval_required` or `denied`. Preparing and approving are **advise**-
authority acts — they bind an intent and record a human approval but never touch
a provider — so a non-denied outcome only requires the six authority inputs to
clear `advise`; `observe` and `disabled` still deny (`INSUFFICIENT_AUTHORITY` /
`DEPLOYMENT_DISABLED`). A GameLift capacity change is never `authorized` outright;
a non-denied outcome always requires a direct human approval.

Every prepared operation and its authorization carry an immutable, hash-bound
`required_execution_authority` (always `remediate`). This is the execution
authority a **future E3 executor MUST independently re-verify** (deployment mode
and policy at or above `remediate`) before any provider write. The advise-
authority E2 phase never satisfies or elevates it, and approving an operation
does not raise it. Because it is inside the hashed material, tampering with it
changes `prepared_hash` and fails validation (it is also `const: "remediate"` in
schema).

The prepared operation is immutable and idempotent: its `prepared_hash` binds
every other field — the target, the current-state observation id/hash, the exact
desired/min/max change, the playbook/profile/capability/contract versions, the
authority inputs/decision, the `required_execution_authority`, the calculated
risk, the requester scope, the expiry, and the future executor binding
identifier — and excludes only itself. Its
timestamps are a deterministic function of the bound current-state revision, not
the wall clock: `created_at` is the trusted E1 observation revision anchor
(`current_state.observed_at`) and `expires_at` is derived as the earlier of the
trusted observation expiry (`current_state.expires_at`) and `created_at` plus the
configured preparation TTL. The live clock is used only to decide whether that
anchor/current-state revision is still fresh, so the same token, intent, and
current-state revision retried later yield byte-for-byte identical timestamps,
`operation_id`, document bytes, and `prepared_hash`; a changed current-state
revision yields a distinct hash. Provider writes and executor credentials are
structurally absent: there is no provider-write parameter and the future executor
binding is an identifier only.

### Expiry (lazy-on-access)

A prepared operation carries an immutable, hash-bound `expires_at`. Expiry is
enforced **lazily on access**: before the E2 request handler treats an operation
as active — on `GET /operations/{id}` evidence and on
`approve` / `reject` / `cancel` — it first calls
`LifecycleDecisionService.expire_if_due`. If the operation is past `expires_at`
and still in a non-terminal, pre-dispatch state (`prepared` / `pending_approval`
/ `approved`), that call atomically transitions it to `expired` with a **system**
actor (`operations.expiry-sweeper`) and an appended `operation.state-changed`
ledger entry, through the same fenced, conditional store transaction the other
terminal decisions use. Expiry is driven by the trusted system clock and needs
no caller credential.

Consequences the handler guarantees:

- **Terminal is a safe no-op.** An already-terminal operation (or a lost race)
  makes `expire_if_due` a no-op; the opportunistic call is swallowed and the
  underlying route returns its own bounded response (e.g. a `409` conflict, or a
  `404` if the record is gone).
- **One terminal state, one transition.** A race between expiry and
  `approve` / `reject` / `cancel` resolves through the single fenced writer, so
  exactly one terminal state and one state transition win; the loser observes a
  bounded `STATE_CONFLICT` / `APPROVAL_EXPIRED`.
- **Approval after due never grants.** `ApprovalService.grant` independently
  rechecks `expires_at` against a fresh clock before and immediately before
  commit, returning `APPROVAL_EXPIRED` (`409`) — a due operation is never
  granted, whether or not the lazy transition has already run.
- **Metric on transition win.** When the lazy transition wins, the handler emits
  the bounded `ApprovalExpired` CloudWatch metric (no identifiers/dimensions).

There is **no scheduler or Step Functions sweep** in this phase. Expiry is
observed only when an operation is next accessed; an untouched due operation
stays at its stored non-terminal state until the first access transitions it.
A periodic background sweep that expires idle operations without an access is
deliberately deferred to **E4**.

## Additive Bounded-Autonomy Profile (E5, v2)

The `gamelift.capacity-adjustment/2.0` capability adds a **bounded-autonomy**
(no-human-in-the-loop) layer that realizes the `operate` rung reserved by
[ADR 0001](adr/0001-preserve-chat-and-add-optional-operations.md) and decided in
[ADR 0006](adr/0006-bounded-autonomy-capacity.md). It is a **v2** layer: every
document carries a `*_contract_version` of `"2.0"` and a distinct
`urn:game-agent:operations:contracts:v2:...` `$id`, all new `$defs` are defined
inline, and only the frozen v1 `common` `$defs` are referenced. The v2 schema
names are disjoint from the published write-contract set and from the E2/E3
capacity/execution sets, so adding this layer leaves every published v1 schema,
vector, hash, and meaning byte-for-byte unchanged. Version selection uses a
separate `is_supported_autonomy_version("2.0")` check; the v1 `== "1.0"`
allowlist is not loosened.

| Contract | Version field | Schema |
|---|---|---|
| Bounded-autonomy policy (immutable, server-owned) | `policy_contract_version` | `gamelift-capacity-autonomy-policy.schema.json` |
| Autonomous decision (deterministic, trusted) | `decision_contract_version` | `gamelift-capacity-autonomous-decision.schema.json` |
| Autonomous prepared operation (immutable) | `operation_contract_version` | `gamelift-capacity-autonomous-operation.schema.json` |

The **bounded-autonomy policy** is the entirely server-owned guardrail envelope.
It is never client, model, or request-body input. It binds the capability `2.0`,
a distinct trusted **automation** principal (`source_type: const "automation"`,
distinct from the human `trusted_identity`), tenant/workspace/enrollment and the
exact enrolled fleet/location, the exact `0/1/1` capacity envelope (`floor`,
`ceiling`, `max_step` are each `const`), the risk ceiling, observation freshness
(max age and skew), an **integer micro-USD** per-action and per-window budget,
cooldown, frequency, `max_in_flight: const 1` concurrency, anti-oscillation,
`decision_ttl_seconds`, and the exact playbook/executor identities/versions/
hashes. `required_execution_authority` is `const "operate"`. Its `policy_hash`
binds every field except itself.

The **autonomous decision** is a pure, deterministic reading that produces
exactly one of `authorized`/`denied` at `operate` authority. A non-denied
decision is `authorized` with the single reason code `APPROVED_AUTONOMOUS` and an
`effective_authority` of `operate` (the deterministic minimum of the six ADR 0001
authority inputs); any guardrail breach denies with the specific closed reason
code(s) from the extended v2 set (`BOUNDS_EXCEEDED`, `RISK_LIMIT_EXCEEDED`,
`OBSERVATION_STALE`, `BUDGET_EXCEEDED`, `COOLDOWN_ACTIVE`, `FREQUENCY_EXCEEDED`,
`CONCURRENCY_LIMIT`, `OSCILLATION_BLOCKED`, `DECISION_EXPIRED`,
`AUTOMATION_PRINCIPAL_INVALID`, and the shared authority/enrollment/workspace
codes) and never carries `APPROVED_AUTONOMOUS`. It binds the exact policy
id/version/hash and observation id/hash and carries `decision_expires_at =
min(observation.expires_at, evaluated_at + policy.decision_ttl_seconds)`. There
is **no human-approval field**: the decision itself is the time-boxed grant.

The **autonomous prepared operation** is immutable and idempotent. Its
`authority.decision` enum is the `["authorized","denied"]` mirror of the v1
`["approval_required","denied"]` prepared operation, and it has **no approval
field anywhere**. Its `prepared_hash` binds every other field — the target, the
autonomy policy id/version/hash, the bound decision id/hash, the observation
id/hash, the exact desired/min/max change, the playbook/profile/capability/
contract versions, the six authority inputs and their minimum, the calculated
risk, the automation principal scope, `decision_expires_at`, the executor
binding, and `required_execution_authority` (`const "operate"`) — and excludes
only itself.

`evaluate_autonomy_policy` is the pure deterministic policy evaluator. It is a
total function of trusted inputs only (a resolved policy, the six authority
inputs, the verified automation principal, a trusted E1 observation, the proposed
capacity triple, the durable rolling-window state, and a supplied epoch clock).
It performs no AWS, runtime, or infrastructure work, reads no environment, and
holds no credential; identical inputs always yield an identical reading. Money is
integer micro-USD only. **Model and untrusted input supply none of** identity,
policy, limits, authorization, playbook, executor, or credentials: all are
server-owned and hash-bound, `additionalProperties` is `false` everywhere, and
the evaluator matches the automation principal against the policy rather than
trusting request-body or model-supplied identity.

The v2 playbook (`playbook.gamelift-capacity-autonomy` / `2.0.0`) **preserves**
the E2/E3 executor binding (`executor.gamelift-capacity` / `1.0`) — the single
`UpdateFleetCapacity` write machinery is reused unchanged — but its precondition
set (the deterministic guardrail envelope instead of a granted human approval)
and its `operate` execution authority make its hash **distinct** from the E2
`playbook.gamelift-capacity` / `1.0.0` hash. `autonomy-contract-vectors.json`
pins the policy, decision, prepared-operation, and playbook hashes and enumerates
the hash-invalidation cases for every bound field.

This E5 layer publishes **contracts and a pure evaluator only**. It defines no
infrastructure, runtime service, executor code path, IAM, or config lever. A
default deployment creates, authorizes, and executes nothing.

## Compatibility and Publication

Version `1.0` is exact, not a minimum. Consumers MUST use an explicit allowlist
and reject unknown versions with `CONTRACT_VERSION_UNSUPPORTED`. They MUST NOT
guess compatibility, silently drop unknown fields, or reinterpret a published
version. Schemas set `additionalProperties` to false at contract boundaries.

After publication, a contract version and its schema identifier never change
meaning. Any semantic or structural change requires:

1. a new version field value and schema identifier;
2. a new schema file rather than modification of the published file;
3. new canonical hash and compatibility vectors;
4. an executor allowlist update; and
5. compatibility tests proving old documents retain their original meaning.

The source-control playbook lists the exact hashes of the complete v1 schema
set. A changed schema therefore invalidates playbook validation and requires a
new playbook version and hash.

The additive **v2** bounded-autonomy layer (`gamelift.capacity-adjustment/2.0`) follows this rule as a new version, not a change to any v1 document: it introduces new `*_contract_version: "2.0"` values, new `urn:...:v2:...` schema identifiers, new schema files, new canonical hashes and vectors (`autonomy-contract-vectors.json`), and a separate `is_supported_autonomy_version` allowlist, while every published v1 schema, playbook hash, and pinned vector remains byte-for-byte unchanged. A future autonomous executor allowlist keys on the capability version and the distinct autonomy playbook/executor hashes so a v1 human-approved path can never execute a v2 autonomous operation.
