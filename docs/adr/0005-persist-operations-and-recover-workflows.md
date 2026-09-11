
# ADR 0005: Persist Operations State and Recover Workflows

- **Status:** Proposed
- **Date:** 2026-09-01
- **Decision issue:** [#279](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/279)

## Status Rationale

This record is **Proposed**, not Accepted. The synchronous single-request
observation design in [Durable observation state without queues](#durable-observation-state-without-queues)
asserts that three bounded provider reads plus persistence fit inside the
API Gateway HTTP API integration timeout. That claim requires measured latency
evidence — a validated request-completion percentile below the timeout with the
declared margin — before acceptance. That evidence is not yet available, and
this record does not fabricate it. The remaining decisions (externalized
content, state model, idempotency, fencing, ledger authority, IAM boundary,
and approval authority) are stable and can be reviewed now, but the record
stays Proposed until the latency acceptance evidence exists. See
[Deferred decisions](#deferred-decisions).

## Context

The optional operations control plane accepted in [ADR 0001](0001-preserve-chat-and-add-optional-operations.md)
needs durable status and audit records for the observation phase and a durable
workflow for future approval and provider writes. The versioned records,
canonical hashes, lifecycle, and source-control profile are already defined in
[Operations Contracts](../OPERATIONS_CONTRACTS.md). This record chooses how
those records are stored, queried, committed, and recovered without weakening
the boundaries accepted in [ADR 0002](0002-use-protocol-neutral-operations-services.md)
and [ADR 0003](0003-isolate-provider-writes.md).

The observation phase must record durable status and an audit trail without
queue infrastructure. It runs three bounded provider reads inside one request
and must fit the API Gateway HTTP API integration timeout. Approval and
provider writes need a durable workflow that can resume after a lost response
or an unclear provider result without repeating an action. The source-control
executor in [#314](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/314)
and its consumer in [#268](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/268)
must never create a duplicate branch, commit, or proposal on retry.

A prepared operation is not small. Operations Contracts allow inline UTF-8 file
content, per-file and total file byte limits, and a rendered diff up to
5,000,000 bytes. A single prepared-operation document can therefore far exceed
the DynamoDB maximum item size of 400 KB and can exceed the 4 MB aggregate limit
of one DynamoDB transaction. The store cannot assume a prepared operation, its
state, and its ledger events all fit in one item or one transaction.

This record defines implementation limits for the optional stack. It adds no
production tables, buckets, API routes, queues, workers, dead-letter queues, or
provider write permissions, and it does not enable operations.

## Decision

### Durable store and access patterns

Persist bounded metadata for prepared operations, operation state, approval
records, idempotency mappings, and ledger events in Amazon DynamoDB. Store large
prepared-operation content as application-write-once, content-addressed objects
in object storage (Amazon S3). Every required read is a key lookup or a
single-partition query. No required query performs a table scan.

The store uses these access patterns:

| Item | Location | Partition key | Sort key | Read pattern |
|---|---|---|---|---|
| Prepared-operation metadata | DynamoDB | operation identifier | fixed record marker | Get by operation identifier |
| Externalized content object | Object storage | content hash address | — | Get by bound content hash |
| Current-state snapshot | DynamoDB | operation identifier | `STATE#current` marker | Get by operation identifier |
| State transition | DynamoDB | operation identifier | `STATE#<sequence>` | Get one transition; query ascending for history |
| Approval record | DynamoDB | operation identifier | approval marker | Get by operation identifier |
| Idempotency mapping | DynamoDB | workspace + idempotency token | fixed marker | Get to resolve one operation |
| Provider-result record | DynamoDB | operation identifier | provider-result correlation identifier | Get by operation identifier and correlation identifier |
| Ledger event | DynamoDB | operation identifier | strictly increasing sequence | Query one operation, ascending |
| Single-flight lease | DynamoDB | operation identifier | lease marker | Get by operation identifier |

The prepared-operation document is written once and never rewritten. Its
canonical hash is stored beside the metadata, as defined in Operations
Contracts, so approval and execution bind to exact content without a circular
hash field. The operation identifier, prepared-operation hash, idempotency
token, and provider-result correlation identifier are distinct values and never
substitute for one another. An operation identifier is not a credential or
authorization.

### Externalized content and serialized size ceilings

Large prepared-operation content — inline file bodies and the rendered diff —
is externalized to application-write-once, content-addressed object storage. The
DynamoDB prepared-operation item holds only bounded metadata: the operation
identifier, the prepared-operation hash, the idempotency token, contract and
profile versions, correlation identifiers, risk fields, path list, per-object
content and diff hashes, per-object byte sizes, and the object-storage
references that resolve to the bytes. It never inlines a body or diff whose size
is unbounded by these ceilings.

Explicit serialized size ceilings apply:

- Each externalized object obeys the Operations Contracts limits: per-file and
  total file byte limits and the 5,000,000-byte rendered diff limit.
- The DynamoDB prepared-operation item, and every other DynamoDB item, MUST
  serialize to at most 400 KB, the DynamoDB item-size limit. The implementing
  issue sets a conservative inline ceiling well below 400 KB; any field that
  could exceed it is externalized by reference rather than inlined.
- Any DynamoDB transaction MUST stay within 100 items and 4 MB aggregate.
  Because content is externalized and only bounded metadata participates in a
  transaction, the transactions defined below stay well within these limits.

Validation obligations bind content to its hash on every read. An object
reference resolves through the contract `content_resolver`; the resolved bytes
MUST hash under the Operations Contracts SHA-256 algorithm to exactly the
recorded content or diff hash before any use. A missing object, a size above
the declared ceiling, or a hash mismatch fails closed with a typed, retryable
or conflict error and never proceeds with unverified bytes. Object storage is
written once per content address and never overwritten in place; a rewrite of
the same address with different bytes is treated as corruption and fails
closed.

### Durable observation state without queues

The observation phase commits the operation state transition and its ledger
events in one DynamoDB transaction, then performs its bounded provider reads in
the request. There is no observation-phase queue, relay, worker, or dead-letter
queue. State and ledger writes use conditional or transactional writes so that
a concurrent or retried request cannot silently replace a record.

The complete observation request runs three provider reads within the API
Gateway HTTP API integration timeout. That integration timeout has a documented
ceiling of 30 seconds and cannot be raised. The request budget is allocated as
explicit sub-budgets that sum to less than the ceiling: an individual bound per
provider read, a bound for persistence and canonical serialization, and a
reserved cancellation margin so the request aborts and returns a typed error
before the gateway itself times out. Each provider read is issued with a
deadline; if a sub-budget is exceeded, in-flight work is cancelled and the
request fails closed with a typed, retryable error rather than returning a
partial result.

Acceptance of this synchronous design requires measured evidence, not a static
budget alone: a validated request-completion percentile (for example p99)
measured under representative provider latency, confirmed to complete within
the ceiling minus the cancellation margin. Until that measured percentile
exists, this design is Proposed. A timed synthetic provider-read test exercises
the budget in continuous integration, but a synthetic test is not the
acceptance evidence and no latency percentile is asserted here.

### Idempotency and independent operations

A submission carries an idempotency token. The canonical idempotency
fingerprint is the SHA-256 canonical hash, computed with the Operations
Contracts algorithm, over the trusted workspace, the idempotency token, and the
prepared-operation hash. The fingerprint is stored on the idempotency mapping.

Creating an operation is one atomic DynamoDB transaction that writes, all or
nothing:

1. the idempotency mapping, conditional on absence, carrying the fingerprint
   and pointing at exactly one operation identifier;
2. the prepared-operation metadata item (content already externalized and
   hash-verified beforehand);
3. the current-state snapshot at the initial `prepared` state, sequence 0, and
   fencing generation 0;
4. the immutable `STATE#0` transition into `prepared`; and
5. the initial ledger event at sequence 0.

Because the mapping, operation, initial state, and initial ledger event commit
together, a partially created operation cannot exist. If the transaction fails
the condition on the mapping, a mapping already exists and the request is a
replay.

A retry recomputes the fingerprint and compares it to the stored mapping.
An identical fingerprint with the same token and trusted workspace resolves to
that same operation and returns its stored result. Reusing the token with
different content produces a different fingerprint and fails with
`IDEMPOTENCY_CONFLICT`; it never mutates the existing operation.

Two independent submissions with identical content remain separate operations
with separate ledger records and separate canonical operation hashes. A
duplicate-content hash is a pre-approval policy input only: policy may reject
the later operation or link it to the earlier one before approval, but it never
silently merges workflows and never reuses an approval. Duplicate content is
never combined after approval.

### State model: mutable snapshot and immutable transitions

Operation state has two distinct representations that must not be conflated:

- The **current-state snapshot** is a single mutable item per operation. It is
  advanced only by a conditional update keyed on the expected prior state, the
  expected prior sequence, and the held fencing generation. It records the
  current state, current sequence, held fencing generation, and a reference to
  the last transition, and — while an execution lease is held — the current
  lease holder identity and the absolute lease deadline copied from the lease
  item, so the full fencing tuple (holder, generation, deadline) lives on the
  snapshot itself.
- Each **state transition** is an immutable `STATE#<sequence>` record. A
  transition is written once with a condition that its sort key does not
  already exist and is never updated or deleted.

Advancing state is one atomic DynamoDB transaction that:

1. puts the new immutable `STATE#<sequence>` transition, conditional on
   `attribute_not_exists` of that sort key;
2. updates the mutable current-state snapshot, conditional on the expected
   prior state, prior sequence, and held fencing generation; and
3. appends the matching ledger event at the next strictly increasing sequence,
   conditional on `attribute_not_exists`.

The transition record, the snapshot advance, and the ledger append therefore
commit together or not at all. A stale or racing writer fails one of the
conditions and changes nothing. Contract `1.0` publishes the transitions in
Operations Contracts, including the terminal states `succeeded`, `failed`,
`rejected`, `cancelled`, and `expired`. Approval expiry, cancellation, and
execution transitions all use these conditional transactions, so a stale or
racing transition fails rather than overwriting a newer state.

Cancellation is a conditional transition allowed only from a non-terminal
state. A cancellation that races a transition into a terminal state loses the
conditional write and does not alter the terminal record. Durable operation
state prevents a browser or model from changing approved files during a resume
request, because resume rebinds to the stored operation and hash.

### Single-flight lease, fencing, and recovery

A single-flight execution lease serializes execution for one operation. The
lease is a distinct DynamoDB item carrying a holder identifier, an absolute
expiry deadline, and a **monotonically increasing fencing generation**. Claiming
and reclaiming a lease each advance the lease item and the current-state
snapshot to the same fencing generation in **one atomic DynamoDB transaction**,
so the two records can never disagree about the active generation:

- **Claim** succeeds only when no active lease exists. One transaction
  conditionally puts the lease item (holder, expiry, generation) and, in the
  same transaction, conditionally updates the current-state snapshot to record
  that same holder, generation, and absolute lease deadline (the lease expiry).
  Either both records move to the new generation or neither does.
- **Reclaim** of an expired lease is one transaction that conditionally
  increments the lease generation — conditional on the stored expiry lying in
  the past — and, in the same transaction, advances the snapshot's held
  generation, holder, and absolute lease deadline to the new values. An
  unexpired holder's reclaim condition fails, so a live holder cannot be
  displaced.

Because claim and reclaim move the lease and the snapshot together, there is no
window in which a replacement holder owns the lease while the snapshot still
trusts a prior generation, and no window in which the snapshot advances while
the lease does not. While a lease is active and unexpired, a second attempt for
the same operation is rejected, which prevents a duplicate execution.

Fencing is mandatory on every commit made under an execution lease. Once an
operation holds an execution lease, each state transition, ledger append,
provider-result write, and snapshot advance the holder makes is a conditional or
transactional write that validates, at the store, that the current-state
snapshot still records that writer's holder identity, that writer's fencing
generation, and an absolute lease deadline that has not passed. All three
components of the snapshot fencing tuple — holder, generation, and deadline —
are checked in the same condition, so a commit is refused unless the holder and
generation still match and the copied deadline has not elapsed. (The pre-lease
creation and observation transactions carry no lease holder; they are guarded by
the expected prior state, prior sequence, and held generation instead.) A holder
whose lease expired or was reclaimed at a higher generation fails these checks,
loses every conditional write, and cannot corrupt state or the ledger even if it
wakes up late and attempts to continue. Fencing is enforced at the store through
the condition, not by trusting the holder to stop.

Recovery is deterministic:

- A **lost response** replays against durable state. If a terminal state is
  already recorded in the snapshot, the replay returns that stored result
  without repeating work.
- An **expired lease** lets the same idempotency token reclaim the lease
  through the atomic claim/reclaim transaction above, which advances the lease
  and the snapshot generation together, and then resumes. It first reads the
  stored operation state and then, only if the state is inconclusive, reconciles
  against the provider before taking any further action.

Database fencing protects DynamoDB records; it cannot by itself prevent a
duplicate side effect at an external provider, because an expired holder may
still have a request in flight when a replacement holder acts. Preventing
duplicate provider actions therefore requires a provider-side primitive, not a
database condition. This record persists **one stable logical-action
identifier** per logical provider action. That identifier is derived from the
operation identifier and the specific action, is recorded in the
provider-result record, and never changes across retries or lease reclaims. It
is not derived from the attempt.

Each retryable provider action MUST be bound to a capability-specific provider
primitive keyed on that stable logical-action identifier: a native provider
idempotency token, a conditional compare-and-set or precondition (for example an
expected-ref or if-match check), or provider-enforced deterministic uniqueness
of the created artifact. The executor selects the primitive per capability and
records which primitive guards the action. If a provider action offers none of
these primitives and its outcome is inconclusive after a failure or reclaim,
automatic retry MUST stop and the operation fails closed with a typed error for
human reconciliation; the executor never blindly re-issues such an action.

Before issuing a write on a reclaimed lease, the executor reconciles: it reads
the stored provider-result record, correlated by the provider-result
correlation identifier, and queries the provider for the deterministic artifact
before acting. The durable write-back of the provider-result record is itself a
fenced conditional write validated against the current holder, generation, and
lease deadline.

For the source-control executor (#314), one operation records the deterministic
proposal branch derived from its operation identifier — the `gba-op-` prefix and
branch derivation defined in Operations Contracts — together with any returned
commit or proposal identifier in a provider-result record correlated by the
provider-result correlation identifier. The deterministic branch name is the
provider-enforced uniqueness primitive: an attempt to create the same branch
twice is rejected by the provider. A retry with the same idempotency token
first checks the stored state, then checks the provider, and reuses the existing
branch, commit set, or proposal. It never creates a second branch, commit set,
or proposal. An unclear provider result reconciles against the provider before
any write and fails closed if the outcome cannot be confirmed.

### Approval authority and state transitions

Approval authority belongs to the `ApprovalService` introduced in
[PR #334](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/pull/334),
a protocol-neutral application service consistent with [ADR 0002](0002-use-protocol-neutral-operations-services.md)
and the approval-identity rules in [Identity and Authorization](../IDENTITY_AND_AUTHORIZATION.md).
The `ApprovalService` receives a direct authenticated approval action, loads the
stored operation and canonical prepared-operation hash, verifies policy, state,
approver identity, and separation-of-duties rules, and records the verified
approval against that one hash. The durable workflow does not make an approval
decision. Step Functions only orchestrates the wait for that decision and the
subsequent dispatch to the executor; it never authorizes an operation.

Approval binds to the stored `operation_id` and the stored prepared-operation
hash, as defined in Operations Contracts. Approval never carries executable
operation data; the durable workflow sends only the operation identifier to an
authenticated executor, which reloads the stored operation, approval, and
playbook, resolves and hash-verifies externalized content, and verifies every
binding before the first provider write.

Every state change follows the [state model](#state-model-mutable-snapshot-and-immutable-transitions)
above. In particular, the `pending_approval` to `approved` change is **one
atomic DynamoDB transaction** that records the approval and the state transition
together so they can never diverge after a partial write or crash. Matching the
PR #334 `ApprovalService` contract, that single transaction contains:

1. a **conditional put of the approval record**, conditional on
   `attribute_not_exists` of the approval marker, so a second approval of the
   same operation cannot be recorded;
2. the PR #334 checks enforced as transaction conditions — the expected current
   state is `pending_approval`, the stored prepared-operation hash equals the
   approved hash, and the approval commit deadline has not passed;
3. the immutable `STATE#<sequence>` transition into `approved`, conditional on
   `attribute_not_exists` of that sort key;
4. the conditional current-state snapshot advance into `approved`, conditional
   on the expected prior state, prior sequence, and held fencing generation; and
5. the matching ledger event at the next strictly increasing sequence,
   conditional on `attribute_not_exists`.

Because the approval put, the expected-state, hash, and deadline checks, the
transition, the snapshot advance, and the ledger event commit all or nothing, a
crash or race can never leave a recorded approval without the `approved` state,
or an `approved` state without its approval record. A stale or racing writer
fails one of the conditions and changes nothing. Approval expiry follows the
same pattern: a `pending_approval` operation past its commit deadline
transitions to `expired` through an equivalent conditional transaction rather
than being approved.

Approval reuse can resume the same idempotent execution after a recoverable
failure. It cannot authorize a second independent execution: a new independent
operation requires its own prepared operation, hash, and approval.

### Future durable execution workflow

Approval and provider writes will run on AWS Step Functions Standard workflows.
Standard workflows provide long-lived waits for the `ApprovalService` decision,
native error handling with bounded retry and catch, and durable orchestration
of dispatch to the executor. Their limits shape the design:

- **Maximum execution duration is 1 year.** A pending approval that would
  outlive one year must expire through a conditional state transition rather
  than relying on the workflow to wait indefinitely.
- **Maximum execution history is 25,000 events**, and an execution that reaches
  it fails. Long human-approval waits must avoid unbounded polling loops that
  grow history; the workflow uses task tokens or callback waits rather than
  busy-poll iterations.
- **Maximum input or output between states is 256 KiB.** The workflow passes
  identifier-only payloads — the operation identifier and correlation
  identifiers — never prepared-operation content, diffs, or externalized
  objects.
- **Execution history is retained only 90 days after an execution closes.**
  Step Functions history is therefore not an audit record of record.

The application-layer DynamoDB ledger is the authoritative audit and replay
record. It is retained for the full audit window (see below), independent of
Step Functions history retention, and every operationally significant event is
written to the ledger, not inferred from workflow history. A Step Functions
approval-contract test guards this boundary — identifier-only payloads, ledger
authority, and `ApprovalService` as the approval authority — before any
executor role is deployed.

### Append-only ledger boundary and retention

The ledger is append-only, and that property is enforced at the application
layer by mandatory conditional writes, not by IAM alone. Each operation has a
strictly increasing `sequence`, each event identifier is unique, and an accepted
event is never updated or deleted. Every ledger write is a `PutItem` with a
condition that the sequence does not already exist (`attribute_not_exists`).

IAM cannot by itself make the ledger append-only. Removing `UpdateItem` and
`DeleteItem` from the application role does not prevent overwriting a ledger
event, because a `PutItem` without a condition expression replaces an existing
item, and IAM has no condition key that forces a request to carry a condition
expression. The no-overwrite guarantee therefore comes from the application
always issuing the conditional `PutItem`; the IAM denial of update and delete
is a defense-in-depth reduction of the mutation surface, not the source of the
guarantee. This is an append-only application boundary, not storage-level
immutability. Any stronger immutability guarantee — such as object-lock or
write-once storage — would require a separate control and its own review.

Retention is a documented limit rather than an enabled resource. Ledger events,
state transitions, current-state snapshots, prepared-operation metadata,
externalized content objects, provider-result records, and idempotency mappings
are all retained for the audit and replay window defined by the optional stack.
Provider-result records are part of this full audit and replay set because
reclaimed execution reconciles against them before issuing a write on a
reclaimed lease; discarding them early would break deterministic recovery.
Retention of the
idempotency mapping does not rely on asynchronous expiry: a mapping is kept for
the full audit and replay window — the same window that bounds token reuse — so
a late replay resolves deterministically against the live mapping rather than
racing a time-to-live deletion, and no separate tombstone record is needed. Only
a truly transient item — the single-flight lease — may use a time-to-live
attribute for expiry, and time-to-live is never applied to ledger events,
transitions, snapshots, mappings, provider-result records, or content within
the audit window.

### Deferred decisions

The following are explicitly deferred and are not decided here:

- Production tables, buckets, API routes, and provider write permissions.
- Observation-phase queues, relays, workers, or dead-letter queues.
- The concrete DynamoDB table names, object-storage bucket names, capacity
  mode, and encryption key selection for the optional stack.
- The exact numeric provider-read sub-budgets, cancellation margin, inline
  content ceiling, lease duration, and retention window, which the implementing
  issues fix with tests.
- The measured request-completion latency percentile that this record requires
  as acceptance evidence for the synchronous observation design. This record
  stays Proposed until that evidence exists; it does not assert a measured
  value.
- Storage-level (write-once or object-lock) immutability beyond the append-only
  application boundary.
- Executor authentication and remote MCP identity propagation, owned by the
  approval-identity work in [#278](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/278).
  This record does not resolve those deferrals; it depends on them.
- Any duplicate-content policy stronger than reject-or-link before approval.

## Consequences

- Observation records durable status and audit events without queue
  infrastructure. Whether its bounded reads fit the 30-second integration
  timeout is subject to measured acceptance evidence, so this record is
  Proposed.
- Large content lives in application-write-once, content-addressed object
  storage, so no DynamoDB item or transaction exceeds the 400 KB item limit or
  the 4 MB transaction limit, and every read hash-verifies content before use.
- Every required read avoids a table scan, so cost and latency stay bounded.
- A mutable current-state snapshot gives an O(1) status read while immutable
  `STATE#<sequence>` transitions preserve a verifiable history, and both advance
  atomically with the ledger.
- Idempotency creates the mapping, operation, initial state, and initial ledger
  event atomically and retains the live mapping for the full audit window, so
  lost responses and replays resolve deterministically without a tombstone and
  without racing a time-to-live deletion.
- Claim and reclaim advance the lease and the snapshot's fencing tuple — holder,
  generation, and absolute lease deadline — in one transaction, and every fenced
  write validates all three against the snapshot, so a stale holder loses every
  conditional write. Duplicate
  provider actions are prevented by a capability-specific provider primitive
  keyed on a stable logical-action identifier, and an inconclusive action
  without such a primitive fails closed instead of retrying.
- Approval binds to the stored, hash-verified prepared-operation content and is
  authorized by the `ApprovalService`; the `pending_approval` to `approved`
  transaction records the approval and the state change atomically, so a browser
  or model cannot change approved files during a resume, the workflow cannot
  self-authorize, and approval and state cannot diverge.
- Step Functions Standard provides durable orchestration for approval waits and
  dispatch within its 1-year, 25,000-event, and 256 KiB limits, passing
  identifier-only payloads, while the DynamoDB ledger remains the authoritative
  audit record beyond the 90-day history retention.
- The application role cannot update or delete ledger events, and mandatory
  conditional puts — not IAM alone — provide the append-only guarantee.
- The optional stack adds DynamoDB, object storage, and, later, Step Functions
  cost only when an owner enables operations.

## Rejected Alternatives

- Storing full prepared-operation content and diffs inline in DynamoDB, which a
  5 MB diff would push past the 400 KB item and 4 MB transaction limits.
- Treating a single mutable state item as both the current status and the audit
  history, which loses immutable per-transition records.
- Relying on asynchronous time-to-live to expire idempotency mappings, which
  would let a late replay race a deletion.
- Adding a separate idempotency tombstone record when the live mapping is
  already retained for the full audit and replay window.
- Omitting a lease fencing generation and trusting an expired holder to stop
  writing.
- Advancing the lease generation and the snapshot generation in separate writes,
  which would leave a window in which an expired holder can still commit.
- Deriving the provider idempotency key from the attempt, or assuming every
  provider deduplicates such a key, instead of persisting one stable
  logical-action identifier bound to a capability-specific provider primitive
  and failing closed when no primitive exists and the outcome is inconclusive.
- Recording an approval outside the transaction that changes state to
  `approved`, which could let approval and state diverge after a partial write.
- Claiming a measured latency result, or accepting the synchronous design,
  without validated percentile evidence.
- Adding an observation-phase queue, worker, or dead-letter queue instead of a
  bounded in-request read with transactional persistence.
- Treating Step Functions execution history as the audit record of record, or
  passing prepared-operation content through workflow state.
- Letting the durable workflow, rather than the `ApprovalService`, authorize an
  approval.
- Relying on IAM update and delete denial alone to claim an append-only or
  storage-level immutable ledger.
- Carrying executable operation data in the approval payload instead of
  rebinding to the stored operation hash and resolving hash-verified content.
- Using a best-effort write without conditional or transactional guards, which
  would let a retry or race overwrite newer state.
- Using Step Functions Express workflows, whose limited execution history and
  five-minute duration do not satisfy durable audit and long-lived approval
  waits.
- Reusing one approval to authorize a second independent execution.
- Depending on identical content to merge independent operations.

## References

- [Amazon DynamoDB service, account, and table quotas](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/ServiceQuotas.html)
- [AWS Step Functions service quotas](https://docs.aws.amazon.com/step-functions/latest/dg/service-quotas.html)
- [Amazon API Gateway quotas](https://docs.aws.amazon.com/apigateway/latest/developerguide/limits.html)
- [JSON Canonicalization Scheme, RFC 8785](https://www.rfc-editor.org/rfc/rfc8785)
