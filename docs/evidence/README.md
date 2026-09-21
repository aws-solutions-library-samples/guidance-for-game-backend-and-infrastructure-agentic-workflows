# E0 Synchronous-Observation Latency Validation (issue #412)

This directory holds the **disposable** validation spike for the intended E1
synchronous GameLift observation. It produces measured evidence for the latency
acceptance gate that keeps [ADR 0005](../adr/0005-persist-operations-and-recover-workflows.md)
in `Proposed`.

Nothing here deploys production infrastructure, grants provider write
permissions, or enables operations. Every provider call is read-only
(`describe`/`list`).

## The exact E1 observation

Inside one API Gateway HTTP API request, the E1 observation performs, in order:

1. **Three bounded, read-only GameLift provider reads** for one fleet, matching
   the GameLift specialist's read surface
   (`backend/src/agents/gamelift_specialist.py`):
   - `describe_fleet_utilization`
   - `describe_fleet_capacity`
   - `describe_scaling_policies`
2. **Representative persistence** of the observation state transition and its
   ledger events (the transactional write described in ADR 0005,
   "Durable observation state without queues"). This is exercised by an
   explicit persistence mode (see [Persistence modes](#persistence-modes-and-the-disposable-table)):
   the real **DynamoDB transactional** mode for live evidence, or a
   deterministic **in-memory** mode for unit tests only.
3. **Canonical serialization** of the persisted records with
   [RFC 8785 (JSON Canonicalization Scheme)](https://www.rfc-editor.org/rfc/rfc8785),
   using `operations.contracts.canonical` — the same canonicalization the
   published contracts use.

Model inference is explicitly excluded.

## Budgets and deadline enforcement

The API Gateway HTTP API **integration timeout has a documented ceiling of 30
seconds and cannot be raised** (see
[Amazon API Gateway quotas](https://docs.aws.amazon.com/apigateway/latest/developerguide/limits.html),
cited in ADR 0005). The harness allocates explicit sub-budgets that sum to
strictly less than that ceiling and reserve a cancellation margin
(`backend/src/operations/validation/e0_latency.py`, `DEFAULT_BUDGET`):

| Sub-budget | Value | Notes |
|---|---|---|
| Per provider read | 3.0 s | applied to each of the three reads individually |
| Persistence + canonical serialization | 3.0 s | one transactional write plus RFC 8785 serialization |
| Reads + persistence subtotal | 12.0 s | `3 × 3.0 + 3.0` |
| Cancellation margin | 3.0 s | reserved headroom below the gateway ceiling |
| **Total request deadline** | **15.0 s** | request aborts with a typed retryable error past this |
| Gateway integration ceiling | 30.0 s | documented, non-raisable |
| **Acceptance ceiling** (`ceiling − margin`) | **27.0 s** | the value a measured p99 must beat |

Each read is enforced with a **real wall-clock deadline**: the read runs in a
worker thread and the request waits at most the smaller of the per-read budget
and the remaining total budget. A read that blocks past that deadline is
**abandoned** — the request raises a typed **retryable** error
(`PROVIDER_UNAVAILABLE`) *before* the read returns and does **not** join the
still-running worker, so a hung provider read can never make the request (or the
harness) wait past its deadline. A read that returns but whose measured elapsed
time still exceeds its budget is likewise rejected.

To keep a stuck socket from silently consuming a read budget below the level the
thread deadline can observe, the live client also bounds botocore itself:
`connect_timeout` and `read_timeout` are set at or below the per-read budget and
retries are capped (`adaptive`, `max_attempts=3`). On any per-call, persistence,
or total overrun the request **fails closed**: in-flight work is abandoned, the
typed retryable error is raised, and no partial observation is ever returned as
success. These numbers are the spike's declared assumptions; the implementing
issue may tighten them.

## Persistence modes and the disposable table

The harness's original default persistence callable was a **no-op**: a live
provider-read measurement showed it measured only canonical serialization, not a
real durable write, so it did not meet ADR 0005's durable acceptance boundary.
The harness now selects persistence with `--persistence-mode`:

| Mode | Flag value | What it does | Acceptable as live evidence? |
|---|---|---|---|
| In-memory (deterministic) | `in-memory` (default) | Canonicalizes the record in-process; writes nothing | **No** — unit tests only |
| DynamoDB transactional (real) | `dynamodb-transactional` | One `TransactWriteItems` write of synthetic items | **Yes** — required for acceptance |

The evidence document states the **actual** mode in
`assumptions.persistence_mode` and `evaluation.persistence_mode`, and the
`evaluation.persistence_acceptable` flag is `true` only for the transactional
mode. **Synchronous acceptance is denied unless the run used the real
transactional mode AND the sample was clean** (`evaluation.rule` records this).

### What the real mode writes (synthetic only)

For each sample, in **one** `TransactWriteItems` call, the harness writes exactly
two items with conditional no-replacement semantics:

- an **operation-state** item (`PK = OP#<uuid4>`, `SK = STATE#0`), conditional on
  `attribute_not_exists(PK)` so it never overwrites an existing item; and
- an **append-only ledger** item (same `PK`, `SK = LEDGER#0`), conditional on
  `attribute_not_exists(SK)` so a ledger event is never overwritten.

Every value is synthetic and bounded: a per-sample-unique random `operation_id`,
a fixed synthetic `state`/`event_type`, a `sequence`, a `synthetic: true` marker,
the canonical byte length, an RFC 8785 `canonical_sha256` digest of each item,
and — when `--ttl-seconds` is set — a numeric `ttl` epoch-second attribute. The
harness **never** writes a fleet id, account id, ARN, or any provider payload,
and it issues **no read and no `Scan`** — only conditional puts. The persistence
deadline is enforced on the injected clock; a slow write fails closed with the
typed retryable `PROVIDER_UNAVAILABLE` error.

### Minimal table contract

The `--persistence-table` must be a **disposable, task-owned** table you create
and delete for this measurement. Minimal contract:

| Attribute / setting | Value |
|---|---|
| Partition key `PK` | String (`S`) |
| Sort key `SK` | String (`S`) |
| Billing mode | `PAY_PER_REQUEST` (on-demand) |
| TTL attribute (optional) | `ttl` (numeric epoch seconds) — enable if using `--ttl-seconds` |

Apply current guidance for a **disposable** validation table: on-demand billing,
least-privilege access (the run needs only `dynamodb:TransactWriteItems` on this
one table — no `Scan`, `Query`, or `GetItem`), synthetic data only, and a TTL
field for safe expiry of leftover rows. **PITR, encryption at rest with a managed
key, and access logging/metrics are required for the final production operations
table (ADR 0005) and are provisioned and reviewed separately — they are out of
scope for this disposable spike.**

### Safe creation and cleanup (non-production account)

Create the disposable table (on-demand, PK/SK, optional TTL):

```bash
aws dynamodb create-table \
  --table-name e0-latency-spike-disposable \
  --attribute-definitions AttributeName=PK,AttributeType=S AttributeName=SK,AttributeType=S \
  --key-schema AttributeName=PK,KeyType=HASH AttributeName=SK,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST \
  --profile <demo> --region us-west-2

# Optional: enable TTL on the 'ttl' attribute (matches --ttl-seconds).
aws dynamodb update-time-to-live \
  --table-name e0-latency-spike-disposable \
  --time-to-live-specification "Enabled=true,AttributeName=ttl" \
  --profile <demo> --region us-west-2
```

Delete it as soon as the measurement is captured:

```bash
aws dynamodb delete-table \
  --table-name e0-latency-spike-disposable \
  --profile <demo> --region us-west-2
```

Deleting a disposable table you created for this spike is safe; do not point
`--persistence-table` at any shared or production table.

## Concurrency arrival model

The harness measures under a **closed-loop** arrival model: `--concurrency`
worker threads each issue observations back-to-back with zero think time, a new
one starting only when the previous returns. This is a bounded-concurrency
model, **not** an open (Poisson-arrival) one, and it is **not** a model of
production request rate. A closed-loop p99 is a conservative, reproducible
stand-in for the single-request synchronous path; declaring the model is what
gives the measured percentile a defensible meaning. The model is recorded in the
evidence document at `assumptions.arrival_model`.

## Acceptance rule

> **Synchronous accepted** iff the run used the **real DynamoDB transactional
> persistence mode** (`evaluation.persistence_acceptable == true`) **and** the
> sample is a **clean run** (zero failures, zero timeouts, zero partial
> denials) **and** the measured **p99 ≤ acceptance ceiling**
> (`gateway_integration_timeout − cancellation_margin` = 27.0 s), over a
> representative sample. Percentiles use the **nearest-rank** method so a
> reported p99 is an actually-observed measurement, never an interpolation. A
> run under the in-memory (test-only) mode is denied regardless of its p99.

A single non-success sample denies acceptance regardless of the p99 over the
successful subset: the whole sample must be clean. The harness records sample
size, successes, failures, timeouts, **partial denials** (reported separately
from timeouts), and p50/p95/p99/max, plus a `clean_run` flag in the evaluation.
Its output conforms to
[`e0-latency-evidence.schema.json`](e0-latency-evidence.schema.json), which
forbids (via `additionalProperties: false`) any field that could carry an
account id, fleet id, ARN, credential, or provider payload. The measured fleet
appears only as a non-reversible short hash (`target_ref`).

## Reproducing the measurement (read-only)

Prerequisites: read-only credentials for a **non-production** account with at
least one **classic** GameLift fleet, and `uv`. Run all commands from the
repository root.

```bash
# Discover a classic fleet id (read-only). Container fleets do NOT support
# describe_fleet_utilization, so a classic (EC2 compute type) fleet is required.
aws gamelift list-fleets --profile <demo> --region us-west-2

# Run the harness against that fleet id (read-only; writes public-safe JSON).
# The harness itself pages list_fleets and filters to classic (EC2) fleets,
# excluding container fleets, when no --fleet-id is given.
PYTHONPATH=backend/src uv --project backend run python -m operations.validation.e0_harness \
  --profile <demo> --region us-west-2 \
  --fleet-id <classic-fleet-id> \
  --persistence-mode dynamodb-transactional \
  --persistence-table e0-latency-spike-disposable \
  --ttl-seconds 3600 \
  --samples 200 --concurrency 4 \
  --out docs/evidence/e0-latency-<YYYY-MM-DD>.json
```

Then scan the emitted document before publishing. The public-content checker
lives at the repository root and resolves paths relative to the repository, so
invoke it from the repository root (not from `backend/`):

```bash
python3 scripts/check_public_content.py docs/evidence/e0-latency-<YYYY-MM-DD>.json
# or scan every tracked text file:
python3 scripts/check_public_content.py
```

## Status: live measurement PENDING — do not accept the ADR yet

**The harness and its full test suite are complete and green**, but a
representative **live** measurement has **not** been produced, because it
requires a GameLift resource this read-only wave must not create.

Verified on the `demo` profile in `us-west-2` (read-only, `2026-09` wave):

- `aws gamelift list-fleets` returns **no classic fleets**.
- The only fleet present is a **container** fleet, and
  `describe_fleet_utilization` rejects it with
  `InvalidRequestException: Operation only supports Fleet resource`. The exact
  three-read observation therefore cannot run against the available fleet.
- Running the harness with no `--fleet-id` exits `3` and prints the remaining
  live step rather than fabricating a measurement.

### Exact remaining live step

Provision (or point at) **one classic GameLift fleet** in a non-production
account, create a disposable, task-owned, on-demand persistence table (see
[Persistence modes](#persistence-modes-and-the-disposable-table)), then run the
reproduction command above with that fleet's id, `--persistence-mode
dynamodb-transactional`, and `--persistence-table` to capture
`e0-latency-<date>.json`. Provisioning a fleet and a table are AWS mutations and
are **out of scope for this read-only wave**; they are the remaining actions
before ADR 0005 can be evaluated. A run under the in-memory (test-only)
persistence mode is **not** acceptance evidence.

Until that live evidence exists and is reviewed:

- ADR 0005 stays **Proposed**.
- Issue #279's remaining acceptance checkbox stays open.

No measured percentile is asserted anywhere in this spike. A synthetic timing
test is **not** the acceptance measurement (issue #412, Out of Scope).
