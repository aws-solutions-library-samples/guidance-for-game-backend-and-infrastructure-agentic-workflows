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
the operation's exact policy and binds it to exact stored content. Identity
lifecycle and expiry enforcement belong to the trusted services defined by
later operations issues.

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
