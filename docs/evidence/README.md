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
   "Durable observation state without queues").
3. **Canonical serialization** of the persisted records with
   [RFC 8785 (JSON Canonicalization Scheme)](https://www.rfc-editor.org/rfc/rfc8785),
   using `operations.contracts.canonical` — the same canonicalization the
   published contracts use.

Model inference is explicitly excluded.

## Budgets

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

Each read is issued with its own deadline; the whole request has a total
deadline of 15.0 s. On any per-call, persistence, or total overrun the request
**fails closed**: in-flight work is abandoned, a typed **retryable** error
(`PROVIDER_UNAVAILABLE`) is raised, and no partial observation is ever returned
as success. These numbers are the spike's declared assumptions; the implementing
issue may tighten them.

## Acceptance rule

> **Synchronous accepted** iff the measured **p99 ≤ acceptance ceiling**
> (`gateway_integration_timeout − cancellation_margin` = 27.0 s), over a
> representative sample. Percentiles use the **nearest-rank** method so a
> reported p99 is an actually-observed measurement, never an interpolation.

The harness records sample size, successes, failures, timeouts, and
p50/p95/p99/max. Its output conforms to
[`e0-latency-evidence.schema.json`](e0-latency-evidence.schema.json), which
forbids (via `additionalProperties: false`) any field that could carry an
account id, fleet id, ARN, credential, or provider payload. The measured fleet
appears only as a non-reversible short hash (`target_ref`).

## Reproducing the measurement (read-only)

Prerequisites: read-only credentials for a **non-production** account with at
least one **classic** GameLift fleet, and `uv`.

```bash
cd backend

# Discover a classic fleet id (read-only). Container fleets do NOT support
# describe_fleet_utilization, so a classic fleet is required.
aws gamelift list-fleets --profile <demo> --region us-west-2

# Run the harness against that fleet id (read-only; writes public-safe JSON).
PYTHONPATH=src uv run python -m operations.validation.e0_harness \
  --profile <demo> --region us-west-2 \
  --fleet-id <classic-fleet-id> \
  --samples 200 --concurrency 4 \
  --out ../docs/evidence/e0-latency-<YYYY-MM-DD>.json
```

Then scan the emitted document before publishing:

```bash
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
account, then run the reproduction command above with that fleet's id to
capture `e0-latency-<date>.json`. Provisioning a fleet is an AWS mutation and is
**out of scope for this read-only wave**; it is the single remaining action
before ADR 0005 can be evaluated.

Until that live evidence exists and is reviewed:

- ADR 0005 stays **Proposed**.
- Issue #279's remaining acceptance checkbox stays open.

No measured percentile is asserted anywhere in this spike. A synthetic timing
test is **not** the acceptance measurement (issue #412, Out of Scope).
