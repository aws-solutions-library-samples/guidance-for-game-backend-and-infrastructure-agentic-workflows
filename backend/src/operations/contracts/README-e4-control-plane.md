# E4 Operations Control-Plane Contract Prelude (issue #416)

This is the blocking, **additive** contract prelude every E4 agent — backend,
frontend, and infrastructure — builds against. It freezes the v1 shapes for the
operations control plane so the three agents can proceed in parallel without
re-negotiating wire formats.

## Additive boundary

This layer never modifies the published E1/E2/E3 contracts. It reuses the
immutable `common` `$defs` but keeps its own schema registry
(`CONTROL_SCHEMA_NAMES` in `control_plane.py`) that is disjoint from every
published set:

- `SCHEMA_NAMES` (source-control / write contracts),
- `CAPACITY_SCHEMA_NAMES` (E2 capacity), and
- `EXECUTION_SCHEMA_NAMES` (E3 execution).

Because the E4 schemas never join those sets, they cannot change a published v1
contract, its validators, or its playbook hash. The disjointness is asserted in
`test_operations_control_plane_contract_unit.py`.

## The eight contracts

| Schema | Purpose |
| --- | --- |
| `operations-kill-switch` | The single deployment-wide AppConfig kill-switch document. |
| `operations-capability-discovery` | Server-computed availability the UI reads before showing a control. |
| `operations-list-request` | Untrusted list request: bounded page size, opaque cursor, coarse filters. |
| `operations-list-response` | Bounded page (≤ 50) of public-safe operation summaries + opaque `next_cursor`. |
| `operations-detail-projection` | Bounded operation detail/evidence projection. |
| `operations-control-request` | Admin control: desired booleans + expected `config_version` only. |
| `operations-control-response` | Outcome (`applied` / `version_conflict` / `denied`) + resulting version. |
| `operations-control-audit-record` | Immutable, hash-bound record of one admin decision. |

All schema `$id`s are `urn:game-agent:operations:contracts:v1:<name>`.

## Kill-switch document

The kill-switch schema is authored to be usable **directly as an AWS AppConfig
JSON Schema validator**. It is fully self-contained: every `$ref` is a local
`#/$defs` fragment (the `contract_version` and the normalized UTC-`Z` timestamp
definitions are inlined rather than pointing at the `common` document), so
AppConfig can validate it with no external reference to resolve. `issued_at` and
`not_after` are constrained to the normalized UTC `Z` form
(`YYYY-MM-DDThh:mm:ss(.sss)Z`), so lexicographic order equals chronological
order. It is a closed object (`additionalProperties: false` everywhere) that
carries exactly:

- `operations_enabled` — the deployment-wide master switch;
- `capabilities."gamelift.capacity-adjustment"` — the single capability's
  `prepare` / `dispatch` / `execute` booleans (no other capability is allowed);
- `config_version` — an immutable monotonic integer;
- `issued_at` / `not_after` — freshness bounds.

Semantic rules (enforced by `validate_control_contract`, beyond what a bare
AppConfig JSON Schema check can express):

- `not_after` must be strictly after `issued_at` (freshness);
- phases are ordered: `prepare >= dispatch >= execute`;
- when `operations_enabled` is `false`, no phase may be enabled.

The **default safe document** disables everything; build it with
`default_safe_kill_switch(...)`.

## Discovery, availability, and gates

`operations-capability-discovery` distinguishes three independent facts —
`available` (code path exists in the build), `provisioned` (deployment created
the resources), `enabled` (runtime kill-switch and authority permit use) — with
the lattice `enabled ⇒ provisioned ⇒ available`. Each gate is `static` (fixed at
build/deploy time) or `dynamic` (evaluated per request). A hidden or disabled
control is never authorization; the backend re-checks every gate.

## Listing: bounds and the opaque cursor

Page size is capped at **50** in both the request and response schemas, and the
response semantic validator re-checks that `len(operations) <= page_size <= 50`.

Pagination uses an **opaque, tamper-evident cursor**. Clients treat it as a
blob: echo it verbatim, never construct or parse it. The backend mints it with
`encode_cursor(position, key=...)` and reads it with `decode_cursor(token,
key=...)`. The token is `base64url(canonical_position).base64url(HMAC-SHA256)`;
`decode_cursor` rejects a flipped payload, a flipped signature, a truncated
token, an empty segment, or a wrong key with `CursorError` rather than returning
a forged position. The position is canonicalized (RFC 8785) before signing, so
an identical logical position always mints an identical cursor.

## Projections exclude sensitive data

`operations-list-response`, `operations-detail-projection`,
`operations-capability-discovery`, `operations-control-response`, and
`operations-control-audit-record` are public-safe: none of them carries
`email`, `display_name`, `token`, `arn`, `account_id`, `fleet_id`, or a raw
provider payload. The detail projection exposes lifecycle **phases**, current
**state**, **verification** visibility, and **rollback** visibility only.

## Controls carry no identity

`operations-control-request` carries only `expected_config_version` and the
`desired` boolean state — never identity, credential, principal, or policy. The
backend resolves the acting admin from the verified caller and enforces
authority independently. `expected_config_version` makes the write a
compare-and-set, so a stale change cannot clobber a newer document.

## Immutable audit record

`operations-control-audit-record` binds every field with `record_hash`, a tagged
SHA-256 over the RFC 8785 canonicalization of the record with `record_hash`
removed (it excludes only itself). Any field mutation breaks the hash and fails
validation. An `applied` record must advance `resulting_config_version` beyond
`previous_config_version`; a non-applied record must not advance it.

## Frozen routes

`ROUTE_KEYS` (in `control_plane.py`) freezes the API Gateway `RouteKey` strings
all three agents must agree on:

```
GET  /operations/capabilities
GET  /operations
GET  /operations/{operationId}
POST /operations/control
GET  /operations/control/kill-switch
```

## Using the contracts

```python
from operations.contracts import (
    validate_control_contract,
    encode_cursor, decode_cursor,
    control_audit_record_hash,
    default_safe_kill_switch,
    ROUTE_KEYS, MAX_PAGE_SIZE,
)
```

- `validate_control_contract(schema_name, document)` — schema + semantic checks;
  raises `ControlContractError`.
- `load_control_schema(schema_name)` — a defensive copy of one schema.

Public-safe JSON examples for copy-paste live in
`backend/tests/fixtures/operations/v1/control-plane-examples.json`, and each is
bound to its schema by the contract test suite.
